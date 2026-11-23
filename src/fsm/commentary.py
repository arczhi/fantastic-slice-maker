"""解说模式：原片只做「引用片段」，成片主体是原创口播 + 原创文字卡。

为什么要重做形态，而不是给现有切片补一层字幕：

抖音驳回的原文是「以**片段拼接/拆条**形式呈现」，B 站驳回的是「网络剧片
发行许可」。前者判的是形态，后者判的是资质，两条都不是水印问题 ——
去水印、镜像、变速、换封面改的都是画面指纹那一层，而判定走的是
「连续原片画面 + 原声 + 原硬字幕」这个组合。只要这三样还在，同一句话
还会再驳回一次，反复几次就是账号级降权。

平台自己给的出路写得很清楚：「本人出镜口播解说、剧情观点梳理、人物关系
分析等原创表达」。所以这里把产出从「一段原片」改成「口播主体 + 短引用」：

    口播卡 → 引用1 → 口播卡 → 引用2 → … → 口播卡（收尾）

三条硬约束由程序保证，不交给模型：
    1. 单条引用 ≤ quote_max（默认 5 秒），且按「第一句完整台词」对齐；
    2. 引用总时长 ≤ quote_ratio_max（默认 30%）；
    3. 音轨重做：口播走 TTS，引用段原声压到 -18dB，原声永远不是主体。

模型只负责写口播词，不负责排时间。它再离谱也产不出「引用占 80%」的片子。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import ffmpeg, planner, render, subtitle
from .config import CommentaryCfg, Config
from .util import atomic_write_json, ensure_dir, get_logger, safe_name

log = get_logger("commentary")

SYSTEM = "你是短剧解说账号的撰稿人，只写口播解说词，只输出 JSON。"

TEMPLATE = """你在给一个短剧解说账号写口播词。下面是一条解说视频的固定分镜，
画面顺序和引用位置已经排死，不能改：

{structure}

规则：
1. 写正好 {n_talks} 段口播，第 i 段对应上面标着「口播{{i}}」的位置。每段 {min_c}-{max_c} 字，口语，
   像跟朋友聊天，不要书面语、不要排比句、不要感叹号堆叠。
2. 口播要讲观众从画面上看不出来的东西：这个人为什么这么做、两人什么关系、
   前面埋了什么伏笔、你判断结果会怎样。**不要复述引用片段里的台词** ——
   那段原话观众听得见，复述一遍等于没写。
3. 第一段第一句就抛判断或反常信息。不要「大家好」「今天给大家讲」「接下来」
   「我们来看」这类开场。
4. 这些词一律不许出现：本片、本视频、视频、片段、素材、剪辑、二创、二次创作、
   搬运、点赞、关注、评论区、主页、私信、微信、抖音、快手、B站。
5. 不要提平台名，不要写「我剪了」「我找了这段」这类作者视角的话。
6. 不涉钱、不煽动对立、不写与画面对不上的夸张描述。
7. 最后一段收在人物处境或一个悬念上，不要「所以说」「总而言之」这种总结。
8. 标题 ≤ 16 字，有钩子但不夸张；简介 30-60 字，最多两句，不要写成剧情梗概。

