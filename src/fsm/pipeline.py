"""单剧流水线编排：四路信号并行 → 对齐 → 召回 → 大模型剪辑单 → 渲染 → 质检。

全自动化的核心约定：任何单点失败都只降级、不中断。只有「拿不到视频元信息」
和「完全没有任何候选单元」会让这部剧失败，其余全部走兜底继续出片。
"""
from __future__ import annotations

import json
import shutil
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from . import bgm, commentary, digest, ffmpeg, overlay, planner, qc, recall, render, shots_desc, subtitle, timeline
from .config import Config
from .signals import asr as asr_sig
from .signals import ocr as ocr_sig
from .signals import shots as shots_sig
from .signals import vision as vision_sig
from .util import (
    StageBook, ensure_dir, get_logger, quick_hash, read_json, safe_name, atomic_write_json,
)

log = get_logger("pipeline")


def video_id_of(path: Path) -> str:
    stem = safe_name(path.stem, 60)
    return f"{stem}_{quick_hash(path, 1 << 19)}"


def plan_counts(cfg: Config, n_reels: int | None = None,
                variants: int | None = None, total: int | None = None) -> list[int]:
    """把「出几条」翻译成每套方案的条数列表。

    三种说法都支持，优先级 total > variants > reels > 配置默认：
      --total 6      → [3, 3]（自动拆套，单套不超过 output.per_plan）
      --variants 3   → [2, 2, 2]（每套按 output.reels）
      --reels 2      → [2]
    """
    o = cfg.output
    if total:
        return planner.split_counts(total, o.per_plan, o.max_total)
    n = max(1, int(n_reels or o.reels))
    v = max(1, min(int(variants or o.variants), max(1, o.max_total)))
    return [n] * v


def _load_plan(work: Path, cands: list[dict], n: int, variant: int,
               used_hooks: set[str], force: bool) -> dict | None:
    """读缓存的剪辑单，指纹不一致就当作没有。

    以前缓存文件只按视频 id 命名，改 --reels 也读回旧的 2 条方案，
    用户要的条数和实际出片对不上。现在每个套号一个文件，文件里带指纹。
    """
    if force:
        return None
    path = work / f"plan_v{variant:02d}.json"
    if not path.exists():
        return None
    data = read_json(path, {}) or {}
    if data.get("n_reels") != n:
        return None
    if data.get("fp") != planner.fingerprint(cands, n, variant, used_hooks):
        return None
    plan = data.get("plan") or {}
    return plan if plan.get("reels") else None


