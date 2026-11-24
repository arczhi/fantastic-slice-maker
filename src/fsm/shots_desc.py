"""分镜描述：给每个镜头写一句 4-10 字的剧情描述，供成片顶部悬浮。

为什么要单独一步：
- plan 里的 caption 是「一个 beat 一句」，一个 beat 往往跨 3-5 个镜头，
  观众看到的是画面在切、文字不动，像没写；
- 这里按 shots 的真实切点把 beat 再切开，每个镜头一句，文字跟着画面走。

只调用一次 LLM（整条切片的所有镜头一起给），失败就退回 beat 级 caption。
"""
from __future__ import annotations

import json

from . import planner
from .config import PlannerCfg
from .util import get_logger

log = get_logger("shots_desc")

SYSTEM = "你是竖屏短剧的剪辑师，为一条切片写分镜描述。只输出 JSON，不输出解释。"

TEMPLATE = """下面是一条竖屏短剧切片的镜头列表。每个镜头给了切片内时间、原片时间和这段的台词。

请为每个镜头写一句 6-14 个字的剧情描述，要求：
1. 说清这一镜在发生什么（谁、对谁、做了什么），不要照抄台词；
2. 不要剧透后面的反转，不要写“镜头”“画面”这类词；
3. 一句话，不要标点结尾。

镜头列表：
{shots}

只输出 JSON：
{{"shots":[{{"i":1,"desc":"..."}}]}}
"""


def _windows(beat: dict, shots: list[dict]) -> list[dict]:
    """把一个 beat 按镜头切点切成若干子窗口（时间相对 beat 起点）。"""
    out: list[dict] = []
    for s in shots:
        a = max(float(s.get("start") or 0), float(beat.get("start") or 0))
        b = min(float(s.get("end") or 0), float(beat.get("end") or 0))
        if b - a < 0.35:
            continue
        out.append({"t0": round(a - float(beat.get("start") or 0), 3),
                    "t1": round(b - float(beat.get("start") or 0), 3),
                    "src": (a, b)})
    if not out:
        out = [{"t0": 0.0, "t1": round(float(beat.get("dur") or 0), 3),
                "src": (float(beat.get("start") or 0), float(beat.get("end") or 0))}]
    return out


def _text_for(a: float, b: float, sentences: list[dict]) -> str:
    parts = [str(s.get("text") or "").strip() for s in sentences
             if float(s.get("end") or 0) > a and float(s.get("start") or 0) < b]
    return "".join(p for p in parts if p)[:80]


def build(beats: list[dict], shots: list[dict], sentences: list[dict],
          cfg: PlannerCfg) -> None:
    """给每个 beat 挂上 shot_descs；失败时静默保留 beat 级 caption。"""
    if not beats or not shots:
        return
    rows: list[dict] = []
    for bi, b in enumerate(beats):
        wins = _windows(b, shots)
        for w in wins:
            rows.append({"i": len(rows) + 1, "beat": bi, **w,
                         "line": _text_for(w["src"][0], w["src"][1], sentences)})
    if not rows:
        return
    lines = [f'{r["i"]}. 切片 {r["t0"]:.1f}-{r["t1"]:.1f}s｜台词：{r["line"] or "（无台词）"}'
             for r in rows]
    prompt = TEMPLATE.format(shots="\n".join(lines))
    try:
        raw = planner._post(cfg, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ], 0.5)
        data = planner._parse(raw or "")
        got = data.get("shots") if isinstance(data, dict) else None
        if not isinstance(got, list):
            return
        by_i = {int(x.get("i") or 0): str(x.get("desc") or "").strip()
                for x in got if isinstance(x, dict)}
    except Exception as e:                           # noqa: BLE001
        log.warning(f"分镜描述生成失败，退回 beat caption: {e}")
        return

    for b in beats:
        b.pop("shot_descs", None)
    for r in rows:
        desc = by_i.get(r["i"]) or ""
        if not desc:
            continue
        beats[r["beat"]].setdefault("shot_descs", []).append(
            {"t0": r["t0"], "t1": r["t1"], "text": desc[:18]})
    n = sum(len(b.get("shot_descs") or []) for b in beats)
    log.info(f"分镜描述 {n} 条（{len(rows)} 个镜头）")
