#!/usr/bin/env python
"""FunASR 转写 worker（双引擎）。

必须用 ~/coding/FunASR/.venv/bin/python 运行（那边是 py3.9 + funasr 1.3.14）。
主项目用 py3.12，两个环境依赖互斥，所以走子进程隔离，顺带让 ASR 可以独立限流。

两个引擎的分工：

- sensevoice（默认）：SenseVoice-Small，230M 非自回归模型，单遍出结果。
  480 秒素材实测 68 秒，比 Paraformer large 的 106-148 秒快 2 倍多；
  代价是文本要自己按标点切句（它只吐一整段），换来的是**附带情感与音频事件**
  （<|ANGRY|> / <|HAPPY|> / <|BGM|> 等），这正是短剧找冲突点最需要的信号。

- paraformer：老路径，SeACo-Paraformer large + VAD + 标点，原生给句级时间戳。
  作为对照与兜底保留。

两条路输出同一套字段（text / timestamp），上游 split_sentences 不用改。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODEL_ROOT = Path.home() / ".cache/modelscope/hub/models"
# SenseVoice 不在 models/ 命名空间下，modelscope 直接放在 hub/iic/
HUB_ROOT = Path.home() / ".cache/modelscope/hub"

ASR_MODEL = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
SENSEVOICE_MODEL = "SenseVoiceSmall"
VAD_MODEL = "speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL = "punc_ct-transformer_cn-en-common-vocab471067-large"

# 每组标签固定是 <|语言|><|情感|><|事件|><|规整|>，后面紧跟这段的正文。
# 语言码是 zh/en/ja/ko/yue 这类 2-3 位小写；情感除 NEUTRAL/ANGRY 等之外
# 还会出现 <|EMO_UNKNOWN|>，事件同理，所以两处都必须允许下划线，
# 否则整组匹配失败、标签会原样留在正文里。
_TAG_GROUP = re.compile(
    r"<\|[a-z]{2,3}\|><\|([A-Z_]+)\|><\|([A-Za-z_]+)\|><\|[a-z]+\|>"
)


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


def resolve_sensevoice() -> str:
    """SenseVoice 的缓存位置和其它模型不一样，modelscope 把它放在 hub/iic/ 下。"""
    for p in (HUB_ROOT / "iic" / SENSEVOICE_MODEL,
              HUB_ROOT / "models" / "iic" / SENSEVOICE_MODEL,
              MODEL_ROOT / "iic" / SENSEVOICE_MODEL):
        if p.is_dir() and (p / "model.pt").exists():
            return str(p)
    return f"iic/{SENSEVOICE_MODEL}"


def default_ncpu() -> int:
    """SenseVoice 的甜点是「性能核数」，不是「总核数」。

    M5 是 4 性能核 + 6 能效核：ncpu=4 时 68 秒，ncpu=8 反而要 155 秒——
    多出来的线程被调度到能效核上，互相等，比不并发还慢。
    """
    try:
        p = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                           capture_output=True, text=True, timeout=3)
        n = int(p.stdout.strip() or 0)
        if n > 0:
            return n
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return max(2, min(4, (os.cpu_count() or 4) // 2))


def parse_sensevoice(raw: str):
    """拆开 SenseVoice 的 `<|zh|><|ANGRY|><|BGM|><|withitn|>正文` 序列。

    返回 (纯文本, 标注列表)。标注里的 at 是「这个情感从纯文本的第几个字符开始」，
    这样上游切句之后，每句都能按自己的字符区间认领对应的情感，
    而不是让整段共用开头那个标签。
    """
    chunks = _TAG_GROUP.split(raw)
    # 两个捕获组，split 后是 [前缀, 情感, 事件, 正文, 情感, 事件, 正文, ...]
    if len(chunks) < 4:                      # 没匹配到标签组，按纯文本处理
        return re.sub(r"<\|[^|]+\|>", "", raw).strip(), []

    text_parts = []
    marks = []
    k = 0
    i = 1
    while i + 2 < len(chunks):
        emotion, event, body = chunks[i], chunks[i + 1], chunks[i + 2]
        # 兜底：标点组形式可能随模型版本变化，残留的 <|...|> 一律清掉
        body = re.sub(r"<\|[^|]+\|>", "", body).strip()
        if body:
            # EMO_UNKNOWN 多是「嗯」「啊」这类语气词，对选段没有区分度，
            # 归一成 NEUTRAL，否则下游要额外处理一个没有意义的类别
            emo = "NEUTRAL" if emotion.startswith("EMO_") else emotion
            marks.append({"at": k, "emotion": emo, "event": event})
            text_parts.append(body)
            k += len(body)
        i += 3
    return "".join(text_parts), marks


def run_sensevoice(args, AutoModel, load_probe):
    model = AutoModel(
        model=resolve_sensevoice(),
        vad_model=resolve(VAD_MODEL),
        # 不配 VAD 时整片会被当成一段，只转写开头几秒（实测 26 字 vs 1457 字）
        vad_kwargs={"max_single_segment_time": 30000},
        device=args.device,
        ncpu=args.ncpu,
        disable_update=True,
        disable_pbar=True,
    )
    load_probe["load"] = time.time() - load_probe["t0"]

    t1 = time.time()
    result = model.generate(
        input=args.audio,
        cache={},
        language=args.language,
        use_itn=True,
        batch_size_s=args.batch_size_s,
        # 默认 False；打开才有字级 timestamp，关掉就只能拿到一整段文本
        output_timestamp=True,
    )
    infer_s = time.time() - t1

    first = result[0] if result else {}
    text, marks = parse_sensevoice(first.get("text", ""))
    return {
        "ok": True,
        "engine": "sensevoice",
        "text": text,
        "timestamp": first.get("timestamp", []),
        "emotions": marks,
        "load_seconds": round(load_probe["load"], 2),
        "infer_seconds": round(infer_s, 2),
        "device": args.device,
        "ncpu": args.ncpu,
        "worker_python": sys.version.split()[0],
    }


def run_paraformer(args, AutoModel, load_probe):
    model = AutoModel(
        model=resolve(ASR_MODEL),
        vad_model=resolve(VAD_MODEL),
        punc_model=resolve(PUNC_MODEL),
        device=args.device,
        ncpu=args.ncpu,
        disable_update=True,
    )
    load_probe["load"] = time.time() - load_probe["t0"]

    t1 = time.time()
    result = model.generate(
        input=args.audio,
        batch_size_s=args.batch_size_s,
        merge_vad=True,
        merge_length_s=15,
    )
    infer_s = time.time() - t1

    first = result[0] if result else {}
    return {
        "ok": True,
        "engine": "paraformer",
        "text": first.get("text", ""),
        "timestamp": first.get("timestamp", []),
        "emotions": [],
        "load_seconds": round(load_probe["load"], 2),
        "infer_seconds": round(infer_s, 2),
        "device": args.device,
        "ncpu": args.ncpu,
        "worker_python": sys.version.split()[0],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", default="sensevoice",
                    choices=["sensevoice", "paraformer"])
    ap.add_argument("--ncpu", type=int, default=0,
                    help="0=按性能核数自动决定（SenseVoice 超过性能核数会变慢）")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--batch-size-s", type=int, default=120)
    args = ap.parse_args()

    if not args.ncpu:
        args.ncpu = default_ncpu()

    from funasr import AutoModel

    load_probe = {"t0": time.time(), "load": 0.0}
    if args.engine == "sensevoice":
        payload = run_sensevoice(args, AutoModel, load_probe)
    else:
        payload = run_paraformer(args, AutoModel, load_probe)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    print(json.dumps({"ok": True, "engine": payload["engine"],
                      "infer_seconds": payload["infer_seconds"]}), flush=True)
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
