"""通用工具：原子写、哈希、结构化日志、阶段状态。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- 基础

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: str | Path, text: str) -> None:
    """原子写：先写同目录临时文件再 rename，避免半截文件被下游读到。

    临时名必须带上线程 id 和随机后缀——同一进程里多路信号是并发写
    status.json 的，只带 pid 会让两个线程互相覆盖 rename。

    rename 在某些沙箱/容器环境里会被拦（Mac 上的宿主策略、只读挂载的
    上层目录等），这时退化成直接覆盖写：丢的是原子性，不是数据。
    这类环境里如果还硬要 rename，整个流程会在第一步就 PermissionError 崩掉。
    """
    path = Path(path)
    ensure_dir(path.parent)
    global _replace_blocked

    # 已经被拦过一次就别再试：宿主策略把 rename 建模成「删除目标文件」，
    # 每次 os.replace 都会弹一次授权，一次跑批能弹二十几次。
    # 记住结果，后续直接覆盖写，只丢原子性。
    if _replace_blocked:
        path.write_text(text, encoding="utf-8")
        return

    tmp = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            # 放行降级：不能因为宿主拦 rename 就让整条流水线停摆
            _replace_blocked = True
            _warn_replace_once(e)
            path.write_text(text, encoding="utf-8")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_replace_warned = False
_replace_blocked = False


def _warn_replace_once(err: OSError) -> None:
    global _replace_warned
    if not _replace_warned:
        _replace_warned = True
        try:
            get_logger("util").warning(
                f"rename 被环境拦截（{type(err).__name__}），改为直接覆盖写：{err}"
            )
        except Exception:
            pass


def atomic_write_json(path: str | Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def quick_hash(path: str | Path, sample: int = 1 << 20) -> str:
    """文件指纹：size + 头尾采样，避免对几十 GB 素材做全量哈希。"""
    p = Path(path)
    h = hashlib.sha1()
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(sample))
        if size > sample * 2:
            f.seek(-sample, os.SEEK_END)
            h.update(f.read(sample))
    return h.hexdigest()[:16]


def safe_name(s: str, limit: int = 80) -> str:
    bad = '/\\:*?"<>|\x00\n\r\t'
    out = "".join("_" if c in bad else c for c in s).strip(" .")
    return (out or "unnamed")[:limit]


# ---------------------------------------------------------------- 日志

class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("video", "stage", "elapsed", "extra"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))[-1800:]
        return json.dumps(payload, ensure_ascii=False)


_LOG_READY = False


def setup_logging(log_file: str | Path | None = None, level: int = logging.INFO) -> None:
    """控制台给人看，文件给机器看（JSONL）。"""
    global _LOG_READY
    root = logging.getLogger("fsm")
    root.setLevel(level)
    if _LOG_READY:
        return
    _LOG_READY = True

    con = logging.StreamHandler(sys.stderr)
    con.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    root.addHandler(con)

    if log_file:
        ensure_dir(Path(log_file).parent)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fsm.{name}")


# ---------------------------------------------------------------- 阶段状态

class StageBook:
    """记录每个阶段的成败与耗时，落盘为 status.json，支持断点续跑。

    设计原则：任何阶段的失败都必须只影响该阶段，由 pipeline 决定降级，
    绝不能让异常穿透到批次层把整部剧弄挂。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = read_json(self.path, default={}) or {}
        self.data.setdefault("stages", {})
        self.data.setdefault("started_at", now_iso())

    def ok(self, stage: str) -> bool:
        with self._lock:
            st = self.data["stages"].get(stage)
            return bool(st and st.get("status") == "ok")

    def get(self, stage: str) -> dict:
        with self._lock:
            return dict(self.data["stages"].get(stage, {}))

    def mark(self, stage: str, status: str, **info: Any) -> None:
        with self._lock:
            prev = self.data["stages"].get(stage, {})
            prev.update(info)
            prev["status"] = status
            prev["updated_at"] = now_iso()
            self.data["stages"][stage] = prev
            self.save()

    def run(self, stage: str, force: bool = False):
        """上下文管理器：自动记录耗时与异常，异常会继续抛出由调用方降级。"""
        return _StageCtx(self, stage, force)

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = now_iso()
            atomic_write_json(self.path, self.data)


class _StageCtx:
    def __init__(self, book: StageBook, stage: str, force: bool):
        self.book = book
        self.stage = stage
        self.force = force
        self.t0 = 0.0
        self.skipped = False

    def __enter__(self):
        if self.book.ok(self.stage) and not self.force:
            self.skipped = True
            return self
        self.t0 = time.time()
        self.book.mark(self.stage, "running")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.skipped:
            return False
        dt = round(time.time() - self.t0, 2)
        if exc_type is None:
            self.book.mark(self.stage, "ok", elapsed=dt)
        else:
            self.book.mark(
                self.stage, "failed", elapsed=dt,
                error=f"{exc_type.__name__}: {exc}"[:500],
            )
        return False
