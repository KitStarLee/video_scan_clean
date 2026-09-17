# -*- coding: utf-8 -*-
"""异步 ffmpeg / ffprobe 封装。

设计要点：
  · 全部走 ``asyncio.create_subprocess_exec``，长任务（重编码/重封装/解码）不阻塞事件循环。
  · ``stream_rawvideo`` 把解码结果按帧流式产出，调用方可以「一次解码喂多个消费者」，
    不必为了不同分析重复解码同一个视频。
  · 每个进程都可以降优先级（macOS 用 ``taskpolicy -b``，其他 POSIX 用 ``nice``；
    Windows 既没有 nice 也没有 taskpolicy，改用进程优先级类 creationflags），
    这是「不要把电脑搞卡」的第一道保险。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

__all__ = [
    "FFmpegError", "run_cmd", "ffprobe_json", "decode_null_check",
    "stream_rawvideo", "scale_height", "decode_audio_mono",
    "priority_prefix", "windows_creationflags", "kill_tree",
]


class FFmpegError(RuntimeError):
    pass


# ---------------------------------------------------------------- 降优先级
# Windows 上没有 taskpolicy/nice；探测本身也包一层 try，保证任何平台都不会在
# import 阶段炸掉（``shutil.which`` 正常只返回 None）。
try:
    _IS_WINDOWS = os.name == "nt"
    _HAS_TASKPOLICY = shutil.which("taskpolicy") is not None
    _HAS_NICE = shutil.which("nice") is not None
except Exception:  # pragma: no cover
    _IS_WINDOWS = False
    _HAS_TASKPOLICY = False
    _HAS_NICE = False


def priority_prefix(level: int = 2) -> List[str]:
    """给命令加「降级」前缀，让系统在批量处理时仍然跟手。

    分级（实测数据来自 M 系列 8 核 Mac，3 分钟 1080p 视频的采样解码）：
      0  不加前缀                        3.2s
      1  nice -n 5                       3.0s   ← 几乎零代价
      2  nice -n 10                      3.0s   ← **默认**
      3  taskpolicy -b（后台 QoS）       29.0s  ← 慢 9 倍，只在「我要干活别卡我」时才用

    结论：macOS 基本忽略 nice，但 ``taskpolicy -b`` 会把进程压到能效核并严格限流。
    所以默认只用 nice，把 taskpolicy 留给显式选择的用户。

    Windows：没有 nice/taskpolicy 可执行文件，这里恒返回 ``[]``；降优先级改由
    :func:`windows_creationflags` 通过 ``creationflags`` 实现。
    """
    if _IS_WINDOWS:
        return []
    if level <= 0:
        return []
    if level >= 3 and _HAS_TASKPOLICY:
        return ["taskpolicy", "-b"]
    if _HAS_NICE:
        return ["nice", "-n", str(min(19, max(1, level) * 5))]
    return []


def windows_creationflags(level: int) -> int:
    """返回 create_subprocess_exec 在 Windows 下应使用的 ``creationflags``。

    Windows 没有 nice，只能设置进程优先级类：
      0      0（不改优先级）
      1-2    BELOW_NORMAL_PRIORITY_CLASS（低于正常，对应 nice 的意图）
      >=3    IDLE_PRIORITY_CLASS（最低，对应 macOS 的 taskpolicy -b）

    非 Windows 恒返回 0；任何异常也返回 0，绝不抛出。
    """
    if not _IS_WINDOWS:
        return 0
    try:
        if level >= 3:
            return int(getattr(subprocess, "IDLE_PRIORITY_CLASS", 0x00000040))
        if level >= 1:
            return int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000))
    except Exception:  # pragma: no cover
        pass
    return 0


def _spawn_kwargs(priority: int) -> Dict[str, Any]:
    """构造 ``asyncio.create_subprocess_exec`` 的平台相关参数。

    POSIX：保留 ``start_new_session=True``（独立进程组，killpg 能整组清理）。
    Windows：``start_new_session`` 无意义，改为传 creationflags 降优先级。
    """
    kw: Dict[str, Any] = {}
    if _IS_WINDOWS:
        flags = windows_creationflags(priority)
        if flags:
            kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    return kw


def _threads_flag(threads: int) -> List[str]:
    return ["-threads", str(threads)] if threads and threads > 0 else []


async def run_cmd(cmd: Sequence[str], *, timeout: Optional[float] = None,
                  priority: int = 0) -> Tuple[int, bytes, bytes]:
    """执行命令，返回 (returncode, stdout, stderr)。不会因为非零退出而抛异常。"""
    argv = priority_prefix(priority) + list(cmd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs(priority),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        raise FFmpegError(f"命令超时（{timeout}s）: {' '.join(cmd[:4])}...")
    except asyncio.CancelledError:
        await kill_tree(proc)
        raise
    return proc.returncode or 0, out or b"", err or b""


def _taskkill_tree(pid: int) -> bool:
    """Windows：用 ``taskkill /F /T`` 结束整棵进程树。返回是否执行成功。

    任何异常（taskkill 不存在、超时、权限不足）都被吞掉并返回 False，
    由调用方回退到 ``proc.kill()``。
    """
    try:
        kwargs: Dict[str, Any] = {"stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "timeout": 10}
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        if flags:
            kwargs["creationflags"] = flags
        done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], **kwargs)
        return done.returncode == 0
    except Exception:
        return False


async def kill_tree(proc: "asyncio.subprocess.Process") -> None:
    """杀掉整个进程树（ffmpeg 会派生线程/子进程，只 kill 父进程可能留垃圾）。

    POSIX 走 ``killpg``；Windows 没有进程组/``killpg``，走 ``taskkill /F /T``。
    对已经退出的进程重复调用是安全的 no-op。
    """
    if proc.returncode is not None:
        return
    if _IS_WINDOWS:
        if not _taskkill_tree(proc.pid):
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        await proc.wait()
    except Exception:
        pass


async def _communicate(proc, timeout: Optional[float]) -> Tuple[int, bytes, bytes]:
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        raise FFmpegError("命令超时")
    except asyncio.CancelledError:
        await kill_tree(proc)
        raise
    return proc.returncode or 0, out or b"", err or b""


# ---------------------------------------------------------------- ffprobe
async def ffprobe_json(path: str, entries: str = "format:streams",
                       extra: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json"]
    if extra:
        cmd += list(extra)
    cmd.append(path)
    rc, out, err = await run_cmd(cmd)
    if rc != 0:
        raise FFmpegError(f"ffprobe 失败: {err.decode('utf-8', 'replace')[:400]}")
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception as exc:
        raise FFmpegError(f"ffprobe 输出无法解析: {exc}")


async def probe_streams(path: str) -> Dict[str, Any]:
    return await ffprobe_json(path, "format:streams", ["-show_chapters"])


def video_size_of(probe: Dict[str, Any]) -> Tuple[int, int]:
    for st in probe.get("streams", []):
        if st.get("codec_type") == "video":
            return int(st.get("width") or 0), int(st.get("height") or 0)
    return 0, 0


# ---------------------------------------------------------------- 帧流
def scale_height(src_w: int, src_h: int, width: int) -> int:
    """复刻 ffmpeg ``scale=W:-2`` 的高度取整规则（偶数、四舍五入）。"""
    if src_w <= 0:
        return 0
    h = int(round(src_h * width / src_w / 2.0)) * 2
    return max(2, h)


@dataclass
class RawFrame:
    index: int
    t: float
    data: Any            # np.ndarray (h, w) 或 (h, w, 3)


async def stream_rawvideo(path: str, *, vf: str, width: int, height: int,
                          pix_fmt: str = "gray", fps: Optional[float] = None,
                          threads: int = 0, priority: int = 2,
                          start: Optional[float] = None,
                          duration: Optional[float] = None,
                          fps_mode: Optional[str] = None,
                          max_frames: Optional[int] = None):
    """流式产出解码后的原始帧。

    ``fps`` 给定时用 ffmpeg 的 ``fps`` 滤镜采样，时间戳按 ``index/fps`` 计算；
    否则按原始帧率输出，时间戳需要调用方自己按 fps 换算。
    必须用 ``async for`` 消费；中途 break 也会清理掉解码进程。

    ``fps_mode``：``rawvideo`` 输出的默认同步模式是 CFR —— 这会把「稀疏抽帧」
    滤镜（``select``）选中的少量帧**重复填充回原帧率**（选 2 帧能吐出上万帧）。
    凡是 ``vf`` 里带 ``select`` 的抽帧场景都必须传 ``fps_mode="passthrough"``。
    ``max_frames``：给输出帧数兜底（``-frames:v``），即使上面写错也不会失控。
    """
    if np is None:
        raise FFmpegError("缺少 numpy")
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-an", "-sn", "-dn", "-vf", vf, "-pix_fmt", pix_fmt]
    cmd += _threads_flag(threads)
    if fps_mode:
        cmd += ["-fps_mode", fps_mode]
    if max_frames:
        cmd += ["-frames:v", str(int(max_frames))]
    cmd += ["-f", "rawvideo", "-"]

    argv = priority_prefix(priority) + cmd
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **_spawn_kwargs(priority),
    )
    npx = width * height * (3 if pix_fmt == "rgb24" else 1)
    shape = (height, width, 3) if pix_fmt == "rgb24" else (height, width)
    i = 0
    try:
        assert proc.stdout is not None
        while True:
            try:
                buf = await proc.stdout.readexactly(npx)
            except asyncio.IncompleteReadError:
                break
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(shape)
            t = (i / fps) if fps else float(i)
            yield RawFrame(index=i, t=t, data=arr)
            i += 1
    finally:
        if proc.returncode is None:
            try:
                if proc.stdout:
                    proc.stdout.feed_eof()
            except Exception:
                pass
        await kill_tree(proc)


async def decode_audio_mono(path: str, *, sr: int = 44100, seconds: float = 180.0,
                            threads: int = 0, priority: int = 2):
    """解码成单声道 float32 numpy 数组。"""
    if np is None:
        raise FFmpegError("缺少 numpy")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1",
           "-ar", str(sr), "-t", f"{seconds:.3f}"] + _threads_flag(threads) + \
          ["-f", "f32le", "-"]
    rc, out, err = await run_cmd(cmd, priority=priority)
    if rc != 0:
        raise FFmpegError(f"音频解码失败: {err.decode('utf-8', 'replace')[:400]}")
    return np.frombuffer(out, dtype="<f4").astype(np.float32)


async def decode_null_check(path: str, priority: int = 2) -> Tuple[bool, str, float]:
    """全片解码一遍，验证输出是否真的能播。返回 (是否零错误, stderr, 耗时秒)。"""
    import time
    t0 = time.time()
    rc, _out, err = await run_cmd(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"],
        priority=priority)
    e = err.decode("utf-8", "replace").strip()
    return (rc == 0 and not e), e, time.time() - t0
