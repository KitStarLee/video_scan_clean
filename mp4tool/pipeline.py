# -*- coding: utf-8 -*-
"""批量编排：对一批视频依次做「扫描 → 修复」，并发数由资源调控器自动决定。

每个视频的输出结构::

    <outdir>/
      index.json                      批次汇总
      <视频名>/
        scan/
          scan_report.txt
          scan_report.json
          artifacts/<视频名>/...       水印图层、SEI 原始字节、频谱图…
        repaired/
          <视频名>.repaired.mp4        修复后的视频
          repair_report.txt
        pipeline.log                   这个视频的完整过程日志
        summary.json                   单个视频的汇总
        .work/                         中间文件（默认清理）
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import time
from datetime import datetime 
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import utils as U
from .repair import (RepairOptions, RepairResult, RepairSession, patch_len,
                     rescan_and_compare)
from .resources import Governor, probe_device
from .scanner import ScanOptions, ScanResult, ScanSession

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".m4a", ".3gp", ".3g2", ".mkv", ".webm", ".avi", ".ts"}

# Windows 保留设备名：拿它们当目录名会直接创建失败
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}

__all__ = ["PipelineOptions", "VideoOutcome", "BatchResult", "collect_targets",
           "run_batch", "safe_name"]


def safe_name(name: str) -> str:
    """把文件名变成安全的目录名（macOS / Windows 都能用）。"""
    s = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    s = s[:80] or "video"
    # Windows 不允许目录名以点或空格结尾（"abc." 会被静默截断成 "abc"）
    s = s.rstrip(". ")
    # Windows 保留设备名做目录名会直接失败
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s or "video"


def collect_targets(targets: Sequence[str], recursive: bool = False) -> List[str]:
    """把「文件或文件夹」的混合列表展开成视频文件列表（去重、稳定排序）。"""
    out: List[str] = []
    seen = set()
    
    EXCLUDE_KEYWORD = "repaired"  # 关键词，大小写不敏感
    
    def is_excluded(name: str) -> bool:
        if EXCLUDE_KEYWORD in name.lower():
            print(f"{name} 包含 {EXCLUDE_KEYWORD} 关键词，可能是修复后的文件/文件夹，不可以再次修复")
            return True
        return False

    def add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)

    for t in targets:
        if os.path.isdir(t):
            # 如果传入的根目录本身就包含关键词，直接跳过
            if is_excluded(os.path.basename(os.path.abspath(t))): 
                continue
            
            # 递归进入 t 下的每一个子目录
            if recursive:
                for root, _dirs, files in os.walk(t):
                    
                    # ★ 关键：原地过滤 dirs，阻止 os.walk 进入这些子目录
                    _dirs[:] = [d for d in _dirs if not is_excluded(d)]
                    
                    for f in sorted(files):
                        if is_excluded(f): 
                            continue
                        if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                            add(os.path.join(root, f))
            else:
                # 只列出顶层的文件
                for f in sorted(os.listdir(t)):
                    fp = os.path.join(t, f)
                    if is_excluded(f): 
                        continue
                    if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in VIDEO_EXT:
                        add(fp)
        elif os.path.isfile(t):
            if not is_excluded(os.path.basename(t)):
                add(t)
    return out


# ---------------------------------------------------------------- 选项
@dataclass
class PipelineOptions:
    outdir: str = "output"
    do_scan: bool = True
    do_repair: bool = True
    max_jobs: int = 0                 # 0 = 自动
    keep_work: bool = False
    # 扫描
    quick: bool = False
    deep: bool = False
    qr: bool = True
    watermark: bool = True
    frames: bool = True
    audio: bool = True
    hires: bool = False
    fps: float = 1.0
    qr_fps: float = 0.5
    wm_width: int = 0         # 0=自动（贴着源分辨率，受内存预算约束）
    # 修复
    repair: RepairOptions = field(default_factory=RepairOptions)
    verbose: bool = False


@dataclass
class VideoOutcome:
    src: str = ""
    name: str = ""
    ok: bool = True
    error: str = ""
    scan: Optional[Dict[str, Any]] = None
    repair: Optional[Dict[str, Any]] = None
    outdir: str = ""
    scan_seconds: float = 0.0
    repair_seconds: float = 0.0
    high_before: int = 0
    high_after: int = 0
    findings_before: int = 0
    findings_after: int = 0
    output_video: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BatchResult:
    started_at: str = ""
    elapsed: float = 0.0
    device: Dict[str, Any] = field(default_factory=dict)
    targets: int = 0
    succeeded: int = 0
    failed: int = 0
    outcomes: List[VideoOutcome] = field(default_factory=list)


# ---------------------------------------------------------------- 单视频流程
class _JobLog:
    """把某个视频的全部日志同时写到文件和控制台（控制台只打关键行）。"""

    def __init__(self, path: str, prefix: str, verbose: bool = False,
                 sink: Optional[Callable[[str], None]] = None):
        self.path = path
        self.prefix = prefix
        self.verbose = verbose
        self.sink = sink
        self.buf: List[str] = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.fh = open(path, "w", encoding="utf-8")

    def __call__(self, msg: str = "", *a: Any, **k: Any) -> None:
        line = msg if not a else (msg % a if "%" in msg else " ".join(map(str, (msg,) + a)))
        self.fh.write(line + "\n")
        self.fh.flush()
        self.buf.append(line)
        if self.verbose and self.sink:
            for l in line.splitlines():
                self.sink(f"{self.prefix} {l}")

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


async def process_video(src: str, opts: PipelineOptions, governor: Governor,
                        index: int, total: int, emit: Callable[[str], None]) -> VideoOutcome:
    """处理一个视频：扫描 →（可选）人工勾选水印 → 修复。

    并发槽位的用法很关键：**扫描和修复各占一次槽位，等待用户勾选水印时不占**。
    否则一个开着对话框没人管的任务会把并发额度一直攥在手里，
    后面的视频永远排不上队 —— 那就成了「一个人不点，整个批量卡死」。
    """
    name = os.path.basename(src)

    file_path = os.path.dirname(src)

    stem = safe_name(os.path.splitext(name)[0])
    vdir = os.path.join(opts.outdir, stem) #导出的文件夹路径

    scan_dir = os.path.join(vdir, "scan")       # 扫描信息路径
    rep_dir = os.path.join(vdir, "repaired")    # 修复路径
    work_dir = os.path.join(vdir, ".work")

    outcome = VideoOutcome(src=src, name=name, outdir=vdir)
    logger = _JobLog(os.path.join(vdir, "pipeline.log"),
                     f"[{index}/{total}] {stem[:18]}", opts.verbose, emit)
    tag = f"[{index}/{total}] {stem[:24]}"

    scan_res: Optional[ScanResult] = None
    scan_payload: Optional[Dict[str, Any]] = None
    ropt: Optional[RepairOptions] = None
    try:
        # ---------------- 扫描（占槽位） ----------------
        async with governor.slot(name):
            if opts.do_scan:
                emit(f"{tag} 扫描中…")
                t0 = time.time()
                sopt = ScanOptions(outdir=scan_dir, quick=opts.quick, deep=opts.deep,
                                   qr=opts.qr, watermark=opts.watermark,
                                   frames=opts.frames, audio=opts.audio,
                                   fps=opts.fps, qr_fps=opts.qr_fps,
                                   hires=opts.hires, wm_width=opts.wm_width, quiet=True,
                                   threads=governor.job_threads,
                                   priority=opts.repair.priority)
                session = ScanSession(src, sopt)
                res: ScanResult = await session.run()
                outcome.scan_seconds = time.time() - t0
                _write_scan_outputs(res, scan_dir)
                outcome.scan = {
                    "report": os.path.join(scan_dir, "scan_report.txt"),
                    "json": os.path.join(scan_dir, "scan_report.json"),
                    "severity": res.severity_counts(),
                    "high": res.high_count,
                    "findings": len(res.findings),
                    "watermark_regions": res.watermark_regions,
                    "seconds": round(outcome.scan_seconds, 2),
                }
                outcome.high_before = res.high_count
                outcome.findings_before = len(res.findings)
                sc = res.severity_counts()
                emit(f"{tag} 扫描完成 {outcome.scan_seconds:.1f}s  "
                     f"高={sc.get(U.HIGH, 0)} 中={sc.get(U.MED, 0)} "
                     f"低={sc.get(U.LOW, 0)} 信息={sc.get(U.INFO, 0)}")
                logger(f"扫描完成：{res.severity_counts()}  耗时 {outcome.scan_seconds:.2f}s")
                scan_res, scan_payload = res, res.json_payload
            else:
                # repair-only 模式：如果之前 scan 过，自动复用那份报告
                # （水印区域来自它，复扫对比也有了基线）
                scan_res, scan_payload = None, _load_scan_payload(scan_dir)
                if scan_payload:
                    emit(f"{tag} 复用已有扫描报告")
                    logger(f"复用已有扫描报告: {os.path.join(scan_dir, 'scan_report.json')}")
                else:
                    emit(f"{tag} 跳过扫描（未找到已有扫描报告）")

        # ---------------- 人工勾选水印区域（不占槽位，不阻塞其它视频） ----------------
        if opts.do_repair:
            # 注意：opts.repair 是整批共用的同一个对象，必须按视频复制再改，
            # 否则「这个视频选了哪几处水印」会串到后面别的视频上。
            ropt = replace(opts.repair, threads=governor.job_threads)
            if ropt.wm_ask:
                ropt = await _ask_watermark_for_video(
                    src, scan_res, scan_payload, ropt, scan_dir, tag, logger, emit)

        # ---------------- 修复（占槽位） ----------------
        async with governor.slot(name):
            if opts.do_repair and ropt is not None:
                emit(f"{tag} 修复中…")
                t1 = time.time()
                os.makedirs(rep_dir, exist_ok=True)
                if ropt.export_to_source:
                    out_video = os.path.join(file_path, 'repaired', f"{stem}.repaired.mp4")
                else:
                    out_video = os.path.join(rep_dir, f"{stem}.repaired.mp4")
                wm_regions = (scan_res.watermark_regions if scan_res
                              else _watermark_regions_of(scan_payload))
                if scan_payload and not scan_res:
                    outcome.findings_before = len(scan_payload.get("findings", []))
                    outcome.high_before = sum(
                        1 for f in scan_payload.get("findings", [])
                        if f.get("severity") == U.HIGH)
                session_r = RepairSession(
                    src, out_video, ropt, workdir=work_dir,
                    scan_payload=scan_payload, watermark_regions=wm_regions,
                    layer_pngs=_region_layer_pngs(scan_dir, src, len(wm_regions), scan_payload),
                    log=logger)
                rres: RepairResult = await session_r.run()

                if ropt.verify and ropt.rescan:
                    logger("\n【复扫对比】沿用原报告的扫描深度重新扫描修复后的文件")
                    rlines = await rescan_and_compare(
                        scan_payload, rres.path,
                        (scan_payload or {}).get("options") or
                        {"quick": opts.quick, "deep": opts.deep, "qr": opts.qr,
                         "watermark": opts.watermark, "frames": opts.frames,
                         "audio": opts.audio, "fps": opts.fps, "qr_fps": opts.qr_fps},
                        os.path.join(vdir, ".work", "rescan"))
                    rres.rescan_lines = rlines
                    for l in rlines:
                        logger("  " + l)

                if ropt.rename_hash and os.path.exists(rres.path):
                    newp = os.path.join(rep_dir, rres.md5_after + os.path.splitext(rres.path)[1])
                    if os.path.abspath(newp) != os.path.abspath(rres.path):
                        shutil.move(rres.path, newp)
                        rres.path = newp

                _write_repair_outputs(rres, rep_dir, scan_payload)
                outcome.repair_seconds = time.time() - t1
                outcome.output_video = rres.path
                outcome.high_after = _count_after(rres.rescan_lines)
                outcome.repair = {
                    "video": rres.path,
                    "report": os.path.join(rep_dir, "repair_report.txt"),
                    "size_before": rres.size_before,
                    "size_after": rres.size_after,
                    "md5_before": rres.md5_before,
                    "md5_after": rres.md5_after,
                    "tiers": rres.tiers,
                    "patches": len(rres.patches),
                    "verify_ok": rres.verify_ok,
                    "seconds": round(outcome.repair_seconds, 2),
                }
                emit(f"{tag} 修复完成 {outcome.repair_seconds:.1f}s  "
                     f"{U.human(rres.size_before)} → {U.human(rres.size_after)}  "
                     f"{'✔验证通过' if rres.verify_ok else '✗验证有问题'}")
                if not rres.verify_ok:
                    outcome.ok = False
                    outcome.error = "验证未通过，详见 repair_report.txt"

        if not opts.keep_work:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        import traceback
        outcome.ok = False
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger(f"!! 处理失败: {outcome.error}")
        logger(traceback.format_exc())
        emit(f"{tag} ✗ 失败: {outcome.error}")
    finally:
        _write_summary(outcome, vdir)
        logger.close()
    return outcome


async def _ask_watermark_for_video(src: str, scan_res: Optional[ScanResult],
                                   scan_payload: Optional[Dict[str, Any]],
                                   ropt: RepairOptions, scan_dir: str,
                                   tag: str, logger: Callable[..., None],
                                   emit: Callable[[str], None]) -> RepairOptions:
    """弹出交互界面让用户勾选要去掉的水印区域，返回按选择改好的 RepairOptions。

    这里是在事件循环里 **await**（不是阻塞等待），而且调用点没有持有并发槽位，
    所以用户迟迟不点，其它视频的扫描/修复照样在跑。
    任何失败（没有浏览器、端口被占、界面出错）都只会退化成「不处理水印」。
    """
    from .wm_picker import ask_watermark_regions, render_annotated_frames

    regions = (scan_res.watermark_regions if scan_res
               else _watermark_regions_of(scan_payload)) or []
    if not regions:
        emit(f"{tag} 未检测到水印区域，无需询问")
        logger("未检测到水印区域，跳过人工选择")
        return replace(ropt, wm="none")

    def say(msg: str) -> None:
        logger(msg)
        emit(f"{tag} {msg.strip()}")

    emit(f"{tag} 等待选择水印区域…（共 {len(regions)} 处）")
    shots: List[str] = []
    cleans: List[str] = []
    thumbs: List[str] = []
    layers = _region_layer_pngs(scan_dir, src, len(regions), scan_payload)
    W = H = 0
    try:
        shots, cleans, W, H, thumbs = await render_annotated_frames(
            src, regions, os.path.join(scan_dir, "wm_select"),
            priority=ropt.priority, log=logger)
    except Exception as exc:
        logger(f"  ! 生成标注图失败：{type(exc).__name__}: {exc}")
    if not shots or W <= 0 or H <= 0:
        say("无法生成水印预览图，跳过人工选择（水印未处理）")
        return replace(ropt, wm="none")

    # 默认选中「最推荐」的 delogo（邻域插值修补，最自然）；界面里它也是第一项。
    default_mode = ropt.wm if ropt.wm != "none" else "delogo"
    try:
        picked = await ask_watermark_regions(
            shots, regions, W=W, H=H, fname=os.path.basename(src),
            clean=cleans, thumbs=thumbs, layers=layers, default_mode=default_mode,
            timeout=ropt.wm_ask_timeout, log=say)
    except Exception as exc:
        logger(f"  ! 交互界面出错：{type(exc).__name__}: {exc}")
        say("交互界面出错，跳过人工选择（水印未处理）")
        return replace(ropt, wm="none")
    if not picked:
        return replace(ropt, wm="none")

    chosen = [regions[i] for i in picked["indices"]]
    specs = [f"{int(r['x'])},{int(r['y'])},{int(r['w'])},{int(r['h'])}" for r in chosen]
    logger(f"人工选定 {len(specs)} 处区域：{' | '.join(specs)}")
    extra: Dict[str, Any] = {}
    if picked.get("mask_threshold") is not None:
        extra["wm_mask_threshold"] = float(picked["mask_threshold"])
    if picked.get("mask_dilate") is not None:
        extra["wm_mask_dilate"] = int(picked["mask_dilate"])
    # 只把**勾中的**那几处对应的水印图层带下去（mask 模式靠它做笔画遮罩）
    extra["wm_mask_layers"] = [layers[i] for i in picked["indices"]
                               if layers and i < len(layers) and layers[i]]
    return replace(ropt, wm=picked["mode"], wm_region=specs, **extra)


def _scan_artifacts_dir(scan_dir: str, src: str) -> str:
    """扫描产物目录：<scan_dir>/artifacts/<去掉特殊字符的文件名>/（与 scanner 的写法一致）。"""
    return os.path.join(scan_dir, "artifacts", re.sub(r"[^\w.\-]", "_", os.path.basename(src)))


def _region_layer_pngs(scan_dir: str, src: str, n: int,
                       payload: Optional[Dict[str, Any]] = None) -> List[str]:
    """按区域顺序取出扫描导出的「水印图层」png。

    **优先**读扫描报告里记录的 `details.watermark_layer_files`（下标与区域一一对应，
    精确无歧义）。只有旧报告没有这个字段时，才退回按文件名 glob 猜 —— 那种猜法在
    artifacts 目录里同时存在两次扫描的图层时会取错（实测踩过：新旧的区域坐标不同，
    于是界面上 #1 显示成了上一次扫描的图层）。
    """
    adir = _scan_artifacts_dir(scan_dir, src)
    names = ((payload or {}).get("details") or {}).get("watermark_layer_files") or []
    out: List[str] = []
    for i in range(max(1, n)):
        name = names[i] if i < len(names) else ""
        p = os.path.join(adir, name) if name else ""
        if p and os.path.exists(p):
            out.append(p)
            continue
        # 兜底：没有清单（旧报告）才 glob
        hits = sorted(glob.glob(os.path.join(adir, f"watermark_region{i + 1}_*.png")))
        out.append(hits[0] if hits else "")
    return out


def _load_scan_payload(scan_dir: str) -> Optional[Dict[str, Any]]:
    """读取同目录下已有的扫描报告（repair-only 模式用）。"""
    p = os.path.join(scan_dir, "scan_report.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _watermark_regions_of(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not payload:
        return []
    return ((payload.get("details") or {}).get("watermark_regions") or [])


def _count_after(rescan_lines: Sequence[str]) -> int:
    """从复扫日志里抠出「仍然存在」的高危条数（用于汇总）。"""
    for l in rescan_lines:
        m = re.search(r"复扫结果:\s*(.*)", l)
        if m:
            mm = re.search(r"HIGH=(\d+)", m.group(1))
            if mm:
                return int(mm.group(1))
    return 0


def _write_scan_outputs(res: ScanResult, scan_dir: str) -> None:
    os.makedirs(scan_dir, exist_ok=True)
    with open(os.path.join(scan_dir, "scan_report.txt"), "w", encoding="utf-8") as fh:
        fh.write(res.report_text)
    with open(os.path.join(scan_dir, "scan_report.json"), "w", encoding="utf-8") as fh:
        json.dump(res.json_payload, fh, ensure_ascii=False, indent=2, default=str)


def _write_repair_outputs(res: RepairResult, rep_dir: str, scan_payload: Optional[Dict[str, Any]]) -> None:
    os.makedirs(rep_dir, exist_ok=True)
    lines = [
        f"mp4tool repair v2.0",
        f"输入: {res.src}",
        f"输出: {res.path}",
        f"原 MD5: {res.md5_before}",
        f"新 MD5: {res.md5_after}",
        f"新 SHA256: {res.sha256_after}",
        f"原大小: {res.size_before}",
        f"新大小: {res.size_after}",
        f"启用层级: {'/'.join(res.tiers) or '（无改动）'}",
        f"耗时: {res.elapsed:.2f}s",
        "",
        "Tier A 补丁:",
    ]
    for p in res.patches:
        lines.append(f"  0x{p['offset']:08x} ({patch_len(p)}B) {p['label']}")
    lines += ["", "验证:"]
    lines += ["  " + l for l in res.verify_lines]
    if res.rescan_lines:
        lines += ["", "修复前后扫描对比:"]
        lines += ["  " + l for l in res.rescan_lines]
    with open(os.path.join(rep_dir, "repair_report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_summary(outcome: VideoOutcome, vdir: str) -> None:
    try:
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(outcome.as_dict(), fh, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(vdir, "summary.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"文件: {outcome.name}\n")
            fh.write(f"状态: {'✔ 成功' if outcome.ok else '✗ 失败'}  {outcome.error}\n")
            if outcome.scan:
                fh.write(f"扫描: {outcome.scan['severity']}  耗时 {outcome.scan_seconds:.1f}s\n")
                fh.write(f"      报告 {outcome.scan['report']}\n")
            if outcome.repair:
                fh.write(f"修复: {U.human(outcome.repair['size_before'])} → "
                         f"{U.human(outcome.repair['size_after'])}  耗时 {outcome.repair_seconds:.1f}s\n")
                fh.write(f"      输出 {outcome.repair['video']}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 批次
async def run_batch(targets: Sequence[str], opts: PipelineOptions,
                    emit: Optional[Callable[[str], None]] = None) -> BatchResult:
    """并发处理一批视频，并发数由 Governor 自动决定并受系统压力动态约束。"""
    emit = emit or (lambda s: print(s, flush=True))
    videos = collect_targets(targets)
    

    now = datetime.now()
    
    date_subdir = f"{now.year % 100}-{now.month}-{now.day}"
    opts.outdir = os.path.join(opts.outdir, date_subdir)
    
    emit(f"导出的根目标为 {opts.outdir}")

    os.makedirs(opts.outdir, exist_ok=True)
    batch = BatchResult(started_at=time.strftime("%Y-%m-%d %H:%M:%S"), targets=len(videos))
    if not videos:
        emit("没有找到任何视频文件。")
        return batch

    governor = Governor(max_jobs=(opts.max_jobs or None), log=emit)
    emit("=" * 78)
    emit(f"mp4tool v2.0  批量处理 {len(videos)} 个视频")
    emit(governor.describe())
    if governor.device.battery:
        emit("（检测到电池供电，已自动减半并发；插电可跑更快）")
    emit(f"输出目录: {os.path.abspath(opts.outdir)}")
    emit("=" * 78)

    tasks = [asyncio.create_task(process_video(v, opts, governor, i + 1, len(videos), emit),
                                 name=f"video:{os.path.basename(v)}")
             for i, v in enumerate(videos)]
    t0 = time.time()
    outcomes: List[VideoOutcome] = []
    try:
        for coro in asyncio.as_completed(tasks):
            outcomes.append(await coro)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    batch.elapsed = time.time() - t0
    batch.device = governor.summary()      # 跑完再取，才有真实的峰值/等待次数
    batch.outcomes = sorted(outcomes, key=lambda o: o.name)
    batch.succeeded = sum(1 for o in outcomes if o.ok)
    batch.failed = len(outcomes) - batch.succeeded

    with open(os.path.join(opts.outdir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"started_at": batch.started_at, "elapsed": round(batch.elapsed, 2),
                   "device": batch.device, "targets": batch.targets,
                   "succeeded": batch.succeeded, "failed": batch.failed,
                   "videos": [o.as_dict() for o in batch.outcomes]},
                  fh, ensure_ascii=False, indent=2, default=str)
    _print_batch_summary(batch, opts, emit)
    return batch


def _print_batch_summary(batch: BatchResult, opts: PipelineOptions,
                         emit: Callable[[str], None]) -> None:
    emit("=" * 78)
    emit(f"批次完成：{batch.succeeded} 成功 / {batch.failed} 失败，"
         f"总耗时 {batch.elapsed:.1f}s")
    emit(f"设备: {batch.device.get('physical_cpus')} 核 / "
         f"{batch.device.get('total_mem_mb', 0) / 1024:.1f} GB，"
         f"并发上限 {batch.device.get('job_limit')}，"
         f"实际峰值 {batch.device.get('peak_active')}，"
         f"因资源紧张等待 {batch.device.get('throttle_waits', 0)} 次")
    emit("-" * 78)
    for o in batch.outcomes:
        st = "✔" if o.ok else "✗"
        extra = ""
        if o.scan:
            sev = o.scan["severity"]
            extra += f" 扫描 高={sev.get('HIGH', 0)}/{o.scan_seconds:.1f}s"
        if o.repair:
            extra += (f" 修复 {U.human(o.repair['size_after'])}"
                      f"/{o.repair_seconds:.1f}s {'✔' if o.repair['verify_ok'] else '✗'}")
        if not o.ok and o.error:
            extra += f"  {o.error[:60]}"
        emit(f"  {st} {o.name[:36]:<36}{extra}")
    emit("-" * 78)
    emit(f"汇总: {os.path.join(os.path.abspath(opts.outdir), 'index.json')}")
    emit("=" * 78)
