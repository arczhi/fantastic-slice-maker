"""BGM 层：选曲（DeepSeek）→ 节拍对齐 → 语音优先的混音。

三个设计决定，都是踩过坑定下来的：

1. **节拍对齐只允许「延长」，不允许「缩短」。**
   缩短片段会切掉台词尾部，这在漫剧里是致命的（台词是唯一的信息载体）。
   延长最多 cfg.max_stretch 秒，且延长区间内必须是有效画面（不能进黑场）。

2. **混音在拼接之后做，不在每个 clip 上做。**
   否则每个片段都会触发一次 BGM 的 fade-in，听感是"一段一段的"，
   而不是一条连续的配乐。用户要的正是「连贯的 BGM」。

3. **人声优先用 sidechaincompress 做 ducking，不用固定音量。**
   漫剧的台词密度不均，固定 BGM 音量要么盖住台词，要么在空镜处听不见。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import ffmpeg
from .config import BGMCfg, PlannerCfg
from .planner import _parse as _parse_json
from .planner import _post as _post_text
from .util import get_logger

log = get_logger("bgm")

SYSTEM = (
    "你是短视频配乐师，为苦情/家庭/都市短剧的抖音、快手切片选背景音乐。"
    "只输出 JSON，不输出解释。"
)

TEMPLATE = """下面是一部漫剧的 {n} 条切片方案，以及一个可选配乐库。

每首配乐的字段：id | 情绪标签 | BPM | 时长 | 一句描述

{library}

{avoid_note}切片方案：

{reels}

请为每条切片各选一首配乐，要求：
1. 配乐情绪要和切片主情绪一致：委屈/苦情配 somber 或 emotional，当众对峙/羞辱配 tense，
   身份揭穿/反杀配 uplifting，家庭日常配 warm。宁可保守，不要用打斗 epic 盖住台词。
2. 配乐时长必须 >= 切片时长（否则会提前断掉）。
3. BPM 建议 90-140，太慢会拖节奏，太快会让台词显得赶。
4. 同一部剧的多条切片尽量用不同的曲子，避免观众刷到重复。

start_ratio 表示从曲子第几成开始播：0.0 从头、0.15 跳过前奏、0.4 直接进高潮段。
切片要在 3 秒内抓住人，所以通常建议 0.05-0.35。

