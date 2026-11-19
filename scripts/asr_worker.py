#!/usr/bin/env python
"""FunASR 转写 worker。

必须用 ~/coding/FunASR/.venv/bin/python 运行（那边是 py3.9 + funasr 1.3.14）。
主项目用 py3.12，两个环境依赖互斥，所以走子进程隔离，顺带让 ASR 可以独立限流。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

MODEL_ROOT = Path.home() / ".cache/modelscope/hub/models"

ASR_MODEL = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL = "speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL = "punc_ct-transformer_cn-en-common-vocab471067-large"


def resolve(name: str) -> str:
    """本地缓存里模型可能落在 iic/ 或 damo/ 命名空间下，两处都找一遍。

    找不到时退回 modelscope 模型 id，让 FunASR 自己下载，
    而不是把裸模型名当成本地路径传进去（那样会报 not registered）。
    """
    for ns in ("iic", "damo", ""):
        p = MODEL_ROOT / ns / name if ns else MODEL_ROOT / name
        if p.is_dir() and any(p.iterdir()):
            return str(p)
    return f"iic/{name}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ncpu", type=int, default=6)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--batch-size-s", type=int, default=120)
    args = ap.parse_args()

    from funasr import AutoModel

    t0 = time.time()
    model = AutoModel(
        model=resolve(ASR_MODEL),
        vad_model=resolve(VAD_MODEL),
        punc_model=resolve(PUNC_MODEL),
        device=args.device,
        ncpu=args.ncpu,
        disable_update=True,
    )
    load_s = time.time() - t0

    t1 = time.time()
    result = model.generate(
        input=args.audio,
        batch_size_s=args.batch_size_s,
        merge_vad=True,
        merge_length_s=15,
    )
    infer_s = time.time() - t1

    first = result[0] if result else {}
    payload = {
        "ok": True,
        "text": first.get("text", ""),
        "timestamp": first.get("timestamp", []),
        "load_seconds": round(load_s, 2),
        "infer_seconds": round(infer_s, 2),
        "device": args.device,
        "worker_python": sys.version.split()[0],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    print(json.dumps({"ok": True, "infer_seconds": payload["infer_seconds"]}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 失败也要给结构化结果，让上游能降级
        import traceback

        payload = {"ok": False, "error": f"{type(e).__name__}: {e}",
                   "trace": traceback.format_exc()[-1500:]}
        out = None
        for i, a in enumerate(sys.argv):
            if a == "--out" and i + 1 < len(sys.argv):
                out = sys.argv[i + 1]
        if out:
            Path(out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
