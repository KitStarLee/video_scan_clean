# -*- coding: utf-8 -*-
"""分层修复：Tier A 等长字节补丁 / Tier B 无损重封装 / Tier C 有损重编码。

编排是异步的（重封装、重编码、验证全走异步 ffmpeg），纯字节操作和解析丢线程池。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import string
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from . import detect as _D
from . import ffmpeg_async as _F
from . import mp4box as _B
from .utils import HIGH, INFO, LOW, MED, eprint, human, md5_file, sha256_file

# 搬运过来的旧函数用到的模块级名字
parse_boxes = _B.parse_boxes
walk = _B.walk
read_stbl = _B.read_stbl
sample_arrays = _B.sample_arrays
merge_intervals = _B.merge_intervals
gaps_in = _B.gaps_in
KNOWN_TOP = _B.KNOWN_TOP
parse_sei = _D.parse_sei
from . import mp4box as scan   # 搬运过来的旧函数体里的 scan.xxx 全部指向 mp4box

REPAIR_VERSION = "2.0"


@dataclass
class RepairOptions:
    """字段名与旧版 argparse 命名空间一致，搬运过来的 build_tier_a 可零改动工作。"""
    vid: str = "blank"                  # blank(默认) | random | keep | 值
    vid_keep_prefix: int = -1
    encoder: str = "blank"              # blank(默认) | keep
    # auto（默认）= 自动判断：SDR 删全部 SEI；HDR 只删私有 user_data，
    # 保留 HDR 静态元数据（主控显示/CLL），否则画面会发灰。
    sei: str = "auto"                   # auto | keep | user-data | all | uuid:<hex>
    zero_unreferenced: bool = True
    zero_trailing: bool = True
    remux: str = "auto"                 # auto | always | never
    clean: bool = False                 # 只留音视频本质：丢封面图/字幕/数据轨/章节/全部元数据
    export_to_source :bool = False
    faststart: bool = True
    wm: str = "none"                    # none | fill | blur | delogo | mask
    # --wm mask：用扫描导出的「水印图层」抠出笔画遮罩，只补笔画像素（ffmpeg removelogo）
    wm_mask_threshold: float = 0.35     # 图层归一化后的二值化阈值（图层是空心轮廓，阈值宜低不宜高）
    wm_mask_dilate: int = 2             # 遮罩膨胀像素，防止笔画边缘残留
    wm_mask_paths: List[str] = field(default_factory=list)   # 运行时生成，不用手填
    # 用户在界面上勾选的那几处，各自对应的「水印图层」路径（与选中的区域一一对应）。
    # 空 = 非交互路径，按扫描顺序用 self.layer_pngs。
    wm_mask_layers: List[str] = field(default_factory=list)
    wm_regions: str = "auto"
    wm_region: Optional[List[str]] = None
    wm_ask: bool = False                # 检测到水印后弹界面让用户勾选要去掉哪几处
    wm_ask_timeout: float = 0.0         # 等待人工选择的秒数；0=一直等（不占用并发槽位）
    wm_color: str = "black"
    wm_blur: int = 12
    wm_crf: int = 18
    wm_preset: str = "medium"
    wm_faststart: bool = True
    verify: bool = True
    decode_check: bool = True
    rescan: bool = True          # 修复后用扫描器复扫做前后对比（最贵但最直接）
    rename_hash: bool = False
    threads: int = 0
    priority: int = 2


@dataclass
class RepairResult:
    path: str = ""
    src: str = ""
    size_before: int = 0
    size_after: int = 0
    md5_before: str = ""
    md5_after: str = ""
    sha256_after: str = ""
    patches: List[Dict[str, Any]] = field(default_factory=list)
    verify_lines: List[str] = field(default_factory=list)
    verify_ok: bool = True
    rescan_lines: List[str] = field(default_factory=list)
    tiers: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    def summary(self) -> str:
        return (f"{os.path.basename(self.path)}  "
                f"{human(self.size_before)} → {human(self.size_after)}  "
                f"{'✔ 验证通过' if self.verify_ok else '✗ 验证有问题'}")



class Container:
    """一个只读的 MP4 结构视图（复用扫描器的解析器）。"""

    def close(self) -> None:
        try:
            self._close()
        except Exception:
            pass

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        from .utils import map_file
        # 只读映射：不再把整个文件读进内存（4G 文件读一遍就是 4G，再复制就爆）
        self.data, self._close = map_file(self.path)
        self.size = len(self.data)
        self.anomalies: List[str] = []
        self.boxes = scan.parse_boxes(self.data, 0, self.size, "", 0, [], self.anomalies)
        self.media_ranges: List[Tuple[int, int]] = []

def locate_metadata_items(reader: "Container") -> List[Dict[str, Any]]:
    """定位所有可原地改写的元数据值，支持两种主流布局：

      1) mdta 风格：moov/udta/meta/keys + ilst，ilst 子盒用「1 起的序号」当类型
      2) iTunes/QuickTime 风格：moov/udta/meta/ilst/©cmt，或 moov/udta/©cmt
         （注意：ffmpeg 重封装/重编码后会把 mdta 自动转成这一种，必须一起处理，
           否则"改过的 vid"会在重编码后原样活下来）
    每个值都在 data 子盒里；老式 QuickTime 文本原子没有 data 子盒，用 [len][lang][text] 兜底。
    """
    out: List[Dict[str, Any]] = []

    keys: List[str] = []
    for kb in [b for b in scan.walk(reader.boxes) if b.type == "keys"]:
        try:
            p = kb.body + 4                       # version/flags
            n = struct.unpack_from(">I", reader.data, p)[0]
            p += 4
            for _ in range(n):
                ksize = struct.unpack_from(">I", reader.data, p)[0]
                ns = reader.data[p + 4:p + 8].decode("latin-1", "replace").strip("\x00")
                kv = reader.data[p + 8:p + ksize].decode("utf-8", "replace")
                keys.append(kv if ns in ("mdta", "") else f"{ns}:{kv}")
                p += ksize
        except Exception:
            continue

    items: List[Any] = []
    for b in scan.walk(reader.boxes):
        if b.type == "ilst":
            items += list(b.children)
        elif b.type == "udta":
            items += [c for c in b.children if c.type.startswith("\xa9") or c.type == "----"]

    for item in items:
        raw = item.type.encode("latin-1")
        if raw[:3] == b"\x00\x00\x00":
            idx = int.from_bytes(raw, "big")
            key = keys[idx - 1] if 1 <= idx <= len(keys) else f"#{idx}"
        elif item.type == "----":
            mean = next((reader.data[c.body + 8:c.end].decode("utf-8", "replace")
                         for c in item.children if c.type == "mean"), "")
            name = next((reader.data[c.body + 8:c.end].decode("utf-8", "replace")
                         for c in item.children if c.type == "name"), "")
            key = f"{mean}:{name}".lstrip(":")
        else:
            key = item.type

        datas = [d for d in item.children if d.type == "data"]
        if not datas:
            # 老式 QuickTime 文本原子：[size]['©xxx'][len(2)][lang(2)][text]
            if item.type.startswith("\xa9") and item.end - item.body > 4:
                voff = item.body + 4
                body = reader.data[voff:item.end]
                if body:
                    out.append({"key": key, "item_box": item, "data_box": None,
                                "value_offset": voff, "value_len": len(body),
                                "value": body.decode("utf-8", "replace"),
                                "trailing_nul": False, "legacy": True})
            continue
        for db in datas:
            # data 盒负载：size(4) 'data'(4) type(4) locale(4) 之后才是值
            voff = db.body + 8
            vlen = db.end - voff
            if vlen <= 0:
                continue
            out.append({"key": key, "item_box": item, "data_box": db,
                        "value_offset": voff, "value_len": vlen,
                        "value": reader.data[voff:voff + vlen].decode("utf-8", "replace"),
                        "trailing_nul": reader.data[voff:voff + vlen].endswith(b"\x00"),
                        "legacy": False})
    return out

def locate_sei(reader: "Container") -> List[Dict[str, Any]]:
    """定位视频码流里所有 SEI NAL（只找，不改），返回每个 NAL 的绝对偏移与长度。"""
    found: List[Dict[str, Any]] = []
    avcc = [b for b in scan.walk(reader.boxes) if b.type == "avcC"]
    if not avcc:
        return found
    cfg = reader.data[avcc[0].body:avcc[0].end]
    if len(cfg) < 7:
        return found
    length_size = (cfg[4] & 0x03) + 1
    for trak in [b for b in scan.walk(reader.boxes) if b.type == "trak"]:
        hdlrs = [b for b in scan.walk([trak]) if b.type == "hdlr"]
        if not hdlrs:
            continue
        if reader.data[hdlrs[0].body + 8:hdlrs[0].body + 12] != b"vide":
            continue
        stbls = [b for b in scan.walk([trak]) if b.type == "stbl"]
        if not stbls:
            continue
        info = scan.read_stbl(reader.data, [b for b in scan.walk(stbls)
                                            if b.type in ("stsz", "stz2", "stsc", "stco", "co64")])
        _offs, _szs = sample_arrays(info)
        for si, (soff, ssize) in enumerate(zip(_offs, _szs)):
            if ssize <= 0 or soff < 0 or soff + ssize > reader.size:
                continue
            p = soff
            end = soff + ssize
            while p + length_size <= end:
                nlen = int.from_bytes(reader.data[p:p + length_size], "big")
                if nlen <= 0 or p + length_size + nlen > end:
                    break
                nal_off = p + length_size
                nal = reader.data[nal_off:nal_off + nlen]
                nt = nal[0] & 0x1F
                if nt in (6, 39):                       # SEI（AVC 6 / HEVC 39）
                    rec = {"sample": si, "length_field_offset": p,
                           "nal_offset": nal_off, "nal_len": nlen,
                           "nal_type": nt, "payloads": []}
                    for pl in parse_sei(nal[1:], si, nal_off):
                        rec["payloads"].append({"type": pl["type"], "uuid": pl.get("uuid"),
                                                "text": pl.get("payload_text", "")})
                    found.append(rec)
                p = nal_off + nlen
        break
    return found

def locate_unreferenced(reader: "Container") -> List[Dict[str, Any]]:
    """找出「不被任何 sample 引用」的字节区间（尾部附加 / mdat 缝隙 / free 盒内容）。"""
    items: List[Dict[str, Any]] = []
    boxes = reader.boxes

    # 1) 尾部附加（最后一个顶层盒之后）
    top_end = max((b.end for b in boxes), default=0)
    if top_end < reader.size:
        items.append({"kind": "trailing", "start": top_end, "end": reader.size,
                      "label": f"文件末尾附加数据 {reader.size - top_end} 字节"})

    # 2) 所有轨道 sample 的并集
    samples: List[Any] = []              # 每个轨道一段 (n,2) 数组，别拼成 50 万个元组
    for trak in [b for b in scan.walk(boxes) if b.type == "trak"]:
        stbls = [b for b in scan.walk([trak]) if b.type == "stbl"]
        if not stbls:
            continue
        info = scan.read_stbl(reader.data, [b for b in scan.walk(stbls)
                                            if b.type in ("stsz", "stz2", "stsc", "stco", "co64")])
        _o, _s = sample_arrays(info)
        if len(_o):
            _m = _s > 0
            samples.append(np.column_stack([_o[_m], _o[_m] + _s[_m]]))
    media = scan.merge_intervals(
        np.concatenate(samples) if samples else [])
    reader.media_ranges = media
    for md in [b for b in boxes if b.type == "mdat"]:
        for a, b in scan.gaps_in(media, md.body, md.end):
            if b - a >= 16:
                items.append({"kind": "mdat_gap", "start": a, "end": b,
                              "label": f"mdat 内未引用缝隙 {b - a} 字节 @0x{a:x}"})

    # 3) free / skip / wide 的内容（保留 8 字节盒头，只清内容，长度不变）
    for fb in scan.walk(boxes):
        if fb.type in ("free", "skip", "wide") and fb.end > fb.body:
            items.append({"kind": f"{fb.type}_body", "start": fb.body, "end": fb.end,
                          "label": f"{fb.type} 盒内容 {fb.end - fb.body} 字节 @0x{fb.body:x}"})
    return items

def _same_length(new: str, old_len: int) -> bytes:
    b = new.encode("utf-8")
    if len(b) > old_len:
        raise ValueError(f"新值太长：{len(b)} > {old_len}")
    return b + b"\x00" * (old_len - len(b))

def make_vid_random(old: str, keep_prefix: Optional[int] = None) -> str:
    """生成同类型、同长度的假 vid（默认保留 "vid:" + 12 位平台格式码前缀）。"""
    if keep_prefix is None:
        if old.startswith("vid:v") and len(old) >= 24:
            keep_prefix = 16          # "vid:"(4) + "v0300fg10000"(12)
        else:
            keep_prefix = 4 if old.startswith("vid:") else 0
    keep_prefix = max(0, min(keep_prefix, len(old)))
    alpha = string.ascii_lowercase + string.digits   # 与原始 ID 用的字符集一致
    tail = "".join(random.choice(alpha) for _ in range(len(old) - keep_prefix))
    return old[:keep_prefix] + tail

def build_tier_a(reader: Container, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[str]]:
    """生成 Tier A 的原地补丁列表。返回 (patches, log_lines)。"""
    patches: List[Dict[str, Any]] = []
    lines: List[str] = []

    # ---- A1/A2 元数据 ----
    items = locate_metadata_items(reader)
    if not items:
        lines.append("  （没有找到 mdta/ilst 元数据）")
    for it in items:
        key = it["key"]
        val = it["value"].rstrip("\x00")
        mode = "keep"
        # vid 的判定看**值的内容**，不看键名 —— 键名可能是 comment / ©cmt / 数字序号，
        # 取决于容器是 mdta 风格还是 iTunes 风格（重编码后会自动变）。
        if re.search(r"(?i)\bvid\s*:", val):
            mode = args.vid
        elif key.lower() in ("encoder", "encoded_by", "encoding_tool", "software",
                             "handler_name", "vendor_id", "\xa9too", "\xa9enc", "\xa9swr") \
                or re.match(r"(?i)^(lavf|lavc|libav|libx26[45]|x26[45]|ffmpeg)", val):
            mode = args.encoder
        if mode == "keep":
            lines.append(f"  · 元数据 {key!r} = {val!r} → 保持不动")
            continue
        if mode == "blank":
            new = ""
            note = "清空"
        elif mode == "random":
            new = make_vid_random(val, None if args.vid_keep_prefix < 0 else args.vid_keep_prefix) \
                if re.search(r"(?i)\bvid\s*:", val) else \
                ("".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(len(val))))
            note = "改写为同长度随机值"
        else:                      # value:xxx
            new = mode.split(":", 1)[1] if ":" in mode else mode
            note = "改写为指定值"
        try:
            blob = _same_length(new, it["value_len"])
        except ValueError as exc:
            lines.append(f"  ! 元数据 {key!r} 跳过：{exc}（长度必须 ≤ {it['value_len']}）")
            continue
        label = f"元数据 {key}: {val!r} → {new!r}（{note}，等长原地改写）"
        patches.append({"offset": it["value_offset"], "data": blob, "label": label,
                        "kind": "metadata", "key": key,
                        "before": val, "after": new})
        lines.append(f"  · {label}  @0x{it['value_offset']:x}  {it['value_len']} 字节")

    # ---- A3 SEI → filler ----
    seis = locate_sei(reader)
    if not seis:
        lines.append("  （码流里没有 SEI）")
    for s in seis:
        keep = False
        # 注意历史坑：老 CLI 里 `--sei none` 的语义是「一个都不删」(remove none)，
        # 和直觉相反。这里把 "none" 当 "keep" 的别名兼容，新代码一律用 "keep"。
        if args.sei in ("keep", "none"):
            keep = True
        elif args.sei == "user-data":
            keep = not any(p["type"] == 5 for p in s["payloads"])
        elif args.sei.startswith("uuid:"):
            want = args.sei.split(":", 1)[1].lower().replace("-", "")
            keep = not any((p.get("uuid") or "") == want for p in s["payloads"])
        if keep:
            lines.append(f"  · SEI @0x{s['nal_offset']:x}（sample#{s['sample']}，"
                         f"payload={[p['type'] for p in s['payloads']]}）→ 按规则保留")
            continue
        # 原地替换成等长的 filler NAL(type 12)：解码器直接跳过，长度不变
        blob = bytes([0x0C]) + b"\x80" + b"\x00" * (s["nal_len"] - 2)
        drops = [f"type{p['type']}" + (f"/{p['uuid']}" if p.get("uuid") else "") for p in s["payloads"]]
        label = (f"SEI @0x{s['nal_offset']:x}（sample#{s['sample']}，{drops}）"
                 f"→ 等长 filler NAL（{s['nal_len']} 字节）")
        patches.append({"offset": s["nal_offset"], "data": blob[:s["nal_len"]], "label": label,
                        "kind": "sei", "sample": s["sample"], "payloads": s["payloads"]})
        lines.append(f"  · {label}")

    # ---- A4 无引用字节清零 ----
    if args.zero_unreferenced:
        unref = locate_unreferenced(reader)
        if not unref:
            lines.append("  · 没有被 sample 引用的多余字节（mdat 全被覆盖、无尾部附加）")
        for u in unref:
            n = u["end"] - u["start"]
            if n <= 0:
                continue
            if u["kind"] == "trailing" and not args.zero_trailing:
                lines.append(f"  · 尾部附加数据 {n} 字节 → 按参数保留")
                continue
            if n >= (1 << 20):
                # 超过 1MB 的整段清零不真的造 bytes（一个 1G 的缝隙 = 1G 内存）
                patches.append({"offset": u["start"], "data": b"", "zero_len": n,
                                "label": f"{u['label']} → 整段清零", "kind": u["kind"]})
            else:
                patches.append({"offset": u["start"], "data": b"\x00" * n,
                                "label": f"{u['label']} → 整段清零", "kind": u["kind"]})
            lines.append(f"  · {u['label']} → 整段清零（长度不变）")
    return patches, lines

def patch_len(p: Dict[str, Any]) -> int:
    """补丁覆盖多少字节。

    大段清零（尾部附加 / mdat 缝隙）用 ``zero_len`` 表示，
    **不在内存里真的造那么多零** —— 一个 1GB 的缝隙就是 1GB 的 bytes。
    """
    z = p.get("zero_len")
    return int(z) if z else len(p.get("data") or b"")


def apply_patches(reader: Container, patches: List[Dict[str, Any]], out_path: str,
                  inplace_source: Optional[str] = None) -> None:
    """把补丁写进一个副本。补丁之间不允许重叠。

    **流式**写入：原来 ``bytearray(reader.data)`` 会复制整份文件（4G → 4G 内存），
    现在按块读源文件、在块内应用补丁、写出；块尾会主动避开补丁起点，
    保证没有补丁被块边界切断。
    """
    ordered = sorted(patches, key=lambda p: p["offset"])
    size = reader.size
    last_end = -1
    for p in ordered:
        o, n = p["offset"], patch_len(p)
        if o < last_end:
            raise RuntimeError(f"补丁重叠：{p['label']}")
        if o + n > size:
            raise RuntimeError(f"补丁越界：{p['label']}")
        last_end = o + n

    CHUNK = 8 << 20
    with open(reader.path, "rb") as src, open(out_path, "wb") as dst:
        pos, pi = 0, 0
        while pos < size:
            end = min(size, pos + CHUNK)
            if pi < len(ordered):
                po = ordered[pi]["offset"]
                if pos < po < end:
                    end = po                                  # 块尾停在补丁前
                elif po == pos:
                    end = min(size, max(end, po + patch_len(ordered[pi])))
            blk = src.read(end - pos)
            if not blk:
                break
            buf = bytearray(blk)
            while pi < len(ordered) and ordered[pi]["offset"] < pos + len(buf):
                p = ordered[pi]
                o, n = p["offset"] - pos, patch_len(p)
                buf[o:o + n] = bytes(n) if p.get("zero_len") else p["data"]
                pi += 1
            dst.write(buf)
            pos += len(blk)

def clamp_region(r: Dict[str, int], W: int, H: int) -> Dict[str, int]:
    """把水印区域收进画面内。delogo 要求区域不能贴边，这里统一留 1px。"""
    x = max(1, int(r["x"]))
    y = max(1, int(r["y"]))
    w = int(r["w"])
    h = int(r["h"])
    if x + w > W - 1:
        w = W - 1 - x
    if y + h > H - 1:
        h = H - 1 - y
    return {"x": x, "y": y, "w": max(2, w), "h": max(2, h)}

def _ff_escape(path: str) -> str:
    """把路径转义成能安全放进 filtergraph 的形式（Windows 的 C: 一定要转义）。"""
    return "".join(("\\" + ch) if ch in "\\':,[];" else ch for ch in path)


def build_watermark_filter(regions: List[Dict[str, int]], mode: str, W: int, H: int,
                           color: str, blur: int,
                           mask_paths: Optional[List[str]] = None) -> Tuple[str, str]:
    """返回 (filter_complex, 输出的视频标签)，不含方括号。"""
    rs = [clamp_region(r, W, H) for r in regions]
    if mode == "mask":
        # removelogo 吃一张「和画面等大」的黑白遮罩，只对遮罩里的像素做邻域修补。
        # 所以它抹掉的是**文字笔画本身**，而不是整个矩形区域。
        paths = [p for p in (mask_paths or []) if p]
        if not paths:
            raise ValueError("mask 模式没有可用的遮罩文件")
        chain = ",".join(f"removelogo=filename={_ff_escape(p)}" for p in paths)
        return f"[0:v]{chain}[vout]", "vout"
    if mode == "fill":
        chain = ",".join(f"drawbox=x={r['x']}:y={r['y']}:w={r['w']}:h={r['h']}"
                         f":color={color}@1.0:t=fill" for r in rs)
        return f"[0:v]{chain}[vout]", "vout"
    if mode == "delogo":
        chain = ",".join(f"delogo=x={r['x']}:y={r['y']}:w={r['w']}:h={r['h']}:show=0" for r in rs)
        return f"[0:v]{chain}[vout]", "vout"
    if mode == "blur":
        parts = []
        prev = "0:v"
        for i, r in enumerate(rs):
            parts.append(f"[{prev}]split=2[a{i}][b{i}]")
            parts.append(f"[b{i}]crop=w={r['w']}:h={r['h']}:x={r['x']}:y={r['y']},"
                         f"boxblur={blur}:2[c{i}]")
            parts.append(f"[a{i}][c{i}]overlay=x={r['x']}:y={r['y']}[d{i}]")
            prev = f"d{i}"
        return ";".join(parts), prev
    raise ValueError(f"未知的水印处理模式: {mode}")

# ================================================================ 验证
# ================================================================ 笔画遮罩（--wm mask）
def _layer_placement(layer_png: str, region: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """图层图在源画面上的落点 (x,y,w,h)。

    优先读扫描时写下的 sidecar json（精确）；没有就退回「区域外扩 8px」的约定。
    """
    try:
        with open(os.path.splitext(layer_png)[0] + ".json", encoding="utf-8") as fh:
            d = json.load(fh)
        return int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"])
    except Exception:
        pad = 8
        return (max(0, int(region.get("x", 0)) - pad),
                max(0, int(region.get("y", 0)) - pad),
                int(region.get("w", 0)) + 2 * pad,
                int(region.get("h", 0)) + 2 * pad)


def _fill_holes(mask: "Any") -> "Any":
    """把空心轮廓填成实心：行内 / 列内「夹在两段笔画之间」的点补上。

    关键原因：水印图层是**梯度幅值**图，粗笔画的边缘梯度大、**内部梯度≈0**，
    所以图层上的字形是**空心轮廓**。直接阈值化只能得到一圈环，
    removelogo 就只补那一圈，笔画内部的白纱原样留下 —— 那就是"脏斑"。
    这里用「行方向夹住 且 列方向也夹住」来填内部（纯 numpy，不用 scipy）。
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return mask
    a = np.asarray(mask).astype(bool)
    if not a.any():
        return mask
    lr = np.logical_and.accumulate(a, axis=1)
    rl = np.logical_and.accumulate(a[:, ::-1], axis=1)[:, ::-1]
    tb = np.logical_and.accumulate(a, axis=0)
    bt = np.logical_and.accumulate(a[::-1, :], axis=0)[::-1, :]
    filled = a | ((lr & rl) & (tb & bt))
    return Image.fromarray((filled.astype(np.uint8) * 255), "L")


