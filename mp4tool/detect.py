# -*- coding: utf-8 -*-
"""感知层检测算法：水印图层、矩形码、音频频谱、SEI/SPS 解析。

这些函数都是「纯计算 + 一次性的 ffmpeg 调用」，重活会通过 asyncio.to_thread
丢到线程池，避免阻塞事件循环。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

from .utils import have, run


def parse_sei(payload: bytes, sample_idx: int, offset: int) -> List[Dict[str, Any]]:
    """解析 SEI RBSP（已去掉 NAL 头字节）"""
    recs = []
    i = 0
    guard = 0
    n = len(payload)
    while i < n and guard < 64:
        guard += 1
        ptype = 0
        while i < n and payload[i] == 0xFF:
            ptype += 255
            i += 1
        if i >= n:
            break
        ptype += payload[i]
        i += 1
        psize = 0
        while i < n and payload[i] == 0xFF:
            psize += 255
            i += 1
        if i >= n:
            break
        psize += payload[i]
        i += 1
        body = payload[i:i + psize]
        i += psize
        rec: Dict[str, Any] = {"type": ptype, "size": psize, "offset": offset,
                               "payload_hex": body[:256].hex()}
        if ptype == 5 and len(body) >= 16:
            rec["uuid"] = body[:16].hex()
            rest = body[16:]
            rec["payload_hex"] = rest[:256].hex()
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in rest[:200])
            rec["payload_text"] = txt
        elif ptype == 4 and len(body) >= 16:
            rec["uuid"] = body[:16].hex()
            rec["payload_hex"] = body[16:256].hex()
        elif ptype == 1:
            rec["payload_text"] = "picture timing"
        elif ptype == 6:
            rec["payload_hex"] = body[:16].hex()
            rec["payload_text"] = "recovery point"
        elif ptype in (47, 137):
            rec["payload_text"] = "payload " + body[:200].hex()
        recs.append(rec)
    return recs



def parse_sps_dimensions(sps: bytes) -> Dict[str, Any]:
    """按 H.264 规范解析 SPS，取真正的编码分辨率/profile/level。

    这一段值得写对：把它和容器声明的分辨率一比，就能发现“改过宽高/裁剪过画面”的文件。
    """
    if len(sps) < 4:
        return {}
    profile = sps[1]
    level = sps[3]
    # 去掉 emulation prevention 字节 0x000003
    rb = bytearray()
    zeros = 0
    for b in sps[1:]:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        rb.append(b)
        zeros = zeros + 1 if b == 0 else 0
    bits = "".join(f"{b:08b}" for b in rb)
    pos = [0]

    def u(n: int) -> int:
        v = bits[pos[0]:pos[0] + n]
        pos[0] += n
        return int(v, 2) if v else 0

    def bit() -> int:
        v = bits[pos[0]] if pos[0] < len(bits) else "0"
        pos[0] += 1
        return int(v)

    def ue() -> int:
        z = 0
        while pos[0] < len(bits) and bits[pos[0]] == "0":
            z += 1
            pos[0] += 1
        pos[0] += 1
        if z == 0:
            return 0
        v = bits[pos[0]:pos[0] + z]
        pos[0] += z
        return (1 << z) - 1 + (int(v, 2) if v else 0)

    def se() -> int:
        k = ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)

    try:
        profile = u(8)
        u(8)                       # constraint_set flags + reserved
        level = u(8)
        ue()                       # seq_parameter_set_id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = ue()
            if chroma == 3:
                bit()
            ue()                   # bit_depth_luma_minus8
            ue()                   # bit_depth_chroma_minus8
            bit()                  # qpprime_y_zero_transform_bypass_flag
            if bit():              # seq_scaling_matrix_present_flag
                for i in range(8 if chroma != 3 else 12):
                    if bit():
                        size = 16 if i < 6 else 64
                        last, nxt = 8, 8
                        for _ in range(size):
                            if nxt != 0:
                                nxt = (last + se() + 256) % 256
                            last = nxt if nxt != 0 else last
        ue()                       # log2_max_frame_num_minus4
        poc = ue()
        if poc == 0:
            ue()                   # log2_max_pic_order_cnt_lsb_minus4
        elif poc == 1:
            bit()                  # delta_pic_order_always_zero_flag
            se()
            se()
            for _ in range(ue()):
                se()
        ue()                       # max_num_ref_frames
        bit()                      # gaps_in_frame_num_value_allowed_flag
        w_mbs = ue() + 1
        h_map = ue() + 1
        frame_mbs_only = bit()
        if not frame_mbs_only:
            bit()                  # mb_adaptive_frame_field_flag
        bit()                      # direct_8x8_inference_flag
        cl = cr = ct = cb = 0
        if bit():                  # frame_cropping_flag
            cl, cr, ct, cb = ue(), ue(), ue(), ue()
        sub_w = 1 if chroma in (0, 3) else 2                            # SubWidthC
        sub_h = (2 if chroma in (1, 2) else 1) * (2 - frame_mbs_only)   # CropUnitY
        width = w_mbs * 16 - (cl + cr) * sub_w
        height = h_map * 16 * (2 - frame_mbs_only) - (ct + cb) * sub_h
        return {"profile_idc": profile, "level_idc": level, "chroma_format_idc": chroma,
                "width": width, "height": height, "frame_mbs_only": frame_mbs_only,
                "crop": [cl, cr, ct, cb], "bit_offset_used": pos[0]}
    except Exception:
        return {"profile_idc": profile, "level_idc": level}



def _block_mean(a: "np.ndarray", rows: int, cols: int) -> "np.ndarray":
    """把二维数组块平均重采样到 (rows, cols)，保持几何比例。"""
    H, W = a.shape
    rows = max(1, min(rows, H))
    cols = max(1, min(cols, W))
    ys = np.linspace(0, H, rows + 1).astype(int)
    xs = np.linspace(0, W, cols + 1).astype(int)
    ys[-1], xs[-1] = H, W
    tmp = np.add.reduceat(a, ys[:-1], axis=0) / np.maximum(1, np.diff(ys))[:, None]
    tmp = np.add.reduceat(tmp, xs[:-1], axis=1) / np.maximum(1, np.diff(xs))[None, :]
    return tmp



def ascii_art(a: "np.ndarray", cols: int = 160) -> str:
    """把灰度图渲染成 ASCII，方便在纯文本报告里直接辨认水印文字/图案。

    终端字符高约为宽的两倍，所以纵向采样步长取横向的两倍，图形才不会变形。
    """
    if np is None:
        return ""
    a = np.asarray(a, dtype=np.float32)
    if a.ndim != 2 or a.size == 0:
        return ""
    cols = max(8, min(cols, a.shape[1]))
    cell = a.shape[1] / float(cols)                 # 一个字符横向代表的像素数
    rows = max(1, min(a.shape[0], int(round(a.shape[0] / max(1e-6, cell * 2.0)))))
    small = _block_mean(a, rows, cols)
    lo = float(np.percentile(small, 2))
    hi = float(np.percentile(small, 98))
    rng = max(1e-6, hi - lo)
    ramp = " .:-=+*#%@"
    return "\n".join("".join(ramp[min(9, int((v - lo) / rng * 9.999))] for v in row) for row in small)



def ocr_available_langs() -> str:
    """返回可用的 tesseract 语言组合；没有 chi_sim 就说明中文水印识别不出来。"""
    if not have("tesseract"):
        return ""
    try:
        langs = run(["tesseract", "--list-langs"], binary=False)
        return "chi_sim+eng" if "chi_sim" in langs else "eng"
    except Exception:
        return ""



def ocr_image(img: "np.ndarray") -> str:
    """可选 OCR：有 tesseract 就跑，有中文包就用中文。失败就返回空串。"""
    if np is None or not have("tesseract") or Image is None:
        return ""
    try:
        use = ocr_available_langs() or "eng"
        os.makedirs(os.path.join(tempfile.gettempdir(), "mp4scan_ocr"), exist_ok=True)
        p = os.path.join(tempfile.gettempdir(), "mp4scan_ocr", "wm.png")
        im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
        im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
        im.save(p)
        best = ""
        for psm in ("6", "7", "11"):
            r = subprocess.run(["tesseract", p, "stdout", "-l", use, "--psm", psm],
                               capture_output=True)
            out = " ".join(r.stdout.decode("utf-8", "replace").split())
            if len(out) > len(best):
                best = out
        if use == "eng":
            best += "  [环境里没有 chi_sim 中文包，中文水印无法被正确识别]"
        return best
    except Exception:
        return ""



def connected_boxes(mask: "np.ndarray", min_area: int = 20, max_boxes: int = 12) -> List[Tuple[int, int, int, int, int]]:
    """用 scipy.ndimage 若可用，否则用简单连通域（按行扫描 union-find 太慢，改用降采样 + 网格聚合）。"""
    try:
        from scipy import ndimage  # type: ignore
        lab, n = ndimage.label(mask)
        out = []
        for sl in ndimage.find_objects(lab):
            ys, xs = sl
            area = int(mask[sl].sum())
            if area >= min_area:
                out.append((xs.start, ys.start, xs.stop, ys.stop, area))
        out.sort(key=lambda r: -r[4])
        return out[:max_boxes]
    except Exception:
        pass
    # 无 scipy：合并成粗网格块
    boxes: List[List[int]] = []
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return []
    used = np.zeros(ys.size, bool)
    order = np.argsort(ys * mask.shape[1] + xs)
    ys, xs = ys[order], xs[order]
    for i in range(ys.size):
        if used[i]:
            continue
        y0 = y1 = int(ys[i]); x0 = x1 = int(xs[i])
        cnt = 1
        j = i + 1
        while j < ys.size and ys[j] <= y1 + 24:
            if used[j] or xs[j] < x0 - 60 or xs[j] > x1 + 60:
                j += 1
                continue
            used[j] = True
            y1 = max(y1, int(ys[j])); x1 = max(x1, int(xs[j]))
            x0 = min(x0, int(xs[j])); y0 = min(y0, int(ys[j]))
            cnt += 1
            j += 1
        if cnt >= min_area:
            boxes.append([x0, y0, x1 + 1, y1 + 1, cnt])
    boxes.sort(key=lambda b: -b[4])
    return [tuple(b) for b in boxes[:max_boxes]]



def binarize_local(img: "np.ndarray", win: int = 25, k: float = 0.10) -> "np.ndarray":
    """局部均值（积分图）自适应二值化，返回 True=暗 的布尔数组。

    直接用全局阈值在光照不均 / 半透明叠加的场合会失效，所以按窗口均值比较。
    """
    a = np.asarray(img, dtype=np.float32)
    pad = win // 2
    ap = np.pad(a, pad, mode="reflect")
    ii = np.cumsum(np.cumsum(ap, axis=0), axis=1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    H, W = a.shape
    ys = np.arange(H)
    xs = np.arange(W)
    S = (ii[np.ix_(ys + win, xs + win)] - ii[np.ix_(ys, xs + win)]
         - ii[np.ix_(ys + win, xs)] + ii[np.ix_(ys, xs)])
    mean = S / float(win * win)
    return a < mean * (1.0 - k)



def _runs_of(row: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
    b = np.flatnonzero(row[1:] != row[:-1]) + 1
    bounds = np.concatenate(([0], b, [row.size]))
    return bounds[:-1], np.diff(bounds), row[bounds[:-1]]



def _finder_rows(bw: "np.ndarray", row_step: int = 3) -> List[Tuple[int, float, float]]:
    """行扫描找 1:1:3:1:1 (暗亮暗亮暗) 的定位图案候选，返回 (y, cx, module)。"""
    from numpy.lib.stride_tricks import sliding_window_view
    bw = np.asarray(bw).astype(bool)      # 必须二值化，否则布尔掩码会退化成整数索引
    out: List[Tuple[int, float, float]] = []
    target = np.array([1.0, 1.0, 3.0, 1.0, 1.0])
    for y in range(0, bw.shape[0], row_step):
        row = bw[y]
        if row.all() or not row.any():
            continue
        starts, runs, colors = _runs_of(row)
        if runs.size < 5:
            continue
        w = sliding_window_view(runs, 5).astype(np.float64)
        c = sliding_window_view(colors, 5)
        pat = (~c[:, 0]) & c[:, 1] & (~c[:, 2]) & c[:, 3] & (~c[:, 4])
        if not pat.any():
            continue
        w = w[pat]
        starts = starts[np.flatnonzero(pat)]
        unit = w.sum(axis=1) / 7.0
        ok = unit >= 1.0
        if not ok.any():
            continue
        w, starts, unit = w[ok], starts[ok], unit[ok]
        dev = np.abs(w / unit[:, None] - target)
        ok2 = dev.max(axis=1) < 0.55
        if not ok2.any():
            continue
        w, starts, unit = w[ok2], starts[ok2], unit[ok2]
        cent = starts + w[:, 0] + w[:, 1] + w[:, 2] / 2.0
        for cx, u in zip(cent, unit):
            out.append((y, float(cx), float(u)))
    return out



def _vcheck(bw: "np.ndarray", y: int, cx: int, u: float) -> bool:
    xi = int(round(cx))
    if xi < 0 or xi >= bw.shape[1]:
        return False
    col = bw[:, xi]
    y0 = max(0, int(y - 4 * u))
    y1 = min(bw.shape[0], int(y + 4 * u) + 1)
    seg = col[y0:y1]
    if seg.size < 5:
        return False
    _, runs, colors = _runs_of(seg)
    if runs.size < 5:
        return False
    target = np.array([1.0, 1.0, 3.0, 1.0, 1.0])
    for i in range(runs.size - 4):
        c = colors[i:i + 5]
        if c[0] or not c[1] or c[2] or not c[3] or c[4]:
            continue
        w = runs[i:i + 5].astype(np.float64)
        unit = w.sum() / 7.0
        if unit < 1.0:
            continue
        if np.abs(w / unit - target).max() < 0.6:
            return True
    return False



def find_finder_triples(bw: "np.ndarray") -> List[Tuple[float, float, float]]:
    """在一帧里找构成直角的 3 个定位图案，返回 [(x,y,module), ...]。"""
    bw = np.asarray(bw).astype(bool)
    cands = _finder_rows(bw, row_step=3)
    if len(cands) < 3:
        return []
    # 纵向校验 + 粗略聚类
    good = [(y, cx, u) for (y, cx, u) in cands if _vcheck(bw, y, cx, u)]
    if len(good) < 3:
        return []
    clusters: List[List[float]] = []
    for y, cx, u in good:
        placed = False
        for cl in clusters:
            if abs(cl[0] - cx) <= max(2, u * 1.5) and abs(cl[1] - y) <= max(2, u * 1.5) and \
               abs(cl[2] - u) <= max(1, u * 0.8):
                cl[0] = (cl[0] * cl[3] + cx) / (cl[3] + 1)
                cl[1] = (cl[1] * cl[3] + y) / (cl[3] + 1)
                cl[2] = (cl[2] * cl[3] + u) / (cl[3] + 1)
                cl[3] += 1
                placed = True
                break
        if not placed:
            clusters.append([cx, y, u, 1.0])
    pts = [(c[0], c[1], c[2]) for c in clusters if c[3] >= 2]
    if len(pts) < 3:
        return []
    if len(pts) > 12:
        pts = pts[:12]
    n = len(pts)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                ax, ay, au = pts[i]
                bx, by, _bu = pts[j]
                cx_, cy_, _cu = pts[k]
                v1 = (bx - ax, by - ay)
                v2 = (cx_ - ax, cy_ - ay)
                d1 = math.hypot(*v1); d2 = math.hypot(*v2)
                if d1 < 12 * au or d2 < 12 * au or d1 > 300 * au or d2 > 300 * au:
                    continue
                if abs(d1 - d2) > 0.25 * max(d1, d2):
                    continue
                dot = v1[0] * v2[0] + v1[1] * v2[1]
                cosv = dot / (d1 * d2 + 1e-9)
                if abs(cosv) > 0.25:
                    continue
                return [pts[i], pts[j], pts[k]]
    return []



def probe_video_size(path: str) -> Tuple[int, int]:
    """视频流声明的宽高。"""
    p = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json", path], binary=False))
    st = p["streams"][0]
    return int(st["width"]), int(st["height"])



def decode_gray(path: str, fps: float = 1.0, width: int = 360) -> Tuple["np.ndarray", int]:
    """解码成灰度帧数组 (n, h, w)，uint8。"""
    probe = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "json", path], binary=False))
    st = probe["streams"][0]
    W, H = int(st["width"]), int(st["height"])
    h = max(2, int(round(H * width / W / 2)) * 2)
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={width}:{h}",
               "-pix_fmt", "gray", "-f", "rawvideo", "-"])
    n = len(raw) // (width * h)
    if n == 0:
        return np.zeros((0, h, width), np.uint8), h
    a = np.frombuffer(raw[:n * width * h], dtype=np.uint8).reshape(n, h, width)
    return a.copy(), h




# ================================================================ 一次性帧分析
# 旧版对同一个视频解码了 4 次（signalstats 全帧 / 360w@1fps / 1080w@0.25fps /
# 640w@0.5fps），占了整个扫描 80% 的时间。这里改成一次解码同时喂给三个消费者：
#   · 全帧率的小尺寸 RGB 帧  → 逐帧亮度/饱和度（闪帧检测）
#   · 采样后的灰度帧          → 静态叠加层（水印）检测 + 图层提取
#   · 同一批采样帧            → 矩形码(QR)扫描
# 两个解码进程并发跑，总耗时约为旧版的 1/5。

FRAME_ANALYSIS_LUMA_WIDTH = 96      # 逐帧统计用的极小宽度（只要均值准确）
FRAME_ANALYSIS_WM_WIDTH = 512       # 水印/QR 采样宽度


@dataclass
class FrameAnalysis:
    """一次解码得到的全部画面层结论。"""
    n_frames: int = 0
    src_w: int = 0
    src_h: int = 0
    times: Any = None            # (n,) 全帧时间戳
    luma: Any = None             # (n,) 全帧平均亮度
    sat: Any = None              # (n,) 全帧平均饱和度
    wm_frames: Any = None        # (m,h,w) 采样灰度帧
    wm_times: Any = None         # (m,)
    wm_width: int = 0
    wm_height: int = 0
    qr_hits: List[Tuple[float, List[Tuple[float, float, float]]]] = field(default_factory=list)
    qr_scanned: int = 0
    error: str = ""

    def gradient_median(self):
        """梯度幅值的时间中位数 —— 运动画面的边缘被时间抹平，只剩静态叠加层。"""
        if np is None or self.wm_frames is None or self.wm_frames.shape[0] < 3:
            return None
        gs = []
        for f in self.wm_frames:
            gy, gx = np.gradient(f.astype(np.float32))
            gs.append(np.hypot(gx, gy))
        return np.median(np.stack(gs), axis=0)


def overlay_regions_from_frames(gray, src_w: int, src_h: int,
                                min_area_ratio: float = 0.0008,
                                max_boxes: int = 12,
                                downscale: int = 2) -> Dict[str, Any]:
    """从已解码的采样帧里找「每一帧都盖在同一位置」的静态叠加层。

    与旧版 detect_static_overlays 的算法完全一致（时间中位数 + MAD + 梯度结构），
    区别只是帧由调用方提供，不再自己解码。
    """
    out: Dict[str, Any] = {"regions": [], "boxes": [], "thr": 0.0,
                           "static": None, "struct": None, "median": None, "grad": None}
    if np is None or gray is None or gray.shape[0] < 8:
        out["error"] = "采样帧数太少"
        return out
    # 时间和空间上的中位数需要在 180 x H x W 上排序，是全流程最贵的一步。
    # 检测本身不需要全分辨率，降采样后精度足够，代价降为 1/downscale^2。
    ds = max(1, int(downscale))
    work = gray[:, ::ds, ::ds] if ds > 1 else gray
    out["downscale"] = ds
    # 时间中位数 + MAD 是这一步最贵的地方，而且**极吃内存**：
    #   np.abs(work - med) 里 work 是 uint8、med 是 float64 → 整个数组提升成 float64，
    #   188 × 960 × 540 就是 780MB 的临时（实测峰值主要来自这一行）。
    # 改成**按行分块**逐块算：中位数在时间轴上本来就是逐像素独立的，
    # 分块结果和整块完全一致，但临时数组被限制在几十 MB。
    _h, _w = work.shape[1], work.shape[2]
    med = np.empty((_h, _w), np.float32)
    mad = np.empty((_h, _w), np.float32)
    _band = max(1, (24 << 20) // max(1, work.shape[0] * _w * 4))     # 每块约 24MB
    for _y0 in range(0, _h, _band):
        _y1 = min(_h, _y0 + _band)
        # work 是 [:, ::ds, ::ds] 的**跨步视图**，直接在上面算会很慢；
        # 先连续化这一条带（只有几十 MB），比整块连续化省内存、又不损失速度。
        _sub = np.ascontiguousarray(work[:, _y0:_y1, :])
        _m = np.median(_sub, axis=0).astype(np.float32)
        _d = _sub.astype(np.float32)
        _d -= _m
        np.abs(_d, out=_d)
        med[_y0:_y1] = _m
        mad[_y0:_y1] = np.median(_d, axis=0)
    thr = max(1.2, float(np.percentile(mad, 20)))
    static = mad < thr
    gy, gx = np.gradient(med)
    grad = np.hypot(gx, gy)
    struct = grad > max(1.5, float(np.percentile(grad, 97)))
    mask = static & struct
    k = max(1, 3 // ds)                     # 形态学膨胀（降采样时要同步缩小，否则区域会被撑大）
    m = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            m |= np.roll(np.roll(mask, dy, 0), dx, 1)
    boxes = connected_boxes(m, min_area=int(min_area_ratio * m.size), max_boxes=max_boxes)
    out.update(median=med, grad=grad, static=static, struct=struct, thr=thr, mask=m)
    sx = src_w / float(work.shape[2]) if work.shape[2] else 1.0
    sy = src_h / float(work.shape[1]) if work.shape[1] else 1.0
    for (x0, y0, x1, y1, area) in boxes:
        w_, h_ = x1 - x0, y1 - y0
        fill = area / max(1, w_ * h_)
        gstd = float(grad[y0:y1, x0:x1].mean())
        out["boxes"].append((x0, y0, x1, y1, area, round(fill, 4), round(gstd, 3)))
        if 0.02 < fill < 0.95 and gstd > 2.0 and w_ > 8 and h_ > 6:
            out["regions"].append({
                "x": int(x0 * sx), "y": int(y0 * sy),
                "w": max(1, int(round((x1 - x0) * sx))),
                "h": max(1, int(round((y1 - y0) * sy))),
                "grad": round(gstd, 3), "fill": round(fill, 4),
                "analysis_box": [x0, y0, x1, y1],
            })
    return out


def analyze_frame_series(times, luma, sat, cut_sigma: float = 6.0) -> Dict[str, Any]:
    """逐帧亮度序列 → 区分「单帧插入(闪帧)」和「普通场景切换」，并找冻结段。

    判据：闪帧是「跳出去又跳回来」（i 与前后都差很大，但 i-1 与 i+1 几乎相同）；
    场景切换是「画面停在新亮度」。
    """
    out: Dict[str, Any] = {"flashes": [], "cuts": [], "frozen_runs": [],
                           "thr": 0.0, "n": 0, "y_mean": 0.0, "y_std": 0.0,
                           "y_min": 0.0, "y_max": 0.0, "sat_mean": 0.0}
    if np is None or luma is None or len(luma) < 10:
        out["error"] = "有效帧太少"
        return out
    y = np.asarray(luma, dtype=np.float64)
    s = np.asarray(sat, dtype=np.float64) if sat is not None else np.zeros_like(y)
    t = np.asarray(times, dtype=np.float64)
    d = np.abs(np.diff(y))
    mu, sd = d.mean(), d.std() + 1e-9
    thr = mu + cut_sigma * sd
    jumps = np.flatnonzero(d > thr)
    for i in jumps:
        if i + 1 >= len(y) - 1:
            out["cuts"].append(int(i))
            continue
        back = abs(y[i + 1] - y[i - 1])
        both = (d[i] > thr) and (d[i + 1] > thr)
        if both and back < 0.45 * max(d[i], d[i + 1]):
            out["flashes"].append(int(i))
        else:
            out["cuts"].append(int(i))
    frozen = np.flatnonzero(d < 0.05)
    if frozen.size:
        runs, start, prev = [], frozen[0], frozen[0]
        for i in frozen[1:]:
            if i != prev + 1:
                runs.append((start, prev))
                start = i
            prev = i
        runs.append((start, prev))
        out["frozen_runs"] = [(int(a), int(b)) for a, b in runs if b - a >= 15]
    out.update(thr=float(thr), n=int(len(y)), y_mean=float(y.mean()), y_std=float(y.std()),
               y_min=float(y.min()), y_max=float(y.max()), sat_mean=float(s.mean()),
               times=t, luma=y, jumps=[int(i) for i in jumps])
    return out


AUDIO_BANDS = [(0, 4000), (4000, 8000), (8000, 12000), (12000, 14000), (14000, 16000),
               (16000, 17000), (17000, 18000), (18000, 19000), (19000, 20000), (20000, 22050)]


def analyze_audio_samples(x, sr: int = 44100, n_fft: int = 4096, hop: int = 2048,
                          max_frames: int = 1500) -> Dict[str, Any]:
    """PCM 单声道 float32 → 频段能量占比 + 持续窄带单音（超声水印/隐藏载波）。"""
    out: Dict[str, Any] = {"bands": {}, "ultrasonic": 0.0, "tones": [], "error": ""}
    if np is None or x is None or len(x) < 8192:
        out["error"] = "音频太短或缺少 numpy"
        return out
    x = np.asarray(x, dtype=np.float32)
    nfr = 1 + (len(x) - n_fft) // hop
    if nfr <= 0:
        out["error"] = "音频太短"
        return out
    win = np.hanning(n_fft).astype(np.float32)
    step = max(1, nfr // max_frames)
    freqs = np.fft.rfftfreq(n_fft, 1 / float(sr))

    # 分块做 FFT：numpy 的 rfft **一律用双精度复数**，一次性对整个 frames 做的话
    # 输出是 1934×2049×16 ≈ 63MB，再叠上 abs / 平方的副本，峰值能到 200MB+。
    # 这里只保留每个频点的**一阶/二阶累加和**，均值与标准差递推得到，
    # 结果与「先算完整谱再 mean/std」完全等价，但临时数组被限制在几 MB。
    nbin = n_fft // 2 + 1
    sum_s = np.zeros(nbin, np.float64)
    sum_s2 = np.zeros(nbin, np.float64)
    cnt = 0
    _idxs = list(range(0, nfr, step))
    _blk = 128
    for _s0 in range(0, len(_idxs), _blk):
        _part = _idxs[_s0:_s0 + _blk]
        _fr = np.stack([x[i * hop:i * hop + n_fft] * win for i in _part])
        _S = np.abs(np.fft.rfft(_fr, axis=1)) ** 2
        sum_s += _S.sum(axis=0)
        np.square(_S, out=_S)
        sum_s2 += _S.sum(axis=0)
        cnt += _S.shape[0]
    _n = max(1, cnt)
    power = sum_s / _n
    total = float(power.sum()) + 1e-12

    band_ratio = {}
    for lo, hi in AUDIO_BANDS:
        m = (freqs >= lo) & (freqs < hi)
        band_ratio[f"{lo}-{hi}"] = float(power[m].sum() / total)
    hi_energy = sum(v for k, v in band_ratio.items()
                    if int(k.split("-")[0]) >= 16000)

    mean_t = power + 1e-12
    cv = np.sqrt(np.maximum(0.0, sum_s2 / _n - power * power)) / mean_t
    mask = freqs > 3000
    cand = np.argsort(-mean_t * (cv < 0.35) * mask)[:12]
    tones = []
    for i in cand:
        if mean_t[i] <= 0 or cv[i] >= 0.5:
            continue
        ratio = float(mean_t[i] / total)
        if ratio < 1e-5:
            continue
        tones.append({"freq": float(freqs[i]), "ratio": ratio, "cv": float(cv[i])})
    tones.sort(key=lambda t: -t["ratio"])

    out.update(bands=band_ratio, ultrasonic=hi_energy, tones=tones[:10], power=power, freqs=freqs)
    return out


def spectrogram_png(power, freqs, path: str) -> bool:
    """把平均功率谱画成一张窄条 PNG，作为频谱存证。"""
    if np is None or Image is None or power is None:
        return False
    try:
        spec = 10 * np.log10(power + 1e-12)
        spec = np.clip((spec - spec.max() + 90) / 90 * 255, 0, 255).astype(np.uint8)
        img = Image.fromarray(spec[:, None])       # 单列 → 横向频谱条
        img = img.resize((max(64, img.width), 512))
        img.save(path)
        return True
    except Exception:
        return False


def gradient_median_crop(frames, y0: int, y1: int, x0: int, x1: int,
                         budget_bytes: int = 64 * 1024 * 1024):
    """只在指定裁剪框内算「梯度幅值的时间中位数」。

    旧实现对每一帧整幅图求梯度；现在只算候选区域周围，运算量降到百分之几。

    但**大区域 × 多帧会吃爆内存**：一个 826x566 的裁剪框堆 300 帧的 float32
    梯度就是 535 MB，再 np.stack 一次峰值翻倍 —— 实测在 8G 机器上直接失败，
    表现就是「某个区域没有水印图层」，界面只好退回显示画面裁剪。
    所以这里：① 按内存预算抽帧（仍是时间中位数，语义不变）；
              ② 用预分配缓冲代替 np.stack，避免峰值翻倍。
    """
    if np is None or frames is None:
        return None
    y0 = max(0, int(y0)); x0 = max(0, int(x0))
    y1 = min(frames.shape[1], int(y1)); x1 = min(frames.shape[2], int(x1))
    if y1 <= y0 or x1 <= x0:
        return None
    sub = frames[:, y0:y1, x0:x1]
    n, h, w = sub.shape
    per = max(1, h * w * 4)
    step = max(1, -(-(n * per) // max(1, int(budget_bytes))))   # ceil
    if step > 1:
        sub = sub[::step]
        n = sub.shape[0]
    buf = np.empty((n, h, w), np.float32)
    for i in range(n):
        gy, gx = np.gradient(sub[i].astype(np.float32))
        np.hypot(gx, gy, out=buf[i])
    return np.median(buf, axis=0)
