# -*- coding: utf-8 -*-
"""MP4/MOV 容器结构解析：box 树、sample 表、字节区间、文件签名与指纹规则。

本模块只做「结构」相关的事，不做任何音视频解码，因此可以安全地在多个
线程里并行调用（纯 CPU、无共享可变状态）。
"""
from __future__ import annotations

import math
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
try:
    import numpy as np
except Exception:      # pragma: no cover - numpy 是声明依赖，这里只是兜底
    np = None

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .utils import HIGH, INFO, LOW, MED, human


CONTAINER_BOXES = {
    "moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "udta", "mvex", "moof",
    "traf", "mfra", "tref", "meco", "hnti", "hinf", "strk", "stri", "strd", "sinf",
    "schi", "clip", "matt", "tapt", "gmhd", "rmra", "rmda", "wave", "trgr", "mdri",
    "ipro", "sbtl", "nmhd", "gmhd", "ilst", "mvex", "mfra", "skip", "iods", "cslg",
}



KNOWN_TOP = {
    "ftyp", "styp", "moov", "mdat", "free", "skip", "wide", "pnot", "uuid", "moof",
    "mfra", "sidx", "ssix", "prft", "emsg", "meta", "junk", "pict", "PICT", "ftyp",
}



VISUAL_ENTRIES = {
    "avc1", "avc2", "avc3", "avc4", "hvc1", "hev1", "mp4v", "encv", "vp08", "vp09",
    "av01", "dvh1", "dvhe", "ap4h", "apch", "apcn", "apcs", "apco", "ap4x", "jpeg",
    "mjpa", "mjpb", "png ", "raw ", "yuv2", "v210", "mx3p", "mx4p", "mx5p", "mjp2",
}



AUDIO_ENTRIES = {
    "mp4a", "enca", "ac-3", "ec-3", "ac-4", "alac", "lpcm", "sowt", "twos", "fl32",
    "fl64", "in24", "in32", "ulaw", "alaw", "Opus", "opus", "fLaC", "dtsc", "dtsh",
    "dtse", "samr", "sawb", "mlpa",
}



TEXT_ENTRIES = {"tx3g", "text", "wvtt", "stpp", "sbtt", "c608", "c708", "mett", "metx", "urim"}



SUSPICIOUS_BOXES = {
    "junk", "pict", "PICT", "prtl", "triK", "TRIK", "albm", "Xtra", "XMP_", "cprt",
    "vndr", "hnti", "COLR", "chpl", "----", "mvex",
}



@dataclass
class Box:
    type: str
    start: int          # 文件内绝对起始偏移
    header: int         # 头长度（8 / 16 / 32）
    size: int           # 整盒长度
    uuid: bytes = b""
    children: List["Box"] = field(default_factory=list)
    path: str = ""
    note: str = ""
    sample_entry: bool = False

    @property
    def body(self) -> int:
        return self.start + self.header

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def is64(self) -> bool:
        return self.header >= 16



