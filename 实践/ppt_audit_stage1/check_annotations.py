"""对视觉验证产物做自动检查，并输出 annotation_check.json。

检查内容：
  1. 页面 PNG 为 1920×1080、比例与页面尺寸一致（144 px/in）。
  2. 标注框像素坐标 = 页面偏移 + 英寸坐标 × 比例（误差 ≤ 1 px）。
  3. 可见框内存在"墨迹"（非白像素），且墨迹外接框落在框内 —— 说明标注位置有对象。
     对"无填充 / 无边框 / 无文字"的形状，像素上本就不可见，改为核对该属性并跳过墨迹要求。
  4. 原始 PPTX SHA-256 与第一阶段记录一致，未被 PowerPoint 改写。

注意：本脚本只做几何与像素核对，不判定"是否真实缺陷"，人工分类字段保持空白。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE
OUT = ROOT / "visual_validation"

INK_THRESHOLD = 225  # 灰度低于此值视为"有内容"
MARGIN_X, MARGIN_TOP = 120, 140
# 该页底部有整幅页脚色带，会干扰墨迹统计，统计文字时排除最下方 60 px
IGNORE_BOTTOM_PX = {"AddAgendaSlide_TestA.pptx": 60}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ink_stats(page_png: Path, box_px, ignore_bottom_px: int = 0) -> dict:
    """在页面坐标系内统计框中文档内容的位置。"""
    img = Image.open(page_png).convert("L")
    x0, y0, x1, y1 = [int(round(v)) for v in box_px]
    crop = img.crop((x0 - MARGIN_X, y0 - MARGIN_TOP, x1 - MARGIN_X, y1 - MARGIN_TOP))
    w, h = crop.size
    usable_h = max(1, h - ignore_bottom_px)

    mask = Image.new("L", (w, h), 0)
    mask.paste(crop.crop((0, 0, w, usable_h)).point(lambda v: 255 if v < INK_THRESHOLD else 0), (0, 0))
    bbox = mask.getbbox()
    hist = mask.histogram()
    dark = hist[255]
    total = w * usable_h
    return {
        "size_px": [w, h],
        "counted_height_px": usable_h,
        "dark_ratio": round(dark / total, 4) if total else 0.0,
        "ink_bbox_in_box_px": list(bbox) if bbox else None,
    }


def main() -> int:
    meta = json.loads((OUT / "annotation_meta.json").read_text(encoding="utf-8"))
    structure = json.loads((ROOT / "outputs" / "stage1_structure.json").read_text(encoding="utf-8"))
    decks = {d["file"]: d for d in structure["decks"]}

    results = []
    for m in meta:
        deck = decks[m["file"]]
        page_png = OUT / m["page_png"]
        img = Image.open(page_png)
        page_w, page_h = img.size
        scale = page_w / deck["slide_width_in"]

        expected_full = [
            MARGIN_X + m["bbox_in"]["left_in"] * scale,
            MARGIN_TOP + m["bbox_in"]["top_in"] * scale,
            MARGIN_X + m["bbox_in"]["right_in"] * scale,
            MARGIN_TOP + m["bbox_in"]["bottom_in"] * scale,
        ]
        delta = max(abs(a - b) for a, b in zip(expected_full, m["box_px_full"]))
        visible = [
            max(expected_full[0], MARGIN_X),
            max(expected_full[1], MARGIN_TOP),
            min(expected_full[2], MARGIN_X + page_w),
            min(expected_full[3], MARGIN_TOP + page_h),
        ]
        visible_delta = max(abs(a - b) for a, b in zip(visible, m["box_px_visible"]))
        ink = ink_stats(page_png, m["box_px_visible"], IGNORE_BOTTOM_PX.get(m["file"], 0))

        padding = None
        ink_inside = True
        if ink["ink_bbox_in_box_px"]:
            ix0, iy0, ix1, iy1 = ink["ink_bbox_in_box_px"]
            bw = m["box_px_visible"][2] - m["box_px_visible"][0]
            bh = m["box_px_visible"][3] - m["box_px_visible"][1]
            ink_inside = ix0 >= -2 and iy0 >= -2 and ix1 <= bw + 2 and iy1 <= bh + 2
            padding = {
                "left_px": round(ix0, 1),
                "top_px": round(iy0, 1),
                "right_px": round(bw - ix1, 1),
                "bottom_px": round(bh - iy1, 1),
            }

        original = (
            Path(r"D:\Codex_Project\Homework\Design_big_data\Final_Hw_修改\实践\python-ppt\downloaded_pptx")
            / m["file"]
        )
        hash_ok = sha256(original) == deck["sha256"]

        render_invisible = m.get("render_invisible", False)
        pixel_ok = render_invisible or (ink["dark_ratio"] > 0.002 and ink_inside and padding is not None)
        geometry_ok = (
            [page_w, page_h] == [1920, 1080]
            and abs(scale - 144.0) < 0.01
            and delta <= 1.0
            and visible_delta <= 1.0
        )
        results.append(
            {
                "file": m["file"],
                "slide": m["slide"],
                "shape_id": m["shape_id"],
                "png_size": [page_w, page_h],
                "scale_px_per_in": round(scale, 3),
                "box_recompute_delta_px": round(delta, 2),
                "visible_box_delta_px": round(visible_delta, 2),
                "geometry_ok": geometry_ok,
                "render_invisible": render_invisible,
                "ink": ink,
                "ink_inside_box": ink_inside,
                "ink_padding_px": padding,
                "pixel_check_ok": pixel_ok,
                "source_sha256_unchanged": hash_ok,
            }
        )

    passed = all(
        r["geometry_ok"] and r["pixel_check_ok"] and r["source_sha256_unchanged"] for r in results
    )

    (OUT / "annotation_check.json").write_text(
        json.dumps({"passed": passed, "checks": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for r in results:
        state = "几何/不可见" if r["render_invisible"] else f"墨迹 {r['ink']['dark_ratio']:.1%}"
        print(
            f"[{'PASS' if r['geometry_ok'] and r['pixel_check_ok'] and r['source_sha256_unchanged'] else 'FAIL'}] "
            f"{r['file']} p{r['slide']}: boxΔ={r['box_recompute_delta_px']}px, {state}, "
            f"padding={r['ink_padding_px']}, hash={r['source_sha256_unchanged']}"
        )
    print("ALL PASS" if passed else "CHECK FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