def process(
    video: str | Path, cfg: Config, n_reels: int | None = None, force: bool = False,
    rerender: bool = False, sub_mode: str | None = None,
    variants: int | None = None, total: int | None = None,
    mode: str | None = None,
) -> dict:
    video = Path(video)
    if not video.exists():
        return {"ok": False, "video": str(video), "error": "file not found"}

    mode = (mode or ("commentary" if cfg.commentary.enabled else "clip")).lower()
    commentary_mode = mode in ("commentary", "overlay")

    vid = video_id_of(video)
    work = ensure_dir(Path(cfg.paths.work) / vid)
    book = StageBook(work / "status.json")
    book.data["video"] = str(video)
    if rerender:
        # 只作废渲染阶段，信号/剪辑单全部复用，方便调渲染参数时快速迭代
        for k in [k for k in book.data["stages"]
                  if k.startswith(("render_", "mix_"))]:
            book.data["stages"].pop(k, None)
    book.save()

    # ---------------------------------------------------------------- probe
    try:
        info = ffmpeg.probe(video)
    except ffmpeg.FFmpegError as e:
        return {"ok": False, "video": str(video), "video_id": vid, "error": f"probe failed: {e}"}
    book.mark("probe", "ok", duration=round(info.duration, 3),
              size=f"{info.width}x{info.height}", fps=round(info.fps, 2))
    log.info(f"[{vid}] {video.name} {info.duration:.1f}s {info.width}x{info.height}")

    # 源片没有音轨是致命的前置问题：ASR 拿不到输入，剪辑单只能靠画面和
    # OCR 猜，成片里也永远不会有任何人声。以前这里只是把 asr 标记成 failed
    # 就继续往下跑，结果渲染出一条「只有配乐」的片子，QC 还判通过——
    # 所以要在这里就把话说明白，并一路带到 report 里。
    warnings: list[str] = []
    if not info.has_audio:
        msg = "源视频无音轨：ASR 无输入，成片只有配乐、没有人声"
        log.error(f"[{vid}] {msg}")
        warnings.append(msg)

    # ---------------------------------------------------------------- 信号（并行）
    shots, vis, sentences, strips = _signals(video, info, work, book, cfg, force)
    # 分镜解说文字卡：用 OCR 采样定位原字幕带顶边，把解说放在它上方一点点。
    try:
        from .signals.ocr import band_top as _band_top
        ratio = _band_top(str(video), cfg.ocr, info.duration)
        if ratio:
            sh = info.height
            dw = cfg.render.dewatermark
            top_crop = 0
            if str(getattr(dw, "mode", "")).lower() == "crop":
                top_crop = int(sh * float(getattr(dw, "crop_ratio", 0) or 0)) // 2 * 2
            ch = max(1, sh - top_crop)
            ov = cfg.render.overlay
            z = max(1.0, ov.zoom)
            y = ((ratio * sh - top_crop) * (cfg.render.height / ch) * z
                 - (cfg.render.height * (z - 1)) / 2)
            cap_h = ov.caption_size * 2.4
            ov.caption_top = int(max(120, min(cfg.render.height * 0.9,
                                              y - cap_h - 16)))
            log.info(f"解说位置：OCR 字幕顶 {ratio:.3f} → caption_top={ov.caption_top}")
    except Exception as e:                           # noqa: BLE001
        log.debug(f"band_top 采样失败，沿用配置位置: {e}")

    # ---------------------------------------------------------------- 时间轴
    tl = read_json(work / "timeline.json") if book.ok("timeline") and not force else None
    if tl is None:
        with book.run("timeline", force=force):
            tl = timeline.build(shots, vis, sentences, strips, info, cfg.recall)
            atomic_write_json(work / "timeline.json", tl)
    units = tl.get("units") or []
    if not units:
        return {"ok": False, "video": str(video), "video_id": vid,
                "error": "no units (video too short or decode failed)"}

    # ---------------------------------------------------------------- 召回
    # 黑场区间先于召回算出来，让「片尾黑底白字字幕卡」在打分阶段就被压下去，
    # 而不是等大模型选中、渲染完、QC 才报错。
    blacks = _blacks(video, work, book, force)
    rec = read_json(work / "recall.json") if book.ok("recall") and not force else None
    if rec is None:
        with book.run("recall"):
            rec = recall.recall(units, info.duration, cfg.recall, blacks)
            atomic_write_json(work / "recall.json", rec)
    cands = rec.get("candidates") or []
    if not cands:
        return {"ok": False, "video": str(video), "video_id": vid, "error": "no candidates"}

    # ---------------------------------------------------------------- 剪辑单
    # 一部剧可以出多套方案（组合式出片）：每套 2-3 条，套与套之间不复用 hook、
    # 候选重合率也压住。这样同一部剧能连续产出多条互不撞车的切片，
    # 而不是把一条 60 秒的片子反复重编码成 8 个近似版。
    counts = plan_counts(cfg, n_reels=n_reels, variants=variants, total=total)
    log.info(f"[{vid}] 出片计划：{len(counts)} 套 / 共 {sum(counts)} 条 {counts}")
    plans: list[dict] = []
    used_hooks: set[str] = set()
    used_prior: list[list[str]] = []
    for v, n in enumerate(counts, start=1):
        pl = _load_plan(work, cands, n, v, used_hooks, force)
        if pl is None:
            with book.run(f"plan_{v}"):
                pl = planner.plan(
                    cands, cfg.planner, n_reels=n,
                    variant=v, variants_total=len(counts),
                    used_hooks=used_hooks, used_prior=used_prior,
                    dedupe_hook=cfg.output.dedupe_hook,
                    overlap_max=cfg.output.overlap,
                )
                atomic_write_json(work / f"plan_v{v:02d}.json", {
                    "fp": planner.fingerprint(cands, n, v, used_hooks),
                    "n_reels": n, "variant": v, "plan": pl,
                })
        vreels = pl.get("reels") or []
        if not vreels:
            log.warning(f"[{vid}] 第 {v} 套方案没有产出可用切片")
        for r in vreels:
            h = next((b["cand_id"] for b in r.get("beats") or []
                      if b.get("role") == "hook"), "")
            if h:
                used_hooks.add(h)
        used_prior.append(sorted({b["cand_id"] for r in vreels
                                  for b in r.get("beats") or []}))
        plans.append(pl)

    # 摊平成 (套号, 套内序号, 方案) 的线性列表，后面渲染按这个顺序跑
    reels_flat = [(v, i, r) for v, pl in enumerate(plans, start=1)
                  for i, r in enumerate(pl.get("reels") or [], start=1)]
    if not reels_flat:
        return {"ok": False, "video": str(video), "video_id": vid,
                "error": "planner produced no reel"}

    # ---------------------------------------------------------------- 渲染 + 质检
    day = date.today().isoformat()
    out_dir = ensure_dir(Path(cfg.paths.out) / day / vid)
    results: list[dict] = []
    produced: set[str] = set()      # 本批次实际产出的文件，用于清理历史残留

    # ---------------------------------------------------------------- 自动补字幕
    # 判定用整部剧的 OCR 字幕覆盖率，不逐条切片判定：同一部剧不该出现
    # 「这条有字幕、那条没有」。覆盖率比条数可靠——无字幕片里 OCR 仍会
    # 零星抓到台标、集数标题和画面招牌。
    sub_dec = subtitle.decide(strips, info.duration, cfg.subtitle, sub_mode)
    if commentary_mode:
        # 解说模式的口播压在文字卡上，再给引用段补原片字幕只会打架
        sub_dec = {"needed": False, "coverage": sub_dec.get("coverage", 0.0),
                   "reason": "解说模式：文字卡承载口播"}
    if sub_dec["needed"] and not sentences:
        log.warning(f"[{vid}] 需要补字幕但没有 ASR 句子，跳过")
        sub_dec["needed"] = False
    if sub_dec["needed"]:
        log.info(f"[{vid}] 自动补字幕：{sub_dec['reason']}，可用 ASR {len(sentences)} 句")

    tracks = (bgm.load_index(cfg.bgm)
              if (cfg.bgm.enabled and (not commentary_mode or cfg.commentary.bgm)) else [])
    flat_reels = [r for _, _, r in reels_flat]
    # 不同剧用不同的曲子：把最近用过的 id 传给选曲，选完回写。
    recent_path = Path(cfg.paths.work) / "bgm_recent.json"
    try:
        recent = json.loads(recent_path.read_text(encoding="utf-8"))
        if not isinstance(recent, list):
            recent = []
    except (OSError, ValueError):
        recent = []
    picks = bgm.choose(tracks, flat_reels, cfg.planner, cfg.bgm,
                       avoid=set(recent[-8:])) if tracks else []
    used = [p.get("bgm_id") for p in picks if p.get("bgm_id")]
    if used:
        recent = (recent + used)[-20:]
        try:
            recent_path.write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if tracks:
        log.info(f"[{vid}] BGM 库 {len(tracks)} 首，已为 {len(picks)} 条切片选曲")
    else:
        log.warning(f"[{vid}] 没有可用 BGM，输出无配乐版本")

    for k, (v, i, reel) in enumerate(reels_flat, start=1):
        # 解说模式走完全不同的产出形态：口播卡 + 短引用，音轨重做。
        # 不共用下面的 BGM/补字幕/拼接逻辑，因为那套都是「一条原片」的假设。
        if commentary_mode:
            try:
                with book.run(f"commentary_{v}_{i}"):
                    fn = commentary.build_overlay if mode == "overlay" else commentary.build
                    cres = fn(
                        src=str(video), reel=reel,
                        work=work / f"v{v:02d}" / f"reel{i:02d}",
                        out_dir=out_dir, cfg=cfg, info=info, sentences=sentences,
                        shots=tl.get("shots") or [], vis=vis, strips=strips,
                        tag=f"v{v:02d}-{i:02d}", variant=v, index=i,
                    )
            except Exception as e:                   # noqa: BLE001
                log.warning(f"[{vid}] commentary v{v}-{i} failed: {e}")
                results.append({"index": i, "variant": v, "ok": False,
                                "error": f"commentary: {e}"[:300]})
                continue

            cover = out_dir / f"{cres['stem']}.jpg"
            try:
                ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-ss", "2",
                            "-i", cres["path"], "-frames:v", "1",
                            "-vf", "scale=540:-2", str(cover)], timeout=120)
            except ffmpeg.FFmpegError:
                cover = None
            try:
                quality = qc.check(cres["path"], cfg.render, expect_audio=True)
            except Exception as e:                   # noqa: BLE001
                quality = {"ok": False, "issues": [f"qc crashed: {e}"]}

            produced |= set(cres["files"])
            produced.add(Path(cres["path"]).name)
            if cover:
                produced.add(cover.name)
            results.append({
                "index": i, "variant": v, "ok": True, "mode": mode,
                "title": cres["title"], "hook": reel.get("hook", ""),
                "reason": reel.get("reason", ""),
                "path": cres["path"], "cover": str(cover) if cover else None,
                "duration": cres["duration"],
                "quote_ratio": cres["quote_ratio"],
                "quote_total": cres["quote_total"],
                "degraded": cres["degraded"],
                "script": cres["script"], "md": cres["md"],
                "beats": [
                    {"role": s.get("role") or s.get("kind", ""),
                     "src_start": s.get("start") or 0.0,
                     "src_end": s.get("end") or 0.0,
                     "dur": s["dur"], "text": (s.get("text") or "")[:60]}
                    for s in cres["segments"]
                ],
                "qc": quality,
            })
            continue

        beats = render.snap_to_shots(reel["beats"], tl.get("shots") or [])
        beats = render.trim_blank(beats, vis)
        if len(beats) < 2:
            log.warning(f"[{vid}] v{v} reel {i} has too few usable beats, skipped")
            results.append({"index": i, "variant": v, "ok": False,
                            "error": "all beats blank after trim"})
            continue

        # 节拍对齐必须在排序之后：对齐是按「成片时间轴」累加计算的，
        # 而 render 内部会按 role 重排，顺序不一致就白算了。
        beats = render.order_beats(beats)
        pick = picks[k - 1] if k - 1 < len(picks) else {}
        track = pick.get("track")
        align_stats: dict = {"aligned": False, "reason": "无可用 BGM"}
        if track:
            beats, align_stats = bgm.align_to_beats(
                beats, track, pick["start"], cfg.bgm, vis)

        # 分镜描述：按 shots 的真实切点给每个镜头一句，挂在 beat.shot_descs 上。
        try:
            shots_desc.build(beats, tl.get("shots") or [], sentences, cfg.planner)
        except Exception as e:                       # noqa: BLE001
            log.warning(f"[{vid}] v{v} reel{i} 分镜描述失败，退回 beat caption: {e}")

        # 字幕必须在 align 之后算：align_to_beats 会把段尾延长到鼓点，
        # 用延长前的区间算字幕会漏掉最后一两句台词。
        sub_count = 0
        if sub_dec["needed"]:
            per_beat = subtitle.build(
                sentences, beats, info, cfg.render, cfg.subtitle,
                work / f"v{v:02d}" / f"reel{i:02d}" / "subs",
            )
            for b, s in zip(beats, per_beat):
                b["subs"] = s
                sub_count += len(s)
            log.info(f"[{vid}] v{v} reel{i} 补字 {sub_count} 条")

        try:
            with book.run(f"render_{v}_{i}"):
                rendered = render.render(
                    str(video), beats, work / f"v{v:02d}" / f"reel{i:02d}", cfg.render,
                    has_audio=info.has_audio, tag=f"v{v:02d}_reel_{i:02d}",
                    src_size=(info.width, info.height),
                )
        except ffmpeg.FFmpegError as e:
            log.warning(f"[{vid}] render v{v}-{i} failed: {e}")
            results.append({"index": i, "variant": v, "ok": False, "error": str(e)[:300]})
            continue

        # 叠加层：放大裁切（去边缘水印）+ 分镜描述 + 旋转水印。
        # 放在拼接之后、混音之前，一次重编码完成。
        try:
            with book.run(f"overlay_{v}_{i}"):
                over_path = work / f"v{v:02d}" / f"reel{i:02d}" / "overlay.mp4"
                overlay.apply(str(rendered["path"]), str(over_path), beats,
                              cfg.render, work / f"v{v:02d}" / f"reel{i:02d}")
            rendered["path"] = str(over_path)
        except Exception as e:                       # noqa: BLE001
            log.warning(f"[{vid}] v{v} reel {i} 叠加层失败，保留无叠加版本: {e}")

        # 混音放在拼接之后：这样 BGM 是一条连续的曲子，
        # 而不是每个片段各自 fade-in 一遍。
        mixed: dict = {}
        if track:
            try:
                with book.run(f"mix_{v}_{i}"):
                    mixed = bgm.mix(
                        str(rendered["path"]),
                        str(work / f"v{v:02d}" / f"reel{i:02d}" / "mixed.mp4"),
                        track, pick["start"], cfg.bgm,
                        has_voice=info.has_audio,
                    )
                rendered["path"] = mixed["path"]
            except Exception as e:
                log.warning(f"[{vid}] v{v} reel {i} 混音失败，输出无配乐版本: {e}")

        title = reel.get("title") or f"{video.stem}_切片{v}-{i}"
        stem = f"v{v:02d}-{i:02d}_{safe_name(title, 22)}"
        final = out_dir / f"{stem}.mp4"
        try:
            Path(rendered["path"]).replace(final)
        except OSError:
            # 沙箱里 rename 可能被拦，退化成复制：成片必须落在 out/ 里，
            # 否则用户拿到的是 work/ 下的中间路径，目录一清就没了。
            try:
                shutil.copy2(rendered["path"], final)
            except OSError:
                final = Path(rendered["path"])

        cover = out_dir / f"{stem}.jpg"
        try:
            ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(final),
                        "-frames:v", "1", "-vf", "scale=540:-2", str(cover)], timeout=120)
        except ffmpeg.FFmpegError:
            cover = None
        produced.add(final.name)
        if cover:
            produced.add(cover.name)

        try:
            quality = qc.check(str(final), cfg.render, expect_audio=info.has_audio)
        except Exception as e:
            quality = {"ok": False, "issues": [f"qc crashed: {e}"]}

        # QC 只查「有没有音轨」，查不出「音轨里只有配乐」。源片无声时
        # 成片照样有 aac 流、响度也正常（BGM 撑着），所以必须显式判死，
        # 否则这种片子会被当成合格品投出去。
        if not info.has_audio:
            quality["issues"].append("源片无音轨，成片无人声")
            quality["ok"] = False

        results.append({
            "index": i, "variant": v, "ok": True,
            "title": title,
            "hook": reel.get("hook", ""),
            "reason": reel.get("reason", ""),
            "path": str(final),
            "cover": str(cover) if cover else None,
            "duration": rendered["duration"],
            "beats": rendered["timeline"],
            "strategy": reel.get("strategy", plans[v - 1].get("strategy")),
            "bgm": {
                "id": mixed.get("bgm_id") or (track or {}).get("id"),
                "title": mixed.get("bgm_title") or (track or {}).get("title", ""),
                "start": mixed.get("bgm_start"),
                "bpm": mixed.get("bpm"),
                "mixed": bool(mixed),
                "attribution": mixed.get("attribution") or (pick.get("attribution") if track else ""),
            },
            "beat_align": align_stats,
            "subtitle": {"auto": sub_count > 0, "count": sub_count},
            "qc": quality,
        })

    # 清掉这部剧目录里不属于本批次的旧成片。
    # 否则重跑一次就多出一套文件，总览页会把历史输出和新输出混在一起，
    # 看起来像"一条剧出了 7 条切片"。
    # 注意：macOS 的文件名是 NFD，而 Python 字符串/JSON 里通常是 NFC，
    # 直接比字符串会全部不匹配，必须先规范化。
    if produced:
        want = {unicodedata.normalize("NFC", x) for x in produced}
        for stale in list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.jpg")):
            if unicodedata.normalize("NFC", stale.name) not in want:
                stale.unlink(missing_ok=True)

    ok_reels = [r for r in results if r.get("ok")]
    report = {
        "ok": bool(ok_reels),
        "video": str(video),
        "video_id": vid,
        "duration": round(info.duration, 2),
        "resolution": f"{info.width}x{info.height}",
        "has_audio": info.has_audio,
        "warnings": warnings,
        "mode": mode,
        "strategy": plans[0].get("strategy") if plans else None,
        "plan_error": next((p.get("error") for p in plans if p.get("error")), None),
        "variants": len(counts),
        "reels_per_variant": counts,
        "stats": tl.get("stats"),
        "candidate_count": len(cands),
        "autosub": {
            "needed": sub_dec["needed"],
            "coverage": sub_dec["coverage"],
            "reason": sub_dec["reason"],
        },
        "reels": results,
        "reel_count": len(ok_reels),
        "elapsed_stages": {k: v for k, v in book.data["stages"].items()},
    }
    atomic_write_json(work / "report.json", report)
    atomic_write_json(out_dir / "report.json", report)
    _write_markdown(report, out_dir / "切片说明.md")
    digest.write_index(Path(cfg.paths.out) / day)
    log.info(f"[{vid}] done: {len(ok_reels)}/{len(results)} reels")
    return report


