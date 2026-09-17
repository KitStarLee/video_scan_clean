# -*- coding: utf-8 -*-
"""交互式水印区域选择：检测到水印后开一个本机页面，由用户勾选要去掉的区域。

设计要点
--------
* **不依赖 tkinter**：用标准库 ``http.server`` + 本机浏览器实现。
  macOS 上 pyenv / Homebrew 装的 Python 默认不带 ``_tkinter``（本机实测就是
  ``ModuleNotFoundError: No module named '_tkinter'``），一旦选了 tkinter，
  功能在用户机器上会直接不可用。浏览器方案在 Windows / macOS / Linux 上
  只要有 Python + 任意浏览器就能跑，不需要任何额外依赖。
* **不阻塞、不卡死批量**：HTTP 服务跑在后台线程，等待结果通过
  ``asyncio.Future`` 回传，事件循环照常跑；同一时刻只允许一个选择界面；
  pipeline 里等待用户的这段时间**不占用并发槽位**（见 pipeline.process_video）。
* **失败必须安全**：端口被占、没有浏览器、用户直接关掉页面、模板文件缺失，都只会得到
  「不处理水印」的结果，绝不抛异常打断批量。
* **界面和逻辑分开**：HTML/CSS/JS 全在同目录的 ``wm_picker_page.html`` 里，
  改样式/交互直接编辑它即可（按 mtime 缓存，改完重跑就生效，不用动 Python）。
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import ffmpeg_async as _F

__all__ = ["ask_watermark_regions", "render_annotated_frames", "probe_video_basics"]

# 同一时刻只允许一个选择界面：批量时不会同时弹出十几个窗口。
_ASK_LOCK: Optional[asyncio.Lock] = None

# 页面里嵌入的预览图最大宽度（原图仍然按原始分辨率存盘，供人工放大查看）
EMBED_MAX_WIDTH = 1000


def _ask_lock() -> asyncio.Lock:
    global _ASK_LOCK
    if _ASK_LOCK is None:          # 延迟创建：避免在 import 期绑定事件循环
        _ASK_LOCK = asyncio.Lock()
    return _ASK_LOCK


# ---------------------------------------------------------------- 取帧 / 标注
async def probe_video_basics(path: str) -> Tuple[int, int, float]:
    """返回 (宽, 高, 时长秒)。取不到时返回 (0, 0, 0)。"""
    try:
        info = await _F.ffprobe_json(path, "format:streams")
    except Exception:
        return 0, 0, 0.0
    W = H = 0
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            W, H = int(st.get("width") or 0), int(st.get("height") or 0)
            break
    try:
        dur = float(info.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:                    # 少数容器没有 format.duration，退回用流的时长
        for st in info.get("streams", []):
            if st.get("codec_type") == "video":
                try:
                    dur = float(st.get("duration") or 0.0)
                except (TypeError, ValueError):
                    dur = 0.0
                break
    return W, H, dur


def make_region_thumbs(frame_png: str, regions: Sequence[Dict[str, Any]],
                       W: int, H: int, *, max_w: int = 260) -> List[str]:
    """给每个区域裁一张小图（自动拉对比度），返回 data URI 列表。

    有些水印在整帧里几乎看不出来，单独裁出来、拉一下对比度就明显多了，
    所以选择界面里每个选项旁边直接放这张图。
    """
    out: List[str] = []
    try:
        from PIL import Image, ImageOps
    except Exception:
        return ["" for _ in regions]
    try:
        with Image.open(frame_png) as src:
            im = src.convert("RGB")
            for r in regions:
                try:
                    x, y = int(r["x"]), int(r["y"])
                    w, h = int(r["w"]), int(r["h"])
                except (KeyError, TypeError, ValueError):
                    out.append("")
                    continue
                pad = max(6, int(max(w, h) * 0.12))
                x0, y0 = max(0, x - pad), max(0, y - pad)
                x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
                if x1 - x0 < 4 or y1 - y0 < 4:
                    out.append("")
                    continue
                c = ImageOps.autocontrast(im.crop((x0, y0, x1, y1)), cutoff=1)
                scale = min(3.0, max(0.2, max_w / max(1, c.width)))
                c = c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))),
                             Image.LANCZOS)
                buf = io.BytesIO()
                c.save(buf, format="PNG", optimize=True)
                out.append("data:image/png;base64,"
                           + base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        return ["" for _ in regions]
    return out


def draw_region_boxes(img: Any, regions: Sequence[Dict[str, Any]],
                      W: int, H: int) -> Any:
    """在 PIL 图上给每个区域画框 + 编号，返回同一张图（原地修改）。"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    try:                            # Pillow ≥10.1 支持字号；旧的退回默认位图字体
        from PIL import ImageFont
        try:
            font = ImageFont.load_default(size=max(16, W // 45))
        except TypeError:
            font = ImageFont.load_default()
    except Exception:
        font = None
    lw = max(2, W // 320)
    for i, r in enumerate(regions, 1):
        try:
            x, y = int(r["x"]), int(r["y"])
            w, h = int(r["w"]), int(r["h"])
        except (KeyError, TypeError, ValueError):
            continue
        x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
        w, h = max(1, min(w, W - x)), max(1, min(h, H - y))
        d.rectangle([x, y, x + w - 1, y + h - 1], outline=(255, 40, 40), width=lw)
        # 编号徽标：贴在框的左上角，尽量放在画面内
        label = f"#{i}"
        try:
            tb = d.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except Exception:
            tw, th = 8 * len(label), 12
        pad = max(2, lw)
        bx0 = x
        by0 = y - th - 3 * pad
        if by0 < 0:
            by0 = y
        bx1, by1 = bx0 + tw + 3 * pad, by0 + th + 2 * pad
        d.rectangle([bx0, by0, min(bx1, W - 1), min(by1, H - 1)], fill=(255, 40, 40))
        d.text((bx0 + pad + 1, by0 + pad), label, fill=(255, 255, 255), font=font)
    return img


async def render_annotated_frames(src: str, regions: Sequence[Dict[str, Any]],
                                  outdir: str, *, times: Sequence[float] = (0.25, 0.5, 0.75),
                                  priority: int = 2,
                                  log: Optional[Callable[[str], None]] = None
                                  ) -> Tuple[List[str], List[str], int, int, List[str]]:
    """在几个代表性时间点各抽一帧、画上编号水印框。

    返回 ``(标注帧路径, **未标注**帧路径, W, H, 各区域裁剪小图 data URI)``。
    同时保留未标注帧是因为界面要**按区域现场裁剪当前这一帧**——拿标注帧去裁，
    会把红框和编号一起裁进去。

    抽多帧是因为有些视频的水印只在中段/片尾出现，单抽一帧可能正好看不见。
    """
    log = log or (lambda *_: None)
    os.makedirs(outdir, exist_ok=True)
    W, H, dur = await probe_video_basics(src)
    if W <= 0 or H <= 0:
        log("  ! 无法读取画面尺寸，跳过可视化标注")
        return [], 0, 0, []

    try:
        from PIL import Image
    except Exception:
        Image = None  # type: ignore

    shots: List[str] = []
    clean: List[str] = []
    for k, frac in enumerate(times, 1):
        t = max(0.0, min(dur * float(frac), max(0.0, dur - 0.05))) if dur > 0 else 0.0
        raw = os.path.join(outdir, f"frame{k}_raw.png")
        cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-ss", f"{t:.3f}", "-i", src,
               "-frames:v", "1", "-pix_fmt", "rgb24", raw]
        rc, _o, err = await _F.run_cmd(cmd, priority=priority)
        if rc != 0 or not os.path.exists(raw):
            log(f"  ! 抽帧失败（t={t:.1f}s）：{err.decode('utf-8', 'replace')[:200]}")
            continue
        out_png = os.path.join(outdir, f"frame{k}_t{t:.1f}s.png")
        if Image is not None and regions:
            try:
                with Image.open(raw) as im:
                    draw_region_boxes(im.convert("RGB"), regions, W, H).save(out_png)
                # raw 保留不删 —— 界面要用它按区域裁「当前帧」
            except Exception as exc:
                log(f"  ! 标注绘制失败，改用原帧：{type(exc).__name__}: {exc}")
                os.replace(raw, out_png)
                raw = out_png
        else:
            os.replace(raw, out_png)
            raw = out_png
        shots.append(out_png)
        clean.append(raw)
    # 区域裁剪小图取中间那帧（通常水印最完整）
    mid = len(clean) // 2
    thumbs = (make_region_thumbs(clean[mid], regions, W, H)
              if clean and regions else ["" for _ in regions])
    return shots, clean, W, H, thumbs


# ---------------------------------------------------------------- 页面
# 界面模板放在同目录的 wm_picker_page.html 里，方便单独改样式/交互。
# 这里按 mtime 缓存：改完 html 直接重跑就生效，不需要动 Python 代码。
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wm_picker_page.html")
_PAGE_CACHE: Dict[str, Any] = {"mtime": None, "text": ""}


def page_template() -> str:
    """读取界面模板；文件不存在时返回空串（调用方会优雅跳过而不是卡住）。"""
    try:
        m = os.path.getmtime(PAGE_PATH)
    except OSError:
        return ""
    if _PAGE_CACHE["mtime"] != m or not _PAGE_CACHE["text"]:
        try:
            with open(PAGE_PATH, encoding="utf-8") as fh:
                _PAGE_CACHE["text"] = fh.read()
            _PAGE_CACHE["mtime"] = m
        except OSError:
            return ""
    return str(_PAGE_CACHE["text"])





class _Handler(BaseHTTPRequestHandler):
    server_version = "mp4tool-wmpick"
    payload: Dict[str, Any] = {}
    page: bytes = b""
    token: str = ""
    loop: Any = None
    fut: Any = None

    def log_message(self, *a: Any) -> None:      # 别把访问日志打到用户终端
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self) -> None:                    # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path in (f"/r/{self.token}", ""):
            self._send(200, self.page, "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:                   # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path != f"/r/{self.token}/submit":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:
            data = {}
        self._send(200, b'{"ok":true}', "application/json")
        fut = self.fut
        if fut is not None and not fut.done():
            try:
                self.loop.call_soon_threadsafe(fut.set_result, data)
            except Exception:
                pass


def _build_page(payload: Dict[str, Any]) -> bytes:
    """把数据塞进外部模板。

    用 ``__XXX__`` 占位符 + ``str.replace`` 而不是 ``string.Template``：
    页面里的 JS 到处都是 ``$(...)``，``Template`` 会当成非法占位符直接报错。
    返回 ``b""`` 表示模板文件缺失。
    """
    page = page_template()
    if not page:
        return b""
    subs = {
        "__TITLE__": html.escape(str(payload["title"])),
        "__FNAME__": html.escape(str(payload["fname"])),
        "__W__": str(payload["w"]), "__H__": str(payload["h"]),
        "__N__": str(len(payload["regions"])),
        "__FIRST__": payload["frames"][0] if payload["frames"] else "",
        "__REGIONS__": json.dumps(payload["regions"], ensure_ascii=False),
        "__FRAMES__": json.dumps(payload["frames"], ensure_ascii=False),
        "__SUBMIT__": json.dumps(payload["submit_path"]),
        "__DEFAULT_MODE__": json.dumps(payload["default_mode"]),
        "__MASK_THRESHOLD__": repr(float(payload.get("mask_threshold", 0.35))),
        "__MASK_DILATE__": str(int(payload.get("mask_dilate", 2))),
    }
    for k, v in subs.items():
        page = page.replace(k, v)
    return page.encode("utf-8")


def _embed_data_uri(path: str, max_w: int = EMBED_MAX_WIDTH) -> str:
    """把 PNG 读成 data URI；过大的先等比缩到 max_w 再嵌。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.width > max_w:
                h = max(2, int(round(im.height * max_w / im.width)))
                im = im.convert("RGB").resize((max_w, h), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass
    with open(path, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")


# ---------------------------------------------------------------- 对外接口
async def ask_watermark_regions(
    shots: Sequence[str],
    regions: Sequence[Dict[str, Any]],
    *,
    W: int,
    H: int,
    fname: str,
    clean: Optional[Sequence[str]] = None,    # 未标注帧路径（界面按区域现场裁剪用）
    layers: Optional[Sequence[str]] = None,   # 每个区域的「水印图层」png 路径（扫描产物，优先）
    thumbs: Optional[Sequence[str]] = None,   # 兜底：从整帧裁出来的小图（data URI）
    default_mode: str = "delogo",     # 默认选中界面里排第一的「最推荐」项
    mask_threshold: float = 0.35,     # 笔画遮罩二值化阈值（界面上可拖；图层是空心轮廓，宜低）
    mask_dilate: int = 2,             # 笔画遮罩膨胀像素（界面上可拖）
    timeout: float = 0.0,
    open_browser: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """弹出选择界面并等待用户决定（异步、不阻塞事件循环）。

    返回 ``{"status": ..., "indices": [...], "mode": ...}``；无法弹窗或取消时返回 ``None``。
    调用方只需要关心 ``indices`` 是否为空。
    """
    log = log or (lambda *_: None)
    if not shots or not regions:
        return None
    if os.environ.get("MP4TOOL_NO_BROWSER"):    # 无头/自动化环境：只打印地址，不弹浏览器
        open_browser = False

    async with _ask_lock():                     # 一次只开一个界面
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        token = secrets.token_urlsafe(12)
        payload = {
            "title": "选择要去掉的水印区域",
            "fname": fname,
            "w": max(1, W), "h": max(1, H),
            "regions": [{"x": int(r.get("x", 0)), "y": int(r.get("y", 0)),
                         "w": int(r.get("w", 0)), "h": int(r.get("h", 0)),
                         "note": r.get("note", ""),
                         "thumb": (thumbs[i] if thumbs and i < len(thumbs) else ""),
                         "layer": ""}
                        for i, r in enumerate(regions)],
            "frames": [],
            "submit_path": f"/r/{token}/submit",
            "default_mode": default_mode,
            "mask_threshold": float(mask_threshold),
            "mask_dilate": int(mask_dilate),
        }
        try:
            payload["frames"] = [_embed_data_uri(p) for p in shots]
            # 水印图层（扫描器导出的 watermark_regionN_*.png）优先；
            # 它是多帧梯度中位数，只有水印本身，比从整帧裁剪清楚得多。
            for i, r in enumerate(payload["regions"]):
                lp = layers[i] if layers and i < len(layers) else ""
                if lp and os.path.exists(lp):
                    r["layer"] = _embed_data_uri(lp)
        except Exception as exc:
            log(f"  ! 页面预览图生成失败：{type(exc).__name__}: {exc}")
            return None

        page = _build_page(payload)
        if not page:
            log(f"  ! 找不到界面模板文件，跳过人工选择：{PAGE_PATH}")
            return None
        handler = type("_BoundHandler", (_Handler,), {
            "payload": payload, "token": token, "loop": loop, "fut": fut,
            "page": page,
        })
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:
            log(f"  ! 无法启动本地选择界面（端口不可用）：{exc}")
            return None
        server.daemon_threads = True
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/r/{token}/"
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True, name="wmpick-http").start()

        log(f"  ⏳ 等待人工选择水印区域（在浏览器里勾选）：{url}")
        if open_browser:
            try:
                await asyncio.to_thread(webbrowser.open, url)
            except Exception:
                log("  · 自动打开浏览器失败，请手动访问上面的地址")

        try:
            if timeout and timeout > 0:
                result: Dict[str, Any] = await asyncio.wait_for(asyncio.shield(fut), timeout)
            else:
                result = await fut
        except asyncio.TimeoutError:
            log(f"  · 等待人工选择超过 {timeout:.0f}s，按「不处理水印」继续")
            result = {"status": "timeout", "indices": []}
        except asyncio.CancelledError:
            log("  · 任务被取消，关闭选择界面")
            raise
        finally:
            def _stop() -> None:
                try:
                    server.shutdown()
                except Exception:
                    pass
                finally:
                    try:
                        server.server_close()
                    except Exception:
                        pass
            threading.Thread(target=_stop, daemon=True, name="wmpick-stop").start()

        if result.get("status") == "cancel":
            log("  · 用户取消了水印处理")
            return None
        idx = [int(i) for i in (result.get("indices") or []) if isinstance(i, (int, float))]
        idx = [i for i in idx if 0 <= i < len(regions)]
        mode = str(result.get("mode") or default_mode)
        if not idx:
            log("  · 用户未选择任何区域，跳过水印处理")
            return None
        log(f"  ✔ 用户选择了 {len(idx)} 处区域（{'、'.join('#' + str(i + 1) for i in idx)}），"
            f"处理方式={mode}")
        out = {"status": str(result.get("status") or "ok"), "indices": idx, "mode": mode}
        # mask 模式的两个参数由界面拖动决定，原样带回给修复层
        for k, cast in (("mask_threshold", float), ("mask_dilate", int)):
            if result.get(k) is not None:
                try:
                    out[k] = cast(result[k])
                except (TypeError, ValueError):
                    pass
        if mode == "mask" and "mask_threshold" in out:
            log(f"  · 笔画遮罩参数：阈值={out['mask_threshold']} 膨胀={out.get('mask_dilate')}")
        return out
