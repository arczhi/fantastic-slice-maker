"""资源调度：批量跑切片时不让机器被拖死。

跑批为什么需要这一层：单部剧内部本身就会并发（三路信号 + 每段裁切 3 线程
ffmpeg），再乘上外层 workers，很容易出现 3 worker × 3 线程 = 9 个 x264 同时
抢 10 核，机器风扇拉满、前台打字卡顿，ASR 还会被饿到超时。所以并发数不能
拍脑袋写死，必须同时看 CPU 和可用内存，并在派活前做实时检查。

三条规则：
1. 并发上限由「核数 / 内存」共同推算，取较小值，再被用户配置压一层。
2. 只在真的需要补一个 worker 时检查负载，超阈值就等，而不是一次把队列灌满。
   等待有上限（默认 180s），避免阈值永远达不成的死等。
3. 子进程统一降 nice，让前台交互优先。

实现只用标准库：不引入 psutil，macOS 走 vm_stat、Linux 走 /proc/meminfo。
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import subprocess
import time

from .util import get_logger

log = get_logger("resource")

_PAGE_RE = re.compile(r"page size of (\d+) bytes")


def cpu_count() -> int:
    return os.cpu_count() or 4


def perf_cores() -> int:
    """性能核数（Apple Silicon 的 perflevel0）。

    ASR 的线程数要按这个值给：M5 是 4P+6E，ncpu=4 时 68 秒，
    开到 8 反而 155 秒——多出来的线程被调度到能效核上互相等。
    拿不到（Intel Mac / Linux）时退回总核数的一半，最多 4。
    """
    try:
        p = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                           capture_output=True, text=True, timeout=3)
        n = int(p.stdout.strip() or 0)
        if n > 0:
            return n
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return max(2, min(4, cpu_count() // 2))


def load1() -> float:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _free_mem_gb_macos() -> float:
    """free + inactive + speculative 才算「现在就能拿来用」。

    只取 free 会严重低估：macOS 会把大量内存当作 inactive 缓存，随时可回收。
    """
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    m = _PAGE_RE.search(out)
    page = int(m.group(1)) if m else 4096

    def pages(label: str) -> int:
        mm = re.search(rf"{label}:\s+([\d,]+)", out)
        return int(mm.group(1).replace(",", "")) if mm else 0

    free = pages(r"Pages free") + pages(r"Pages inactive") + pages(r"Pages speculative")
    return free * page / (1 << 30)


def _free_mem_gb_linux() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1 << 20)
    except OSError:
        pass
    return 0.0


def free_mem_gb() -> float:
    if platform.system() == "Darwin":
        return _free_mem_gb_macos()
    return _free_mem_gb_linux()


def snapshot() -> dict:
    cores = cpu_count()
    return {
        "cpu_cores": cores,
        "load1": round(load1(), 2),
        "load_per_core": round(load1() / cores, 2),
        "free_mem_gb": round(free_mem_gb(), 1),
    }


def safe_workers(cfg, n_tasks: int) -> int:
    """推算这次批处理最多开几个 worker。

    每部剧内部并发按 cpu_per_worker 核 + mem_per_worker GB 估算（默认 3 核 / 2.5GB，
    实测 700s 素材峰值内存约 2GB：numpy 帧差 + OCR 模型 + ASR 客户端）。
    内存不足时宁可降到 1，也不要靠 swap 硬撑——一 swap，整批速度会掉一个量级。
    """
    n_tasks = max(1, n_tasks)
    cores = cpu_count()
    by_cpu = max(1, cores // max(1, cfg.cpu_per_worker))
    free = free_mem_gb()
    if free > 0:
        by_mem = max(1, int(free // max(0.5, cfg.mem_per_worker_gb)))
    else:
        by_mem = by_cpu
    want = cfg.max_workers if cfg.max_workers > 0 else (cores // max(1, cfg.cpu_per_worker))
    workers = max(1, min(int(want), by_cpu, by_mem, n_tasks))
    log.info(
        f"workers={workers} (tasks={n_tasks}, cores={cores}, "
        f"free_mem={free:.1f}GB, by_cpu={by_cpu}, by_mem={by_mem})"
    )
    return workers


class Gate:
    """派活闸门：负载/内存越线时先把下一个任务压在手里。

    只在新任务即将提交时检查，正在跑的任务不受影响。等待有硬上限，
    超时后放行并留一条 warning——批量任务卡死在闸门上比慢一点更糟。
    """

    def __init__(self, cfg, max_wait: float = 180.0, poll: float = 5.0):
        self.cfg = cfg
        self.max_wait = max_wait
        self.poll = poll
        self.waited = 0.0

    def _too_busy(self, snap: dict) -> str:
        if snap["load_per_core"] > self.cfg.max_load_per_core:
            return f"load/core {snap['load_per_core']:.2f} > {self.cfg.max_load_per_core}"
        if 0 < snap["free_mem_gb"] < self.cfg.min_free_mem_gb:
            return f"free mem {snap['free_mem_gb']:.1f}GB < {self.cfg.min_free_mem_gb}GB"
        return ""

    def wait(self) -> None:
        t0 = time.time()
        announced = False
        while True:
            reason = self._too_busy(snapshot())
            if not reason:
                return
            if time.time() - t0 >= self.max_wait:
                log.warning(f"资源闸门等待 {self.max_wait:.0f}s 仍未缓解（{reason}），放行")
                return
            if not announced:
                log.warning(f"资源紧张（{reason}），暂停派活，等现有任务收尾")
                announced = True
            time.sleep(self.poll)
            self.waited += self.poll


def apply_nice(level: int) -> None:
    """子进程降优先级。macOS/Linux 都支持 os.nice，权限不足时静默跳过。"""
    if level <= 0:
        return
    try:
        os.nice(level)
    except OSError:
        pass
