"""视觉信号：帧差运动、中心区运动、亮度、画面复杂度。

全部在 160x90 灰度上算，4 分钟视频约 1000 帧，耗时不到 1 秒。
这批数字是后面规则召回的地基，也是 OCR 是否需要重新识别的判据来源。
"""
from __future__ import annotations

import numpy as np

from .. import ffmpeg
from ..config import VisionCfg
from ..util import get_logger

log = get_logger("vision")


def analyze(src: str, duration: float, cfg: VisionCfg) -> dict:
    """返回逐采样点的视觉特征，以及按秒聚合的摘要。"""
    samples: list[dict] = []
    prev: np.ndarray | None = None
    h, w = cfg.height, cfg.width
    cy0, cy1 = int(h * 0.22), int(h * 0.78)
    cx0, cx1 = int(w * 0.25), int(w * 0.75)

    for t, frame in ffmpeg.iter_gray_frames(src, cfg.fps, w, h):
        f = frame.astype(np.int16)
        if prev is None:
            motion = 0.0
            center = 0.0
        else:
            diff = np.abs(f - prev)
            motion = float(diff.mean())
            center = float(diff[cy0:cy1, cx0:cx1].mean())
        samples.append({
            "t": round(t, 3),
            "motion": round(motion, 2),
            "center": round(center, 2),
            "bright": int(frame.mean()),
            "std": round(float(frame.std()), 2),
        })
        prev = f

    if not samples:
        return {"samples": [], "fps": cfg.fps, "blank_ratio": 1.0}

    stds = np.array([s["std"] for s in samples])
    brights = np.array([s["bright"] for s in samples])
    motions = np.array([s["motion"] for s in samples])

    blank = (stds < cfg.blank_std)
    too_bright = brights > 248
    blank_ratio = float((blank | too_bright).mean())

    log.info(
        f"vision: {len(samples)} samples, motion p50={np.percentile(motions,50):.1f} "
        f"p95={np.percentile(motions,95):.1f}, blank={blank_ratio:.2%}"
    )
    return {
        "samples": samples,
        "fps": cfg.fps,
        "blank_ratio": round(blank_ratio, 4),
        "motion_p95": round(float(np.percentile(motions, 95)), 2),
        "motion_max": round(float(motions.max()), 2),
        "duration": duration,
    }


def slice_stats(vis: dict, start: float, end: float) -> dict:
    """取某个时间区间内的视觉统计，供 shot 聚合使用。"""
    fps = vis.get("fps") or 1.0
    samples = vis.get("samples") or []
    if not samples:
        return {"motion": 0.0, "center": 0.0, "std": 0.0, "bright": 0.0, "blank": True}
    i0 = max(0, int(start * fps))
    i1 = min(len(samples), max(i0 + 1, int(end * fps)))
    seg = samples[i0:i1]
    if not seg:
        return {"motion": 0.0, "center": 0.0, "std": 0.0, "bright": 0.0, "blank": True}
    motion = float(np.mean([s["motion"] for s in seg]))
    center = float(np.mean([s["center"] for s in seg]))
    std = float(np.mean([s["std"] for s in seg]))
    bright = float(np.mean([s["bright"] for s in seg]))
    return {
        "motion": round(motion, 2),
        "center": round(center, 2),
        "std": round(std, 2),
        "bright": round(bright, 1),
        "blank": bool(std < 8.0 or bright > 246),
    }
