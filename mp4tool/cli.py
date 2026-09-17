# -*- coding: utf-8 -*-
"""命令行入口。三个模式共用同一套参数：

    main.py <目标…>           扫描 + 修复（默认）
    main.py scan <目标…>      只扫描
    main.py repair <目标…>    只修复（会自动读取旁边的扫描报告）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List, Optional, Sequence

from . import __version__
from .pipeline import PipelineOptions, collect_targets, run_batch
from .repair import RepairOptions
from .resources import Governor, probe_device
from .utils import setup_console_utf8


__all__ = ["build_parser", "main", "main_scan", "main_repair", "main_all"]


def build_parser(prog: str = "mp4tool") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="MP4 隐藏数据扫描 + 清理修复（异步批量）\n"
                    "修复 = 涂掉隐藏标记 + 丢掉外包装；去水印是可选的额外一步（有损）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例（%(prog)s 只是当前子命令，整条命令按下面写）:
  python main.py video.mp4                     # 扫描 + 完整清理 + 修复（默认，不需要任何参数）
  python main.py ./videos -o ./out --jobs 2    # 批量
  python main.py video.mp4 --wm-ask            # 额外去掉画面里可见的水印（有损，弹窗勾选）
  python main.py repair video.mp4 --tier-a-only  # 最保守：只打等长补丁，不重建容器
  python main.py scan video.mp4 --deep         # 只扫描
  python main.py slim video.mp4                # 画质优先瘦身（独立一条线）
  python main.py --device-info                 # 看看这台机器会自动开多少并发

默认档做的事：vid/encoder 置空 · SEI 自动处理（SDR 全删 / HDR 保留静态元数据）·
重建容器并丢掉封面图、字幕与 data 轨、章节、全部容器元数据。
不会动音频（-c:a copy），也不会碰像素域不可见水印 —— 详见 README「清理范围」。
""")

    ap.add_argument("targets", nargs="*", help="视频文件或文件夹（可多个）")
    ap.add_argument("-o", "--outdir", default="output", help="输出根目录（默认 ./output）")
    ap.add_argument("--jobs", type=int, default=0,
                    help="同时处理的视频数，0=按设备能力自动（默认）")
    ap.add_argument("--device-info", action="store_true", help="只打印设备能力与并发预算")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要处理的文件，不实际执行")
    ap.add_argument("-v", "--verbose", action="store_true", help="把每个视频的详细日志也打到屏幕")
    ap.add_argument("--keep-work", action="store_true", help="保留中间文件（.work 目录）")

    g = ap.add_argument_group("扫描")
    g.add_argument("--quick", action="store_true", help="只做静态/容器分析，不解码音视频")
    g.add_argument("--deep", action="store_true", help="全文件字符串扫描 + 全部 sample 的 NAL 扫描")
    g.add_argument("--no-qr", dest="qr", action="store_false", default=True, help="关闭矩形码扫描")
    g.add_argument("--no-watermark", dest="watermark", action="store_false", default=True,
                   help="关闭静态叠加水印检测")
    g.add_argument("--no-frames", dest="frames", action="store_false", default=True,
                   help="关闭逐帧异常检测")
    g.add_argument("--no-audio", dest="audio", action="store_false", default=True,
                   help="关闭音频频谱分析")
    g.add_argument("--hires", action="store_true",
                   help="额外解一遍 1080p 用于水印图层导出（更清楚但要多解一次码）")
    g.add_argument("--fps", type=float, default=1.0, help="水印采样帧率（默认 1.0）")
    g.add_argument("--qr-fps", type=float, default=0.5, help="矩形码采样帧率（默认 0.5）")
    g.add_argument("--wm-width", type=int, default=0,
                   help="采样帧宽度；0=自动（默认，贴着源分辨率并守住内存预算）")

    # 默认就是「完全清理」：用户不需要选任何东西。
    # 清理需要的信息（vid/encoder 是否置空、SEI 删到什么程度、要不要重封装）
    # 全部由程序自己推导，不再暴露成参数。
    g = ap.add_argument_group(
        "修复方式（默认：完整清理）",
        "默认会把文件重新写一遍，只留下画面和声音：\n"
        "  ① 涂掉藏在里面的标记（vid 视频号 / 编码器指纹 / SEI / 缝隙垃圾）\n"
        "  ② 丢掉外包装（封面图 / 字幕轨 / 章节 / 全部元数据）\n"
        "画面和声音本身一个字节都不改，画质不受影响。")
    g.add_argument("--minimal", dest="tier_a_only", action="store_true",
                   help="最保守：不重写文件，只在原文件上把几个标记涂掉。"
                        "文件长度和结构完全不变，播放风险最低；"
                        "代价是封面图 / 字幕轨 / 标题等『外包装』会留下来")
    # 旧名字，兼容用
    g.add_argument("--tier-a-only", dest="tier_a_only", action="store_true",
                   help=argparse.SUPPRESS)
    g.add_argument("--export-to-source", action="store_true",
                   help="把修复后的视频导出到原视频所在目录，原 output 目录里的修复视频就没了")

    # ↓ 下面这些只为兼容旧命令而保留，默认不出现在 --help 里（不看就不乱）。
    #   显式写出来仍然生效，但正常用法不需要它们。
    g.add_argument("--vid", default=None, choices=["random", "blank", "keep"],
                   help=argparse.SUPPRESS)
    g.add_argument("--vid-keep-prefix", type=int, default=-1, help=argparse.SUPPRESS)
    g.add_argument("--encoder", default=None, choices=["blank", "keep"], help=argparse.SUPPRESS)
    g.add_argument("--sei", default=None, help=argparse.SUPPRESS)
    g.add_argument("--remux", default=None, choices=["auto", "always", "never"],
                   help=argparse.SUPPRESS)
    g.add_argument("--clean", action="store_true", help=argparse.SUPPRESS)

    g = ap.add_argument_group("水印处理（可选；有损，默认关闭）")
    # mask（笔画级清理）的阈值/膨胀**不在这里暴露** —— 用 --wm-ask 在网页上拖着调，
    # 并且能实时看到遮罩效果。命令行只要选个默认值就够了。
    g.add_argument("--wm", default="none", choices=["none", "fill", "blur", "delogo", "mask"],
                   help="水印处理方式（默认 none）。推荐用 --wm-ask 在界面上选")
    g.add_argument("--wm-ask", action="store_true",
                   help="检测到水印后弹出选择界面，由你勾选要去掉哪几处（推荐配合 --wm-region 之外使用）")
    g.add_argument("--wm-ask-timeout", type=float, default=0.0,
                   help="等待人工勾选的秒数，0=一直等（默认）。等待期间不占用并发，其它视频照常跑")
    g.add_argument("--wm-region", action="append", default=None,
                   help="手工指定区域 x,y,w,h（可重复，优先于扫描结果）")
    g.add_argument("--wm-color", default="black", help="fill 模式颜色")
    g.add_argument("--wm-blur", type=int, default=12, help="blur 模式强度")
    g.add_argument("--wm-crf", type=int, default=18, help="重编码 CRF（默认 18）")
    g.add_argument("--wm-preset", default="medium", help="x264 preset")

    g = ap.add_argument_group("验证与性能")
    g.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                   help="跳过输出验证")
    g.add_argument("--no-decode-check", dest="decode_check", action="store_false", default=True,
                   help="跳过全片解码验证（快，但少了最强的检查）")
    g.add_argument("--no-rescan", dest="rescan", action="store_false", default=True,
                   help="跳过「修复后复扫对比」（最贵的一步，约占修复耗时 2/3）")
    g.add_argument("--rename-hash", action="store_true", help="输出按新内容 MD5 命名")
    g.add_argument("--priority", type=int, default=2, choices=[0, 1, 2, 3],
                   help="子进程降级等级：0=不降级 1=nice5 2=nice10(默认) "
                        "3=taskpolicy后台(系统最跟手，但实测慢约 9 倍)")
    return ap


