"""AI repair: the model proposes, python-pptx edits a copy, rules re-check it.

The tests below pin the promises the feature makes:

* only findings with object-level coordinates may be rewritten automatically;
  everything the vision detector reported stays with a human;
* the model can pick an action but never invent one, and never touch a locked
  row;
* the edited copy is re-parsed with the very same structure rules, so "fixed"
  means the rule stopped firing instead of "the writer said so";
* a page with several kinds of defect gets all of them edited, not just the
  first one the planner happened to look at;
* the easy pages can leave the app as a bundle (deck + annotated render + a
  per-page prompt) and the hard pages accept a human's own instructions;
* the optional review loop only spends extra model calls while it is still
  making progress.
"""
from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

from app.config import DATA_DIR
from app.services import annotate, repair, structure_rules
from app.services.pptx_parser import parse_document
from app.storage import store_for


# ------------------------------------------------------------------ fixtures
def finding(issue_id: str, slide: int, defect: str, *, rule: str = "R01",
            source: str = "structure_rule", **extra) -> dict:
    data = {
        "issue_id": issue_id,
        "slide_index": slide,
        "defect_type": defect,
        "rule_id": rule,
        "title": "fixture finding",
        "severity": "medium",
        "evidence_source": source,
        "review_status": "confirmed",
        "element_id": f"s{slide:04d}-e001",
        "bbox": {"x": 1.0, "y": 1.0, "w": 2.0, "h": 0.6},
        "details": {"element": "TextBox 1", "box_in": {"x": 1.0, "y": 1.0, "w": 2.0, "h": 0.6}},
    }
    data.update(extra)
    return data


def shape_named(slide, name: str):
    for shape in slide.shapes:
        if shape.name == name:
            return shape
    raise AssertionError(f"shape {name!r} not found: {[s.name for s in slide.shapes]}")


