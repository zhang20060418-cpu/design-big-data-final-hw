"""Regression checks for rendered backgrounds and empty text-box margins."""
from copy import deepcopy

from PIL import Image, ImageDraw
import pytest

from app.services import layout_context, review_agent, structure_rules
from app.services.six_detector import LABELS, SixDetectorAdapter


def sample(tmp_path, *, decoration=True, color="white"):
    path = tmp_path / "s0001.png"
    image = Image.new("RGB", (960, 540), color)
    if decoration:
        draw = ImageDraw.Draw(image)
        for x in range(0, 960, 36):
            draw.polygon([(x, 350), (x+22, 350), (x+90, 540), (x+60, 540)], fill="#bb180e")
    image.save(path)
    region = {"x": .05, "y": .55, "w": .9, "h": .4}
    context = {"page_index": 1, "page": {"png_path": str(path)}, "objects": []}
    detectors = {k: {"status": "no_candidate_in_available_scope", "score": 0., "evidence": [], "features": {},
                     "version": "frozen-fixture", "limitations": [], "coverage": "partial"} for k in LABELS}
    detectors["D5"].update(status="candidate", score=.6, evidence=[{"bbox": region, "object_ids": [], "kind": "known_empty_region"}])
    decisions = {k: {"automatic_risk_candidate": k == "D5", "review_required": k == "D5", "critical_review_reasons": [], "semantic_unresolved": []} for k in LABELS}
    raw = {"pages": [{"slide_index": 1, "slide_id": "1", "page": {"width_pt": 960., "height_pt": 540.},
                       "detectors": detectors, "screening_route": {"decisions": decisions, "candidate_labels": ["D5"]}}]}
    return raw, context, region


def test_background_in_geometric_void_becomes_unknown_not_a_defect(tmp_path):
    raw, context, region = sample(tmp_path)
    before = deepcopy(raw)
    corrected = layout_context.refine_results(raw, [context])
    page = corrected["pages"][0]
    assert raw == before  # No change to the frozen research output.
    assert page["detectors"]["D5"]["status"] == "unknown"
    assert page["detectors"]["D5"]["score"] == .6  # Preserve the original ordinal geometry score.
    assert not page["screening_route"]["decisions"]["D5"]["automatic_risk_candidate"]
    assert page["screening_route"]["decisions"]["D5"]["review_required"]
    result = SixDetectorAdapter().from_worker(corrected, {"slide_count": 1}, "doc")
    assert not result["issues"]
    assert result["claims"][4]["raw_status"] == "unknown"


@pytest.mark.parametrize("color", ["white", "#13213a", "#f5d4b4"])
def test_plain_background_does_not_hide_real_geometric_whitespace(tmp_path, color):
    raw, context, region = sample(tmp_path, decoration=False, color=color)
    corrected = layout_context.refine_results(raw, [context])
    assert corrected["pages"][0]["screening_route"]["decisions"]["D5"]["automatic_risk_candidate"]
    assert corrected["pages"][0]["detectors"]["D5"]["status"] == "candidate"


def test_native_text_is_excluded_from_background_texture_measurement(tmp_path):
    raw, context, region = sample(tmp_path)
    context["objects"] = [{"paragraphs": [{"text_chars": 30, "bbox": region}]}]
    check = layout_context.inspect_void(tmp_path/"s0001.png", region, context)
    assert check["sampled_cells"] == 0
    assert not check["conflicts_with_empty_region"]


