"""在 PowerPoint 渲染图上标注 stage1 检出的越界对象。

输入：visual_validation/pages/*.png（1920×1080，144 px/in）
      outputs/stage1_violations.json / stage1_structure.json
输出：visual_validation/annotated/*_annotated.png + annotation_meta.json

画布策略：页面图像四周留白，页面外被裁切的部分用虚线框 + 斜纹画在白边里，
这样"对象超出页面但渲染被裁切"能被直接看见，而不是被误认为对象不存在。
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from lxml import etree
from pptx import Presentation

PPTX_DIR = Path(r"D:\Codex_Project\Homework\Design_big_data\Final_Hw_修改\实践\python-ppt\downloaded_pptx")

HERE = Path(__file__).resolve().parent
ROOT = HERE
OUT = ROOT / "visual_validation"
PAGES = OUT / "pages"
ANNOTATED = OUT / "annotated"

MARGIN_X = 120
MARGIN_TOP = 140
MARGIN_BOTTOM = 120
RED = (214, 48, 49)
RED_DARK = (150, 24, 28)
GREEN = (34, 160, 107)
INK = (32, 36, 44)
GREY = (110, 118, 130)

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

TARGETS = [
    ("AddAgendaSlide_TestA.pptx", 3),
    ("CalibriToTimesTestA.pptx", 14),
    ("CenterAlignTitles_TestA.pptx", 4),
    ("CenterAlignTitles_TestA.pptx", 5),
]

SIDE_CN = {"left": "左侧", "right": "右侧", "top": "上方", "bottom": "下方"}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)


def dashed_rect(draw: ImageDraw.ImageDraw, box, color=RED, width: int = 3, dash: int = 10) -> None:
    x0, y0, x1, y1 = box
    for x in range(int(x0), int(x1), dash * 2):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(int(y0), int(y1), dash * 2):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


def hatch_region(canvas: Image.Image, box, color=(214, 48, 49, 110), step: int = 9) -> None:
    """在矩形内画 45° 斜线（用带蒙版的图层，保证不越界）。"""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    for offset in range(-h, w + h, step):
        layer_draw.line([(offset, h), (offset + h, 0)], fill=color, width=1)
    canvas.paste(layer, (x0, y0), layer)


def label(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fg=INK, bg=(255, 255, 255, 242), pad=7):
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=fnt)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rectangle([x, y, x + w + pad * 2, y + h + pad * 2], fill=bg, outline=(214, 218, 224))
    draw.text((x + pad, y + pad - bbox[1]), text, font=fnt, fill=fg)
    return w + pad * 2


def is_render_invisible(shape, text: str) -> bool:
    """无填充、无边框、无文字的形状，渲染图中不可见（典型为编辑遗留的空形状）。"""
    if text.strip():
        return False
    try:
        sp_pr = shape._element.spPr
    except AttributeError:
        return False
    tags = {etree.QName(child).localname for child in sp_pr}
    return "noFill" in tags and "ln" not in tags


def find_shape(deck_name: str, slide_no: int, shape_id: int):
    """从原始 PPTX 中取回形状对象（用于判断渲染可见性）。"""
    prs = Presentation(str(PPTX_DIR / deck_name))
    slide = list(prs.slides)[slide_no - 1]

    def walk(shapes):
        for sh in shapes:
            if sh.shape_id == shape_id:
                return sh
            if sh.shape_type is not None and str(sh.shape_type).startswith("GROUP"):
                found = walk(sh.shapes)
                if found is not None:
                    return found
        return None

    return walk(slide.shapes)


def main() -> int:
    structure = json.loads((ROOT / "outputs" / "stage1_structure.json").read_text(encoding="utf-8"))
    decks = {d["file"]: d for d in structure["decks"]}

    meta = []
    for deck_name, slide_no in TARGETS:
        deck = decks[deck_name]
        slide = deck["slides"][slide_no - 1]
        rec = slide["violations"][0]
        shape = next(s for s in slide["shapes"] if s["shape_id"] == rec["shape_id"])

        png = PAGES / f"{Path(deck_name).stem}_p{slide_no}.png"
        img = Image.open(png).convert("RGB")
        page_w, page_h = img.size
        slide_w_in, slide_h_in = deck["slide_width_in"], deck["slide_height_in"]
        scale_x, scale_y = page_w / slide_w_in, page_h / slide_h_in

        canvas_w = page_w + 2 * MARGIN_X
        canvas_h = page_h + MARGIN_TOP + MARGIN_BOTTOM
        canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
        canvas.paste(img, (MARGIN_X, MARGIN_TOP))
        draw = ImageDraw.Draw(canvas, "RGBA")

        left_px, top_px = MARGIN_X, MARGIN_TOP
        right_px, bottom_px = MARGIN_X + page_w, MARGIN_TOP + page_h

        # 页面边界
        draw.rectangle([left_px, top_px, right_px, bottom_px], outline=GREEN, width=3)

        bbox = shape["bbox"]
        full = [
            left_px + bbox["left_in"] * scale_x,
            top_px + bbox["top_in"] * scale_y,
            left_px + bbox["right_in"] * scale_x,
            top_px + bbox["bottom_in"] * scale_y,
        ]
        visible = [
            max(full[0], left_px),
            max(full[1], top_px),
            min(full[2], right_px),
            min(full[3], bottom_px),
        ]
        clipped = any(abs(a - b) > 0.5 for a, b in zip(full, visible))

        if clipped:
            # 页面外部分：淡红底 + 斜纹 + 虚线框，画在白边里
            outside_parts = [
                [full[0], full[1], visible[0], visible[3]],  # 左
                [visible[2], full[1], full[2], visible[3]],  # 右
                [full[0], full[1], visible[2], visible[1]],  # 上
                [full[0], visible[3], visible[2], full[3]],  # 下
            ]
            for part in outside_parts:
                if part[2] - part[0] > 0 and part[3] - part[1] > 0:
                    draw.rectangle(part, fill=(214, 48, 49, 36))
                    hatch_region(canvas, part, color=(214, 48, 49, 105), step=9)
            dashed_rect(draw, full, color=RED, width=3, dash=10)

        # 页内可见部分：实线红框
        draw.rectangle(visible, outline=RED, width=6)

        # 页眉
        f_title = font(30, bold=True)
        f_sub = font(20)
        draw.text((left_px, 24), f"{deck_name} · 第 {slide_no} 页", font=f_title, fill=INK)
        dirs_text = "、".join(
            f"{SIDE_CN[d['side']]} {d['distance_in']:.3f} in" for d in rec["directions"]
        )
        sub = (
            f"对象 {rec['shape_id']}（{rec['name']}，{rec['shape_type']}） · "
            f"外框 {bbox['width_in']:.2f} × {bbox['height_in']:.2f} in · 越界：{dirs_text}"
        )
        draw.text((left_px, 64), sub, font=f_sub, fill=GREY)

        # 图例（右上）
        f_leg = font(18)
        lx = canvas_w - MARGIN_X - 330
        draw.line([(lx, 30), (lx + 40, 30)], fill=RED, width=5)
        draw.text((lx + 48, 20), "对象外框（页内可见）", font=f_leg, fill=INK)
        dashed_rect(draw, [lx, 60, lx + 40, 74], width=2, dash=6)
        draw.text((lx + 48, 52), "页面外部分（渲染已裁切）", font=f_leg, fill=INK)
        draw.rectangle([lx, 94, lx + 24, 110], outline=GREEN, width=2)
        draw.text((lx + 32, 86), "页面边界", font=f_leg, fill=INK)

        # 越界说明标签：优先放在页面外下方，其次放在框内下方/上方
        f_note = font(20)
        invisible = False
        try:
            px_shape = find_shape(deck_name, slide_no, rec["shape_id"])
            px_text = px_shape.text_frame.text if getattr(px_shape, "has_text_frame", False) else ""
            invisible = is_render_invisible(px_shape, px_text)
        except Exception:  # noqa: BLE001 - 可见性判断失败不应阻断标注
            invisible = False
        note = f"超出页面：{dirs_text}" + ("（灰色斜纹区为被裁切部分）" if clipped else "")
        if invisible:
            note += " · 对象无填充/无边框，渲染不可见"
        sides = {d["side"] for d in rec["directions"]}
        if "bottom" in sides:
            note_w = draw.textlength(note, font=f_note)
            x = min(max(left_px, visible[0] + 6), right_px - note_w - 30)
            label(draw, (x, bottom_px + 12), note, f_note, fg=RED_DARK)
        elif "top" in sides:
            note_w = draw.textlength(note, font=f_note)
            x = min(max(left_px, visible[0] + 6), right_px - note_w - 30)
            label(draw, (x, visible[3] + 10), note, f_note, fg=RED_DARK)
        elif "right" in sides:
            label(draw, (right_px - 560, visible[3] + 10), note, f_note, fg=RED_DARK)
        else:
            label(draw, (visible[2] + 10, max(top_px, visible[1])), note, f_note, fg=RED_DARK)

        out = ANNOTATED / f"{Path(deck_name).stem}_p{slide_no}_annotated.png"
        canvas.save(out)

        meta.append(
            {
                "file": deck_name,
                "slide": slide_no,
                "shape_id": rec["shape_id"],
                "shape_name": rec["name"],
                "shape_type": rec["shape_type"],
                "directions": rec["directions"],
                "bbox_in": bbox,
                "page_png": f"pages/{png.name}",
                "annotated_png": f"annotated/{out.name}",
                "scale_px_per_in": round(scale_x, 4),
                "box_px_full": [round(v, 1) for v in full],
                "box_px_visible": [round(v, 1) for v in visible],
                "clipped": clipped,
                "render_invisible": invisible,
                "page_size_px": [page_w, page_h],
                "canvas_size_px": [canvas_w, canvas_h],
                "text_in_object": shape["text"][:300],
            }
        )
        print(f"[OK] {out.name}  visible={[round(v) for v in visible]}  clipped={clipped}")

    (OUT / "annotation_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
