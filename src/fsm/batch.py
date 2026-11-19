"""批量编排：每天数十部剧，进程级并行 + 单剧失败隔离。

并行策略：外层按剧开进程（绕开 GIL，也让 ASR/OCR 的内存互不干扰），
内层每部剧内部再并行三路信号。实测 10 核 M5 上 workers=3 是甜点，
再高会和 ASR 抢 CPU 反而变慢。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .pipeline import process
from .util import ensure_dir, get_logger, now_iso

log = get_logger("batch")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts"}


def discover(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(
                q for q in p.rglob("*")
                if q.is_file() and q.suffix.lower() in VIDEO_EXT
                and not q.name.startswith(".")
            ))
    seen: set[str] = set()
    uniq = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _worker(args: tuple[str, dict, int, bool, bool]) -> dict:
    video, cfg_dict, n_reels, force, rerender = args
    from . import config as cfg_mod

    cfg = cfg_mod.load(overrides=_strip(cfg_dict))
    try:
        return process(video, cfg, n_reels=n_reels, force=force, rerender=rerender)
    except Exception as e:  # 单剧崩了不能拖垮批次
        import traceback

        return {
            "ok": False, "video": video, "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc()[-1200:],
        }


def _strip(cfg_dict: dict) -> dict:
    """只保留可覆盖的字段，避免把 paths/workers 这类运行时字段传乱。"""
    keep = {"asr", "ocr", "vision", "recall", "planner", "render"}
    out = {k: v for k, v in cfg_dict.items() if k in keep}
    for k in out:
        if isinstance(out[k], dict):
            out[k] = {kk: vv for kk, vv in out[k].items()}
    return out


def run(
    inputs: list[str], cfg: Config, n_reels: int = 2,
    workers: int | None = None, force: bool = False, limit: int | None = None,
    rerender: bool = False,
) -> dict:
    videos = discover(inputs)
    if limit:
        videos = videos[:limit]
    if not videos:
        return {"ok": False, "error": "no videos found", "videos": 0}

    workers = workers or cfg.workers
    workers = max(1, min(workers, len(videos)))
    log.info(f"batch: {len(videos)} videos, workers={workers}, reels/video={n_reels}")

    cfg_dict = {k: v for k, v in cfg.to_dict().items()}
    tasks = [(str(v), cfg_dict, n_reels, force, rerender) for v in videos]

    t0 = time.time()
    results: list[dict] = []
    done = 0
    fail = 0

    if workers == 1:
        for t in tasks:
            r = _worker(t)
            results.append(r)
            done += 1
            fail += 0 if r.get("ok") else 1
            log.info(f"[{done}/{len(tasks)}] {Path(r.get('video','?')).name} "
                     f"{'ok' if r.get('ok') else 'FAIL'} ({r.get('reel_count', 0)} 条)")
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, t): t[0] for t in tasks}
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                done += 1
                fail += 0 if r.get("ok") else 1
                log.info(f"[{done}/{len(tasks)}] {Path(r.get('video','?')).name} "
                         f"{'ok' if r.get('ok') else 'FAIL'} ({r.get('reel_count', 0)} 条)")

    elapsed = round(time.time() - t0, 1)
    total_reels = sum(r.get("reel_count", 0) for r in results)
    summary = {
        "ok": fail < len(results),
        "finished_at": now_iso(),
        "videos": len(results),
        "succeeded": len(results) - fail,
        "failed": fail,
        "reels": total_reels,
        "elapsed_seconds": elapsed,
        "avg_seconds_per_video": round(elapsed / max(1, len(results)), 1),
        "workers": workers,
        "results": [
            {
                "video": r.get("video"),
                "ok": r.get("ok"),
                "reels": r.get("reel_count", 0),
                "strategy": r.get("strategy"),
                "error": r.get("error"),
                "outputs": [x.get("path") for x in r.get("reels", []) if x.get("ok")],
            }
            for r in results
        ],
    }

    out_dir = ensure_dir(Path(cfg.paths.out) / "batches")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"batch_{stamp}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(
        f"batch done in {elapsed}s: {summary['succeeded']}/{len(results)} ok, "
        f"{total_reels} reels -> {path}"
    )
    summary["report_path"] = str(path)
    return summary


def watch(inputs: list[str], cfg: Config, n_reels: int = 2, interval: int = 60,
          state_file: str | None = None) -> None:
    """目录监听：新剧落盘就自动处理，适合挂在常驻机器上跑。"""
    state_path = Path(state_file or (Path(cfg.paths.work) / ".watched.json"))
    ensure_dir(state_path.parent)
    seen: set[str] = set()
    if state_path.exists():
        try:
            seen = set(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()

    log.info(f"watch mode: {inputs}, interval={interval}s, known={len(seen)}")
    while True:
        fresh = [str(v) for v in discover(inputs) if str(v.resolve()) not in seen]
        if fresh:
            log.info(f"watch: {len(fresh)} new videos")
            run(fresh, cfg, n_reels=n_reels)
            seen.update(str(Path(v).resolve()) for v in fresh)
            state_path.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
        time.sleep(interval)