只输出 JSON：
{{"title": "标题", "desc": "简介", "talks": [{{"i": 0, "text": "口播词"}}]}}
talks 必须正好 {n_talks} 段，i 从 0 开始递增。"""

# 出现这些词就是「作者视角」或「站外导流」，两种都会被压流量
_BANNED = (
    "本片", "本视频", "视频", "片段", "素材", "剪辑", "二创", "二次创作",
    "搬运", "点赞", "关注", "评论区", "主页", "私信", "微信", "抖音", "快手",
    "bilibili", "B站", "转载", "侵删",
)

_TTS_FALLBACK_CPS = 4.2      # 没有 TTS 时按中文 4.2 字/秒估时长


# ---------------------------------------------------------------- 对外入口

def build(
    src: str, reel: dict, work: Path, out_dir: Path, cfg: Config,
    info, sentences: list[dict], shots: list[dict], vis: dict,
    *, tag: str, variant: int = 1, index: int = 1, strips: list[dict] | None = None,
) -> dict:
    """跑一条解说片：口播词 → 配音 → 引用片段 → 文字卡 → 组装 → 脚本。"""
    ccfg = cfg.commentary
    work = ensure_dir(Path(work))
    title_hint = reel.get("title") or ""
    strips = strips or []

    # 1. 选段：与切片模式共用 snap/trim/order，保证引用区间不落在黑场里
    beats = render.snap_to_shots(reel.get("beats") or [], shots)
    beats = render.trim_blank(beats, vis)
    beats = render.order_beats(beats)
    if not beats:
        raise RuntimeError("no usable beats for commentary")
    # 段内台词：ASR 优先，没有 ASR 就用 OCR 硬字幕。
    # 这一步不能省——口播词要讲「观众看不出来的东西」，模型必须先知道
    # 这一段到底在说什么，否则只能写出「她的反应很快」这种放之四海皆准的空话。
    for b in beats:
        b["line"] = _text_in(strips, sentences, b["start"], b["end"], 110)

    # 2. 引用窗口：只截「第一句完整台词」，不给整段
    quotes = _pick_quotes(beats, sentences, ccfg, info.duration)
    if not quotes:
        raise RuntimeError("no quote window")
    n_talks = len(quotes) + 1

    # 3. 口播词
    talks, meta, degraded = _narrate(quotes, n_talks, title_hint, cfg)

    # 4. 引用占比 / 总时长闸门：超了就压短，压到下限还超就丢引用
    quotes, talks = _enforce_ratio(quotes, talks, ccfg)
    n_talks = len(quotes) + 1
    while len(talks) < n_talks:
        talks.append({"i": len(talks), "text": talks[-1]["text"]})
    talks = talks[:n_talks]

    # 5. 配音 + 文字卡 + 引用片段，逐段产出标准段
    seg_dir = ensure_dir(work / "seg")
    card_dir = ensure_dir(work / "cards")
    quote_dir = ensure_dir(out_dir / "quotes" / tag)
    segments: list[Path] = []
    timeline: list[dict] = []
    talk_sec = 0.0
    quote_sec = 0.0

    for i, talk in enumerate(talks):
        text = talk["text"]
        wav = seg_dir / f"t{i:02d}.wav"
        tts_sec = _tts(text, wav, ccfg)
        dur = round(tts_sec + ccfg.lead_pad, 2)
        card = card_dir / f"c{i:02d}.png"
        _render_card(text, i, n_talks, meta.get("title", ""), ccfg, card, cfg)
        seg = seg_dir / f"s{i:02d}_talk.mp4"
        ffmpeg.still_segment(
            card, seg, dur,
            width=cfg.render.width, height=cfg.render.height, fps=cfg.render.fps,
            crf=cfg.render.crf, preset=cfg.render.preset, threads=cfg.render.threads,
            encoder=cfg.render.encoder, vt_bitrate=cfg.render.vt_bitrate,
            audio=(wav if wav.exists() else None),
        )
        segments.append(seg)
        talk_sec += dur
        timeline.append({
            "kind": "talk", "i": i, "text": text, "dur": dur,
            "card": str(card.relative_to(work)),
            "tts": "say" if wav.exists() else "none",
        })

        if i < len(quotes):
            q = quotes[i]
            qpath = quote_dir / f"q{i + 1:02d}_{q['role']}.mp4"
            ffmpeg.cut(
                src, qpath, q["start"], q["end"],
                width=cfg.render.width, height=cfg.render.height,
                fps=cfg.render.fps, crf=cfg.render.crf, preset=cfg.render.preset,
                loudness_i=cfg.render.loudness_i, blur_sigma=cfg.render.blur_sigma,
                audio_bitrate=cfg.render.audio_bitrate, timeout=cfg.render.timeout,
                has_audio=info.has_audio, threads=cfg.render.threads,
                src_size=(info.width, info.height), blur_div=cfg.render.blur_div,
                encoder=("libx264" if cfg.render.encoder == "auto" else cfg.render.encoder),
                vt_bitrate=cfg.render.vt_bitrate, dewatermark=cfg.render.dewatermark,
                fit=cfg.render.fit,
                # 内缩 + 原声压低：这两项就是「这是引用」的形态本身
                inset=ccfg.quote_inset, pad_color=ccfg.pad_color,
                audio_gain_db=ccfg.quote_audio_gain_db,
            )
            segments.append(qpath)
            qdur = ffmpeg.probe(qpath).duration
            quote_sec += qdur
            timeline.append({
                "kind": "quote", "i": i, "file": str(qpath.relative_to(out_dir)),
                "start": q["start"], "end": q["end"], "dur": round(qdur, 2),
                "role": q["role"], "text": q["text"], "note": q.get("note", ""),
            })

    # 6. 组装：文字卡段与引用段参数不同，必须重编码拼接
    stem = f"{tag}_{safe_name(meta.get('title') or title_hint or '解说', 22)}"
    final = out_dir / f"{stem}.mp4"
    ffmpeg.concat(
        segments, final, reencode=True, crf=cfg.render.crf,
        preset=cfg.render.preset, width=cfg.render.width, height=cfg.render.height,
        fps=cfg.render.fps, threads=cfg.render.threads,
    )

    total = ffmpeg.probe(final).duration
    ratio = round(quote_sec / total, 4) if total else 0.0

    # 7. 脚本落盘（json 给程序读，md 给人改）
    script = {
        "tag": tag, "variant": variant, "index": index,
        "video": str(src), "title": meta.get("title", ""), "desc": meta.get("desc", ""),
        "degraded": degraded, "warnings": meta.get("warnings") or [],
        "duration": round(total, 2),
        "quote_total": round(quote_sec, 2), "talk_total": round(talk_sec, 2),
        "quote_ratio": ratio,
        "segments": timeline,
    }
    sdir = ensure_dir(out_dir / "script")
    atomic_write_json(sdir / f"{stem}.json", script)
    _write_md(script, sdir / f"{stem}.md", ccfg)

    files = {p.name for p in [final] + segments}
    return {
        "path": str(final), "stem": stem, "duration": round(total, 2),
        "script": str(sdir / f"{stem}.json"), "md": str(sdir / f"{stem}.md"),
        "title": meta.get("title") or title_hint, "desc": meta.get("desc", ""),
        "quote_ratio": ratio, "quote_total": round(quote_sec, 2),
        "degraded": degraded, "segments": timeline, "files": files,
    }


# ---------------------------------------------------------------- 选段


def build_overlay(
    src: str, reel: dict, work: Path, out_dir: Path, cfg: Config,
    info, sentences: list[dict], shots: list[dict], vis: dict,
    *, tag: str, variant: int = 1, index: int = 1, strips: list[dict] | None = None,
) -> dict:
    """叠加解说片：画面全程用原片（无独立文字卡），解说字幕叠在原画面上。

    与 commentary.build 的差别：
    - 口播段不再渲染成深底文字卡，而是取口播对应引用位置附近的**原片画面**
      铺满画布，口播 TTS 压在压低的原声上，口播词按字符数均匀切成字幕条，
      用 subtitle.render_png 出 PNG 叠在原画面底边——「解说字幕放在原视频上」。
    - 引用片段全幅展示（不内缩），原声不压——两段解说之间留一段原声台词
      作呼吸口，形态上就是一条带解说的原片剪辑。
    - 去水印走 cfg.render.dewatermark（delogo/crop），全程生效。
    """
    ccfg = cfg.commentary
    work = ensure_dir(Path(work))
    title_hint = reel.get("title") or ""
    strips = strips or []

    # 1-2. 选段与引用窗口：与文字卡版完全同源
    beats = render.snap_to_shots(reel.get("beats") or [], shots)
    beats = render.trim_blank(beats, vis)
    beats = render.order_beats(beats)
    if not beats:
        raise RuntimeError("no usable beats for overlay commentary")
    for b in beats:
        b["line"] = _text_in(strips, sentences, b["start"], b["end"], 110)
    quotes = _pick_quotes(beats, sentences, ccfg, info.duration)
    if not quotes:
        raise RuntimeError("no quote window")
    n_talks = len(quotes) + 1

    # 3. 口播词
    talks, meta, degraded = _narrate(quotes, n_talks, title_hint, cfg)
    while len(talks) < n_talks:
        talks.append({"i": len(talks), "text": talks[-1]["text"]})
    talks = talks[:n_talks]

    # 4. 时长闸门：估算总时长超上限就从尾部丢引用（合并口播），保住完播率
    def _est_dur() -> float:
        t = sum(ccfg.lead_pad + len(x["text"]) / 8.0 for x in talks)
        q = sum(float(q["end"]) - float(q["start"]) for q in quotes)
        return t + q

    while _est_dur() > ccfg.target_seconds + 6 and len(quotes) > 1:
        quotes = quotes[:-1]
        talks = talks[:-1]
    n_talks = len(quotes) + 1
    talks = talks[:n_talks]

    # 5. 逐段产出：口播压原片 + 字幕叠加；引用段全幅原声
    seg_dir = ensure_dir(work / "seg")
    sub_dir = ensure_dir(work / "subs")
    quote_dir = ensure_dir(out_dir / "quotes" / tag)
    lay = subtitle.layout_of(info.width, info.height,
                             cfg.render.width, cfg.render.height, cfg.subtitle)
    segments: list[Path] = []
    timeline: list[dict] = []
    talk_sec = 0.0
    quote_sec = 0.0

    for i, talk in enumerate(talks):
        text = talk["text"]
        wav = seg_dir / f"t{i:02d}.wav"
        tts_sec = _tts(text, wav, ccfg)
        dur = round(tts_sec + ccfg.lead_pad, 2)

        # 画面窗口：引用句起始位置往后铺满口播时长，贴近句首让画面与解说对齐
        if i < len(quotes):
            q0 = float(quotes[i]["start"])
        else:
            # 收尾口播：从最后一个引用句结束继续往后放原片
            q0 = float(quotes[-1]["end"]) + 0.4
        bg_start = max(0.0, min(q0, info.duration - dur - 0.5))
        bg_dur = min(dur, max(0.5, info.duration - bg_start))

        # 解说字幕：按字符数占比把口播词切成 ≤2 行的字幕条，均匀铺在 TTS 时长上
        lines = subtitle.split_text(text, cfg.subtitle.max_chars)
        # 两行一组作为一条字幕
        groups = [lines[k:k + 2] for k in range(0, len(lines), 2)] or [[text]]
        total_chars = sum(len(g[0]) + len(g[1]) if len(g) > 1 else len(g[0])
                          for g in groups) or 1
        subs: list[dict] = []
        cum = 0.0
        for gi, g in enumerate(groups):
            gchars = len(g[0]) + (len(g[1]) if len(g) > 1 else 0)
            cstart = cum / total_chars * tts_sec
            cum += gchars
            cend = cum / total_chars * tts_sec
            png = sub_dir / f"t{i:02d}_{gi:02d}.png"
            try:
                h = subtitle.render_png(g, lay, cfg.subtitle, png)
            except Exception as e:                       # noqa: BLE001
                log.warning(f"解说字幕渲染失败 {g[0][:12]}: {e}")
                continue
            subs.append({
                "png": png,
                "start": round(cstart, 3),
                "end": round(max(cend, cstart + 0.5), 3),
                "y": max(0, lay["bottom"] - h),
            })
        seg = seg_dir / f"s{i:02d}_talk.mp4"
        ffmpeg.cut(
            src, seg, bg_start, bg_start + bg_dur,
            width=cfg.render.width, height=cfg.render.height,
            fps=cfg.render.fps, crf=cfg.render.crf, preset=cfg.render.preset,
            loudness_i=cfg.render.loudness_i, blur_sigma=cfg.render.blur_sigma,
            audio_bitrate=cfg.render.audio_bitrate, timeout=cfg.render.timeout,
            has_audio=info.has_audio, threads=cfg.render.threads,
            src_size=(info.width, info.height), blur_div=cfg.render.blur_div,
            encoder=("libx264" if cfg.render.encoder == "auto" else cfg.render.encoder),
            vt_bitrate=cfg.render.vt_bitrate, dewatermark=cfg.render.dewatermark,
            fit=cfg.render.fit,
            inset=1.0,                                   # 全幅，无内缩边
            audio_gain_db=ccfg.overlay_bg_gain_db,       # 原声退到背景
            narr_gain_db=ccfg.narr_gain_db,              # 口播增益（用户要求口播更突出）
            subs=subs,
            extra_audio=(wav if wav.exists() else None),  # TTS 压在原声上
        )
        segments.append(seg)
        talk_sec += dur
        timeline.append({
            "kind": "talk_over", "i": i, "text": text, "dur": dur,
            "start": round(bg_start, 2), "subs": len(subs),
            "tts": "say" if wav.exists() else "none",
        })

        if i < len(quotes):
            q = quotes[i]
            qpath = quote_dir / f"q{i + 1:02d}_{q['role']}.mp4"
            ffmpeg.cut(
                src, qpath, q["start"], q["end"],
                width=cfg.render.width, height=cfg.render.height,
                fps=cfg.render.fps, crf=cfg.render.crf, preset=cfg.render.preset,
                loudness_i=cfg.render.loudness_i, blur_sigma=cfg.render.blur_sigma,
                audio_bitrate=cfg.render.audio_bitrate, timeout=cfg.render.timeout,
                has_audio=info.has_audio, threads=cfg.render.threads,
                src_size=(info.width, info.height), blur_div=cfg.render.blur_div,
                encoder=("libx264" if cfg.render.encoder == "auto" else cfg.render.encoder),
                vt_bitrate=cfg.render.vt_bitrate, dewatermark=cfg.render.dewatermark,
                fit=cfg.render.fit,
                inset=1.0,                               # 全幅，两段解说之间的原声呼吸口
                # 引用段没有 extra_audio，走 loudnorm 之后的 gain 分支，压低有效；
                # pre-loudnorm 压会被归一化拉回来等于没压。
                audio_gain_db=ccfg.overlay_quote_gain_db,
            )
            segments.append(qpath)
            qdur = ffmpeg.probe(qpath).duration
            quote_sec += qdur
            timeline.append({
                "kind": "quote", "i": i, "file": str(qpath.relative_to(out_dir)),
                "start": q["start"], "end": q["end"], "dur": round(qdur, 2),
                "role": q["role"], "text": q["text"], "note": q.get("note", ""),
            })

    # 6. 组装
    stem = f"{tag}_{safe_name(meta.get('title') or title_hint or '解说', 22)}"
    final = out_dir / f"{stem}.mp4"
    ffmpeg.concat(
        segments, final, reencode=True, crf=cfg.render.crf,
        preset=cfg.render.preset, width=cfg.render.width, height=cfg.render.height,
        fps=cfg.render.fps, threads=cfg.render.threads,
    )

    total = ffmpeg.probe(final).duration
    ratio = round(quote_sec / total, 4) if total else 0.0

    # 7. 脚本落盘
    script = {
        "tag": tag, "variant": variant, "index": index, "mode": "overlay",
        "video": str(src), "title": meta.get("title", ""), "desc": meta.get("desc", ""),
        "degraded": degraded, "warnings": meta.get("warnings") or [],
        "duration": round(total, 2),
        "quote_total": round(quote_sec, 2), "talk_total": round(talk_sec, 2),
        "quote_ratio": ratio,
        "segments": timeline,
    }
    sdir = ensure_dir(out_dir / "script")
    atomic_write_json(sdir / f"{stem}.json", script)
    _write_md(script, sdir / f"{stem}.md", ccfg)

    files = {p.name for p in [final] + segments}
    return {
        "path": str(final), "stem": stem, "duration": round(total, 2),
        "script": str(sdir / f"{stem}.json"), "md": str(sdir / f"{stem}.md"),
        "title": meta.get("title") or title_hint, "desc": meta.get("desc", ""),
        "quote_ratio": ratio, "quote_total": round(quote_sec, 2),
        "degraded": degraded, "segments": timeline, "files": files,
        "mode": "overlay",
    }

def _text_in(strips: list[dict], sentences: list[dict],
             start: float, end: float, limit: int = 100) -> str:
    """取区间内的台词：OCR 硬字幕优先，退回 ASR 句子。

    源片是硬字幕时（短剧基本都烧了字幕），OCR 比 ASR 准也快；两条都没有
    才是真的无解，那时口播只能靠画面猜，脚本里会标 degraded。
    """
    parts: list[str] = []
    for s in strips or []:
        if float(s["end"]) > start and float(s["start"]) < end:
            t = str(s.get("text") or "").strip()
            if t and (not parts or t != parts[-1]):
                parts.append(t)
    if not parts:
        for s in sentences or []:
            if float(s["end"]) > start and float(s["start"]) < end:
                t = str(s.get("text") or "").strip()
                if t and (not parts or t != parts[-1]):
                    parts.append(t)
    txt = " / ".join(parts) if parts else ""
    return txt[:limit]


def _pick_quotes(beats: list[dict], sentences: list[dict],
                 ccfg: CommentaryCfg, src_dur: float) -> list[dict]:
    """每段最多截 quote_max，起点对齐到段内第一句完整台词的开口。

    为什么不在段中间随便截 5 秒：切在半句话上，观众听不出说的是什么，
    引用就白费了；而「一整句台词」通常 2-5 秒，正好落在合规区间里。
    """
    out: list[dict] = []
    for b in beats:
        lo = max(0.0, float(b["start"]))
        hi = min(float(src_dur), float(b["end"]))
        if hi - lo < 1.6:
            continue
        starts = [float(s["start"]) for s in sentences
                  if lo - 0.25 <= float(s["start"]) <= hi - 0.8]
        start = min(starts) if starts else lo
        dur = min(ccfg.quote_max, hi - start)
        if dur < ccfg.quote_min:
            start = lo
            dur = min(ccfg.quote_max, hi - lo)
        if dur < ccfg.quote_min:
            continue
        line = (b.get("line") or b.get("text") or "").strip()
        out.append({
            "start": round(start, 3), "end": round(start + dur, 3),
            "dur": round(dur, 3), "role": b.get("role", "rise"),
            "text": _clip(line, 40), "context": line,
        })
    # 引用条数上限 max_quotes：再多口播就压不住占比，片子也会变成报流水账
    if len(out) > ccfg.max_quotes:
        keep = sorted(range(len(out)),
                      key=lambda i: (out[i]["role"] != "hook", out[i]["role"] != "climax", i))
        out = [out[i] for i in sorted(keep[:ccfg.max_quotes])]
    return out


def _enforce_ratio(quotes: list[dict], talks: list[dict],
                   ccfg: CommentaryCfg) -> tuple[list[dict], list[dict]]:
    """两条闸门：引用占比 ≤ quote_ratio_max，总时长 ≤ target_seconds。

    先用字数估口播时长做预判（还没合成，拿不到真实时长），超了就逐条压短；
    全部压到 quote_min 还超，就丢掉最后一条引用并把它的口播并到前一段。
    解说片的时长宁可短：抖音上 60-90 秒的完播率明显好于 2 分钟。
    """
    def est_talk() -> float:
        return sum(max(3.0, len(t["text"]) / _TTS_FALLBACK_CPS + ccfg.lead_pad)
                   for t in talks)

    def too_long() -> bool:
        q = sum(x["dur"] for x in quotes)
        return q > ccfg.quote_ratio_max * (q + est_talk()) and q > 0

    def too_slow() -> bool:
        return (sum(x["dur"] for x in quotes) + est_talk()) > ccfg.target_seconds

    guard = 0
    while (too_long() or too_slow()) and guard < 40:
        guard += 1
        longest = max(range(len(quotes)), key=lambda i: quotes[i]["dur"])
        q = quotes[longest]
        if q["dur"] > ccfg.quote_min + 0.25 and too_long():
            q["dur"] = round(q["dur"] - 0.5, 3)
            q["end"] = round(q["start"] + q["dur"], 3)
        elif len(quotes) > 2:
            quotes.pop()
            if len(talks) > len(quotes) + 1:
                # 丢掉一条引用后，它前后两段口播要并成一段，否则时间轴会错位。
                # 合并必须截断到 talk_max_chars：直接拼接会得到一段 40 秒的
                # 独白，比原来的「引用太多」更糟。
                a = len(quotes)
                merged = talks[a]["text"] + talks[a + 1]["text"]
                talks = (talks[:a]
                         + [{"i": a, "text": _clip_sentence(merged, ccfg.talk_max_chars)}]
                         + talks[a + 2:])
        else:
            break
    return quotes, talks


# ---------------------------------------------------------------- 口播词

def _narrate(quotes: list[dict], n_talks: int, title_hint: str,
             cfg: Config) -> tuple[list[dict], dict, bool]:
    ccfg = cfg.commentary
    if not ccfg.llm or not cfg.planner.enabled or not cfg.planner.api_key:
        if ccfg.llm and not cfg.planner.api_key:
            log.warning("没有 DeepSeek key，口播词退化为规则版（必须人工重写）")
        return _fallback(quotes, title_hint), {"title": title_hint, "desc": ""}, True

    structure = _structure(quotes)
    prompt = TEMPLATE.format(
        structure=structure, n_talks=n_talks,
        min_c=ccfg.talk_min_chars, max_c=ccfg.talk_max_chars,
    )
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}]
    raw = ""
    for attempt in (1, 2):
        try:
            raw = planner._post(cfg.planner, msgs, 0.8)
            data = planner._parse(raw)
        except Exception as e:                       # noqa: BLE001
            log.warning(f"口播词生成失败（第 {attempt} 次）：{e}")
            continue
        talks = _clean_talks(data.get("talks"), n_talks, ccfg)
        bad = sorted({w for t in talks for w in _bad_words(t["text"])})
        if not bad:
            return talks, {"title": _clip(str(data.get("title") or title_hint), 16),
                           "desc": _clip(str(data.get("desc") or ""), 70)}, False
        log.warning(f"口播词命中禁用词 {bad}，要求重写")
        msgs = msgs[:2] + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"上一版含有这些禁用词：{'、'.join(bad)}。"
                                        "换成不点名的说法重写全部口播词，其余规则不变。"},
        ]
    log.warning("口播词两次都不合格，退化为规则版")
    return _fallback(quotes, title_hint), {"title": title_hint, "desc": ""}, True


def _structure(quotes: list[dict]) -> str:
    lines = ["口播0（开场，3-4 句）"]
    for i, q in enumerate(quotes):
        role = {"hook": "钩子", "setup": "铺垫", "rise": "推进",
                "climax": "高潮", "cliff": "悬念"}.get(q["role"], "推进")
        lines.append(f"引用{i + 1}（{role}，原片 {q['start']:.1f}-{q['end']:.1f}s，"
                     f"{q['dur']:.1f} 秒，台词：{q.get('context') or '无'}）")
        lines.append(f"口播{i + 1}")
    return "\n".join(lines)


def _clean_talks(raw, n: int, ccfg: CommentaryCfg) -> list[dict]:
    src = raw if isinstance(raw, list) else []
    texts = []
    for x in src:
        if isinstance(x, dict):
            t = str(x.get("text") or "").strip()
            if t:
                texts.append(t)
        elif isinstance(x, str) and x.strip():
            texts.append(x.strip())
    if len(texts) < n:
        raise ValueError(f"only {len(texts)} talks (need {n})")
    out = []
    for i, t in enumerate(texts[:n]):
        t = re.sub(r"\s+", "", t)
        if len(t) > ccfg.talk_max_chars:
            t = _clip_sentence(t, ccfg.talk_max_chars)
        out.append({"i": i, "text": t})
    return out


def _fallback(quotes: list[dict], title_hint: str) -> list[dict]:
    """规则兜底：只保证结构完整，内容必须人工重写（脚本里会标 degraded）。"""
    first = quotes[0]["text"][:26] if quotes else ""
    talks = [{"i": 0, "text": f"这段先说结果。{first}。她这一步不是临时起意，是前面就铺好的。"}
             if first else
             {"i": 0, "text": "这段先说结果，她的反应比在场所有人都快一步。"}]
    for i, q in enumerate(quotes):
        t = q["text"][:24]
        talks.append({"i": i + 1,
                      "text": (f"到这儿就清楚了：{t}。旁边的人还在按老规矩办事，"
                               f"她已经把后手摆出来了。") if t else
                              "到这儿就清楚了，她早就不打算再按对方给的规矩走。"})
    return talks[:len(quotes) + 1]


# ---------------------------------------------------------------- 配音

def _tts(text: str, dst: Path, ccfg: CommentaryCfg) -> float:
    """macOS say 合成中文配音，返回真实时长。失败则按字数估时长。"""
    if not ccfg.tts or not shutil.which("say"):
        return max(2.5, len(text) / _TTS_FALLBACK_CPS)

    aiff = dst.with_suffix(".aiff")
    try:
        p = subprocess.run(
            ["say", "-v", ccfg.voice, "-r", str(int(ccfg.rate)), "-o", str(aiff), text],
            capture_output=True, text=True, timeout=180,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[:200])
        ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-i", str(aiff),
                    "-ar", "48000", "-ac", "2", str(dst)], timeout=120)
        return ffmpeg.duration_of(dst)
    except Exception as e:                          # noqa: BLE001
        log.warning(f"TTS 失败，按字数估时长：{e}")
        return max(2.5, len(text) / _TTS_FALLBACK_CPS)
    finally:
        Path(aiff).unlink(missing_ok=True)


# ---------------------------------------------------------------- 文字卡

def _render_card(text: str, idx: int, total: int, title: str,
                 ccfg: CommentaryCfg, dst: Path, cfg: Config) -> Path:
    """把口播词渲染成一张 1080x1920 的文字卡。

    底色刻意不用纯黑：QC 的 blackdetect 阈值是 pix_th=0.10，纯黑卡会被判成
    「黑场」直接不合格。深蓝灰（亮度约 19%）既够暗有质感，又不会被误判。
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = cfg.render.width, cfg.render.height
    fp = subtitle._font_path(cfg.subtitle.font)
    if not fp:
        raise RuntimeError("找不到可用的中文字体，请在 subtitle.font 指定路径")

    bg = _hex(ccfg.card_bg, (42, 51, 66))
    accent = _hex(ccfg.card_accent, (224, 164, 88))

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    # 竖向微渐变，避免大块纯色像 PPT
    top, bottom = _shift(bg, 14), _shift(bg, -12)
    for y in range(H):
        k = y / max(1, H - 1)
        draw.line([(0, y), (W, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))

    draw.rectangle([0, 0, W, 14], fill=accent)
    draw.text((72, 92), "剧情评述", font=_font(fp, 38), fill=accent)
    tag = f"{idx + 1:02d}/{total:02d}"
    tw = draw.textlength(tag, font=_font(fp, 38))
    draw.text((W - 72 - tw, 92), tag, font=_font(fp, 38), fill=(168, 176, 190))

    max_w = W - 168
    size, lines = 78, []
    for size in (78, 72, 66, 60, 54, 48, 44, 40, 36):
        f = _font(fp, size)
        lines = _wrap(text, max_w, f, draw)
        if len(lines) <= 7:
            break
    f = _font(fp, size)
    lh = int(size * 1.55)
    block_h = lh * len(lines)
    y = max(380, (H - block_h) // 2 - 60)
    for ln in lines:
        draw.text((84, y), ln, font=f, fill=(246, 247, 250))
        y += lh

    fy = H - 150
    draw.line([(84, fy - 46), (84 + 96, fy - 46)], fill=accent, width=5)
    draw.text((84, fy), ccfg.card_footer, font=_font(fp, 34), fill=(150, 158, 172))

    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, "PNG")
    return dst


def _font(path: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def _wrap(text: str, max_w: int, font, draw) -> list[str]:
    """按像素宽度折行，但不让标点落到行首。

    纯按宽度切会把「…就安静 / 了。」切成这样，读起来像断气。
    中文排版里行首禁则（标点不能起行）是基本功，这里做最小实现：
    下一个字是收尾标点就跟着当前行走，宁可超出几像素。
    """
    no_lead = "，。！？；：、）》」』…—,.!?;:"
    lines, cur = [], ""
    for ch in text:
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            if ch in no_lead:
                cur += ch
                continue
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [text]


def _hex(s: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        s = str(s).lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except Exception:                                # noqa: BLE001
        return default


def _shift(rgb: tuple[int, int, int], d: int) -> tuple[int, int, int]:
    return tuple(max(0, min(255, c + d)) for c in rgb)  # type: ignore[return-value]


# ---------------------------------------------------------------- 文本工具

def _bad_words(text: str) -> list[str]:
    return [w for w in _BANNED if w in text]


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n]


def _clip_sentence(s: str, n: int) -> str:
    """按标点截断，不切在半句话上。"""
    if len(s) <= n:
        return s
    head = s[:n]
    m = list(re.finditer(r"[。！？；，,.]", head))
    if m:
        return head[:m[-1].start() + 1]
    return head


def _write_md(script: dict, path: Path, ccfg: CommentaryCfg) -> None:
    try:
        lines = [
            f"# {script['title'] or '解说脚本'}",
            "",
            f"- 成片：{script['duration']}s，引用 {script['quote_total']}s"
            f"（占 {script['quote_ratio']:.1%}，上限 {ccfg.quote_ratio_max:.0%}）",
            f"- 简介：{script['desc']}",
            f"- 口播：{script['talk_total']}s",
        ]
        if script.get("degraded"):
            lines.append("- **口播词是规则兜底版，必须人工重写后再发**")
        for w in script.get("warnings") or []:
            lines.append(f"- 注意：{w}")
        lines += ["", "## 分镜（照这个顺序剪/拍）", ""]
        for s in script["segments"]:
            if s["kind"] == "talk_over":
                lines += [f"### 口播 {s['i'] + 1}（{s['dur']}s，压原片 "
                          f"{s['start']:.1f}s 起，{s.get('subs', 0)} 条叠加字幕）", "",
                          f"> {s['text']}", ""]
            elif s["kind"] == "talk":
                lines += [f"### 口播 {s['i'] + 1}（{s['dur']}s，文字卡）", "",
                          f"> {s['text']}", ""]
            else:
                lines += [f"### 引用 {s['i'] + 1}（{s['dur']}s，原片 "
                          f"{s['start']:.1f}-{s['end']:.1f}s，{s['role']}）", "",
                          f"- 文件：`{s['file']}`",
                          f"- 台词：{s['text'] or '（无）'}", ""]
        lines += [
            "## 使用说明",
            "",
            "1. 引用片段已去水印、内缩、原声压到 "
            f"{ccfg.quote_audio_gain_db:.0f}dB，可以直接当素材用；",
            "2. 想换成真人出镜，把文字卡段替换成对应时长的口播镜头即可，"
            "口播词和顺序不用改；",
            "3. 配音想换人，用同一份口播词重新合成，时间轴按新音频重算。",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:                           # noqa: BLE001
        log.warning(f"解说脚本 md 写入失败：{e}")
