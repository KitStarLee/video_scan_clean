# -*- coding: utf-8 -*-
"""独立的「画质优先」视频瘦身。

和扫描/修复**完全独立**：不读扫描报告、不碰 Tier A/B/C、输出目录也分开。
它只做一件事 —— 在**感知质量不掉**的前提下把文件压小。

设计大前提（用户明确要求）
--------------------------
**宁可瘦不了多少，也绝不让画面变糊。** 所以目标不是「压到某个体积」，
而是「先定一个感知质量下限，再在这个下限之上尽量压」。具体落到代码里：

1. **用 VMAF 当裁判**（本机 ffmpeg 带 ``libvmaf``，没有就退到 SSIM）。
   它不是「像不像」而是「人眼看着差多少」，正是「清晰度」这件事的量化指标。
2. **先小样校准，再全片编码**：拿几段小样反复试，达不到质量下限就把 CRF 往
   「更好」的方向调，最多试 N 次；全片压完**再验一次**，不达标就**放弃输出、
   保留原文件**。所以最坏结果是「没瘦下来」，不会是「糊了」。
3. **绝不默认降分辨率、降帧率**（那是最伤清晰度的两件事），需要显式开
   ``--allow-downscale`` 才做。
4. **不划算就不做**：如果按校准结果推算只能省不到 ``min_savings``（默认 10%），
   直接跳过并且不动原文件 —— 为了 3% 的体积去重编一遍不值。
5. **音频默认直接 copy**（音频不影响「画面清晰度」，而且重编只会更差）；
   只有无损音频或码率离谱高时才转 AAC。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import ffmpeg_async as _F
from .resources import Governor
from .utils import human

__all__ = ["SlimOptions", "SlimResult", "run_slim", "main", "collect_targets"]

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts"}

# 感知质量下限。VMAF 和 SSIM 各有一套刻度，两套都给，按实际可用的度量选。
QUALITY_TARGETS: Dict[str, Dict[str, float]] = {
    "transparent": {"vmaf": 97.0, "ssim": 0.988},   # 几乎看不出差别
    "high":        {"vmaf": 95.0, "ssim": 0.980},   # 默认：正常观看看不出
    "balanced":    {"vmaf": 93.0, "ssim": 0.970},   # 省得多一点，细看有差
}


# ---------------------------------------------------------------- 编码器表
@dataclass(frozen=True)
class EncoderSpec:
    name: str                       # ffmpeg 编码器名
    family: str                     # h264 | hevc | av1
    quality_flag: str               # -crf 或 -q:v
    quality: Dict[str, int]         # 各质量档的起始值
    preset_flag: str = ""
    presets: Dict[str, str] = field(default_factory=dict)
    extra: Tuple[str, ...] = ()
    tag: str = ""                   # mp4 兼容 tag（hvc1 对 QuickTime/苹果生态很重要）
    efficient: bool = True          # 是否属于「同画质更小」的一档
    speed_note: str = ""


# 顺序 = 默认优先级（同画质下更省体积的排前面）。AV1 不进默认候选：
# 兼容性还不如 h264/h265 稳，必须显式 --encoder libsvtav1 才用。
ENCODERS: Tuple[EncoderSpec, ...] = (
    EncoderSpec("libx265", "hevc", "-crf", {"transparent": 20, "high": 22, "balanced": 24},
                "-preset", {"slow": "slow", "medium": "medium", "fast": "fast"},
                ("-x265-params", "log-level=error"), "hvc1", True, "中等"),
    EncoderSpec("libx264", "h264", "-crf", {"transparent": 18, "high": 20, "balanced": 22},
                "-preset", {"slow": "slow", "medium": "medium", "fast": "fast"},
                (), "", True, "快"),
    EncoderSpec("libsvtav1", "av1", "-crf", {"transparent": 26, "high": 30, "balanced": 34},
                "-preset", {"slow": "4", "medium": "6", "fast": "8"},
                ("-svtav1-params", "fast-decode=1"), "av01", True, "中等（兼容性差）"),
    EncoderSpec("hevc_videotoolbox", "hevc", "-q:v", {"transparent": 80, "high": 75, "balanced": 70},
                "", {}, ("-allow_sw", "1"), "hvc1", False, "很快（同画质更大）"),
    EncoderSpec("h264_videotoolbox", "h264", "-q:v", {"transparent": 80, "high": 75, "balanced": 70},
                "", {}, ("-allow_sw", "1"), "", False, "很快（同画质更大）"),
)

COLOR_HDR = {"smpte2084", "arib-std-b67"}       # PQ / HLG


def _available_encoders() -> List[str]:
    """从 ffmpeg -encoders 里读一次，缓存。"""
    global _ENC_CACHE
    if _ENC_CACHE is None:
        names: List[str] = []
        try:
            import subprocess
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, timeout=20)
            for line in out.stdout.decode("utf-8", "replace").splitlines():
                m = re.match(r"\s*[VAS][\w.]{5}\s+(\S+)", line)
                if m:
                    names.append(m.group(1))
        except Exception:
            pass
        _ENC_CACHE = names
    return _ENC_CACHE


_ENC_CACHE: Optional[List[str]] = None


def pick_candidates(opts: "SlimOptions") -> List[EncoderSpec]:
    """挑出这次实际要试的编码器（自动模式下会试多个，谁小用谁）。"""
    have = set(_available_encoders())
    if opts.encoder and opts.encoder != "auto":
        want = [e for e in ENCODERS if e.name == opts.encoder]
        if not want:
            raise ValueError(f"不认识的编码器: {opts.encoder}")
        return [e for e in want if e.name in have] or want
    out = [e for e in ENCODERS if e.efficient and e.name in have and not e.name.startswith("libsvtav1")]
    if not out:                              # 兜底：连着硬件编码器一起上
        out = [e for e in ENCODERS if e.name in have]
    if not out:
        raise ValueError(
            "这个 ffmpeg 里找不到任何可用的 H.264/H.265 编码器"
            f"（已检测到: {sorted(have)[:12] or '空'}）。"
            "Windows 上建议用 gyan.dev 或 BtbN 的完整构建，自带 libx264/libx265；"
            "也可以显式指定 --encoder <名称>。")
    return out


# ---------------------------------------------------------------- 媒体信息
@dataclass
class MediaInfo:
    path: str = ""
    size: int = 0
    W: int = 0
    H: int = 0
    fps: float = 0.0
    duration: float = 0.0
    vcodec: str = ""
    pix_fmt: str = ""
    vbitrate: int = 0
    acodec: str = ""
    abitrate: int = 0
    achannels: int = 0
    color_transfer: str = ""
    nb_frames: int = 0

    @property
    def bpp(self) -> float:
        """每像素每帧的比特数 —— 判断「这文件是不是已经被压得很紧了」的关键指标。"""
        if not (self.W and self.H and self.fps and self.vbitrate):
            return 0.0
        return self.vbitrate / (self.W * self.H * self.fps)

    @property
    def is_hdr(self) -> bool:
        return (self.color_transfer or "").lower() in COLOR_HDR


async def probe_media(path: str) -> MediaInfo:
    info = await _F.ffprobe_json(path, "format:streams")
    mi = MediaInfo(path=path, size=os.path.getsize(path))
    fmt = info.get("format", {})
    try:
        mi.duration = float(fmt.get("duration") or 0.0)
        mi.vbitrate = int(float(fmt.get("bit_rate") or 0))
    except (TypeError, ValueError):
        pass
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and not mi.W:
            mi.vcodec = st.get("codec_name") or ""
            mi.W, mi.H = int(st.get("width") or 0), int(st.get("height") or 0)
            mi.pix_fmt = st.get("pix_fmt") or ""
            mi.color_transfer = st.get("color_transfer") or ""
            mi.fps = _parse_fps(st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1")
            try:
                mi.nb_frames = int(st.get("nb_frames") or 0)
            except (TypeError, ValueError):
                mi.nb_frames = 0
            try:                              # 容器总码率优先，其次用流码率
                vb = int(float(st.get("bit_rate") or 0))
            except (TypeError, ValueError):
                vb = 0
            if vb:
                mi.vbitrate = vb
        elif st.get("codec_type") == "audio" and not mi.acodec:
            mi.acodec = st.get("codec_name") or ""
            mi.achannels = int(st.get("channels") or 0)
            try:
                mi.abitrate = int(float(st.get("bit_rate") or 0))
            except (TypeError, ValueError):
                mi.abitrate = 0
    if not mi.duration and mi.fps and mi.nb_frames:
        mi.duration = mi.nb_frames / mi.fps
    return mi


def _parse_fps(s: str) -> float:
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except Exception:
        return 0.0


# ---------------------------------------------------------------- 质量度量
@dataclass
class QualityScore:
    metric: str = ""            # vmaf | ssim | none
    mean: float = 0.0
    worst: float = 0.0
    note: str = ""
    p1: float = 0.0             # VMAF 第 1 百分位：均值会掩盖「少数几帧特别糊」


async def measure_quality(ref: str, dist: str, start: float, dur: float,
                          workdir: str, priority: int = 2,
                          ref_vf: str = "",
                          dist_start: Optional[float] = None) -> QualityScore:
    """量一段（同一个时间窗）的感知质量。优先 VMAF，没有就退 SSIM。

    ``ref_vf``：降分辨率时参考帧要跟着缩到同一尺寸，否则 libvmaf
    会因为两路尺寸不一致直接报错。
    ``dist_start``：被测文件里这段的起点。比较「整片 vs 整片」时等于 ``start``；
    但小样校准是拿**已经切好的片段**去比原片，片段自己从 0 开始，
    这时候必须传 0 —— 否则会对片段再 seek 一次，seek 到片段长度之外，
    一帧都不剩，VMAF 直接算不出来。
    """
    ds = start if dist_start is None else dist_start
    os.makedirs(workdir, exist_ok=True)
    logp = os.path.join(workdir, f"vmaf_{uuid.uuid4().hex[:8]}.json")
    rs = ("," + ref_vf) if ref_vf else ""
    fc = (f"[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS{rs}[r];"
          f"[d][r]libvmaf=log_fmt=json:log_path={logp}")
    cmd = ["ffmpeg", "-v", "error", "-nostdin",
           "-ss", f"{ds:.3f}", "-t", f"{dur:.3f}", "-i", dist,
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", ref,
           "-lavfi", fc, "-f", "null", "-"]
    rc, _o, err = await _F.run_cmd(cmd, priority=priority)
    if rc == 0 and os.path.exists(logp):
        try:
            with open(logp, encoding="utf-8") as fh:
                data = json.load(fh)
            pm = (data.get("pooled_metrics") or {}).get("vmaf") or {}
            mean = float(pm.get("mean") or 0.0)
            # 用 harmonic_mean 而不是 min 当「最差」：小样片段首尾常有单帧
            # 重排/错位，会把 min 拉到 40 分以下，但它不代表整段画质。
            # 判定始终只看 mean，最后还会拿整片复验一次。
            worst = float(pm.get("harmonic_mean") or pm.get("min") or mean)
            p1 = mean
            frames = data.get("frames") or []
            vals = []
            for fr in frames:
                try:
                    vals.append(float((fr.get("metrics") or {}).get("vmaf")))
                except (TypeError, ValueError):
                    pass
            if vals:
                vals.sort()
                p1 = vals[max(0, int(len(vals) * 0.01) - 1)]
            if mean > 0:
                return QualityScore("vmaf", mean, worst, "", p1)
        except Exception:
            pass
        finally:
            try:
                os.remove(logp)
            except Exception:
                pass
    # VMAF 不可用（或失败）→ SSIM
    # 注意：ssim 的统计摘要由 log 以 info 级别打印，用 -v error 会被吞掉，
    # 所以这里必须放宽到 info，否则永远解析不到分数。
    cmd = ["ffmpeg", "-v", "info", "-nostdin",
           "-ss", f"{ds:.3f}", "-t", f"{dur:.3f}", "-i", dist,
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", ref,
           "-lavfi", f"[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS{rs}[r];[d][r]ssim",
           "-f", "null", "-"]
    rc, _o, err = await _F.run_cmd(cmd, priority=priority)
    text = err.decode("utf-8", "replace")
    vals = [float(m) for m in re.findall(r"All:\s*([0-9.]+)", text)]
    if rc == 0 and vals:
        return QualityScore("ssim", sum(vals) / len(vals), min(vals),
                            "本机 ffmpeg 没有 libvmaf，改用 SSIM")
    return QualityScore("none", 0.0, 0.0, "VMAF 和 SSIM 都不可用，无法做画质校验")


# 第 1 百分位允许比均值低多少。均值达标不代表没有几帧糊掉，
# 而人眼恰恰只记得那几帧；这里给最差的 1% 帧单独设一道线。
P1_MARGIN = 10.0


def _gate(score: QualityScore, target: Dict[str, float]) -> bool:
    if score.metric == "vmaf":
        return (score.mean >= target["vmaf"]
                and (score.p1 <= 0 or score.p1 >= target["vmaf"] - P1_MARGIN))
    if score.metric == "ssim":
        return score.mean >= target["ssim"]
    return False


# ---------------------------------------------------------------- 计划
@dataclass
class SlimPlan:
    action: str = "skip"            # slim | skip
    reason: str = ""
    encoder: str = ""
    encoder_family: str = ""
    args: List[str] = field(default_factory=list)
    crf: int = 0
    preset: str = ""
    quality: QualityScore = field(default_factory=QualityScore)
    projected_bytes: int = 0
    tried: List[str] = field(default_factory=list)


def _sample_windows(duration: float, n: int, seg: float) -> List[Tuple[float, float]]:
    """在片子里分散取 n 个小窗口（避开开头结尾的字幕/黑场）。"""
    n = max(1, n)
    seg = max(1.0, min(seg, duration / max(1, n)))
    if duration <= seg * n:
        return [(0.0, duration)]
    span = duration - seg
    return [(span * (i + 0.5) / n, seg) for i in range(n)]


def _encode_cmd(src: str, dst: str, spec: EncoderSpec, crf: int, preset: str,
                opts: "SlimOptions", start: Optional[float] = None,
                dur: Optional[float] = None, with_audio: bool = True) -> List[str]:
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-map", "0:v:0"]
    if with_audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
    if opts.vf:
        cmd += ["-vf", opts.vf]
    cmd += ["-c:v", spec.name, spec.quality_flag, str(crf),
            "-pix_fmt", opts.pix_fmt or "yuv420p"]
    if spec.preset_flag and preset:
        cmd += [spec.preset_flag, preset]
    cmd += list(spec.extra)
    if spec.tag:
        cmd += ["-tag:v", spec.tag]
    cmd += _audio_args(opts)
    if opts.threads:
        cmd += ["-threads", str(opts.threads)]
    if not with_audio:
        cmd += ["-an"]
    if opts.faststart and dst.lower().endswith((".mp4", ".mov", ".m4v")):
        cmd += ["-movflags", "+faststart"]
    cmd.append(dst)
    return cmd


def _audio_args(opts: "SlimOptions") -> List[str]:
    if opts.audio == "copy":
        return ["-c:a", "copy"]
    if opts.audio == "aac":
        return ["-c:a", "aac", "-b:a", opts.audio_bitrate]
    return []          # auto：由 plan 决定，调用方会覆盖


def _audio_args_for(mi: MediaInfo, opts: "SlimOptions") -> List[str]:
    if not mi.acodec:
        return ["-an"]
    if opts.audio == "copy":
        return ["-c:a", "copy"]
    if opts.audio == "aac":
        return ["-c:a", "aac", "-b:a", opts.audio_bitrate]
    lossless = mi.acodec in ("flac", "alac", "wavpack", "truehd", "dts") \
        or mi.acodec.startswith("pcm_")
    if lossless or mi.abitrate > 256_000:
        return ["-c:a", "aac", "-b:a", opts.audio_bitrate]
    return ["-c:a", "copy"]


# ---------------------------------------------------------------- 选项 / 结果
@dataclass
class SlimOptions:
    outdir: str = "slim_output"
    export_to_source: bool = False
    suffix: str = ".slim"
    quality: str = "high"                 # transparent | high | balanced
    min_savings: float = 0.10             # 省不到 10% 就不动
    max_crf_tries: int = 3                # 不达标时最多往下调几次
    sample_count: int = 2
    sample_seconds: float = 3.0
    full_verify: bool = True              # 压完全片解码验证
    encoder: str = "auto"
    preset: str = ""                      # 空=按编码器挑
    audio: str = "auto"                   # auto | copy | aac
    audio_bitrate: str = "192k"
    allow_downscale: bool = False
    max_height: int = 0
    allow_hdr: bool = False
    pix_fmt: str = ""
    vf: str = ""
    faststart: bool = True
    keep_work: bool = False
    max_jobs: int = 0
    threads: int = 0
    priority: int = 2
    verbose: bool = False


@dataclass
class SlimResult:
    src: str = ""
    ok: bool = True
    skipped: bool = False
    reason: str = ""
    dst: str = ""
    size_before: int = 0
    size_after: int = 0
    encoder: str = ""
    crf: int = 0
    preset: str = ""
    quality_metric: str = ""
    quality_score: float = 0.0
    quality_target: float = 0.0
    elapsed: float = 0.0
    log_lines: List[str] = field(default_factory=list)

    @property
    def saved_pct(self) -> float:
        if not self.size_before:
            return 0.0
        return (1 - self.size_after / self.size_before) * 100.0


# ---------------------------------------------------------------- 单文件流程
async def slim_one(src: str, opts: SlimOptions, *, emit: Callable[[str], None] = print,
                   governor: Optional[Governor] = None) -> SlimResult:
    """给一个视频做「画质优先」瘦身。"""
    gov = governor or Governor(max_jobs=1, log=lambda *_: None)
    name = os.path.basename(src)
    stem = os.path.splitext(name)[0]
    vdir = os.path.join(opts.outdir, stem)
    work = os.path.join(vdir, ".work")
    os.makedirs(work, exist_ok=True)
    res = SlimResult(src=src, size_before=os.path.getsize(src))

    def log(msg: str = "") -> None:
        res.log_lines.append(msg)
        emit(msg)

    async with gov.slot(name):
        try:
            thread_args = opts.threads or gov.job_threads
            mi = await probe_media(src)
            o = _with_threads(opts, thread_args)
            o.pix_fmt, o.vf = _pix_fmt_and_scale(mi, opts)
            log(f"[{name}] {mi.W}x{mi.H} @{mi.fps:.3f}fps  {mi.vcodec}/{mi.pix_fmt}  "
                f"{human(mi.size)}  视频码率 {mi.vbitrate // 1000}kbps  bpp={mi.bpp:.4f}")
            if mi.acodec:
                log(f"           音频 {mi.acodec} {mi.achannels}ch {mi.abitrate // 1000}kbps")

            skip = _precheck(mi, o)
            if skip:
                res.skipped, res.reason = True, skip
                log(f"[{name}] 跳过：{skip}")
                _write_report(res, vdir)
                return res

            target = QUALITY_TARGETS[o.quality]
            plan = await _calibrate(src, mi, o, target, work, log, thread_args)
            for t in plan.tried:
                log("           试过 " + t)
            if plan.action != "slim":
                res.skipped, res.reason = True, plan.reason
                log(f"[{name}] 跳过：{plan.reason}")
                _write_report(res, vdir)
                return res

            tkey = "vmaf" if plan.quality.metric == "vmaf" else "ssim"
            tval = target[tkey]
            log(f"[{name}] 方案：{plan.encoder} {' '.join(plan.args)}  小样质量 "
                f"{plan.quality.metric}={plan.quality.mean:.4f}（下限 {tval}）  "
                f"预计 {human(plan.projected_bytes)}")
            res.encoder, res.crf, res.preset = plan.encoder, plan.crf, plan.preset
            res.quality_metric, res.quality_target = plan.quality.metric, tval

            dst = _dst_path(src, vdir, stem, opts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            t0 = time.time()
            cmd = _encode_cmd(src, dst, _spec(plan.encoder), plan.crf,
                              _preset_for(plan.encoder, o), o)
            cmd = _replace_audio_args(cmd, _audio_args_for(mi, o))
            rc, _out, err = await _F.run_cmd(cmd, priority=o.priority)
            res.elapsed = time.time() - t0
            if rc != 0 or not os.path.exists(dst):
                res.ok, res.reason = False, "编码失败：" + err.decode("utf-8", "replace")[:300]
                log(f"[{name}] ✗ {res.reason}")
                _write_report(res, vdir)
                return res

            res.dst = dst
            res.size_after = os.path.getsize(dst)
            log(f"[{name}] 编码完成 {res.elapsed:.0f}s  {human(res.size_before)} → "
                f"{human(res.size_after)}（省 {res.saved_pct:.1f}%）")

            # ---- 全片解码验证（保证能正常播放） ----
            if o.full_verify:
                good, e, secs = await _F.decode_null_check(dst, priority=o.priority)
                if not good:
                    res.ok, res.reason = False, f"输出无法正常解码：{e[:200]}"
                    log(f"[{name}] ✗ {res.reason} → 丢弃输出，保留原文件")
                    _discard(dst)
                    _write_report(res, vdir)
                    return res
                log(f"[{name}] ✔ 全片解码零错误（{secs:.1f}s）")

            # ---- 压完再抽 3 段验一次画质（最坏情况：不达标就丢弃） ----
            worst = await _verify_output(src, dst, mi, o, work, thread_args)
            if worst is not None and worst.metric != "none":
                res.quality_metric, res.quality_score = worst.metric, worst.mean
                ok_q = _gate(worst, target)
                _t = target["vmaf" if worst.metric == "vmaf" else "ssim"]
                log(f"[{name}] {'✔' if ok_q else '✗'} 成品画质 {worst.metric}="
                    f"{worst.mean:.4f}（下限 {_t}）"
                    + (f"  p1={worst.p1:.2f}（下限 {_t - P1_MARGIN:.1f}）"
                       if worst.metric == "vmaf" and worst.p1 > 0 else ""))
                if not ok_q:
                    res.ok, res.reason = False, "成品画质低于下限，已丢弃输出"
                    log(f"[{name}] ✗ {res.reason} → 保留原文件")
                    _discard(dst)
                    _write_report(res, vdir)
                    return res

            # ---- 不够省也不留 ----
            if res.size_after >= res.size_before * (1 - o.min_savings):
                res.skipped = True
                res.reason = f"只能省 {res.saved_pct:.1f}%（低于 {o.min_savings * 100:.0f}%），不值得"
                log(f"[{name}] 跳过：{res.reason} → 保留原文件")
                _discard(dst)
                _write_report(res, vdir)
                return res

            if not opts.export_to_source:
                _move_into_place(dst, vdir, stem, opts)
                res.dst = _final_path(vdir, stem, opts)
            if not opts.keep_work:
                shutil.rmtree(work, ignore_errors=True)
            _write_report(res, vdir)
            return res
        except Exception as exc:
            import traceback
            res.ok = False
            res.reason = f"{type(exc).__name__}: {exc}"
            log(f"[{name}] ✗ {res.reason}")
            log(traceback.format_exc())
            _write_report(res, vdir)
            return res


def _pix_fmt_and_scale(mi: MediaInfo, o: SlimOptions) -> Tuple[str, str]:
    """定像素格式和（可选的）缩放滤镜。

    默认**保持原分辨率**；10bit 源保持 10bit（降到 8bit 会出 banding）。
    只有显式 --allow-downscale 且给了 --max-height 时才缩放。
    """
    pix = mi.pix_fmt if ("10" in (mi.pix_fmt or "") or "12" in (mi.pix_fmt or "")) else "yuv420p"
    vf = ""
    if o.allow_downscale and o.max_height and mi.H > o.max_height:
        h = int(o.max_height) - (int(o.max_height) % 2)
        w = max(2, int(round(mi.W * h / mi.H / 2)) * 2)
        vf = f"scale={w}:{h}"
    return pix, vf


def _with_threads(o: SlimOptions, threads: int) -> SlimOptions:
    import copy
    c = copy.copy(o)
    c.threads = threads
    return c


def _spec(name: str) -> EncoderSpec:
    for e in ENCODERS:
        if e.name == name:
            return e
    return ENCODERS[0]


def _preset_for(name: str, o: SlimOptions) -> str:
    spec = _spec(name)
    if o.preset:
        return o.preset
    return spec.presets.get("medium") or (list(spec.presets.values())[0] if spec.presets else "")


def _replace_audio_args(cmd: List[str], audio: List[str]) -> List[str]:
    """把命令里占位的音频参数换掉（auto 模式要按源文件决定）。"""
    out: List[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ("-c:a", "-b:a") or cmd[i] == "-an":
            i += 2 if cmd[i] != "-an" else 1
            continue
        out.append(cmd[i])
        i += 1
    # 插到输出文件前面
    return out[:-1] + audio + [out[-1]]


def _dst_path(src: str, vdir: str, stem: str, o: SlimOptions) -> str:
    if o.export_to_source:
        return os.path.join(os.path.dirname(src), f"{stem}{o.suffix}.mp4")
    return os.path.join(vdir, ".work", f"{stem}{o.suffix}.mp4")


def _final_path(vdir: str, stem: str, o: SlimOptions) -> str:
    return os.path.join(vdir, f"{stem}{o.suffix}.mp4")


def _move_into_place(tmp: str, vdir: str, stem: str, o: SlimOptions) -> None:
    final = _final_path(vdir, stem, o)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    if os.path.abspath(tmp) != os.path.abspath(final):
        shutil.move(tmp, final)


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def _precheck(mi: MediaInfo, o: SlimOptions) -> str:
    """一眼就不该压的情况，直接跳过（省得白跑一遍编码）。"""
    if mi.W <= 0 or mi.H <= 0 or mi.duration <= 0:
        return "容器信息不完整，无法规划参数"
    if not mi.vcodec:
        return "没有视频流"
    if mi.is_hdr and not o.allow_hdr:
        return (f"检测到 HDR（color_transfer={mi.color_transfer}）：重编码容易把 HDR 元数据"
                "弄丢导致发灰/发白，默认不动它（确需处理加 --allow-hdr）")
    if mi.bpp and mi.bpp < 0.045 and mi.vcodec in ("hevc", "av1", "vp9"):
        return (f"已经是 {mi.vcodec} 且码率很紧（bpp={mi.bpp:.4f}），"
                "再压基本只会掉画质，不划算")
    return ""


async def _calibrate(src: str, mi: MediaInfo, o: SlimOptions, target: Dict[str, float],
                     work: str, log: Callable[..., None], threads: int) -> SlimPlan:
    """小样试编码 + 量质量，选出一个「达标且最省」的方案。"""
    windows = _sample_windows(mi.duration, o.sample_count, o.sample_seconds)
    best: Optional[SlimPlan] = None
    for spec in pick_candidates(o):
        preset = _preset_for(spec.name, o)
        crf = spec.quality.get(o.quality, 22)
        for attempt in range(max(1, o.max_crf_tries)):
            scores: List[QualityScore] = []
            total_bytes = 0
            total_secs = 0.0
            for k, (st, dur) in enumerate(windows):
                seg = os.path.join(work, f"sample_{spec.name}_{crf}_{k}.mp4")
                cmd = _encode_cmd(src, seg, spec, crf, preset,
                                  _with_threads(o, threads), start=st, dur=dur,
                                  with_audio=False)
                rc, _out, _err = await _F.run_cmd(cmd, priority=o.priority)
                if rc != 0 or not os.path.exists(seg):
                    scores = []
                    break
                total_bytes += os.path.getsize(seg)
                total_secs += dur
                q = await measure_quality(src, seg, st, dur, work, o.priority, o.vf,
                                          dist_start=0.0)
                scores.append(q)
                _discard(seg)
            if not scores or not total_secs:
                log(f"           {spec.name} crf={crf}：小样编码失败，换下一个")
                break
            metric = scores[0].metric
            mean = sum(s.mean for s in scores) / len(scores)
            worst = min(s.worst for s in scores)
            p1 = min(s.p1 for s in scores if s.p1 > 0) if any(s.p1 > 0 for s in scores) else 0.0
            q = QualityScore(metric, mean, worst, scores[0].note, p1)
            vbr = int(total_bytes * 8 / total_secs)
            # 音频按原样保留时，投影体积要把音频加回去
            proj = int((vbr + (mi.abitrate if mi.acodec else 0)) * mi.duration / 8)
            log(f"           {spec.name} crf={crf} preset={preset}: "
                f"{metric}={mean:.4f} (p1 {p1:.2f}, worst {worst:.4f})  "
                f"预计 {human(proj)}（省 {(1 - proj / max(1, mi.size)) * 100:.1f}%）")
            if _gate(q, target):
                args = [spec.quality_flag, str(crf)]
                if spec.preset_flag and preset:
                    args += [spec.preset_flag, preset]
                cand = SlimPlan("slim", "", spec.name, spec.family, args, crf,
                                preset, q, proj)
                if best is None or cand.projected_bytes < best.projected_bytes:
                    best = cand
                break
            # 不达标 → CRF 往「更好画质」方向调（只往更清晰的方向走）
            if attempt < o.max_crf_tries - 1:
                crf = max(0, crf - (2 if spec.quality_flag == "-crf" else 5))
    if best is not None:
        if best.projected_bytes >= mi.size * (1 - o.min_savings):
            best.action = "skip"
            best.reason = (f"最好也只能省 {(1 - best.projected_bytes / max(1, mi.size)) * 100:.1f}%"
                           f"（低于 {o.min_savings * 100:.0f}%），不值得重编")
        return best
    return SlimPlan("skip", "所有编码器在质量下限之上都省不下来；为了不掉画质，保持原样")


async def _verify_output(src: str, dst: str, mi: MediaInfo, o: SlimOptions,
                         work: str, threads: int) -> Optional[QualityScore]:
    """成品抽 3 段再验一次质量。"""
    windows = _sample_windows(mi.duration, 3, max(2.0, o.sample_seconds))
    scores: List[QualityScore] = []
    for st, dur in windows:
        q = await measure_quality(src, dst, st, dur, work, o.priority, o.vf)
        if q.metric == "none":
            return q
        scores.append(q)
    if not scores:
        return None
    metric = scores[0].metric
    return QualityScore(metric, sum(s.mean for s in scores) / len(scores),
                        min(s.worst for s in scores), scores[0].note,
                        min((s.p1 for s in scores if s.p1 > 0), default=0.0))


# ---------------------------------------------------------------- 报告 / 批处理
def _write_report(res: SlimResult, vdir: str) -> None:
    try:
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "slim_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"mp4tool slim v1 —— 画质优先瘦身\n")
            fh.write(f"输入: {res.src}\n")
            fh.write(f"状态: {'跳过' if res.skipped else ('成功' if res.ok else '失败')}  {res.reason}\n")
            if res.dst:
                fh.write(f"输出: {res.dst}\n")
                fh.write(f"体积: {human(res.size_before)} → {human(res.size_after)}"
                         f"（省 {res.saved_pct:.1f}%）\n")
                fh.write(f"编码: {res.encoder} crf/q={res.crf} preset={res.preset or '-'}\n")
                fh.write(f"画质: {res.quality_metric}={res.quality_score:.4f}"
                         f"（下限 {res.quality_target}）\n")
            fh.write(f"耗时: {res.elapsed:.1f}s\n\n")
            fh.write("\n".join(res.log_lines) + "\n")
    except Exception:
        pass


def collect_targets(targets: Sequence[str], recursive: bool = False) -> List[str]:
    """展开成视频文件列表（去重）。``slim`` 产物不会被子目录扫描二次吃到。"""
    out: List[str] = []
    seen = set()

    def add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)

    for t in targets:
        if os.path.isdir(t):
            if recursive:
                for root, _d, files in os.walk(t):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                            add(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(t)):
                    fp = os.path.join(t, f)
                    if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in VIDEO_EXT:
                        add(fp)
        elif os.path.isfile(t):
            add(t)
    return out


async def run_slim(targets: Sequence[str], opts: SlimOptions,
                   emit: Optional[Callable[[str], None]] = None) -> List[SlimResult]:
    emit = emit or (lambda s: print(s, flush=True))
    videos = collect_targets(targets)
    os.makedirs(opts.outdir, exist_ok=True)
    if not videos:
        emit("没有找到任何视频文件。")
        return []
    gov = Governor(max_jobs=(opts.max_jobs or None), log=emit)
    emit("=" * 78)
    emit(f"mp4tool slim v1  画质优先瘦身  共 {len(videos)} 个视频")
    emit(gov.describe())
    emit(f"质量下限: {opts.quality}（{QUALITY_TARGETS[opts.quality]}）  "
         f"省不到 {opts.min_savings * 100:.0f}% 就不动原文件")
    emit("=" * 78)
    t0 = time.time()
    results = await asyncio.gather(*[slim_one(v, opts, emit=emit, governor=gov) for v in videos])
    total_before = sum(r.size_before for r in results)
    total_after = sum((r.size_after or r.size_before) for r in results)
    emit("-" * 78)
    for r in results:
        if r.skipped:
            emit(f"  · {os.path.basename(r.src)[:34]:<34} 跳过  {r.reason[:52]}")
        elif r.ok:
            emit(f"  ✔ {os.path.basename(r.src)[:34]:<34} {human(r.size_before)} → "
                 f"{human(r.size_after)}  省 {r.saved_pct:.1f}%  {r.encoder} crf={r.crf}  "
                 f"{r.quality_metric}={r.quality_score:.4f}")
        else:
            emit(f"  ✗ {os.path.basename(r.src)[:34]:<34} {r.reason[:60]}")
    emit("-" * 78)
    emit(f"合计 {human(total_before)} → {human(total_after)}"
         f"（省 {(1 - total_after / max(1, total_before)) * 100:.1f}%），"
         f"耗时 {time.time() - t0:.0f}s")
    emit("=" * 78)
    return results


# ---------------------------------------------------------------- CLI
def build_parser(prog: str = "mp4tool slim"):
    import argparse
    ap = argparse.ArgumentParser(
        prog=prog,
        description="画质优先的视频瘦身（独立于扫描/修复；宁可瘦不了多少，也不掉画质）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s video.mp4                      # 自动选参，只求不掉画质
  %(prog)s ./videos -o ./slim_out         # 批量
  %(prog)s video.mp4 --quality transparent   # 更保守（几乎零感知差）
  %(prog)s video.mp4 --quality balanced      # 省得多一点，细看有差
  %(prog)s video.mp4 --encoder libx264       # 强制某个编码器
""")
    ap.add_argument("targets", nargs="*", help="视频文件或文件夹")
    ap.add_argument("-o", "--outdir", default="slim_output", help="输出根目录（默认 ./slim_output）")
    ap.add_argument("--quality", default="high", choices=list(QUALITY_TARGETS),
                    help="感知质量下限：transparent(最高) | high(默认) | balanced")
    ap.add_argument("--min-savings", type=float, default=0.10,
                    help="至少省这么多才输出，否则保留原文件（默认 0.10）")
    ap.add_argument("--encoder", default="auto",
                    help="auto(默认) | libx265 | libx264 | libsvtav1 | hevc_videotoolbox | h264_videotoolbox")
    ap.add_argument("--preset", default="", help="编码器 preset（默认按编码器自动）")
    ap.add_argument("--audio", default="auto", choices=["auto", "copy", "aac"],
                    help="音频处理：auto(默认，无损/码率过高才转 AAC) | copy | aac")
    ap.add_argument("--audio-bitrate", default="192k", help="转 AAC 时的码率（默认 192k）")
    ap.add_argument("--allow-downscale", action="store_true",
                    help="允许降分辨率（默认绝不做，这是最伤清晰度的操作）")
    ap.add_argument("--allow-hdr", action="store_true",
                    help="允许处理 HDR 视频（默认跳过，避免弄丢 HDR 元数据）")
    ap.add_argument("--max-height", type=int, default=0, help="配合 --allow-downscale 使用")
    ap.add_argument("--export-to-source", action="store_true",
                    help="把瘦身结果放到原视频旁边（文件名加 .slim）")
    ap.add_argument("--jobs", type=int, default=0, help="并发数，0=自动")
    ap.add_argument("--no-verify", dest="full_verify", action="store_false", default=True,
                    help="跳过成品全片解码验证（不建议）")
    ap.add_argument("--priority", type=int, default=2, choices=[0, 1, 2, 3])
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def _opts_from_args(a: Any) -> SlimOptions:
    return SlimOptions(
        outdir=os.path.abspath(a.outdir), export_to_source=a.export_to_source,
        quality=a.quality, min_savings=a.min_savings, encoder=a.encoder, preset=a.preset,
        audio=a.audio, audio_bitrate=a.audio_bitrate, allow_downscale=a.allow_downscale,
        allow_hdr=a.allow_hdr, max_height=a.max_height, max_jobs=a.jobs, full_verify=a.full_verify,
        priority=a.priority, keep_work=a.keep_work, verbose=a.verbose)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import sys
    try:
        from .utils import setup_console_utf8
        setup_console_utf8()
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.targets:
        ap.print_help()
        return 2
    opts = _opts_from_args(args)
    if not os.path.isdir(opts.outdir):
        os.makedirs(opts.outdir, exist_ok=True)
    try:
        asyncio.run(run_slim(args.targets, opts))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0
