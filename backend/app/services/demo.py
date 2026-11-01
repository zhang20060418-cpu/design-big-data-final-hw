"""Demonstration mode.

Demo data is generated from a fixture deck owned by this application.  Every
record is flagged ``demo=True`` and carries an explicit DEMO model name; demo
tasks live in a separate database and a separate folder tree, so they can never
be mistaken for a real audit of a user document.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from ..config import DEFECT_TYPES, FIXTURE_DIR

# Historical demo probability fixtures cover the original four labels only.
# D5/D6 come from the real six-detector adapter, never invented probabilities.
DEFECT_TYPES = DEFECT_TYPES[:4]

INK = RGBColor(0x0B, 0x10, 0x20)
MUTED = RGBColor(0x5B, 0x64, 0x78)
CYAN = RGBColor(0x00, 0xC2, 0xCB)
INDIGO = RGBColor(0x3D, 0x5A, 0xFE)
ORANGE = RGBColor(0xFF, 0x7A, 0x3D)
PAPER = RGBColor(0xF6, 0xF7, 0xFB)
FONT = "Microsoft YaHei"


def _text(slide, left, top, width, height, text, size=18, color=INK, bold=False,
          align=PP_ALIGN.LEFT, font=FONT):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    lines = text.split("\n")
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        run = para.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = font
    return box


def _rect(slide, left, top, width, height, color, line=False):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    if line:
        shape.line.color.rgb = color
    else:
        shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _background(slide, color=PAPER):
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(13.333), Inches(7.5))
    bg.fill.solid()
    bg.fill.fore_color.rgb = color
    bg.line.fill.background()
    bg.shadow.inherit = False
    return bg


def _image_only_png(path: Path) -> None:
    img = Image.new("RGB", (1600, 900), (12, 18, 36))
    draw = ImageDraw.Draw(img)
    for i in range(0, 1600, 40):
        draw.line([(i, 0), (i, 900)], fill=(18, 26, 48), width=1)
    draw.rectangle([90, 120, 1510, 780], outline=(0, 194, 203), width=3)
    try:
        title = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 74)
        body = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
    except Exception:
        title = ImageFont.load_default()
        body = ImageFont.load_default()
    draw.text((140, 220), "整页位图示例", font=title, fill=(240, 246, 255))
    draw.text((140, 340), "这一页的文字已经烘焙进图片，PowerPoint 对象层没有可编辑文本。", font=body, fill=(170, 190, 215))
    draw.text((140, 400), "结构规则在此页只能提示“能力受限”，视觉检查仍然可用。", font=body, fill=(170, 190, 215))
    draw.text((140, 660), "DEMO / IMAGE-ONLY PAGE", font=body, fill=(0, 194, 203))
    img.save(path)


def build_fixture(target: Path | None = None) -> Path:
    """Create (or reuse) the demo deck.  Returns the deck path."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(target or FIXTURE_DIR / "demo_deck.pptx")
    if target.exists():
        return target

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # 1 — cover
    s = prs.slides.add_slide(blank)
    _background(s, RGBColor(0x08, 0x0D, 0x1C))
    _text(s, 0.9, 1.9, 9.6, 0.5, "SLIDE SENTINEL / DEMO", 16, CYAN, True)
    _text(s, 0.9, 2.4, 11.4, 1.6, "演示用生成文稿", 54, RGBColor(0xF2, 0xF6, 0xFF), True)
    _text(s, 0.9, 4.1, 10.6, 1.2, "本文件由系统脚本生成，仅用于演示审查流程，不是真实模型输出，也不属于研究基准。",
          20, RGBColor(0x9F, 0xB0, 0xCB))
    _text(s, 0.9, 6.5, 8.0, 0.4, "DEMO FIXTURE · 10 SLIDES", 12, RGBColor(0x5F, 0x72, 0x93))

    # 2 — agenda
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 8.0, 0.6, "本次演示包含什么", 32, INK, True)
    items = [("01", "干净的版式"), ("02", "字号偏小"), ("03", "内容溢出"), ("04", "文本被覆盖"),
             ("05", "高密度版面"), ("06", "整页位图")]
    for i, (num, label) in enumerate(items):
        x = 0.9 + (i % 3) * 4.1
        y = 2.2 + (i // 3) * 2.2
        _rect(s, x, y, 3.6, 1.5, RGBColor(0xFF, 0xFF, 0xFF))
        _text(s, x + 0.3, y + 0.25, 0.8, 0.5, num, 18, INDIGO, True)
        _text(s, x + 0.3, y + 0.75, 3.0, 0.5, label, 18, INK, True)

    # 3 — clean
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 9.0, 0.7, "三层审查，逐页给出证据", 32, INK, True)
    _text(s, 0.9, 1.35, 11.5, 0.5, "结构规则负责可解释的几何证据，视觉模型负责整页观感，多模态复核只在必要时介入。",
          15, MUTED)
    for i, (title, body) in enumerate([
        ("结构规则", "对象越界、文本溢出、字号、遮挡候选"),
        ("视觉分类", "页面级缺陷概率与告警"),
        ("按需复核", "只对高风险页发起，保留调用记录"),
    ]):
        x = 0.9 + i * 4.05
        _rect(s, x, 2.4, 3.7, 3.4, RGBColor(0xFF, 0xFF, 0xFF))
        _text(s, x + 0.35, 2.75, 3.0, 0.5, f"0{i+1}", 16, CYAN, True)
        _text(s, x + 0.35, 3.25, 3.0, 0.5, title, 22, INK, True)
        _text(s, x + 0.35, 3.95, 3.0, 1.4, body, 14, MUTED)

    # 4 — tiny font
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 10.0, 0.6, "参数附录（字号偏小示例）", 30, INK, True)
    body = ("工作电压 3.3V，待机电流 12mA，峰值电流 180mA；通信接口 UART/I2C/SPI 可选；"
            "采样频率 50Hz，量程 ±16g，分辨率 16bit；工作温度 -20℃ 至 70℃；"
            "存储温度 -40℃ 至 85℃；湿度 5%–95% 无凝露；尺寸 32×18×4.2mm；"
            "重量 3.6g；校准周期 12 个月；外壳材质 PA66；防护等级 IP54。") * 2
    _text(s, 0.9, 1.7, 11.6, 3.4, body, 8, RGBColor(0x33, 0x3B, 0x4D))
    _text(s, 0.9, 5.4, 11.0, 0.5, "以上为整段 8pt 正文，正常观看时已难以辨认。", 18, ORANGE, True)

    # 5 — overflow (textbox extends past right edge)
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 8.0, 0.6, "溢出与裁切示例", 30, INK, True)
    _rect(s, 0.9, 2.0, 5.2, 3.4, RGBColor(0xFF, 0xFF, 0xFF))
    box = _text(s, 6.4, 2.0, 7.9, 1.6,
                "这段文字所在的文本框右端超出了画布右边缘，导出后会被直接裁掉，"
                "观众无法读到句子结尾。", 20, RGBColor(0x2A, 0x33, 0x45))
    box.width = Inches(7.9)  # right edge = 14.3in > 13.333in

    # 6 — overlap
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 8.0, 0.6, "文本被上层色块覆盖", 30, INK, True)
    _text(s, 1.0, 2.2, 5.6, 1.8, "这段说明文字本应完整可见，但被上层矩形压住，"
                                "读者只能看到左侧一小部分。", 20, RGBColor(0x2A, 0x33, 0x45))
    _rect(s, 1.6, 2.35, 6.2, 1.35, RGBColor(0xE9, 0xED, 0xF7))
    _text(s, 1.75, 2.55, 5.8, 0.6, "装饰层（本不该挡住正文）", 16, RGBColor(0x7C, 0x86, 0x9B))

    # 7 — dense
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.8, 0.55, 9.0, 0.6, "高密度版面示例", 28, INK, True)
    for i in range(18):
        x = 0.8 + (i % 6) * 2.05
        y = 1.5 + (i // 6) * 1.85
        _rect(s, x, y, 1.85, 1.55, RGBColor(0xFF, 0xFF, 0xFF))
        _text(s, x + 0.1, y + 0.1, 1.65, 1.35,
              f"指标 {i+1}\n数值 {12.4+i*3:.1f}\n同比 +{i%7+1}.2%\n口径说明", 8.8, RGBColor(0x33, 0x3B, 0x4D))

    # 8 — data slide with small axis labels
    s = prs.slides.add_slide(blank)
    _background(s)
    _text(s, 0.9, 0.7, 9.0, 0.6, "季度趋势（刻度文字偏小）", 30, INK, True)
    base_x, base_y, h = 1.4, 6.2, 3.8
    for i, value in enumerate([2.6, 3.4, 2.9, 4.4, 4.1, 5.2]):
        _rect(s, base_x + i * 1.75, base_y - value, 1.1, value, INDIGO if i % 2 == 0 else CYAN)
        _text(s, base_x + i * 1.75, base_y + 0.08, 1.3, 0.3, f"Q{i+1}  {value:.1f}", 8.5, MUTED)
    _text(s, 0.9, 1.5, 8.0, 0.4, "单位：十亿元", 12, MUTED)

    # 9 — image-only page
    s = prs.slides.add_slide(blank)
    png = FIXTURE_DIR / "demo_image_only.png"
    if not png.exists():
        _image_only_png(png)
    s.shapes.add_picture(str(png), 0, 0, width=prs.slide_width, height=prs.slide_height)

    # 10 — closing
    s = prs.slides.add_slide(blank)
    _background(s, RGBColor(0x08, 0x0D, 0x1C))
    _text(s, 0.9, 2.6, 11.0, 1.0, "审查的最后一公里", 44, RGBColor(0xF2, 0xF6, 0xFF), True)
    _text(s, 0.9, 3.7, 10.0, 0.8, "把可解释的证据交给设计者，而不是一个无法追问的分数。",
          20, RGBColor(0x9F, 0xB0, 0xCB))
    _text(s, 0.9, 6.4, 8.0, 0.4, "DEMO FIXTURE · END", 12, RGBColor(0x5F, 0x72, 0x93))

    prs.save(target)
    return target


DEMO_MODEL_NAME = "DEMO-FIXTURE（演示占位，非真实模型）"

# deterministic, hand-written demo probabilities per fixture slide
DEMO_PROBS: dict[int, dict[str, float]] = {
    1: {DEFECT_TYPES[0]: 0.03, DEFECT_TYPES[1]: 0.02, DEFECT_TYPES[2]: 0.04, DEFECT_TYPES[3]: 0.02},
    2: {DEFECT_TYPES[0]: 0.12, DEFECT_TYPES[1]: 0.03, DEFECT_TYPES[2]: 0.22, DEFECT_TYPES[3]: 0.04},
    3: {DEFECT_TYPES[0]: 0.08, DEFECT_TYPES[1]: 0.02, DEFECT_TYPES[2]: 0.19, DEFECT_TYPES[3]: 0.03},
    4: {DEFECT_TYPES[0]: 0.91, DEFECT_TYPES[1]: 0.05, DEFECT_TYPES[2]: 0.31, DEFECT_TYPES[3]: 0.10},
    5: {DEFECT_TYPES[0]: 0.14, DEFECT_TYPES[1]: 0.06, DEFECT_TYPES[2]: 0.28, DEFECT_TYPES[3]: 0.88},
    6: {DEFECT_TYPES[0]: 0.10, DEFECT_TYPES[1]: 0.86, DEFECT_TYPES[2]: 0.24, DEFECT_TYPES[3]: 0.07},
    7: {DEFECT_TYPES[0]: 0.34, DEFECT_TYPES[1]: 0.07, DEFECT_TYPES[2]: 0.83, DEFECT_TYPES[3]: 0.12},
    8: {DEFECT_TYPES[0]: 0.22, DEFECT_TYPES[1]: 0.04, DEFECT_TYPES[2]: 0.18, DEFECT_TYPES[3]: 0.09},
    9: {DEFECT_TYPES[0]: 0.05, DEFECT_TYPES[1]: 0.03, DEFECT_TYPES[2]: 0.12, DEFECT_TYPES[3]: 0.04},
    10: {DEFECT_TYPES[0]: 0.04, DEFECT_TYPES[1]: 0.02, DEFECT_TYPES[2]: 0.04, DEFECT_TYPES[3]: 0.02},
}

DEMO_VERDICTS: dict[int, list[dict]] = {
    4: [{"defect_type": DEFECT_TYPES[0], "judgement": "supported",
         "evidence": "参数段落约 8pt，整页观看难以辨认", "recommendation": "正文提升到 12pt 以上并拆页",
         "confidence": "high"}],
    5: [{"defect_type": DEFECT_TYPES[3], "judgement": "supported",
         "evidence": "右侧文本越出画布，结尾被裁掉", "recommendation": "缩短文本或把文本框左移",
         "confidence": "high"}],
    6: [{"defect_type": DEFECT_TYPES[1], "judgement": "supported",
         "evidence": "浅色矩形覆盖正文中段", "recommendation": "把装饰层置于文字下方",
         "confidence": "high"}],
    7: [{"defect_type": DEFECT_TYPES[2], "judgement": "uncertain",
         "evidence": "18 个卡片间距紧，但分组仍可区分", "recommendation": "拆成两页或加大卡片间距",
         "confidence": "medium"}],
}


def visual_results(slide_count: int, scenario: str) -> list[dict]:
    results = []
    for index in range(1, slide_count + 1):
        probs = DEMO_PROBS.get(index, {label: 0.05 for label in DEFECT_TYPES})
        if scenario == "risk":
            probs = {k: min(0.97, v + 0.25) for k, v in probs.items()}
        results.append({
            "status": "ok",
            "slide_index": index,
            "probabilities": probs,
            "alerts": {label: int(probs[label] >= 0.5) for label in DEFECT_TYPES},
            "model_version": DEMO_MODEL_NAME,
            "work_point": "演示固定阈值 0.50（非研究结论）",
            "input_recipe": "DEMO fixture",
            "latency_ms": 0,
            "demo": True,
        })
    return results


def reviews(pages: list[dict], scenario: str) -> list[dict]:
    results = []
    for page in pages:
        index = page["slide_index"]
        if scenario == "api_failure":
            results.append({
                "slide_index": index, "status": "network_error", "model": "DEMO-REVIEWER",
                "verdicts": [], "overall_note": "",
                "error": "演示场景：模拟 API 网络失败，其他检查结果已保留。",
                "error_code": "demo_network_error", "latency_ms": 1200, "attempts": 2, "demo": True,
            })
            continue
        verdicts = DEMO_VERDICTS.get(index, [])
        if scenario == "full" and index not in DEMO_VERDICTS:
            verdicts = [{"defect_type": DEFECT_TYPES[0], "judgement": "rejected",
                         "evidence": "正文尺寸正常，无可见过小文字", "recommendation": "无需修改",
                         "confidence": "medium"}]
        results.append({
            "slide_index": index, "status": "ok", "model": "DEMO-REVIEWER",
            "verdicts": verdicts, "overall_note": "演示复核结果。",
            "error": None, "error_code": None, "latency_ms": 1800, "attempts": 1, "demo": True,
        })
    return results
