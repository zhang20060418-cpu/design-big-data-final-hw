"""PPTX validation, parsing and structural inventory.

Read-only: python-pptx is only ever used to open the uploaded copy.  The
original upload is stored with its SHA-256 and is never rewritten.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Emu

EMU_PER_INCH = 914400
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


class InvalidPptxError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_package(path: Path, max_bytes: int, max_slides: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size == 0:
        raise InvalidPptxError("empty_file", "文件为空。")
    if size > max_bytes:
        raise InvalidPptxError("file_too_large", f"文件超过 {max_bytes // (1024*1024)} MB 限制。")
    if not zipfile.is_zipfile(path):
        raise InvalidPptxError("not_zip", "文件不是有效的 PPTX（ZIP 容器校验失败）。")
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            missing = required - names
            if missing:
                raise InvalidPptxError("not_pptx", "缺少 PowerPoint 必需的 OpenXML 部件。")
            if "ppt/vbaProject.bin" in names:
                raise InvalidPptxError("macro_present", "文件包含宏工程（.pptm 内容），出于安全原因不支持。")
            total = sum(info.file_size for info in zf.infolist())
            compressed = max(1, sum(info.compress_size for info in zf.infolist()))
            if total > 800 * 1024 * 1024 or total / compressed > 300:
                raise InvalidPptxError("suspicious_archive", "压缩包解压比异常，已拒绝处理。")
    except zipfile.BadZipFile as exc:
        raise InvalidPptxError("corrupt_zip", f"压缩容器损坏：{exc}") from exc

    try:
        prs = Presentation(str(path))
        count = len(prs.slides)
    except Exception as exc:  # python-pptx raises a variety of errors
        raise InvalidPptxError("unreadable_pptx", f"无法解析演示文稿：{exc}") from exc
    if count == 0:
        raise InvalidPptxError("no_slides", "演示文稿不包含任何幻灯片。")
    if count > max_slides:
        raise InvalidPptxError("too_many_slides", f"页数 {count} 超过 {max_slides} 页限制。")
    return {"size_bytes": size, "slide_count": count}


# --------------------------------------------------------------------------- fonts
def _installed_fonts() -> set[str]:
    import winreg

    result: set[str] = set()

    def add(root: int, sub: str) -> None:
        try:
            with winreg.OpenKey(root, sub) as key:
                index = 0
                while True:
                    try:
                        value, _, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    label = re.sub(r"\s*\((TrueType|OpenType|Type 1)\)\s*$", "", value, flags=re.I)
                    for part in label.split("&"):
                        part = part.strip().lower()
                        if part:
                            result.add(part)
                            result.add(re.sub(r"\s+(bold|italic|light|semibold|semilight|black|regular)$", "", part))
        except OSError:
            return

    add(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
    add(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
    result.update({
        "宋体", "simsun", "微软雅黑", "microsoft yahei", "等线", "dengxian",
        "新細明體", "pmingliu", "malgun gothic", "맑은 고딕",
    })
    return {x for x in result if x}


FONT_ALIASES = {
    "宋体": "simsun",
    "微软雅黑": "microsoft yahei",
    "等线": "dengxian",
    "新細明體": "pmingliu",
    "新细明体": "pmingliu",
    "맑은 고딕": "malgun gothic",
    "ＭＳ Ｐゴシック": "ms pgothic",
    "游ゴシック": "yu gothic",
}


def _font_installed(name: str, installed: set[str]) -> bool:
    key = FONT_ALIASES.get(name, name).strip().lower()
    if key in installed:
        return True
    stripped = re.sub(r"\s+(bold|italic|light|semibold|semilight|black|regular)$", "", key)
    return stripped in installed


def _theme_fonts(path: Path) -> list[str]:
    fonts: set[str] = set()
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if re.match(r"ppt/theme/theme\d+\.xml$", name):
                    root = etree.fromstring(zf.read(name))
                    for node in root.iter("{%s}latin" % NS["a"]):
                        face = node.get("typeface")
                        if face:
                            fonts.add(face)
                    for node in root.iter("{%s}ea" % NS["a"]):
                        face = node.get("typeface")
                        if face:
                            fonts.add(face)
    except Exception:
        pass
    return sorted(fonts)


# --------------------------------------------------------------------------- geometry
def _emu_in(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / EMU_PER_INCH, 3)


@dataclass
class Leaf:
    shape: Any
    left: float
    top: float
    width: float
    height: float
    z: int
    group_path: tuple[str, ...]
    scale_x: float
    scale_y: float


def _iter_leaves(shapes, ox=0.0, oy=0.0, sx=1.0, sy=1.0, path: tuple[str, ...] = ()) -> Iterator[Leaf]:
    for z, shape in enumerate(shapes):
        left = shape.left if shape.left is not None else 0
        top = shape.top if shape.top is not None else 0
        width = shape.width if shape.width is not None else 0
        height = shape.height if shape.height is not None else 0
        abs_left = ox + left * sx
        abs_top = oy + top * sy
        abs_w = width * sx
        abs_h = height * sy
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            # children use the group's child coordinate space
            ch_off_x = ch_off_y = 0
            ch_ext_x = ch_ext_y = 1
            try:
                xfrm = shape._element.grpSpPr.xfrm
                ch_off_x = int(xfrm.chOff.x)
                ch_off_y = int(xfrm.chOff.y)
                ch_ext_x = max(1, int(xfrm.chExt.cx))
                ch_ext_y = max(1, int(xfrm.chExt.cy))
            except Exception:
                pass
            inner_sx = (width / ch_ext_x) * sx
            inner_sy = (height / ch_ext_y) * sy
            yield from _iter_leaves(
                shape.shapes,
                abs_left - ch_off_x * inner_sx,
                abs_top - ch_off_y * inner_sy,
                inner_sx,
                inner_sy,
                path + (shape.name,),
            )
            continue
        if width <= 0 or height <= 0:
            continue
        yield Leaf(shape, _emu_in(abs_left) or 0.0, _emu_in(abs_top) or 0.0,
                   _emu_in(abs_w) or 0.0, _emu_in(abs_h) or 0.0, z, path, sx, sy)


def _shape_text(shape) -> str:
    try:
        if shape.has_text_frame:
            return shape.text_frame.text
    except Exception:
        return ""
    return ""


def _run_sizes(shape) -> list[float]:
    sizes: list[float] = []
    try:
        if not shape.has_text_frame:
            return sizes
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.size is not None and run.text.strip():
                    sizes.append(round(run.font.size.pt, 1))
    except Exception:
        pass
    return sizes


def _is_placeholder_footer(shape, slide_h: float, top: float, height: float) -> bool:
    name = (getattr(shape, "name", "") or "").lower()
    if "footer" in name or "slide number" in name or "页码" in name:
        return True
    return top + height > slide_h * 0.93 and (top > slide_h * 0.9)


def _is_opaque(shape) -> bool:
    """True when the shape likely paints a solid area over what is behind it."""
    try:
        if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            return True
    except Exception:
        pass
    try:
        fill = shape.fill
        ftype = fill.type
        if ftype is None:
            return False
        # MSO_FILL.SOLID = 1, MSO_FILL.PICTURE = 6, MSO_FILL.PATTERNED = 5, GRADIENT = 4
        return int(ftype) in (1, 4, 5, 6)
    except Exception:
        return False



def parse_document(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(document_meta, slides_detail)``."""
    prs = Presentation(str(path))
    slide_w = prs.slide_width / EMU_PER_INCH
    slide_h = prs.slide_height / EMU_PER_INCH
    ratio = round(slide_w / slide_h, 4)
    if abs(ratio - 16 / 9) < 0.02:
        aspect = "16:9"
    elif abs(ratio - 4 / 3) < 0.02:
        aspect = "4:3"
    elif abs(ratio - 3 / 2) < 0.02:
        aspect = "3:2"
    else:
        aspect = f"{ratio:.3f}:1"

    installed = _installed_fonts()
    fonts: set[str] = set()
    slides_detail: list[dict[str, Any]] = []
    total_textboxes = total_pictures = total_tables = total_charts = 0
    total_chars = 0
    modes: Counter[str] = Counter()

    for index, slide in enumerate(prs.slides, 1):
        leaves = list(_iter_leaves(slide.shapes))
        text_shapes = []
        pictures = []
        tables = charts = 0
        chars = 0
        blocks: list[dict[str, Any]] = []
        elements: list[dict[str, Any]] = []

        for leaf in leaves:
            shape = leaf.shape
            is_pic = shape.shape_type == MSO_SHAPE_TYPE.PICTURE
            has_tbl = bool(getattr(shape, "has_table", False))
            has_cht = bool(getattr(shape, "has_chart", False))
            text = _shape_text(shape).strip()
            if is_pic:
                pictures.append(leaf)
            if has_tbl:
                tables += 1
            if has_cht:
                charts += 1
            kind = "picture" if is_pic else (
                "table" if has_tbl else ("chart" if has_cht else ("text" if text else "shape"))
            )
            elements.append({
                "i": len(elements),
                "name": shape.name,
                "kind": kind,
                "bbox": {"x": leaf.left, "y": leaf.top, "w": leaf.width, "h": leaf.height},
                "chars": len(re.sub(r"\s+", "", text)),
                "opaque": is_pic or _is_opaque(shape),
                "group": "/".join(leaf.group_path),
            })
            if not text:
                continue
            text_shapes.append(shape)
            count = len(re.sub(r"\s+", "", text))
            chars += count
            sizes = _run_sizes(shape)
            blocks.append({
                "element_id": f"s{index:04d}-e{len(blocks)+1:03d}",
                "element_i": len(elements) - 1,
                "name": shape.name,
                "shape_type": str(shape.shape_type),
                "bbox": {"x": leaf.left, "y": leaf.top, "w": leaf.width, "h": leaf.height},
                "text": text[:400],
                "chars": count,
                "sizes": sorted(set(sizes)),
                "word_wrap": bool(getattr(shape.text_frame, "word_wrap", True)),
                "auto_size": str(getattr(shape.text_frame, "auto_size", "") or ""),
                "placeholder": bool(getattr(shape, "is_placeholder", False)),
                "group": "/".join(leaf.group_path),
            })
            for run in _iter_runs(shape):
                if run.font.name:
                    fonts.add(run.font.name.strip())

        full_slide_pic = False
        for leaf in pictures:
            area_ratio = (leaf.width * leaf.height) / max(1e-6, slide_w * slide_h)
            covers = (
                area_ratio >= 0.88
                and leaf.left <= slide_w * 0.06
                and leaf.top <= slide_h * 0.06
                and leaf.left + leaf.width >= slide_w * 0.94
                and leaf.top + leaf.height >= slide_h * 0.94
            )
            if covers:
                full_slide_pic = True
                break

        if full_slide_pic and chars < 100:
            content_mode = "image_only"
        elif pictures:
            content_mode = "mixed"
        else:
            content_mode = "editable"

        modes[content_mode] += 1
        total_textboxes += len(text_shapes)
        total_pictures += len(pictures)
        total_tables += tables
        total_charts += charts
        total_chars += chars

        slides_detail.append({
            "index": index,
            "text_shapes": len(text_shapes),
            "text_chars": chars,
            "pictures": len(pictures),
            "tables": tables,
            "charts": charts,
            "full_slide_picture": full_slide_pic,
            "content_mode": content_mode,
            "blocks": blocks,
            "elements": elements,
            "notes_chars": len(slide.notes_slide.notes_text_frame.text) if slide.has_notes_slide else 0,
        })

    fonts.update(_theme_fonts(path))
    missing = sorted(f for f in fonts if f and not _font_installed(f, installed))

    document = {
        "file_name": path.name,
        "slide_count": len(prs.slides),
        "slide_width_in": round(slide_w, 3),
        "slide_height_in": round(slide_h, 3),
        "aspect_ratio": ratio,
        "aspect_label": aspect,
        "textboxes": total_textboxes,
        "text_chars": total_chars,
        "pictures": total_pictures,
        "tables": total_tables,
        "charts": total_charts,
        "editable_pages": modes.get("editable", 0),
        "mixed_pages": modes.get("mixed", 0),
        "image_only_pages": modes.get("image_only", 0),
        "fonts_used": sorted(fonts),
        "missing_fonts": missing,
        "slides": [
            {k: v for k, v in slide.items() if k not in ("blocks", "elements")} for slide in slides_detail
        ],
    }
    detail = {"slide_width_in": slide_w, "slide_height_in": slide_h, "slides": slides_detail}
    return document, detail


def _iter_runs(shape) -> Iterator[Any]:
    try:
        if not shape.has_text_frame:
            return
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                yield run
    except Exception:
        return
