"""单剧流水线编排：四路信号并行 → 对齐 → 召回 → 大模型剪辑单 → 渲染 → 质检。

全自动化的核心约定：任何单点失败都只降级、不中断。只有「拿不到视频元信息」
和「完全没有任何候选单元」会让这部剧失败，其余全部走兜底继续出片。
"""
from __future__ import annotations

import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from . import bgm, digest, ffmpeg, planner, qc, recall, render, timeline
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


def process(
    video: str | Path, cfg: Config, n_reels: int = 2, force: bool = False,
    rerender: bool = False,
) -> dict:
    video = Path(video)
    if not video.exists():
        return {"ok": False, "video": str(video), "error": "file not found"}

    vid = video_id_of(video)
    work = ensure_dir(Path(cfg.paths.work) / vid)
    book = StageBook(work / "status.json")
    book.data["video"] = str(video)
    if rerender:
        # 只作废渲染阶段，信号/剪辑单全部复用，方便调渲染参数时快速迭代
        for k in [k for k in book.data["stages"] if k.startswith("render_")]:
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

    # ---------------------------------------------------------------- 信号（并行）
    shots, vis, sentences, strips = _signals(video, info, work, book, cfg, force)

    # ---------------------------------------------------------------- 时间轴
    tl = read_json(work / "timeline.json") if book.ok("timeline") and not force else None
    if tl is None:
        with book.run("timeline"):
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
    pl = read_json(work / "plan.json") if book.ok("plan") and not force else None
    if pl is None:
        with book.run("plan"):
            pl = planner.plan(cands, cfg.planner, n_reels=n_reels)
            atomic_write_json(work / "plan.json", pl)
    reels = pl.get("reels") or []
    if not reels:
        return {"ok": False, "video": str(video), "video_id": vid, "error": "planner produced no reel"}

    # ---------------------------------------------------------------- 渲染 + 质检
    day = date.today().isoformat()
    out_dir = ensure_dir(Path(cfg.paths.out) / day / vid)
    results: list[dict] = []
    produced: set[str] = set()      # 本批次实际产出的文件，用于清理历史残留

    tracks = bgm.load_index(cfg.bgm) if cfg.bgm.enabled else []
    picks = bgm.choose(tracks, reels, cfg.planner, cfg.bgm) if tracks else []
    if tracks:
        log.info(f"[{vid}] BGM 库 {len(tracks)} 首，已为 {len(picks)} 条切片选曲")
    else:
        log.warning(f"[{vid}] 没有可用 BGM，输出无配乐版本")

    for i, reel in enumerate(reels, start=1):
        beats = render.snap_to_shots(reel["beats"], tl.get("shots") or [])
        beats = render.trim_blank(beats, vis)
        if len(beats) < 2:
            log.warning(f"[{vid}] reel {i} has too few usable beats, skipped")
            results.append({"index": i, "ok": False, "error": "all beats blank after trim"})
            continue

        # 节拍对齐必须在排序之后：对齐是按「成片时间轴」累加计算的，
        # 而 render 内部会按 role 重排，顺序不一致就白算了。
        beats = render.order_beats(beats)
        pick = picks[i - 1] if i - 1 < len(picks) else {}
        track = pick.get("track")
        align_stats: dict = {"aligned": False, "reason": "无可用 BGM"}
        if track:
            beats, align_stats = bgm.align_to_beats(
                beats, track, pick["start"], cfg.bgm, vis)

        try:
            with book.run(f"render_{i}"):
                rendered = render.render(
                    str(video), beats, work / f"reel{i:02d}", cfg.render,
                    has_audio=info.has_audio, tag=f"reel_{i:02d}",
                )
        except ffmpeg.FFmpegError as e:
            log.warning(f"[{vid}] render {i} failed: {e}")
            results.append({"index": i, "ok": False, "error": str(e)[:300]})
            continue

        # 混音放在拼接之后：这样 BGM 是一条连续的曲子，
        # 而不是每个片段各自 fade-in 一遍。
        mixed: dict = {}
        if track:
            try:
                with book.run(f"mix_{i}"):
                    mixed = bgm.mix(
                        str(rendered["path"]),
                        str(work / f"reel{i:02d}" / "mixed.mp4"),
                        track, pick["start"], cfg.bgm,
                        has_voice=info.has_audio,
                    )
                rendered["path"] = mixed["path"]
            except Exception as e:
                log.warning(f"[{vid}] reel {i} 混音失败，输出无配乐版本: {e}")

        title = reel.get("title") or f"{video.stem}_切片{i}"
        stem = f"{i:02d}_{safe_name(title, 24)}"
        final = out_dir / f"{stem}.mp4"
        try:
            Path(rendered["path"]).replace(final)
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

        results.append({
            "index": i, "ok": True,
            "title": title,
            "hook": reel.get("hook", ""),
            "reason": reel.get("reason", ""),
            "path": str(final),
            "cover": str(cover) if cover else None,
            "duration": rendered["duration"],
            "beats": rendered["timeline"],
            "strategy": reel.get("strategy", pl.get("strategy")),
            "bgm": {
                "id": mixed.get("bgm_id") or (track or {}).get("id"),
                "title": mixed.get("bgm_title") or (track or {}).get("title", ""),
                "start": mixed.get("bgm_start"),
                "bpm": mixed.get("bpm"),
                "mixed": bool(mixed),
                "attribution": mixed.get("attribution") or (pick.get("attribution") if track else ""),
            },
            "beat_align": align_stats,
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
        "strategy": pl.get("strategy"),
        "plan_error": pl.get("error"),
        "stats": tl.get("stats"),
        "candidate_count": len(cands),
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
                with book.run("shots"):
                    sh = shots_sig.detect(str(video), info.duration, cfg.vision)
                    atomic_write_json(work / "shots.json", [s.to_dict() for s in sh])
            if not (book.ok("vision") and not force):
                with book.run("vision"):
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
            with book.run("asr"):
                raw = asr_sig.transcribe(audio, work / "asr_raw.json", cfg.asr)
                sents = []
                if raw.get("ok"):
                    sents = asr_sig.split_sentences(
                        raw.get("text", ""), raw.get("timestamp", []), info.duration
                    )
                atomic_write_json(work / "asr.json", {
                    "ok": bool(raw.get("ok")), "error": raw.get("error"),
                    "sentences": sents, "char_count": len(raw.get("text", "")),
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
            with book.run("ocr"):
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
        with book.run("blacks"):
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
            f"- 产出：{report['reel_count']} 条",
            "",
        ]
        for r in report["reels"]:
            if not r.get("ok"):
                lines.append(f"## 切片 {r['index']} 失败\n\n{r.get('error','')}\n")
                continue
            lines += [
                f"## 切片 {r['index']}：{r['title']}",
                "",
                f"- 时长：{r['duration']}s",
                f"- 钩子：{r['hook']}",
                f"- 编排：{r['reason']}",
            ]
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
