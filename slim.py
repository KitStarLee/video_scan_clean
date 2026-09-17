#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""画质优先的视频瘦身 —— 独立入口，和扫描/修复互不干扰。

    python slim.py <视频或文件夹…> [选项]
    python slim.py --help

它只做一件事：在**感知质量不掉**（用 VMAF/SSIM 把关）的前提下把文件压小。
压完会再验一次，不达标就丢弃输出、保留原文件。
细节见 README.md 的「画质优先瘦身」一节。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mp4tool.slim import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
