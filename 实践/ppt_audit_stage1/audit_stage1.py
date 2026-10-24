"""第一阶段技术验证：PPTX 结构解析 + 基础越界检测。

用法：
    python audit_stage1.py --input <pptx目录> --out-dir outputs

设计约束：
  1. 只读打开源文件（python-pptx 不写回），不修改原始 PPT。
  2. 只做结构解析与确定性几何规则，不训练模型、不调用大模型。
  3. 所有结论必须能回溯到 幻灯片序号 + shape_id + 四边坐标 + 越界距离。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pptx
from pptx import Presentation

EMU_PER_INCH = 914400
EMU_PER_PT = 12700
EMU_PER_CM = 360000

# 默认纳入越界判定的内容型对象；图片为装饰/满版元素、线条几何语义不同、组为容器，默认排除
NON_TARGET_TYPES = {"PICTURE", "LINE", "GROUP"}
SKIP_FILE_PREFIX = "~$"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def emu_to_inch(value: int | None) -> float | None:
    return None if value is None else round(value / EMU_PER_INCH, 4)


def emu_to_pt(value: int | None) -> float | None:
    return None if value is None else round(value / EMU_PER_PT, 2)


def emu_to_cm(value: int | None) -> float | None:
    return None if value is None else round(value / EMU_PER_CM, 3)


def shape_type_name(shape: Any) -> str:
    """返回稳定的形状类型名，避免依赖枚举的 str() 输出格式。"""
    st = shape.shape_type
    if st is None:
        return "UNKNOWN"
    return getattr(st, "name", str(st).split(" ")[0])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 组形状坐标变换
# --------------------------------------------------------------------------- #
class Affine:
    """一维线性变换 x' = sx * x + tx（x、y 各自一套）。"""

    __slots__ = ("sx", "tx", "sy", "ty")

    def __init__(self, sx: float = 1.0, tx: float = 0.0, sy: float = 1.0, ty: float = 0.0):
        self.sx, self.tx, self.sy, self.ty = sx, tx, sy, ty

    def apply_x(self, x: float) -> float:
        return self.sx * x + self.tx

    def apply_y(self, y: float) -> float:
        return self.sy * y + self.ty

    def compose(self, child: "Affine") -> "Affine":
        """先做 child（组内坐标 → 父坐标），再做 self（父坐标 → 幻灯片坐标）。"""
        return Affine(
            sx=self.sx * child.sx,
            tx=self.sx * child.tx + self.tx,
            sy=self.sy * child.sy,
            ty=self.sy * child.ty + self.ty,
        )


def group_child_transform(shape: Any) -> Affine | None:
    """读取 a:xfrm 的 off/ext 与 chOff/chExt，构造组内坐标到幻灯片坐标的变换。

    组内子形状的 left/top 使用子坐标系（chOff/chExt），并不直接等于幻灯片坐标；
    不做这层换算会把组内元素全部误判为越界。
    """
    try:
        xfrm = shape._element.grpSpPr.xfrm
        if xfrm is None:
            return None
        off, ext = xfrm.off, xfrm.ext
        ch_off, ch_ext = xfrm.chOff, xfrm.chExt
        # 注意：lxml 元素不能用真值判断（无子节点的 <a:off> 会被判为 False）
        if off is None or ext is None or ch_off is None or ch_ext is None:
            return None
        if not ch_ext.cx or not ch_ext.cy:
            return None
        sx = ext.cx / ch_ext.cx
        sy = ext.cy / ch_ext.cy
        return Affine(sx=sx, tx=off.x - ch_off.x * sx, sy=sy, ty=off.y - ch_off.y * sy)
    except (AttributeError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# 形状解析
# --------------------------------------------------------------------------- #
def collect_text(shape: Any) -> str:
    """提取可见文字：文本框取 text_frame，表格取单元格，图表取标题。"""
    parts: list[str] = []
    try:
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            parts.append(shape.text_frame.text)
    except (AttributeError, ValueError):
        pass
    try:
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)
    except (AttributeError, ValueError):
        pass
    try:
        if getattr(shape, "has_chart", False) and shape.has_chart:
            if shape.chart.has_title and shape.chart.chart_title.text_frame.text:
                parts.append(shape.chart.chart_title.text_frame.text)
    except (AttributeError, ValueError):
        pass
    return "\n".join(p for p in parts if p).strip()


def collect_font_sizes(shape: Any) -> list[float]:
    """仅收集显式设置过的字号；主题继承字号无法从幻灯片 XML 直接得到。"""
    sizes: set[float] = set()
    try:
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        sizes.add(round(run.font.size.pt, 2))
    except (AttributeError, ValueError):
        pass
    return sorted(sizes)


