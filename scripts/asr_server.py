#!/usr/bin/env python
"""FunASR 常驻转写服务。

为什么需要它：AutoModel 单次加载约 73 秒，而真正推理 4 分钟音频只要 13 秒。
每天几十部剧如果每部都重新加载，光加载就烧掉半小时以上。所以模型只加载
一次，之后常驻，batch 期间所有剧共用。

必须用 ~/coding/FunASR/.venv/bin/python 启动（py3.9 + funasr 1.3.14）。
用标准库 http.server，不引入额外依赖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asr_worker import (  # noqa: E402  复用 worker 的路径解析与标签解析
    PUNC_MODEL,
    VAD_MODEL,
    default_ncpu,
    parse_sensevoice,
    resolve,
    resolve_sensevoice,
    ASR_MODEL,
)


class Engine:
    """模型单例。ASR 本身是 CPU 密集型，串行化反而比多线程抢核更稳。"""

    def __init__(self, ncpu: int, device: str, engine: str = "sensevoice"):
        self.ncpu = ncpu or default_ncpu()
        self.device = device
        self.engine = engine
        self.model: Optional[Any] = None
        self.lock = threading.Lock()        # 推理串行，避免抢核
        self.load_lock = threading.Lock()   # 加载只做一次，其余线程在这等
        self.loading = False
        self.error: Optional[str] = None
        self.load_seconds: Optional[float] = None

    def load(self) -> None:
        if self.model is not None:
            return
        with self.load_lock:
            if self.model is not None:
                return
            self.loading = True
            t0 = time.time()
            try:
                from funasr import AutoModel

                if self.engine == "sensevoice":
                    self.model = AutoModel(
                        model=resolve_sensevoice(),
                        vad_model=resolve(VAD_MODEL),
                        vad_kwargs={"max_single_segment_time": 30000},
                        device=self.device,
                        ncpu=self.ncpu,
                        disable_update=True,
                        disable_pbar=True,
                    )
                else:
                    self.model = AutoModel(
                        model=resolve(ASR_MODEL),
                        vad_model=resolve(VAD_MODEL),
                        punc_model=resolve(PUNC_MODEL),
                        device=self.device,
                        ncpu=self.ncpu,
                        disable_update=True,
                    )
                self.load_seconds = round(time.time() - t0, 1)
                print("[asr-server] %s loaded in %.1fs (ncpu=%d)"
                      % (self.engine, self.load_seconds, self.ncpu), flush=True)
            except Exception as e:  # 加载失败要让客户端看到原因
                self.error = "%s: %s" % (type(e).__name__, e)
                print("[asr-server] load failed: %s" % self.error, file=sys.stderr, flush=True)
            finally:
                self.loading = False

    def transcribe(self, audio: str, batch_size_s: int) -> dict:
        if self.model is None:
            self.load()
        if self.model is None:
            return {"ok": False, "error": self.error or "model unavailable"}
        t0 = time.time()
        with self.lock:
            if self.engine == "sensevoice":
                result = self.model.generate(
                    input=audio, cache={}, language="zh", use_itn=True,
                    batch_size_s=batch_size_s, output_timestamp=True,
                )
            else:
                result = self.model.generate(
                    input=audio, batch_size_s=batch_size_s,
                    merge_vad=True, merge_length_s=15,
                )
        first = result[0] if result else {}
        if self.engine == "sensevoice":
            text, marks = parse_sensevoice(first.get("text", ""))
        else:
            text, marks = first.get("text", ""), []
        return {
            "ok": True,
            "engine": self.engine,
            "text": text,
            "timestamp": first.get("timestamp", []),
            "emotions": marks,
            "ncpu": self.ncpu,
            "infer_seconds": round(time.time() - t0, 2),
        }


class Handler(BaseHTTPRequestHandler):
    engine: Engine = None  # type: ignore

    def log_message(self, fmt, *args):  # 静默访问日志，避免污染 stdout
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            e = self.engine
            self._send(200, {
                "ok": True,
                "loaded": e.model is not None,
                "loading": e.loading,
                "error": e.error,
                "load_seconds": e.load_seconds,
                # 客户端要能识别「常驻服务的引擎和当前配置不一致」，
                # 否则切了 engine 之后仍然打到旧模型上，静默跑错
                "engine": e.engine,
                "ncpu": e.ncpu,
            })
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != "/transcribe":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            audio = req.get("audio")
            if not audio or not os.path.exists(audio):
                self._send(400, {"ok": False, "error": "audio not found: %s" % audio})
                return
            out = self.engine.transcribe(audio, int(req.get("batch_size_s", 120)))
            self._send(200, out)
        except Exception as e:
            import traceback

            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                             "trace": traceback.format_exc()[-800:]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--ncpu", type=int, default=0, help="0=按性能核数自动决定")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--engine", default="sensevoice",
                    choices=["sensevoice", "paraformer"])
    ap.add_argument("--preload", action="store_true", help="启动即加载模型")
    args = ap.parse_args()

    engine = Engine(args.ncpu, args.device, args.engine)
    Handler.engine = engine
    if args.preload:
        threading.Thread(target=engine.load, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("[asr-server] listening on 127.0.0.1:%d (engine=%s, ncpu=%d)"
          % (args.port, args.engine, engine.ncpu), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
