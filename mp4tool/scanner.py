# -*- coding: utf-8 -*-
"""单个视频的隐藏数据 / 暗码 / 标记扫描（14 项检查）。

并发模型
--------
* **静态检查**（文件层 / 容器 / 覆盖 / 元数据 / 流 / SEI / 字符串 / 自洽性）是纯 CPU
  加少量短命令，整批丢进 ``asyncio.to_thread``，一次线程切换跑完。
* **画面与音频分析**走异步 ffmpeg：两个解码进程并发，各自流式产出帧，
  一次解码同时喂给「逐帧异常」「静态叠加层」「矩形码」三个消费者 —— 旧版为此解码了 4 次。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

from . import detect as _D
from . import mp4box as _B
from . import ffmpeg_async as _F
from .utils import (HIGH, INFO, LOW, MED, SEV_ICON, SEV_ORDER, Finding, eprint,
                    entropy, have, hexdump, human, nonzero_ratio, run)

# ---- 把搬运过来的旧方法体用到的模块级名字直接映射好，保证逻辑零改动 ----
Box = _B.Box
parse_boxes = _B.parse_boxes
parse_stsd = _B.parse_stsd
walk = _B.walk
find_all = _B.find_all
box_tree_text = _B.box_tree_text
read_stbl = _B.read_stbl
sample_arrays = _B.sample_arrays
merge_intervals = _B.merge_intervals
gaps_in = _B.gaps_in
complement = _B.complement
CONTAINER_BOXES = _B.CONTAINER_BOXES
KNOWN_TOP = _B.KNOWN_TOP
SUSPICIOUS_BOXES = _B.SUSPICIOUS_BOXES
KNOWN_SEI_UUIDS = _B.KNOWN_SEI_UUIDS
scan_magics = _B.scan_magics
offset_in_segments = _B.offset_in_segments
validate_magic = _B.validate_magic
is_interesting_string = _B.is_interesting_string
fingerprint_scan = _B.fingerprint_scan
decode_ilst = _B.decode_ilst
FINGERPRINTS = _B.FINGERPRINTS
PRINT_RE = _B.PRINT_RE
HEX_RE = _B.HEX_RE
BASE64_RE = _B.BASE64_RE
URL_RE = _B.URL_RE
EMAIL_RE = _B.EMAIL_RE
IP_RE = _B.IP_RE
UUID_RE = _B.UUID_RE
UTF16_RE = _B.UTF16_RE

parse_sei = _D.parse_sei
parse_sps_dimensions = _D.parse_sps_dimensions
ascii_art = _D.ascii_art
ocr_image = _D.ocr_image
ocr_available_langs = _D.ocr_available_langs
binarize_local = _D.binarize_local
connected_boxes = _D.connected_boxes
find_finder_triples = _D.find_finder_triples
decode_gray = _D.decode_gray
probe_video_size = _D.probe_video_size
overlay_regions_from_frames = _D.overlay_regions_from_frames
gradient_median_crop = _D.gradient_median_crop
analyze_frame_series = _D.analyze_frame_series
analyze_audio_samples = _D.analyze_audio_samples
AUDIO_BANDS = _D.AUDIO_BANDS
spectrogram_png = _D.spectrogram_png

SCANNER_VERSION = "2.0"
STREAM_LUMA_WIDTH = 96
STREAM_WM_WIDTH = 512
# 采样帧宽度的自动策略：水印图层的精度直接决定 mask 遮罩的精度，
# 所以默认贴着源分辨率取；但采样帧是**全部堆在内存里**的（灰度 uint8），
# 长视频必须按内存预算自动往回收，否则会吃掉几个 GB。
WM_AUTO_MAX_W = 1080                       # 自动时的宽度上限
WM_AUTO_BUDGET = 350 * 1024 * 1024         # 采样帧堆叠内存预算
WM_MAX_SAMPLES = 800                       # 采样帧数上限（2 小时按 1fps 是 7200 帧，必须压）

# ---- C2PA 内容凭证 / DRM 的识别常量 ----
# C2PA 在 ISO BMFF 里是把 JUMBF(manifest) 塞进 uuid 盒（或 jumb 盒）。
# 已知的 C2PA uuid；但**不依赖它**——任何 uuid 盒都会报出来，
# 名字认不出来也一样报，避免只认一个魔数导致漏检。
C2PA_UUID = bytes.fromhex("6332706100110010800000aa00389b71")
C2PA_MARKERS = (b"c2pa", b"jumb", b"c2pa.claim", b"c2pa.assertion", b"urn:uuid:")
# DRM 相关盒（pssh 是保护系统头，sinf/tenc/senc 是样本加密信息）
DRM_BOXES = ("pssh", "sinf", "schi", "schm", "tenc", "senc", "saiz", "saio", "frma")
DRM_SYSTEMS = {
    bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed"): "Widevine (Google)",
    bytes.fromhex("9a04f07998404286ab92e65be0885f95"): "PlayReady (Microsoft)",
    bytes.fromhex("94ce86fb07ff4f43adb893d2fa968ca2"): "FairPlay (Apple)",
    bytes.fromhex("1077efecc0b24d02ace33c1e52e2fb4b"): "ClearKey (W3C)",
    bytes.fromhex("e2719d58a985b3c9781ab030af78d30e"): "Marlin",
}


def _img_kind(payload: bytes) -> int:
    """按魔数判断图片类型，返回 ilst 的类型号（12/13/14/27），认不出返回 0。"""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return 14
    if payload.startswith(b"\xff\xd8\xff"):
        return 13
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return 12
    if payload[:2] == b"BM":
        return 27
    return 0


def _looks_like_image(key: str, payload: bytes) -> bool:
    """ilst 里的内嵌图片：键名是 covr，或者负载本身就是一张图。"""
    return key.strip() == "covr" or _img_kind(payload) != 0


def _qr_finder_candidates(frame: Any) -> List[Any]:
    """单帧的「二值化 + 定位图案」检测（纯 CPU，供 ``asyncio.to_thread`` 调用）。

    放在模块级是为了能直接丢进线程池：这两步加起来每帧几十毫秒，
    在事件循环里连跑上万帧会把整个批量流程一起卡住。
    """
    try:
        return find_finder_triples(binarize_local(frame))
    except Exception:
        return []


@dataclass
class ScanOptions:
    """扫描选项。字段名刻意与旧版 argparse 命名空间保持一致，
    这样搬运过来的方法体（self.args.xxx）可以零改动工作。"""
    outdir: str = "."
    quick: bool = False
    deep: bool = False
    qr: bool = True
    watermark: bool = True
    frames: bool = True
    audio: bool = True
    fps: float = 1.0            # 水印/叠加层采样帧率
    qr_fps: float = 0.5         # 矩形码采样帧率
    verbose: bool = False
    hires: bool = False         # 额外解一遍高分辨率用于水印图层导出
    wm_width: int = 0           # 采样帧宽度；0=自动（贴源分辨率，受内存预算约束）
    threads: int = 0            # 每个 ffmpeg 进程的线程上限
    priority: int = 2           # 进程降级等级（0=不降级 2=nice 3=taskpolicy 后台）
    quiet: bool = False         # 批量运行时关掉逐项进度输出


@dataclass
class ScanResult:
    path: str = ""
    name: str = ""
    size: int = 0
    md5: str = ""
    sha256: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    report_text: str = ""
    json_payload: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)
    watermark_regions: List[Dict[str, Any]] = field(default_factory=list)
    elapsed: float = 0.0
    timings: Dict[str, float] = field(default_factory=dict)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f["severity"] == HIGH)

    def severity_counts(self) -> Dict[str, int]:
        c = Counter(f["severity"] for f in self.findings)
        return {k: c.get(k, 0) for k in (HIGH, MED, LOW, INFO)}



class ScanSession:
    def __init__(self, path: str, opts: ScanOptions):
        self.path = os.path.abspath(path)
        self.args = opts
        self.name = os.path.basename(path)
        self.data = b""
        self.size = 0
        self.findings: List[Finding] = []
        self.boxes: List[Box] = []
        self.anomalies: List[str] = []
        self.sections: List[str] = []
        self.json_extra: Dict[str, Any] = {}
        self.artifacts: List[str] = []
        self.outdir = opts.outdir
        self.artdir = os.path.join(opts.outdir, "artifacts", re.sub(r"[^\w.\-]", "_", self.name))
        self.watermark_regions: List[Dict[str, Any]] = []
        self.media_ranges: List[Tuple[int, int]] = []
        self.struct_segments: List[Tuple[int, int]] = []
        self.probe: Dict[str, Any] = {}
        self._close_data: Callable[[], None] = lambda: None
        self.frame_analysis: Any = None
        self.timings: Dict[str, float] = {}

    def _say(self, msg: str) -> None:
        if not getattr(self.args, "quiet", False):
            eprint(msg)

    def _time(self, label: str, t0: float) -> None:
        self.timings[label] = round(time.time() - t0, 3)
        self._say(f"    完成 ({self.timings[label]:.1f}s)")


    def add(self, sev: str, cat: str, title: str, detail: str = "", evidence: Any = None) -> None:
        self.findings.append(Finding(sev, cat, title, detail, evidence))

    def sec(self, title: str, body: str = "") -> None:
        self.sections.append(f"\n{'=' * 78}\n## {title}\n{'=' * 78}\n{body}".rstrip() + "\n")

    def save_artifact(self, fname: str, blob: bytes) -> str:
        try:
            os.makedirs(self.artdir, exist_ok=True)
            safe = re.sub(r"[^\w.\-]+", "_", fname)[:120] or "artifact.bin"
            p = os.path.join(self.artdir, safe)
            with open(p, "wb") as fh:
                fh.write(blob)
            self.artifacts.append(p)
            return p
        except Exception as exc:
            eprint(f"  ! 证据保存失败 {fname}: {exc}")
            return ""

    def layer_file(self) -> None:
        with open(self.path, "rb") as fh:
            # 只读映射，不把整个文件读进内存（4G 文件读一遍就是 4G）
            from .utils import map_file
            self.data, self._close_data = map_file(self.path)
        self.size = len(self.data)
        md5 = hashlib.md5(self.data).hexdigest()
        sha1 = hashlib.sha1(self.data).hexdigest()
        sha256 = hashlib.sha256(self.data).hexdigest()
        stem = os.path.splitext(self.name)[0].lower()
        lines = [
            f"路径        : {self.path}",
            f"大小        : {self.size} bytes ({human(self.size)})",
            f"MD5         : {md5}",
            f"SHA1        : {sha1}",
            f"SHA256      : {sha256}",
            f"整体熵      : {entropy(self.data):.4f} bit/byte  (0=全同, 8=完全随机)",
            f"头部 32 字节: {self.data[:32].hex(' ')}",
            f"尾部 32 字节: {self.data[-32:].hex(' ')}",
        ]
        if stem in (md5, sha1, sha256) or md5.startswith(stem) or stem.startswith(md5):
            lines.append(f"★ 文件名 = 文件内容哈希（{stem}），这是内容寻址命名，常见于 CDN/去重存储")
            self.add(HIGH, "文件层", "文件名就是文件内容的哈希",
                     f"文件名 `{stem}` 与 MD5 完全一致，说明这是按内容哈希命名/去重后的文件，"
                     f"原始文件名与拍摄信息已被剥离。")
        self.json_extra["file"] = {
            "path": self.path, "size": self.size, "md5": md5, "sha1": sha1,
            "sha256": sha256, "entropy": round(entropy(self.data), 6),
        }
        self.sec("1. 文件层：哈希与整体特征", "\n".join(lines))
        self.data_md5 = md5

    def special_boxes(self) -> List[str]:
        """C2PA 内容凭证 / DRM 头 / 私有 uuid 盒 —— 返回给容器小节用的文本行。

        这三类都藏在 box 结构里，属于「画面和声音之外夹带的东西」：
          · C2PA：内容凭证（谁拍的/是否 AI 生成/编辑链），塞在 uuid 或 jumb 盒里；
          · DRM：pssh/sinf/tenc 等，说明内容是加密的、由哪个 DRM 系统保护；
          · 私有 uuid：任何厂商自定义的 uuid 盒，认不出来也要报出来。
        """
        lines: List[str] = []
        uuid_boxes: List[Tuple[Any, bytes]] = []
        jumb_boxes: List[Any] = []
        drm_boxes: List[Any] = []
        for b in walk(self.boxes):
            if b.type == "uuid":
                uuid_boxes.append((b, self.data[b.body:b.end]))
            elif b.type in ("jumb", "jumbf"):
                jumb_boxes.append(b)
            if b.type in DRM_BOXES:
                drm_boxes.append(b)

        # ---- C2PA / 私有 uuid ----
        c2pa_hits: List[str] = []
        for b, blob in uuid_boxes:
            known = bool(b.uuid) and b.uuid == C2PA_UUID
            marks = [m.decode("ascii", "replace") for m in C2PA_MARKERS if m in blob]
            desc = (f"uuid 盒 @0x{b.start:x} uuid={b.uuid.hex() if b.uuid else '?'} "
                    f"({human(b.size)})")
            if known or marks:
                c2pa_hits.append(f"{desc} 标记={marks or ['已知 C2PA uuid']}")
            lines.append("  · " + desc + (f"  ← 疑似 C2PA 内容凭证（{'已知 uuid' if known else '含 ' + '/'.join(marks) + ' 标记'}）"
                                          if (known or marks) else "  ← 私有/厂商自定义盒，认不出来也要留意"))
            self.save_artifact(f"uuid_box_{b.start:#x}.bin", blob[:65536])
        for b in jumb_boxes:
            blob = self.data[b.body:b.end]
            marks = [m.decode("ascii", "replace") for m in C2PA_MARKERS if m in blob]
            c2pa_hits.append(f"jumb 盒 @0x{b.start:x} （{human(b.size)}）标记={marks or ['无']}")
            lines.append(f"  · jumb/JUMBF 盒 @0x{b.start:x} （{human(b.size)}）"
                         "  ← JUMBF 是 C2PA manifest 的标准容器")

        # 兜底：非 mdat 区域里直接出现 c2pa/jumb 字样（盒子没被解析到也能发现）
        for b in self.boxes:
            if b.type in ("mdat", "moof"):
                continue
            blob = self.data[b.start:b.end]
            for m in (b"c2pa", b"jumb"):
                off = blob.find(m)
                if off >= 0:
                    c2pa_hits.append(f"{b.type} 内 @0x{b.start + off:x} 出现字样 {m.decode()!r}")

        if c2pa_hits:
            self.add(HIGH, "内容凭证", "发现 C2PA / JUMBF 内容凭证数据",
                     "内容凭证记录的是「谁生成/编辑的、是否 AI 生成、编辑链」这类来源信息，\n"
                     "它和平台水印不同：它是**可验证的签名声明**，通常带证书与哈希清单。\n"
                     + "\n".join(c2pa_hits[:6]))
        elif uuid_boxes:
            self.add(MED, "容器层", f"存在 {len(uuid_boxes)} 个私有 uuid 盒（非 C2PA）",
                     "uuid 盒是厂商自定义扩展的通用容器，可能夹带任何东西；已导出原始字节供人工查看。")

        # ---- DRM ----
        drm_systems: List[str] = []
        for b in drm_boxes:
            blob = self.data[b.body:b.end]
            for sid, name in DRM_SYSTEMS.items():
                if sid in blob and name not in drm_systems:
                    drm_systems.append(name)
            if b.type == "pssh" and len(blob) >= 16:
                # pssh 负载：version/flags(4) + system_id(16)
                sid = blob[4:20] if len(blob) >= 20 else blob[:16]
                nm = DRM_SYSTEMS.get(sid)
                if nm:
                    drm_systems.append(nm)
        # moov 里出现了 DRM 盒名但没被解析到（例如嵌在 stsd 的样本条目里）
        for b in self.boxes:
            if b.type != "moov":
                continue
            blob = self.data[b.start:b.end]
            for nm in DRM_BOXES:
                if blob.find(nm.encode()) >= 0 and nm not in [x.type for x in drm_boxes]:
                    drm_boxes.append(Box(nm, b.start + blob.find(nm.encode()), 8, 0, b"", [], f"{b.path}/{nm}", ""))
        for b in drm_boxes:
            lines.append(f"  · DRM 盒 {b.type} @0x{b.start:x}"
                         + (f"（{human(b.size)}）" if b.size else ""))
        if drm_boxes:
            kinds = sorted({x.type for x in drm_boxes})
            self.add(HIGH, "DRM", f"发现 DRM / 加密相关盒：{', '.join(kinds)}",
                     ("识别到的保护系统: " + "、".join(sorted(set(drm_systems))) + "。\n"
                      if drm_systems else "")
                     + "pssh 是保护系统头；sinf/tenc/senc 说明样本本身是加密的（cenc/cbcs）。\n"
                     + "注意：这类文件的内容是**加密**的，普通播放器不放行是正常的；"
                       "去掉这些盒并不会解密，只会让文件彻底放不出来。")
        self.json_extra["special_boxes"] = {
            "uuid": [{"offset": b.start, "uuid": b.uuid.hex() if b.uuid else "",
                      "size": b.size} for b, _ in uuid_boxes],
            "jumb": [b.start for b in jumb_boxes],
            "drm": [{"offset": b.start, "type": b.type} for b in drm_boxes],
            "c2pa": c2pa_hits,
            "drm_systems": sorted(set(drm_systems)),
        }
        return lines

    def layer_container(self) -> None:
        self.boxes = parse_boxes(self.data, 0, self.size, "", 0, [], self.anomalies)
        tree = box_tree_text(self.boxes)
        covered = sum(b.size for b in self.boxes)
        lines = [f"顶层盒个数: {len(self.boxes)}，盒子覆盖 {covered} / {self.size} bytes"]
        if covered != self.size:
            lines.append(f"⚠ 未被顶层盒覆盖的字节: {self.size - covered} bytes")
        lines += ["", "--- box 树（前 400 行）---"] + tree

        top_types = Counter(b.type for b in self.boxes)
        dup = {k: v for k, v in top_types.items() if v > 1 and k in ("moov", "mdat", "ftyp")}
        if dup:
            self.add(MED, "容器层", "出现重复的关键顶层盒",
                     f"{dup} —— 一个正常文件通常只有一个 moov / 一个 mdat。多出来的那个可能是附加负载的容器。")
        unknown_top = [b for b in self.boxes if b.type not in KNOWN_TOP]
        if unknown_top:
            self.add(MED, "容器层", "存在未知/私有顶层盒",
                     ", ".join(f"{b.type}@{b.start:#x}({human(b.size)})" for b in unknown_top[:10]))
            for b in unknown_top[:5]:
                blob = self.data[b.body:b.end]
                self.save_artifact(f"unknown_top_{b.type.strip()}_{b.start:#x}.bin", blob)

        # 非标准 box（ilst 里的数字索引键是 mdta 元数据的正常形态，排除）
        weird = []
        for b in walk(self.boxes):
            if b.type in CONTAINER_BOXES or b.type.isprintable():
                continue
            if "/ilst/" in b.path or b.path.endswith("/ilst"):
                continue
            if b.type[:3] == "\x00\x00\x00":
                continue
            weird.append(b)
        if weird:
            self.add(MED, "容器层", "包含非 ASCII 名称的盒",
                     ", ".join(f"{b.type!r}@{b.start:#x}" for b in weird[:10]))

        if self.anomalies:
            self.add(HIGH if len(self.anomalies) > 2 else MED, "容器层",
                     f"box 结构异常 {len(self.anomalies)} 处", "\n".join(self.anomalies[:25]))
        special = self.special_boxes()
        lines.append("\n--- 内容凭证 / DRM / 私有 uuid 盒 ---")
        lines += (special or ["无"])
        lines.append("\n--- 结构异常 ---")
        lines += (["无"] if not self.anomalies else self.anomalies[:40])
        self.sec("2. 容器层：box/atom 结构树", "\n".join(lines))
        self.json_extra["top_boxes"] = [
            {"type": b.type, "offset": b.start, "size": b.size,
             **({"uuid": b.uuid.hex()} if b.uuid else {})} for b in self.boxes
        ]

        # free / skip 内容
        self.layer_padding()

    def layer_padding(self) -> None:
        rows = []
        for b in walk(self.boxes):
            if b.type in ("free", "skip", "wide"):
                blob = self.data[b.body:b.end]
                ent = entropy(blob)
                nz = nonzero_ratio(blob)
                rows.append((b, blob, ent, nz))
        if not rows:
            self.sec("3. 填充区（free/skip/wide）", "没有 free/skip/wide 盒。")
            return
        lines, suspicious = [], []
        for b, blob, ent, nz in rows:
            lines.append(f"{b.type} @0x{b.start:08x} size={b.size} ({human(b.size)}) "
                         f"熵={ent:.3f} 非零字节={nz * 100:.2f}%")
            if b.size > 64 and nz > 0.02:
                suspicious.append(b)
        if suspicious:
            for b in suspicious:
                blob = self.data[b.body:b.end]
                p = self.save_artifact(f"padding_{b.type}_{b.start:#x}.bin", blob)
                self.add(HIGH, "隐藏通道", f"{b.type} 填充盒里有非零数据",
                         f"@{b.start:#x} 共 {b.size} 字节，非零占比 "
                         f"{nonzero_ratio(blob) * 100:.1f}%，熵 {entropy(blob):.3f}。"
                         f"填充区本应全是 0x00。证据已存: {p}")
        else:
            self.add(INFO, "隐藏通道", "填充盒内容正常",
                     f"共 {len(rows)} 个 free/skip 盒，均以 0x00 填充，未见夹带。")
        self.sec("3. 填充区（free/skip/wide）", "\n".join(lines))

    def layer_coverage(self) -> None:
        top_end = max((b.end for b in self.boxes), default=0)
        trailing = self.data[top_end:]
        mdats = [b for b in self.boxes if b.type == "mdat"]
        lines = []

        if trailing:
            ent = entropy(trailing)
            p = self.save_artifact("trailing_after_last_box.bin", trailing)
            lines.append(f"最后一个顶层盒在 0x{top_end:x}，其后面还有 {len(trailing)} bytes（熵 {ent:.3f}）")
            self.add(HIGH, "隐藏通道", "文件末尾有附加数据（tail payload）",
                     f"最后一个 box 之后多出 {len(trailing)} bytes（熵 {ent:.3f}）。"
                     f"视频播放器会忽略它，是藏数据的经典位置。证据: {p}")
            sigs = scan_magics(trailing)
            if sigs:
                lines.append("  尾部识别到的文件签名: " + ", ".join(f"{n}@{o}" for o, n, _ in sigs))
        else:
            lines.append("最后一个顶层盒正好到文件结尾，无尾部附加数据。")

        # ---- 每个 track 的 sample 定位
        moov = [b for b in self.boxes if b.type == "moov"]
        all_samples: List[Any] = []          # 每个轨道一段 (n,2) 数组，避免拼 50 万个元组
        bad_ranges: List[Any] = []
        if moov:
            traks = [b for b in walk(moov) if b.type == "trak"]
            lines.append(f"\ntrak 数: {len(traks)}")
            for ti, trak in enumerate(traks):
                stbl = [b for b in walk([trak]) if b.type == "stbl"]
                if not stbl:
                    continue
                info = read_stbl(self.data, [b for b in walk(stbl) if b.type in
                                             ("stsz", "stz2", "stsc", "stco", "co64")])
                samples = info.get("samples", [])
                hdlr = [b for b in walk([trak]) if b.type == "hdlr"]
                htype = ""
                if hdlr:
                    htype = self.data[hdlr[0].body + 8:hdlr[0].body + 12].decode("latin-1")
                lines.append(
                    f"  trak#{ti} handler={htype!r}: {len(samples)} samples ({info.get('total_bytes', 0)} bytes)"
                    + (f", 警告:{info['error']}" if 'error' in info else "")
                    + (f", 无法定位的 sample:{info['unreachable_samples']}"
                       if 'unreachable_samples' in info else "")
                )
                offs, szs = sample_arrays(info)
                if len(offs):
                    ends = offs + szs
                    ok = (szs > 0) & (offs >= 0) & (ends <= self.size)
                    if ok.any():
                        all_samples.append(np.column_stack([offs[ok], ends[ok]]))
                    if (~ok).any():
                        _o, _s = offs[~ok], szs[~ok]
                        bad_ranges.append(np.column_stack([_o, _o + np.maximum(0, _s)]))
        # 所有轨道 sample 的**并集**才算“被引用”
        self.media_ranges = merge_intervals(
            np.concatenate(all_samples) if all_samples else [])
        self.struct_segments = complement(self.media_ranges, 0, self.size)
        media_bytes = sum(b - a for a, b in self.media_ranges)

        if mdats:
            lines.append("\nmdat 盒: " + ", ".join(f"@{m.body:#x}..{m.end:#x} ({human(m.size)})" for m in mdats))
            lines.append(f"sample 并集覆盖 {media_bytes} bytes（{media_bytes * 100.0 / max(1, self.size):.2f}% 的整个文件）")
            raw_gaps: List[Tuple[int, int]] = []
            for md in mdats:
                raw_gaps += gaps_in(self.media_ranges, md.body, md.end)
            merged = [(a, b) for a, b in merge_intervals(raw_gaps) if b - a >= 16]
            total_gap = sum(b - a for a, b in merged)
            lines.append(f"mdat 内未被任何 sample 覆盖的缝隙: {len(merged)} 段 / {total_gap} bytes（≥16B）")
            shown = 0
            for a, b in merged:
                blob = self.data[a:b]
                lines.append(f"  缝隙 @0x{a:08x}..0x{b:08x}  {b - a} bytes  熵={entropy(blob):.3f}  "
                             f"非零={nonzero_ratio(blob) * 100:.1f}%")
                if shown < 8 and b - a >= 64:
                    self.save_artifact(f"mdat_gap_{a:#x}_{b - a}.bin", blob)
                    shown += 1
            if total_gap >= 256:
                self.add(HIGH, "隐藏通道", "mdat 内部存在未被引用的数据（缝隙）",
                         f"共 {total_gap} bytes 落在 mdat 里，却不属于任何音视频 sample。"
                         f"这些字节不会被解码播放，是夹带数据最常用的位置。逐段熵值见报告。")
            elif total_gap > 0:
                self.add(LOW, "隐藏通道", "mdat 内有少量未引用字节",
                         f"{total_gap} bytes，通常是编码器对齐/填充。逐段已列出。")
            else:
                self.add(INFO, "隐藏通道", "mdat 被 sample 100% 覆盖",
                         "每一字节都属于某个音视频 sample，没有夹带空间。")
        if bad_ranges:
            _br = np.concatenate(bad_ranges)
            self.add(HIGH, "隐藏通道", "存在越界/非法的 sample 偏移",
                     "、".join(f"0x{int(a):x}+{int(b) - int(a)}" for a, b in _br[:10]))
        outside = [r for r in self.media_ranges
                   if not any(m.body <= r[0] and r[1] <= m.end for m in mdats)]
        if outside and mdats:
            self.add(HIGH, "隐藏通道", "有 sample 指向 mdat 之外",
                     "、".join(f"0x{a:x}..0x{b:x}" for a, b in outside[:10])
                     + " —— 正常文件里所有媒体数据都应在 mdat 内。")
        lines.append(f"\n结构性字节（不属于任何 sample 的部分）: "
                     f"{self.size - media_bytes} bytes，已用于元数据/字符串/签名分析。")
        self.sec("4. 数据覆盖：尾部附加 & mdat 缝隙", "\n".join(lines) if lines else "无 moov / mdat 信息。")
        if trailing:
            self.json_extra["trailing_bytes"] = len(trailing)
        self.json_extra["media_bytes"] = media_bytes
        self.json_extra["mdat_gap_bytes"] = sum(b - a for a, b in
                                                merge_intervals([g for md in mdats
                                                                 for g in gaps_in(self.media_ranges, md.body, md.end)]))

    def struct_blob(self) -> Tuple[bytes, List[Tuple[int, int]]]:
        segs = self.struct_segments or [(0, self.size)]
        parts = []
        for a, b in segs:
            parts.append(self.data[a:b])
        return b"".join(parts), segs

    def struct_find(self, needle: bytes) -> int:
        for a, b in (self.struct_segments or [(0, self.size)]):
            i = self.data.find(needle, a, b)
            if i >= 0:
                return i
        return -1

    def layer_metadata(self) -> None:
        entries: List[Tuple[str, str]] = []
        images: List[Tuple[str, int, int]] = []      # (键, 图像类型, 字节数)

        def ilst_walk(boxes: Sequence[Box], prefix: str = "") -> None:
            for b in boxes:
                if b.type == "ilst":
                    for item in b.children:
                        key = f"{prefix}{item.type}"
                        for d in item.children:
                            if d.type == "data":
                                p = d.body + 4                    # version/flags
                                dtype = struct.unpack_from(">I", self.data, p)[0]
                                p += 4
                                payload = self.data[p:d.end]
                                base = dtype & 0xFFFFFF
                                # 有些封装（实测 ffmpeg）写 covr 时 data type 给 0，
                                # 所以除了类型号，还要按「键名 + 图像魔数」兜底认。
                                if base in (12, 13, 14, 27) or _looks_like_image(key, payload):
                                    images.append((key, base if base in (12, 13, 14, 27) else _img_kind(payload),
                                                   len(payload)))
                                entries.append((key, decode_ilst(dtype, payload)))
                            elif d.type == "mean":
                                entries.append((key + ".mean", self.data[d.body + 8:d.end].decode("latin-1", "replace")))
                            elif d.type == "name":
                                entries.append((key + ".name", self.data[d.body + 8:d.end].decode("latin-1", "replace")))
                elif b.children:
                    ilst_walk(b.children, prefix)

        ilst_walk(self.boxes)

        # hdlr 名称
        handler_names = []
        for b in walk(self.boxes):
            if b.type == "hdlr":
                htype = self.data[b.body + 8:b.body + 12].decode("latin-1")
                nstart = b.body + 24
                nm = self.data[nstart:b.end].split(b"\x00")[0].decode("utf-8", "replace")
                handler_names.append((htype, nm))
                entries.append((f"hdlr({b.path})", f"type={htype} name={nm}"))

        # ffprobe 全局标签
        try:
            probe = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                                    "-show_chapters", "-of", "json", self.path], binary=False))
        except Exception as exc:
            probe = {}
            eprint(f"  ! ffprobe 失败: {exc}")
        self.probe = probe
        for k, v in (probe.get("format", {}).get("tags") or {}).items():
            entries.append((f"format.{k}", str(v)))
        for st in probe.get("streams", []):
            for k, v in (st.get("tags") or {}).items():
                entries.append((f"stream{st['index']}.{k}", str(v)))

        lines = [f"{k:<44} = {v}" for k, v in entries]
        self.sec("5. 元数据：全部键值", "\n".join(lines) if lines else "（没有任何元数据标签）")
        self.json_extra["metadata"] = [{"key": k, "value": v} for k, v in entries]
        if images:
            _imgname = {12: "gif", 13: "jpeg", 14: "png", 27: "bmp"}
            detail = "、".join(f"{k}({_imgname.get(t, str(t))}, {n}B)" for k, t, n in images[:8])
            self.add(MED, "元数据/标记", f"内嵌图片 {len(images)} 张（封面图 / 缩略图）",
                     detail + "。这类图跟着文件一起分发，是「这条内容出自谁」的线索之一，"
                              "和视频画面无关，删掉不影响播放。")
            self.json_extra["embedded_images"] = [
                {"key": k, "type": _imgname.get(t, str(t)), "bytes": n} for k, t, n in images]

        # 对元数据做情报筛选
        blob = ("\n".join(f"{k}={v}" for k, v in entries)).encode("utf-8", "replace")
        hits = fingerprint_scan(blob)
        if hits:
            for name, sev, samples in hits:
                self.add(sev, "元数据/标记", f"元数据里发现 {name}",
                         "样例: " + " | ".join(samples[:5]))
        # 高熵/长十六进制/Base64 的值
        for k, v in entries:
            vb = v.encode("utf-8", "replace")
            if len(v) >= 24 and HEX_RE.match(vb) and len(set(v.lower())) > 8:
                self.add(MED, "元数据/标记", f"元数据 `{k}` 是长十六进制串",
                         f"{v[:120]}{'...' if len(v) > 120 else ''}（长度 {len(v)}，可能是哈希/ID/密钥）")
            elif len(v) >= 32 and BASE64_RE.match(vb) and re.search(r"[+/=_\-]", v):
                self.add(MED, "元数据/标记", f"元数据 `{k}` 像 Base64 数据",
                         f"{v[:120]}{'...' if len(v) > 120 else ''}（长度 {len(v)}，解码后约 {len(v) * 3 // 4} 字节）")
        for k, v in entries:
            m = re.search(r"(?i)\bvid\s*:\s*([0-9a-z_\-]{6,})", v)
            if m:
                self.add(HIGH, "元数据/标记", "发现平台视频 ID（Douyin/TikTok vid）",
                         f"`{k}` = `{v}`。`vid:` 后面是字节跳动系（抖音/剪映/TikTok）的**视频唯一 ID**，"
                         f"形如 v0300fg10000dajpti7og65jr7nm1mog。它能直接用于回源定位原始作品，"
                         f"是这条视频的来源指纹，也是最能定位“这是什么视频”的标记。")

    def layer_streams(self) -> None:
        probe = getattr(self, "probe", {}) or {}
        streams = probe.get("streams", [])
        lines = []
        for st in streams:
            lines.append(
                f"[{st['index']}] {st['codec_type']:<6} {st.get('codec_name'):<8} "
                f"tag={st.get('codec_tag_string')} id={st.get('id')} "
                f"{st.get('width', '')}x{st.get('height', '')} "
                f"dur={st.get('duration')} br={st.get('bit_rate')} frames={st.get('nb_frames')}"
            )
            lines.append(f"      pix_fmt={st.get('pix_fmt')} profile={st.get('profile')} level={st.get('level')}")
            lines.append(f"      disposition={ {k: v for k, v in (st.get('disposition') or {}).items() if v} }")
        fmtd = probe.get("format", {})
        lines.append(f"\n容器: {fmtd.get('format_long_name')}  时长={fmtd.get('duration')}s  "
                     f"总码率={fmtd.get('bit_rate')}  流数={fmtd.get('nb_streams')}")
        lines.append(f"chapters: {len(probe.get('chapters', []) or [])} 个")
        self.sec("6. 流清单（ffprobe）", "\n".join(lines))

        kinds = Counter(st["codec_type"] for st in streams)
        extra = [st for st in streams if st["codec_type"] not in ("video", "audio")]
        if extra:
            self.add(HIGH, "流层", f"存在非音视频数据流 {len(extra)} 条",
                     "\n".join(f"[{s['index']}] {s['codec_type']}/{s.get('codec_name')} tag={s.get('codec_tag_string')}"
                               for s in extra))
        if kinds.get("video", 0) > 1:
            self.add(HIGH, "流层", "存在多条视频流", f"{kinds['video']} 条——可能一条是诱饵/预览，另一条是真内容。")
        if kinds.get("audio", 0) > 1:
            self.add(MED, "流层", "存在多条音频流", f"{kinds['audio']} 条。")
        for s in streams:
            for flag in ("attached_pic", "still_image", "timed_thumbnails", "captions", "descriptions"):
                if (s.get("disposition") or {}).get(flag):
                    if flag in ("attached_pic", "still_image", "timed_thumbnails"):
                        self.add(MED, "流层", f"流 [{s['index']}] 是封面图/缩略图（disposition.{flag}）",
                                 f"codec={s.get('codec_name')}，这条流不是正片画面，"
                                 "而是随文件携带的封面图或缩略图。")
                    else:
                        self.add(MED, "流层", f"流 [{s['index']}] 带 disposition.{flag}",
                                 f"codec={s.get('codec_name')}，这条流承载的是附加数据而不是正常画面/声音。")
        if probe.get("chapters"):
            self.add(LOW, "流层", "含章节信息", f"{len(probe['chapters'])} 个，章节标题里可能带品牌/ID。")

    def layer_bitstream(self) -> None:
        vidx = None
        for st in (getattr(self, "probe", {}) or {}).get("streams", []):
            if st["codec_type"] == "video":
                vidx = st["index"]
                codec = st.get("codec_name")
                break
        if vidx is None:
            self.sec("7. 码流层：NAL / SEI", "没有视频流。")
            return

        # codec private
        cp_lines = []
        for b in walk(self.boxes):
            if b.type in ("avcC", "hvcC", "esds", "dOps", "dfLa", "vpcC", "av1C", "dac3", "dec3"):
                blob = self.data[b.body:b.end]
                cp_lines.append(f"{b.type} @0x{b.start:08x} size={len(blob)}\n{hexdump(blob, b.body, 256)}")
        self.sec("7a. 码流层：codec private / extradata", "\n\n".join(cp_lines) if cp_lines else "未找到。")

        avcc = [b for b in walk(self.boxes) if b.type == "avcC"]
        if not avcc:
            self.add(INFO, "码流层", "非 AVC 或未找到 avcC", f"codec={codec}，跳过 NAL/SEI 深扫。")
            return
        cfg = self.data[avcc[0].body:avcc[0].end]
        if len(cfg) < 7:
            return
        length_size = (cfg[4] & 0x03) + 1
        num_sps = cfg[5] & 0x1F
        p = 6
        sps_list = []
        for _ in range(num_sps):
            ln = struct.unpack_from(">H", cfg, p)[0]
            sps_list.append(cfg[p + 2:p + 2 + ln])
            p += 2 + ln
        pps_list = []
        if p < len(cfg):
            npps = cfg[p]
            p += 1
            for _ in range(npps):
                ln = struct.unpack_from(">H", cfg, p)[0]
                pps_list.append(cfg[p + 2:p + 2 + ln])
                p += 2 + ln

        sei_records: List[Dict[str, Any]] = []
        nal_counter: Counter = Counter()
        total_nals = 0

        # 找视频 trak 的 samples
        samples: List[Tuple[int, int]] = []
        limit = 0
        for trak in [b for b in walk(self.boxes) if b.type == "trak"]:
            hdlrs = [b for b in walk([trak]) if b.type == "hdlr"]
            if not hdlrs:
                continue
            htype = self.data[hdlrs[0].body + 8:hdlrs[0].body + 12].decode("latin-1")
            if htype != "vide":
                continue
            stbls = [b for b in walk([trak]) if b.type == "stbl"]
            if not stbls:
                continue
            stbl_boxes = [b for b in walk(stbls) if b.type in ("stsz", "stz2", "stsc", "stco", "co64")]
            info = read_stbl(self.data, stbl_boxes)
            _offs, _szs = sample_arrays(info)
            limit = len(_offs) if self.args.deep else min(len(_offs), 4000)
            for si, (off, sz) in enumerate(zip(_offs[:limit], _szs[:limit])):
                if sz <= 0 or off < 0 or off + sz > self.size:
                    continue
                blob = self.data[off:off + sz]
                q = 0
                while q + length_size <= len(blob):
                    nlen = int.from_bytes(blob[q:q + length_size], "big")
                    q += length_size
                    if nlen <= 0 or q + nlen > len(blob):
                        break
                    nal = blob[q:q + nlen]
                    q += nlen
                    total_nals += 1
                    nt = nal[0] & 0x1F
                    nal_counter[nt] += 1
                    if nt in (6, 39):     # SEI (AVC) / SEI prefix (HEVC)
                        rec = parse_sei(nal[1:], si, off)
                        for r in rec:
                            r["sample"] = si
                            sei_records.append(r)
            break

        lines = [
            f"codec={codec} lengthSize={length_size} SPS={len(sps_list)} PPS={len(pps_list)}",
            f"扫描 sample 数: {limit} / {len(_offs)}，NAL 总数 {total_nals}",
            "",
            "--- NAL 类型直方图（1=非IDR片 5=IDR 6=SEI 7=SPS 8=PPS 9=AUD）---",
        ]
        names = {1: "非IDR片", 2: "片A", 3: "片B", 4: "片C", 5: "IDR片", 6: "SEI",
                 7: "SPS", 8: "PPS", 9: "AUD", 10: "片尾", 11: "片尾", 12: "填充"}
        for t, c in sorted(nal_counter.items()):
            lines.append(f"  type {t:>3} ({names.get(t, '其他'):<8}) x {c}")
        lines.append("")
        if sei_records:
            by_uuid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for r in sei_records:
                by_uuid[r.get("uuid") or f"type{r['type']}"].append(r)
            lines.append(f"--- SEI 消息 {len(sei_records)} 条，分 {len(by_uuid)} 类 ---")
            for key, recs in by_uuid.items():
                r0 = recs[0]
                lines.append(f"  UUID/类型 {key}  x{len(recs)}  payloadType={r0['type']}  "
                             f"size={r0['size']}  首个在 sample#{r0['sample']} @{r0['offset']:#x}")
                if r0.get("payload_hex"):
                    lines.append(f"      hex: {r0['payload_hex'][:200]}")
                if r0.get("payload_text"):
                    lines.append(f"      text: {r0['payload_text'][:200]}")
                known = KNOWN_SEI_UUIDS.get(key)
                if known:
                    lines.append(f"      ★ {known}")
                    self.add(MED, "码流层", f"SEI 私有 UUID 命中已知指纹 {key}",
                             known + f"\n共出现 {len(recs)} 次，首个在 sample#{r0['sample']}。")
            unreg = [r for r in sei_records if r["type"] == 5]
            if unreg:
                blob = b"".join(bytes.fromhex(r["payload_hex"]) for r in unreg[:50] if r.get("payload_hex"))
                p = self.save_artifact("sei_user_data_unregistered.bin", blob)
                self.add(HIGH, "码流层", f"码流里有 SEI user_data_unregistered 消息（{len(unreg)} 条）",
                         "这是 H.264/H.265 标准允许的**任意私有数据**容器，UUID+自定义 payload，"
                         "播放器不解码也不显示，是厂商水印/暗码最常用载体。UUID 与内容见报告。证据: " + p)
            other = [r for r in sei_records if r["type"] not in (5,)]
            if other:
                self.add(MED, "码流层", f"码流里有其他 SEI 消息 {len(other)} 条",
                         "\n".join(f"payloadType={r['type']} size={r['size']} sample#{r['sample']}" for r in other[:10]))
        else:
            lines.append("未发现任何 SEI 消息。")
            self.add(INFO, "码流层", "码流里没有 SEI 私有数据", f"扫描 {total_nals} 个 NAL，没有 SEI。")

        # SPS/PPS 存证 + 与容器声明分辨率交叉校验
        lines.append("")
        sps_info: Dict[str, Any] = {}
        for i, s in enumerate(sps_list):
            lines.append(f"SPS[{i}] ({len(s)}B): {s.hex()}")
            self.save_artifact(f"sps_{i}.bin", s)
        for i, s in enumerate(pps_list):
            lines.append(f"PPS[{i}] ({len(s)}B): {s.hex()}")
        if sps_list:
            sps_info = parse_sps_dimensions(sps_list[0])
            lines.append(f"\nSPS 解析: profile_idc={sps_info.get('profile_idc')} "
                         f"level_idc={sps_info.get('level_idc')} "
                         f"chroma_format_idc={sps_info.get('chroma_format_idc')} "
                         f"编码分辨率={sps_info.get('width')}x{sps_info.get('height')} "
                         f"裁剪={sps_info.get('crop')}")
            declared = [st for st in (getattr(self, "probe", {}) or {}).get("streams", [])
                        if st.get("codec_type") == "video"]
            if declared:
                dw, dh = declared[0].get("width"), declared[0].get("height")
                lines.append(f"容器/tkhd 声明分辨率: {dw}x{dh}")
                if sps_info.get("width") and dw and (sps_info["width"] != dw or sps_info["height"] != dh):
                    self.add(MED, "码流层", "SPS 编码分辨率与容器声明不一致",
                             f"SPS 说 {sps_info['width']}x{sps_info['height']}，容器说有 {dw}x{dh}。"
                             f"正常转码两者一致；不一致通常意味着画面被裁剪/改过宽高，或文件被二次拼接过。")
                else:
                    lines.append("✔ 两者一致")
        self.sec("7b. 码流层：NAL / SEI 深扫", "\n".join(lines))

        self.json_extra["nal_histogram"] = {str(k): v for k, v in nal_counter.items()}
        self.json_extra["sei"] = sei_records[:200]
        if sps_info:
            self.json_extra["sps_hex"] = [s.hex() for s in sps_list]
            self.json_extra["frame_size_from_sps"] = sps_info

    def layer_strings(self) -> None:
        sblob, segs = self.struct_blob()
        strs = [m.group().decode("latin-1") for m in PRINT_RE.finditer(sblob)]
        uniq = sorted(set(strs))
        interesting = [s for s in uniq if is_interesting_string(s)]
        lines = [
            f"结构性字节区（整个文件里不属于任何音视频 sample 的部分）: "
            f"{len(sblob)} bytes / {len(segs)} 段",
            f"其中可打印字符串（≥7 字符）去重后 {len(uniq)} 条，其中“值得注意的” {len(interesting)} 条",
            "",
            "（只扫描结构性字节区，是因为压缩后的视频/音频数据本身就是高熵噪声，"
            "在里面找字符串必然全是误报）",
            "",
            f"--- 值得注意的字符串 ---",
        ] + (interesting[:300] if interesting else ["（无）"])
        self.sec("8. 字符串层：可打印字符串与情报", "\n".join(lines))
        self.json_extra["struct_strings"] = interesting[:300]

        hits = fingerprint_scan(sblob)
        for name, sev, samples in hits:
            self.add(sev, "字符串层", f"文件内出现 {name}", "样例: " + " | ".join(samples[:5]))
            for s in samples[:2]:
                idx = self.struct_find(s[:20].encode("utf-8", "replace"))
                if idx >= 0:
                    ctx = self.data[max(0, idx - 48):idx + 192]
                    p = self.save_artifact(f"hit_{idx:#x}.bin", ctx)
                    self.add(INFO, "字符串层", f"{name} 上下文已存证",
                             f"文件偏移 @0x{idx:x}\n{hexdump(ctx, max(0, idx - 48), 256)}\n文件: {p}")

        urls = sorted({u.decode("latin-1") for u in URL_RE.findall(sblob)})
        emails = sorted({e.decode("latin-1") for e in EMAIL_RE.findall(sblob)})
        ips = sorted({i.decode() for i in IP_RE.findall(sblob)})
        uuids = sorted({u.decode().lower() for u in UUID_RE.findall(sblob)})
        if urls:
            self.add(MED, "字符串层", f"文件里有 {len(urls)} 个 URL", "\n".join(urls[:20]))
        if emails:
            self.add(LOW, "字符串层", f"文件里有 {len(emails)} 个邮箱", ", ".join(emails[:10]))
        if ips:
            self.add(LOW, "字符串层", f"文件里有 {len(ips)} 个 IP 字面量", ", ".join(ips[:10]))
        if uuids:
            self.add(MED, "字符串层", f"文件里有 {len(uuids)} 个 UUID", ", ".join(uuids[:10]))
        self.json_extra["urls"] = urls[:50]
        self.json_extra["emails"] = emails[:50]
        self.json_extra["uuids"] = uuids[:50]

        # 附加文件签名（只在结构性字节区里找 + 二次校验）
        sigs = scan_magics(sblob)
        rows = []
        for off, nm, _ln in sigs[:80]:
            foff = offset_in_segments(off, segs)
            rows.append((off, foff, nm, validate_magic(sblob, off, nm)))
        lines = [f"在结构性字节区匹配到 {len(rows)} 处文件签名（含二次校验结果）:"]
        for off, foff, nm, ok in rows[:60]:
            lines.append(f"  @0x{off:08x} (文件内 0x{foff:08x})  {nm}  {'✔校验通过' if ok else '×不可信'}")
        self.sec("9. 附加文件签名扫描", "\n".join(lines) if sigs else "未在结构性字节区发现任何嵌入文件签名。")

        seen = set()
        for off, foff, nm, ok in rows:
            if not ok or nm in seen:
                continue
            seen.add(nm)
            blob2 = sblob[off:off + 4096]
            p = self.save_artifact(f"sig_{re.sub(r'[^A-Za-z0-9]+', '_', nm)}_{foff:#x}.bin", blob2)
            self.add(MED, "隐藏通道", f"文件内部嵌有 {nm}（校验通过）",
                     f"文件偏移 0x{foff:x}（{foff}），距 mdat 之外的结构区。上下文已存证: {p}")
        if not any(ok for _o, _f, _n, ok in rows):
            self.add(INFO, "隐藏通道", "没有发现可信的嵌入文件",
                     "结构性字节区里没有通过校验的嵌入文件签名。")

        # 深扫：媒体区里的“高置信”指纹
        if self.args.deep:
            mblob = b"".join(self.data[a:b] for a, b in (self.media_ranges or [(0, self.size)]))
            strict = [fp for fp in FINGERPRINTS[:5]]
            found = []
            for name, rx, _sev in strict:
                try:
                    ms = re.findall(rx, mblob)
                except re.error:
                    continue
                if ms:
                    found.append((name, [m.decode("utf-8", "replace")[:120] for m in ms[:5]]))
            lines = ["深度模式：在压缩码流区用高置信规则复查，" +
                     ("命中如下:" if found else "无命中。")]
            for name, samples in found:
                lines.append(f"  {name}: " + " | ".join(samples))
                self.add(LOW, "字符串层", f"压缩码流区疑似命中「{name}」",
                         " | ".join(samples) + "（出现在高熵码流里，也可能是巧合，需人工确认）")
            self.sec("9b. 深扫：压缩码流区指纹复查", "\n".join(lines))

    def save_frame_png(self, g: "np.ndarray", fname: str) -> str:
        try:
            os.makedirs(self.artdir, exist_ok=True)
            p = os.path.join(self.artdir, fname)
            if Image is not None:
                Image.fromarray(np.clip(g, 0, 255).astype(np.uint8)).save(p)
                self.artifacts.append(p)
                return p
        except Exception:
            pass
        return ""

    def layer_consistency(self) -> None:
        probe = getattr(self, "probe", {}) or {}
        lines = []
        for st in probe.get("streams", []):
            declared = st.get("nb_frames")
            actual = None
            for trak in [b for b in walk(self.boxes) if b.type == "trak"]:
                stbls = [b for b in walk([trak]) if b.type == "stbl"]
                if not stbls:
                    continue
                infos = [b for b in walk(stbls) if b.type in ("stsz", "stz2", "stsc", "stco", "co64")]
                info = read_stbl(self.data, infos)
                hdlrs = [b for b in walk([trak]) if b.type == "hdlr"]
                ht = self.data[hdlrs[0].body + 8:hdlrs[0].body + 12].decode("latin-1") if hdlrs else "?"
                isv = (ht == "vide" and st["codec_type"] == "video") or (ht == "soun" and st["codec_type"] == "audio")
                if isv:
                    actual = int(info.get("n_samples", 0) or len(info.get("samples", [])))
                    break
            lines.append(f"[{st['index']}] {st['codec_type']}: 容器声明 nb_frames={declared}, stbl 实际 sample 数={actual}"
                         + ("  ✔一致" if declared and actual and int(declared) == actual else "  ⚠不一致"))
            if declared and actual and int(declared) != actual:
                self.add(MED, "自洽性", f"流 [{st['index']}] 声明帧数与实际不符",
                         f"ffprobe 报 {declared} 帧，stbl 里只有 {actual} 个 sample。"
                         f"可能是容器被二次编辑/拼接，多余或缺失的帧值得注意。")
        # 时间戳连续性
        for trak in [b for b in walk(self.boxes) if b.type == "trak"]:
            stbls = [b for b in walk([trak]) if b.type == "stbl"]
            if not stbls:
                continue
            stts = [b for b in walk(stbls) if b.type == "stts"]
            ctts = [b for b in walk(stbls) if b.type == "ctts"]
            elst = [b for b in walk([trak]) if b.type == "elst"]
            hdlrs = [b for b in walk([trak]) if b.type == "hdlr"]
            ht = self.data[hdlrs[0].body + 8:hdlrs[0].body + 12].decode("latin-1") if hdlrs else "?"
            if stts:
                p = stts[0].body + 4
                n = struct.unpack_from(">I", self.data, p)[0]
                total = 0
                zero = 0
                for i in range(n):
                    cnt, delta = struct.unpack_from(">II", self.data, p + 4 + 8 * i)
                    total += cnt * delta
                    if delta == 0:
                        zero += cnt
                lines.append(f"trak({ht}): stts {n} 组，累计时长 {total} 单位，delta==0 的 sample: {zero}")
                if zero:
                    self.add(LOW, "自洽性", f"trak({ht}) 有 {zero} 个时长为 0 的 sample",
                             "零时长帧是“隐形帧”，某些工具用它藏数据或做时间轴标记。")
            if ctts:
                p = ctts[0].body + 4
                n = struct.unpack_from(">I", self.data, p)[0]
                vals = [struct.unpack_from(">i", self.data, p + 8 + 8 * i)[0] for i in range(min(n, 5000))]
                if vals:
                    lines.append(f"trak({ht}): ctts {n} 组，offset 范围 [{min(vals)},{max(vals)}]")
            if elst:
                p = elst[0].body + 4
                n = struct.unpack_from(">I", self.data, p)[0]
                lines.append(f"trak({ht}): edit list {n} 条（裁剪/偏移，可用于隐藏开头画面）")
                self.add(LOW, "自洽性", f"trak({ht}) 含 edit list",
                         f"{n} 条 edit。edit list 能在不删数据的情况下让播放器跳过某段画面，值得看一眼。")
        self.sec("10. 结构自洽性检查", "\n".join(lines) if lines else "无。")

    # ============================================================ 媒体分析（新）
    def _video_stream(self) -> Optional[Dict[str, Any]]:
        for s in (self.probe.get("streams") or []):
            if s.get("codec_type") == "video":
                return s
        return None

    @staticmethod
    def _fps_of(stream: Dict[str, Any]) -> float:
        for key in ("avg_frame_rate", "r_frame_rate"):
            v = str(stream.get(key) or "0/0")
            try:
                num, den = v.split("/")
                if float(num) > 0 and float(den) > 0:
                    return float(num) / float(den)
            except Exception:
                continue
        return 30.0

    def _wm_width(self, src_w: int, src_h: int, sample_fps: Optional[float] = None) -> int:
        """采样帧宽度。0=自动：尽量贴合源分辨率，同时守住内存预算。

        为什么要贴源分辨率：水印图层是从采样帧算出来的，图层精度 → 阈值化之后
        遮罩的贴合度 → removelogo 的插值距离。512 宽的分析帧放大回 720p 源，
        笔画边缘本来就是糊的，遮罩必然不准。所以默认取 min(源宽, 1080)，
        再按「总帧数 × 单帧像素」超预算就把宽度乘 0.8 往回收。
        """
        want = int(getattr(self.args, "wm_width", 0) or 0)
        if want > 0:
            return want
        ww = max(256, min(int(src_w or STREAM_WM_WIDTH), WM_AUTO_MAX_W))
        try:
            dur = float((self.probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        fps = float(self.args.fps) if sample_fps is None else float(sample_fps)
        n = max(1, int(dur * max(0.05, fps))) + 8
        while ww > 256 and ww * _F.scale_height(src_w, src_h, ww) * n > WM_AUTO_BUDGET:
            ww = int(ww * 0.8)
        return ww

    async def _analyze_frames(self) -> Any:
        """两个解码进程并发，一次拿到画面层的全部结论。

        旧版为了「逐帧异常 / 静态叠加 / 矩形码」分别解码了 3 次（还多一次 1080p），
        占了整个扫描 80% 的耗时。这里合并成：
          A. 全帧率 + 极小 RGB   → 逐帧亮度/饱和度（闪帧 vs 场景切换）
          B. 采样帧率 + 中等灰度 → 静态叠加层（水印）+ 矩形码
        """
        if self.frame_analysis is not None:
            return self.frame_analysis
        st = self._video_stream()
        if not st:
            self.frame_analysis = _D.FrameAnalysis(error="没有视频流")
            return self.frame_analysis
        src_w, src_h = int(st.get("width") or 0), int(st.get("height") or 0)
        want_luma = bool(self.args.frames)
        want_wm = bool(self.args.watermark)
        want_qr = bool(self.args.qr)
        if not (want_luma or want_wm or want_qr):
            self.frame_analysis = _D.FrameAnalysis(src_w=src_w, src_h=src_h, error="画面检查已全部关闭")
            return self.frame_analysis

        lw = STREAM_LUMA_WIDTH
        lh = _F.scale_height(src_w, src_h, lw)
        fps0 = self._fps_of(st)
        sample_fps = max(0.25, float(self.args.fps))
        # 采样帧是**全部堆在内存里**的：n × 高 × 宽。2 小时视频按 1fps 是 7200 帧，
        # 光靠降宽度救不回来，必须在源头把帧数压下来。
        # （静态叠加层检测用几百帧完全够，多出来的帧只是浪费内存。）
        try:
            _dur = float((self.probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            _dur = 0.0
        dur = _dur
        if _dur > 0 and sample_fps * _dur > WM_MAX_SAMPLES:
            sample_fps = max(0.05, WM_MAX_SAMPLES / _dur)
            self._say(f"    · 时长 {_dur / 60:.0f} 分钟：采样帧率自动降到 "
                      f"{sample_fps:.2f}fps（上限 {WM_MAX_SAMPLES} 帧，控制内存）")
        ww = self._wm_width(src_w, src_h, sample_fps)
        wh = _F.scale_height(src_w, src_h, ww)
        qr_every = max(1, int(round(sample_fps / max(0.05, float(self.args.qr_fps))))) if want_qr else 0

        async def pass_luma():
            times: List[float] = []
            lumas: List[float] = []
            sats: List[float] = []
            vf = f"scale={lw}:{lh},format=rgb24"
            async for fr in _F.stream_rawvideo(
                    self.path, vf=vf, width=lw, height=lh, pix_fmt="rgb24",
                    threads=self.args.threads, priority=self.args.priority):
                a = fr.data
                r = a[:, :, 0].astype(np.float32)
                g = a[:, :, 1].astype(np.float32)
                b = a[:, :, 2].astype(np.float32)
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                mx = np.maximum(np.maximum(r, g), b)
                mn = np.minimum(np.minimum(r, g), b)
                sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6) * 255.0, 0.0)
                times.append(fr.index / fps0)
                lumas.append(float(lum.mean()))
                sats.append(float(sat.mean()))
            return times, lumas, sats

        async def pass_sampled():
            times: List[float] = []
            hits: List[Tuple[float, List[Tuple[float, float, float]]]] = []
            scanned = 0
            # 采样帧直接**预分配**进一个大数组：原来是先存 list 再 np.stack，
            # 那一刻内存里同时有「N 个单帧」+「拼好的大数组」，峰值翻倍
            # （188 帧 × 864×1536 就已经接近 500MB）。这里只占 1 份。
            n_est = max(8, int(sample_fps * max(1.0, _dur)) + 16)
            buf = np.empty((n_est, wh, ww), np.uint8) if want_wm else None
            extra: List[Any] = []
            k = 0
            vf = f"fps={sample_fps},scale={ww}:{wh}"
            async for fr in _F.stream_rawvideo(
                    self.path, vf=vf, width=ww, height=wh, pix_fmt="gray",
                    fps=sample_fps, threads=self.args.threads, priority=self.args.priority):
                if want_wm:
                    times.append(fr.t)
                    if k < n_est:
                        buf[k] = fr.data          # 拷进预分配缓冲
                    else:
                        extra.append(fr.data)     # 估计偏小才会走到（罕见）
                    k += 1
                if qr_every and (fr.index % qr_every == 0):
                    scanned += 1
                    cs = await asyncio.to_thread(_qr_finder_candidates, fr.data)
                    if cs:
                        hits.append((fr.t, cs))
            if buf is None:
                return times, None, hits, scanned
            arr = buf[:k] if not extra else np.concatenate([buf[:k]] + extra)
            return times, arr, hits, scanned

        tasks = []
        if want_luma:
            tasks.append(asyncio.create_task(pass_luma(), name="scan:luma"))
        if want_wm or want_qr:
            tasks.append(asyncio.create_task(pass_sampled(), name="scan:sampled"))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        fa = _D.FrameAnalysis(src_w=src_w, src_h=src_h, wm_width=ww, wm_height=wh)
        errs = [r for r in results if isinstance(r, Exception)]
        if errs:
            fa.error = f"{type(errs[0]).__name__}: {errs[0]}"
        for r in results:
            if isinstance(r, Exception):
                continue
            if len(r) == 3:
                fa.times, fa.luma, fa.sat = r[0], r[1], r[2]
                fa.n_frames = len(r[0])
            else:
                fa.wm_times, arr, fa.qr_hits, fa.qr_scanned = r[0], r[1], r[2], r[3]
                if arr is not None and len(arr):
                    fa.wm_frames = arr
        self.frame_analysis = fa
        return fa

    async def _qr_scan_flash_frames(self, frame_indices: Sequence[int]) -> List[Tuple[float, List[Any]]]:
        """对「单帧插入」的那些帧定点补扫矩形码：一次 ffmpeg 进程搞定，不逐帧启动。"""
        idx = sorted(set(int(i) for i in frame_indices))[:40]
        if not idx or np is None:
            return []
        st = self._video_stream()
        if not st:
            return []
        src_w, src_h = int(st.get("width") or 0), int(st.get("height") or 0)
        ww = self._wm_width(src_w, src_h)
        wh = _F.scale_height(src_w, src_h, ww)
        sel = "+".join(f"eq(n\\,{i})" for i in idx)
        vf = f"select='{sel}',scale={ww}:{wh}"
        fps0 = self._fps_of(st)
        out: List[Tuple[float, List[Any]]] = []
        try:
            k = 0
            # fps_mode="passthrough"：select 是稀疏抽帧，rawvideo 默认 CFR 会把
            # 选中的少数帧重复补齐成整段帧率（本片 2 帧 → 5 万多帧 / 23 GB），
            # 再叠加每帧的 QR 检测就是几十分钟的「假死」。
            async for fr in _F.stream_rawvideo(
                    self.path, vf=vf, width=ww, height=wh, pix_fmt="gray",
                    threads=self.args.threads, priority=self.args.priority,
                    fps_mode="passthrough", max_frames=len(idx)):
                cs = await asyncio.to_thread(_qr_finder_candidates, fr.data)
                if cs:
                    src_i = idx[k] if k < len(idx) else fr.index
                    out.append((src_i / fps0, cs))
                    self.save_frame_png(fr.data, f"qr_on_flashframe_{src_i / fps0:07.2f}s.png")
                k += 1
        except Exception as exc:
            self._say(f"    ! 闪帧补扫失败: {exc}")
        return out

    async def layer_audio(self) -> None:
        if self.args.quick or np is None or not self.args.audio:
            self.sec("11. 音频层：频谱 / 超声水印", "（已跳过）")
            return
        streams = [s for s in (self.probe.get("streams") or []) if s.get("codec_type") == "audio"]
        if not streams:
            self.sec("11. 音频层：频谱 / 超声水印", "没有音频流。")
            return
        dur = float(self.probe.get("format", {}).get("duration") or 180.0)
        try:
            x = await _F.decode_audio_mono(self.path, seconds=min(180.0, dur),
                                           threads=self.args.threads, priority=self.args.priority)
        except Exception as exc:
            self.sec("11. 音频层：频谱 / 超声水印", f"解码失败: {exc}")
            return
        res = await asyncio.to_thread(analyze_audio_samples, x)
        if res.get("error"):
            self.sec("11. 音频层：频谱 / 超声水印", res["error"])
            return
        bands = res["bands"]
        hi = res["ultrasonic"]
        lines = ["频段能量占比（相对总量，均值谱）:"]
        for k, r in bands.items():
            db = 10 * math.log10(r + 1e-12)
            bar = "#" * int(max(0, (db + 100) / 4))
            lines.append(f"  {k:>13} Hz : {r * 100:8.4f}%   ({db:7.2f} dB)  {bar}")
        lines.append(f"\n16–22.05 kHz 超声段总占比: {hi * 100:.5f}%")
        lines.append("（正常有损压缩音频在 16kHz 以上几乎没有能量；"
                     "该段占比明显偏高通常意味着超声水印 / 隐藏载波）")
        lines.append("\n候选持续窄带单音（频率, 能量占比, 时间稳定度CV<0.5）:")
        tones = res["tones"]
        if tones:
            for t in tones:
                lines.append(f"  {t['freq']:9.1f} Hz  占比={t['ratio'] * 100:.5f}%  CV={t['cv']:.3f}")
        else:
            lines.append("  未发现稳定的窄带单音。")
        try:
            os.makedirs(self.artdir, exist_ok=True)
            p = os.path.join(self.artdir, "spectrogram.png")
            if spectrogram_png(res.get("power"), res.get("freqs"), p):
                self.artifacts.append(p)
                lines.append(f"\n频谱图已存: {p}")
        except Exception:
            pass

        if hi > 0.0008:
            self.add(HIGH, "音频层", "音频高频段能量异常偏高（疑似超声水印/暗码载波）",
                     f"16–22.05kHz 占总能量 {hi * 100:.4f}%（远高于正常有损音频的 <0.01%）。"
                     f"频段明细见报告；频谱图已导出。")
        elif hi > 0.0001:
            self.add(LOW, "音频层", "音频高频段能量略高",
                     f"16–22.05kHz 占比 {hi * 100:.5f}%，可人工复核频谱图。")
        else:
            self.add(INFO, "音频层", "音频频谱正常",
                     f"16–22.05kHz 占比仅 {hi * 100:.6f}%，未发现超声载波。")
        if tones:
            self.add(MED, "音频层", f"存在 {len(tones)} 个持续窄带单音",
                     "、".join(f"{t['freq']:.0f}Hz(占比{t['ratio'] * 100:.4f}%)" for t in tones[:6])
                     + "。持续单音可能是音频水印或次声/超声信标，也可能是配乐本身的乐器长音。")
        self.sec("11. 音频层：频谱 / 超声水印", "\n".join(lines))
        self.json_extra["audio_bands"] = {k: round(v, 8) for k, v in bands.items()}
        self.json_extra["audio_ultrasonic_ratio"] = hi
        self.json_extra["audio_tones"] = tones

    async def layer_overlay(self) -> None:
        if self.args.quick or np is None or not self.args.watermark:
            self.sec("12. 画面层：静态叠加（水印/台标）", "（已跳过）")
            return
        fa = await self._analyze_frames()
        if fa.wm_frames is None or fa.wm_frames.shape[0] < 8:
            self.sec("12. 画面层：静态叠加（水印/台标）",
                     f"采样帧不足（{0 if fa.wm_frames is None else fa.wm_frames.shape[0]} 帧）{fa.error}")
            return
        det = await asyncio.to_thread(overlay_regions_from_frames,
                                      fa.wm_frames, fa.src_w, fa.src_h)
        src_regions = det["regions"]
        self.watermark_regions = src_regions
        lines = [
            f"采样帧数: {fa.wm_frames.shape[0]}，分析分辨率 {fa.wm_width}x{fa.wm_height}，"
            f"源视频 {fa.src_w}x{fa.src_h}",
            f"时间稳定像素（MAD<{det['thr']:.2f}）占比: {det['static'].mean() * 100:.2f}%",
            f"其中“稳定且有结构”的像素占比: {det['struct'].mean() * 100:.2f}%",
            "",
            f"候选静态叠加区域 {len(det['boxes'])} 个（分析分辨率坐标）:",
        ]
        for (x0, y0, x1, y1, area, fill, gstd) in det["boxes"]:
            lines.append(f"  x[{x0:>4},{x1:>4}] y[{y0:>4},{y1:>4}]  尺寸 {x1 - x0}x{y1 - y0} "
                         f"填充率={fill:.2f} 平均梯度={gstd:.2f}")
        if not src_regions:
            self.add(INFO, "画面层", "未发现明显的静态叠加水印",
                     f"稳定且有结构的像素只占 {det['struct'].mean() * 100:.2f}%，且没有形成规则区域。")
            self.sec("12. 画面层：静态叠加（水印/台标）", "\n".join(lines))
            return

        self.add(HIGH, "画面层", f"检测到 {len(src_regions)} 处画面内静态叠加层（疑似水印/台标/烧录文字）",
                 "这些区域在所有采样帧中几乎不变、但含有明显的边缘结构，符合 logo/文字水印特征；"
                 "纯背景与黑边已被排除。源视频像素坐标: "
                 + "、".join(f"({r['x']},{r['y']},{r['w']}x{r['h']})" for r in src_regions[:4])
                 + "。图层与放大图已存 artifacts/，修复工具可直接用这些坐标。")
        lines.append("")
        for i, r in enumerate(src_regions):
            lines.append(f"★ 区域 {i + 1}（源视频像素）x[{r['x']},{r['x'] + r['w']}] "
                         f"y[{r['y']},{r['y'] + r['h']}] 尺寸 {r['w']}x{r['h']} "
                         f"填充率={r['fill']} 平均梯度={r['grad']}")

        # 图层提取：优先用「梯度幅值的时间中位数」（静态叠加层的笔画最清楚）。
        # --hires 时额外解一遍高分辨率，代价是再来一次解码。
        src = None
        method = "梯度幅值的时间中位数（区域裁剪）"
        if self.args.hires:
            try:
                gh, _h = await asyncio.to_thread(decode_gray, self.path, 0.25, 1080)
                if gh.shape[0] >= 3:
                    gs = []
                    for f in gh:
                        gyy, gxx = np.gradient(f.astype(np.float32))
                        gs.append(np.hypot(gxx, gyy))
                    src = np.median(np.stack(gs), axis=0)
                    method = "梯度幅值的时间中位数（--hires 1080p）"
            except Exception:
                src = None
        # 图层来源：默认用采样帧的「区域裁剪梯度中位数」；--hires 时额外解一遍 1080p。
        items: List[Tuple[int, Any, int, int]] = []   # (源区域下标, 图层数组, 标注x, 标注y)
        method = "梯度幅值的时间中位数（区域裁剪）"
        if self.args.hires:
            try:
                gh, _h = await asyncio.to_thread(decode_gray, self.path, 0.25, 1080)
                if gh.shape[0] >= 3:
                    gs = []
                    for f in gh:
                        gyy, gxx = np.gradient(f.astype(np.float32))
                        gs.append(np.hypot(gxx, gyy))
                    hires_med = np.median(np.stack(gs), axis=0)
                    method = "梯度幅值的时间中位数（--hires 1080p）"
                    for ri, r in enumerate(src_regions[:3]):
                        y0 = max(0, r["y"] - 8); y1 = min(hires_med.shape[0], r["y"] + r["h"] + 8)
                        x0 = max(0, r["x"] - 8); x1 = min(hires_med.shape[1], r["x"] + r["w"] + 8)
                        items.append((ri, hires_med[y0:y1, x0:x1], x0, y0))
            except Exception as exc:
                self._say(f"    ! --hires 图层提取失败，回退到采样分辨率: {exc}")
        if not items and fa.wm_frames is not None and fa.wm_width:
            ds = int(det.get("downscale", 1) or 1)
            sx = fa.src_w / float(fa.wm_frames.shape[2]) if fa.wm_frames.shape[2] else ds
            sy = fa.src_h / float(fa.wm_frames.shape[1]) if fa.wm_frames.shape[1] else ds
            for ri, r in enumerate(src_regions[:3]):
                ax0, ay0, ax1, ay1 = (r.get("analysis_box") or [0, 0, 0, 0])
                py0 = max(0, ay0 * ds - 6 * ds); py1 = min(fa.wm_frames.shape[1], (ay1 + 1) * ds + 6 * ds)
                px0 = max(0, ax0 * ds - 6 * ds); px1 = min(fa.wm_frames.shape[2], (ax1 + 1) * ds + 6 * ds)
                try:
                    g = await asyncio.to_thread(gradient_median_crop,
                                                fa.wm_frames, py0, py1, px0, px1)
                except Exception as exc:
                    # 某个区域失败不能拖累其它区域，而且必须在报告里说出来 ——
                    # 否则只会表现为「界面上这个区域没有水印图」，没人知道为什么。
                    self._say(f"    ! 区域 {ri + 1} 图层提取失败，已跳过: "
                              f"{type(exc).__name__}: {exc}")
                    self.add(LOW, "画面层", f"区域 {ri + 1} 的水印图层提取失败",
                             f"{type(exc).__name__}: {exc}。该区域仍可正常用来去水印，"
                             "只是在图层预览里显示不出来。")
                    g = None
                if g is not None:
                    items.append((ri, g, int(px0 * sx), int(py0 * sy)))
        # 先清掉上一次扫描留下的 watermark_region*，否则新旧文件混在一起，
        # 按文件名找图层时会取到陈旧的（实测就踩过：新旧区域不同 → 图层张冠李戴）。
        try:
            import glob as _glob
            for old in _glob.glob(os.path.join(self.artdir, "watermark_region*.png")) + \
                       _glob.glob(os.path.join(self.artdir, "watermark_region*.json")):
                try:
                    os.remove(old)
                except OSError:
                    pass
        except Exception:
            pass
        self.json_extra["watermark_layer_files"] = ["" for _ in src_regions]

        lines.append(f"\n--- 候选区域图层（{method}）ASCII 渲染，可直接肉眼辨认 ---")
        lines.append("说明：只有“每一帧都盖在上面”的内容才会在这里显示出来，运动的画面已被时间维抹平。")
        for ri, crop, lx, ly in items:
            if crop is None or crop.size == 0:
                continue
            lines.append(f"\n# 区域 {ri + 1}: 源视频像素 x[{lx},{lx + crop.shape[1]}] "
                         f"y[{ly},{ly + crop.shape[0]}]，平均梯度={src_regions[ri]['grad']:.2f}")
            lo, hi = float(np.percentile(crop, 2)), float(np.percentile(crop, 98))
            norm = np.clip((crop - lo) * 255.0 / max(1.0, hi - lo), 0, 255)
            lines.append(ascii_art(norm, cols=min(190, max(60, crop.shape[1] // 2))))
            if Image is not None:
                try:
                    os.makedirs(self.artdir, exist_ok=True)
                    p = os.path.join(self.artdir, f"watermark_region{ri + 1}_{lx}x{ly}.png")
                    im = Image.fromarray(norm.astype(np.uint8))
                    im.resize((im.width * 3, im.height * 3), Image.LANCZOS).save(p)
                    self.artifacts.append(p)
                    try:
                        self.json_extra["watermark_layer_files"][ri] = os.path.basename(p)
                    except IndexError:
                        pass
                    lines.append(f"（放大 3 倍已存: {p}）")
                    # 记下这张图层在「源视频像素坐标系」里的位置和覆盖范围，
                    # 供 repair 的 --wm mask 精确摆放笔画遮罩（不然只能靠猜 padding）。
                    try:
                        cw_src = int(round(crop.shape[1] * sx))
                        ch_src = int(round(crop.shape[0] * sy))
                        with open(os.path.splitext(p)[0] + ".json", "w", encoding="utf-8") as jf:
                            json.dump({"x": int(lx), "y": int(ly),
                                       "w": cw_src, "h": ch_src,
                                       "frame_w": int(fa.src_w), "frame_h": int(fa.src_h),
                                       "scale": 3}, jf)
                    except Exception as exc:
                        self._say(f"    ! 区域 {ri + 1} 图层 sidecar 写入失败: {exc}")
                except Exception as exc:
                    # 以前这里是 `except Exception: pass` —— 保存失败会**彻底静默**，
                    # 结果就是界面上这个区域没有水印图，却查不出任何原因。
                    self._say(f"    ! 区域 {ri + 1} 图层保存失败: {type(exc).__name__}: {exc}")
                    self.add(LOW, "画面层", f"区域 {ri + 1} 的水印图层保存失败",
                             f"{type(exc).__name__}: {exc}")
            txt = await asyncio.to_thread(ocr_image, norm)
            if txt:
                lines.append(f"OCR 原始输出（仅供人工参考）: {txt[:400]}")
                cjk = "chi_sim" in ocr_available_langs()
                self.add(MED if cjk else INFO, "画面层",
                         "水印图层 OCR 结果（需人工复核）" if cjk else
                         "水印图层 OCR 结果（本机无中文语言包，中文水印识别不可信）",
                         f"区域 {ri + 1} 识别到: {txt[:300]}")
        self.sec("12. 画面层：静态叠加（水印/台标）", "\n".join(lines))

    async def layer_frames(self) -> None:
        if self.args.quick or not self.args.frames:
            self.sec("13. 画面层：逐帧异常", "（已跳过）")
            self.suspect_times = []
            return
        fa = await self._analyze_frames()
        if fa.luma is None or len(fa.luma) < 10:
            self.sec("13. 画面层：逐帧异常", f"有效帧太少。{fa.error}")
            self.suspect_times = []
            return
        res = await asyncio.to_thread(analyze_frame_series, fa.times, fa.luma, fa.sat)
        t = res["times"]
        n = res["n"]
        flashes, cuts = res["flashes"], res["cuts"]
        lines = [
            f"帧数 {n}，时长 {t[-1]:.2f}s",
            f"亮度 YAVG 均值 {res['y_mean']:.2f} 标准差 {res['y_std']:.2f} "
            f"范围 [{res['y_min']:.1f},{res['y_max']:.1f}]",
            f"饱和度 SATAVG 均值 {res['sat_mean']:.2f}",
            f"帧间亮度突变阈值 (>均值+6σ) = {res['thr']:.2f}，共 {len(res['jumps'])} 处",
            f"  ├ 判为「单帧插入 / 闪帧」（跳出去又跳回来）: {len(flashes)} 处",
            f"  └ 判为「普通场景切换」（画面停在新亮度）: {len(cuts)} 处",
            "",
            "单帧插入明细:",
        ]
        if flashes:
            for i in flashes[:30]:
                d = abs(res["luma"][i + 1] - res["luma"][i])
                lines.append(f"  t={t[i]:8.3f}s  一帧 ΔY={d:.1f}  "
                             f"邻帧差={abs(res['luma'][i + 1] - res['luma'][i - 1]):.1f}")
        else:
            lines.append("  （无）")
        lines.append("\n场景切换明细:")
        for i in cuts[:30]:
            lines.append(f"  t={t[i]:8.3f}s → {t[i + 1]:8.3f}s   ΔY={abs(res['luma'][i + 1] - res['luma'][i]):.2f}")
        lines.append(f"\n冻结/静止段（帧间亮度几乎不变 ≥15 帧）: {len(res['frozen_runs'])} 段")
        for a, b in res["frozen_runs"][:20]:
            lines.append(f"  t={t[a]:8.2f}s .. {t[b]:8.2f}s  ({b - a + 1} 帧)")
        self.sec("13. 画面层：逐帧异常", "\n".join(lines))

        self.suspect_times = [round(float(t[i]), 3) for i in flashes[:40]]
        self.suspect_frame_indices = [int(i) for i in flashes[:40]]
        if flashes:
            self.add(HIGH, "画面层", f"检测到 {len(flashes)} 处单帧插入（闪帧）",
                     "某帧与前后帧都明显不同、而前后两帧彼此又几乎相同 —— 画面“跳出去又跳回来”，"
                     "这正是把内容（二维码/图片/暗码）塞进单一帧的特征。时刻: "
                     + "、".join(f"{t[i]:.2f}s" for i in flashes[:12])
                     + "。已对这些时刻做矩形码定点补扫。")
        else:
            self.add(INFO, "画面层", "没有单帧插入（闪帧）",
                     f"{len(res['jumps'])} 处亮度突变全部是普通场景切换（画面停在新亮度），符合正常剪辑。")
        if cuts:
            self.add(INFO, "画面层", f"共 {len(cuts)} 处场景切换",
                     f"约每 {t[-1] / max(1, len(cuts)):.1f} 秒一次，属正常剪辑节奏。")
        self.json_extra["frame_cuts_s"] = [round(float(t[i]), 3) for i in cuts[:200]]
        self.json_extra["frame_flashes_s"] = self.suspect_times

    async def layer_qr(self) -> None:
        if self.args.quick or np is None or not self.args.qr:
            self.sec("14. 画面层：矩形码启发式扫描", "（已跳过）")
            return
        fa = await self._analyze_frames()
        hits = list(fa.qr_hits)
        extra: List[Tuple[float, List[Any]]] = []
        idxs = getattr(self, "suspect_frame_indices", [])
        if idxs:
            extra = await self._qr_scan_flash_frames(idxs)
        lines = [
            f"按 {self.args.qr_fps} fps 采样，实际扫描 {fa.qr_scanned} 帧，"
            f"命中含 3 个定位图案（1:1:3:1:1）且构成直角的帧: {len(hits)}",
        ]
        if idxs:
            lines.append(f"对逐帧分析挑出的 {len(idxs)} 个异常帧定点补扫，命中: {len(extra)}")
        for t, cs in (hits + extra)[:40]:
            lines.append(f"  t={t:8.2f}s  定位图案 {len(cs)} 个: " +
                         ", ".join(f"({x},{y},m{u:.0f})" for x, y, u in cs[:4]))
        if extra:
            self.add(HIGH, "画面层", f"在 {len(extra)} 个“异常帧”上检测到矩形码类图案",
                     "这些时刻来自逐帧突变分析（疑似单帧插入），命中矩形码定位图案，"
                     "是“把暗码塞进某一帧”的典型特征。时间点见报告，帧已导出 PNG。")
        elif hits:
            self.add(MED, "画面层", f"{len(hits)} 帧检测到矩形码类定位图案",
                     "命中帧的时间点见报告，可疑帧已存为 PNG。注意：这是启发式算法，"
                     "画面里出现类似棋盘/回字纹纹路也会误报。")
        else:
            self.add(INFO, "画面层", "未检测到矩形码（QR 类）定位图案", "")
        self.sec("14. 画面层：矩形码启发式扫描", "\n".join(lines))


    # ============================================================ 编排
    def finish(self) -> Dict[str, Any]:
        self.json_extra["watermark_regions"] = self.watermark_regions
        self.json_extra["timings"] = self.timings
        counts = Counter(f.sev for f in self.findings)
        order = sorted(self.findings, key=lambda f: SEV_ORDER[f.sev])
        head = [
            "=" * 78,
            "MP4 隐藏数据 / 暗码 / 标记 扫描报告",
            f"文件: {self.name}",
            f"扫描时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"扫描器: mp4tool v{SCANNER_VERSION} (异步版)",
            "=" * 78,
            "",
            f"结论摘要: 高={counts.get(HIGH, 0)}  中={counts.get(MED, 0)}  "
            f"低={counts.get(LOW, 0)}  信息={counts.get(INFO, 0)}",
            "",
        ]
        for f in order:
            head.append(f"{SEV_ICON[f.sev]} [{f.sev}] ({f.cat}) {f.title}")
            if f.detail:
                for ln in f.detail.splitlines():
                    head.append(f"        {ln}")
            head.append("")
        if self.artifacts:
            head.append("提取的证据文件:")
            head += [f"  - {a}" for a in self.artifacts]
        report = "\n".join(head) + "\n" + "".join(self.sections)
        return {"report": report, "findings": [f.as_dict() for f in order],
                "sections": self.sections, "json_extra": self.json_extra}

    async def run(self) -> ScanResult:
        """跑完 14 项检查。

        静态检查整批丢进线程池（纯 CPU + 短命令），媒体分析走异步 ffmpeg。
        """
        t_start = time.time()
        opts = self.args
        static_steps = [
            ("文件层", self.layer_file),
            ("容器结构", self.layer_container),
            ("数据覆盖", self.layer_coverage),
            ("元数据", self.layer_metadata),
            ("流清单", self.layer_streams),
            ("码流 NAL/SEI", self.layer_bitstream),
            ("字符串情报", self.layer_strings),
            ("结构自洽性", self.layer_consistency),
        ]

        def _static_batch() -> None:
            for name, fn in static_steps:
                t0 = time.time()
                self._say(f"  · {name} ...")
                try:
                    fn()
                except Exception as exc:
                    import traceback
                    self._say(f"    ! {name} 出错: {exc}")
                    self.add(LOW, "扫描器", f"检查项「{name}」执行出错",
                             f"{type(exc).__name__}: {exc}")
                    if opts.verbose:
                        traceback.print_exc()
                self._time(name, t0)

        await asyncio.to_thread(_static_batch)

        async_steps = [
            ("音频频谱", self.layer_audio),
            ("画面静态叠加", self.layer_overlay),
            ("逐帧异常", self.layer_frames),
            ("矩形码扫描", self.layer_qr),
        ]
        for name, fn in async_steps:
            t0 = time.time()
            self._say(f"  · {name} ...")
            try:
                await fn()
            except Exception as exc:
                import traceback
                self._say(f"    ! {name} 出错: {exc}")
                self.add(LOW, "扫描器", f"检查项「{name}」执行出错",
                         f"{type(exc).__name__}: {exc}")
                if opts.verbose:
                    traceback.print_exc()
            self._time(name, t0)

        try:
            self._close_data()
        except Exception:
            pass
        res = self.finish()
        scan_result = ScanResult(
            path=self.path, name=self.name, size=self.size,
            md5=getattr(self, "md5", ""), sha256=getattr(self, "sha256", ""),
            findings=res["findings"], report_text=res["report"], json_payload={
                "tool": f"mp4tool v{SCANNER_VERSION}",
                "file": self.path,
                "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "options": {"quick": opts.quick, "deep": opts.deep, "qr": opts.qr,
                            "watermark": opts.watermark, "frames": opts.frames,
                            "audio": opts.audio, "fps": opts.fps, "qr_fps": opts.qr_fps,
                            "hires": opts.hires},
                "findings": res["findings"],
                "details": res["json_extra"],
                "artifacts": self.artifacts,
            },
            artifacts=list(self.artifacts),
            watermark_regions=list(self.watermark_regions),
            elapsed=time.time() - t_start,
            timings=dict(self.timings),
        )
        return scan_result
