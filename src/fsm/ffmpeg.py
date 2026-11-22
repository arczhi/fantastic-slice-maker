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


_encoders_cache: set[str] | None = None


def has_encoder(name: str) -> bool:
    """本机 ffmpeg 是否带某个编码器。结果缓存，批量时只探测一次。"""
    global _encoders_cache
    if _encoders_cache is None:
        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, timeout=20)
            text = p.stdout.decode("utf-8", "replace")
            _encoders_cache = {
                m.group(1) for m in re.finditer(r"^\s*V\S{5}\s+(\S+)", text, re.M)
            }
        except (OSError, subprocess.SubprocessError):
            _encoders_cache = set()
    return name in _encoders_cache


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


def _fills_canvas(src_w: int, src_h: int, width: int, height: int) -> bool:
    """竖屏源（如 480x854、360x640）按宽度铺满后，前景是否已经盖住整块画布。

    这类源用 scale=1080:-2 放大后高度正好等于或略超 1920，模糊层被前景整块
    遮住、一个像素也看不到。实测单条 45s 切片里，全分辨率 gblur 占 39% 的
    CPU（58.7 CPU 秒 → 34.0 CPU 秒）却零收益，所以这种情况直接跳过模糊。
    """
    if src_w <= 0 or src_h <= 0:
        return False
    return width * src_h / src_w >= height


