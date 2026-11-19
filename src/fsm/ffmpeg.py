"""ffmpeg / ffprobe 封装。所有外部进程调用都要有超时，失败抛 FFmpegError。"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


class FFmpegError(RuntimeError):
    pass


def _bin(name: str) -> str:
    return name


def run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"timeout after {timeout}s: {' '.join(args[:6])}") from e
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")[-1200:]
        raise FFmpegError(f"rc={p.returncode} cmd={' '.join(args[:6])}\n{err}")
    return p


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    vcodec: str
    acodec: str

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width


def probe(path: str | Path) -> MediaInfo:
    p = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60)
    d = json.loads(p.stdout.decode("utf-8", "replace"))
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise FFmpegError(f"no video stream: {path}")

    duration = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    fps = 25.0
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = v.get(key) or ""
        if "/" in raw:
            n, _, m = raw.partition("/")
            try:
                if float(m):
                    fps = float(n) / float(m)
                    break
            except ValueError:
                pass
    return MediaInfo(
        path=str(path), duration=duration,
        width=int(v.get("width") or 0), height=int(v.get("height") or 0),
        fps=fps or 25.0,
        has_audio=a is not None,
        vcodec=v.get("codec_name", ""), acodec=(a or {}).get("codec_name", ""),
    )


def extract_audio(src: str | Path, dst: str | Path, sr: int = 16000) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-v", "error", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst),
    ], timeout=600)
    return dst


_SCENE_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_scenes(src: str | Path, threshold: float = 0.28, timeout: int = 900) -> list[float]:
    """返回镜头切换时间点（不含 0）。用 ffmpeg 内置 scene score，无需额外依赖。"""
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as f:
        meta_path = f.name
    try:
        run([
            "ffmpeg", "-v", "error", "-i", str(src),
            "-filter_complex",
            f"select='gt(scene,{threshold})',metadata=print:file={meta_path}",
            "-an", "-f", "null", "-",
        ], timeout=timeout)
        text = Path(meta_path).read_text(encoding="utf-8", errors="replace")
    finally:
        Path(meta_path).unlink(missing_ok=True)
    return sorted({round(float(m), 3) for m in _SCENE_RE.findall(text)})


def iter_gray_frames(
    src: str | Path, fps: float, width: int, height: int, timeout: int = 900,
) -> Iterator[tuple[float, np.ndarray]]:
    """以固定 fps / 尺寸流式读取灰度帧，避免一次性把整片解码进内存。"""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(src),
        "-vf", f"fps={fps},scale={width}:{height}",
        "-pix_fmt", "gray", "-f", "rawvideo", "-",
    ]
    frame_size = width * height
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf or len(buf) < frame_size:
                break
            yield idx / fps, np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
            idx += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def iter_color_frames(
    src: str | Path, fps: float, width: int, height: int, timeout: int = 1800,
) -> Iterator[tuple[float, np.ndarray]]:
    """彩色帧流，统一尺寸并加黑边补位，保证字幕带位置可预测。"""
    vf = (
        f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(src),
        "-vf", vf, "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
    ]
    frame_size = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf or len(buf) < frame_size:
                break
            yield idx / fps, np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
            idx += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def cut(
    src: str | Path, dst: str | Path, start: float, end: float,
    *, width: int, height: int, fps: int, crf: int, preset: str,
    loudness_i: float, blur_sigma: int, audio_bitrate: str,
    timeout: int = 600, has_audio: bool = True,
) -> Path:
    """精确切片 + 竖屏改装（上下模糊，保留原画面完整不被裁切）。

    关键点：原片已烧字幕，绝不能裁切画面，否则字幕会被切掉。
    所以用「模糊背景铺满 + 原比例居中」而不是 force_original_aspect_ratio=increase + crop。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.2, end - start)

    vf = (
        f"split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma={blur_sigma}[bgb];"
        f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        f"fps={fps},format=yuv420p[v]"
    )
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
    ]
    if has_audio:
        cmd += [
            "-filter_complex", f"[0:v]{vf};[0:a]aresample=async=1,loudnorm=I={loudness_i}:TP=-1.5:LRA=11[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2",
        ]
    else:
        cmd += [
            "-filter_complex", f"[0:v]{vf}",
            "-map", "[v]", "-an",
        ]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-threads", "3",
        "-r", str(fps), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dst),
    ]
    run(cmd, timeout=timeout)
    actual = probe(dst).duration
    if actual < 0.1:
        raise FFmpegError(f"produced empty clip: {dst}")
    return dst


def concat(clips: list[Path], dst: str | Path, timeout: int = 900) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{Path(c).resolve()}'\n")
        list_path = f.name
    try:
        run([
            "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", "-movflags", "+faststart", str(dst),
        ], timeout=timeout)
    finally:
        Path(list_path).unlink(missing_ok=True)
    return dst


def detect_black(src: str | Path, timeout: int = 300) -> list[tuple[float, float]]:
    p = subprocess.run([
        "ffmpeg", "-v", "info", "-i", str(src),
        "-vf", "blackdetect=d=0.1:pix_th=0.10", "-an", "-f", "null", "-",
    ], capture_output=True, timeout=timeout)
    out = p.stderr.decode("utf-8", "replace")
    spans = []
    for m in re.finditer(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)", out):
        spans.append((float(m.group(1)), float(m.group(2))))
    return spans


def mean_volume(src: str | Path, timeout: int = 300) -> float:
    p = subprocess.run([
        "ffmpeg", "-v", "info", "-i", str(src),
        "-af", "volumedetect", "-f", "null", "-",
    ], capture_output=True, timeout=timeout)
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", p.stderr.decode("utf-8", "replace"))
    return float(m.group(1)) if m else -99.0