def parse_boxes(data: bytes, start: int, end: int, path: str = "", depth: int = 0,
                out: Optional[List[Box]] = None, anomalies: Optional[List[str]] = None) -> List[Box]:
    """递归解析 [start,end) 内的 box 列表。"""
    if out is None:
        out = []
    if anomalies is None:
        anomalies = []
    pos = start
    guard = 0
    while pos + 8 <= end:
        guard += 1
        if guard > 200000:
            anomalies.append(f"box 数量异常，在 {pos} 处停止解析")
            break
        size = struct.unpack_from(">I", data, pos)[0]
        raw_type = data[pos + 4:pos + 8]
        btype = raw_type.decode("latin-1")
        header = 8
        uuid = b""
        note = ""
        if size == 1:
            if pos + 16 > end:
                anomalies.append(f"offset {pos}: 64 位 box 头越界")
                break
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
            note = "size==0，延伸到末尾"
            anomalies.append(f"{path}/{btype} @{pos}: size==0（延伸到段尾）")
        if btype == "uuid":
            uuid = data[pos + header:pos + header + 16]
            header += 16
            note = (note + " " if note else "") + f"uuid={uuid.hex()}"
        if size < header or pos + size > end:
            anomalies.append(
                f"{path}/{btype} @{pos}: 非法 size={size}（头={header}, 段尾={end}），可能结构损坏或被刻意混淆"
            )
            break
        box = Box(btype, pos, header, size, uuid, [], f"{path}/{btype}", note)
        out.append(box)
        if depth < 12:
            if btype == "meta":
                # meta 通常是 fullbox（4 字节 version/flags），QuickTime 有时不是
                p = box.body
                looks_full = data[p:p + 4] == b"\x00\x00\x00\x00" and data[p + 4:p + 8] in (
                    b"hdlr", b"keys", b"ilst", b"free", b"mhdr",
                )
                child_start = p + 4 if looks_full else p
                if not looks_full:
                    # 也可能是 (version=0, flags=0) 恰好后面跟 hdlr
                    if data[p + 4:p + 8] == b"hdlr" or data[p + 8:p + 12] == b"hdlr":
                        child_start = p + 4
                box.children = parse_boxes(data, child_start, box.end, box.path, depth + 1, [], anomalies)
            elif btype in CONTAINER_BOXES:
                box.children = parse_boxes(data, box.body, box.end, box.path, depth + 1, [], anomalies)
            elif btype == "ilst":
                box.children = parse_boxes(data, box.body, box.end, box.path, depth + 1, [], anomalies)
            elif path.endswith("/ilst") or btype == "----" or btype.startswith("\xa9"):
                # iTunes/mdta 元数据项：键名可能是数字序号(\x00\x00\x00\x01)或 ©xxx / ----，
                # 里面装着 data / mean / name 子盒。不解析它就拿不到值的真实偏移。
                box.children = parse_boxes(data, box.body, box.end, box.path, depth + 1, [], anomalies)
            elif btype == "stsd":
                box.children = parse_stsd(data, box, depth, anomalies)
            elif btype in ("mp4a", "avc1", "avc3", "hvc1", "hev1", "mp4v", "enca", "encv",
                           "alac", "Opus", "fLaC", "tx3g", "wvtt", "stpp", "ac-3", "ec-3"):
                skip = 0
                if btype in VISUAL_ENTRIES:
                    skip = 78
                elif btype in AUDIO_ENTRIES:
                    ver = struct.unpack_from(">H", data, box.body + 8)[0]
                    skip = 28 + (16 if ver == 1 else 36 if ver == 2 else 0)
                elif btype in TEXT_ENTRIES:
                    skip = 8
                if box.body + skip < box.end:
                    box.sample_entry = True
                    box.children = parse_boxes(data, box.body + skip, box.end, box.path, depth + 1, [], anomalies)
        pos += size
    return out



def parse_stsd(data: bytes, box: Box, depth: int, anomalies: List[str]) -> List[Box]:
    p = box.body + 4                      # version/flags
    count = struct.unpack_from(">I", data, p)[0]
    p += 4
    out: List[Box] = []
    for _ in range(min(count, 64)):
        if p + 8 > box.end:
            anomalies.append(f"{box.path}: 声明的样本条目数 {count} 超出盒边界")
            break
        size = struct.unpack_from(">I", data, p)[0]
        t = data[p + 4:p + 8].decode("latin-1")
        if size < 8 or p + size > box.end:
            anomalies.append(f"{box.path}/{t} @{p}: 样本条目 size={size} 非法")
            break
        sub = Box(t, p, 8, size, b"", [], f"{box.path}/{t}")
        skip = 0
        if t in VISUAL_ENTRIES:
            skip = 78
        elif t in AUDIO_ENTRIES:
            ver = struct.unpack_from(">H", data, sub.body + 8)[0]
            skip = 28 + (16 if ver == 1 else 36 if ver == 2 else 0)
        elif t in TEXT_ENTRIES:
            skip = 8
        if sub.body + skip < sub.end and depth < 12:
            sub.children = parse_boxes(data, sub.body + skip, sub.end, sub.path, depth + 1, [], anomalies)
        out.append(sub)
        p += size
    return out



