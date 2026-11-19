"""FFmpeg 执行层：切片 → 竖屏改装 → 响度归一 → 拼接。

竖屏方案说明：原片已烧死中文字幕，任何「放大裁切」都会把字幕切掉，
所以用「模糊背景铺满 + 原比例居中」，字幕 100% 保留。这也是大量漫剧号
在用的做法，观感可接受且零风险。
"""
from __future__ import annotations

from pathlib import Path

from . import ffmpeg
from .config import RenderCfg
from .util import ensure_dir, get_logger

log = get_logger("render")

_ROLE_ORDER = {"hook": 0, "setup": 1, "rise": 2, "climax": 3, "cliff": 4}


def _is_content(s: dict) -> bool:
    """判断这个采样点是不是「有内容的画面」。

    注意黑底白字的片尾字幕卡：它 std 很高（白字拉高方差），只看方差会被
    误判成内容。真正的判据是平均亮度——纯黑卡 bright 只有个位数。
    """
    return 22 <= s["bright"] <= 246 and s["std"] >= 8.0


def trim_blank(beats: list[dict], vis: dict, min_dur: float = 1.5,
               pad: float = 0.12, max_gap: float = 0.9) -> list[dict]:
    """剪掉片段首尾的黑场/白场，并在中途出现长黑场处截断。

    漫剧片尾常有几秒纯黑字幕卡；段与段之间也可能有 1-5 秒的全黑过渡。
    这些进成片会直接毁掉节奏，所以在渲染前用视觉采样自己切掉，
    而不是等 QC 事后发现。
    """
    fps = float(vis.get("fps") or 4.0)
    samples = vis.get("samples") or []
    if not samples:
        return beats

    step = 1.0 / fps
    limit = max(1, int(round(max_gap * fps)))
    out: list[dict] = []

    for b in beats:
        i0 = max(0, int(b["start"] * fps))
        i1 = min(len(samples), int(b["end"] * fps))
        seg = samples[i0:i1]
        if not seg:
            continue

        first = next((k for k, s in enumerate(seg) if _is_content(s)), None)
        if first is None:
            log.warning(f"  drop blank beat {b['role']} {b['start']:.1f}-{b['end']:.1f}s")
            continue

        last = first
        bad_run = 0
        for k in range(first, len(seg)):
            if _is_content(seg[k]):
                bad_run = 0
                last = k
            else:
                bad_run += 1
                if bad_run >= limit:
                    break

        new_start = max(b["start"], seg[first]["t"] - pad)
        new_end = min(b["end"], seg[last]["t"] + step + pad)
        if new_end - new_start < min_dur:
            log.warning(f"  drop too-short beat {b['role']} "
                        f"{new_start:.1f}-{new_end:.1f}s")
            continue
        if abs(new_start - b["start"]) > 0.05 or abs(new_end - b["end"]) > 0.05:
            log.info(f"  trim {b['role']}: {b['start']:.1f}-{b['end']:.1f} -> "
                     f"{new_start:.1f}-{new_end:.1f}")
        out.append({**b, "start": round(new_start, 3), "end": round(new_end, 3),
                    "dur": round(new_end - new_start, 3)})
    return out


def snap_to_shots(beats: list[dict], shots: list[dict], tol: float = 0.65) -> list[dict]:
    """把切点吸附到镜头边界，避免切在人脸中间或动作一半。

    只吸附到「更靠内」的边界：start 取不晚于原值的边界，end 取不早于原值的边界，
    这样只会让片段变长一点，不会把台词切掉。
    """
    starts = [s["start"] for s in shots]
    ends = [s["end"] for s in shots]
    out = []
    for b in beats:
        s, e = b["start"], b["end"]
        near_s = [x for x in starts if abs(x - s) <= tol and x <= s]
        near_e = [x for x in ends if abs(x - e) <= tol and x >= e]
        if near_s:
            s = max(near_s)
        if near_e:
            e = min(near_e)
        if e - s < 1.2:
            s, e = b["start"], b["end"]
        out.append({**b, "start": round(s, 3), "end": round(e, 3), "dur": round(e - s, 3)})
    return out


def order_beats(beats: list[dict]) -> list[dict]:
    """hook 可以来自片中任意位置，所以成片顺序按角色排，不按时间排。"""
    return sorted(beats, key=lambda b: (_ROLE_ORDER.get(b["role"], 2), b["start"]))


def render(
    src: str, beats: list[dict], work_dir: Path, cfg: RenderCfg,
    *, has_audio: bool = True, tag: str = "reel",
    src_size: tuple[int, int] | None = None,
) -> dict:
    """各段独立切片并行渲染。

    x264 自己就吃多核，所以并发 3 段 + 每段限 3 线程，比串行快 2 倍以上，
    又不会把 10 核机器打满导致 ASR 阶段饿死。

    src_size 传源画面宽高，用来判断竖屏源是否值得建模糊层（见 ffmpeg.cut）。
    """
    from concurrent.futures import ThreadPoolExecutor

    work_dir = ensure_dir(work_dir)
    ordered = order_beats(beats)
    clips: list[Path] = []

    def render_one(pair):
        i, b = pair
        dst = work_dir / f"part_{i:02d}_{b['role']}.mp4"
        ffmpeg.cut(
            src, dst, b["start"], b["end"],
            width=cfg.width, height=cfg.height, fps=cfg.fps,
            crf=cfg.crf, preset=cfg.preset,
            loudness_i=cfg.loudness_i, blur_sigma=cfg.blur_sigma,
            audio_bitrate=cfg.audio_bitrate, timeout=cfg.timeout,
            has_audio=has_audio,
            # 自动补的字幕挂在 beat 上（pipeline 已按最终顺序算好段内时间）
            subs=b.get("subs"),
            threads=cfg.threads,
            src_size=src_size,
            blur_div=cfg.blur_div,
        )
        log.info(f"  clip {i}: {b['role']:7s} {b['start']:.1f}-{b['end']:.1f}s -> {dst.name}")
        return dst

    jobs = list(enumerate(ordered))
    with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as pool:
        clips = list(pool.map(render_one, jobs))

    final = work_dir / f"{tag}.mp4"
    if len(clips) == 1:
        ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-i", str(clips[0]),
                    "-c", "copy", "-movflags", "+faststart", str(final)],
                   timeout=cfg.timeout)
    else:
        ffmpeg.concat(clips, final, timeout=cfg.timeout)

    info = ffmpeg.probe(final)
    log.info(f"reel: {final.name} {info.duration:.1f}s {info.width}x{info.height}")
    return {
        "path": str(final), "duration": round(info.duration, 2),
        "width": info.width, "height": info.height,
        "clips": [str(c) for c in clips],
        "timeline": [
            {"role": b["role"], "src_start": b["start"], "src_end": b["end"],
             "dur": b["dur"], "text": b.get("text", "")[:60]}
            for b in ordered
        ],
    }