def overlapping_frames():
    detail = {"slide_width_in": 10., "slide_height_in": 6., "slides": [{"index": 1, "text_chars": 20, "pictures": 0,
        "elements": [{"i": 0, "shape_id": 10, "kind": "text", "name": "body", "bbox": {"x": 1., "y": 1., "w": 6., "h": 4.}, "chars": 20, "opaque": False},
                     {"i": 1, "shape_id": 11, "kind": "shape", "name": "upper", "bbox": {"x": 1., "y": 3., "w": 6., "h": 2.}, "chars": 0, "opaque": True}],
        "blocks": [{"element_i": 0, "element_id": "body", "shape_id": 10, "name": "body", "text": "A line of text", "chars": 20,
                    "sizes": [18.], "bbox": {"x": 1., "y": 1., "w": 6., "h": 4.}, "word_wrap": True}]}]}
    native = [{"page_index": 1, "objects": [
        {"id": "s0001-shapeid10", "rotation": 0, "paragraphs": [{"text_chars": 20, "bbox": {"x": .12, "y": .2, "w": .5, "h": .07}}]},
        {"id": "s0001-shapeid11", "kind": "shape", "geometry_type": "rect", "opacity": {"alpha": 1.}}]}]
    return detail, native


def test_frame_overlap_without_actual_text_overlap_is_not_occlusion():
    detail, native = overlapping_frames()
    legacy, _ = structure_rules.analyze(detail, {})
    measured, _ = structure_rules.analyze(detail, {}, native_contexts=native)
    assert any(i["rule_id"] == "R05" for i in legacy)
    assert not any(i["rule_id"] == "R05" for i in measured)


def test_real_opaque_cover_over_measured_text_is_still_detected():
    detail, native = overlapping_frames()
    native[0]["objects"][0]["paragraphs"][0]["bbox"]["y"] = .6
    issues, _ = structure_rules.analyze(detail, {}, native_contexts=native)
    actual = [i for i in issues if i["rule_id"] == "R05"]
    assert len(actual) == 1 and actual[0]["details"]["actual_paragraph_layout_used"]
    assert actual[0]["bbox"]["h"] == pytest.approx(.42)


def test_transparent_fill_is_not_an_opaque_text_cover():
    detail, native = overlapping_frames()
    native[0]["objects"][0]["paragraphs"][0]["bbox"]["y"] = .6
    native[0]["objects"][1]["opacity"]["alpha"] = 0.
    issues, _ = structure_rules.analyze(detail, {}, native_contexts=native)
    assert not any(i["rule_id"] == "R05" for i in issues)


def test_missing_actual_layout_does_not_assert_text_is_occluded():
    detail, _ = overlapping_frames()
    issues, _ = structure_rules.analyze(detail, {}, native_contexts=[])
    assert not any(i["rule_id"] == "R05" for i in issues)


@pytest.mark.parametrize("defect", ["occluded_content", "excessive_whitespace"])
def test_visual_layout_verdict_cannot_rely_only_on_candidate_json(defect):
    item = {"candidate_id": "candidate-1", "defect_type": defect, "slide_index": 1,
            "evidence": [{"evidence_id": "candidate-1:e1"}], "object_ids": []}
    answer = {"verdicts": [{"candidate_id": "candidate-1", "defect_type": defect, "judgement": "supported",
        "evidence": "Candidate JSON alone", "evidence_refs": ["candidate-1:e1"], "severity": "low", "recommendation": "Inspect page."}]}
    with pytest.raises(ValueError, match="完整页面图"):
        review_agent.validate_review(answer, [item], {"page:s0001"})
    answer["verdicts"][0]["evidence_refs"].append("page:s0001")
    assert review_agent.validate_review(answer, [item], {"page:s0001"})["verdicts"][0]["judgement"] == "supported"


def test_whitespace_review_receives_real_full_page_and_roi_pixels(tmp_path):
    raw, context, region = sample(tmp_path)
    item = {"candidate_id": "d5", "defect_type": "excessive_whitespace", "slide_index": 1, "normalized_bbox": region, "object_ids": []}
    images = review_agent._images({"images": [{"slide_index": 1, "png_path": str(tmp_path/"s0001.png")}]}, [item], tmp_path)
    assert {i["kind"] for i in images} == {"full_page", "roi_context_crop"}
    prompt = review_agent.PromptBuilder().build([item], images, {})
    assert sum(p["type"] == "image_url" for p in prompt[1]["content"]) == 2
    assert "先仔细查看实际发送的完整页面图" in prompt[0]["content"]
    assert "透明文本框边界相交" in prompt[0]["content"]
