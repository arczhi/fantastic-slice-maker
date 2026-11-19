"""命令行入口。默认全自动：一条命令跑完整链路，不需要任何交互。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import batch, config as cfg_mod
from .pipeline import process
from .util import ensure_dir, get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fsm", description="fantastic-slice-maker: 漫剧高光切片全自动流水线",
    )
    p.add_argument("--config", default=None, help="config.yaml 路径")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--reels", type=int, default=2, help="每部剧产出几条切片")
        sp.add_argument("--force", action="store_true", help="忽略中间产物，全部重跑")
        sp.add_argument("--rerender", action="store_true", help="只重跑渲染与质检")
        sp.add_argument("--no-asr", action="store_true")
        sp.add_argument("--no-ocr", action="store_true")
        sp.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek，用规则兜底")
        sp.add_argument("--no-bgm", action="store_true", help="不混入背景音乐")
        sp.add_argument(
            "--subtitle", choices=["auto", "on", "off"], default="auto",
            help="自动补字幕：auto=源片无字幕时才补（默认），on=强制补，off=不补",
        )

    sp = sub.add_parser("run", help="处理单个视频")
    sp.add_argument("video")
    add_common(sp)

    sp = sub.add_parser("batch", help="批量处理目录或文件列表")
    sp.add_argument("inputs", nargs="+")
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--limit", type=int, default=None)
    add_common(sp)

    sp = sub.add_parser("watch", help="监听目录，新片自动处理")
    sp.add_argument("inputs", nargs="+")
    sp.add_argument("--interval", type=int, default=60)
    add_common(sp)

    sub.add_parser("doctor", help="环境自检")
    sub.add_parser("bgm", help="构建/更新本地 BGM 曲库")
    return p


def _cfg(args) -> cfg_mod.Config:
    overrides: dict = {}
    if args.no_asr:
        overrides.setdefault("asr", {})["enabled"] = False
    if args.no_ocr:
        overrides.setdefault("ocr", {})["enabled"] = False
    if args.no_llm:
        overrides.setdefault("planner", {})["enabled"] = False
    if args.no_bgm:
        overrides.setdefault("bgm", {})["enabled"] = False
    return cfg_mod.load(getattr(args, "config", None), overrides)


def _setup(args) -> None:
    cfg = cfg_mod.load(getattr(args, "config", None))
    log_dir = Path(cfg.paths.log)
    ensure_dir(log_dir)
    setup_logging(log_dir / "fsm.jsonl", level=20 if args.verbose else 20)


def cmd_run(args) -> int:
    cfg = _cfg(args)
    _ensure_asr(cfg, wait=False)
    try:
        rep = process(args.video, cfg, n_reels=args.reels, force=args.force,
                      rerender=args.rerender, sub_mode=args.subtitle)
    finally:
        _stop_asr(cfg)
    print(json.dumps({
        "ok": rep.get("ok"),
        "video": rep.get("video"),
        "error": rep.get("error"),
        "strategy": rep.get("strategy"),
        "reels": [
            {"title": r.get("title"), "path": r.get("path"),
             "duration": r.get("duration"), "qc": (r.get("qc") or {}).get("ok")}
            for r in rep.get("reels", []) if r.get("ok")
        ],
    }, ensure_ascii=False, indent=1))
    return 0 if rep.get("ok") else 1


def cmd_batch(args) -> int:
    cfg = _cfg(args)
    # 批次前先把 ASR 服务拉起来预热，30 部剧共用一次模型加载
    _ensure_asr(cfg, wait=True)
    try:
        summary = batch.run(
            args.inputs, cfg, n_reels=args.reels,
            workers=args.workers, force=args.force, limit=args.limit,
            rerender=args.rerender, sub_mode=args.subtitle,
        )
    finally:
        _stop_asr(cfg)
    brief = {k: v for k, v in summary.items() if k != "results"}
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    for r in summary.get("results", []):
        if not r.get("ok"):
            print(f"  FAILED {Path(r.get('video') or '?').name}: {r.get('error')}", file=sys.stderr)
    return 0 if summary.get("ok") else 1


def _ensure_asr(cfg, wait: bool) -> None:
    from .signals import asr

    if not cfg.asr.enabled:
        return
    ok = asr.ensure_server(cfg.asr, cfg.paths.log)
    if not ok:
        return
    if wait:
        # 等模型真正加载完再派活，避免第一部剧承担加载时间又被超时打断
        import time

        t0 = time.time()
        while time.time() - t0 < 300:
            h = asr.health(cfg.asr)
            if h and h.get("loaded"):
                print(f"[asr] model ready in {h.get('load_seconds')}s", file=sys.stderr)
                return
            if h and h.get("error"):
                print(f"[asr] load failed: {h['error']}", file=sys.stderr)
                return
            time.sleep(1.0)


def _stop_asr(cfg) -> None:
    from .signals import asr

    if cfg.asr.enabled:
        asr.stop_server(cfg.asr)


def cmd_watch(args) -> int:
    cfg = _cfg(args)
    try:
        batch.watch(args.inputs, cfg, n_reels=args.reels, interval=args.interval)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_doctor(args) -> int:
    cfg = cfg_mod.load(getattr(args, "config", None))
    rows: list[tuple[str, bool, str]] = []

    rows.append(("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "missing"))
    rows.append(("ffprobe", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "missing"))

    py = Path(cfg.asr.python).expanduser()
    asr_ok = False
    asr_note = f"missing: {py}"
    if py.exists():
        import subprocess
        try:
            r = subprocess.run([str(py), "-c", "import funasr,sys;print(funasr.__version__, sys.version.split()[0])"],
                               capture_output=True, timeout=90)
            asr_ok = r.returncode == 0
            asr_note = r.stdout.decode().strip() or r.stderr.decode()[-120:]
        except Exception as e:
            asr_note = str(e)[:120]
    rows.append(("FunASR env", asr_ok, asr_note))

    worker = Path(cfg.asr.worker).expanduser()
    rows.append(("ASR worker", worker.exists(), str(worker)))

    for label, path in (("PP-OCR det", cfg.ocr.det_model), ("PP-OCR rec", cfg.ocr.rec_model)):
        p = Path(path).expanduser()
        rows.append((label, p.exists(), f"{p.name} {p.stat().st_size // 1024 // 1024}MB" if p.exists() else str(p)))

    key = cfg.planner.api_key or os.environ.get(cfg.planner.api_key_env, "")
    rows.append(("DeepSeek key", bool(key), (key[:8] + "..." + key[-4:]) if len(key) > 14 else ("set" if key else "missing")))

    # 补字幕要能渲染中文，字体缺失时字幕会整条静默跳过
    if cfg.subtitle.mode != "off":
        from .subtitle import _font_path

        fp = _font_path(cfg.subtitle.font)
        note = Path(fp).name if fp else "找不到中文字体，请设 subtitle.font"
        rows.append(("字幕字体", bool(fp), note))

    index = Path(cfg.bgm.index)
    if index.exists():
        try:
            tracks = json.loads(index.read_text(encoding="utf-8")).get("tracks") or []
            rows.append(("BGM library", bool(tracks),
                         f"{len(tracks)} 首 / {sum(t.get('duration', 0) for t in tracks)/60:.0f} 分钟"))
        except (OSError, ValueError):
            rows.append(("BGM library", False, f"索引损坏: {index}"))
    else:
        rows.append(("BGM library", False, "未构建，跑 scripts/build_bgm_lib.py"))

    rows.append(("work dir", True, cfg.paths.work))
    rows.append(("out dir", True, cfg.paths.out))

    width = max(len(r[0]) for r in rows) + 2
    print("\n环境自检\n" + "-" * 52)
    for name, ok, note in rows:
        print(f"{'✓' if ok else '✗'} {name:<{width}} {note}")
    critical = [r for r in rows if r[0] in ("ffmpeg", "ffprobe")]
    all_ok = all(ok for _, ok, _ in critical)
    print("-" * 52)
    if not asr_ok and not cfg.ocr.enabled:
        print("注意：ASR 与 OCR 都不可用，只能靠画面信号选片，质量会明显下降")
    if not key:
        print(f"注意：未设置 {cfg.planner.api_key_env}，将使用规则兜底出片")
    return 0 if all_ok else 1


def cmd_bgm(args) -> int:
    """构建曲库：下载 → 分析 BPM/beat → 写索引。已有分析结果会复用。"""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "build_bgm_lib.py"
    if not script.exists():
        print(f"找不到 {script}", file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, str(script)])


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup(args)
    handler = {
        "run": cmd_run, "batch": cmd_batch,
        "watch": cmd_watch, "doctor": cmd_doctor, "bgm": cmd_bgm,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
