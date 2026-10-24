"""生成第一阶段自检样本：一页正常、一页人为越界。

输出：test_samples/selfcheck_stage1.pptx（2 页）
  - 第 1 页：全部对象在页面内，用于检查误报
  - 第 2 页：四个文本框分别向左/右/上/下越界，另有一个旋转元素与一个正常对象作对照
只写入 test_samples 目录，不触碰 downloaded_pptx 中的原始样本。
"""

from __future__ import annotations

import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

W, H = 10.0, 7.5  # 4:3，便于手算越界距离
OUT_DIR = Path(__file__).resolve().parent / "test_samples"

# 期望被检出的越界：(形状名, 方向, 距离英寸)
EXPECTED = [
    ("越界-左", "left", 0.60),
    ("越界-右", "right", 0.50),
    ("越界-上", "top", 0.35),
    ("越界-下", "bottom", 0.50),
]


def add_text(slide, name, left, top, width, height, text, size=18):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.name = name
    tf = box.text_frame
    tf.word_wrap = True
    tf.text = text
    for para in tf.paragraphs:
        para.alignment = PP_ALIGN.LEFT
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.name = "Microsoft YaHei"
    return box


def build() -> Path:
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    blank = prs.slide_layouts[6]

    # ---------------- 第 1 页：正常页面 ----------------
    s1 = prs.slides.add_slide(blank)

    title = add_text(s1, "正常-标题", 0.6, 0.4, 8.8, 0.9, "第一阶段自检：正常页面", 30)
    title.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    add_text(
        s1,
        "正常-正文",
        0.6,
        1.6,
        5.6,
        2.2,
        "本页所有对象均在页面边界内。\n用于验证检测器不产生误报。",
        18,
    )

    card = s1.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6.5), Inches(1.6), Inches(2.9), Inches(1.5)
    )
    card.name = "正常-色块"
    card.fill.solid()
    card.fill.fore_color.rgb = RGBColor(0xE8, 0xF0, 0xFE)
    card.line.color.rgb = RGBColor(0x2F, 0x6F, 0xED)
    card.text_frame.text = "装饰色块"
    for run in card.text_frame.paragraphs[0].runs:
        run.font.size = Pt(14)

    table_shape = s1.shapes.add_table(3, 2, Inches(0.6), Inches(4.2), Inches(5.6), Inches(1.8))
    table_shape.name = "正常-表格"
    table = table_shape.table
    for r in range(3):
        for c in range(2):
            cell = table.cell(r, c)
            cell.text = f"R{r}C{c}" if r else f"列 {c + 1}"
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(12)

    add_text(s1, "正常-底部说明", 6.5, 4.4, 2.9, 1.4, "页内对象：文本框、圆角矩形、表格。", 14)

    # ---------------- 第 2 页：人为越界 ----------------
    s2 = prs.slides.add_slide(blank)
    add_text(s2, "越界-页标题", 0.6, 0.3, 8.8, 0.7, "第二阶段自检：人为越界（预期 4 条）", 26)

    add_text(s2, "越界-左", -0.60, 1.6, 3.0, 0.8, "向左越界 0.60 英寸")
    add_text(s2, "越界-右", W - 1.5, 2.8, 2.0, 0.8, "向右越界 0.50 英寸")
    add_text(s2, "越界-上", 1.0, -0.35, 3.0, 0.8, "向上越界 0.35 英寸")
    add_text(s2, "越界-下", 1.0, H - 0.5, 3.0, 1.0, "向下越界 0.50 英寸")

    # 旋转对照：外接框变大但仍在页内，不应被误判
    rot = add_text(s2, "对照-旋转但未越界", 1.0, 4.2, 2.0, 1.0, "旋转 30°")
    rot.rotation = 30

    # 正常对照：与越界对象同页，不应被误判
    add_text(s2, "对照-正常对象", 6.0, 4.2, 3.2, 1.2, "本对象完全在页面内，不应出现在结果里。", 14)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "selfcheck_stage1.pptx"
    prs.save(str(out))

    (OUT_DIR / "expected.json").write_text(
        json.dumps(
            {
                "deck": out.name,
                "slide_size_in": [W, H],
                "slide1_expected_violations": 0,
                "slide2_expected": [
                    {"name": n, "side": s, "distance_in": d} for n, s, d in EXPECTED
                ],
                "slide2_expected_clean_names": ["对照-旋转但未越界", "对照-正常对象"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[OK] 已生成 {out}")
    return out


if __name__ == "__main__":
    build()