def make_text_mask(W: int, H: int, regions: List[Dict[str, Any]],
                   layer_pngs: Optional[List[str]], workdir: str,
                   threshold: float = 0.55, dilate: int = 2) -> List[str]:
    """把「水印图层」变成笔画遮罩（PGM），每个区域一张，供 ffmpeg removelogo 使用。

    水印图层是扫描器对多帧做梯度中位数得到的（scanner.layer_overlay），
    上面只有水印本身、没有背景，所以最适合拿来抠笔画。
    """
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return []
    os.makedirs(workdir, exist_ok=True)
    out: List[str] = []
    for i, r in enumerate(regions):
        lp = layer_pngs[i] if layer_pngs and i < len(layer_pngs) else ""
        if not lp or not os.path.exists(lp):
            continue
        try:
            lay = Image.open(lp).convert("L")
            x, y, w, h = _layer_placement(lp, r)
            if w < 4 or h < 4:
                continue
            m = lay.resize((max(2, w), max(2, h)), Image.LANCZOS)
            lo, hi = m.getextrema()
            if hi <= lo:
                continue
            # 先在本区域内归一化，再按阈值抠出亮笔画
            m = m.point(lambda v: 255 if (v - lo) / float(hi - lo) >= threshold else 0)
            m = _fill_holes(m)          # 先把空心轮廓填成实心笔画，再调整胖瘦
            # dilate>0 膨胀；dilate<0 腐蚀。
            # 略胖一点更保险（把笔画边缘的抗锯齿和残影也盖住）；
            # 如果发现吃到了周围画面，就往负方向调成腐蚀。
            if dilate > 0:
                m = m.filter(ImageFilter.MaxFilter(min(9, dilate * 2 + 1)))
            elif dilate < 0:
                m = m.filter(ImageFilter.MinFilter(min(9, -dilate * 2 + 1)))
            canvas = Image.new("L", (W, H), 0)          # removelogo 要求遮罩与画面等大
            x0, y0 = max(0, x), max(0, y)
            cw, ch = min(w, W - x0), min(h, H - y0)
            if cw <= 0 or ch <= 0:
                continue
            canvas.paste(m.crop((x0 - x, y0 - y, x0 - x + cw, y0 - y + ch)), (x0, y0))
            p = os.path.join(workdir, f"textmask_{i + 1}.pgm")
            canvas.save(p)
            out.append(p)
        except Exception:
            continue
    return out