def _make_options(args: argparse.Namespace, mode: str) -> PipelineOptions:
    # ---- 默认档：完全清理（用户什么参数都不写就是这个）----
    #   · vid / encoder 一律置空（等长原地改写，零播放风险）
    #   · SEI 交给修复器自动判断（SDR 全删 / HDR 保留静态元数据）
    #   · 强制执行重封装，只留「真画面 + 声音」
    # --tier-a-only 是唯一的保守逃生门：只打等长补丁，不重建容器。
    if args.tier_a_only:
        clean, remux = False, "never"
    else:
        clean = True
        remux = args.remux or "always"
    repair = RepairOptions(
        vid=args.vid or "blank", vid_keep_prefix=args.vid_keep_prefix,
        encoder=args.encoder or "blank",
        sei=args.sei or "auto",
        export_to_source=args.export_to_source,
        remux=remux, clean=clean, wm=args.wm, wm_region=args.wm_region,
        wm_ask=args.wm_ask, wm_ask_timeout=args.wm_ask_timeout,
        wm_color=args.wm_color, wm_blur=args.wm_blur, wm_crf=args.wm_crf,
        wm_preset=args.wm_preset, verify=args.verify, decode_check=args.decode_check,
        rescan=getattr(args, 'rescan', True),
        rename_hash=args.rename_hash, priority=args.priority)
    return PipelineOptions(
        outdir=os.path.abspath(args.outdir),
        do_scan=(mode in ("all", "scan")),
        do_repair=(mode in ("all", "repair")),
        max_jobs=args.jobs, keep_work=args.keep_work,
        quick=args.quick, deep=args.deep, qr=args.qr, watermark=args.watermark,
        frames=args.frames, audio=args.audio, hires=args.hires, fps=args.fps,
        qr_fps=args.qr_fps, wm_width=args.wm_width,
        repair=repair, verbose=args.verbose)