def _dewatermark_prefix(dw, src_size: tuple[int, int] | None,
                        src: str | Path, width: int = 1080,
                        height: int = 1920) -> tuple[str, tuple[int, int] | None]:
    """构建去水印前置滤镜，返回 (滤镜前缀, 处理后的有效尺寸)。

    单独抽出来是因为裁切会改变源画面比例。crop 模式会把裁切框一次算准
    （顶部一条 + 左右各裁一点补回目标宽高比），这样后续直接缩放就能铺满画布，
    不必让 swscale 用 force_original_aspect_ratio 每帧现算比例——那条路实测
    慢 50%（45 秒切片 7.68s vs 4.97s），而精确裁切只要 3.06s，比不去水印还快。
    """
    if dw is None:
        return "", src_size

    # YAML 1.1 把裸写的 off/on 解析成布尔，配置里没加引号就会拿到 False，
    # 这里统一归一化，避免调用方被这种细节绊住。
    mode = getattr(dw, "mode", "off")
    if mode is False or mode is None:
        mode = "off"
    mode = str(mode).lower()
    if mode == "off":
        return "", src_size

    size = src_size
    if size is None:
        # 没传尺寸时探一次；切片阶段本来就知道尺寸，这里只是兜底。
        try:
            info = probe(src)
            size = (info.width, info.height)
        except Exception:  # pragma: no cover - 探测失败就不去水印，别让出片中段
            return "", src_size

    if mode == "crop":
        ratio = float(getattr(dw, "crop_ratio", 0.0) or 0.0)
        if ratio <= 0 or size is None:
            return "", size
        sw, sh = size
        top = int(sh * ratio) // 2 * 2             # yuv420p 要求偶数
        if top <= 0 or top >= sh:
            return "", size
        ch = sh - top
        # 裁掉顶部后画面变宽，再按目标宽高比裁掉左右，把比例补回 9:16
        cw = max(2, int(ch * width / height) // 2 * 2)
        if cw > sw:                                  # 源本来就窄，别裁到反向外扩
            cw = sw // 2 * 2
        if cw >= sw * 0.8:
            cx = (sw - cw) // 2 // 2 * 2
            return f"crop={cw}:{ch}:{cx}:{top},", (cw, ch)
        # 侧裁会丢掉两成以上宽度（横屏源按 9:16 裁更是要砍掉大半），不值当。
        # 此时只去掉顶部水印，剩下的交给模糊填充路径：整幅画面都在，只是上下补模糊。
        return f"crop=iw:ih-{top}:0:{top},", (sw, ch)

    if mode == "delogo":
        w, h = int(getattr(dw, "w", 0) or 0), int(getattr(dw, "h", 0) or 0)
        if w <= 0 or h <= 0:
            return "", size
        x, y = int(dw.x or 0), int(dw.y or 0)
        # delogo 的 x/y/w/h 需落在画面内，越界会直接报错
        if size is not None:
            x = max(0, min(x, size[0] - 2))
            y = max(0, min(y, size[1] - 2))
            w = min(w, size[0] - x - 1)
            h = min(h, size[1] - y - 1)
        return f"delogo=x={x}:y={y}:w={w}:h={h},", size

    return "", size


def cut(
    src: str | Path, dst: str | Path, start: float, end: float,
    *, width: int, height: int, fps: int, crf: int, preset: str,
    loudness_i: float, blur_sigma: int, audio_bitrate: str,
    timeout: int = 600, has_audio: bool = True,
    subs: list[dict] | None = None, threads: int = 3,
    src_size: tuple[int, int] | None = None, blur_div: int = 4,
    encoder: str = "libx264", vt_bitrate: str = "6M",
    dewatermark=None, fit: str = "auto",
) -> Path:
    """精确切片 + 竖屏改装（上下模糊，保留原画面完整不被裁切）。

    关键点：原片已烧字幕，绝不能裁切画面，否则字幕会被切掉。
    所以用「模糊背景铺满 + 原比例居中」而不是 force_original_aspect_ratio=increase + crop。

    blur_div：模糊在 1/blur_div 分辨率上做，再放大到画布尺寸。模糊本身就会
    抹掉细节，1/4 分辨率上做 sigma/4 再放大，肉眼看不出差别（抽帧对比过），
    但 CPU 从 58.7 秒降到 34.0 秒。竖屏源前景本来就铺满画布，进一步跳过整层。

    subs 是自动补的字幕条 [{png, start, end, y}]，start/end 为**段内相对时间**。
    用 overlay + enable 叠加静态 PNG，和切片同一次编码完成，不额外损失画质。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.2, end - start)
    subs = subs or []

    pre, eff_size = _dewatermark_prefix(dewatermark, src_size, src, width, height)
    fills = bool(eff_size) and _fills_canvas(eff_size[0], eff_size[1], width, height)
    if fills:
        if pre:
            # 前置裁切一般已把画面裁成目标比例，这时直接缩放最准也最快。
            # 但源比目标更窄时（如 864x1920 这种超长竖屏）侧裁补不回 9:16，
            # 直接 scale=w:h 会把画面横向拉宽，所以退回「按宽缩放 + 居中裁」。
            ew, eh = eff_size
            if abs(ew / eh - width / height) < 0.01:
                vf = pre + f"scale={width}:{height}[base]"
            else:
                vf = pre + f"scale={width}:-2,crop={width}:{height}[base]"
        else:
            # 前景即全屏：只做等比缩放 + 居中裁到画布尺寸，不建模糊层
            vf = (
                f"scale={width}:-2:force_original_aspect_ratio=decrease,"
                f"crop={width}:{height}[base]"
            )
    elif str(fit).lower() == "crop" and eff_size:
        # 横屏源铺满竖屏画布：按目标比例居中裁，再缩放到画布。
        # 先裁后缩比「放大填满再裁」少搬运大量像素，实测快一个档。
        ew, eh = eff_size
        ch = eh // 2 * 2
        cw = max(2, int(ch * width / height) // 2 * 2)   # yuv420p 要偶数
        if cw > ew:                                      # 源比目标还窄，退化为不裁
            cw = ew // 2 * 2
        cx = (ew - cw) // 2 // 2 * 2
        vf = pre + f"crop={cw}:{ch}:{cx}:0,scale={width}:{height}[base]"
    else:
        div = max(1, blur_div)
        bw = max(2, (width // div) // 2 * 2)
        bh = max(2, (height // div) // 2 * 2)
        bsigma = max(1.0, blur_sigma / div)
        vf = (
            pre
            + f"split=2[bg][fg];"
            f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},gblur=sigma={bsigma:.2f},"
            f"scale={width}:{height}:flags=bicubic[bgb];"
            f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]"
        )
    cur = "base"
    for i, s in enumerate(subs):
        nxt = f"sub{i}"
        vf += (
            f";[{cur}][{i + 1}:v]overlay=0:{int(s['y'])}:"
            f"enable='between(t,{s['start']:.3f},{s['end']:.3f})'[{nxt}]"
        )
        cur = nxt
    vf += f";[{cur}]fps={fps},format=yuv420p[v]"

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
    ]
    for s in subs:
        cmd += ["-i", str(s["png"])]
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
    if encoder == "h264_videotoolbox":
        # 硬件编码走独立媒体引擎，不占 CPU；代价是只能定码率，画质随内容波动。
        enc = [
            "-c:v", "h264_videotoolbox", "-b:v", vt_bitrate,
            "-maxrate", vt_bitrate, "-bufsize", "12M",
        ]
    else:
        enc = [
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-threads", str(max(1, threads)),
        ]
    cmd += enc + [
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
