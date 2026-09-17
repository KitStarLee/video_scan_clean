# -*- coding: utf-8 -*-
"""设备能力探测 + 并发调控。

目标：批量处理视频时**跑满但不跑炸**。做法分三层：

1. **静态预算**：开机探测物理核数、内存、是否电池供电，算出理论上限。
2. **准入控制**：每个新任务开始前检查 1 分钟负载和可用内存，压力大就先等一等。
3. **进程降级**：ffmpeg 统一走 ``taskpolicy -b`` / ``nice``，并用 ``-threads`` 限制
   单进程线程数，避免 N 个 ffmpeg 各开 8 线程把 CPU 抢成一锅粥。

另外还有**自适应降档**：连续观测到高压就把上限临时调低，平稳一段时间再调回去。
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["DeviceProfile", "probe_device", "Governor", "Pressure"]


# ---------------------------------------------------------------- 设备探测
def _sysctl(name: str) -> Optional[int]:
    try:
        out = subprocess.run(["sysctl", "-n", name], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=3)
        if out.returncode == 0:
            return int(out.stdout.decode().strip())
    except Exception:
        pass
    return None


def _windows_memory_mb() -> Optional[Tuple[int, int]]:
    """Windows 物理内存探测：kernel32!GlobalMemoryStatusEx。

    返回 ``(总内存 MB, 可用内存 MB)``；非 Windows、ctypes 不可用或调用失败
    一律返回 ``None``（绝不抛异常）。
    """
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        func = ctypes.windll.kernel32.GlobalMemoryStatusEx
        func.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        func.restype = ctypes.c_int
        if not func(ctypes.byref(st)):
            return None
        mb = 1024 * 1024
        return int(st.ullTotalPhys // mb), int(st.ullAvailPhys // mb)
    except Exception:
        return None


def total_memory_mb() -> int:
    try:
        if platform.system() == "Darwin":
            n = _sysctl("hw.memsize")
        elif os.path.exists("/proc/meminfo"):
            n = None
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        n = int(line.split()[1]) * 1024
                        break
        else:
            n = None
        if n:
            return int(n // (1024 * 1024))
    except Exception:
        pass
    # Windows 没有 /proc/meminfo 也没有 ``os.sysconf``，走 kernel32。
    mem = _windows_memory_mb()
    if mem:
        return mem[0]
    try:
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return 4096
        pages = sysconf("SC_PHYS_PAGES")
        psize = sysconf("SC_PAGE_SIZE")
        return int(pages * psize // (1024 * 1024))
    except Exception:
        return 4096


def available_memory_mb() -> int:
    """可用内存（把 inactive/purgeable 也算进来，更贴近 macOS 的真实余量）。"""
    try:
        import psutil  # type: ignore
        return int(psutil.virtual_memory().available // (1024 * 1024))
    except Exception:
        pass
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["vm_stat"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=3)
            text = out.stdout.decode("utf-8", "replace")
            page = 4096
            for line in text.splitlines():
                if "page size of" in line:
                    page = int("".join(c for c in line if c.isdigit()) or 4096)
                    break
            free_pages = 0
            for line in text.splitlines():
                key = line.split(":")[0].strip().lower()
                if key in ("pages free", "pages inactive", "pages purgeable",
                           "pages speculative"):
                    digits = "".join(c for c in line.split(":")[1] if c.isdigit())
                    free_pages += int(digits or 0)
            return int(free_pages * page // (1024 * 1024))
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
    except Exception:
        pass
    # Windows（或上面全部失败）：kernel32 兜底。
    mem = _windows_memory_mb()
    if mem:
        return mem[1]
    return total_memory_mb() // 2


def on_battery() -> bool:
    try:
        if platform.system() == "Darwin" and shutil.which("pmset"):
            out = subprocess.run(["pmset", "-g", "batt"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=3)
            return b"Battery Power" in out.stdout
        if platform.system() == "Windows":
            # GetSystemPowerStatus：ACLineStatus == 0 表示在用电池（255=未知）。
            import ctypes

            class _SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                            ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte),
                            ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_uint32),
                            ("BatteryFullLifeTime", ctypes.c_uint32)]

            status = _SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return status.ACLineStatus == 0
            return False
        if os.path.exists("/sys/class/power_supply"):
            for name in os.listdir("/sys/class/power_supply"):
                p = f"/sys/class/power_supply/{name}/type"
                if os.path.exists(p) and open(p).read().strip() == "Battery":
                    st = f"/sys/class/power_supply/{name}/status"
                    if os.path.exists(st) and open(st).read().strip() != "Charging":
                        return True
    except Exception:
        pass
    return False


@dataclass
class DeviceProfile:
    logical_cpus: int = 4
    physical_cpus: int = 4
    total_mem_mb: int = 4096
    battery: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"logical_cpus": self.logical_cpus, "physical_cpus": self.physical_cpus,
                "total_mem_mb": self.total_mem_mb, "battery": self.battery}


def probe_device() -> DeviceProfile:
    logical = os.cpu_count() or 4
    physical = logical
    if platform.system() == "Darwin":
        physical = _sysctl("hw.physicalcpu") or logical
    elif os.path.exists("/proc/cpuinfo"):
        try:
            ids = set()
            cur = {}
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if not line.strip():
                        if cur.get("physical id") is not None and cur.get("core id") is not None:
                            ids.add((cur["physical id"], cur["core id"]))
                        cur = {}
                        continue
                    if ":" in line:
                        k, v = line.split(":", 1)
                        cur[k.strip()] = v.strip()
            if ids:
                physical = len(ids)
        except Exception:
            pass
    elif platform.system() == "Windows":
        # Windows 没有 /proc/cpuinfo 也没有 sysctl；优先用 psutil 拿物理核，
        # 拿不到就退化成逻辑核数（>=1），绝不抛异常。
        try:
            import psutil  # type: ignore
            physical = int(psutil.cpu_count(logical=False) or logical)
        except Exception:
            physical = logical
    return DeviceProfile(logical_cpus=logical, physical_cpus=max(1, int(physical or logical)),
                         total_mem_mb=total_memory_mb(), battery=on_battery())


# ---------------------------------------------------------------- 压力采样
@dataclass
class Pressure:
    busy_pct: float = -1.0      # -1 表示采样失败
    load1: float = 0.0
    avail_mb: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def cpu_busy_percent() -> float:
    """真实 CPU 占用率（0-100），失败返回 -1。

    **不用 load average**：它在 macOS 上是 1 分钟衰减均值，把线程和不可中断睡眠
    都算进去，实测会出现「CPU 空闲 65% 但 load average 19.86」的情况，
    拿它当节流依据会让任务白白干等好几分钟。
    """
    try:
        if platform.system() == "Darwin" and shutil.which("iostat"):
            out = subprocess.run(["iostat", "-c", "2"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5)
            lines = [l for l in out.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
            if not lines:
                return -1.0
            nums = lines[-1].split()
            if len(nums) >= 6:
                us, sy, idle = float(nums[-6]), float(nums[-5]), float(nums[-4])
                return max(0.0, min(100.0, us + sy))
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as fh:
                parts = fh.readline().split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return 100.0 * (1.0 - idle / max(1, total))
    except Exception:
        pass
    return -1.0


def sample_pressure(cache: Optional[Dict[str, Any]] = None, ttl: float = 4.0) -> Pressure:
    p = Pressure()
    try:
        la = os.getloadavg()
        p.load1, p.load5 = la[0], la[1]
    except Exception:
        p.load1 = 0.0
    p.avail_mb = available_memory_mb()
    now = time.time()
    if cache is not None and cache.get("t", 0) + ttl > now:
        p.busy_pct = cache.get("busy", -1.0)
    else:
        p.busy_pct = cpu_busy_percent()
        if cache is not None:
            cache["t"] = now
            cache["busy"] = p.busy_pct
    return p


# ---------------------------------------------------------------- 调控器
class Governor:
    """并发闸门：既限制同时在跑的视频数，也在系统吃紧时主动踩刹车。"""

    def __init__(self, *, max_jobs: Optional[int] = None, mem_reserve_mb: int = 2048,
                 per_job_mem_mb: int = 700, cpu_fraction: float = 0.75,
                 busy_limit_pct: float = 85.0, min_free_mb: int = 0,
                 max_wait_s: float = 60.0,
                 probe: Optional[DeviceProfile] = None, log=None):
        self.device = probe or probe_device()
        self.mem_reserve_mb = mem_reserve_mb
        self.per_job_mem_mb = per_job_mem_mb
        self.busy_limit_pct = busy_limit_pct
        self.max_wait_s = max_wait_s
        self._pcache: Dict[str, Any] = {}
        # 自适应内存下限：总内存的 1/12，夹在 400MB~1200MB 之间。
        # 固定阈值在 8GB 机器上会误判（macOS 常态可用内存本来就不高）。
        self.min_free_mb = int(min_free_mb) if min_free_mb else \
            max(400, min(1200, self.device.total_mem_mb // 12))
        self.log = log or (lambda *a, **k: None)

        # 目标：视频数 × 2 个解码进程 × job_threads ≈ 物理核数
        by_cpu = max(1, self.device.physical_cpus // (2 * max(1, self.job_threads)))
        by_mem = max(1, (self.device.total_mem_mb - mem_reserve_mb) // per_job_mem_mb)
        auto = max(1, min(by_cpu, by_mem, 6))
        if self.device.battery:
            auto = max(1, (auto + 1) // 2)
        self.limit = int(max_jobs) if max_jobs else auto
        self.limit = max(1, self.limit)
        self.auto_limit = auto
        self._sem = asyncio.Semaphore(self.limit)
        self._active = 0
        self._peak = 0
        self._throttled = 0
        self.lock = asyncio.Lock()

    # 每个任务分到的 ffmpeg 线程数（避免 N 个进程各开满核）
    @property
    def job_threads(self) -> int:
        """每个 ffmpeg 进程的线程上限。

        扫描阶段每个视频**峰值会同时跑 2 个解码进程**（全帧率小图 + 采样大图），
        所以「视频数 × 2 × 本值」才等于要占用的核数。取 2 是实测下来的平衡点：
        再高会让 8 核机器上的 ffmpeg 互相抢核，单个视频反而更慢。
        """
        return 2 if self.device.physical_cpus >= 4 else 1

    def summary(self) -> Dict[str, Any]:
        return {**self.device.as_dict(), "job_limit": self.limit, "auto_limit": self.auto_limit,
                "ffmpeg_threads_per_job": self.job_threads, "peak_active": self._peak,
                "throttle_waits": self._throttled, "cpu_busy_limit_pct": self.busy_limit_pct,
                "min_free_mb": self.min_free_mb}

    def describe(self) -> str:
        d = self.device
        return (f"设备: {d.physical_cpus} 物理核 / {d.logical_cpus} 逻辑核 / "
                f"{d.total_mem_mb / 1024:.1f} GB 内存"
                + ("（电池供电）" if d.battery else "")
                + f" → 并发上限 {self.limit} 个视频，每个 ffmpeg 限 {self.job_threads} 线程")

    async def _headroom_ok(self) -> bool:
        pr = sample_pressure(self._pcache)
        if 0 <= pr.busy_pct > self.busy_limit_pct:
            pr.reason = f"CPU 占用 {pr.busy_pct:.0f}%（上限 {self.busy_limit_pct:.0f}%）"
            self._last = pr
            return False
        if pr.avail_mb < self.min_free_mb:
            pr.reason = f"可用内存 {pr.avail_mb} MB（下限 {self.min_free_mb} MB）"
            self._last = pr
            return False
        self._last = pr
        return True

    async def acquire(self, label: str = "") -> None:
        """拿到一个执行槽位。

        关键策略：**第一个任务永远放行**。资源检查只用来拦住「再加一个」的冲动，
        而不是让单个视频在机器本来就忙时干等 —— 那只会让用户觉得工具卡住了。
        """
        waited = 0.0
        while True:
            if self._active == 0:
                break
            if await self._headroom_ok():
                break
            if waited >= self.max_wait_s:
                self.log(f"  · 已等待 {waited:.0f}s 仍未缓解（{self._last.reason}），"
                         f"按当前并发继续启动：{label}")
                break
            async with self.lock:
                self._throttled += 1
            self.log(f"  ⏸ 系统繁忙，暂缓启动：{self._last.reason}（等待中：{label}）")
            await asyncio.sleep(2.0)
            waited += 2.0
        await self._sem.acquire()
        async with self.lock:
            self._active += 1
            self._peak = max(self._peak, self._active)

    def release(self) -> None:
        async def _rel():
            async with self.lock:
                self._active = max(0, self._active - 1)
        asyncio.get_event_loop().create_task(_rel())
        self._sem.release()

    class _Slot:
        def __init__(self, gov: "Governor", label: str):
            self.gov, self.label = gov, label

        async def __aenter__(self):
            await self.gov.acquire(self.label)
            return self

        async def __aexit__(self, *exc):
            self.gov.release()
            return False

    def slot(self, label: str = "") -> "Governor._Slot":
        return Governor._Slot(self, label)
