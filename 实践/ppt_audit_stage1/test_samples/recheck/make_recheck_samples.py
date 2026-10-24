"""构造第三步（文字可见性复检）的三个对照样本。

产物：test_samples/recheck/recheck_samples.pptx（5 页，13.333×7.5 in）

  第 1 页 S1_normal        ：文本框外框越界，但文字完整在页内（对应 AddAgenda/Calibri 的形态）
  第 2 页 S2_right_clip    ：单行文本向右伸出页面，字符应被页面右边缘切掉
  第 3 页 S2_bottom_clip   ：单行大字号文本底部越过页面下边缘，字形下半应被切掉
  第 4 页 S3_tight_none    ：小框 + 大量文字 + 不自动缩放（观察 PowerPoint 是否裁切框内文字）
  第 5 页 S3_tight_shrink  ：同样小框 + 大量文字 + 溢出时缩小文字（观察自动缩放，不是裁切）

期望标签在运行渲染核实后才写入 expected.json（见 verify_samples.py）。
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

OUT_DIR = Path(__file__).resolve().parent
OUT_FILE = OUT_DIR / "recheck_samples.pptx"

PAGE_W, PAGE_H = 13.333, 7.5
INK = RGBColor(0x1F, 0x24, 0x2B)
ACCENT = RGBColor(0x1D, 0x4E, 0xD8)
MUTED = RGBColor(0x6B, 0x72, 0x80)


def blank(prs: Presentation):
    # 0 = Title Slide, 6 = Blank
    return prs.slides.add_slide(prs.slide_layouts[6])


def textbox(slide, left, top, width, height, text, size=24, *, wrap=True,
            autosize=MSO_AUTO_SIZE.NONE, align=PP_ALIGN.LEFT, bold=False,
            color=INK, anchor=MSO_ANCHOR.TOP, name=None):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    if name:
        box.name = name
    tf = box.text_frame
    tf.word_wrap = wrap
    tf.auto_size = autosize
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(0.1)
    tf.margin_right = Inches(0.1)
    tf.margin_top = Inches(0.05)
    tf.margin_bottom = Inches(0.05)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = "Arial"
        run.font.color.rgb = color
    return box


def caption(slide, text, top=0.35):
    textbox(slide, 0.6, top, 8.0, 0.45, text, 14, color=MUTED)


def build() -> Presentation:
    prs = Presentation()
    prs.slide_width = Inches(PAGE_W)
    prs.slide_height = Inches(PAGE_H)

    # ---------- 样本 1：外框越界，文字完整 ----------
    s1 = blank(prs)
    caption(s1, "S1_normal  ·  外框越界，文字应完整可见")
    # 右侧越界：框右边缘 14.6 in > 13.333 in，但文字很短，左对齐
    b1 = textbox(s1, 8.6, 1.6, 6.0, 1.2, "ALL VISIBLE", 28, bold=True, color=ACCENT,
                 name="S1_RightFrame_TextVisible")
    # 下方越界：框底 8.9 in > 7.5 in，文字只有一行且在框顶
    b2 = textbox(s1, 0.9, 6.9, 6.0, 2.0, "BOTTOM FRAME OVERFLOW, TEXT INSIDE", 18,
                 name="S1_BottomFrame_TextVisible")
    textbox(s1, 0.9, 2.4, 7.0, 1.6,
            "这两个文本框的外框分别越过页面右边缘和下边缘，\n但文字本身完全落在页面内。\n预期：没有可见文字裁切。",
            16, color=MUTED)
    assert b1.left is not None and b2.left is not None

    # ---------- 样本 2a：页面右边缘真实裁切 ----------
    s2 = blank(prs)
    caption(s2, "S2_right_clip  ·  单行文本向右伸出页面，右端字符应被切掉")
    textbox(s2, 9.0, 3.0, 4.0, 1.0, "RIGHT EDGE CLIPPED TEXT", 40, wrap=False, bold=True,
            color=ACCENT, name="S2_RightClippedText")
    textbox(s2, 0.9, 5.3, 7.0, 1.2,
            "该文本框不换行（wrap=none），文字从 9.0 in 处向右排，\n按字号估算应超出 13.333 in 的页面右边缘。",
            15, color=MUTED)

    # ---------- 样本 2b：页面下边缘真实裁切 ----------
    s3 = blank(prs)
    caption(s3, "S2_bottom_clip  ·  大字号文本底部越过页面下边缘")
    textbox(s3, 1.2, 6.95, 10.0, 1.0, "BOTTOM EDGE CLIPPED TEXT", 54, wrap=False, bold=True,
            color=ACCENT, name="S2_BottomClippedText")
    textbox(s3, 0.9, 2.0, 9.0, 1.6,
            "该行文字基线位于 7.5 in 页面下边缘之外，\n渲染后字形的下半部分应被页面裁掉。",
            16, color=MUTED)

    # ---------- 样本 3a：小框 + 长文字 + 不缩放 ----------
    s4 = blank(prs)
    caption(s4, "S3_tight_none  ·  1.6×0.5 in 小框，18pt 长文字，不自动缩放")
    textbox(s4, 1.0, 1.8, 1.6, 0.5, "This sentence is far too long for a 1.6 by 0.5 inch box.",
            18, autosize=MSO_AUTO_SIZE.NONE, name="S3_TightBox_NoAutofit")
    textbox(s4, 1.0, 4.2, 7.5, 1.6,
            "预期观察点：PowerPoint 对普通文本框的溢出文字\n是继续绘制在框外，还是裁切在框内。\n以渲染结果为准，不预设结论。",
            16, color=MUTED)

    # ---------- 样本 3b：同样小框 + 溢出时缩小文字 ----------
    s5 = blank(prs)
    caption(s5, "S3_tight_shrink  ·  同样 1.6×0.5 in 小框，开启溢出时缩小文字")
    textbox(s5, 1.0, 1.8, 1.6, 0.5, "This sentence is far too long for a 1.6 by 0.5 inch box.",
            18, autosize=MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE, name="S3_TightBox_ShrinkText")
    textbox(s5, 1.0, 4.2, 7.5, 1.6,
            "预期观察点：文字是否被自动缩小（记录实际字号），\n以及缩小后是否仍越出框外。",
            16, color=MUTED)

    return prs


def main() -> int:
    prs = build()
    prs.save(str(OUT_FILE))
    print(f"[OK] {OUT_FILE}  slides={len(prs.slides)}  "
          f"{prs.slide_width.inches:.3f}x{prs.slide_height.inches:.3f} in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