def build_deck(path: Path) -> Path:
    """Three pages, one defect each: out of bounds, tiny font, occluded text."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    bounds = prs.slides.add_slide(blank)
    box = bounds.shapes.add_textbox(Inches(12.6), Inches(1.2), Inches(1.6), Inches(1.0))
    box.name = "越界文本框"
    box.text_frame.text = "这段说明文字从右侧越出了画布边界，需要移回安全区。"
    for para in box.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(14)

    fonts = prs.slides.add_slide(blank)
    small = fonts.shapes.add_textbox(Inches(0.9), Inches(1.0), Inches(3.2), Inches(1.4))
    small.name = "小字号正文"
    small.text_frame.word_wrap = True
    small.text_frame.text = ("这段正文的字号只有七磅，远低于审查系统给出的九磅参考线，在投影环境下"
                             "基本无法阅读。修复时既要按参考线放大字号，也要把文本框撑高，否则文字会"
                             "从框里溢出来，重新解析时又会被判成越界或截断，等于没有修好。")
    for para in small.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(7)

    layers = prs.slides.add_slide(blank)
    under = layers.shapes.add_textbox(Inches(1.4), Inches(1.6), Inches(6.0), Inches(1.4))
    under.name = "被压住的表头"
    under.text_frame.word_wrap = True
    under.text_frame.text = "这段文字原本在卡片里，后被一个不透明色块盖住，只露出一小部分。"
    for para in under.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(16)
    cover = layers.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1.35), Inches(1.55),
                                    Inches(6.3), Inches(1.6))
    cover.name = "覆盖色块"
    cover.fill.solid()
    cover.fill.fore_color.rgb = RGBColor(0x1F, 0x2A, 0x44)
    cover.line.fill.background()

    prs.save(str(path))
    return path


def detected(path: Path) -> tuple[list[dict], dict]:
    document, detail = parse_document(path)
    issues, _risks = structure_rules.analyze(detail, document)
    payload = {"document": document, "issues": issues,
               "task": {"id": "fixture"}, "summary": {"visible_issue_count": len(issues)}}
    return issues, payload


# --------------------------------------------------------------------- plan
def test_plan_keeps_detector_findings_for_humans():
    payload = {"issues": [
        finding("i-1", 2, "content_overflow_cutoff", rule="R01"),
        finding("i-2", 3, "content_overflow_cutoff", rule="R02"),
        finding("i-3", 4, "improper_font_sizing", rule="R03"),
        finding("i-4", 5, "occluded_content", rule="R05"),
        finding("i-5", 6, "cluttered_layout", rule="R04"),
        finding("i-6", 7, "occluded_content", source="vision_detector"),
        finding("i-7", 8, "occluded_content", user_verdict="dismissed"),
    ]}
    ops = {op["issue_id"]: op for op in repair.build_plan(payload)}

    assert set(ops) == {"i-1", "i-2", "i-3", "i-4", "i-5", "i-6"}
    assert ops["i-1"]["action"] == "move_inside"
    assert ops["i-2"]["action"] == "wrap_and_grow", "R02 是文本框内部溢出，应换行撑高而不是挪位置"
    assert ops["i-3"]["action"] == "raise_font" and ops["i-3"]["target_pt"] == 12.0
    assert ops["i-4"]["action"] == "bring_front"
    # 拥挤页没有对象坐标，只能整页整理；它不会让密度指标消失，但也不能整页不动
    assert ops["i-5"]["action"] == "tidy_layout" and ops["i-5"]["status"] == "pending"
    # 视觉检测器只有归一化区域，没有对象级坐标：锁定为人工，模型不许改
    assert ops["i-6"]["action"] == "manual" and ops["i-6"]["locked"] is True
    assert ops["i-6"]["detail"] == repair.DETECTOR_REASON
    assert ops["i-4"]["locked"] is False


def test_model_cannot_invent_actions_or_rewrite_locked_rows(monkeypatch):
    payload = {"issues": [
        finding("i-1", 2, "content_overflow_cutoff"),
        finding("i-2", 3, "improper_font_sizing", rule="R03"),
        finding("i-3", 4, "occluded_content", source="vision_detector"),
    ], "document": {"file_name": "fixture.pptx", "slide_count": 4, "aspect_label": "16:9"},
        "summary": {"visible_issue_count": 3}}
    ops = repair.build_plan(payload)

    reply = {"decisions": [
        {"issue_id": "i-1", "action": "delete_slide"},                      # 词表外的动作
        {"issue_id": "i-2", "action": "raise_font", "target_pt": 99,
         "note": "把正文提到 12 磅并撑高文本框。"},
        {"issue_id": "i-3", "action": "bring_front", "note": "模型试图改写锁定条目。"},
    ], "summary": "修好字号"}

    def fake_post(base, key, body, timeout):  # noqa: ANN001
        return 200, {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}

    monkeypatch.setattr("app.services.mllm._post_chat", fake_post)
    meta = repair.refine(ops, payload,
                         {"api_base_url": "http://127.0.0.1:8000/v1", "api_model": "fixture-model"},
                         "sk-test", backend="api")

    by_id = {op["issue_id"]: op for op in ops}
    assert meta["source"] == "llm" and meta["model"] == "fixture-model"
    assert by_id["i-1"]["action"] == "move_inside", "词表外的动作必须被丢弃"
    assert by_id["i-2"]["action"] == "raise_font"
    assert by_id["i-2"]["target_pt"] == 14.0, "字号上限 14pt，模型给 99 也要截断"
    assert by_id["i-2"]["note"].startswith("把正文提到 12 磅")
    assert by_id["i-3"]["action"] == "manual", "视觉检测器条目 Locked，模型改不动"


def test_model_failure_falls_back_to_rule_plan(monkeypatch):
    payload = {"issues": [finding("i-1", 2, "content_overflow_cutoff")]}
    ops = repair.build_plan(payload)

    def boom(*_a, **_k):
        raise TimeoutError("upstream timeout")

    monkeypatch.setattr("app.services.mllm._post_chat", boom)
    meta = repair.refine(ops, payload, {"api_base_url": "http://127.0.0.1:8000/v1"}, "sk-test")
    assert meta["source"] == "rule"
    assert "TimeoutError" in meta["degraded"]
    assert ops[0]["action"] == "move_inside", "回退后仍按规则计划执行"


def test_rule_backend_never_calls_the_model(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.mllm._post_chat", lambda *a, **k: calls.append(a))
    ops = repair.build_plan({"issues": [finding("i-1", 2, "improper_font_sizing")]})
    meta = repair.refine(ops, {"issues": []}, {"api_base_url": "http://127.0.0.1:8000/v1"},
                         "sk-test", backend="rule")
    assert meta["source"] == "rule" and "未配置模型" in meta["degraded"]
    assert calls == []


# -------------------------------------------------------------------- apply
def test_repair_clears_every_structure_finding(tmp_path):
    source = build_deck(tmp_path / "source.pptx")
    issues, payload = detected(source)
    defects = {issue["defect_type"] for issue in issues}
    assert {"content_overflow_cutoff", "improper_font_sizing", "occluded_content"} <= defects, defects

    before = repair.verify(source, issues)
    ops = repair.build_plan(payload)
    prs = Presentation(str(source))
    repair.apply_plan(prs, ops)
    fixed = tmp_path / "fixed.pptx"
    prs.save(str(fixed))

    reopen = Presentation(str(fixed))
    assert len(reopen.slides) == 3, "修复只改副本，页数与原件一致"

    status = {op["issue_id"]: op["status"] for op in ops}
    assert "skipped" not in status.values(), status
    after = repair.verify(fixed, issues)
    assert after["after_structure"] < before["before_structure"]
    assert after["introduced"] == 0, after
    assert after["fixed"] >= 3, after


def test_bounds_and_font_edits_are_measurable(tmp_path):
    source = build_deck(tmp_path / "source.pptx")
    issues, payload = detected(source)
    ops = repair.build_plan(payload)
    prs = Presentation(str(source))
    repair.apply_plan(prs, ops)

    slide_w = prs.slide_width / repair.EMU_IN
    slide_h = prs.slide_height / repair.EMU_IN
    moved = shape_named(prs.slides[0], "越界文本框")
    assert moved.left / repair.EMU_IN + moved.width / repair.EMU_IN <= slide_w - repair.MARGIN_IN + 0.01
    assert moved.top / repair.EMU_IN >= repair.MARGIN_IN - 0.01 and moved.left / repair.EMU_IN >= 0

    grown = shape_named(prs.slides[1], "小字号正文")
    sizes = [run.font.size.pt for para in grown.text_frame.paragraphs for run in para.runs]
    assert sizes and min(sizes) == pytest.approx(12.0), sizes
    assert grown.height / repair.EMU_IN > 1.1 + 0.2, "字号变大后文本框要跟着撑高"

    # 图层清理：文字被提上来，卡片底色（覆盖色块）仍在，没有被删掉
    page = prs.slides[2]
    names = [shape.name for shape in page.shapes]
    assert "覆盖色块" in names and "被压住的表头" in names
    order = {shape.name: index for index, shape in enumerate(page.shapes)}
    assert order["被压住的表头"] > order["覆盖色块"], "被压文字应排到覆盖色块之后（更上层）"


def test_wrap_and_grow_keeps_text_inside_the_canvas(tmp_path):
    source = build_deck(tmp_path / "source.pptx")
    prs = Presentation(str(source))
    slide_h = prs.slide_height / repair.EMU_IN
    box = shape_named(prs.slides[0], "越界文本框")
    box.left, box.top = Inches(1.2), Inches(6.4)
    op = {"issue_id": "wrap-1", "slide_index": 1, "defect_type": "content_overflow_cutoff",
          "action": "wrap_and_grow", "target_pt": None, "status": "pending",
          "ref": {"element_id": "s0001-e001", "name": "越界文本框"}, "bbox": {"x": 1.2, "y": 6.4, "w": 1.6, "h": 1.0},
          "note": "", "detail": "", "locked": False}
    repair.apply_plan(prs, [op])
    assert op["status"] == "applied", op["detail"]
    assert box.top / repair.EMU_IN + box.height / repair.EMU_IN <= slide_h - repair.MARGIN_IN + 0.01, \
        "撑高后底边不能越出画布"


def test_missing_target_is_reported_not_forced(tmp_path):
    source = build_deck(tmp_path / "source.pptx")
    prs = Presentation(str(source))
    op = {"issue_id": "ghost", "slide_index": 1, "defect_type": "content_overflow_cutoff",
          "action": "move_inside", "target_pt": None, "status": "pending",
          "ref": {"element_id": "s0001-e099", "name": "并不存在的对象"},
          "bbox": {"x": 9.5, "y": 6.5, "w": 0.1, "h": 0.1}, "note": "", "detail": "", "locked": False}
    repair.apply_plan(prs, [op])
    assert op["status"] == "skipped" and "没有在源文件中定位到对应对象" in op["detail"]


def test_verify_counts_by_type_and_page(tmp_path):
    source = build_deck(tmp_path / "source.pptx")
    issues, _payload = detected(source)
    result = repair.verify(source, issues)
    assert result["fixed"] == 0 and result["introduced"] == 0
    assert result["before_structure"] == result["after_structure"]
    assert set(result["by_type"]) == set(repair.STRUCTURE_TYPES)
    assert len(result["pages"]) == 3
    assert all("before" in page and "after" in page for page in result["pages"])


# ---------------------------------------------------------------------- api
def test_repair_list_endpoint_is_empty_on_a_fresh_store(client):
    response = client.get("/api/repairs")
    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_repair_request_rejects_unknown_task(client):
    response = client.post("/api/tasks/ffffffffffffffff/repairs?backend=rule")
    assert response.status_code == 404


# ---------------------------------------------------------- 修复后的可见重叠
def test_visible_overlaps_flags_text_painted_over_text(tmp_path):
    """四类结构规则全过、但两块文字压在一起时，必须被单独报出来。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    base = slide.shapes.add_textbox(Inches(1.0), Inches(2.0), Inches(5.0), Inches(0.6))
    base.text_frame.text = "这一行是原本就在页面上的说明文字。"
    for para in base.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(18)
    overlap = slide.shapes.add_textbox(Inches(3.2), Inches(2.1), Inches(5.0), Inches(0.6))
    overlap.text_frame.text = "这一行是后来压上去的脚注，落点和其他文字重叠。"
    for para in overlap.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(18)
    path = tmp_path / "collision.pptx"
    prs.save(str(path))

    result = repair.visible_overlaps(path)
    assert result["count"] >= 1
    assert result["pages"][0]["index"] == 1
    assert any(item["kind"] == "text" for item in result["pages"][0]["items"]), \
        "要给出具体是哪两块文字压在一起"