def rotated_bounds(left: float, top: float, width: float, height: float, rotation: float):
    """把旋转矩形的外接轴对齐框换算出来，避免旋转元素被误判越界。"""
    if not rotation:
        return left, top, width, height
    import math

    rad = math.radians(rotation % 360)
    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
    new_w = width * cos_a + height * sin_a
    new_h = width * sin_a + height * cos_a
    cx, cy = left + width / 2, top + height / 2
    return cx - new_w / 2, cy - new_h / 2, new_w, new_h


def resolve_placeholder(shape: Any) -> dict[str, Any] | None:
    if not getattr(shape, "is_placeholder", False):
        return None
    info: dict[str, Any] = {}
    try:
        info["idx"] = shape.placeholder_format.idx
        ph_type = shape.placeholder_format.type
        info["type"] = getattr(ph_type, "name", str(ph_type))
    except (AttributeError, ValueError, KeyError):
        pass
    return info or None


def inherited_placeholder_frame(shape: Any, slide: Any) -> tuple[tuple[int, int, int, int], str] | None:
    """占位符常把位置继承自版式/母版，此时 shape.left 为 None；此处回溯取实际几何。"""
    if slide is None or not getattr(shape, "is_placeholder", False):
        return None
    try:
        idx = shape.placeholder_format.idx
    except (AttributeError, ValueError, KeyError):
        return None
    containers = []
    try:
        containers.append(("layout", slide.slide_layout))
    except (AttributeError, ValueError, KeyError):
        pass
    try:
        containers.append(("master", slide.slide_layout.slide_master))
    except (AttributeError, ValueError, KeyError):
        pass
    for source, container in containers:
        try:
            placeholders = container.placeholders
        except (AttributeError, ValueError):
            continue
        for ph in placeholders:
            try:
                if ph.placeholder_format.idx != idx:
                    continue
            except (AttributeError, ValueError):
                continue
            if None not in (ph.left, ph.top, ph.width, ph.height):
                return (ph.left, ph.top, ph.width, ph.height), source
    return None


