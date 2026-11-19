"""时间轴对齐：把四路信号按时间缝到 shot 上，再合并成语义单元（unit）。

为什么要合并：单个 shot 常只有 1-2 秒，装不下一句台词；而一条 beat 通常
是「一个动作 + 一句台词」，2-8 秒。合并时优先在句子结束处断开，避免把
半句话切片出去。
"""
from __future__ import annotations

from .config import RecallCfg
from .signals.vision import slice_stats
from .util import get_logger

log = get_logger("timeline")


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _assign(items: list[dict], start: float, end: float) -> list[dict]:
    """把时间区间的条目分配给与之重叠最多的那个单元。"""
    out = []
    for it in items:
        ov = _overlap(it["start"], it["end"], start, end)
        span = max(1e-6, it["end"] - it["start"])
        if ov / span >= 0.4 or (ov > 0 and it["start"] >= start and it["end"] <= end):
            out.append(it)
    return out


def build_units(
    shots: list, vis: dict, sentences: list[dict], strips: list[dict],
    cfg: RecallCfg,
) -> list[dict]:
    if not shots:
        return []

    sent_ends = [s["end"] for s in sentences]
    boundary_eps = 0.45

    def near_sentence_end(t: float) -> bool:
        return any(abs(t - e) <= boundary_eps for e in sent_ends)

    groups: list[list] = []
    cur = [shots[0]]
    for s in shots[1:]:
        cur_dur = cur[-1].end - cur[0].start
        if cur_dur >= cfg.max_beat:
            groups.append(cur)
            cur = [s]
        elif cur_dur >= cfg.min_beat and near_sentence_end(s.start):
            groups.append(cur)
            cur = [s]
        else:
            cur.append(s)
    if cur:
        groups.append(cur)

    # 尾部过短的组并入前一组
    if len(groups) > 1 and (groups[-1][-1].end - groups[-1][0].start) < cfg.min_beat * 0.6:
        groups[-2].extend(groups.pop())

    units: list[dict] = []
    for gi, g in enumerate(groups):
        start = round(g[0].start, 3)
        end = round(g[-1].end, 3)
        st = slice_stats(vis, start, end)
        ss = _assign(sentences, start, end)
        tt = _assign(strips, start, end)
        text = "".join(x["text"] for x in ss).strip()
        sub_text = "".join(x["text"] for x in tt).strip()
        chars = sum(1 for c in text if c not in "，。？！、；：\u201c\u201d ")
        units.append({
            "id": f"u{gi:03d}",
            "start": start,
            "end": end,
            "dur": round(end - start, 3),
            "shots": len(g),
            "motion": st["motion"],
            "center": st["center"],
            "bright": st["bright"],
            "blank": st["blank"],
            "asr": [x["text"] for x in ss],
            "text": text,
            "subtitle": sub_text,
            "chars": chars,
            "has_text": bool(text or sub_text),
        })

    log.info(f"units: {len(units)} from {len(shots)} shots")
    return units


def build(shots: list, vis: dict, sentences: list[dict], strips: list[dict], info, cfg: RecallCfg) -> dict:
    units = build_units(shots, vis, sentences, strips, cfg)
    return {
        "video": info.path,
        "duration": round(info.duration, 3),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 3),
        "has_audio": info.has_audio,
        "shots": [s.to_dict() for s in shots],
        "sentences": sentences,
        "subtitles": strips,
        "units": units,
        "stats": {
            "shot_count": len(shots),
            "unit_count": len(units),
            "sentence_count": len(sentences),
            "subtitle_count": len(strips),
            "blank_ratio": vis.get("blank_ratio", 0.0),
            "motion_p95": vis.get("motion_p95", 0.0),
        },
    }