def test_visible_overlaps_flags_borderline_cover(tmp_path):
    """小于 R05 门槛（3%/45%）的遮挡不自动改，但要在复扫里报出来。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    text = slide.shapes.add_textbox(Inches(1.0), Inches(6.1), Inches(5.0), Inches(0.6))
    text.text_frame.text = "这段脚注被一条窄色带压住了下半截。"
    for para in text.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(11)
    # 覆盖 42%、占页面 1.2%：够不上 R05 的 45%/3%，所以不自动改，但要被复扫报出来
    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1.0), Inches(6.45), Inches(4.0), Inches(0.3))
    band.fill.solid()
    band.fill.fore_color.rgb = RGBColor(0xF2, 0xF2, 0xF2)
    path = tmp_path / "band.pptx"
    prs.save(str(path))

    result = repair.visible_overlaps(path)
    assert result["count"] >= 1
    assert result["pages"][0]["items"][0]["kind"] == "cover"


def test_visible_overlaps_is_quiet_on_a_clean_deck(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    first = prs.slides.add_slide(prs.slide_layouts[6])
    title = first.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(8.0), Inches(0.6))
    title.text_frame.text = "一张只有标题、没有重叠的干净页面"
    for para in title.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(28)
    body = first.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(8.0), Inches(0.5))
    body.text_frame.text = "下面这行正文和标题隔了整整一英寸。"
    for para in body.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(14)
    path = tmp_path / "clean.pptx"
    prs.save(str(path))
    assert repair.visible_overlaps(path) == {"count": 0, "pages": []}


# ------------------------------------------------- 同页多类缺陷都要被改到
def _card(slide, name, x, y, w, h):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y),
                                   Inches(w), Inches(h))
    shape.name = name
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xEE, 0xF2, 0xFB)
    shape.line.fill.background()
    return shape


def _text(slide, name, x, y, w, h, body, size=14):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    shape.name = name
    shape.text_frame.word_wrap = True
    shape.text_frame.text = body
    for para in shape.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(size)
    return shape


def test_moved_text_lands_inside_its_card(tmp_path):
    """用户要求：出界的文字不仅要移回画布，还要落回它自己的圆角衬底里。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _card(slide, "数据卡片", 8.5, 1.2, 4.0, 2.0)
    _text(slide, "卡片正文", 10.4, 1.45, 4.6, 1.2,
          "这段说明文字原本应该待在右侧的圆角卡片里，却被拉出了画布右边界。", 13)
    source = tmp_path / "card.pptx"
    prs.save(str(source))

    issues, payload = detected(source)
    assert [issue["rule_id"] for issue in issues] == ["R01"], issues
    ops = repair.build_plan(payload)
    prs = Presentation(str(source))
    repair.apply_plan(prs, ops)

    assert ops[0]["status"] == "applied", ops[0]["detail"]
    assert "收进衬底" in ops[0]["detail"], ops[0]["detail"]
    body = shape_named(prs.slides[0], "卡片正文")
    left, top = body.left / repair.EMU_IN, body.top / repair.EMU_IN
    right, bottom = left + body.width / repair.EMU_IN, top + body.height / repair.EMU_IN
    assert 8.5 + 0.05 <= left and right <= 12.5 - 0.05, (left, right)
    assert 1.2 + 0.05 <= top and bottom <= 3.2 - 0.05, (top, bottom)