# ---------------------------------------------------------------- 内部分步

def _signals(video, info, work: Path, book: StageBook, cfg: Config, force: bool):
    need = force or not all(
        book.ok(s) for s in ("shots", "vision", "asr", "ocr")
    )
    if not need:
        return (
            _load_shots(work),
            read_json(work / "vision.json", {}) or {},
            (read_json(work / "asr.json", {}) or {}).get("sentences", []),
            (read_json(work / "ocr.json", {}) or {}).get("strips", []),
        )

    audio = work / "audio.wav"
    if info.has_audio and not audio.exists():
        try:
            ffmpeg.extract_audio(video, audio)
        except ffmpeg.FFmpegError as e:
            log.warning(f"audio extract failed: {e}")

    def run_shots_vision():
        try:
            if not (book.ok("shots") and not force):
                with book.run("shots", force=force):
                    sh = shots_sig.detect(str(video), info.duration, cfg.vision)
                    atomic_write_json(work / "shots.json", [s.to_dict() for s in sh])
            if not (book.ok("vision") and not force):
                with book.run("vision", force=force):
                    v = vision_sig.analyze(str(video), info.duration, cfg.vision)
                    atomic_write_json(work / "vision.json", v)
        except Exception as e:
            log.warning(f"shots/vision degraded: {type(e).__name__}: {e}")
            if not (work / "shots.json").exists():
                atomic_write_json(work / "shots.json", [])
            if not (work / "vision.json").exists():
                atomic_write_json(work / "vision.json", {"samples": [], "fps": cfg.vision.fps})

    def run_asr():
        if not cfg.asr.enabled:
            book.mark("asr", "ok", skipped="disabled")
            return
        if book.ok("asr") and not force:
            return
        if not audio.exists():
            book.mark("asr", "failed", error="no audio track")
            atomic_write_json(work / "asr.json", {"ok": False, "sentences": []})
            return
        try:
            with book.run("asr", force=force):
                raw = asr_sig.transcribe(audio, work / "asr_raw.json", cfg.asr)
                sents = []
                if raw.get("ok"):
                    sents = asr_sig.split_sentences(
                        raw.get("text", ""), raw.get("timestamp", []),
                        info.duration, raw.get("emotions"),
                    )
                atomic_write_json(work / "asr.json", {
                    "ok": bool(raw.get("ok")), "error": raw.get("error"),
                    "sentences": sents, "char_count": len(raw.get("text", "")),
                    "engine": raw.get("engine"),
                })
                if not raw.get("ok"):
                    raise RuntimeError(raw.get("error", "asr failed"))
        except Exception as e:
            log.warning(f"ASR degraded: {type(e).__name__}: {e}")
            if not (work / "asr.json").exists():
                atomic_write_json(work / "asr.json", {"ok": False, "sentences": []})

    def run_ocr():
        if not cfg.ocr.enabled:
            book.mark("ocr", "ok", skipped="disabled")
            return
        if book.ok("ocr") and not force:
            return
        try:
            with book.run("ocr", force=force):
                strips = ocr_sig.extract(str(video), cfg.ocr, info.duration)
                atomic_write_json(work / "ocr.json", {"ok": True, "strips": strips})
        except Exception as e:
            log.warning(f"OCR degraded: {type(e).__name__}: {e}")
            if not (work / "ocr.json").exists():
                atomic_write_json(work / "ocr.json", {"ok": False, "strips": []})

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_shots_vision), pool.submit(run_asr), pool.submit(run_ocr)]
        for f in futures:
            f.result()

    return (
        _load_shots(work),
        read_json(work / "vision.json", {}),
        (read_json(work / "asr.json", {}) or {}).get("sentences", []),
        (read_json(work / "ocr.json", {}) or {}).get("strips", []),
    )


