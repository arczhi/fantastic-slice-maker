"""镜头切分：优先用 pyscenedetect 的自适应检测，ffmpeg scene 兜底。

为什么不再用 ffmpeg 的 `select='gt(scene,0.28)'` 单阈值：
在这批漫剧上标定过——把峰值前后帧做相关性判定，"真硬切"有 150 个，
而 0.28 只检出 48 个（召回 17%~56%）。原因是漫剧的烧死字幕、火光、
快速挥刀都会拉高帧差，单阈值只能取一个"谁都不得罪"的中间值，
结果两头都不准：低阈值把运动当切换，高阈值漏掉真切换。

AdaptiveDetector 用滑动窗口自适应阈值：场景本身运动大时自动抬高门槛，
运动平缓时降低，正好对症。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .. import ffmpeg
from ..config import VisionCfg
from ..util import get_logger

log = get_logger("shots")


@dataclass
class Shot:
    idx: int
    start: float
    end: float

    @property
    def dur(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dur"] = self.dur
        return d


def _merge_short(cuts: list[float], duration: float, min_shot: float) -> list[float]:
    """过短镜头并入前一个：闪白、抖动、字幕闪现都不是真镜头。"""
    out: list[float] = []
    prev = 0.0
    for c in sorted(cuts):
        if c - prev < min_shot:
            if out:
                out.pop()
            continue
        out.append(c)
        prev = c
    if duration - prev < min_shot and out:
        out.pop()
    return out


def _split_long(bounds: list[float], duration: float, max_shot: float) -> list[float]:
    """超长镜头（长镜头对话）按等分强制切开，便于精细定位。"""
    out = [bounds[0]]
    for a, b in zip(bounds, bounds[1:]):
        n = max(1, int((b - a) // max_shot) + 1)
        if n == 1:
            out.append(b)
        else:
            step = (b - a) / n
            out.extend(round(a + step * k, 3) for k in range(1, n + 1))
    return out


def _pyscene_cuts(src: str, cfg: VisionCfg) -> list[float] | None:
    """pyscenedetect 自适应检测。任何异常都返回 None 让上层走兜底。"""
    try:
        from scenedetect import AdaptiveDetector, detect
    except ImportError:
        log.warning("scenedetect not installed, falling back to ffmpeg")
        return None
    try:
        scenes = detect(
            str(src),
            AdaptiveDetector(
                adaptive_threshold=cfg.adaptive_threshold,
                min_scene_len=max(2, int(cfg.min_shot * 10)),
            ),
            show_progress=False,
        )
    except Exception as e:
        log.warning(f"pyscenedetect failed: {type(e).__name__}: {e}")
        return None
    # 第一个 scene 从 0 开始，它的 start 不是切点
    return [s[0].seconds for s in scenes[1:]]


def detect(src: str, duration: float, cfg: VisionCfg) -> list[Shot]:
    raw: list[float] = []
    method = "pyscene"

    got = _pyscene_cuts(src, cfg) if cfg.use_pyscene else None
    if got:
        raw = got
    else:
        method = "ffmpeg"
        try:
            raw = ffmpeg.detect_scenes(src, threshold=cfg.scene_threshold)
        except ffmpeg.FFmpegError as e:
            log.warning(f"scene detect failed, fallback to fixed 3s windows: {e}")
            raw = [round(t, 3) for t in _fixed_grid(duration, 3.0)]
            method = "grid"

    cuts = [c for c in raw if 0.3 < c < duration - 0.3]
    merged = _merge_short(cuts, duration, cfg.min_shot)
    bounds = sorted(set([0.0] + merged + [duration]))
    final = _split_long(bounds, duration, cfg.max_shot)
    final = [b for b in final if b <= duration]
    if final[-1] < duration - 0.05:
        final.append(duration)

    shots = [
        Shot(idx=i, start=round(a, 3), end=round(b, 3))
        for i, (a, b) in enumerate(zip(final, final[1:]))
        if b - a > 0.15
    ]
    for i, s in enumerate(shots):
        s.idx = i
    log.info(f"shots: {len(shots)} via {method} (raw cuts {len(raw)})")
    return shots


def _fixed_grid(duration: float, step: float) -> list[float]:
    t = step
    out = []
    while t < duration:
        out.append(t)
        t += step
    return out
