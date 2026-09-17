# -*- coding: utf-8 -*-
"""通用小工具：字节统计、哈希、人类可读格式、子进程执行、报告条目。

这里刻意保持「无状态 + 无第三方依赖」，方便被任何线程/任务安全调用。
"""
from __future__ import annotations

import hashlib
import math
import mmap
import os
import shutil
import subprocess
import sys
from collections import Counter
from typing import Any, Dict, Optional, Sequence

__all__ = [
    "HIGH", "MED", "LOW", "INFO", "SEV_ORDER", "SEV_ICON",
    "Finding", "eprint", "human", "entropy", "nonzero_ratio", "have",
    "hexdump", "run", "hash_file", "map_file", "md5_file", "sha256_file",
    "setup_console_utf8",
]

# ---------------------------------------------------------------- 可疑度分级
HIGH, MED, LOW, INFO = "HIGH", "MEDIUM", "LOW", "INFO"
SEV_ORDER = {HIGH: 0, MED: 1, LOW: 2, INFO: 3}
SEV_ICON = {HIGH: "[!!]", MED: "[! ]", LOW: "[. ]", INFO: "[i ]"}


def setup_console_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台/重定向下编码炸掉。

    本工具会打印 ``✔ ✗ ⚠`` 等字符，Windows 默认代码页（cp936/cp1252）编码不了，
    会抛 ``UnicodeEncodeError``。这里尽力 ``reconfigure(encoding="utf-8",
    errors="replace")``；流不是文本流（没有 ``reconfigure``）或调用失败时静默跳过，
    绝不抛异常。调用方负责在程序启动时调用一次，本模块自己不调用。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is None:
                continue
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}PB"


def entropy(data: bytes) -> float:
    """Shannon 熵, bit/byte, 0..8"""
    if not data:
        return 0.0
    c = Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def nonzero_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(1 for b in data if b) / len(data)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def hexdump(data: bytes, base: int = 0, limit: int = 512) -> str:
    out = []
    data = data[:limit]
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{base + off:08x}  {hx:<47}  |{asc}|")
    return "\n".join(out)


def run(cmd: Sequence[str], binary: bool = True, check: bool = True) -> Any:
    """同步执行子进程。

    只应该在工作线程里调用（扫描器的静态检查阶段），不要直接在事件循环里用。
    """
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"命令失败 ({p.returncode}): {' '.join(cmd[:6])}...\n"
            f"{p.stderr.decode('utf-8', 'replace')[:2000]}"
        )
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def hash_file(path: str, algos: Sequence[str] = ("md5", "sha1", "sha256"),
              chunk: int = 4 << 20) -> Dict[str, str]:
    """一次遍历算出多个哈希（旧版分三次读，这里合并成一次 IO + 一次遍历）。"""
    hs = {a: hashlib.new(a) for a in algos}
    with open(path, "rb") as fh:
        while True:
            blk = fh.read(chunk)
            if not blk:
                break
            for h in hs.values():
                h.update(blk)
    return {a: h.hexdigest() for a, h in hs.items()}


def map_file(path: str):
    """只读映射整个文件，返回 ``(data, closer)``。

    **不要再用 ``fh.read()`` 读整个视频**：4G 的文件读一遍就是 4G 内存，
    加上后面复制一份直接就爆了。mmap 把文件映射进地址空间，
    只有真正**切片**的时候才拷贝，用法和 bytes 完全一致
    （切片 / len / find / struct.unpack_from 都支持）。
    用完必须调用 ``closer()``；文件为空时无法映射，退回空 bytes。
    """
    fh = open(path, "rb")
    try:
        if os.fstat(fh.fileno()).st_size <= 0:
            fh.close()
            return b"", (lambda: None)
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)

        def _close() -> None:
            for x in (mm, fh):
                try:
                    x.close()
                except Exception:
                    pass
        return mm, _close
    except Exception:
        try:
            fh.seek(0)
            data = fh.read()
            return data, fh.close
        except Exception:
            try:
                fh.close()
            except Exception:
                pass
            return b"", (lambda: None)


def md5_file(path: str) -> str:
    return hash_file(path, ("md5",))["md5"]


def sha256_file(path: str) -> str:
    return hash_file(path, ("sha256",))["sha256"]


# ---------------------------------------------------------------- 报告条目
class Finding:
    """一条扫描结论。"""

    __slots__ = ("sev", "cat", "title", "detail", "evidence")

    def __init__(self, sev: str, cat: str, title: str, detail: str = "", evidence: Any = None):
        self.sev, self.cat, self.title, self.detail, self.evidence = sev, cat, title, detail, evidence

    def as_dict(self) -> Dict[str, Any]:
        d = {"severity": self.sev, "category": self.cat, "title": self.title, "detail": self.detail}
        if self.evidence is not None:
            d["evidence"] = self.evidence
        return d