def _load_shots(work: Path) -> list:
    from .signals.shots import Shot

    raw = read_json(work / "shots.json", []) or []
    return [Shot(idx=s["idx"], start=s["start"], end=s["end"]) for s in raw]


def _blacks(video: Path, work: Path, book: StageBook, force: bool) -> list[tuple[float, float]]:
    """全片黑场区间。失败不影响主流程，只是少了这一道惩罚。"""
    path = work / "blacks.json"
    if book.ok("blacks") and not force and path.exists():
        return [tuple(x) for x in (read_json(path, []) or [])]
    try:
        with book.run("blacks", force=force):
            found = ffmpeg.detect_black(str(video))
            atomic_write_json(path, [list(x) for x in found])
            return found
    except Exception as e:
        log.warning(f"blackdetect degraded: {type(e).__name__}: {e}")
        atomic_write_json(path, [])
        return []


def _write_markdown(report: dict, path: Path) -> None:
    try:
        lines = [
            f"# {Path(report['video']).stem} 切片说明",
            "",
            f"- 原片：{report['duration']}s，{report['resolution']}",
            f"- 候选片段：{report['candidate_count']} 个",
            f"- 剪辑策略：{report['strategy']}",
            f"- 产出：{report['reel_count']} 条"
            + (f"（{report.get('variants', 1)} 套方案："
               f"{report.get('reels_per_variant')}）" if report.get("variants", 1) > 1 else ""),
        ]
        sub = report.get("autosub") or {}
        if sub.get("needed"):
            lines.append(f"- 字幕：源片无内嵌字幕（OCR 覆盖率 "
                         f"{sub.get('coverage', 0):.1%}），已用 ASR 自动补字")
        elif sub:
            lines.append(f"- 字幕：沿用源片内嵌字幕（覆盖率 {sub.get('coverage', 0):.1%}）")
        lines.append("")
        for r in report["reels"]:
            if not r.get("ok"):
                lines.append(f"## 切片 {r['index']} 失败\n\n{r.get('error','')}\n")
                continue
            # 多套方案时把套号写进标题，否则「切片 1」会出现好几次
            head = (f"第 {r.get('variant', 1)} 套 · " if report.get("variants", 1) > 1 else "")
            lines += [
                f"## {head}切片 {r['index']}：{r['title']}",
                "",
                f"- 时长：{r['duration']}s",
                f"- 钩子：{r['hook']}",
                f"- 编排：{r['reason']}",
            ]
            if r.get("mode") in ("commentary", "overlay"):
                tail = ("口播压原片+叠加字幕" if r.get("mode") == "overlay"
                        else "口播为主体")
                lines.append(f"- 引用占比：{r.get('quote_ratio', 0):.0%}"
                             f"（{r.get('quote_total', 0)}s），{tail}")
                if r.get("degraded"):
                    lines.append("- **口播词是规则兜底版，发布前必须人工重写**")
                if r.get("md"):
                    lines.append(f"- 解说脚本：`{Path(r['md']).name}`")
            b = r.get("bgm") or {}
            if b.get("title"):
                al = r.get("beat_align") or {}
                grid = (f"，{al.get('beats_on_grid')}/{al.get('beats_total')} 段对齐鼓点"
                        if al.get("aligned") else "，未做节拍对齐")
                lines.append(f"- 配乐：{b['title']}（{b.get('bpm') or '?'} BPM{grid}）")
                if b.get("attribution"):
                    lines.append(f"- 配乐授权：{b['attribution']}")
            lines += [
                f"- 质检：{'通过' if r['qc'].get('ok') else '；'.join(r['qc'].get('issues') or [])}",
                "",
                "| 角色 | 原片区间 | 时长 | 台词 |",
                "|---|---|---|---|",
            ]
            for b in r["beats"]:
                lines.append(
                    f"| {b['role']} | {b['src_start']:.1f}-{b['src_end']:.1f}s "
                    f"| {b['dur']:.1f}s | {b['text']} |"
                )
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:  # 说明文档写不出来不影响成片
        log.warning(f"markdown report failed: {e}")
