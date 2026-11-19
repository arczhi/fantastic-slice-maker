"""字幕 OCR 信号（PP-OCRv4）。

为什么不用 Unlimited-OCR：它是 3B MoE 文档模型，最佳部署是 vLLM + CUDA，
按 0.5s 一帧跑一天几十小时的素材是小时级开销。本任务只需识别白字黑边的
台词条，PP-OCRv4 det+rec ONNX 在 CPU 上单帧约 90ms，模型本机已有。

两个实测结论决定了这里的实现：
1) 裁字幕带反而更慢（det 会把小图放大到固定边长），所以送整帧再按框位置过滤。
2) 相邻帧字幕带几乎不变时复用上次结果，实际引擎调用量约为总帧数的 20-40%。
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .. import ffmpeg
from ..config import OCRCfg
from ..util import get_logger

log = get_logger("ocr")


def _load_engine(cfg: OCRCfg):
    if cfg.backend != "ppocr":
        raise NotImplementedError(f"ocr backend '{cfg.backend}' not wired yet")
    from rapidocr_onnxruntime import RapidOCR

    kwargs: dict[str, Any] = {}
    for key, path in (
        ("det_model_path", cfg.det_model),
        ("rec_model_path", cfg.rec_model),
        ("cls_model_path", cfg.cls_model),
    ):
        if path and Path(path).expanduser().exists():
            kwargs[key] = str(Path(path).expanduser())
    if len(kwargs) < 2:
        log.warning("local PP-OCR models missing, falling back to bundled defaults")
        kwargs = {}
    else:
        log.info(f"OCR engine: PP-OCRv4 local ({len(kwargs)} models)")
    return RapidOCR(**kwargs)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 1.0 if a == b else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _clean(raw: str) -> str:
    return "".join(ch for ch in str(raw) if ch not in "\u3000 ").strip()


def _pick_subtitle_lines(res, frame_h: int, y_from: float) -> tuple[str, float]:
    """从整帧识别结果里挑出字幕行的文本。

    box 是四点坐标；用中心 y 判断是否落在字幕区，并按 y 排序拼接，
    这样能滤掉台标、水印和画面里的招牌字。
    """
    lines: list[tuple[float, str, float]] = []
    cutoff = frame_h * y_from
    for item in (res or []):
        try:
            box = item[0]
            txt = _clean(item[1])
            score = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not txt:
            continue
        try:
            ys = [float(p[1]) for p in box]
            cy = sum(ys) / len(ys)
            h = max(ys) - min(ys)
        except (TypeError, ValueError, IndexError):
            continue
        if cy < cutoff:
            continue
        if h < frame_h * 0.025:      # 太小的字不是台词
            continue
        lines.append((cy, txt, score))
    if not lines:
        return "", 0.0
    lines.sort(key=lambda x: x[0])
    text = "".join(t for _, t, _ in lines)
    conf = float(np.mean([s for _, _, s in lines]))
    return text, round(conf, 3)


def extract(src: str, cfg: OCRCfg, duration: float = 0.0) -> list[dict]:
    """返回字幕条 [{start, end, text, conf}]。整条链路失败抛异常由上游降级。"""
    engine = _load_engine(cfg)
    h, w = 360, 640
    y_from = cfg.band_top         # 字幕区起点（画面高度比例）
    probe_top = int(h * 0.62)     # 变化检测只看更靠下的窄带，更敏感

    rows: list[dict] = []
    prev_probe: np.ndarray | None = None
    state_text = ""               # 当前字幕状态（"" 表示确认无字幕）
    state_conf = 0.0
    state_checked = False         # 这一帧的视觉状态是否已识别过
    ocr_calls = 0
    total = 0

    for t, frame in ffmpeg.iter_color_frames(src, cfg.fps, w, h):
        total += 1
        probe = cv2.cvtColor(frame[probe_top:, :], cv2.COLOR_BGR2GRAY)

        changed = True
        if prev_probe is not None:
            diff = float(np.abs(probe.astype(np.int16) - prev_probe.astype(np.int16)).mean())
            changed = diff >= cfg.change_threshold

        if not changed and state_checked:
            rows.append({"t": round(t, 3), "text": state_text,
                         "conf": state_conf, "reused": True})
            prev_probe = probe
            continue

        # 窄带有变化才识别，识别仍用整帧（实测更准也更快）
        ocr_calls += 1
        try:
            res, _ = engine(frame)
        except Exception as e:  # 单帧失败不影响整片
            log.debug(f"ocr frame failed at {t:.1f}s: {e}")
            res = None

        text, conf = _pick_subtitle_lines(res, h, y_from)
        state_text, state_conf, state_checked = text, conf, True
        rows.append({"t": round(t, 3), "text": text, "conf": conf, "reused": False})
        prev_probe = probe

    log.info(f"OCR: {total} frames, {ocr_calls} engine calls ({ocr_calls / max(1, total):.0%})")
    strips = _group(rows, cfg.fps, duration, cfg.min_confidence)
    log.info(f"OCR: {len(strips)} subtitle strips")
    return strips


def _group(rows: list[dict], fps: float, duration: float, min_conf: float) -> list[dict]:
    """把逐帧识别结果合并成字幕条，丢掉单帧低置信的误检。"""
    strips: list[dict] = []
    tick = 1.0 / max(fps, 0.5)
    for r in rows:
        if not r["text"]:
            continue
        if strips and _similar(strips[-1]["text"], r["text"]) > 0.62 \
                and r["t"] - strips[-1]["end"] <= tick * 1.6:
            cur = strips[-1]
            cur["end"] = round(r["t"], 3)
            cur["frames"] += 1
            if len(r["text"]) > len(cur["text"]) and not r["reused"]:
                cur["text"] = r["text"]
                cur["conf"] = max(cur["conf"], r["conf"])
            continue
        strips.append({
            "start": round(r["t"], 3), "end": round(r["t"] + tick, 3),
            "text": r["text"], "conf": r["conf"], "frames": 1,
        })

    out: list[dict] = []
    for s in strips:
        if s["frames"] < 2 and s["conf"] < max(0.75, min_conf + 0.15):
            continue
        if len(s["text"]) < 2:
            continue
        s["end"] = round(min(s["end"] + tick, duration) if duration else s["end"] + tick, 3)
        if s["end"] - s["start"] < 0.3:
            continue
        out.append({k: s[k] for k in ("start", "end", "text", "conf")})
    return out