只输出 JSON：
{{
  "picks": [
    {{"reel": 1, "bgm_id": "epic_xxx", "start_ratio": 0.15, "reason": "一句话"}}
  ]
}}"""


# ---------------------------------------------------------------- 索引

def load_index(cfg: BGMCfg) -> list[dict]:
    p = Path(cfg.index)
    if not p.exists():
        log.warning(f"BGM 索引不存在: {p}（先跑 scripts/build_bgm_lib.py）")
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning(f"BGM 索引读取失败: {e}")
        return []
    tracks = [t for t in data.get("tracks") or [] if Path(t.get("file", "")).exists()
              or (Path(cfg.index).parents[1] / t.get("file", "")).exists()]
    for t in tracks:
        f = Path(t["file"])
        if not f.is_absolute():
            t["_path"] = str(Path(cfg.index).parents[1] / f)
        else:
            t["_path"] = str(f)
    return tracks


def _resolve(track: dict) -> str:
    return track.get("_path") or track["file"]


def _fmt_library(tracks: list[dict]) -> str:
    lines = []
    for t in tracks:
        tags = "/".join(t.get("tags") or [])[:28]
        desc = re.sub(r"\s+", " ", t.get("desc") or "")[:60]
        lines.append(
            f"{t['id']:42s} | {t['bucket']:9s} | {tags:28s} | "
            f"{t['bpm']:5.1f}bpm | {t['duration']:5.0f}s | {desc}"
        )
    return "\n".join(lines)


def _fmt_reels(reels: list[dict]) -> str:
    lines = []
    for i, r in enumerate(reels, 1):
        texts = " / ".join((b.get("text") or "").strip()[:26]
                           for b in r.get("beats") or [] if (b.get("text") or "").strip())
        total = sum(float(b.get("dur") or 0) for b in r.get("beats") or [])
        lines.append(f"切片{i}: 标题「{r.get('title','')}」 时长 {total:.0f}s")
        lines.append(f"  钩子：{r.get('hook','')[:50]}")
        lines.append(f"  台词流：{texts[:220]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 选曲

def _fallback_track(tracks: list[dict], reel: dict, used: set[str],
                    need_secs: float) -> dict | None:
    """模型不可用时的规则选曲：按切片里的冲突词挑桶。"""
    text = " ".join([str(reel.get("hook") or "")]
                    + [str(b.get("text") or "") for b in reel.get("beats") or []])
    if re.search(r"杀|死|血|斩|战|敌|攻|刀|剑|叛|怒|吼", text):
        order = ("epic", "tense", "uplifting")
    elif re.search(r"哭|泪|死别|别走|对不起|来不及|可惜|终究", text):
        order = ("somber", "mystical", "tense")
    elif re.search(r"为师|宗门|仙|秘境|古|灵|咒", text):
        order = ("mystical", "tense", "epic")
    else:
        order = ("tense", "mystical", "epic")
    for bucket in order:
        pool = [t for t in tracks
                if t["bucket"] == bucket and t["id"] not in used
                and t["duration"] >= need_secs]
        pool.sort(key=lambda t: (t.get("beat_cv", 1.0), -t.get("energy", 0)))
        if pool:
            return pool[0]
    pool = [t for t in tracks if t["id"] not in used and t["duration"] >= need_secs]
    return pool[0] if pool else None


def choose(tracks: list[dict], reels: list[dict], cfg: PlannerCfg,
           bgm_cfg: BGMCfg, avoid: set[str] | None = None) -> list[dict]:
    """为每条切片返回 {bgm_id, start_ratio, reason}。永不抛异常。"""
    if not tracks or not reels:
        return []

    need = [sum(float(b.get("dur") or 0) for b in r.get("beats") or []) for r in reels]
    # 时长不够的曲子直接过滤，避免出现"配乐提前结束"
    usable = [t for t in tracks if t["duration"] >= min(need) * 0.95] or tracks
    # 不同剧尽量不用同一首：把最近用过的曲子排除掉，但至少留够本剧的条数。
    if avoid:
        fresh = [t for t in usable if t["id"] not in avoid]
        if len(fresh) >= len(reels):
            usable = fresh
    picked: list[dict] = []

    if cfg.enabled and cfg.api_key:
        note = ""
        if avoid:
            note = ("最近其他剧已经用过这些曲子，本次尽量避开，避免观众耳熟："
                    + "、".join(sorted(avoid)) + "。\n\n")
        prompt = TEMPLATE.format(
            n=len(reels), library=_fmt_library(usable), reels=_fmt_reels(reels),
            avoid_note=note,
        )
        for attempt, temp in enumerate((cfg.temperature, 0.4), start=1):
            try:
                raw = _post_text(cfg, [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ], temp)
                data = _parse_json(raw)
                by_id = {t["id"]: t for t in usable}
                got: list[dict] = []
                for i in range(len(reels)):
                    got.append({"bgm_id": "", "start_ratio": 0.1, "reason": ""})
                for item in data.get("picks") or []:
                    try:
                        k = int(item.get("reel")) - 1
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= k < len(reels)):
                        continue
                    tid = str(item.get("bgm_id") or "")
                    if tid not in by_id:
                        continue
                    try:
                        ratio = float(item.get("start_ratio", 0.1))
                    except (TypeError, ValueError):
                        ratio = 0.1
                    got[k] = {
                        "bgm_id": tid,
                        "start_ratio": max(0.0, min(0.6, ratio)),
                        "reason": str(item.get("reason") or "")[:120],
                    }
                if any(g["bgm_id"] for g in got):
                    log.info(f"bgm 选曲成功 attempt={attempt}")
                    picked = got
                    break
            except Exception as e:
                log.warning(f"bgm 选曲尝试 {attempt} 失败: {type(e).__name__}: {e}")

    used: set[str] = set()
    out: list[dict] = []
    for i, (reel, secs) in enumerate(zip(reels, need)):
        tid = picked[i]["bgm_id"] if i < len(picked) else ""
        track = next((t for t in tracks if t["id"] == tid), None)
        if track is None or track["id"] in used:
            track = _fallback_track(tracks, reel, used, secs)
            ratio = 0.1
            reason = "规则兜底选曲"
        else:
            ratio = picked[i]["start_ratio"]
            reason = picked[i]["reason"] or "deepseek 选曲"
        if track is None:
            out.append({})
            continue
        used.add(track["id"])
        out.append({
            "track": track,
            "start_ratio": ratio,
            "start": round(track["duration"] * ratio, 2),
            "reason": reason,
            "attribution": track.get("attribution", ""),
        })
    return out


# ---------------------------------------------------------------- 节拍对齐

def _is_content_at(vis: dict, t: float) -> bool:
    fps = float(vis.get("fps") or 4.0)
    samples = vis.get("samples") or []
    k = int(t * fps)
    if not (0 <= k < len(samples)):
        return False
    s = samples[k]
    return 22 <= s["bright"] <= 246 and s["std"] >= 8.0


def _safe_extend(vis: dict, end: float, delta: float) -> bool:
    """延长 delta 秒后，新增的这段是否仍是有效画面。"""
    if delta <= 0:
        return True
    if not vis:
        return True
    step = 1.0 / float(vis.get("fps") or 4.0)
    n = max(1, int(delta / step))
    return all(_is_content_at(vis, end + step * (k + 1)) for k in range(n))


def align_to_beats(beats: list[dict], track: dict, start: float,
                   cfg: BGMCfg, vis: dict | None = None) -> tuple[list[dict], dict]:
    """把每个片段的结束点对齐到最近的 BGM 鼓点（只延长，不缩短）。

    返回调整后的 beats 与统计信息。若曲子节拍不稳定（beat_cv 高），
    整体放弃对齐——强行凑点会把台词切碎，得不偿失。
    """
    grid = track.get("beats") or []
    if not grid or track.get("beat_cv", 1.0) > cfg.min_beat_cv:
        return beats, {"aligned": False,
                       "reason": "beat_cv 过高或没有 beat 网格",
                       "beat_cv": track.get("beat_cv")}

    shift = float(start)
    out: list[dict] = []
    t = 0.0
    hit = 0
    applied_sum = 0.0
    applied_max = 0.0
    residual: list[float] = []      # 没能对齐的剩余误差，用于判断卡点质量

    for b in beats:
        dur = float(b["end"]) - float(b["start"])
        target = t + dur
        want = shift + target
        nxt = next((g for g in grid if g >= want), None)
        applied = 0.0
        if nxt is not None:
            delta = (nxt - shift) - target
            if 0 < delta <= cfg.max_stretch and _safe_extend(vis or {}, float(b["end"]), delta):
                dur += delta
                applied = delta
                hit += 1
            elif delta >= 0:
                # 最近的鼓点太远（超过 max_stretch）或延后会进黑场，放弃这一段
                residual.append(round(delta, 3))
        new_end = round(float(b["start"]) + dur, 3)
        out.append({**b, "end": new_end, "dur": round(dur, 3)})
        t += dur
        applied_sum += applied
        applied_max = max(applied_max, applied)

    stats = {
        "aligned": hit > 0,
        "beats_total": len(out),
        "beats_on_grid": hit,
        "stretch_seconds": round(applied_sum, 2),
        "max_stretch_used": round(applied_max, 2),
        "mean_residual": round(sum(residual) / len(residual), 2) if residual else 0.0,
        "bpm": track.get("bpm"),
        "beat_cv": track.get("beat_cv"),
    }
    log.info(f"beat align: {hit}/{len(out)} 段落对齐到鼓点，"
             f"共延长 {stats['stretch_seconds']}s（单段最多 {stats['max_stretch_used']}s）")
    return out, stats


# ---------------------------------------------------------------- 混音

def mix(reel: str, out: str, track: dict, start: float, cfg: BGMCfg,
        *, has_voice: bool = True, timeout: int = 600) -> dict:
    """把 BGM 混进成片：BGM 循环铺满全片，人声处自动压低。

    用 sidechaincompress 而不是固定音量：漫剧台词密度不均，固定音量
    要么盖住台词，要么在空镜处听不见。
    """
    info = ffmpeg.probe(reel)
    dur = round(info.duration, 3)
    if dur <= 0.5:
        raise ffmpeg.FFmpegError(f"成片时长异常: {dur}")

    bgm = _resolve(track)
    fi = min(cfg.fade_in, dur / 4)
    fo = min(cfg.fade_out, dur / 3)
    fo_st = max(0.0, dur - fo)

    # BGM 按响度归一，不用固定衰减：
    # 各首曲子的出厂响度差很远（实测从 -42dB 到 -16dB），固定 -14dB 会让
    # 安静的那几首几乎听不见。归一到一个确定的 LUFS，相对人声就稳定了。
    parts = [
        f"[1:a]atrim=0:{dur:.3f},asetpts=N/SR/TB,"
        f"aformat=sample_rates=48000:channel_layouts=stereo,"
        f"loudnorm=I={cfg.bgm_lufs}:TP=-2.0:LRA=11,"
        f"volume={cfg.gain_db:+.1f}dB,"
        f"afade=t=in:st=0:d={fi:.2f},afade=t=out:st={fo_st:.2f}:d={fo:.2f}[bgm]"
    ]

    if has_voice:
        # 人声要同时喂给 sidechain 和混音，必须 asplit 成两路。
        # 之前直接复用同一个 [vo] 标签，ffmpeg 没报错但电平异常（人声中频莫名掉 3dB）。
        parts.append(
            "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            "asplit=2[vo_det][vo_mix]"
        )
        parts.append(
            f"[bgm][vo_det]sidechaincompress=threshold={cfg.duck_threshold:.3f}:"
            f"ratio={cfg.duck_ratio:.1f}:attack=20:release=400:makeup=1[duck]"
        )
        # 用 amerge + pan 精确相加，而不是 amix：
        # amix 即使 normalize=0 实测仍会做平均，导致人声整体降 3dB。
        parts.append(
            "[vo_mix][duck]amerge=inputs=2,"
            "pan=stereo|c0=c0+c2|c1=c1+c3[amixed]"
        )
    else:
        parts.append("[bgm]anull[amixed]")

    # 这里刻意不用 loudnorm：
    # 切片在 cut 阶段已经归一到 -14 LUFS 了，叠加 BGM 后再整体归一化，
    # 会把多出来的能量从人声身上扣走（实测人声中频掉了 3.5dB），
    # 结果是"加了配乐反而听不清台词"。用限幅器只防削波，人声响度不变。
    # loudnorm 内部会升采样到 192k，不显式降回来会输出 96kHz 的音轨。
    parts.append("[amixed]alimiter=limit=0.95:level=false,aresample=48000[aout]")

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", reel,
        "-stream_loop", "-1", "-ss", f"{max(0.0, start):.3f}", "-i", bgm,
        "-filter_complex", ";".join(parts),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    ffmpeg.run(cmd, timeout=timeout)
    got = ffmpeg.probe(out)
    return {
        "path": str(out),
        "duration": round(got.duration, 2),
        "bgm_id": track["id"],
        "bgm_title": track.get("title", ""),
        "bgm_start": round(start, 2),
        "bpm": track.get("bpm"),
        "ducked": has_voice,
        "attribution": track.get("attribution", ""),
    }