def test_page_with_only_a_locked_finding_still_gets_tidied(tmp_path):
    """只剩视觉检测器条目时，也要先把间距整理掉，不能交一张原样的图。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _text(slide, "标题块", 1.0, 1.0, 5.0, 0.8, "本页用来检验密集版式的整理效果", 20)
    _text(slide, "块A", 1.0, 1.55, 4.6, 1.2, "第一段内容压住了标题块的下沿，需要拉开间距。", 12)
    _text(slide, "块B", 1.05, 2.30, 4.6, 1.2, "第二段内容和第一段有重叠，也要一起处理。", 12)
    _text(slide, "块D", 1.0, 4.20, 4.6, 1.0, "这一块离上面很远，间距明显过大。", 12)
    source = tmp_path / "sparse.pptx"
    prs.save(str(source))

    _issues, payload = detected(source)
    locked = finding("vision-1", 1, "excessive_whitespace", source="vision_detector",
                     bbox={"x": 0.4, "y": 0.3, "w": 0.2, "h": 0.2})
    payload["issues"] = [locked]
    ops = repair.build_plan(payload)
    assert ops[0]["action"] == "manual" and ops[0]["locked"] is True

    prs = Presentation(str(source))
    repair.apply_plan(prs, ops)

    assert ops[0]["status"] == "manual", "视觉条目本身不能被自动判定为修好"
    assert ops[0]["page_changed"] is True, "但页面必须真的动过"
    assert ops[0]["tidy"], "要记录整理了哪些块"
    assert "本页同时整理了" in ops[0]["detail"], ops[0]["detail"]
    tops = [shape_named(prs.slides[0], name).top / repair.EMU_IN
            for name in ("标题块", "块A", "块B", "块D")]
    assert tops != [1.0, 1.55, 2.30, 4.20], "块位置应有变化"


def test_wrap_and_grow_admits_when_it_changes_nothing(tmp_path):
    """文本框已经换行且够高时，不能再报 applied——那正是「前后一模一样」的来源。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = _text(slide, "已经很宽裕的正文", 1.0, 1.0, 5.0, 4.0,
                "这段正文所在的文本框本来就够高，自动撑高不会产生任何像素变化。", 12)
    box.text_frame.word_wrap = True
    source = tmp_path / "tall.pptx"
    prs.save(str(source))

    op = {"issue_id": "noop", "slide_index": 1, "defect_type": "content_overflow_cutoff",
          "action": "wrap_and_grow", "target_pt": None, "status": "pending",
          "ref": {"element_id": "s0001-e001", "name": "已经很宽裕的正文"},
          "bbox": {"x": 1.0, "y": 1.0, "w": 5.0, "h": 4.0}, "note": "", "detail": "",
          "locked": False}
    repair.apply_plan(prs, [op])
    assert op["status"] == "skipped", op["detail"]
    assert "不会改变版面" in op["detail"]
    assert op["page_changed"] is False