def inspect_shape(
    shape: Any,
    slide_size: tuple[int, int],
    transform: Affine,
    tolerance_emu: int,
    parent_group_id: int | None,
    include_pictures: bool,
    depth: int = 0,
    slide: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """递归解析一个形状，返回 (形状记录列表, 越界记录列表)。"""
    records: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    stype = shape_type_name(shape)
    shape_id = shape.shape_id
    left, top, width, height = shape.left, shape.top, shape.width, shape.height
    geometry_source = "shape"
    if None in (left, top, width, height):
        inherited = inherited_placeholder_frame(shape, slide)
        if inherited is not None:
            (left, top, width, height), inherited_from = inherited
            geometry_source = f"inherited_{inherited_from}"
    has_frame = None not in (left, top, width, height)

    # ---- 组内坐标系 → 幻灯片坐标系 ----
    # 组内子形状的 left/top 是子坐标系（chOff/chExt），组自身的外框则已在父坐标系中
    local = transform
    if has_frame:
        child_tf = group_child_transform(shape) if stype == "GROUP" else None
        if child_tf is not None:
            local = transform.compose(child_tf)
        gx, gy = transform.apply_x(left), transform.apply_y(top)
        gw, gh = width * transform.sx, height * transform.sy
    else:
        gx = gy = gw = gh = None

    text = collect_text(shape)
    record: dict[str, Any] = {
        "shape_id": shape_id,
        "name": shape.name,
        "shape_type": stype,
        "is_placeholder": bool(getattr(shape, "is_placeholder", False)),
        "placeholder": resolve_placeholder(shape),
        "parent_group_id": parent_group_id,
        "depth": depth,
        "rotation_deg": round(float(getattr(shape, "rotation", 0.0) or 0.0), 2),
        "has_text": bool(text),
        "text": text,
        "explicit_font_sizes_pt": collect_font_sizes(shape),
        "geometry_source": geometry_source,
        "geometry": None,
        "bbox": None,
        "overflows": [],
    }

    if has_frame:
        record["geometry"] = {
            "left_emu": gx,
            "top_emu": gy,
            "width_emu": gw,
            "height_emu": gh,
            "right_emu": gx + gw,
            "bottom_emu": gy + gh,
            "left_in": emu_to_inch(gx),
            "top_in": emu_to_inch(gy),
            "width_in": emu_to_inch(gw),
            "height_in": emu_to_inch(gh),
            "right_in": emu_to_inch(gx + gw),
            "bottom_in": emu_to_inch(gy + gh),
        }
        rotation = float(getattr(shape, "rotation", 0.0) or 0.0)
        bx, by, bw, bh = rotated_bounds(gx, gy, gw, gh, rotation)
        record["bbox"] = {
            "left_in": emu_to_inch(bx),
            "top_in": emu_to_inch(by),
            "width_in": emu_to_inch(bw),
            "height_in": emu_to_inch(bh),
            "right_in": emu_to_inch(bx + bw),
            "bottom_in": emu_to_inch(by + bh),
            "rotation_applied": bool(rotation),
        }
        is_target = stype not in NON_TARGET_TYPES or (include_pictures and stype == "PICTURE")
        if is_target:
            found = detect_overflow(slide_size, bx, by, bw, bh, tolerance_emu)
            if found:
                detail = {
                    "shape_id": shape_id,
                    "name": shape.name,
                    "shape_type": stype,
                    "parent_group_id": parent_group_id,
                    "directions": found["directions"],
                    "max_overflow_pt": found["max_overflow_pt"],
                    "max_overflow_in": found["max_overflow_in"],
                    "severity": found["severity"],
                    "geometry_in": record["geometry"],
                    "bbox_in": record.get("bbox"),
                }
                record["overflows"] = [
                    {"side": d["side"], "distance_pt": d["distance_pt"], "distance_in": d["distance_in"]}
                    for d in found["directions"]
                ]
                violations.append(detail)

    records.append(record)

    # ---- 组内递归 ----
    if stype == "GROUP" and depth < 5:
        try:
            for child in shape.shapes:
                child_records, child_violations = inspect_shape(
                    child,
                    slide_size,
                    local,
                    tolerance_emu,
                    parent_group_id=shape_id,
                    include_pictures=include_pictures,
                    depth=depth + 1,
                    slide=slide,
                )
                records.extend(child_records)
                violations.extend(child_violations)
        except (AttributeError, ValueError):
            record["group_error"] = "无法读取组成员"

    return records, violations


def detect_overflow(
    slide_size: tuple[int, int],
    left: float,
    top: float,
    width: float,
    height: float,
    tolerance_emu: int,
) -> dict[str, Any] | None:
    """四边比较，返回越界方向与距离（EMU / pt / inch）。"""
    slide_w, slide_h = slide_size
    right, bottom = left + width, top + height
    dirs: list[dict[str, Any]] = []

    checks = [
        ("left", -left, left < -tolerance_emu),
        ("top", -top, top < -tolerance_emu),
        ("right", right - slide_w, right > slide_w + tolerance_emu),
        ("bottom", bottom - slide_h, bottom > slide_h + tolerance_emu),
    ]
    for side, distance_emu, hit in checks:
        if not hit:
            continue
        dirs.append(
            {
                "side": side,
                "distance_emu": int(round(distance_emu)),
                "distance_pt": emu_to_pt(int(round(distance_emu))),
                "distance_in": emu_to_inch(int(round(distance_emu))),
            }
        )
    if not dirs:
        return None

    max_emu = max(d["distance_emu"] for d in dirs)
    max_in = emu_to_inch(max_emu)
    if max_in <= 0.1:
        severity = "low"
    elif max_in <= 0.5:
        severity = "medium"
    else:
        severity = "high"
    return {
        "directions": dirs,
        "max_overflow_pt": emu_to_pt(max_emu),
        "max_overflow_in": max_in,
        "severity": severity,
    }


# --------------------------------------------------------------------------- #
# 单份 PPT 解析
# --------------------------------------------------------------------------- #
def audit_deck(
    path: Path,
    tolerance_in: float,
    include_pictures: bool,
) -> dict[str, Any]:
    tolerance_emu = int(round(tolerance_in * EMU_PER_INCH))
    deck: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_of(path),
        "parse_ok": False,
        "error": None,
        "slide_width_emu": None,
        "slide_height_emu": None,
        "slide_width_in": None,
        "slide_height_in": None,
        "slide_count": 0,
        "shape_count": 0,
        "text_shape_count": 0,
        "violation_count": 0,
        "slides": [],
    }
    try:
        prs = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 - 单份文件失败不应中断批处理
        deck["error"] = f"{type(exc).__name__}: {exc}"
        return deck

    slide_w, slide_h = prs.slide_width, prs.slide_height
    deck.update(
        {
            "parse_ok": True,
            "slide_width_emu": slide_w,
            "slide_height_emu": slide_h,
            "slide_width_in": emu_to_inch(slide_w),
            "slide_height_in": emu_to_inch(slide_h),
            "slide_count": len(prs.slides),
        }
    )

    for idx, slide in enumerate(prs.slides, start=1):
        shapes: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        for shape in slide.shapes:
            recs, vios = inspect_shape(
                shape,
                (slide_w, slide_h),
                Affine(),
                tolerance_emu,
                parent_group_id=None,
                include_pictures=include_pictures,
                slide=slide,
            )
            shapes.extend(recs)
            violations.extend(vios)

        try:
            layout_name = slide.slide_layout.name
        except (AttributeError, ValueError, KeyError):
            layout_name = None

        deck["slides"].append(
            {
                "index": idx,
                "layout": layout_name,
                "shape_count": len(shapes),
                "violation_count": len(violations),
                "shapes": shapes,
                "violations": violations,
            }
        )
        deck["shape_count"] += len(shapes)
        deck["text_shape_count"] += sum(1 for s in shapes if s["has_text"])
        deck["violation_count"] += len(violations)

    return deck


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def iter_decks(input_dir: Path) -> Iterable[Path]:
    for path in sorted(input_dir.glob("*.pptx")):
        if path.name.startswith(SKIP_FILE_PREFIX):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="PPTX 解析与基础越界检测（第一阶段）")
    parser.add_argument("--input", required=True, help="PPTX 目录")
    parser.add_argument("--out-dir", default="outputs", help="输出目录")
    parser.add_argument("--tolerance-in", type=float, default=0.02, help="边界容差（英寸），默认 0.02")
    parser.add_argument("--include-pictures", action="store_true", help="把图片也纳入越界判定")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    decks = [audit_deck(p, args.tolerance_in, args.include_pictures) for p in iter_decks(input_dir)]

    total_slides = sum(d["slide_count"] for d in decks)
    total_shapes = sum(d["shape_count"] for d in decks)
    total_violations = sum(d["violation_count"] for d in decks)
    source_counts: dict[str, int] = {}
    for d in decks:
        for s in d["slides"]:
            for sh in s["shapes"]:
                key = sh["geometry_source"] if sh["geometry"] else "unresolved"
                source_counts[key] = source_counts.get(key, 0) + 1
    flat_violations = [
        {"file": d["file"], "slide": s["index"], **v}
        for d in decks
        for s in d["slides"]
        for v in s["violations"]
    ]

    structure = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tool": {
            "python": platform.python_version(),
            "python_pptx": pptx.__version__,
            "platform": platform.platform(),
        },
        "config": {
            "input_dir": str(input_dir),
            "tolerance_in": args.tolerance_in,
            "include_pictures": args.include_pictures,
            "target_shape_types": "全部内容型对象（默认排除 PICTURE / LINE / GROUP，图片可用 --include-pictures 纳入）",
        },
        "summary": {
            "deck_count": len(decks),
            "parsed_deck_count": sum(1 for d in decks if d["parse_ok"]),
            "failed_deck_count": sum(1 for d in decks if not d["parse_ok"]),
            "slide_count": total_slides,
            "shape_count": total_shapes,
            "text_shape_count": sum(d["text_shape_count"] for d in decks),
            "violation_count": total_violations,
            "decks_with_violations": sum(1 for d in decks if d["violation_count"]),
            "geometry_source_counts": source_counts,
        },
        "decks": decks,
    }
    (out_dir / "stage1_structure.json").write_text(
        json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    violations_doc = {
        "generated_at": structure["generated_at"],
        "tolerance_in": args.tolerance_in,
        "summary": {
            "deck_count": len(decks),
            "slide_count": total_slides,
            "violation_count": total_violations,
            "by_side": {
                side: sum(1 for v in flat_violations for d in v["directions"] if d["side"] == side)
                for side in ("left", "right", "top", "bottom")
            },
            "by_severity": {
                level: sum(1 for v in flat_violations if v["severity"] == level)
                for level in ("low", "medium", "high")
            },
            "by_type": {},
        },
        "violations": flat_violations,
    }
    for v in flat_violations:
        by_type = violations_doc["summary"]["by_type"]
        by_type[v["shape_type"]] = by_type.get(v["shape_type"], 0) + 1
    (out_dir / "stage1_violations.json").write_text(
        json.dumps(violations_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[OK] 读取 {len(decks)} 份 / {total_slides} 页 / {total_shapes} 个对象")
    print(f"[OK] 越界记录 {total_violations} 条 -> {out_dir / 'stage1_violations.json'}")
    for d in decks:
        flag = "OK " if d["parse_ok"] else "ERR"
        print(
            f"  {flag} {d['file']}: {d['slide_count']} 页, {d['shape_count']} 对象"
            f"（含文字 {d['text_shape_count']}）, 越界 {d['violation_count']}"
            + (f" | {d['error']}" if d["error"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
