"""生成第三阶段（智能体调度）测试案例。

设计目标：准备“真正存在可见问题 / 人工判断不确定”的页面，而不是重复第一阶段
四个没有可见裁切的案例。

四页各司其职：
  V1  正常对照页                 -> 期望规则直接 report，不调用视觉模型
  V2  复杂照片背景 + 文字越过页底 -> 期望第二阶段 uncertain -> recheck（真实裁切）
  V3  文字被上层不透明色块遮挡     -> 第二阶段看不到（no_visible_clip），
                                    由智能体的“遮挡”规则触发 recheck（真实可见问题）
  V4  文字压在花哨图片上、对比度低 -> 第二阶段看不到越界，由“图片背景可读性”
                                    规则触发 recheck（人工判断不确定）

只生成测试文件，不修改任何原始 PPT。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

HERE = Path(__file__).resolve().parent
DECK = HERE / "stage3_visual_cases.pptx"
ASSETS = HERE / "assets"
EXPECTED = HERE / "expected.json"


def _bg_photo(path: Path, w: int = 1600, h: int = 900) -> None:
    """深色星云状背景：颜色分布分散，主背景占比低，墨迹判定应降级。"""
    rnd = random.Random(20261024)
    im = Image.new("RGB", (w, h), (8, 12, 30))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(8 + 18 * t), int(12 + 10 * t), int(30 + 40 * t)))
    for _ in range(90):
        cx, cy = rnd.randrange(w), rnd.randrange(h)
        r = rnd.randrange(60, 260)
        color = (rnd.randrange(30, 120), rnd.randrange(20, 90), rnd.randrange(90, 200))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    im = im.filter(ImageFilter.GaussianBlur(38))
    d = ImageDraw.Draw(im)
    for _ in range(500):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r = rnd.choice([1, 1, 1, 2])
        d.ellipse([x, y, x + r, y + r], fill=(220, 225, 240))
    im.save(path)


def _bg_confetti(path: Path, w: int = 1600, h: int = 900) -> None:
    """高亮花哨背景：用于构造低对比度文字（可读性存疑，非裁切）。"""
    rnd = random.Random(7)
    im = Image.new("RGB", (w, h), (246, 238, 205))
    d = ImageDraw.Draw(im)
    palette = [(250, 220, 90), (245, 200, 120), (255, 236, 160),
               (232, 210, 140), (250, 245, 210), (235, 220, 170)]
    for _ in range(420):
        x, y = rnd.randrange(w), rnd.randrange(h)
        rw, rh = rnd.randrange(30, 130), rnd.randrange(10, 45)
        d.rounded_rectangle([x, y, x + rw, y + rh], radius=8, fill=rnd.choice(palette))
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    im.save(path)


def _set_text(tf, lines: list[str], size: int, color: RGBColor, bold: bool = False,
              align: PP_ALIGN = PP_ALIGN.LEFT) -> None:
    tf.word_wrap = True
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(6)
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = "Microsoft YaHei"


def build() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    photo = ASSETS / "bg_nebula.png"
    confetti = ASSETS / "bg_confetti.png"
    _bg_photo(photo)
    _bg_confetti(confetti)

    prs = Presentation()
    prs.slide_width = Inches(13.3333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # ---------------- V1：正常对照 ----------------
    s1 = prs.slides.add_slide(blank)
    box = s1.shapes.add_textbox(Inches(0.83), Inches(1.4), Inches(8.6), Inches(0.9))
    box.name = "QA_V1_TITLE"
    _set_text(box.text_frame, ["V1 正常对照页"], 30, RGBColor(0x1F, 0x2A, 0x37), bold=True)
    body = s1.shapes.add_textbox(Inches(0.83), Inches(2.6), Inches(8.2), Inches(3.2))
    body.name = "QA_V1_TEXT"
    _set_text(body.text_frame, [
        "This page is a clean control sample.",
        "The paragraph stays well inside the slide boundary,",
        "so the second pass should report no visible clipping",
        "and the agent should not spend a vision call here.",
    ], 20, RGBColor(0x33, 0x3D, 0x4A))

    # ---------------- V2：复杂背景 + 越过页底（真实裁切） ----------------
    s2 = prs.slides.add_slide(blank)
    s2.shapes.add_picture(str(photo), 0, 0, width=prs.slide_width, height=prs.slide_height)
    t2 = s2.shapes.add_textbox(Inches(0.9), Inches(6.85), Inches(10.8), Inches(1.4))
    t2.name = "QA_V2_TEXT"
    _set_text(t2.text_frame, [
        "Reservoir damage caused by additive residue can plug the pore throat",
        "and reduce permeability in the invaded zone; the last lines of this",
        "paragraph deliberately run past the bottom edge of the slide.",
    ], 22, RGBColor(0xFF, 0xFF, 0xFF))

    # ---------------- V3：文字被上层色块遮挡（真实可见问题） ----------------
    s3 = prs.slides.add_slide(blank)
    title3 = s3.shapes.add_textbox(Inches(0.8), Inches(0.8), Inches(9.0), Inches(0.8))
    title3.name = "QA_V3_TITLE"
    _set_text(title3.text_frame, ["V3 文字被覆盖"], 30, RGBColor(0x1F, 0x2A, 0x37), bold=True)
    t3 = s3.shapes.add_textbox(Inches(1.0), Inches(2.8), Inches(8.0), Inches(1.6))
    t3.name = "QA_V3_TEXT"
    _set_text(t3.text_frame, [
        "The conclusion of this slide is hidden behind the red block:",
        "the agent must fuse z-order and fill evidence,",
        "because page-edge clipping rules cannot see this defect.",
    ], 20, RGBColor(0x22, 0x22, 0x22))
    block = s3.shapes.add_shape(1, Inches(4.55), Inches(2.65), Inches(3.5), Inches(1.95))
    block.name = "QA_V3_COVER"
    block.fill.solid()
    block.fill.fore_color.rgb = RGBColor(0xD6, 0x28, 0x28)
    block.line.fill.background()
    block.text_frame.text = ""

    # ---------------- V4：花哨背景上的低对比文字（人工判断不确定） ----------------
    s4 = prs.slides.add_slide(blank)
    s4.shapes.add_picture(str(confetti), 0, 0, width=prs.slide_width, height=prs.slide_height)
    t4 = s4.shapes.add_textbox(Inches(0.8), Inches(3.0), Inches(11.2), Inches(1.6))
    t4.name = "QA_V4_TEXT"
    _set_text(t4.text_frame, [
        "Pale yellow text on a bright confetti photo:",
        "nothing is clipped, but readability is a judgement call.",
    ], 24, RGBColor(0xFF, 0xF6, 0xB0))

    prs.save(DECK)

    expected = {
        "deck": DECK.name,
        "cases": [
            {"case_id": "V1", "slide": 1, "shape_name": "QA_V1_TEXT",
             "expected_second_pass": "no_visible_clip",
             "expected_route": "report",
             "why": "文字完整位于页内，无任何异常信号"},
            {"case_id": "V2", "slide": 2, "shape_name": "QA_V2_TEXT",
             "expected_second_pass": "uncertain",
             "expected_route": "recheck",
             "why": "可见文字越过页底，但复杂照片背景使页边墨迹判定不可靠"},
            {"case_id": "V3", "slide": 3, "shape_name": "QA_V3_TEXT",
             "expected_second_pass": "no_visible_clip",
             "expected_route": "recheck",
             "why": "文字未被页边裁切，但被更高 z 序的不透明色块覆盖，需遮挡规则"},
            {"case_id": "V4", "slide": 4, "shape_name": "QA_V4_TEXT",
             "expected_second_pass": "no_visible_clip",
             "expected_route": "recheck",
             "why": "无裁切，但文字整体压在花哨图片上，可读性需视觉判断"},
        ],
    }
    EXPECTED.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {DECK}")
    print(f"written: {EXPECTED}")


if __name__ == "__main__":
    build()