def test_one_page_with_two_defect_types_gets_both_edits(tmp_path):
    """用户明确要求过：一页里有多类缺陷时，不能只改第一类就交差。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    small = slide.shapes.add_textbox(Inches(0.9), Inches(0.7), Inches(3.2), Inches(1.2))
    small.name = "小字号正文"
    small.text_frame.word_wrap = True
    small.text_frame.text = "这段正文只有七磅，投影环境下根本看不清，需要按参考线放大。"
    for para in small.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(7)
    under = slide.shapes.add_textbox(Inches(1.4), Inches(3.6), Inches(6.0), Inches(1.4))
    under.name = "被压住的表头"
    under.text_frame.word_wrap = True
    under.text_frame.text = "这段文字原本在卡片里，后被一个不透明色块盖住，只露出下半截。"
    for para in under.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(16)
    cover = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1.35), Inches(3.55),
                                   Inches(6.3), Inches(1.6))
    cover.name = "覆盖色块"
    cover.fill.solid()
    cover.fill.fore_color.rgb = RGBColor(0x1F, 0x2A, 0x44)
    cover.line.fill.background()
    source = tmp_path / "mixed.pptx"
    prs.save(str(source))

    issues, payload = detected(source)
    kinds = {issue["defect_type"] for issue in issues if issue["slide_index"] == 1}
    assert {"improper_font_sizing", "occluded_content"} <= kinds, kinds

    ops = repair.build_plan(payload)
    prs = Presentation(str(source))
    repair.apply_plan(prs, ops)

    sizes = [run.font.size.pt for para in shape_named(prs.slides[0], "小字号正文")
             .text_frame.paragraphs for run in para.runs]
    assert sizes and min(sizes) >= 12.0, f"字号这一类必须改到：{sizes}"
    order = {shape.name: index for index, shape in enumerate(prs.slides[0].shapes)}
    assert order["被压住的表头"] > order["覆盖色块"], "遮挡这一类也必须改到，不能只改字号"


# -------------------------------------------------- 标注图 / 单页 prompt
def test_annotated_pages_mark_findings_even_without_a_box(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "renders").mkdir(parents=True)
    for index in (1, 2):
        Image.new("RGB", (320, 180), "white").save(task_dir / "renders" / f"s{index:04d}.png")
    issues = [
        finding("i-1", 1, "content_overflow_cutoff"),
        finding("i-2", 2, "generative_design_anomaly", bbox=None),
        finding("i-3", 3, "occluded_content"),          # 没有渲染图，跳过
    ]
    done = annotate.annotate_pages(task_dir, tmp_path / "out", issues, 13.333, 7.5)
    assert done == {1: "s0001.png", 2: "s0002.png"}, done

    for index, name in done.items():
        with Image.open(task_dir / "renders" / f"s{index:04d}.png") as original:
            plain = original.convert("RGB")
        with Image.open(tmp_path / "out" / name) as saved:
            marked = saved.convert("RGB")
        assert marked.size == plain.size
        assert ImageChops.difference(plain, marked).getbbox() is not None, \
            f"第 {index} 页的标注图和原图一模一样，等于没标"


def test_hints_are_kept_per_page_and_quoted_in_the_prompt(tmp_path):
    task_dir = tmp_path / "task"
    hint = "表格太挤，允许缩小字号并合并重复列。"
    repair.save_hint(task_dir, 7, hint)
    assert repair.load_hints(task_dir) == {"7": hint}

    payload = {"document": {"file_name": "fixture.pptx", "slide_count": 9,
                            "aspect_label": "16:9", "slide_width_in": 13.333,
                            "slide_height_in": 7.5},
               "issues": [finding("i-1", 7, "content_overflow_cutoff")]}
    ops = [{"issue_id": "i-1", "slide_index": 7, "action": "move_inside",
            "status": "applied", "detail": "把「越界文本框」移回画布：水平 -0.90in。"}]
    brief = repair.page_brief(payload, 7, ops, repair.load_hints(task_dir))

    assert brief["hint"] == hint
    assert "人工附加要求" in brief["prompt"] and hint in brief["prompt"]
    assert "系统已完成：把「越界文本框」移回画布" in brief["prompt"]
    assert "x=1.00, y=1.00" in brief["prompt"], "坐标要写成可以直接照着改的英寸值"
    assert brief["issues"][0]["status"] == "applied"

    repair.save_hint(task_dir, 7, "")
    assert repair.load_hints(task_dir) == {}, "清空输入框即删除这一页的想法"


def test_bundle_zip_contains_deck_pages_prompt_and_plan(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "source").mkdir(parents=True)
    source = build_deck(task_dir / "source" / "source.pptx")
    (task_dir / "renders").mkdir()
    for index in (1, 2, 3):
        Image.new("RGB", (320, 180), "white").save(task_dir / "renders" / f"s{index:04d}.png")
    repair_id = "r-20261101-120000"
    folder = task_dir / "repairs" / repair_id
    folder.mkdir(parents=True)
    shutil.copy2(source, folder / "fixed.pptx")
    repair._write_json(folder / "plan.json", {"ops": [], "meta": {}, "rounds": []})

    issues, payload = detected(source)
    payload["task"] = {"id": "bundle-fixture"}
    path = repair.build_bundle(task_dir, repair_id, payload)

    with zipfile.ZipFile(path) as bundle:
        names = set(bundle.namelist())
        prompt_md = bundle.read("prompt.md").decode("utf-8")
    assert {"source.pptx", "fixed.pptx", "prompt.md", "plan.json"} <= names, names
    assert {"annotated/s0001.png", "annotated/s0002.png", "annotated/s0003.png"} <= names, names
    assert "## 第 1 页" in prompt_md and "范围：x=" in prompt_md
    assert "annotated/s0001.png" in prompt_md, "prompt 要指明该看哪张标注图"


def test_prompts_carry_the_card_and_the_layout_rules(tmp_path):
    """模型要能读到「这块文字属于哪张卡片」，否则「放回衬底」只是句空话。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _card(slide, "数据卡片", 2.0, 1.2, 4.0, 2.0)
    _text(slide, "卡片正文", 2.4, 1.5, 3.2, 1.2, "这段正文原本就在卡片里，只是被挪动过。", 13)
    source = tmp_path / "layout.pptx"
    prs.save(str(source))

    issues, payload = detected(source)
    payload["issues"] = [finding("i-1", 1, "content_overflow_cutoff",
                                 bbox={"x": 2.5, "y": 1.6, "w": 3.0, "h": 1.0},
                                 details={"element": "卡片正文",
                                          "box_in": {"x": 2.5, "y": 1.6, "w": 3.0, "h": 1.0}})]
    ops = repair.build_plan(payload)
    layout = repair.deck_layout(source)[1]
    brief = repair.page_brief(payload, 1, [], {}, layout)
    assert "所属衬底：数据卡片" in brief["prompt"]
    assert "版式要求" in brief["prompt"] and "距内缘至少" in brief["prompt"]
    assert "不允许只处理最显眼的一条" in brief["prompt"]

    plan_brief = repair._plan_brief(payload, ops, "", {"layouts": repair.deck_layout(source)})
    assert plan_brief["pages"][0]["cards"][0]["name"] == "数据卡片"
    assert "数据卡片" in json.dumps(plan_brief["issues"], ensure_ascii=False)
    assert any("衬底" in line for line in plan_brief["layout_rules"])
    assert plan_brief["canvas"]["margin_in"] == pytest.approx(0.03)


