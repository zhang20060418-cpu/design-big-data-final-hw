"""Annotated page renders: every finding gets a numbered box on the picture.

The repair itself never looks at pixels — it works on shape coordinates — but a
human reviewer (and any vision model the user pastes the page into) needs to see
*where* each finding sits.  This module draws those boxes on the original render
and writes them next to the repair record, so the result page can offer a
"标注画面" view and the exported task bundle can ship the same images.

Nothing here touches the deck or the audit report.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import DEFECT_LABELS_ZH
from . import verdicts

# 与前端 DEFECT_COLOR 同色系：越界偏红、遮挡深红、字号橙、拥挤棕、留白蓝、图标语义紫
COLORS = {
    "content_overflow_cutoff": (219, 78, 48),
    "occluded_content": (198, 52, 52),
    "improper_font_sizing": (222, 146, 40),
    "cluttered_layout": (186, 120, 26),
    "excessive_whitespace": (74, 128, 214),
    "generative_design_anomaly": (140, 96, 200),
}
FALLBACK = (110, 118, 136)

FONT_PATHS = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font(size: int):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_findings(image: Image.Image, issues: list[dict],
                  width_in: float, height_in: float) -> Image.Image:
    """Return a copy of *image* with a numbered box per finding that has a bbox.

    Findings without a box (the detectors sometimes only name an element) still
    keep their number in the page list; they simply cannot be circled here.
    """
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    sx = canvas.width / max(1e-6, float(width_in))
    sy = canvas.height / max(1e-6, float(height_in))
    font = _font(max(16, canvas.width // 74))
    pad = max(5, canvas.width // 320)
    stroke = max(3, canvas.width // 400)

    unlocated = []
    for number, issue in enumerate(issues, 1):
        box = issue.get("bbox") or {}
        try:
            x, y = float(box["x"]), float(box["y"])
            w, h = float(box["w"]), float(box["h"])
        except (KeyError, TypeError, ValueError):
            unlocated.append((number, issue))
            continue
        if w <= 0 or h <= 0:
            unlocated.append((number, issue))
            continue
        color = COLORS.get(issue.get("defect_type"), FALLBACK)
        x1 = max(0.0, x * sx)
        y1 = max(0.0, y * sy)
        x2 = min(canvas.width - 1.0, (x + w) * sx)
        y2 = min(canvas.height - 1.0, (y + h) * sy)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue

        draw.rectangle([x1, y1, x2, y2], fill=color + (26,))
        draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=stroke)

        label = str(number)
        tw = draw.textlength(label, font=font)
        bw = tw + pad * 2
        bh = font.size + pad * 1.6
        bx = min(max(0.0, x1), canvas.width - bw)
        by = max(0.0, y1 - bh)
        draw.rectangle([bx, by, bx + bw, by + bh], fill=color + (238,))
        draw.text((bx + pad, by + pad * 0.5), label, font=font, fill=(255, 255, 255))

    # 整页判断类缺陷没有对象坐标，画不出框；不标一下的话，标注图会和原图一模一样。
    # 放在左下角向上堆叠：顶部通常是标题区，挡在那里反而看不清页面本身。
    offset = canvas.height - pad
    for number, issue in unlocated:
        color = COLORS.get(issue.get("defect_type"), FALLBACK)
        label = DEFECT_LABELS_ZH.get(issue.get("defect_type"), "整页判断")
        text = f"{number}  {label} · 无对象坐标，整页判断"
        tw = draw.textlength(text, font=font)
        bh = font.size + pad * 1.6
        offset -= bh
        draw.rectangle([pad, offset, pad + tw + pad * 2, offset + bh], fill=color + (238,))
        draw.text((pad * 2, offset + pad * 0.5), text, font=font, fill=(255, 255, 255))
        offset -= pad

    return canvas


def annotate_pages(task_dir: Path, out_dir: Path, issues: list[dict],
                   width_in: float, height_in: float) -> dict[int, str]:
    """Write one annotated PNG per page that has visible findings."""
    renders = Path(task_dir) / "renders"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_page: dict[int, list[dict]] = {}
    for issue in issues:
        if not verdicts.is_visible(issue):
            continue
        index = int(issue.get("slide_index") or 0)
        if index <= 0:
            continue
        by_page.setdefault(index, []).append(issue)

    done: dict[int, str] = {}
    for index in sorted(by_page):
        source = renders / f"s{index:04d}.png"
        if not source.is_file():
            continue
        try:
            with Image.open(source) as image:
                canvas = draw_findings(image, by_page[index], width_in, height_in)
        except OSError:
            continue
        name = f"s{index:04d}.png"
        canvas.save(out_dir / name, optimize=True)
        done[index] = name
    return done
