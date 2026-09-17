# -*- coding: utf-8 -*-
"""mp4tool —— MP4 隐藏数据扫描 + 分层修复工具包。

对外主要接口：
    mp4tool.pipeline.process_many(...)   批量异步处理（scan → repair）
    mp4tool.scanner.ScanSession          单个视频的扫描
    mp4tool.repair.RepairSession         单个视频的修复
    mp4tool.resources.DeviceProfile      设备能力探测与并发调控
"""

# 必须在 numpy 被导入之前设置：BLAS 默认会为每个矩阵运算开满核数的线程，
# 多视频并发时 N 个任务 × M 个线程会互相抢，实测把 load average 顶到 27。
# 限制成单线程后，并发反而更快，系统也不会发烫。
import os as _os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ.setdefault(_v, "1")
del _os, _v

__version__ = "2.0.0"
__all__ = ["__version__"]
