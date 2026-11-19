"""ASR 信号：优先走常驻服务，不可用时退回一次性子进程。

常驻服务的意义：AutoModel 加载约 73 秒，推理 4 分钟音频只要 13 秒。
每天几十部剧若各加载一次，光加载就烧掉半小时。服务只加载一次，全局复用。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import requests

from ..config import PROJECT_ROOT, ASRCfg
from ..util import get_logger, read_json

log = get_logger("asr")

_SENT_END = "。？！?!…"
_SOFT_BREAK = "，,；;：:"
PUNCT = set(_SENT_END + _SOFT_BREAK + "、\"'“”‘’（）()《》【】—· \t\n\r\u3000" + ".,")

_proc: subprocess.Popen | None = None


def _url(cfg: ASRCfg) -> str:
    return f"http://127.0.0.1:{cfg.server_port}"


def health(cfg: ASRCfg, timeout: float = 2.0) -> dict | None:
    try:
        r = requests.get(_url(cfg) + "/health", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        return None
    return None


def ensure_server(cfg: ASRCfg, log_dir: str | Path | None = None) -> bool:
    """确保常驻服务在跑。只保证端口可用，不等模型加载完（加载在后台）。"""
    global _proc
    if not cfg.use_server:
        return False
    if health(cfg):
        return True

    py = Path(cfg.python).expanduser()
    server = Path(cfg.server).expanduser()
    if not py.exists() or not server.exists():
        log.warning(f"ASR server unavailable (py={py.exists()}, script={server.exists()})")
        return False

    log_path = Path(log_dir or (PROJECT_ROOT / "logs")) / "asr-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a", encoding="utf-8")
    try:
        _proc = subprocess.Popen(
            [str(py), str(server), "--port", str(cfg.server_port),
             "--ncpu", str(cfg.ncpu), "--device", cfg.device, "--preload"],
            stdout=fh, stderr=subprocess.STDOUT, cwd=str(server.parent.parent),
        )
    except OSError as e:
        log.warning(f"ASR server spawn failed: {e}")
        return False

    deadline = time.time() + 40
    while time.time() < deadline:
        if health(cfg, timeout=1.0):
            log.info(f"ASR server up on :{cfg.server_port} (pid {_proc.pid}), model loading in background")
            return True
        if _proc.poll() is not None:
            log.warning(f"ASR server exited early, see {log_path}")
            return False
        time.sleep(0.5)
    log.warning("ASR server did not become ready in 40s")
    return False


def stop_server(cfg: ASRCfg, timeout: float = 5.0) -> None:
    global _proc
    try:
        requests.post(_url(cfg) + "/shutdown", timeout=timeout)
    except requests.RequestException:
        pass
    if _proc is not None:
        try:
            _proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _proc.terminate()
        _proc = None


def _via_server(audio: Path, out_json: Path, cfg: ASRCfg) -> dict | None:
    try:
        r = requests.post(
            _url(cfg) + "/transcribe",
            json={"audio": str(audio), "batch_size_s": cfg.batch_size_s},
            timeout=cfg.timeout,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"ASR server call failed: {type(e).__name__}: {e}")
        return None
    if data.get("ok"):
        data["source"] = "server"
        _write_json(out_json, data)
    return data


def transcribe(audio: str | Path, out_json: str | Path, cfg: ASRCfg) -> dict:
    """返回 {ok, text, timestamp, ...}；失败不抛异常，交给上游降级。"""
    audio = Path(audio)
    out_json = Path(out_json)

    if cfg.use_server:
        got = _via_server(audio, out_json, cfg)
        if got and got.get("ok"):
            log.info(f"ASR(server) ok in {got.get('infer_seconds')}s, "
                     f"{len(got.get('text', ''))} chars")
            return got

    py = Path(cfg.python).expanduser()
    worker = Path(cfg.worker).expanduser()
    if not py.exists():
        return {"ok": False, "error": f"asr python missing: {py}"}
    if not worker.exists():
        return {"ok": False, "error": f"asr worker missing: {worker}"}

    cmd = [
        str(py), str(worker), "--audio", str(audio), "--out", str(out_json),
        "--device", cfg.device, "--batch-size-s", str(cfg.batch_size_s),
        "--ncpu", str(cfg.ncpu),
    ]
    log.info(f"ASR start (subprocess): {audio.name}")
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=cfg.timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ASR timeout after {cfg.timeout}s"}

    data = read_json(out_json)
    if data and data.get("ok"):
        log.info(f"ASR ok in {data.get('infer_seconds')}s, {len(data.get('text', ''))} chars")
        return data

    err = p.stderr.decode("utf-8", "replace")[-800:]
    log.warning(f"ASR failed rc={p.returncode}: {err[-300:]}")
    return data or {"ok": False, "error": err}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def split_sentences(text: str, timestamps: list, duration: float) -> list[dict]:
    """按标点切句，并把「第 k 个正文字符」映射到时间戳。

    FunASR 的 timestamp 是字符级且不含标点，长度常与正文差 1-2 个
    （英文词被合并、数字被合并）。这里用比例映射而非严格对齐，
    位置误差最多一两个字，对切片边界完全够用。
    """
    if not text.strip():
        return []
    if not timestamps:
        return [{"text": text.strip(), "start": 0.0,
                 "end": round(duration, 3), "chars": len(text)}]

    n_ts = len(timestamps)
    n_content = sum(1 for c in text if c not in PUNCT)

    def ts_at(k: int) -> list:
        if n_content <= 0:
            return timestamps[0]
        if n_ts == n_content:
            i = k
        else:
            i = int(round(k * (n_ts - 1) / max(1, n_content - 1)))
        return timestamps[max(0, min(n_ts - 1, i))]

    sentences: list[dict] = []
    buf: list[str] = []
    k0 = 0
    k = 0
    for ch in text:
        buf.append(ch)
        is_punct = ch in PUNCT
        if not is_punct:
            k += 1
        hard = ch in _SENT_END
        soft = ch in _SOFT_BREAK and (k - k0) >= 14
        if hard or soft:
            body = "".join(buf).strip()
            if sum(1 for c in body if c not in PUNCT) >= 2:
                start_ms = ts_at(k0)[0]
                end_ms = ts_at(max(k0, k - 1))[1]
                sentences.append({
                    "text": body,
                    "start": round(start_ms / 1000.0, 3),
                    "end": round(max(end_ms / 1000.0, start_ms / 1000.0 + 0.25), 3),
                    "chars": sum(1 for c in body if c not in PUNCT),
                })
            buf = []
            k0 = k

    tail = "".join(buf).strip()
    if sum(1 for c in tail if c not in PUNCT) >= 2:
        start_ms = ts_at(k0)[0]
        end_ms = ts_at(max(k0, len(timestamps) - 1))[1]
        sentences.append({
            "text": tail,
            "start": round(start_ms / 1000.0, 3),
            "end": round(max(end_ms / 1000.0, start_ms / 1000.0 + 0.25), 3),
            "chars": sum(1 for c in tail if c not in PUNCT),
        })

    merged: list[dict] = []
    for s in sentences:
        s["start"] = max(0.0, min(s["start"], duration))
        s["end"] = max(s["start"] + 0.2, min(s["end"], duration))
        if merged and s["start"] - merged[-1]["end"] < 0.35 and s["chars"] < 6:
            prev = merged[-1]
            prev["text"] += s["text"]
            prev["chars"] += s["chars"]
            prev["end"] = max(prev["end"], s["end"])
        else:
            merged.append(s)
    return merged


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()