def walk(boxes: Iterable[Box]) -> Iterable[Box]:
    for b in boxes:
        yield b
        yield from walk(b.children)



def find_all(boxes: Sequence[Box], btype: str) -> List[Box]:
    return [b for b in walk(boxes) if b.type == btype]



def box_tree_text(boxes: Sequence[Box], depth: int = 0, limit: int = 400) -> List[str]:
    lines: List[str] = []
    for b in boxes:
        if len(lines) > limit:
            lines.append(" " * depth + "...（省略）")
            return lines
        name = b.type if b.type.isprintable() else repr(b.type)
        extra = ""
        if b.uuid:
            extra += f" uuid={b.uuid.hex()}"
        if b.note:
            extra += f" [{b.note}]"
        if b.type in ("mdat", "free", "skip", "wide") or b.type == "uuid":
            pass
        lines.append(f"{'  ' * depth}{name:<12} off=0x{b.start:08x} size={b.size:<10} ({human(b.size)}){extra}")
        if b.children:
            lines += box_tree_text(b.children, depth + 1, limit - len(lines))
    return lines



# sample 数超过这个量才切到 numpy：numpy 有约 4~5ms 固定开销，
# 几千个 sample 时反而比纯 Python 慢，长视频（几万~几十万）才有压倒性优势。
STBL_NUMPY_MIN = 20000


def sample_arrays(info: Dict[str, Any]):
    """把 read_stbl 的结果拆成 (offsets, sizes) 两个一维 numpy 数组。

    ``info["samples"]`` 现在是 (n,2) 的 numpy 数组（同样的信息只占元组列表的 1/8），
    但两种形态都能拆，所以调用方不用关心。
    """
    smp = info.get("samples")
    if np is None:
        return [], []
    if smp is None or len(smp) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    a = np.asarray(smp, dtype=np.int64)
    if a.ndim != 2 or a.shape[1] < 2:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return a[:, 0], a[:, 1]