# --------------------------------------------------------- 多轮 LLM 复核
def _fake_render(path, out_dir, long_edge, timeout):  # noqa: ANN001
    """Stand-in for the PowerPoint worker.

    The page image is derived from the slide's geometry, so a repaired copy
    renders a visibly different page while an untouched copy stays identical --
    exactly the property the pixel check looks for.  Blank images would make
    every repair look like a no-op and hide the bug we are testing for.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slides = []
    for index, slide in enumerate(Presentation(str(path)).slides, 1):
        seed = 0
        for shape in slide.shapes:
            seed = zlib.crc32(repr((seed, shape.name, shape.left, shape.top,
                                    shape.width, shape.height)).encode())
        image = Image.new("RGB", (320, 180), "white")
        draw = ImageDraw.Draw(image)
        seed |= 1  # 至少一格有内容，否则「改过但全白」和「没改」分不开
        for bit in range(8):  # 每位一格黑块：几何一变，画面必然不同
            if seed >> bit & 1:
                draw.rectangle([bit * 40, 100, bit * 40 + 39, 139], fill="black")
        image.save(out_dir / f"s{index:04d}.png")
        slides.append({"index": index, "status": "rendered", "png": f"s{index:04d}.png"})
    return {"ok": True, "slides": slides, "error": None}


def _blank_render(path, out_dir, long_edge, timeout):  # noqa: ANN001
    """A renderer that ignores the file content: before and after look identical.

    Mimics the real failure mode where a repair only reshuffles z-order, so the
    rules stop firing while the page itself does not change one pixel.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slides = []
    for index, _slide in enumerate(Presentation(str(path)).slides, 1):
        Image.new("RGB", (320, 180), "white").save(out_dir / f"s{index:04d}.png")
        slides.append({"index": index, "status": "rendered", "png": f"s{index:04d}.png"})
    return {"ok": True, "slides": slides, "error": None}


