"""阶段 B：DeepSeek 出剪辑单。

核心设计：模型只做「选与排序」，不做「写文案」，输出被约束成结构化 JSON。
一旦输出不合规（id 不存在、时间越界、总时长超限、段数太少），
就走重试 → 规则兜底两级降级，保证全流程永不因模型失败而卡住。

一部剧要出多条切片（切片集锦），一次请求出 N 条方案，省钱也省一次往返。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from .config import PlannerCfg
from .util import get_logger

log = get_logger("planner")

_ROLES = ("hook", "setup", "rise", "climax", "cliff")

SYSTEM = (
    "你是短视频剪辑师，专做古装/修仙漫剧的抖音切片。"
    "你只输出 JSON，不输出任何解释文字。"
)

TEMPLATE = """把下面这部漫剧剪成 {n_reels} 条竖屏切片，每条 {target:.0f} 秒左右（投放抖音）。

候选片段（按时间顺序，时间单位秒，台词为语音转写原文）：

{candidates}

每条切片的硬性要求：
1. 从候选中选 4-7 段，总时长必须落在 {min_s:.0f}-{max_s:.0f} 秒之间。
   低于 {min_s:.0f} 秒的方案会被系统直接退回，宁可多选一段也不要交短片。
2. 结构必须是：hook（前 3 秒就要抓人）→ setup → rise → climax → cliff（留悬念）。
3. hook 要选冲突最凶、信息量最大的那一句，可以来自片中任意位置，不必是开头。
4. 每段 4-12 秒，必须完整包含至少一句台词，不要切在半句话上。
5. 优先选有强冲突词、人物对峙、情绪爆发的片段；不要选空镜、纯风景、无声画面。
6. 相邻两段台词要有承接（问答、因果、情绪递进），连起来像一段连贯剧情。
7. in/out 必须落在候选的可剪区间内，用原始时间轴秒数。

{n_reels} 条之间不允许共用同一个 hook 段落，切入角度要不同（比如一条主打对抗、
一条主打悬念或反转）。同一条内不要重复使用同一个候选 id。{variant_note}

只输出如下 JSON：
{{
  "reels": [
    {{
      "title": "标题不超过 18 字，要带钩子",
      "hook": "钩子那句台词原文",
      "beats": [
        {{"id": "c01", "role": "hook", "in": 12.3, "out": 18.9, "caption": "留给观众看的字幕，可留空"}}
      ],
      "reason": "一句话说明编排逻辑"
    }}
  ]
}}"""

VARIANT_NOTE = """