def read_stbl(data: bytes, boxes: Sequence[Box]) -> Dict[str, Any]:
    """从 stbl 里读出每个 sample 的 (offset, size)，用于计算 mdat 覆盖情况。"""
    def one(t: str) -> Optional[Box]:
        r = [b for b in boxes if b.type == t]
        return r[0] if r else None

    info: Dict[str, Any] = {}
    stsz = one("stsz")
    stz2 = one("stz2")
    stsc = one("stsc")
    stco = one("stco") or one("co64")
    sizes: List[int] = []
    try:
        if stsz is not None:
            p = stsz.body + 4
            sample_size, count = struct.unpack_from(">II", data, p)
            p += 8
            if sample_size:
                sizes = [sample_size] * count
            else:
                avail = (stsz.end - p) // 4
                n = min(count, avail)
                if n != count:
                    info["stsz_truncated"] = (count, n)
                sizes = list(struct.unpack_from(f">{n}I", data, p))
        elif stz2 is not None:
            p = stz2.body + 4
            p += 3
            field = data[p]
            count = struct.unpack_from(">I", data, p + 1)[0]
            p += 5
            if field == 4:
                for i in range(count):
                    byte = data[p + i // 2]
                    sizes.append((byte >> 4) if i % 2 == 0 else (byte & 0xF))
            elif field == 8:
                sizes = list(data[p:p + count])
            elif field == 16:
                sizes = list(struct.unpack_from(f">{count}H", data, p))
        if stsc is not None:
            p = stsc.body + 4
            n = struct.unpack_from(">I", data, p)[0]
            p += 4
            entries = [struct.unpack_from(">III", data, p + 12 * i) for i in range(n)]
        else:
            entries = []
        if stco is not None:
            p = stco.body + 4
            n = struct.unpack_from(">I", data, p)[0]
            p += 4
            if stco.type == "stco":
                chunks = list(struct.unpack_from(f">{n}I", data, p))
            else:
                chunks = list(struct.unpack_from(f">{n}Q", data, p))
        else:
            chunks = []
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    info.update(sizes=sizes, entries=entries, chunks=chunks)
    samples: Any = []
    n_placed = 0
    tbytes = 0
    nsamp = len(sizes)
    # 原实现是「每个 chunk × 每个 sample」的三重 Python 循环，
    # 2 小时视频（52 万个 sample）光拼这个列表就要几秒、还要 60MB 元组。
    # 这里改成向量化：sample 的顺序 = chunk 顺序 + chunk 内顺序，
    # 偏移 = 该 chunk 起始偏移 + chunk 内 sizes 的前缀和。
    # 小文件继续用纯 Python：numpy 有约 4~5ms 的固定开销，
    # 几千个 sample 时反而更慢（实测 5387 个：Python 2ms / numpy 6ms）。
    # sample 数一多（长视频）才有压倒性优势，所以按数量切换。
    _use_np = np is not None and len(sizes) >= STBL_NUMPY_MIN
    if _use_np and entries and chunks and sizes:
        ent = []
        for e in entries:                     # first<1 之后的条目按原语义直接丢弃
            if e[0] < 1:
                break
            ent.append(e)
        if ent:
            firsts = np.array([e[0] for e in ent], np.int64)
            spcs = np.array([e[1] for e in ent], np.int64)
            nxt = np.append(firsts[1:], len(chunks) + 1)
            c0 = firsts
            c1 = np.minimum(nxt, len(chunks) + 1)
            cnt = np.maximum(0, c1 - c0) * spcs
            total = int(cnt.sum())
            if total > nsamp:                 # 样本不够就在样本级别截断
                acc = np.cumsum(cnt)
                keep = int(np.searchsorted(acc, nsamp, side="right"))
                c0, c1, spcs = c0[:keep + 1], c1[:keep + 1], spcs[:keep + 1]
                cnt = cnt[:keep + 1].copy()
                cnt[-1] = nsamp - int(cnt[:-1].sum())
                total = nsamp
            if total > 0:
                chunk_ids = np.concatenate([
                    np.repeat(np.arange(c0[i], c1[i], dtype=np.int64), int(spcs[i]))
                    for i in range(len(cnt)) if cnt[i] > 0])[:total]
                szs = np.asarray(sizes, np.int64)[:total]
                cs = np.concatenate([[0], np.cumsum(szs)])
                first_i = np.searchsorted(chunk_ids, chunk_ids, side="left")
                # chunk_ids 是 stsc 里的 1-based chunk 号，索引 chunks 时要减 1
                # cs[k] = sizes[0:k] 的前缀和 → 第 i 个 sample 在块内的偏移是 cs[i]-cs[first_i]
                offs = np.asarray(chunks, np.int64)[chunk_ids - 1] + cs[:-1] - cs[first_i]
                samples = np.column_stack([offs, szs])
                n_placed = total
                tbytes = int(szs.sum())
        if n_placed < nsamp:
            info["unreachable_samples"] = nsamp - n_placed
        info["n_samples"] = n_placed
        info["total_bytes"] = tbytes
        if n_placed:
            info["samples"] = samples
        return info
    # --- numpy 不可用时的旧实现（保底） ---
    if entries and chunks and sizes:
        si = 0
        for ei, (first, spc, _sdi) in enumerate(entries):
            nxt = entries[ei + 1][0] if ei + 1 < len(entries) else len(chunks) + 1
            if first < 1:
                break
            for ci in range(first, min(nxt, len(chunks) + 1)):
                base = chunks[ci - 1]
                off = 0
                for _k in range(spc):
                    if si >= nsamp:
                        break
                    samples.append((base + off, sizes[si]))
                    off += sizes[si]
                    si += 1
                if si >= nsamp:
                    break
            if si >= nsamp:
                break
        n_placed = si
        info["n_samples"] = si
        if si < nsamp:
            info["unreachable_samples"] = nsamp - si
    info["samples"] = samples
    info["total_bytes"] = sum(s for _, s in samples)
    return info



def merge_intervals(iv) -> List[Tuple[int, int]]:
    """合并区间。输入可以是 list[(a,b)]，也可以是 (n,2) 的 numpy 数组。

    输出**始终是 list[tuple]**：合并之后区间数量通常只剩几十上百个，
    转回列表几乎不花代价，而下游（gaps_in / 各处 for a,b 循环）一行都不用改。
    输入很大的时候走 numpy，排序+合并都在 C 里做。
    """
    if np is not None:
        try:
            n = len(iv)
        except TypeError:
            n = 0
        if n >= 2048:
            a = np.asarray(iv, dtype=np.int64)
            if a.ndim == 2 and a.shape[1] >= 2:
                a = a[a[:, 1] > 0]
                if not len(a):
                    return []
                a = a[np.argsort(a[:, 0], kind="stable")]
                starts, ends = a[:, 0], a[:, 1]
                # 累积最大 end：下一个 start 比它还大，就说明要另起一段
                cummax = np.maximum.accumulate(ends)
                newgrp = np.empty(len(a), bool)
                newgrp[0] = True
                np.greater(starts[1:], cummax[:-1], out=newgrp[1:])
                idx = np.flatnonzero(newgrp)
                out_a = starts[idx]
                out_b = np.maximum.reduceat(ends, idx)
                return list(zip(out_a.tolist(), out_b.tolist()))
    iv = sorted((a, b) for a, b in iv if b > 0)
    out: List[Tuple[int, int]] = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out



def gaps_in(ranges: List[Tuple[int, int]], lo: int, hi: int) -> List[Tuple[int, int]]:
    """[lo,hi) 里没有被 ranges 覆盖的区间"""
    cov = merge_intervals(ranges)
    out: List[Tuple[int, int]] = []
    cur = lo
    for a, b in cov:
        if b <= lo:
            continue
        if a >= hi:
            break
        if a > cur:
            out.append((cur, min(a, hi)))
        cur = max(cur, b)
        if cur >= hi:
            break
    if cur < hi:
        out.append((cur, hi))
    return [(a, b) for a, b in out if b > a]



def complement(ranges: List[Tuple[int, int]], lo: int, hi: int) -> List[Tuple[int, int]]:
    """ranges 在 [lo,hi) 中的补集"""
    return gaps_in(ranges, lo, hi)



MAGICS: List[Tuple[bytes, str, int]] = [
    (b"PK\x03\x04", "ZIP / JAR / DOCX", 0),
    (b"PK\x05\x06", "ZIP (空)", 0),
    (b"Rar!\x1a\x07", "RAR", 0),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip", 0),
    (b"\x1f\x8b\x08", "gzip", 0),
    (b"BZh9", "bzip2", 0),
    (b"\xfd7zXZ\x00", "xz", 0),
    (b"(\xb5/\xfd", "zstd", 0),
    (b"%PDF-", "PDF", 0),
    (b"\x89PNG\r\n\x1a\n", "PNG", 0),
    (b"\xff\xd8\xff\xe0", "JPEG", 0),
    (b"\xff\xd8\xff\xe1", "JPEG", 0),
    (b"GIF89a", "GIF", 0),
    (b"OggS", "Ogg", 0),
    (b"fLaC", "FLAC", 0),
    (b"\x7fELF", "ELF 可执行文件", 0),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat", 0),
    (b"SQLite format 3\x00", "SQLite 数据库", 0),
    (b"-----BEGIN PGP", "PGP 消息", 0),
    (b"-----BEGIN RSA", "RSA 私钥", 0),
    (b"-----BEGIN OPENSSH", "OpenSSH 私钥", 0),
    (b"ssh-rsa ", "SSH 公钥", 0),
    (b"{\\rtf", "RTF", 0),
    (b"\xd0\xcf\x11\xe0", "OLE (doc/xls)", 0),
    (b"ustar", "TAR", 257),
]



WEAK_MAGICS = {"MZ", "BM", "ID3"}



BASE64_RE = re.compile(rb"^[A-Za-z0-9+/=_-]{24,}$")



HEX_RE = re.compile(rb"^[0-9a-fA-F]{16,}$")



URL_RE = re.compile(rb"https?://[^\s\"'<>\x00-\x1f]{6,200}")



EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,10}")



