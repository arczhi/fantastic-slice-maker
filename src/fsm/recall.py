"""阶段 A：规则召回。不用模型，纯打分，负责把长片压到 25-35 个候选。

这一步的价值不是「选得准」，而是「不漏掉该看的」，并且给大模型一份
它读得懂的、带台词原文的结构化清单。分数不进最终决策，只用来排序和截断。
"""
from __future__ import annotations

import re

from .config import RecallCfg
from .util import get_logger

log = get_logger("recall")

# 冲突/情绪词：漫剧的戏剧张力几乎都藏在这些词里
_STRONG = (
    "杀|死|血|尸|找死|受死|纳命|偿命|报仇|叛徒|畜生|混蛋|放肆|大胆|狂妄|"
    "住口|闭嘴|滚开|岂有此理|休想|休得|找死|来人|拿下|斩|废了|灭|毁|"
    "救命|救我|快走|别过来|不要|不可能|怎么会|竟然|居然"
)
_MEDIUM = (
    "打|冲|砍|刺|劈|斩|抓|踢|推|摔|吼|喊|叫|哭|笑|怕|怒|恨|惊|"
    "誓|绝不|休想|凭你|就凭|你算|敢|不敢|等着|给我"
)
_STRONG_RE = re.compile(_STRONG)
_MEDIUM_RE = re.compile(_MEDIUM)


def emotion_score(text: str) -> float:
    """0-6 的情绪强度：强词 2 分、中词 1 分、感叹疑问各 0.5。"""
    if not text:
        return 0.0
    s = len(_STRONG_RE.findall(text)) * 2.0 + len(_MEDIUM_RE.findall(text)) * 1.0
    s += text.count("！") * 0.5 + text.count("？") * 0.5 + text.count("!") * 0.5 + text.count("?") * 0.5
    return min(6.0, s)


def score_units(units: list[dict], duration: float, cfg: RecallCfg,
                blacks: list[tuple[float, float]] | None = None) -> list[dict]:
    if not units:
        return []

    max_motion = max((u["motion"] for u in units), default=1.0) or 1.0
    max_center = max((u["center"] for u in units), default=1.0) or 1.0
    head_edge = duration * cfg.head_skip
    tail_edge = duration * (1 - cfg.tail_skip)
    blacks = blacks or []

    def black_ratio(start: float, end: float) -> float:
        """unit 落在 ffmpeg 检出的黑场区间里的比例。

        vision 采样是 4fps 的整帧统计，对「黑底白字字幕卡」会误判成有内容
        （白字把方差拉高）；ffmpeg 的 blackdetect 只看亮度，正好互补。
        """
        span = max(1e-6, end - start)
        covered = 0.0
        for b0, b1 in blacks:
            covered += max(0.0, min(end, b1) - max(start, b0))
        return min(1.0, covered / span)

    scored = []
    for u in units:
        dur = max(0.5, u["dur"])
        density = min(1.0, (u["chars"] / dur) / 3.2)          # 字/秒，3.2 字/秒视为满
        # 两路情绪信号取较大值：词表能抓到「找死」「放肆」这类系统未必标 ANGRY 的
        # 台词，SenseVoice 能抓到词表覆盖不到的语气爆发（比如没有脏字的怒吼）。
        # recall 的定位是「别漏掉该看的」，所以宁可比实际更敏感。
        emo = min(1.0, emotion_score(u["text"] or u["subtitle"]) / 4.0)
        emo_sv = u.get("emotion_sv")
        if emo_sv is not None:
            emo = max(emo, emo_sv)
        motion = min(1.0, u["motion"] / max_motion)
        center = min(1.0, u["center"] / max_center)
        sub = 1.0 if u["subtitle"] else (0.6 if u["text"] else 0.0)

        s = (
            cfg.w_density * density
            + cfg.w_emotion * emo
            + cfg.w_motion * motion
            + cfg.w_subtitle * sub
            + cfg.w_center * center
        )

        # 惩罚项：空镜、黑场、片头片尾、过短
        if u["blank"]:
            s *= 0.08
        br = black_ratio(u["start"], u["end"])
        if br >= 0.5:
            s *= 0.05
        elif br > 0.15:
            s *= 0.4
        if u["start"] < head_edge or u["end"] > tail_edge:
            s *= 0.55
        if dur < 1.5:
            s *= 0.5

        scored.append({**u, "score": round(s, 4), "emotion": round(emo, 3),
                       "density": round(density, 3), "black": round(br, 3)})

    return scored


def _nms(items: list[dict], min_gap: float, limit: int) -> list[dict]:
    """非极大抑制：避免候选全挤在同一个爆点附近。"""
    picked: list[dict] = []
    for it in sorted(items, key=lambda x: -x["score"]):
        if any(abs(it["start"] - p["start"]) < min_gap for p in picked):
            continue
        picked.append(it)
        if len(picked) >= limit:
            break
    return picked


def recall(units: list[dict], duration: float, cfg: RecallCfg,
           blacks: list[tuple[float, float]] | None = None) -> dict:
    scored = score_units(units, duration, cfg, blacks)
    if not scored:
        return {"candidates": [], "total_units": 0, "basis": "none"}

    has_text = sum(1 for u in scored if u["has_text"])
    basis = "text" if has_text >= max(3, len(scored) * 0.25) else "visual"

    # 候选密度必须跟时长挂钩。以前是 gap = duration / 16，11 分钟的片子
    # 算出 42 秒间隔，整片只放得下 13 个候选，平均 52 秒才摸一下样本——
    # 真正的高光只有十几秒，直接被 NMS 挤掉，模型只能在几个孤点上凑段子，
    # 于是出现「hook 在 571 秒、setup 在 463 秒」这种拼不上的剪辑单。
    #
    # 改法：间隔固定成「略粗于一个语义单元」（min_gap），数量按时长放开
    # （每 cand_step 秒一个），两头设上下限。注意间隔不能用被时长除出来的
    # 大值——那会让短片反而比原来更粗，属于改出回归。
    gap = cfg.min_gap
    limit = int(max(cfg.candidates,
                    min(cfg.candidates_max, duration / max(1.0, cfg.cand_step))))
    picked = _nms(scored, min_gap=gap, limit=limit)
    picked.sort(key=lambda x: x["start"])

    for i, u in enumerate(picked):
        u["cand_id"] = f"c{i:02d}"

    log.info(
        f"recall: {len(scored)} units -> {len(picked)} candidates "
        f"(basis={basis}, text_units={has_text})"
    )
    return {
        "candidates": picked,
        "total_units": len(scored),
        "basis": basis,
        "top_score": round(picked[0]["score"], 4) if picked else 0.0,
    }
