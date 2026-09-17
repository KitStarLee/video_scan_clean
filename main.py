#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp4tool 顶层入口。

    python main.py <视频或文件夹…> [选项]

先扫描（找出暗码/标记/ID/水印/隐藏数据流），再按 Tier A/B/C 分层修复，
修复后的视频写到 <输出目录>/<视频名>/repaired/ 下。

细节见 README.md；`python main.py --help` 有全部参数。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mp4tool.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