IP_RE = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")



UUID_RE = re.compile(rb"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")



PRINT_RE = re.compile(rb"[\x20-\x7e]{7,}")



UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")



KNOWN_SEI_UUIDS: Dict[str, str] = {
    "dc45e9bde6d948b7962cd820d923eeef":
        "x264/x265 系编码器写「版本信息」用的公共 UUID（实测 libx264 在此 UUID 下写出 "
        "`x264 - core 164 r3108 ... - options: cabac=...`）。payload 是编码器自报的版本/参数串，"
        "通常只在码流开头出现一次，属于编码器指纹，不是逐帧用户水印。"
        "若 payload 是短串（如 `bvc0ot v2.2.1.3-20250220`），说明用的是基于 x264 定制/封装的编码器。",
}



FINGERPRINTS: List[Tuple[str, bytes, str]] = [
    # 不能要求 vid: 后面必须跟 v —— 修复侧的判定是 (?i)\bvid\s*:，
    # 只认 v 开头会让 vid:XXX 这类值「扫描不报、修复却清掉」。
    ("Douyin/TikTok 视频 ID (vid:)", rb"(?i)\bvid\s*:\s*[0-9a-zA-Z_\-]{6,}", HIGH),
    ("Douyin aweme_id", rb"aweme[_a-z]*[=\":\s]{1,3}\d{15,}", HIGH),
    ("ByteDance 相关", rb"(?i)bytedance|toutiao|douyin|tiktok|musical\.ly|snssdk", HIGH),
    ("TikTok/Douyin 设备标识", rb"(?i)(ttwid|sec_uid|device_id|openudid|odin_tt|snssdk)", HIGH),
    ("用户/账号 ID 字段", rb"(?i)(user_?id|uid|account_?id|author_?id|owner_?id)[=:\"\s]{1,3}[0-9a-zA-Z_\-]{6,}", MED),
    ("会话/追踪 token", rb"(?i)(token|session|secret|apikey|api_key|access_key|bearer)[=:\"\s]{1,3}[0-9A-Za-z_\-\.]{12,}", MED),
    ("水印文本", rb"(?i)(watermark|wm_|logo_|brand_|stamp)", LOW),
    ("FFmpeg 版本串", rb"Lavf\d+\.\d+\.\d+|Lavc\d+\.\d+\.\d+", INFO),
    ("Adobe/其他编辑器", rb"(?i)Premiere|After Effects|Adobe|DaVinci|CapCut|JianyingPro", INFO),
    ("标准 ID3/XMP 元数据块", rb"(?i)<\?xpacket|xmpmeta|photoshop:", INFO),
    ("C2PA / JUMBF 内容凭证标记", rb"(?i)c2pa|jumbf", HIGH),
    ("DRM 保护系统名称", rb"(?i)widevine|playready|fairplay|clearkey|marlin", MED),
]