这是同一部剧的第 {v} 套方案（共 {total} 套），前几套已经交付，本套必须换角度重剪：
{used_hooks}
{used_ids}
- 已用过的候选能不用就不用，优先从没出现过的候选里重新组故事线；
- 结构仍然要完整，但人物关系、冲突点、悬念落点都要和前几套明显不同。"""



class PlannerError(RuntimeError):
    pass


def _fmt_candidates(cands: list[dict]) -> str:
    lines = []
    for c in cands:
        text = (c.get("text") or c.get("subtitle") or "").strip()
        if not text:
            text = "（无台词，纯画面）"
        text = re.sub(r"\s+", " ", text)[:110]
        lines.append(
            f"[{c['cand_id']}] {c['start']:.1f}-{c['end']:.1f}s "
            f"({c['dur']:.1f}s, 运动{c['motion']:.0f}) {text}"
        )
    return "\n".join(lines)


def _post(cfg: PlannerCfg, messages: list[dict], temperature: float) -> str:
    if not cfg.api_key:
        raise PlannerError(f"missing api key (env {cfg.api_key_env})")
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": cfg.max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    last_err = ""
    for attempt in range(1, cfg.retries + 1):
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=cfg.timeout,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (400, 401, 402, 403):
                break  # 认证/额度问题，重试无意义
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = f"{type(e).__name__}: {e}"
        log.warning(f"deepseek attempt {attempt}/{cfg.retries} failed: {last_err[:200]}")
        time.sleep(min(2 ** attempt, 8))
    raise PlannerError(last_err or "unknown")


def _parse(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    for candidate in (s,):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        raise PlannerError(f"no json in response: {raw[:200]}")
    return json.loads(m.group(0))


def _validate(plan: dict, cands: list[dict], cfg: PlannerCfg) -> list[dict]:
    """把模型给的 id/时间校准成可执行、且在候选区间内的 beat。"""
    by_id = {c["cand_id"]: c for c in cands}
    beats: list[dict] = []
    used: set[str] = set()
    for b in plan.get("beats") or []:
        if not isinstance(b, dict):
            continue
        cid = str(b.get("id"))
        c = by_id.get(cid)
        if not c or cid in used:
            continue
        try:
            i = float(b.get("in", c["start"]))
            o = float(b.get("out", c["end"]))
        except (TypeError, ValueError):
            continue
        i = max(c["start"], min(i, c["end"] - 1.0))
        o = min(c["end"], max(o, i + 1.0))
        # prompt 要求每段 4-12 秒，但模型经常交出 3 秒的碎段——那种段子放进
        # 成片就是一闪而过，观众还没反应过来就切走了。这里在候选区间内往回
        # 补足到 3.5 秒；候选本身就太短（2 秒的空镜）则放弃这一段。
        min_beat = 3.5
        if o - i < min_beat:
            o = min(c["end"], i + min_beat)
            if o - i < min_beat:
                i = max(c["start"], o - min_beat)
        if o - i < 2.5:
            continue
        used.add(cid)
        role = str(b.get("role") or "rise")
        beats.append({
            "cand_id": cid,
            "role": role if role in _ROLES else "rise",
            "start": round(i, 3),
            "end": round(o, 3),
            "dur": round(o - i, 3),
            "caption": str(b.get("caption") or "")[:60],
            "text": (c.get("text") or "").strip(),
        })
    return beats


def _accept(beats: list[dict], cfg: PlannerCfg,
            cands: list[dict] | None = None) -> tuple[bool, str, float]:
    """校验一条切片能否交付。返回 (ok, 驳回原因, 实测总时长)。

    下限必须真的卡在 min_seconds 上。之前写成 min_seconds * 0.6（32s 的配置
    实际只卡 19.2s），模型交 27 秒的片子也能过——投到抖音上根本铺不满一个
    完整的高光弧，观众刚看出味就结束了。

    但也不能一刀切：预告/花絮这类素材的候选池本身就只有十几秒，硬卡 32 秒
    会永远交不出片。所以候选池不够长时按可用时长折算。
    """
    total = sum(b["dur"] for b in beats)
    n_cands = len(cands) if cands else 99
    min_beats = min(3, n_cands)   # 短素材可能只够 2 段
    if len(beats) < min_beats:
        return False, f"only {len(beats)} beats (need {min_beats})", total

    floor = cfg.min_seconds
    if cands:
        avail = sum(c["dur"] for c in cands)
        floor = min(floor, avail * 0.85)
    if total < floor:
        return False, f"total {total:.0f}s < {floor:.0f}s", total
    return True, "", total


def _trim(beats: list[dict], max_seconds: float) -> list[dict]:
    out = list(beats)
    while sum(b["dur"] for b in out) > max_seconds and len(out) > 3:
        out.pop(-2)  # 优先砍掉 climax 前那一段
    return out


def _fallback_reels(cands: list[dict], cfg: PlannerCfg, n: int,
                    offset: int = 0, avoid: set[str] | None = None) -> list[dict]:
    """模型失败时的兜底：按规则分排序，为每条切片错开候选。

    offset 让不同套方案从排序结果的不同起点切进去，否则每套兜底都是同一条片子。
    """
    avoid = avoid or set()
    ordered = sorted(cands, key=lambda c: -c["score"])
    pool = [c for c in ordered if c["cand_id"] not in avoid]
    if len(pool) < 3:            # 躲得太狠会凑不出片，宁可重复
        pool = ordered
    reels = []
    for r in range(n):
        picked: list[dict] = []
        total = 0.0
        for k, c in enumerate(pool):
            if k % n != (r + offset) % max(1, n):
                continue
            if total >= cfg.target_seconds or len(picked) >= 5:
                break
            if any(abs(c["start"] - p["start"]) < 2.0 for p in picked):
                continue
            picked.append(c)
            total += c["dur"]
        if len(picked) < min(3, len(ordered)):
            continue
        picked.sort(key=lambda c: c["start"])
        roles = ["hook"] + ["rise"] * max(0, len(picked) - 2) + ["cliff"]
        if len(picked) >= 3:
            roles[len(picked) // 2] = "climax"
        reels.append({
            "title": "", "hook": (picked[0].get("text") or "")[:40],
            "reason": "rule-based fallback", "strategy": "fallback",
            "beats": _enforce_hook([{
                "cand_id": c["cand_id"], "role": roles[i] if i < len(roles) else "rise",
                "start": c["start"], "end": c["end"], "dur": c["dur"],
                "caption": "", "text": (c.get("text") or "").strip(),
            } for i, c in enumerate(picked)]),
            "total": round(total, 2),
        })
    return reels


def _enforce_hook(beats: list[dict]) -> list[dict]:
    """hook 必须是带台词的一段。

    实测模型会挑「画面最炸但一句台词都没有」的段落当开头，这种放在抖音上
    前 3 秒留不住人——观众需要一句话就知道发生了什么。
    """
    if not beats:
        return beats
    hooks = [b for b in beats if b["role"] == "hook"]
    if hooks and any(b["text"].strip() for b in hooks):
        return beats
    cand = next((b for b in beats if b["text"].strip()), None)
    if cand is None:
        return beats
    for b in beats:
        if b["role"] == "hook":
            b["role"] = "rise"
    cand["role"] = "hook"
    log.info(f"hook 无台词，改用 {cand['cand_id']} 作为开头")
    return beats


def split_counts(total: int, per_plan: int = 3, max_total: int = 12) -> list[int]:
    """把「总共要几条」拆成每套方案的条数。

    为什么不一次性让模型出 8 条：实测一次要 5 条以上，模型会开始凑数——
    结构完整的那两条还不错，后面的 hook 越来越弱、段落开始重复。所以拆成
    每套 2-3 条、多套方案，跨套还用不同的候选，成品之间的差异更真实。

    例：total=7, per_plan=3 → [3, 2, 2]；total=3 → [3]；total=1 → [1]。
    """
    total = max(1, min(int(total), max(1, int(max_total))))
    per_plan = max(1, int(per_plan))
    variants = min(max(1, -(-total // per_plan)), total)   # 向上取整，且不超过总条数
    base, rem = divmod(total, variants)
    return [base + (1 if i < rem else 0) for i in range(variants)]


def fingerprint(cands: list[dict], n_reels: int, variant: int,
                used_hooks: set[str] | None = None) -> str:
    """剪辑单缓存的指纹：候选集或套数变了就必须重新规划。

    以前 plan.json 只按视频 id 缓存，改 --reels 也照样读旧的 2 条方案，
    出片数量和用户要求对不上。指纹里带上候选 id、条数、套号和已用 hook，
    任何一种变化都会重新请求模型。
    """
    import hashlib

    raw = "|".join([
        str(n_reels), str(variant),
        ",".join(sorted(used_hooks or ())),
        ",".join(c["cand_id"] for c in cands),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def plan(
    cands: list[dict], cfg: PlannerCfg, n_reels: int = 2, *,
    variant: int = 1, variants_total: int = 1,
    used_hooks: set[str] | None = None, used_prior: list[list[str]] | None = None,
    dedupe_hook: bool = True, overlap_max: float = 0.6,
) -> dict:
    """返回 {strategy, reels:[{title,hook,reason,beats,total}], elapsed, error}。

    variant/variants_total 用于「同一部剧出多套方案」：会把前几套已用过的
    hook 与候选喂给模型，并在校验阶段驳回撞 hook、或与旧方案重合度过高的片子。
    """
    t0 = time.time()
    if not cands:
        return {"strategy": "none", "reels": [], "error": "no candidates"}

    n_reels = max(1, n_reels)
    used_hooks = set(used_hooks or ())
    prior_sets = [set(x) for x in (used_prior or [])]

    if cfg.enabled:
        note = ""
        if variant > 1 or variants_total > 1:
            hooks_line = ("- 已用过的开头段落（禁止再用作 hook）："
                          + ("、".join(sorted(used_hooks)) if used_hooks else "无"))
            used_all = sorted({c for s in prior_sets for c in s})
            ids_line = ("- 已用过的候选（能不用就不用）："
                        + ("、".join(used_all[:40]) if used_all else "无"))
            note = VARIANT_NOTE.format(v=variant, total=variants_total,
                                       used_hooks=hooks_line, used_ids=ids_line)
        prompt = TEMPLATE.format(
            n_reels=n_reels, target=cfg.target_seconds,
            min_s=cfg.min_seconds, max_s=cfg.max_seconds,
            candidates=_fmt_candidates(cands), variant_note=note,
        )
        # 被驳回时把原因回灌给模型再试一次。原样重发 prompt 只会让它重复
        # 同样的错误——实测三次都用同一个偏短的方案，最后白烧三次调用。
        feedback: list[str] = []
        # 多套方案时温度略微上调，让后几套真的换角度而不是照抄前一套
        base_temp = min(1.0, cfg.temperature + 0.06 * (variant - 1))
        for attempt, temp in enumerate((base_temp, min(1.0, base_temp + 0.1), 0.4), start=1):
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ]
            if feedback:
                messages.append({
                    "role": "user",
                    "content": (
                        "上一版被驳回，原因：\n"
                        + "\n".join(f"- {f}" for f in feedback)
                        + "\n请针对这些原因修正，重新输出完整的 JSON（reels 数组）。"
                    ),
                })
            try:
                raw = _post(cfg, messages, temp)
                parsed = _parse(raw)

                raw_reels = parsed.get("reels")
                if not isinstance(raw_reels, list):
                    raw_reels = [parsed]

                reels: list[dict] = []
                reasons: list[str] = []
                for item in raw_reels:
                    if not isinstance(item, dict):
                        continue
                    beats = _validate(item, cands, cfg)
                    ok, why, total = _accept(beats, cfg, cands)
                    if not ok:
                        log.warning(f"reel rejected: {why}")
                        reasons.append(why)
                        continue
                    if total > cfg.max_seconds * 1.35:
                        beats = _trim(beats, cfg.max_seconds)
                        total = sum(b["dur"] for b in beats)
                    beats = _enforce_hook(beats)
                    ids = {b["cand_id"] for b in beats}
                    hook_id = next((b["cand_id"] for b in beats
                                    if b["role"] == "hook"), "")

                    # 跨套去重：hook 撞了就等于两条片子开头一模一样，
                    # 投出去会被判重复内容；重合率过高也同理。
                    if dedupe_hook and hook_id and hook_id in used_hooks:
                        why = f"hook {hook_id} 已被前一套用过"
                        log.warning(f"reel rejected: {why}")
                        reasons.append(why)
                        continue
                    worst = max((_overlap(ids, s) for s in prior_sets), default=0.0)
                    if worst >= overlap_max:
                        why = f"与前一套重合 {worst:.0%}，换一批候选"
                        log.warning(f"reel rejected: {why}")
                        reasons.append(why)
                        continue

                    reels.append({
                        "title": str(item.get("title") or "")[:40],
                        "hook": str(item.get("hook") or "")[:80],
                        "reason": str(item.get("reason") or "")[:200],
                        "strategy": "llm",
                        "beats": beats,
                        "cand_ids": sorted(ids),
                        "total": round(total, 2),
                    })
                if reels:
                    log.info(
                        f"plan v{variant} ok: {len(reels)} reels, "
                        f"{[r['total'] for r in reels]}, attempt {attempt}"
                    )
                    return {"strategy": "llm", "reels": reels, "variant": variant,
                            "elapsed": round(time.time() - t0, 2)}
                feedback = reasons or ["没有任何一条切片通过校验"]
                raise PlannerError("no valid reel in response: " + "; ".join(feedback[:3]))
            except PlannerError as e:
                log.warning(f"plan attempt {attempt} rejected: {e}")
            except Exception as e:
                log.warning(f"plan attempt {attempt} crashed: {type(e).__name__}: {e}")

    reels = _fallback_reels(cands, cfg, n_reels, offset=variant - 1,
                            avoid=used_hooks)
    log.warning(f"plan v{variant} fallback: {len(reels)} reels")
    return {"strategy": "fallback", "reels": reels, "variant": variant,
            "elapsed": round(time.time() - t0, 2),
            "error": "llm unavailable, used rule fallback"}
