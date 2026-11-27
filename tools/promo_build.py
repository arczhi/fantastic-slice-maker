"""星火计划推广片：读取任务卡 → 用授权素材出 30-90s 竖屏原创二创 → 打包发布信息。

发布环节仍在快手 App 的任务详情页里点「发布作品去赚钱」，本脚本只负责到「可上传的成片+文案」。
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from fsm import recap
from fsm.config import load

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "config/promo/tasks"


def load_task(task_id: str) -> dict:
    p = TASKS / f"{task_id}.json"
    if not p.exists():
        raise SystemExit(f"没有任务卡：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--material", required=True, help="任务素材视频（已获授权的原片）")
    ap.add_argument("--start", type=float, default=5)
    ap.add_argument("--end", type=float, default=65)
    ap.add_argument("--title", default="")
    ap.add_argument("--work", default="", help="已跑过 fsm run 的 work 目录；留空会自动跑")
    args = ap.parse_args()

    task = load_task(args.task)
    src = Path(args.material).expanduser()
    if not src.exists():
        raise SystemExit(f"素材不存在：{src}")
    out_dir = ROOT / task["deliverable_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{task['id']}-{int(args.end - args.start)}s.mp4"

    work = Path(args.work).expanduser() if args.work else None
    if work is None or not (work / "timeline.json").exists():
        print("没有时间轴，先跑 fsm run 建时间轴…")
        subprocess.run([str(ROOT / ".venv/bin/fsm"), "run", str(src), "--reels", "1"],
                       check=True, cwd=str(ROOT))
        cands = sorted(ROOT.glob("work/*"), key=lambda p: p.stat().st_mtime)
        work = cands[-1]
    cfg = load()
    title = args.title or task["name"]
    # 复用 recap 的竖屏分段文字能力；无配音，原声+BGM，避免和素材人声打架
    from fsm import tts, overlay  # noqa: F401
    res = recap.run(str(src), work, out, title, args.start, args.end,
                    vertical=True, segmented=True, voiceover=False)

    meta = {
        "task": task["id"], "task_name": task["name"], "platform": "kuaishou",
        "plan": "星火计划", "video": str(out.resolve()),
        "title": title,
        "desc": f"《{task['name']}》原创解说二创。{task['notes']}",
        "tags": ["星火计划", "短剧推广", task["name"]],
        "entry": task["publish_entry"],
        "rules": task["requirements"],
        "checklist": [
            "从任务详情页「发布作品去赚钱」进入，不要从相册直接发",
            "标题/简介与任务主题相关，不夸大、不诱导点击",
            "成片为原创剪辑，非纯搬运/拼接",
            "发布后回任务详情页确认「已参与」",
        ],
    }
    (out_dir / "publish.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"video": str(out), "meta": str(out_dir / "publish.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