def scan_magics(data: bytes) -> List[Tuple[int, str, int]]:
    out = []
    for magic, name, pad in MAGICS:
        start = 0
        cnt = 0
        while cnt < 8:
            i = data.find(magic, start)
            if i < 0:
                break
            out.append((i - pad if i >= pad else i, name, len(magic)))
            start = i + 1
            cnt += 1
    out.sort()
    return out



def offset_in_segments(off: int, segs: Sequence[Tuple[int, int]]) -> int:
    """把拼接 blob 里的偏移换算回文件内偏移"""
    cur = 0
    for a, b in segs:
        n = b - a
        if off < cur + n:
            return a + (off - cur)
        cur += n
    return off



def validate_magic(data: bytes, off: int, name: str) -> bool:
    """对短签名做二次结构校验，压掉巧合命中"""
    try:
        if name == "PNG":
            # PNG 签名是 8 字节 \x89PNG\r\n\x1a\n；IHDR 块紧跟其后：
            # [len(4)][IHDR]。原实现把第二段写成 data[off+8:off+12]（那其实是
            # IHDR 的长度字段 00 00 00 0D），导致**任何** PNG 都被判为不可信。
            return (data[off:off + 8] == b"\x89PNG\r\n\x1a\n"
                    and data[off + 12:off + 16] == b"IHDR")
        if name == "JPEG":
            return b"\xff\xd9" in data[off + 2:off + 262144]
        if name.startswith("ZIP"):
            if off + 30 > len(data):
                return False
            ver, _flags, method = struct.unpack_from("<HHH", data, off + 4)
            return method in (0, 8, 9, 12, 14, 93, 95, 98) and 0 < ver <= 63
        if name == "gzip":
            return data[off + 3] == 0x08
        if name == "PDF":
            return data[off:off + 1024].startswith(b"%PDF-1.")
        if name == "SQLite 数据库":
            hdr = data[off:off + 100]
            return len(hdr) >= 100 and hdr[16:18] == b"\x01\x00"
        if name == "Ogg":
            return data[off + 4:off + 5] == b"\x00" and data[off + 5] in (2, 4, 6)
        if name == "FLAC":
            return data[off + 4:off + 5] == b"\x00" or data[off + 4] in (0x80, 0x00)
        if name == "ELF":
            return data[off + 4] in (1, 2) and data[off + 5] in (1, 2)
        if name == "7-Zip":
            return len(data) > off + 8
        if name == "RAR":
            return data[off + 6:off + 7] in (b"\x00", b"\x01")
        return len(name) > 3
    except Exception:
        return False