def main(argv: Optional[Sequence[str]] = None, mode: str = "all") -> int:
    setup_console_utf8()      # Windows 控制台/重定向默认是 cp936/cp1252，先切成 UTF-8
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "slim":
        # 瘦身是完全独立的一条线（不读扫描报告、不碰 Tier A/B/C），
        # 可以直接 `python main.py slim …`，也可以走独立的 `python slim.py …`。
        from .slim import main as slim_main
        return slim_main(argv[1:])
    prog = "mp4tool"
    if argv and argv[0] in ("scan", "repair", "all", "clean"):
        mode = argv.pop(0)
        # `clean` 只是默认档的别名 —— 清理本来就是修复的一部分，
        # 不写子命令、写 repair、写 clean 三种写法结果一样。
        if mode == "clean":
            mode = "all"
        prog = f"mp4tool {mode}"
    args = build_parser(prog).parse_args(argv)

    if args.device_info:
        gov = Governor(max_jobs=(args.jobs or None))
        print(gov.describe())
        print(f"  每进程 ffmpeg 线程上限: {gov.job_threads}")
        print(f"  自动并发上限: {gov.auto_limit}   实际采用: {gov.limit}")
        print(f"  设备探测: {gov.device.as_dict()}")
        return 0

    if not args.targets:
        build_parser(prog).print_help()
        return 2

    targets = collect_targets(args.targets)
    if not targets:
        print("没有找到任何视频文件。", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"将处理 {len(targets)} 个文件，输出根目录 {os.path.abspath(args.outdir)}：")
        for t in targets:
            print(f"  · {t}")
        return 0

    opts = _make_options(args, mode)
    try:
        asyncio.run(run_batch(targets, opts))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


def main_all(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, mode="all")


def main_scan(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, mode="scan")


def main_repair(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, mode="repair")