def _wait_repair(task_dir: Path, repair_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = repair.load_status(task_dir, repair_id)
        if status and status["status"] in ("completed", "failed"):
            return status
        time.sleep(0.15)
    raise AssertionError(f"repair {repair_id} did not finish: {status and status.get('status')}")


def _prepared_task(tmp_path: Path) -> tuple[Path, dict]:
    task_dir = tmp_path / "task"
    (task_dir / "source").mkdir(parents=True)
    source = build_deck(task_dir / "source" / "source.pptx")
    (task_dir / "renders").mkdir()
    for index in (1, 2, 3):
        Image.new("RGB", (320, 180), "white").save(task_dir / "renders" / f"s{index:04d}.png")
    issues, payload = detected(source)
    payload["task"] = {"id": "review-fixture"}
    return task_dir, payload


def test_review_mode_stops_as_soon_as_the_rules_pass(tmp_path, monkeypatch):
    task_dir, payload = _prepared_task(tmp_path)
    monkeypatch.setattr(repair, "render_pptx", _fake_render)

    started = repair.start(task_dir, payload, {}, "", backend="rule", review=True, max_rounds=3)
    status = _wait_repair(task_dir, started["repair_id"])

    assert status["status"] == "completed", status.get("message")
    assert status["review"]["enabled"] is True and status["review"]["max_rounds"] == 3
    assert [item["round"] for item in status["review"]["rounds"]] == [1], \
        "首轮就清零时再调模型纯属浪费"
    first = status["review"]["rounds"][0]
    assert (first["before"], first["after"], first["introduced"]) == (4, 0, 0), first
    assert any("结构规则已全部通过" in line["text"] for line in status["log"])
    assert status["summary"]["rounds"] == 1 and status["summary"]["applied"] == 4
    assert status["output"]["bundle_url"].endswith("/bundle")
    assert (task_dir / "repairs" / started["repair_id"] / "annots" / "s0001.png").is_file()


def test_layer_only_repair_is_reported_as_no_visible_change(tmp_path, monkeypatch):
    """规则清零但画面一模一样：不许报「已修复」，必须降级并解释原因。"""
    task_dir, payload = _prepared_task(tmp_path)
    monkeypatch.setattr(repair, "render_pptx", _blank_render)

    started = repair.start(task_dir, payload, {}, "", backend="rule", review=False)
    status = _wait_repair(task_dir, started["repair_id"])

    assert status["status"] == "completed", status.get("message")
    assert status["verify"]["visual"]["visible_pages"] == 0
    assert status["summary"]["visible_pages"] == 0
    assert status["summary"]["applied"] == 0, "画面没变就不能算已修"
    demoted = [op for op in status["ops"] if op["status"] == "partial"]
    assert demoted and all("画面一致" in op["detail"] for op in demoted)
    assert any("画面没有产生任何可见改动" in line["text"] for line in status["log"])
    assert all(page["changed"] is False for page in status["pages"])


def test_review_mode_stops_when_only_human_rework_is_left(tmp_path, monkeypatch):
    """剩余条目全是「必须人工重排」时，多跑几轮只会白烧 token。"""
    task_dir, payload = _prepared_task(tmp_path)
    monkeypatch.setattr(repair, "render_pptx", _fake_render)

    manual_only = {"before_structure": 3, "after_structure": 3, "fixed": 0, "introduced": 0,
                   "by_type": {dtype: {"before": 1, "after": 1} for dtype in repair.STRUCTURE_TYPES},
                   "pages": [{"index": 1, "before": 3, "after": 3}],
                   "issues": [finding("m-1", 1, "cluttered_layout", rule="R04")]}
    calls = []

    def fake_verify(fixed_path, before):  # noqa: ANN001
        calls.append(1)
        return dict(manual_only)

    monkeypatch.setattr(repair, "verify", fake_verify)
    started = repair.start(task_dir, payload, {}, "", backend="rule", review=True, max_rounds=3)
    status = _wait_repair(task_dir, started["repair_id"])

    assert status["status"] == "completed", status.get("message")
    assert len(calls) == 1, "只剩人工项时不允许再开下一轮"
    assert len(status["review"]["rounds"]) == 1
    assert any("必须人工重排" in line["text"] for line in status["log"])


def test_review_mode_gives_up_when_a_round_stops_improving(tmp_path, monkeypatch):
    task_dir, payload = _prepared_task(tmp_path)
    monkeypatch.setattr(repair, "render_pptx", _fake_render)

    stuck = {"before_structure": 4, "after_structure": 2, "fixed": 2, "introduced": 0,
             "by_type": {dtype: {"before": 0, "after": 0} for dtype in repair.STRUCTURE_TYPES},
             "pages": [{"index": 1, "before": 4, "after": 2}],
             "issues": [finding("s-1", 1, "content_overflow_cutoff", rule="R01")]}
    calls = []

    def fake_verify(fixed_path, before):  # noqa: ANN001
        calls.append(1)
        return dict(stuck)

    monkeypatch.setattr(repair, "verify", fake_verify)
    started = repair.start(task_dir, payload, {}, "", backend="rule", review=True, max_rounds=3)
    status = _wait_repair(task_dir, started["repair_id"])

    assert status["status"] == "completed", status.get("message")
    assert len(calls) == 2, "第二轮没有任何进展就该收手"
    assert len(status["review"]["rounds"]) == 2
    assert any("没有进一步减少结构问题" in line["text"] for line in status["log"])


# ------------------------------------------------------- 新端点：想法与任务包
def _seed_task() -> tuple[str, Path, str]:
    """A finished task on disk plus one finished repair record."""
    store = store_for("real")
    record = store.create(name="修复任务包测试", options={}, source_name="fixture.pptx",
                          size_bytes=1024)
    documents = {"file_name": "fixture.pptx", "slide_count": 3, "aspect_label": "16:9",
                 "slide_width_in": 13.333, "slide_height_in": 7.5,
                 "slides": [{"index": index} for index in (1, 2, 3)]}
    store.update(record["id"], status="completed", slide_count=3, document_json=documents)

    directory = DATA_DIR / "tasks" / record["id"]
    (directory / "source").mkdir(parents=True)
    build_deck(directory / "source" / "source.pptx")
    (directory / "renders").mkdir()
    for index in (1, 2, 3):
        Image.new("RGB", (320, 180), "white").save(directory / "renders" / f"s{index:04d}.png")

    issues = [finding("i-1", 1, "content_overflow_cutoff", rule="R01"),
              finding("i-2", 1, "improper_font_sizing", rule="R03"),
              finding("i-3", 2, "occluded_content", rule="R05")]
    payload = {"task": {"id": record["id"]}, "document": documents, "issues": issues,
               "summary": {"visible_issue_count": len(issues)}}
    (directory / "report").mkdir()
    (directory / "report" / "report.json").write_text(json.dumps(payload, ensure_ascii=False),
                                                     encoding="utf-8")
    repair_id = "r-20261101-120500"
    folder = directory / "repairs" / repair_id
    folder.mkdir(parents=True)
    repair._write_json(folder / "status.json",
                       {"repair_id": repair_id, "task_id": record["id"], "status": "completed",
                        "ops": [], "pages": []})
    return record["id"], directory, repair_id


def _drop_task(task_id: str) -> None:
    store_for("real").delete(task_id)


def test_hint_endpoint_saves_a_page_note_and_feeds_the_prompt(client):
    task_id, _directory, repair_id = _seed_task()
    try:
        saved = client.put(f"/api/tasks/{task_id}/repair-hints",
                           json={"slide_index": 1, "text": "两列可以合并，字号允许降到 10pt。"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["hints"]["1"] == "两列可以合并，字号允许降到 10pt。"

        assert client.get(f"/api/tasks/{task_id}/repair-hints").json()["hints"]["1"] == \
            "两列可以合并，字号允许降到 10pt。"

        brief = client.get(f"/api/tasks/{task_id}/repairs/{repair_id}/brief/1").json()
        assert brief["slide"] == 1 and brief["hint"] in brief["prompt"]
        assert "两列可以合并" in brief["prompt"]
        assert len(brief["issues"]) == 2, "同页两条缺陷都要进 prompt"

        cleared = client.put(f"/api/tasks/{task_id}/repair-hints",
                             json={"slide_index": 1, "text": "   "})
        assert cleared.json()["hints"] == {}
    finally:
        _drop_task(task_id)


def test_hint_endpoint_rejects_an_impossible_page(client):
    task_id, _directory, _repair_id = _seed_task()
    try:
        response = client.put(f"/api/tasks/{task_id}/repair-hints",
                              json={"slide_index": 0, "text": "无效页码"})
        assert response.status_code == 400
    finally:
        _drop_task(task_id)


def test_bundle_endpoint_ships_images_and_the_annot_view_serves_them(client):
    task_id, directory, repair_id = _seed_task()
    try:
        shutil.copy2(directory / "source" / "source.pptx",
                     directory / "repairs" / repair_id / "fixed.pptx")
        response = client.get(f"/api/tasks/{task_id}/repairs/{repair_id}/bundle")
        assert response.status_code == 200, response.text
        assert "attachment" in response.headers.get("content-disposition", "")
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            names = set(bundle.namelist())
        assert {"source.pptx", "fixed.pptx", "prompt.md"} <= names, names
        assert {"annotated/s0001.png", "annotated/s0002.png"} <= names, names

        image = client.get(f"/api/tasks/{task_id}/repairs/{repair_id}/slides/1/annot")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert client.get(f"/api/tasks/{task_id}/repairs/{repair_id}/slides/3/annot").status_code == 404
    finally:
        _drop_task(task_id)