def is_interesting_string(s: str) -> bool:
    if len(s) < 7:
        return False
    if re.search(r"(?i)http|ftp|www\.|\.com|\.cn|\.net|\.org|\.ru|\.io", s):
        return True
    if re.search(r"(?i)vid:|aweme|douyin|tiktok|bytedance|watermark|token|secret|passw|key=|uid|user", s):
        return True
    if re.search(r"\d{10,}", s):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{16,}", s):
        return True
    if re.search(r"(?i)\.(mp4|mov|jpg|png|json|xml|txt|db|log|zip)$", s):
        return True
    if re.search(r"[A-Za-z0-9+/]{28,}={0,2}", s) and re.search(r"[A-Z]", s) and re.search(r"[a-z]", s):
        return True
    if re.match(r"^[A-Za-z]:\\\\|^/[\w/\.\-]{6,}", s):
        return True
    return False



def fingerprint_scan(blob: bytes) -> List[Tuple[str, str, List[str]]]:
    out = []
    for name, rx, sev in FINGERPRINTS:
        try:
            ms = re.findall(rx, blob)
        except re.error:
            continue
        if not ms:
            continue
        samples = []
        for m in ms[:5]:
            s = m.decode("utf-8", "replace") if isinstance(m, (bytes, bytearray)) else str(m)
            samples.append(s[:160])
        out.append((name, sev, samples))
    return out



def decode_ilst(dtype: int, payload: bytes) -> str:
    base = dtype & 0xFFFFFF
    try:
        if base == 1:
            return payload.decode("utf-8", "replace")
        if base == 2:
            return payload.decode("utf-16-be", "replace")
        if base == 21:
            return f"<int {int.from_bytes(payload[:4], 'big')}>"
        if base == 22:
            return f"<uint {int.from_bytes(payload[:4], 'big')}>"
        if base == 0:
            return f"<binary {len(payload)}B> {payload[:64].hex()}"
        if base == 13:
            return f"<jpeg {len(payload)}B>"
        if base == 14:
            return f"<png {len(payload)}B>"
        if base == 27:
            return f"<bmp {len(payload)}B>"
        return f"<type{base} {len(payload)}B> {payload[:64].hex()}"
    except Exception:
        return f"<decode error {len(payload)}B>"