async def detect_hdr(path: str) -> bool:
    """视频流是不是 HDR（PQ / HLG）。用来决定 SEI 该删到什么程度。"""
    info = await ffprobe_streams(path)
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            return (st.get("color_transfer") or "").lower() in ("smpte2084", "arib-std-b67")
    return False


async def ffprobe_streams(path: str) -> Dict[str, Any]:
    try:
        return await _F.ffprobe_json(path, "format:streams")
    except Exception:
        return {}


async def verify(orig: str, out: str, patches: Optional[List[Dict[str, Any]]],
                 decode_check: bool = True, tmpdir: Optional[str] = None,
                 duration_tol: float = 0.05, clean: bool = False) -> Tuple[bool, List[str]]:
    """验证输出：结构一致 / 时长一致 / 全片解码零错误 / 等长补丁只落在预期区间。"""
    lines: List[str] = []
    ok = True
    if not os.path.exists(out):
        return False, ["✗ 输出文件不存在"]

    a, b = await ffprobe_streams(orig), await ffprobe_streams(out)
    if not b.get("streams"):
        return False, ["✗ 输出的流信息无法解析 —— 文件结构已损坏"]

    def _keep(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理模式下的预期结果：封面图和字幕/data 轨是**故意**丢掉的，不该算损坏。"""
        out = []
        for st in streams:
            ct = st.get("codec_type")
            if ct in ("video", "audio"):
                if clean and (st.get("disposition") or {}).get("attached_pic"):
                    continue                      # 封面图/缩略图：故意丢
                out.append(st)
            elif not clean:
                out.append(st)                    # 非清理模式：字幕/data 轨必须原样保留
        return out

    def _group(streams: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        g: Dict[str, List[Dict[str, Any]]] = {}
        for st in streams:
            g.setdefault(st.get("codec_type") or "?", []).append(st)
        return g

    _ka = a.get("streams", [])
    _kept_a = _keep(_ka)
    ga, gb = _group(_kept_a), _group(_keep(b.get("streams", [])))
    for ct in sorted(set(ga) | set(gb)):
        la, lb = ga.get(ct, []), gb.get(ct, [])
        if len(la) != len(lb):
            ok = False
            lines.append(f"✗ {ct} 流数量不一致：原件 {len(la)} 条 / 输出 {len(lb)} 条")
            continue
        for i, (x, y) in enumerate(zip(la, lb)):
            tag = f"{ct}[{i}]"
            for f in ("codec_name", "width", "height", "sample_rate", "channels", "nb_frames"):
                vx, vy = x.get(f), y.get(f)
                if vx in (None, "N/A") and vy in (None, "N/A"):
                    continue
                if f in ("codec_name", "width", "height", "sample_rate", "channels"):
                    if str(vx) != str(vy):
                        ok = False
                        lines.append(f"✗ {tag}.{f} 不一致：{vx} → {vy}")
                elif vx not in (None, "N/A") and str(vx) != str(vy):
                    lines.append(f"! {tag}.{f} 变化：{vx} → {vy}"
                                 "（重封装/重编码后容器计数值可能改变）")
    if clean:
        dropped = [s.get("codec_type") for s in _ka if s not in _kept_a]
        if dropped:
            lines.append("· 按清理要求删除了这些流：" + "、".join(sorted(set(dropped)))
                         + "（封面图/字幕/data 轨属于要丢掉的外包装）")
    if not any(l.startswith("✗") for l in lines):
        lines.append("✔ 流结构、编码、分辨率、采样率、声道一致")

    da = a.get("format", {}).get("duration")
    db = b.get("format", {}).get("duration")
    try:
        diff = abs(float(da) - float(db))
        if diff > duration_tol:
            ok = False
            lines.append(f"✗ 时长不一致：{da} → {db}（差 {diff:.3f}s，容差 {duration_tol:.2f}s）")
        elif diff > 0.05:
            # 重编码时 ffmpeg 会按最长流补帧，几十到几百毫秒的差是正常现象
            lines.append(f"· 时长有微小变化：{da} → {db}（差 {diff * 1000:.0f} ms，"
                         f"重编码按最长流对齐，属正常）")
        else:
            lines.append(f"✔ 时长一致：{float(db):.3f}s（差 {diff * 1000:.0f} ms）")
    except (TypeError, ValueError):
        lines.append("! 时长无法比较")

    if decode_check:
        good, err, secs = await _F.decode_null_check(out)
        if not good:
            ok = False
            lines.append(f"✗ 完整解码报错（{secs:.1f}s）：")
            lines += [f"    {l}" for l in err.splitlines()[:12]]
        else:
            lines.append(f"✔ 全片解码零错误（{secs:.1f}s）")

    if patches is not None and os.path.getsize(orig) == os.path.getsize(out):
        runs, ndiff, fsize = await asyncio.to_thread(_diff_runs, orig, out)
        if not runs:
            lines.append("! 输出与原文件完全相同（没有产生任何改动）")
        else:
            planned = sorted((p["offset"], p["offset"] + patch_len(p)) for p in (patches or []))
            outside = [(x, y) for x, y in runs
                       if not any(pa <= x and y < pb for pa, pb in planned)]
            lines.append(f"✔ Tier A 等长校验：文件大小不变（{fsize} 字节），"
                         f"差异 {ndiff} 字节 / {len(runs)} 个区间")
            for x, y in runs[:20]:
                lines.append(f"    0x{x:x}..0x{y + 1:x}  ({y - x + 1} 字节)")
            if outside:
                ok = False
                lines.append(f"✗ 有 {len(outside)} 个改动区间落在预期补丁之外："
                             + "、".join(f"0x{x:x}..0x{y + 1:x}" for x, y in outside[:5]))
            else:
                lines.append("✔ 所有改动都落在预期补丁区间内")
    elif patches:
        lines.append("· 文件大小已改变（做了重封装/重编码），跳过逐字节比对")
    return ok, lines


def _diff_runs(a: str, b: str, chunk: int = 8 << 20
               ) -> Tuple[List[Tuple[int, int]], int, int]:
    """流式逐字节比较两个等长文件，返回 (差异区间, 差异字节数, 文件大小)。

    旧实现把**两份文件整个读进内存**（4G 文件 = 8G），这是最容易爆的一处。
    现在按块比较：先用 C 速度的 bytes 比较整块，只有真的不同的块才做细粒度定位，
    所以正常情况下代价近似一次 memcmp。返回的区间格式与旧实现一致。
    """
    try:
        import numpy as np
    except Exception:
        np = None
    runs: List[Tuple[int, int]] = []
    total = 0
    pos = 0
    cur: Optional[Tuple[int, int]] = None
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ba = fa.read(chunk)
            if not ba:
                break
            bb = fb.read(len(ba))
            if ba != bb:
                if np is not None:
                    idx = np.nonzero(np.frombuffer(ba, dtype=np.uint8)
                                     != np.frombuffer(bb, dtype=np.uint8))[0]
                else:
                    idx = [i for i, (x, y) in enumerate(zip(ba, bb)) if x != y]
                for i in idx:
                    i = int(i)
                    if cur is not None and pos + i == cur[1] + 1:
                        cur = (cur[0], pos + i)
                    else:
                        if cur is not None:
                            runs.append(cur)
                        cur = (pos + i, pos + i)
                    total += 1
            pos += len(ba)
    if cur is not None:
        runs.append(cur)
    return runs, total, pos


# ================================================================ 清理范围（诚实清单）
#
# 默认配置（--clean，即不写任何参数）**会**清掉：
#   · vid: 平台视频 ID、encoder/©too 等编码器指纹 ……………… Tier A 等长改写
#   · 码流里的 SEI（SDR 删全部；HDR 只删私有 user_data）…… Tier A 换等长 filler NAL
#   · mdat 缝隙 / 尾部附加数据 / free 盒夹带 …………………… Tier A 清零，Tier B 再丢弃
#   · 封面图/缩略图、字幕/data/timecode/hint 轨、章节 ……… Tier B（--clean 的 -map 组合）
#   · 全部容器元数据（title/artist/date/GPS/自定义键…）…… Tier B（-map_metadata -1）
#   · 未知/私有顶层盒（C2PA 的 uuid/jumb、DRM 的 pssh…）… Tier B 重建容器时自然丢弃
#   · 画面里**可见的**烧录水印/台标 ……………………………… 只有显式开 Tier C（--wm*），有损
#
# **不会**清掉的（别指望，这里写清楚免得误判）：
#   · 音频域水印（超声载波/扩频/窄带单音）：
#       本模块没有任何音频处理，音频一律 -c:a copy；扫描器只能检测，不能去除。
#   · 像素域**不可见**水印（空域/变换域图案、时间维调制）：
#       Tier C 只处理你用 --wm-region / --wm-ask 圈出来的可见区域。
#   · HDR 静态元数据（mastering display / CLL）：
#       HDR 片源上**刻意保留**（删了画面会发灰），这是设计不是遗漏。
#   · 内容指纹（感知哈希 / 音频指纹）：
#       它由画面和声音本身决定，改容器、改元数据都动不了它。
#   · 文件名（当文件名就是内容哈希时，扫描器会报 HIGH，但修复不改名，
#       除非显式 --rename-hash）。
# ================================================================ 复扫对比
async def rescan_and_compare(orig_payload: Optional[Dict[str, Any]], out_path: str,
                             scan_options: Dict[str, Any], outdir: str) -> List[str]:
    """用同一个扫描器复扫修复后的文件，和原报告对比 findings。

    直接复用进程内的扫描器（不再起子进程），并且**沿用原报告的扫描深度**，
    否则「深度模式才有的发现」会被误判成「已消除」。
    """
    from .scanner import ScanSession, ScanOptions
    lines: List[str] = []
    opts = ScanOptions(outdir=outdir, quick=bool(scan_options.get("quick")),
                       deep=bool(scan_options.get("deep")),
                       qr=bool(scan_options.get("qr", True)),
                       watermark=bool(scan_options.get("watermark", True)),
                       frames=bool(scan_options.get("frames", True)),
                       audio=bool(scan_options.get("audio", True)),
                       fps=float(scan_options.get("fps", 1.0)),
                       qr_fps=float(scan_options.get("qr_fps", 0.5)),
                       quiet=True, threads=0, priority=2)
    try:
        sess = ScanSession(out_path, opts)
        res = await sess.run()
    except Exception as exc:
        return [f"! 复扫失败：{type(exc).__name__}: {exc}"]
    new_sig = {(f["severity"], f["title"]): f for f in res.findings}
    counts = res.severity_counts()
    lines.append(f"复扫结果: " + "  ".join(f"{k}={v}" for k, v in counts.items() if v))
    if not orig_payload:
        lines.append("（没有原扫描报告，只给出修复后的结论）")
        for sev, title in sorted(new_sig):
            if sev in (HIGH, MED):
                lines.append(f"  [{sev}] {title}")
        return lines

    old_sig = {(f["severity"], f["title"]): f for f in orig_payload.get("findings", [])}
    gone = [k for k in old_sig if k not in new_sig]
    still = [k for k in old_sig if k in new_sig and k[0] in (HIGH, MED)]
    added = [k for k in new_sig if k not in old_sig]
    lines.append(f"原有 finding {len(old_sig)} 条 → 现在 {len(new_sig)} 条")
    if gone:
        lines.append(f"✔ 已消除 {len(gone)} 条:")
        for sev, t in sorted(gone):
            lines.append(f"    [{sev}] {t}")
    if still:
        lines.append(f"· 仍然存在 {len(still)} 条"
                     f"（若含 vid:，说明是同类型改写而非删除，用 --vid blank 可彻底去掉）:")
        for sev, t in sorted(still):
            lines.append(f"    [{sev}] {t}")
    if added:
        lines.append(f"! 新增 {len(added)} 条:")
        for sev, t in sorted(added):
            lines.append(f"    [{sev}] {t}")
    return lines


# ================================================================ 修复会话
class RepairSession:
    """对一个视频做 Tier A/B/C 修复。全部 IO 走异步 ffmpeg，纯字节操作走线程池。"""

    def __init__(self, src: str, out_path: str, opts: RepairOptions, *,
                 workdir: str, scan_payload: Optional[Dict[str, Any]] = None,
                 watermark_regions: Optional[List[Dict[str, Any]]] = None,
                 layer_pngs: Optional[List[str]] = None,
                 report_path: Optional[str] = None, log=None):
        self.src = os.path.abspath(src)
        self.out = os.path.abspath(out_path)
        self.opts = opts
        self.workdir = workdir
        self.scan_payload = scan_payload
        self.watermark_regions = watermark_regions or []
        self.layer_pngs = layer_pngs or []
        self.report_path = report_path
        self.log = log or (lambda *a, **k: None)
        self.result = RepairResult(src=self.src, path=self.out)

    async def _post_scrub(self, path: str, tag: str) -> str:
        """重封装/重编码之后再跑一遍 Tier A。

        必须做：ffmpeg 会把**它自己的编码器标签**写进元数据（实测把 Lavf58.76.100
        换成 Lavf61.7.100），把 --encoder blank 覆盖掉；libx264 还会塞进自己的 SEI。
        """
        r = Container(path)
        p2, l2 = await asyncio.to_thread(build_tier_a, r, self.opts)
        self.log(f"  · 后置复扫({tag})：重新定位到 {len(p2)} 个可清理目标")
        for l in l2:
            self.log("    " + l)
        if not p2:
            return path
        outp = os.path.join(self.workdir, f"postscrub_{tag}.mp4")
        await asyncio.to_thread(apply_patches, r, p2, outp)
        self.result.patches.extend(p2)
        return outp

    async def run(self) -> RepairResult:
        t0 = time.time()
        opts = self.opts
        os.makedirs(self.workdir, exist_ok=True)
        reader = Container(self.src)
        self.result.size_before = reader.size
        self.log(f"\n原文件: {reader.size} 字节")
        self.log(f"顶层盒: {', '.join(f'{b.type}({b.size})' for b in reader.boxes)}")

        # ---------------- SEI 策略（自动，不需要用户选） ----------------
        if opts.sei == "auto":
            hdr = await detect_hdr(self.src)
            opts.sei = "user-data" if hdr else "all"
            self.log("\n【SEI】" + ("检测到 HDR → 只删私有 user_data SEI，"
                                    "保留 HDR 静态元数据（主控显示/CLL），避免画面发灰"
                                    if hdr else
                                    "SDR 片源 → 删除全部 SEI 私有数据"))

        # ---------------- Tier A ----------------
        self.log("\n【Tier A】等长字节补丁（文件长度不变，stco 不用动，零播放风险）")
        patches, lines = await asyncio.to_thread(build_tier_a, reader, opts)
        for l in lines:
            self.log(l)
        self.log(f"  → 共 {len(patches)} 个补丁，覆盖 {sum(patch_len(p) for p in patches)} 字节")
        self.result.patches.extend(patches)
        if patches or opts.verify:
            self.result.tiers.append("A")

        cur = self.src
        if patches:
            cur = os.path.join(self.workdir, "step_a.mp4")
            await asyncio.to_thread(apply_patches, reader, patches, cur)

        # ---------------- Tier C ----------------
        if opts.wm != "none":
            probe = await ffprobe_streams(cur)
            vs = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
            if not vs:
                self.log("\n【Tier C】没有视频流，跳过。")
            else:
                W, H = int(vs[0]["width"]), int(vs[0]["height"])
                regions = self._resolve_regions(W, H)
                if not regions:
                    self.log("\n【Tier C】没有可用的水印区域，跳过。")
                else:
                    if opts.wm == "mask":
                        # ★ 只能用 regions（= _resolve_regions 出来的、用户真正选中的那些），
                        #   绝对不能用 self.watermark_regions（那是扫描出的**全部**候选）——
                        #   否则没勾的区域（比如被误判成"静态叠加层"的烧录字幕）也会被一起修补。
                        if opts.wm_mask_layers:
                            layers = list(opts.wm_mask_layers)
                        elif opts.wm_region:
                            layers = []          # --wm-region 手填坐标没有对应图层
                        else:
                            layers = list(self.layer_pngs)
                        opts.wm_mask_paths = await asyncio.to_thread(
                            make_text_mask, W, H, regions, layers, self.workdir,
                            opts.wm_mask_threshold, opts.wm_mask_dilate)
                        if opts.wm_mask_paths:
                            self.log(f"\n【Tier C】笔画遮罩模式：{len(opts.wm_mask_paths)} 张遮罩，"
                                     f"只补文字笔画（阈值 {opts.wm_mask_threshold}，"
                                     f"膨胀 {opts.wm_mask_dilate}px）")
                        else:
                            self.log("\n【Tier C】mask 模式拿不到水印图层（扫描时没导出），"
                                     "自动回退到 delogo")
                            opts.wm = "delogo"
                    self.log(f"\n【Tier C】重编码去除烧录水印：模式={opts.wm}，"
                             f"{len(regions)} 个区域，画面 {W}x{H}")
                    for r in regions:
                        self.log(f"  · x={r['x']} y={r['y']} w={r['w']} h={r['h']}")
                    self.log("  ⚠ 这一步是有损的：画面会被重编码，原始码流不再保留。")
                    step_c = os.path.join(self.workdir, "step_c.mp4")
                    t1 = time.time()
                    okc, errc = await self._reencode(cur, step_c, regions, W, H)
                    if not okc:
                        self.log(f"  ✗ 重编码失败：\n{errc[:1200]}")
                        self.log("  → Tier C 失败，已自动跳过，继续用上一步的结果。")
                    else:
                        self.log(f"  ✔ 重编码完成（{time.time() - t1:.0f}s）")
                        self.result.tiers.append("C")
                        cur = await self._post_scrub(step_c, "C")

        # ---------------- Tier B ----------------
        do_remux = opts.remux == "always"
        if opts.remux == "auto":
            r3 = Container(cur)
            unref = locate_unreferenced(r3)
            junk = [u for u in unref if u["kind"] in ("trailing", "mdat_gap")
                    or (u["kind"].endswith("_body")
                        and r3.data[u["start"]:u["end"]] != b"\x00" * (u["end"] - u["start"]))]
            unknown = [b for b in r3.boxes if b.type not in KNOWN_TOP]
            do_remux = bool(junk or unknown)
            self.log(f"\n【Tier B】auto 判断：尾部/缝隙垃圾 {len(junk)} 处，未知顶层盒 {len(unknown)} 个 "
                     f"→ {'执行重封装' if do_remux else '没有可丢的东西，跳过'}")
        # --clean 必须重建容器（封面图/字幕轨/元数据/章节都只能靠重封装丢掉），
        # 注意要放在 auto 判定**之后**，否则会被 auto 的结论覆盖。
        if opts.clean:
            if not do_remux:
                self.log("\n【Tier B】--clean 需要重建容器（要丢封面图/字幕轨/元数据/章节），"
                         "本次强制执行重封装")
            do_remux = True
        if do_remux:
            self.log("\n【Tier B】ffmpeg -c copy 重封装（画质零损失，容器重建）")
            step_b = os.path.join(self.workdir, "step_b.mp4")
            okb, errb = await self._remux(cur, step_b)
            if not okb:
                self.log(f"  ✗ 重封装失败，保留上一步结果：\n{errb[:1200]}")
            else:
                self.log("  ✔ 重封装完成")
                self.result.tiers.append("B")
                cur = await self._post_scrub(step_b, "B")

        # ---------------- 落盘 ----------------
        os.makedirs(os.path.dirname(self.out) or ".", exist_ok=True)
        if os.path.abspath(cur) != self.out:
            await asyncio.to_thread(shutil.copyfile, cur, self.out)
        self.result.size_after = os.path.getsize(self.out)
        self.result.md5_after = await asyncio.to_thread(md5_file, self.out)
        self.result.sha256_after = await asyncio.to_thread(sha256_file, self.out)
        self.result.md5_before = await asyncio.to_thread(md5_file, self.src)

        # ---------------- 验证 ----------------
        if opts.verify:
            self.log("\n【验证】")
            same_size = self.result.size_before == self.result.size_after
            # 重编码（Tier C）会按最长流补帧，容差放宽到 0.3s；无损路径保持严格
            tol = 0.3 if "C" in self.result.tiers else 0.05
            ok, vlines = await verify(self.src, self.out,
                                      self.result.patches if same_size else None,
                                      opts.decode_check, duration_tol=tol,
                                      clean=bool(opts.clean))
            self.result.verify_ok = ok
            self.result.verify_lines = vlines
            for l in vlines:
                self.log("  " + l)

        self.result.elapsed = time.time() - t0
        return self.result

    # ---------------- 内部：区域解析 ----------------
    def _resolve_regions(self, W: int, H: int) -> List[Dict[str, int]]:
        opts = self.opts
        if opts.wm_region:
            out = []
            for spec in opts.wm_region:
                parts = [int(v) for v in re.split(r"[,\sx]+", spec.strip()) if v]
                if len(parts) != 4:
                    raise ValueError(f"--wm-region 格式应为 x,y,w,h，收到: {spec}")
                out.append({"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]})
            return out
        if opts.wm_regions == "none":
            return []
        if self.watermark_regions:
            self.log(f"  （水印区域来自扫描结果，共 {len(self.watermark_regions)} 个）")
            return [{"x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]}
                    for r in self.watermark_regions]
        self.log("  ! 没有水印坐标：请先跑扫描，或用 --wm-region x,y,w,h 指定")
        return []

    # ---------------- 内部：ffmpeg 调用 ----------------
    async def _remux(self, src: str, dst: str) -> Tuple[bool, str]:
        # 普通重封装：-map 0 全搬，元数据原样复制。
        # --clean：只留「真画面 + 声音」——
        #   · `0:V` 是所有**非附加图片**的视频流（自动排除 attached_pic 封面图/缩略图）
        #   · 不映射字幕/data/timecode/hint/附件轨
        #   · -map_metadata -1 丢掉全部容器元数据，-map_chapters -1 丢章节
        if self.opts.clean:
            maps = ["-map", "0:V?", "-map", "0:a?"]
            meta = ["-map_metadata", "-1", "-map_chapters", "-1"]
        else:
            maps = ["-map", "0"]
            meta = ["-map_metadata", "0"]
        cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-i", src] + maps + \
              ["-c", "copy"] + meta
        if self.opts.threads:
            cmd += ["-threads", str(self.opts.threads)]
        if self.opts.faststart:
            cmd += ["-movflags", "+faststart"]
        cmd.append(dst)
        rc, _o, err = await _F.run_cmd(cmd, priority=self.opts.priority)
        return rc == 0, err.decode("utf-8", "replace")

    async def _reencode(self, src: str, dst: str, regions: List[Dict[str, int]],
                        W: int, H: int) -> Tuple[bool, str]:
        opts = self.opts
        fc, vout = build_watermark_filter(regions, opts.wm, W, H, opts.wm_color,
                                          opts.wm_blur, opts.wm_mask_paths)
        cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-i", src,
               "-filter_complex", fc, "-map", f"[{vout}]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", opts.wm_preset, "-crf", str(opts.wm_crf),
               "-pix_fmt", "yuv420p", "-c:a", "copy"]
        cmd += (["-map_metadata", "-1", "-map_chapters", "-1"] if opts.clean
                else ["-map_metadata", "0"])
        if opts.threads:
            cmd += ["-threads", str(opts.threads)]
        if opts.wm_faststart:
            cmd += ["-movflags", "+faststart"]
        cmd.append(dst)
        rc, _o, err = await _F.run_cmd(cmd, priority=opts.priority)
        return rc == 0, err.decode("utf-8", "replace")
