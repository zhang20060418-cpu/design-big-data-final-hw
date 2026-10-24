"""第三步：PPT 文字可见性二次复检（独立可运行）。

用途
----
对第一阶段 `S-OVERSHOOT`（对象外框越界）的记录做第二次检测：判断**实际可见文字**
是否越出页面或被页面裁切，同时保留第一次的几何结果作为 `geometry_evidence`。

核心做法
--------
1. 用 PowerPoint COM（TextFrame2 / TextRange）读取**实际排版范围**；
2. 不用整段 Bound* 直接下结论——它会把末尾空段落算进去（实测见报告），
   因此按**非空段落**取并集得到 visible_bbox，同时保留 layout_bbox 作对照；
3. 用 PowerPoint 导出页面 PNG，在像素层面核对页边是否有墨迹被切断；
4. 只有"排版范围越界 + 页边确有墨迹"才给 confirmed_clip，其余按证据给
   no_visible_clip / uncertain / not_applicable；
5. 真实样本只复检第一阶段越界候选，`page_coverage` 逐页记录
   “含文字对象 / 已复检 / 跳过”，不把单个对象的不适用当成整页结论；
6. 判 not_applicable 且无文字/填充/边框/效果的空形状，审查图默认不画几何框，
   `--debug-frames` 可恢复显示（结构数据始终保留）。

用法
----
    python text_visibility_recheck.py                # 复检 4 条真实记录 + 3 个合成样本
    python text_visibility_recheck.py --refresh      # 忽略 COM 探针缓存，重新读取
    python text_visibility_recheck.py --skip-render  # 只做 COM 判定，不导出/不留证据图
    python text_visibility_recheck.py --debug-frames # 调试：空形状也画几何框

依赖：python-pptx、Pillow、pywin32、本机 Microsoft PowerPoint。
原始 PPTX 只读打开，不保存、不改写。
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import statistics
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation

HERE = Path(__file__).resolve().parent          # .../ppt_audit_stage1/outputs/recheck
ROOT = HERE.parents[1]                          # .../ppt_audit_stage1
STAGE1_DIR = ROOT / "outputs"
DECK_DIR = ROOT.parent / "python-ppt" / "downloaded_pptx"
SAMPLES = ROOT / "test_samples" / "recheck" / "recheck_samples.pptx"

EVIDENCE = HERE / "evidence"
RENDER = HERE / "render"
PROBE_CACHE = HERE / "probe"
PROBE_SCHEMA = "v2"

# ---- 判定口径（工程默认值，未经校准）----
PAGE_TOL_PT = 1.0          # 页面边界容差：0.35 mm
INNER_TOL_PT = 6.0         # 框内压力阈值：避免把排版舍入/尾随空格当成溢出
EDGE_STRIP_PX = 3          # 页边墨迹检测带宽度（144 dpi 下约 1.5 pt）
INK_DIFF = 45              # 与页面主背景亮度差超过该值记为墨迹
EDGE_INK_MIN = 0.02        # 页边带内墨迹占比阈值
BG_UNIFORM_MIN = 0.55      # 主背景占比低于此值 → 背景复杂，墨迹判定降级为不确定
RENDER_W, RENDER_H = 1920, 1080

STATUS_CN = {
    "confirmed_clip": "已确认裁切",
    "no_visible_clip": "未发现可见裁切",
    "uncertain": "不确定",
    "not_applicable": "不适用",
}
STATUS_COLOR = {
    "confirmed_clip": (214, 40, 40),
    "no_visible_clip": (42, 157, 143),
    "uncertain": (196, 121, 28),
    "not_applicable": (120, 128, 140),
}
SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_blank_text(text: str) -> bool:
    """空段落判定：空串、空白、不间断空格、仅换行等都不产生可见字形。"""
    if text is None:
        return True
    cleaned = text.replace("\xa0", " ").replace("\u200b", " ")
    cleaned = cleaned.replace("\r", "").replace("\n", "").replace("\v", "")
    return cleaned.strip() == ""


def bbox_pt(left: float, top: float, width: float, height: float) -> dict[str, float]:
    return {
        "left_pt": round(left, 3),
        "top_pt": round(top, 3),
        "width_pt": round(width, 3),
        "height_pt": round(height, 3),
        "right_pt": round(left + width, 3),
        "bottom_pt": round(top + height, 3),
    }


def to_inch(value: float | None) -> float | None:
    return None if value is None else round(value / 72.0, 4)


def bbox_with_inch(box: dict[str, float] | None) -> dict[str, Any] | None:
    if box is None:
        return None
    out: dict[str, Any] = dict(box)
    for key in ("left", "top", "right", "bottom", "width", "height"):
        out[f"{key}_in"] = to_inch(box.get(f"{key}_pt"))
    return out


def union_bbox(boxes: list[dict[str, float]]) -> dict[str, float] | None:
    boxes = [b for b in boxes if b and b.get("width_pt") is not None]
    if not boxes:
        return None
    left = min(b["left_pt"] for b in boxes)
    top = min(b["top_pt"] for b in boxes)
    right = max(b["right_pt"] for b in boxes)
    bottom = max(b["bottom_pt"] for b in boxes)
    return bbox_pt(left, top, right - left, bottom - top)


# --------------------------------------------------------------------------- #
# PowerPoint COM 探针
# --------------------------------------------------------------------------- #
def probe_deck(deck: Path, slides: list[int] | None, export_dir: Path | None,
               force: bool = False) -> dict[str, Any]:
    """只读打开 PPTX，读取形状与 TextFrame2 排版数据，并按需导出页面 PNG。

    缓存键包含文件 SHA-256 与目标页，避免重复启动 PowerPoint。
    """
    deck = Path(deck)
    digest = sha256_of(deck)
    key = (f"{deck.stem}__{'-'.join(map(str, slides)) if slides else 'all'}"
           f"__{digest[:12]}__{PROBE_SCHEMA}.json")
    cache = PROBE_CACHE / key
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("deck_sha256") == digest:
            return data

    import win32com.client as win32  # 延迟导入：无 PowerPoint 环境也能 --help

    result: dict[str, Any] = {
        "deck": str(deck),
        "deck_sha256": digest,
        "powerpoint_version": None,
        "slide_width_pt": None,
        "slide_height_pt": None,
        "probed_at": now_iso(),
        "slides": [],
        "errors": [],
    }

    app = None
    pres = None
    preexisting = True
    try:
        app = win32.Dispatch("PowerPoint.Application")
        preexisting = app.Presentations.Count > 0
        result["powerpoint_version"] = str(app.Version)
        # ReadOnly=-1(msoTrue), Untitled=0, WithWindow=0：不显示窗口、不写回
        pres = app.Presentations.Open(str(deck), -1, 0, 0)
        result["slide_width_pt"] = round(float(pres.PageSetup.SlideWidth), 3)
        result["slide_height_pt"] = round(float(pres.PageSetup.SlideHeight), 3)

        for slide in pres.Slides:
            idx = int(slide.SlideIndex)
            if slides and idx not in slides:
                continue
            shapes: list[dict[str, Any]] = []
            z = 0
            for shape in slide.Shapes:
                z += 1
                rec: dict[str, Any] = {
                    "z": z,
                    "id": int(shape.Id),
                    "name": str(shape.Name),
                    "type": int(shape.Type),
                    "shape_type": SHAPE_TYPE_CN.get(int(shape.Type), f"TYPE_{int(shape.Type)}"),
                    "left_pt": _num(shape.Left),
                    "top_pt": _num(shape.Top),
                    "width_pt": _num(shape.Width),
                    "height_pt": _num(shape.Height),
                    "rotation": _num(shape.Rotation),
                    "visible": int(shape.Visible),
                    "has_text_frame": False,
                    "has_text": False,
                    "text": "",
                    "bound_left_pt": None,
                    "bound_top_pt": None,
                    "bound_width_pt": None,
                    "bound_height_pt": None,
                    "autosize": None,
                    "word_wrap": None,
                    "anchor": None,
                    "margin_left_pt": None,
                    "margin_right_pt": None,
                    "margin_top_pt": None,
                    "margin_bottom_pt": None,
                    "font_size_eff": None,
                    "paragraphs": [],
                    "fill_visible": None,
                    "line_visible": None,
                    "probe_error": None,
                }
                try:
                    tf = shape.TextFrame2
                    rec["has_text_frame"] = True
                    rec["autosize"] = int(tf.AutoSize)
                    rec["word_wrap"] = int(tf.WordWrap)
                    rec["anchor"] = int(tf.VerticalAnchor)
                    rec["margin_left_pt"] = _num(tf.MarginLeft)
                    rec["margin_right_pt"] = _num(tf.MarginRight)
                    rec["margin_top_pt"] = _num(tf.MarginTop)
                    rec["margin_bottom_pt"] = _num(tf.MarginBottom)
                    if int(tf.HasText) != 0:
                        tr = tf.TextRange
                        rec["has_text"] = True
                        rec["text"] = str(tr.Text)
                        rec["bound_left_pt"] = _num(tr.BoundLeft)
                        rec["bound_top_pt"] = _num(tr.BoundTop)
                        rec["bound_width_pt"] = _num(tr.BoundWidth)
                        rec["bound_height_pt"] = _num(tr.BoundHeight)
                        rec["font_size_eff"] = _num(tr.Font.Size)
                        rec["paragraphs"] = _probe_paragraphs(tr)
                except Exception as exc:  # noqa: BLE001 - 单个形状失败不阻断整页
                    rec["probe_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    rec["fill_visible"] = int(shape.Fill.Visible)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    rec["line_visible"] = int(shape.Line.Visible)
                except Exception:  # noqa: BLE001
                    pass
                shapes.append(rec)

            slide_rec: dict[str, Any] = {"index": idx, "shapes": shapes}
            if export_dir is not None:
                export_dir.mkdir(parents=True, exist_ok=True)
                png = export_dir / f"slide{idx:02d}.png"
                slide.Export(str(png), "PNG", RENDER_W, RENDER_H)
                slide_rec["png"] = str(png)
            result["slides"].append(slide_rec)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["traceback"] = traceback.format_exc(limit=4)
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if app is not None and not preexisting:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        del pres, app
        gc.collect()

    PROBE_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _num(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _probe_paragraphs(tr: Any) -> list[dict[str, Any]]:
    """逐段取边界：整段 Bound* 会包含末尾空段落，逐段取才能排除。

    注意：pywin32 里 `tr.Paragraphs` 直接返回段落集合，`tr.Paragraphs(i)` 会被当成
    带参数的属性调用而报“不支持集合”，必须走 `.Item(i)`。
    """
    out: list[dict[str, Any]] = []
    try:
        collection = tr.Paragraphs
        count = int(collection.Count)
    except Exception:  # noqa: BLE001
        return out
    for i in range(1, count + 1):
        rec = {
            "index": i,
            "text": "",
            "font_size": None,
            "bound_left_pt": None,
            "bound_top_pt": None,
            "bound_width_pt": None,
            "bound_height_pt": None,
            "probe_error": None,
        }
        try:
            para = collection.Item(i)
            rec["text"] = str(para.Text)
            rec["font_size"] = _num(para.Font.Size)
            rec["bound_left_pt"] = _num(para.BoundLeft)
            rec["bound_top_pt"] = _num(para.BoundTop)
            rec["bound_width_pt"] = _num(para.BoundWidth)
            rec["bound_height_pt"] = _num(para.BoundHeight)
        except Exception as exc:  # noqa: BLE001
            rec["probe_error"] = f"{type(exc).__name__}: {exc}"
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# 几何与文字范围
# --------------------------------------------------------------------------- #
def shape_geometry(shape: dict[str, Any]) -> dict[str, float] | None:
    if None in (shape.get("left_pt"), shape.get("top_pt"),
                shape.get("width_pt"), shape.get("height_pt")):
        return None
    return bbox_pt(shape["left_pt"], shape["top_pt"], shape["width_pt"], shape["height_pt"])


def layout_bbox(shape: dict[str, Any]) -> dict[str, float] | None:
    if None in (shape.get("bound_left_pt"), shape.get("bound_top_pt"),
                shape.get("bound_width_pt"), shape.get("bound_height_pt")):
        return None
    return bbox_pt(shape["bound_left_pt"], shape["bound_top_pt"],
                   shape["bound_width_pt"], shape["bound_height_pt"])


def visible_bbox(shape: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """非空段落边界并集；并记录被排除的空段落。"""
    boxes = []
    blank = 0
    for para in shape.get("paragraphs") or []:
        if is_blank_text(para.get("text", "")):
            blank += 1
            continue
        if None in (para.get("bound_left_pt"), para.get("bound_top_pt"),
                    para.get("bound_width_pt"), para.get("bound_height_pt")):
            continue
        boxes.append(bbox_pt(para["bound_left_pt"], para["bound_top_pt"],
                             para["bound_width_pt"], para["bound_height_pt"]))
    meta = {
        "basis": "union_of_non_blank_paragraphs",
        "paragraph_count": len(shape.get("paragraphs") or []),
        "blank_paragraph_count": blank,
        "used_paragraphs": len(boxes),
    }
    if boxes:
        return union_bbox(boxes), meta
    # 回退：COM 未给逐段边界时用整段边界，但标注 basis 以便追溯
    full = layout_bbox(shape)
    if full is not None and not is_blank_text(shape.get("text", "")):
        meta["basis"] = "fallback_full_text_range"
        return full, meta
    return None, meta


def overflow_sides(box: dict[str, float] | None, page_w: float, page_h: float,
                   tol: float = PAGE_TOL_PT) -> list[dict[str, Any]]:
    if box is None:
        return []
    checks = [
        ("left", -box["left_pt"], box["left_pt"] < -tol),
        ("top", -box["top_pt"], box["top_pt"] < -tol),
        ("right", box["right_pt"] - page_w, box["right_pt"] > page_w + tol),
        ("bottom", box["bottom_pt"] - page_h, box["bottom_pt"] > page_h + tol),
    ]
    return [
        {
            "side": side,
            "distance_pt": round(max(dist, 0.0), 3),
            "distance_in": to_inch(max(dist, 0.0)),
        }
        for side, dist, hit in checks if hit
    ]


def inner_frame(shape: dict[str, Any], geom: dict[str, float] | None) -> dict[str, float] | None:
    if geom is None:
        return None
    ml = shape.get("margin_left_pt") or 0.0
    mr = shape.get("margin_right_pt") or 0.0
    mt = shape.get("margin_top_pt") or 0.0
    mb = shape.get("margin_bottom_pt") or 0.0
    return bbox_pt(geom["left_pt"] + ml, geom["top_pt"] + mt,
                   max(geom["width_pt"] - ml - mr, 0.0),
                   max(geom["height_pt"] - mt - mb, 0.0))


def frame_exceed(box: dict[str, float] | None, inner: dict[str, float] | None) -> list[dict[str, Any]]:
    """文字范围超出文本框内部显示区域（PowerPoint 不裁切，仅记录为排版压力）。"""
    if box is None or inner is None:
        return []
    checks = [
        ("left", inner["left_pt"] - box["left_pt"], box["left_pt"] < inner["left_pt"] - INNER_TOL_PT),
        ("top", inner["top_pt"] - box["top_pt"], box["top_pt"] < inner["top_pt"] - INNER_TOL_PT),
        ("right", box["right_pt"] - inner["right_pt"], box["right_pt"] > inner["right_pt"] + INNER_TOL_PT),
        ("bottom", box["bottom_pt"] - inner["bottom_pt"], box["bottom_pt"] > inner["bottom_pt"] + INNER_TOL_PT),
    ]
    return [
        {"side": side, "distance_pt": round(max(d, 0.0), 3), "distance_in": to_inch(max(d, 0.0))}
        for side, d, hit in checks if hit
    ]


# --------------------------------------------------------------------------- #
# 渲染图的墨迹分析
# --------------------------------------------------------------------------- #
def page_background(im: Image.Image) -> tuple[int, float]:
    """返回页面主背景亮度与占比；占比低说明背景复杂，墨迹判定不可靠。"""
    small = im.resize((im.width // 4, im.height // 4))
    hist = small.histogram()
    total = sum(hist) or 1
    # 以 4 为量化步长合并
    buckets = [sum(hist[i:i + 4]) for i in range(0, 256, 4)]
    idx = max(range(len(buckets)), key=lambda i: buckets[i])
    return idx * 4 + 2, buckets[idx] / total


def ink_analysis(png: Path, box: dict[str, float], page_w: float, page_h: float,
                 sides: list[str]) -> dict[str, Any]:
    """在页面图上核对：文字范围内是否有墨迹、页边是否被墨迹触碰。"""
    im = Image.open(png).convert("L")
    sx = im.width / page_w
    sy = im.height / page_h
    bg, share = page_background(im)

    clip = {
        "left_pt": max(box["left_pt"], 0.0),
        "top_pt": max(box["top_pt"], 0.0),
        "right_pt": min(box["right_pt"], page_w),
        "bottom_pt": min(box["bottom_pt"], page_h),
    }
    if clip["right_pt"] <= clip["left_pt"] or clip["bottom_pt"] <= clip["top_pt"]:
        return {"available": False, "reason": "文字范围与页面无交集", "bg_lum": bg, "bg_share": round(share, 4)}

    x0, y0 = int(math.floor(clip["left_pt"] * sx)), int(math.floor(clip["top_pt"] * sy))
    x1, y1 = int(math.ceil(clip["right_pt"] * sx)), int(math.ceil(clip["bottom_pt"] * sy))
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, im.width), min(y1, im.height)
    crop = im.crop((x0, y0, x1, y1))
    px = crop.tobytes()          # L 模式：每像素 1 字节
    w, h = crop.size
    mask = bytearray(w * h)
    for i, v in enumerate(px):
        if abs(v - bg) > INK_DIFF:
            mask[i] = 1

    ink_px = sum(mask)
    ink_bbox = None
    if ink_px:
        minx, miny, maxx, maxy = w, h, -1, -1
        for yy in range(h):
            row = mask[yy * w:(yy + 1) * w]
            if any(row):
                miny = min(miny, yy)
                maxy = yy
                first = row.index(1)
                last = w - 1 - row[::-1].index(1)
                minx = min(minx, first)
                maxx = max(maxx, last)
        ink_bbox = {
            "left_pt": round((x0 + minx) / sx, 2),
            "top_pt": round((y0 + miny) / sy, 2),
            "right_pt": round((x0 + maxx + 1) / sx, 2),
            "bottom_pt": round((y0 + maxy + 1) / sy, 2),
        }
        ink_bbox["width_pt"] = round(ink_bbox["right_pt"] - ink_bbox["left_pt"], 2)
        ink_bbox["height_pt"] = round(ink_bbox["bottom_pt"] - ink_bbox["top_pt"], 2)

    edge: dict[str, Any] = {}
    for side in sides:
        edge[side] = _edge_ink(im, box, side, page_w, page_h, bg, ink_bbox)

    return {
        "available": True,
        "bg_lum": bg,
        "bg_share": round(share, 4),
        "bg_reliable": share >= BG_UNIFORM_MIN,
        "region_px": [x0, y0, x1, y1],
        "ink_px": ink_px,
        "ink_ratio": round(ink_px / (w * h), 5) if w * h else 0.0,
        "ink_bbox_pt": ink_bbox,
        "edge": edge,
    }


def _edge_ink(im: Image.Image, box: dict[str, float], side: str, page_w: float,
              page_h: float, bg: int, ink_bbox: dict[str, float] | None) -> dict[str, Any]:
    """页边 3 px 带内的墨迹占比：>0 表示有字形被页面边缘切开。"""
    sx = im.width / page_w
    sy = im.height / page_h
    span = {
        "left": (max(box["top_pt"], 0), min(box["bottom_pt"], page_h)),
        "right": (max(box["top_pt"], 0), min(box["bottom_pt"], page_h)),
        "top": (max(box["left_pt"], 0), min(box["right_pt"], page_w)),
        "bottom": (max(box["left_pt"], 0), min(box["right_pt"], page_w)),
    }[side]
    if span[1] <= span[0]:
        return {"edge_ink_ratio": None, "reason": "越界方向与页面无重叠区间"}

    if side in ("left", "right"):
        x = 0 if side == "left" else im.width - EDGE_STRIP_PX
        strip = im.crop((x, int(span[0] * sy), x + EDGE_STRIP_PX, int(math.ceil(span[1] * sy))))
    else:
        y = 0 if side == "top" else im.height - EDGE_STRIP_PX
        strip = im.crop((int(span[0] * sx), y, int(math.ceil(span[1] * sx)), y + EDGE_STRIP_PX))

    data = strip.tobytes()
    if not data:
        return {"edge_ink_ratio": None, "reason": "边缘条带为空"}
    ink = sum(1 for v in data if abs(v - bg) > INK_DIFF)
    ratio = ink / len(data)

    gap = None
    if ink_bbox:
        gap = {
            "left": ink_bbox["left_pt"],
            "right": round(page_w - ink_bbox["right_pt"], 2),
            "top": ink_bbox["top_pt"],
            "bottom": round(page_h - ink_bbox["bottom_pt"], 2),
        }[side]
    return {
        "edge_ink_ratio": round(ratio, 5),
        "edge_ink_px": ink,
        "strip_px": len(data),
        "ink_gap_to_edge_pt": gap,
    }


# --------------------------------------------------------------------------- #
# 证据图
# --------------------------------------------------------------------------- #
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _dashed_rect(draw: ImageDraw.ImageDraw, rect, color, width=3, dash=14, gap=8):
    x0, y0, x1, y1 = [int(round(v)) for v in rect]
    for x in range(x0, x1, dash + gap):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash + gap):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


def _dotted_rect(draw: ImageDraw.ImageDraw, rect, color, width=2, step=10):
    x0, y0, x1, y1 = [int(round(v)) for v in rect]
    for x in range(x0, x1, step):
        draw.line([(x, y0), (min(x + 3, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + 3, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, step):
        draw.line([(x0, y), (x0, min(y + 3, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + 3, y1))], fill=color, width=width)


def annotate_case(case: dict[str, Any], png: Path, out_path: Path,
                  debug_frames: bool = False) -> dict[str, Any]:
    """在页面图上画：对象外框（虚线）、可见文字范围（实线）、整段排版范围（点线）。

    `not_applicable` 且无文字/无填充/无边框/无可见效果的空形状，默认不画几何框；
    `debug_frames=True` 时仍绘制，便于排查坐标。
    """
    page = Image.open(png).convert("RGB")
    page_w = case["page"]["width_pt"]
    page_h = case["page"]["height_pt"]
    sx = page.width / page_w
    sy = page.height / page_h
    status = case["second_pass"]["status"]
    color = STATUS_COLOR[status]

    suppressed = bool((case.get("annotation") or {}).get("frame_suppressed"))
    draw_frame = not suppressed or debug_frames

    boxes = [case["text_layout"]["layout_bbox_pt"],
             case["text_layout"]["visible_bbox_pt"]]
    if draw_frame:
        boxes.append(case["geometry_evidence"]["frame_bbox_pt"])
    boxes = [b for b in boxes if b]
    left_pt = min([0.0] + [b["left_pt"] for b in boxes])
    top_pt = min([0.0] + [b["top_pt"] for b in boxes])
    right_pt = max([page_w] + [b["right_pt"] for b in boxes])
    bottom_pt = max([page_h] + [b["bottom_pt"] for b in boxes])

    pad_left = int(max(28, (-left_pt) * sx + 28))
    pad_top = int(max(68, (-top_pt) * sy + 40))
    pad_right = int(max(28, (right_pt - page_w) * sx + 28))
    pad_bottom = int(max(120, (bottom_pt - page_h) * sy + 84))

    canvas = Image.new("RGB",
                       (page.width + pad_left + pad_right, page.height + pad_top + pad_bottom),
                       (247, 248, 250))
    canvas.paste(page, (pad_left, pad_top))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([pad_left - 1, pad_top - 1, pad_left + page.width, pad_top + page.height],
                   outline=(180, 186, 196), width=2)

    def rect(box):
        return (pad_left + box["left_pt"] * sx, pad_top + box["top_pt"] * sy,
                pad_left + box["right_pt"] * sx, pad_top + box["bottom_pt"] * sy)

    if draw_frame:
        geom = case["geometry_evidence"]["frame_bbox_pt"]
        _dashed_rect(draw, rect(geom), (180, 140, 40), width=3)

    lay = case["text_layout"]["layout_bbox_pt"]
    vis = case["text_layout"]["visible_bbox_pt"]
    if lay and vis and (abs(lay["bottom_pt"] - vis["bottom_pt"]) > 3
                        or abs(lay["right_pt"] - vis["right_pt"]) > 3
                        or abs(lay["top_pt"] - vis["top_pt"]) > 3):
        _dotted_rect(draw, rect(lay), (70, 110, 180), width=2)
    if vis:
        draw.rectangle(rect(vis), outline=color, width=4)

    # 标题条
    head = f"{case['case_id']}  {case['file']}  p{case['slide']}  id={case['shape_id']}  ·  {STATUS_CN[status]}"
    draw.text((18, 12), head, font=_font(24, True), fill=(28, 32, 40))
    draw.text((18, 42), f"{case['name']}  /  {case['shape_type']}  /  {case['text_excerpt'][:48]}",
              font=_font(17), fill=(96, 104, 118))

    # 底部说明（放在所有越界框之外）
    st = case["second_pass"]
    if suppressed and not debug_frames:
        legend = ("空形状（无文字、无填充、无边框、无可见效果）：按策略不绘制几何边界框；"
                  "结构数据与坐标保留在 JSON")
    elif suppressed and debug_frames:
        legend = "调试模式：显示空形状的几何边界框（默认审查图不绘制）"
    else:
        legend = "虚线=对象外框（第一阶段几何）   实线=实际文字排版范围（COM）   点线=含空段落的整段范围"
    notes = [
        legend,
        f"{st['status_cn']} · {st['reasons'][0] if st['reasons'] else ''}",
    ]
    y = canvas.height - 66
    for i, line in enumerate(notes):
        draw.text((18, y + i * 27), line, font=_font(18 if i else 16),
                  fill=(40, 44, 52) if i else (120, 128, 140))
    canvas.save(out_path)
    return {
        "annotated_png": str(out_path),
        "frame_drawn": draw_frame,
        "frame_suppressed": suppressed,
        "debug_frames": debug_frames,
    }


def zoom_crop(case: dict[str, Any], png: Path, out_path: Path) -> dict[str, Any] | None:
    """对已确认裁切的页面，放大截取被切断的页边区域（限制在易读的窗口内）。"""
    ov = case["page_overflow"]["sides"]
    if not ov:
        return None
    side = ov[0]["side"]
    page_w = case["page"]["width_pt"]
    page_h = case["page"]["height_pt"]
    im = Image.open(png).convert("RGB")
    sx = im.width / page_w
    sy = im.height / page_h
    box = case["text_layout"]["visible_bbox_pt"]
    band = 100      # 边缘带宽度（pt）
    window = 420    # 另一方向的最大窗口（pt）
    if side in ("right", "left"):
        x0 = max(page_w - band, 0) if side == "right" else 0
        x1 = page_w if side == "right" else min(band, page_w)
        y0 = max(box["top_pt"] - 40, 0)
        y1 = min(max(box["bottom_pt"] + 40, y0 + 60), page_h)
    else:
        y0 = max(page_h - band, 0) if side == "bottom" else 0
        y1 = page_h if side == "bottom" else min(band, page_h)
        x0 = max(box["left_pt"] - 20, 0)
        x1 = min(x0 + window, page_w)
    crop = im.crop((int(x0 * sx), int(y0 * sy), int(math.ceil(x1 * sx)), int(math.ceil(y1 * sy))))
    scale = max(2, min(4, int(1500 / max(crop.width, 1)) + 1))
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    draw = ImageDraw.Draw(crop)
    if side == "right":
        draw.line([(crop.width - 2, 0), (crop.width - 2, crop.height)], fill=(214, 40, 40), width=5)
    elif side == "bottom":
        draw.line([(0, crop.height - 2), (crop.width, crop.height - 2)], fill=(214, 40, 40), width=5)
    elif side == "left":
        draw.line([(1, 0), (1, crop.height)], fill=(214, 40, 40), width=5)
    else:
        draw.line([(0, 1), (crop.width, 1)], fill=(214, 40, 40), width=5)
    label = f"{case['case_id']} 页面{SIDE_CN[side]}边缘 ×{scale}（红线=页面边界）"
    tw = draw.textlength(label, font=_font(22, True))
    draw.rectangle([0, 0, min(tw + 20, crop.width), 34], fill=(255, 255, 255))
    draw.text((10, 6), label, font=_font(22, True), fill=(214, 40, 40))
    crop.save(out_path)
    return {"zoom_png": str(out_path), "zoom_side": side, "zoom_px": [crop.width, crop.height]}



# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def classify(case: dict[str, Any]) -> dict[str, Any]:
    vis = case["visibility"]
    bbox = case["text_layout"]["visible_bbox_pt"]
    overflow = case["page_overflow"]["sides"]

    if not vis["has_text_frame"] or not vis["has_text"] or bbox is None:
        if vis["empty_graphics"]:
            return _verdict("not_applicable", "high",
                            ["对象无文字，且无填充、无边框、无效果；渲染不可见",
                             "保留第一阶段几何记录，但不构成可见文字缺陷"],
                            ["com_shape_properties", "render"],
                            defect="none",
                            multimodal=False)
        return _verdict("not_applicable", "high",
                        ["对象没有可见文字（无文字内容或文字为空段落）"],
                        ["com_textframe2"], defect="none", multimodal=False)

    if not vis["shape_visible"]:
        return _verdict("not_applicable", "high", ["形状 Visible=False"], ["com_shape_properties"],
                        defect="none", multimodal=False)

    if case["text_layout"]["probe_error"]:
        return _verdict("uncertain", "low",
                        [f"COM 读取文字范围失败：{case['text_layout']['probe_error']}"],
                        ["com_textframe2"], defect="unknown", multimodal=True,
                        unresolved="无法取得文字排版范围")

    if not overflow:
        ink = case["render_evidence"].get("ink") or {}
        reasons = ["实际文字排版范围完整落在页面内，按页面裁切定义不成立"]
        ib = ink.get("ink_bbox_pt")
        if ink.get("available") and ib:
            gap = min(ib["left_pt"], ib["top_pt"],
                      case["page"]["width_pt"] - ib["right_pt"],
                      case["page"]["height_pt"] - ib["bottom_pt"])
            reasons.append(f"渲染图墨迹距最近页面边缘 {gap:.1f} pt，无字迹贴近边界")
        return _verdict("no_visible_clip", "high", reasons,
                        ["com_textframe2", "rendered_png", "pixel_ink"], defect="none",
                        multimodal=False)

    # 文字范围越界：必须有页边墨迹才确认裁切
    evidence_ok, evidence_bad, notes = [], [], []
    reliable = True
    for side in [s["side"] for s in overflow]:
        edge = (case["render_evidence"].get("ink") or {}).get("edge", {}).get(side, {})
        ratio = edge.get("edge_ink_ratio")
        if ratio is None:
            reliable = False
            notes.append(f"{SIDE_CN[side]}边：无法取得墨迹数据")
            continue
        if ratio >= EDGE_INK_MIN:
            evidence_ok.append(f"{SIDE_CN[side]}边墨迹占比 {ratio:.1%}，字形在页面边界处被切断")
        else:
            notes.append(f"{SIDE_CN[side]}边墨迹占比 {ratio:.1%}，未见字迹贴边")

    if not case["render_evidence"].get("ink", {}).get("bg_reliable", True):
        reliable = False
        notes.append("页面背景复杂（主背景占比过低），页边墨迹判定不可靠")

    if evidence_ok and reliable:
        reasons = ["实际文字排版范围越出页面"] + evidence_ok
        return _verdict("confirmed_clip", "high", reasons,
                        ["com_textframe2", "rendered_png", "pixel_ink"], defect="high",
                        multimodal=False)
    if evidence_ok and not reliable:
        return _verdict("uncertain", "low",
                        ["文字排版范围越出页面"] + evidence_ok + notes,
                        ["com_textframe2", "pixel_ink"], defect="unknown", multimodal=True,
                        unresolved="页边背景复杂，无法可靠区分文字墨迹与背景")
    reasons = ["文字排版范围越出页面，但页边未检出对应墨迹"] + notes
    return _verdict("uncertain", "low", reasons,
                    ["com_textframe2", "rendered_png", "pixel_ink"], defect="unknown",
                    multimodal=True,
                    unresolved="排版越界但无可见墨迹，可能被上层对象遮挡、文字透明或为空白段落")


def _verdict(status: str, confidence: str, reasons: list[str], sources: list[str],
             defect: str, multimodal: bool, unresolved: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "status_cn": STATUS_CN[status],
        "confidence": confidence,
        "reasons": reasons,
        "evidence_sources": sources,
        "defect_severity": defect,
        "needs_multimodal_review": multimodal,
        "unresolved_reason": unresolved,
    }


def annotation_policy(case: dict[str, Any]) -> dict[str, Any]:
    """决定审查图是否绘制该对象的几何框。

    仅当对象被判 not_applicable 且确属“无文字、无填充、无边框、无可见效果”的空形状时隐藏；
    结构数据、坐标与复检记录仍保留在 JSON。
    """
    v = case["visibility"]
    empty = (not v["has_text"] and bool(v["empty_graphics"])
             and v["fill_visible"] in (0, None) and v["line_visible"] in (0, None)
             and v.get("xml_effect") is False)
    suppress = case["second_pass"]["status"] == "not_applicable" and empty
    return {
        "frame_suppressed": suppress,
        "frame_drawn": None,          # 由渲染阶段回填
        "suppress_reason": (
            "对象无文字、无填充、无边框、无可见效果，渲染不可见；默认不绘制几何边界框，"
            "结构数据与坐标仍保留在 geometry_evidence"
        ) if suppress else None,
        "debug_switch": "--debug-frames",
    }


# --------------------------------------------------------------------------- #
# 组装单个案例
# --------------------------------------------------------------------------- #
def build_case(meta: dict[str, Any], shape: dict[str, Any], page: dict[str, float],
               stage1_rec: dict[str, Any] | None, png: Path | None,
               pptx_path: Path, slide_idx: int) -> dict[str, Any]:
    geom = shape_geometry(shape)
    inner = inner_frame(shape, geom)
    lay = layout_bbox(shape)
    vis, vis_meta = visible_bbox(shape)
    overflow = overflow_sides(vis, page["width_pt"], page["height_pt"])
    exceed = frame_exceed(vis, inner)

    xml_facts = text_xml_facts(pptx_path, slide_idx, shape["id"])
    vis_meta["trailing_blank_paragraphs_in_xml"] = xml_facts["trailing_blank_paragraphs_in_xml"]

    autofit_note = None
    explicit = xml_facts.get("explicit_font_pt") or []
    eff = shape["font_size_eff"]
    if shape["autosize"] == 2 and exceed and eff and explicit and abs(eff - max(explicit)) < 0.01:
        autofit_note = (
            f"自动缩放（溢出时缩小文字）已开启，但实际字号仍为 {eff:g} pt，与显式字号一致；"
            "PowerPoint 未重算 fontScale，本次渲染没有缩小"
        )

    layout_overflow = overflow_sides(lay, page["width_pt"], page["height_pt"])

    empty_graphics = (not shape["has_text"]) and shape["fill_visible"] in (0, None) \
        and shape["line_visible"] in (0, None) and xml_facts.get("xml_effect") is False

    case: dict[str, Any] = {
        "case_id": meta["case_id"],
        "group": meta["group"],
        "source": meta["source"],
        "file": pptx_path.name,
        "deck_path": str(pptx_path),
        "slide": slide_idx,
        "shape_id": shape["id"],
        "name": shape["name"],
        "shape_type": shape["shape_type"],
        "text_excerpt": (shape.get("text") or "")[:160],
        "page": {
            "width_pt": page["width_pt"], "height_pt": page["height_pt"],
            "width_in": to_inch(page["width_pt"]), "height_in": to_inch(page["height_pt"]),
        },
        "geometry_evidence": {
            "label": "geometry_evidence",
            "note": "第一阶段仅判断对象外框越界，不代表文字被裁切",
            "first_pass_detector": "S-OVERSHOOT" if stage1_rec else None,
            "frame_bbox_pt": geom,
            "frame_bbox_in": bbox_with_inch(geom),
            "violations": stage1_rec["directions"] if stage1_rec else [],
            "geometry_severity": stage1_rec["severity"] if stage1_rec else None,
            "frame_overflow_sides": overflow_sides(geom, page["width_pt"], page["height_pt"]),
            "in_stage1_result": stage1_rec is not None,
        },
        "visibility": {
            "shape_visible": bool(shape["visible"] != 0),
            "has_text_frame": shape["has_text_frame"],
            "has_text": shape["has_text"],
            "fill_visible": shape["fill_visible"],
            "line_visible": shape["line_visible"],
            "xml_effect": xml_facts.get("xml_effect"),
            "empty_graphics": empty_graphics,
            "z_order": shape["z"],
        },
        "text_layout": {
            "source": "PowerPoint COM TextFrame2",
            "layout_bbox_pt": lay,
            "layout_bbox_in": bbox_with_inch(lay),
            "visible_bbox_pt": vis,
            "visible_bbox_in": bbox_with_inch(vis),
            "visible_basis": vis_meta,
            "inner_frame_pt": inner,
            "text_exceeds_inner_frame": exceed,
            "autosize": shape["autosize"],
            "word_wrap": shape["word_wrap"],
            "anchor": shape["anchor"],
            "effective_font_pt": shape["font_size_eff"],
            "explicit_font_pt": xml_facts.get("explicit_font_pt"),
            "xml_autofit": xml_facts.get("xml_autofit"),
            "font_scale_attr": xml_facts.get("font_scale_attr"),
            "autofit_note": autofit_note,
            "paragraph_font_pt": [p.get("font_size") for p in shape.get("paragraphs") or []],
            "probe_error": shape.get("probe_error"),
        },
        "page_overflow": {
            "basis": "visible_bbox",
            "sides": overflow,
            "max_pt": max([s["distance_pt"] for s in overflow], default=0.0),
            "layout_bbox_sides": layout_overflow,
            "layout_only_note": (
                "整段 Bound* 越界但可见段落未越界；差额来自末尾空段落"
                if layout_overflow and not overflow else None
            ),
        },
        "render_evidence": {"page_png": str(png) if png else None},
        "second_pass": {},
    }

    if png and png.exists() and vis is not None:
        sides = [s["side"] for s in overflow]
        ink = ink_analysis(png, vis, page["width_pt"], page["height_pt"], sides)
        case["render_evidence"]["ink"] = ink
    case["second_pass"] = classify(case)

    # 记录"文字溢出框外但仍可见"这一独立质量信号（不是裁切）
    if exceed:
        case["second_pass"]["quality_signals"] = ["text_spills_outside_frame"]
        case["second_pass"]["reasons"].append(
            "文字排版范围超出文本框内部区域（PowerPoint 不裁切文本框内文字，溢出部分仍可见）"
        )
        if case["second_pass"]["defect_severity"] == "none":
            case["second_pass"]["defect_severity"] = "low"
    if autofit_note:
        case["second_pass"].setdefault("quality_signals", []).append("autofit_shrink_inactive")
        case["second_pass"]["reasons"].append(autofit_note)

    # 文字溢出框外但仍可见：裁切结论已确定，但可读性/遮挡需人工或多模态判断
    if case["second_pass"]["status"] == "no_visible_clip" and exceed:
        case["second_pass"]["needs_multimodal_review"] = True
        case["second_pass"]["multimodal_question"] = (
            "文字溢出文本框后与相邻内容重叠，是否影响可读性？"
        )
    case["annotation"] = annotation_policy(case)
    return case


def shape_has_effect(shape: Any) -> bool | None:
    """XML 层判断对象是否带可见效果（阴影/发光/映像/柔化等）。

    None 表示无法判断；空 `<a:effectLst/>` 视为无效果。
    注意 lxml 元素不能用真值判断，必须显式比较 len()。
    """
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        el = shape._element
        sp_pr = getattr(el, "spPr", None)
        if sp_pr is None:
            sp_pr = el.find(f".//{ns}spPr")
        if sp_pr is None:
            return None
        for tag in ("effectLst", "effectDag"):
            node = sp_pr.find(f"{ns}{tag}")
            if node is not None and len(node) > 0:
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


def text_xml_facts(pptx_path: Path, slide_idx: int, shape_id: int) -> dict[str, Any]:
    """从 XML 补充事实：末尾空段落数、显式字号、自动缩放节点、是否带效果。"""
    out: dict[str, Any] = {
        "trailing_blank_paragraphs_in_xml": None,
        "explicit_font_pt": [],
        "xml_autofit": None,
        "xml_effect": None,
    }
    try:
        prs = Presentation(str(pptx_path))
        slide = list(prs.slides)[slide_idx - 1]
        for shape in slide.shapes:
            if shape.shape_id != shape_id:
                continue
            out["xml_effect"] = shape_has_effect(shape)
            if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
                return out
            tf = shape.text_frame
            paras = list(tf.paragraphs)
            n = 0
            for para in reversed(paras):
                if is_blank_text("".join(r.text for r in para.runs)):
                    n += 1
                else:
                    break
            out["trailing_blank_paragraphs_in_xml"] = n
            sizes = []
            for para in paras:
                for run in para.runs:
                    if run.font.size is not None:
                        sizes.append(round(run.font.size.pt, 2))
            out["explicit_font_pt"] = sorted(set(sizes))
            # bodyPr 子节点：noAutofit / normAutofit / spAutoFit
            body = tf._txBody.find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}bodyPr")
            if body is not None:
                for child in body:
                    tag = child.tag.split("}")[-1]
                    if tag in ("noAutofit", "normAutofit", "spAutoFit"):
                        out["xml_autofit"] = tag
                        if tag == "normAutofit":
                            out["font_scale_attr"] = child.get("fontScale")
            return out
    except Exception:  # noqa: BLE001
        return out
    return out


# --------------------------------------------------------------------------- #
# 目标清单
# --------------------------------------------------------------------------- #
SHAPE_TYPE_CN = {
    1: "AUTO_SHAPE", 13: "PICTURE", 14: "PLACEHOLDER", 17: "TEXT_BOX",
}


def load_stage1() -> tuple[dict[str, Any], dict[str, Any]]:
    structure = json.loads((STAGE1_DIR / "stage1_structure.json").read_text(encoding="utf-8"))
    violations = json.loads((STAGE1_DIR / "stage1_violations.json").read_text(encoding="utf-8"))
    return structure, violations


def real_targets(violations: dict[str, Any]) -> list[dict[str, Any]]:
    order = {
        ("AddAgendaSlide_TestA.pptx", 3, 13): "R1",
        ("CalibriToTimesTestA.pptx", 14, 4): "R2",
        ("CenterAlignTitles_TestA.pptx", 4, 4): "R3",
        ("CenterAlignTitles_TestA.pptx", 5, 4): "R4",
    }
    targets = []
    for rec in violations["violations"]:
        key = (rec["file"], rec["slide"], rec["shape_id"])
        targets.append({
            "case_id": order.get(key, f"R{len(targets) + 1}"),
            "group": "real_case",
            "source": "stage1_violation",
            "deck": DECK_DIR / rec["file"],
            "slide": rec["slide"],
            "shape_id": rec["shape_id"],
            "shape_type": rec["shape_type"],
            "stage1": rec,
        })
    targets.sort(key=lambda t: t["case_id"])
    return targets


def sample_targets(deck: Path) -> list[dict[str, Any]]:
    """样本目录：扫描全部页，挑出有几何/文字范围问题的对象（帧内越界或页面越界）。

    这里需要全页扫描，因为"文字越出页面但外框在页内"的对象第一阶段不会报出。
    候选筛选在拿到 COM 数据后进行。
    """
    if not deck.exists():
        return []
    return [{
        "case_id": "S",
        "group": "synthetic_sample",
        "source": "sample_scan",
        "deck": deck,
        "slide": None,
        "shape_id": None,
    }]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(force: bool = False, skip_render: bool = False,
        debug_frames: bool = False) -> dict[str, Any]:
    structure, violations = load_stage1()
    stage1_index = {
        (d["file"], s["index"], sh["shape_id"]): (d, s, sh)
        for d in structure["decks"] for s in d["slides"] for sh in s["shapes"]
    }
    stage1_hashes = {d["file"]: d["sha256"] for d in structure["decks"]}

    cases: list[dict[str, Any]] = []
    probe_errors: list[str] = []
    render_meta: dict[str, Any] = {}
    ppt_versions: list[str] = []
    probes: dict[str, dict[str, Any]] = {}

    # ---------- 真实案例 ----------
    targets = real_targets(violations)
    by_deck: dict[Path, list[dict[str, Any]]] = {}
    for t in targets:
        by_deck.setdefault(Path(t["deck"]), []).append(t)

    for deck, group in by_deck.items():
        slides = sorted({t["slide"] for t in group})
        png_dir = None if skip_render else RENDER / deck.stem
        probe = probe_deck(deck, slides, png_dir, force=force)
        probes[deck.name] = probe
        if probe["errors"]:
            probe_errors.extend(f"{deck.name}: {e}" for e in probe["errors"])
        if probe.get("powerpoint_version"):
            ppt_versions.append(probe["powerpoint_version"])
        page = {"width_pt": probe["slide_width_pt"], "height_pt": probe["slide_height_pt"]}
        index = {s["index"]: s for s in probe["slides"]}
        for t in group:
            slide_rec = index.get(t["slide"])
            shape = next((s for s in (slide_rec or {}).get("shapes", []) if s["id"] == t["shape_id"]), None)
            if shape is None:
                probe_errors.append(f"{deck.name} p{t['slide']} id={t['shape_id']}: COM 未找到该形状")
                continue
            _, _, stage1_sh = stage1_index.get((deck.name, t["slide"], t["shape_id"]), (None, None, None))
            shape = dict(shape)
            shape["shape_type"] = t["shape_type"]
            png = Path(slide_rec["png"]) if slide_rec.get("png") else None
            case = build_case(t, shape, page, t["stage1"], png, deck, t["slide"])
            if stage1_sh:
                case["geometry_evidence"]["stage1_object"] = {
                    "name": stage1_sh["name"], "has_text": stage1_sh["has_text"],
                    "text": stage1_sh["text"][:120],
                }
            cases.append(case)

    # ---------- 合成样本 ----------
    sample_case_no = 0
    if SAMPLES.exists():
        probe = probe_deck(SAMPLES, None, None if skip_render else RENDER / SAMPLES.stem, force=force)
        probes[SAMPLES.name] = probe
        if probe["errors"]:
            probe_errors.extend(f"{SAMPLES.name}: {e}" for e in probe["errors"])
        if probe.get("powerpoint_version"):
            ppt_versions.append(probe["powerpoint_version"])
        page = {"width_pt": probe["slide_width_pt"], "height_pt": probe["slide_height_pt"]}
        for slide_rec in probe["slides"]:
            slide_no = slide_rec["index"]
            for shape in slide_rec["shapes"]:
                if not shape["has_text"]:
                    continue
                geom = shape_geometry(shape)
                inner = inner_frame(shape, geom)
                vis, _ = visible_bbox(shape)
                ov = overflow_sides(vis, page["width_pt"], page["height_pt"])
                exc = frame_exceed(vis, inner)
                frame_ov = overflow_sides(geom, page["width_pt"], page["height_pt"])
                # 只保留有触发条件的对象：文字越界 / 文字溢出框 / 外框越界
                if not (ov or exc or frame_ov):
                    continue
                sample_case_no += 1
                meta = {
                    "case_id": f"S{sample_case_no}",
                    "group": "synthetic_sample",
                    "source": "sample_scan",
                }
                png = Path(slide_rec["png"]) if slide_rec.get("png") else None
                case = build_case(meta, shape, page, None, png, SAMPLES, slide_no)
                case["sample_label"] = shape["name"]
                cases.append(case)

    # ---------- 证据图 ----------
    if not skip_render:
        for case in cases:
            png = case["render_evidence"].get("page_png")
            if not png or not Path(png).exists():
                continue
            name = f"{case['case_id']}_{Path(case['file']).stem}_p{case['slide']}_id{case['shape_id']}"
            try:
                meta = annotate_case(case, Path(png), EVIDENCE / f"{name}_annotated.png",
                                     debug_frames=debug_frames)
                case["render_evidence"]["annotated_png"] = _rel(Path(meta["annotated_png"]))
                case["annotation"].update({
                    "frame_drawn": meta["frame_drawn"],
                    "frame_suppressed": meta["frame_suppressed"],
                    "debug_frames": meta["debug_frames"],
                })
                if case["second_pass"]["status"] == "confirmed_clip":
                    z = zoom_crop(case, Path(png), EVIDENCE / f"{name}_zoom.png")
                    if z:
                        case["render_evidence"].update({k: _rel(Path(v)) if k.endswith("png") else v
                                                        for k, v in z.items()})
            except Exception as exc:  # noqa: BLE001
                case["render_evidence"]["annotate_error"] = f"{type(exc).__name__}: {exc}"
            case["render_evidence"]["page_png"] = _rel(Path(png))

    # ---------- 源文件校验 ----------
    source_check = []
    for deck in {Path(c["deck_path"]) for c in cases}:
        after = sha256_of(deck)
        expected = stage1_hashes.get(deck.name)
        source_check.append({
            "file": deck.name,
            "sha256_after": after,
            "sha256_stage1": expected,
            "unchanged": (after == expected) if expected else None,
        })

    summary = summarize(cases)
    sample_check = compare_samples(cases)
    page_coverage = build_page_coverage(structure, probes, cases)
    result = {
        "generated_at": now_iso(),
        "tool": {
            "name": "text_visibility_recheck",
            "stage": 3,
            "version": "1.0",
            "python": sys.version.split()[0],
            "powerpoint": ppt_versions[0] if ppt_versions else None,
            "render": f"{RENDER_W}x{RENDER_H} PNG via PowerPoint Slide.Export",
        },
        "policy": {
            "page_tolerance_pt": PAGE_TOL_PT,
            "inner_tolerance_pt": INNER_TOL_PT,
            "edge_strip_px": EDGE_STRIP_PX,
            "edge_ink_min": EDGE_INK_MIN,
            "ink_diff": INK_DIFF,
            "visible_bbox": "非空段落 Bound* 的并集；排除末尾空段落",
            "confirmed_clip": "文字范围越出页面 且 页边条带检出墨迹",
            "no_visible_clip": "文字范围在页面内；或越界但页边无墨迹（不代表证明所有文字完整）",
            "not_applicable": "无文字或渲染不可见的对象",
        },
        "stage1_inputs": {
            "structure": str(STAGE1_DIR / "stage1_structure.json"),
            "violations": str(STAGE1_DIR / "stage1_violations.json"),
            "violation_count": violations["summary"]["violation_count"],
        },
        "summary": summary,
        "sample_check": sample_check,
        "page_coverage": page_coverage,
        "unresolved_items": [
            {
                "topic": "文本框内部文字裁切",
                "status": "unresolved",
                "reason": "实测 PowerPoint 对普通文本框的溢出文字继续绘制在框外（不裁切），"
                          "本批样本无法构造该正例；只有页面边界会真正切掉字形。",
                "evidence": "test_samples/recheck/recheck_samples.pptx 第 4、5 页渲染",
                "next_step": "若需覆盖该场景，应改测表格单元格、图片占位符或艺术字等会裁切内容的容器",
            },
            {
                "topic": "文字越界但页边无墨迹时的可读性",
                "status": "needs_multimodal",
                "reason": "可能是被上层对象遮挡、文字透明或与背景同色，本地像素证据不足",
                "next_step": "交给后续多模态模型判断该区域是否真的看不到文字",
            },
        ],
        "cases": cases,
        "source_check": source_check,
        "probe_errors": probe_errors,
        "render_meta": render_meta,
    }
    return result


def compare_samples(cases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按人工核定的期望标签做验收比对（只比对，不参与判定）。"""
    expected_file = SAMPLES.parent / "expected.json"
    if not expected_file.exists():
        return None
    exp = json.loads(expected_file.read_text(encoding="utf-8"))
    labels = exp.get("labels", {})
    items = []
    for c in cases:
        if c["group"] != "synthetic_sample":
            continue
        key = c.get("sample_label") or c["name"]
        e = labels.get(key)
        if not e:
            continue
        items.append({
            "case_id": c["case_id"],
            "sample": key,
            "expected": e["expected"],
            "actual": c["second_pass"]["status"],
            "pass": e["expected"] == c["second_pass"]["status"],
            "intent": e.get("intent"),
        })
    return {
        "expected_file": str(expected_file),
        "total": len(items),
        "passed": sum(1 for i in items if i["pass"]),
        "failed": [i for i in items if not i["pass"]],
        "items": items,
        "known_unresolved": exp.get("known_unresolved", []),
    }


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(HERE.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def build_page_coverage(structure: dict[str, Any], probes: dict[str, dict[str, Any]],
                        cases: list[dict[str, Any]]) -> dict[str, Any]:
    """统计每一页“含文字对象 / 已复检 / 跳过”，并说明第二轮的真实覆盖范围。

    真实样本的第二轮候选只来自第一阶段越界记录，因此页内绝大多数文字对象并未复检。
    这份台账用于避免把单个对象的 not_applicable 误读成整页无问题。
    """
    s1_text: dict[tuple[str, int], dict[int, str]] = {}
    for deck in structure["decks"]:
        for slide in deck["slides"]:
            s1_text[(deck["file"], slide["index"])] = {
                sh["shape_id"]: sh["name"] for sh in slide["shapes"] if sh["has_text"]
            }

    rows: list[dict[str, Any]] = []
    real_pages = sorted({(c["file"], c["slide"]) for c in cases if c["group"] == "real_case"})
    for file, slide in real_pages:
        names = s1_text.get((file, slide), {})
        page_cases = [c for c in cases
                      if c["group"] == "real_case" and c["file"] == file and c["slide"] == slide]
        checked = [c for c in page_cases
                   if c["visibility"]["has_text"]
                   and c["second_pass"]["status"] != "not_applicable"]
        checked_ids = {c["shape_id"] for c in checked} & set(names)
        skipped = sorted(set(names) - checked_ids)
        rows.append({
            "file": file,
            "slide": slide,
            "source": "stage1_candidates_only",
            "text_objects_total": len(names),
            "text_objects_checked": len(checked_ids),
            "text_objects_skipped": len(skipped),
            "checked_shape_ids": sorted(checked_ids),
            "skipped_shape_ids": skipped,
            "skipped_names": [names[i] for i in skipped],
            "stage1_candidates": len(page_cases),
            "stage1_candidates_with_text": sum(1 for c in page_cases if c["visibility"]["has_text"]),
            "stage1_candidates_not_applicable": sum(
                1 for c in page_cases if c["second_pass"]["status"] == "not_applicable"),
            "scope_note": "第二轮仅复检第一阶段越界候选；本页其余含文字对象未做 COM 排版与像素核对",
            "interpretation_note": (
                "本页候选为空形状（无文字），其 not_applicable 只说明该对象不适用，"
                "不能推断本页文字没有裁切风险"
            ) if any(not c["visibility"]["has_text"] for c in page_cases) else None,
        })

    sample_probe = probes.get(SAMPLES.name)
    if sample_probe:
        for slide_rec in sample_probe["slides"]:
            idx = slide_rec["index"]
            text_ids = [s["id"] for s in slide_rec["shapes"] if s["has_text"]]
            names = {s["id"]: s["name"] for s in slide_rec["shapes"] if s["has_text"]}
            page_cases = [c for c in cases
                          if c["group"] == "synthetic_sample" and c["slide"] == idx]
            checked_ids = {c["shape_id"] for c in page_cases} & set(text_ids)
            skipped = sorted(set(text_ids) - checked_ids)
            rows.append({
                "file": SAMPLES.name,
                "slide": idx,
                "source": "synthetic_full_scan",
                "text_objects_total": len(text_ids),
                "text_objects_checked": len(checked_ids),
                "text_objects_skipped": len(skipped),
                "checked_shape_ids": sorted(checked_ids),
                "skipped_shape_ids": skipped,
                "skipped_names": [names[i] for i in skipped],
                "stage1_candidates": None,
                "stage1_candidates_with_text": None,
                "stage1_candidates_not_applicable": None,
                "scope_note": "合成样本按全页扫描，所有含文字对象都参与候选筛选",
                "interpretation_note": None,
            })

    real_rows = [r for r in rows if r["source"] == "stage1_candidates_only"]
    return {
        "note": "真实样本的第二轮只覆盖第一阶段越界候选，不是全页文字扫描；"
                "checked + skipped = 本页含文字对象总数",
        "real_pages": {
            "text_objects_total": sum(r["text_objects_total"] for r in real_rows),
            "text_objects_checked": sum(r["text_objects_checked"] for r in real_rows),
            "text_objects_skipped": sum(r["text_objects_skipped"] for r in real_rows),
        },
        "pages": rows,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    def counts(group: str) -> dict[str, int]:
        sub = [c for c in cases if c["group"] == group]
        return {k: sum(1 for c in sub if c["second_pass"]["status"] == k)
                for k in ("confirmed_clip", "no_visible_clip", "uncertain", "not_applicable")}

    real = [c for c in cases if c["group"] == "real_case"]
    return {
        "total": len(cases),
        "real_cases": len(real),
        "synthetic_samples": len(cases) - len(real),
        "real": counts("real_case"),
        "samples": counts("synthetic_sample"),
        "needs_multimodal": sum(1 for c in cases if c["second_pass"]["needs_multimodal_review"]),
        "real_not_confirmed_clip": sum(
            1 for c in real
            if c["geometry_evidence"]["in_stage1_result"] and c["second_pass"]["status"] != "confirmed_clip"
        ),
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_report(result: dict[str, Any]) -> str:
    cases = result["cases"]
    real = [c for c in cases if c["group"] == "real_case"]
    samples = [c for c in cases if c["group"] == "synthetic_sample"]
    s = result["summary"]

    lines: list[str] = []
    lines.append("# 第三步 · 文字可见性复检报告\n")
    lines.append(f"生成时间：{result['generated_at']}　|　程序：`outputs/recheck/text_visibility_recheck.py`　"
                 f"|　渲染：PowerPoint {result['tool']['powerpoint']}，{result['tool']['render']}\n")
    lines.append("> 本报告只回答“**实际可见文字**是否越出页面或被页面裁切”。第一阶段的外框越界结果"
                 "以 `geometry_evidence` 原样保留，**不作为文字裁切的证据**。\n")

    lines.append("## 一、结论摘要\n")
    lines.append("| 组 | 条目 | 已确认裁切 | 未发现可见裁切 | 不确定 | 不适用 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    lines.append(f"| 第一阶段 4 条真实记录 | {s['real_cases']} | {s['real']['confirmed_clip']} | "
                 f"{s['real']['no_visible_clip']} | {s['real']['uncertain']} | {s['real']['not_applicable']} |")
    lines.append(f"| 合成对照样本 | {s['synthetic_samples']} | {s['samples']['confirmed_clip']} | "
                 f"{s['samples']['no_visible_clip']} | {s['samples']['uncertain']} | {s['samples']['not_applicable']} |")
    lines.append("")
    lines.append(f"建议交给后续多模态复核：**{s['needs_multimodal']} 条**。\n")

    pc = result.get("page_coverage") or {}
    real_cov = [r for r in pc.get("pages", []) if r["source"] == "stage1_candidates_only"]
    sample_cov = [r for r in pc.get("pages", []) if r["source"] == "synthetic_full_scan"]
    lines.append("## 二、复检覆盖范围（真实样本不是全页扫描）\n")
    lines.append("第二轮的真实候选**只来自第一阶段 4 条越界记录**，没有遍历每页所有含文字对象；"
                 "合成样本才是全页扫描。已探测页面的台账：\n")
    lines.append("| 文件 | 页 | 含文字对象 | 已复检 | 未复检 | 本轮候选 | 候选中的空形状 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in real_cov:
        lines.append(f"| {r['file']} | {r['slide']} | {r['text_objects_total']} | "
                     f"{r['text_objects_checked']} | {r['text_objects_skipped']} | "
                     f"{r['stage1_candidates']} | {r['stage1_candidates_not_applicable']} |")
    lines.append("")
    p4 = next((r for r in real_cov
               if r["file"] == "CenterAlignTitles_TestA.pptx" and r["slide"] == 4), None)
    if p4:
        lines.append(f"**第 4 页的具体数字**：页内识别到 **{p4['text_objects_total']}** 个含文字对象，"
                     f"本轮成功检查 **{p4['text_objects_checked']}** 个，跳过 **{p4['text_objects_skipped']}** 个。"
                     "唯一候选是 id=4 的空形状（无文字）：对象读取成功 1 个，但文字可见性检查为 0 个。"
                     "因此 `not_applicable` 只说明该对象不适用，"
                     "**不能推断整页没有文字裁切问题**。\n")
    if sample_cov:
        st = sum(r["text_objects_total"] for r in sample_cov)
        sc = sum(r["text_objects_checked"] for r in sample_cov)
        lines.append(f"合成样本：{len(sample_cov)} 页、含文字对象 {st} 个，全部参与候选筛选，"
                     f"{sc} 个触发条件并完成复检。\n")
    lines.append("计数口径：真实页取第一阶段结构 JSON 的叶子文字对象（组容器不重复计数）；"
                 "合成样本取 COM 全页扫描结果。\n")

    lines.append("### 判定口径\n")
    lines.append("每个对象走三层证据，**后一层只用于确认，不用于推翻前一层的事实**：\n")
    lines.append("1. **COM 排版层**：PowerPoint `TextFrame2.TextRange` 给出每段文字的 `Bound*`，"
                 "取**非空段落**的并集作为 `visible_bbox`；整段范围另存为 `layout_bbox` 作对照。"
                 "这样既拿到真实排版范围，又不会把末尾空段落算成文字。")
    lines.append("2. **像素层**：用 PowerPoint 导出 144 dpi 页面图，在 `visible_bbox` 内统计墨迹范围，"
                 "并在页面边缘 3 px 带内统计墨迹占比；文字越界且页边有墨迹才判 `confirmed_clip`。")
    lines.append("3. **局部放大层**：对 `confirmed_clip` 生成页边 2–4 倍放大图，人工可核。")
    lines.append("")
    lines.append("状态含义：`confirmed_clip` = 排版越界且页边确有字迹被切断；"
                 "`no_visible_clip` = **本次观察范围内**未发现可见文字裁切（不等于证明所有文字都完整）；"
                 "`uncertain` = 有越界迹象但证据不足；`not_applicable` = 无文字或渲染不可见。")
    lines.append("")
    lines.append("未使用 OCR：字符是否被切由“排版范围 + 页边墨迹 + 放大图”三重证据判断。"
                 "OCR 缺字可能来自识别误差，本阶段不把它作为裁切证据。\n")

    lines.append("## 三、第一次 vs 第二次（真实记录）\n")
    lines.append("| # | 文件 / 页 / 对象 | 第一次（外框几何） | 第二次（可见文字） | 变化 |")
    lines.append("|---|---|---|---|---|")
    for c in real:
        g = c["geometry_evidence"]
        dirs = "、".join(f"{SIDE_CN[d['side']]}{d['distance_pt']:.1f}pt" for d in g["violations"]) or "—"
        first = f"S-OVERSHOOT（{dirs}；几何级别 {g['geometry_severity']}）" if g["in_stage1_result"] else "未报出"
        st = c["second_pass"]
        if st["status"] == "confirmed_clip":
            changed = "升级为已确认裁切"
        elif st["status"] == "not_applicable":
            changed = "降级为不适用（渲染不可见）"
        elif st["status"] == "uncertain":
            changed = "降级为不确定"
        else:
            changed = "维持（非文字裁切）"
        lines.append(f"| {c['case_id']} | {c['file']} p{c['slide']} id={c['shape_id']} | {first} | "
                     f"**{st['status']}**（缺陷级别 {st['defect_severity']}） | {changed} |")
    lines.append("")

    lines.append("## 四、逐条证据\n")
    for c in real + samples:
        st = c["second_pass"]
        tl = c["text_layout"]
        lines.append(f"### {c['case_id']} · {c['file']} · 第 {c['slide']} 页 · id={c['shape_id']} "
                     f"（{c['name']}）\n")
        lines.append(f"- **状态**：`{st['status']}`（{st['status_cn']}），置信度 {st['confidence']}，"
                     f"缺陷级别 `{st['defect_severity']}`")
        lines.append(f"- **对象**：{c['shape_type']}；文字：{_quote(c['text_excerpt'])}")
        lines.append(f"- **外观属性**：填充 visible={c['visibility']['fill_visible']}、"
                     f"边框 visible={c['visibility']['line_visible']}、形状 visible={c['visibility']['shape_visible']}"
                     f"、XML 效果={c['visibility'].get('xml_effect')}")
        ann = c.get("annotation") or {}
        if ann.get("frame_suppressed"):
            if ann.get("frame_drawn"):
                lines.append("- **审查图边界框**：调试模式已绘制（默认审查图不绘制）")
            else:
                lines.append("- **审查图边界框**：已隐藏 —— 空形状（无文字、无填充、无边框、无可见效果）；"
                             "坐标与结构仍保留在 `geometry_evidence`")
        g = c["geometry_evidence"]
        if g["in_stage1_result"]:
            lines.append(f"- **第一次几何证据**：外框 {_fmt_box(g['frame_bbox_pt'])}；"
                         f"越界 {_fmt_dirs(g['violations'])}（几何级别 {g['geometry_severity']}）")
        else:
            lines.append(f"- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；"
                         f"外框 {_fmt_box(g['frame_bbox_pt'])}，本次补算外框越界 "
                         f"{_fmt_dirs(g['frame_overflow_sides']) or '无'}")
        lines.append(f"- **第二次文字范围**：可见文字 {_fmt_box(tl['visible_bbox_pt'])}；"
                     f"整段（含空段落）{_fmt_box(tl['layout_bbox_pt'])}")
        lines.append(f"- **页面**：{c['page']['width_pt']:.0f}×{c['page']['height_pt']:.0f} pt；"
                     f"文字越界 {_fmt_dirs(c['page_overflow']['sides']) or '无'}")
        if tl["text_exceeds_inner_frame"]:
            lines.append(f"- **框内压力**：文字超出文本框内部区域 {_fmt_dirs(tl['text_exceeds_inner_frame'])}"
                         f"（PowerPoint 不裁切框内文字，溢出仍可见）")
        if c["page_overflow"].get("layout_only_note"):
            lines.append(f"- **空段落影响**：{c['page_overflow']['layout_only_note']}"
                         f"（XML 末尾空段落 {tl['visible_basis'].get('trailing_blank_paragraphs_in_xml')} 个）")
        ink = (c.get("render_evidence") or {}).get("ink") or {}
        if ink.get("available"):
            lines.append(f"- **像素核对**：页面主背景亮度 {ink['bg_lum']}（占比 {ink['bg_share']:.0%}）；"
                         f"文字区内墨迹 {ink['ink_ratio']:.1%}；墨迹范围 {_fmt_box(ink.get('ink_bbox_pt'))}")
            for side, e in (ink.get("edge") or {}).items():
                if e.get("edge_ink_ratio") is None:
                    continue
                lines.append(f"  - {SIDE_CN[side]}边 3px 带墨迹占比 {e['edge_ink_ratio']:.1%}"
                             f"（{e['edge_ink_px']}/{e['strip_px']} px）；墨迹距该边 "
                             f"{e.get('ink_gap_to_edge_pt')} pt")
        for r in st["reasons"]:
            lines.append(f"- **判定依据**：{r}")
        if st.get("quality_signals"):
            lines.append(f"- **附带信号**：{', '.join(st['quality_signals'])}")
        if st.get("unresolved_reason"):
            lines.append(f"- **未解决原因**：{st['unresolved_reason']}")
        lines.append(f"- **是否建议多模态复核**："
                     f"{'是 —— ' + st['multimodal_question'] if st.get('multimodal_question') else ('是' if st['needs_multimodal_review'] else '否')}")
        if c.get("sample_label"):
            lines.append(f"- **样本身份**：`{c['sample_label']}`")
        pics = []
        if c["render_evidence"].get("annotated_png"):
            pics.append(f"[标注图]({c['render_evidence']['annotated_png']})")
        if c["render_evidence"].get("zoom_png"):
            pics.append(f"[页边放大]({c['render_evidence']['zoom_png']})")
        if c["render_evidence"].get("page_png"):
            pics.append(f"[原始渲染]({c['render_evidence']['page_png']})")
        if pics:
            lines.append(f"- **截图**：{' · '.join(pics)}")
        lines.append("")

    lines.append("## 五、合成样本的实测结论\n")
    check = result.get("sample_check") or {}
    by_sample = {i["sample"]: i for i in check.get("items", [])}
    lines.append("| 样本 | 设计意图 | 预期标签 | 程序判定 | 验收 | 渲染实测 |")
    lines.append("|---|---|---|---|---|---|")
    for c in samples:
        label = c.get("sample_label", c["name"])
        intent = {
            "S1_RightFrame_TextVisible": "外框越界、文字完整",
            "S1_BottomFrame_TextVisible": "外框越界、文字完整",
            "S2_RightClippedText": "文字伸出页面右边缘",
            "S2_BottomClippedText": "文字伸出页面下边缘",
            "S3_TightBox_NoAutofit": "小框+长文，观察是否裁切",
            "S3_TightBox_ShrinkText": "小框+长文+自动缩放",
        }.get(label, "—")
        verdict = c["second_pass"]["status"]
        obs = {
            "confirmed_clip": "字符在页边被切断（见放大图）",
            "no_visible_clip": "字符完整可见",
            "not_applicable": "对象不可见",
            "uncertain": "无法确认",
        }[verdict]
        item = by_sample.get(label)
        exp = f"`{item['expected']}`" if item else "—"
        ok = ("✅" if item["pass"] else "❌") if item else "—"
        lines.append(f"| {label} | {intent} | {exp} | `{verdict}` | {ok} | {obs} |")
    lines.append("")
    if check:
        lines.append(f"验收比对：**{check['passed']}/{check['total']} 通过**"
                     f"（期望标签见 `test_samples/recheck/expected.json`，只用于比对，不参与判定）。\n")
    lines.append("样本 3 的关键实测：**PowerPoint 不会把溢出文本框的文字裁掉**，而是继续画在框外。"
                 "因此“文本框内部文字裁切”在普通文本框上不可复现，本阶段不能提供该类型的正例，"
                 "相应检测标记为未解决（见第八节）。\n")

    lines.append("## 六、本次能回答与不能回答的问题\n")
    lines.append("| 验收项 | 结果 | 证据 |")
    lines.append("|---|---|---|")
    r1 = next((c for c in real if c["case_id"] == "R1"), None)
    r2 = next((c for c in real if c["case_id"] == "R2"), None)
    tested = [i for i in (r1, r2) if i]
    if tested:
        ok = all(c["second_pass"]["status"] != "confirmed_clip" for c in tested)
        lines.append(f"| 原来两条“外框越界但文字正常”的案例不再被判为已确认裁切 | "
                     f"{'通过' if ok else '未通过'} | R1 `{r1['second_pass']['status']}`、"
                     f"R2 `{r2['second_pass']['status']}` |")
    na = [c for c in real if c["second_pass"]["status"] == "not_applicable"]
    lines.append(f"| 两条不可见空形状不被报告为可见文字缺陷 | "
                 f"{'通过' if len(na) == 2 else '未通过'} | R3/R4 `not_applicable` |")
    clips = [i for i in check.get("items", []) if i["expected"] == "confirmed_clip"]
    found = [i for i in clips if i["pass"]]
    lines.append(f"| 真实存在文字被页面裁切的样本能被发现 | "
                 f"{'通过' if clips and len(found) == len(clips) else '未通过'} | "
                 f"{len(found)}/{len(clips)}：{'、'.join(i['sample'] for i in found) or '—'} |")
    lines.append("| 文本框内部裁切能否可靠检测 | 未解决 | 实测 PowerPoint 不裁切溢出文本，无正例 |")
    lines.append("| 第一阶段结果是否保留、可追溯 | 通过 | `geometry_evidence` 原样保留；"
                 "源文件 SHA-256 与第一阶段一致 |")
    lines.append("")

    lines.append("## 七、本地已能处理 / 仍需多模态判断\n")
    lines.append("**本地可处理（确定性证据）**\n")
    lines.append("1. 对象外框与页面边界的关系（第一阶段）。")
    lines.append("2. 实际文字排版范围是否越出页面（COM `TextRange`，按非空段落取并集）。")
    lines.append("3. 文字范围是否溢出文本框内部区域（排版压力，不等于裁切）。")
    lines.append("4. 空形状过滤：无填充、无边框、无文字的对象不再进入视觉告警。")
    lines.append("5. 页边墨迹核对：文字越界且页边确有墨迹 → 确认裁切。\n")
    lines.append("**仍需后续多模态智能体判断**\n")
    lines.append("1. 文字越界但被上层对象遮挡、或文字与背景同色导致无墨迹时的真实可读性。")
    lines.append("2. 文字溢出文本框后与相邻元素重叠是否构成可读性问题（本阶段只报几何信号）。")
    lines.append("3. 艺术字、3D 变换、图片内文字（本阶段 COM/像素均无法取得排版）。")
    lines.append("4. 字体替换导致的重新排版（本机字体与原稿作者环境不同）。\n")

    lines.append("## 八、技术限制\n")
    lines.append("1. **COM `Bound*` 不是“可见文字”边界**：它包含末尾空段落。Calibri p14 的整段范围到 "
                 "566.5 pt（越界 26.5 pt），但越界部分是两个空段落，可见文字止于 530.5 pt。"
                 "本模块因此改用非空段落并集，并保留整段范围作对照。")
    lines.append("2. **文本框内部裁切不可复现**：实测（样本 3）PowerPoint 对溢出文字继续绘制在框外，"
                 "不裁切到框内；只有页面边界会真正切掉字形。")
    lines.append("3. **墨迹统计按矩形区域**：区域内其它对象（页码、分隔线、背景图）会被一并计入；"
                 "背景复杂（主背景占比 < 55%）时判定降级为 `uncertain`。")
    cov = pc.get("real_pages") or {}
    lines.append(f"4. **只覆盖候选对象，不是全页扫描**：真实样本只复检第一阶段的 {s['real_cases']} 条记录；"
                 f"这些页共 {cov.get('text_objects_total')} 个含文字对象，实际复检 "
                 f"{cov.get('text_objects_checked')} 个，跳过 {cov.get('text_objects_skipped')} 个。"
                 "其余 58 页完全未做文字范围扫描。")
    lines.append("5. **字体与版本相关**：排版范围由本机 PowerPoint + 本地字体计算，"
                 "与原稿作者机器可能存在差异。")
    lines.append("6. **旋转/组合内文字**未纳入本次复检（4 条记录旋转均为 0°，且均为顶层对象）。\n")

    lines.append("## 九、人工分类（请填写，未预填）\n")
    lines.append("| # | 文件 / 页 / 对象 | 复检状态 | 真实缺陷 | 合理设计 | 不确定 | 备注 |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for c in real:
        lines.append(f"| {c['case_id']} | {c['file']} p{c['slide']} id={c['shape_id']} | "
                     f"`{c['second_pass']['status']}` | ☐ | ☐ | ☐ | |")
    lines.append("")

    lines.append("## 十、复现\n")
    lines.append("```bash")
    lines.append("# 依赖：python-pptx、Pillow、pywin32、本机 Microsoft PowerPoint")
    lines.append("pip install python-pptx pillow pywin32")
    lines.append("")
    lines.append("# 1) 重建对照样本（可选；仓库内已含成品）")
    lines.append("python test_samples/recheck/make_recheck_samples.py")
    lines.append("")
    lines.append("# 2) 运行复检（--refresh 忽略 COM 探针缓存）")
    lines.append("python outputs/recheck/text_visibility_recheck.py --refresh")
    lines.append("")
    lines.append("# 3) 运行验收断言")
    lines.append("python outputs/recheck/verify_recheck.py")
    lines.append("```")
    lines.append("")
    lines.append("产物：`text_recheck.json`（结构化结果）、`recheck_report.md`（本文件）、"
                 "`evidence/`（标注图与页边放大图）、`render/`（原始页面渲染）、`verification.json`（验收断言）。")
    lines.append("")
    return "\n".join(lines)


def _quote(text: str, n: int = 70) -> str:
    t = (text or "").replace("\r", " ").replace("\n", " ").replace("\xa0", " ").strip()
    return "“" + (t[:n] + "…" if len(t) > n else t) + "”" if t else "（空）"


def _fmt_box(box: dict[str, float] | None) -> str:
    if not box:
        return "—"
    return (f"({box['left_pt']:.1f}, {box['top_pt']:.1f})–"
            f"({box['right_pt']:.1f}, {box['bottom_pt']:.1f}) pt")


def _fmt_dirs(dirs: list[dict[str, Any]] | None) -> str:
    if not dirs:
        return ""
    return "、".join(f"{SIDE_CN.get(d['side'], d['side'])} {d['distance_pt']:.1f}pt"
                    f"（{d['distance_in']:.3f} in）" for d in dirs)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="PPT 文字可见性二次复检")
    ap.add_argument("--refresh", action="store_true", help="忽略 COM 探针缓存")
    ap.add_argument("--skip-render", action="store_true", help="不导出页面、不生成证据图")
    ap.add_argument("--debug-frames", action="store_true",
                    help="调试模式：即使空形状被判 not_applicable，也绘制其几何边界框")
    ap.add_argument("--out", default=str(HERE / "text_recheck.json"))
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = run(force=args.refresh, skip_render=args.skip_render,
                 debug_frames=args.debug_frames)
    out = Path(args.out)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    report = build_report(result)
    (HERE / "recheck_report.md").write_text(report, encoding="utf-8")

    s = result["summary"]
    print(f"[OK] cases={s['total']} real={s['real_cases']} samples={s['synthetic_samples']}")
    print(f"     real   : {s['real']}")
    print(f"     samples: {s['samples']}")
    for c in result["cases"]:
        print(f"  {c['case_id']} p{c['slide']:>2} id={c['shape_id']:<3} {c['second_pass']['status']:<16} "
              f"{c['name'][:28]}")
    cov = result["page_coverage"]["real_pages"]
    print(f"     coverage: 真实页含文字对象 {cov['text_objects_checked']}/{cov['text_objects_total']} 已复检，"
          f"{cov['text_objects_skipped']} 未复检")
    if result["probe_errors"]:
        print("[WARN] " + " | ".join(result["probe_errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
