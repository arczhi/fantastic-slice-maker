"""成片质检：全自动流程里这是最后一道闸，不合格要能自己发现并给出修正建议。"""
from __future__ import annotations

from . import ffmpeg
from .config import RenderCfg
from .util import get_logger

log = get_logger("qc")


def check(path: str, cfg: RenderCfg, expect_audio: bool = True) -> dict:
    issues: list[str] = []
    info = ffmpeg.probe(path)

    if info.duration < 22:
        issues.append(f"太短: {info.duration:.1f}s")
    if info.duration > 150:
        issues.append(f"太长: {info.duration:.1f}s")
    if (info.width, info.height) != (cfg.width, cfg.height):
        issues.append(f"分辨率异常: {info.width}x{info.height}，期望 {cfg.width}x{cfg.height}")
    if expect_audio and not info.has_audio:
        issues.append("无音轨")

    blacks = ffmpeg.detect_black(path)
    long_blacks = [b for b in blacks if b[1] - b[0] > 0.9]
    if long_blacks:
        issues.append(f"黑场 {len(long_blacks)} 处，最长 {max(b[1]-b[0] for b in long_blacks):.1f}s")

    vol = -99.0
    if info.has_audio:
        vol = ffmpeg.mean_volume(path)
        if vol < -42:
            issues.append(f"音量过低: {vol:.1f}dB")

    result = {
        "ok": not issues,
        "issues": issues,
        "duration": round(info.duration, 2),
        "resolution": f"{info.width}x{info.height}",
        "mean_volume_db": round(vol, 1),
        "black_segments": len(blacks),
    }
    if issues:
        log.warning(f"QC 未通过: {'; '.join(issues)}")
    else:
        log.info(f"QC 通过: {result['duration']}s {result['resolution']} {vol:.1f}dB")
    return result
