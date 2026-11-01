from copy import deepcopy
from pathlib import Path

import pytest

from app.schemas import DetectorClaim
from app.services.six_detector import LABELS, SixDetectorAdapter, _display_box


def worker_result():
    detectors = {code: dict(status="no_candidate_in_available_scope", version="frozen-fixture", score=0.,
        score_kind="ordinal_heuristic_not_probability", evidence=[], features={}, limitations=[], coverage="partial") for code in LABELS}
    decisions = {code: dict(automatic_risk_candidate=False, review_required=False,
        critical_review_reasons=[], semantic_unresolved=[]) for code in LABELS}
    return dict(status="complete", pages=[dict(slide_index=1, slide_id="267", page=dict(width_pt=720., height_pt=540., png_sha256="p" * 64),
        detectors=detectors, screening_route=dict(decisions=decisions))], input_source_sha256="s" * 64)


def test_six_claims_validate_and_no_probability():
    result = SixDetectorAdapter().from_worker(worker_result(), {"slide_count": 1}, "doc")
    assert len(result["claims"]) == 6
    assert not result["issues"]
    assert {DetectorClaim.model_validate(c).defect_type for c in result["claims"]} == set(LABELS.values())
    assert all("probability" in c["score_kind"] for c in result["claims"])
    assert result["capability"]["A05_semantic_status"] == "unsupported"


@pytest.mark.parametrize("status", ["candidate", "no_candidate_in_available_scope", "unknown", "unsupported", "not_checked"])
def test_five_raw_states_are_preserved(status):
    raw = worker_result()
    raw["pages"][0]["detectors"]["D1"]["status"] = status
    result = SixDetectorAdapter().from_worker(raw, {"slide_count": 1}, "doc")
    assert result["claims"][0]["raw_status"] == status


@pytest.mark.parametrize("status", ["unknown", "unsupported", "not_checked"])
def test_fallback_never_certifies_normal(status):
    result = SixDetectorAdapter().fallback({"slide_count": 3}, "doc", "measurement missing", status)
    assert len(result["claims"]) == 18 and not result["issues"]
    assert all(c["raw_status"] == status and c["score"] is None and c["screening_decision"]["review_required"] for c in result["claims"])
    assert not result["capability"]["zero_miss_guarantee"]
    assert {"type", "title", "detail"}.issubset(result["risks"][0])


def test_disabled_does_not_open_uploaded_source(tmp_path):
    result = SixDetectorAdapter(enabled=False).analyze(tmp_path / "missing.pptx", tmp_path, {"slide_count": 1}, {}, {}, "doc")
    assert result["status"] == "not_checked" and len(result["claims"]) == 6


def test_missing_runtime_is_explicitly_unsupported(tmp_path):
    result = SixDetectorAdapter(ml_python=tmp_path / "missing.exe").analyze(tmp_path / "a.pptx", tmp_path, {"slide_count": 1}, {}, {}, "doc")
    assert result["status"] == "unsupported"


def test_missing_measurement_is_unknown(tmp_path):
    import sys
    result = SixDetectorAdapter(ml_python=sys.executable).analyze(tmp_path / "a.pptx", tmp_path, {"slide_count": 1}, {}, {}, "doc")
    assert result["status"] == "unknown"


def test_candidate_uses_frozen_screening_decision_and_preserves_unknown():
    raw = worker_result()
    page = raw["pages"][0]
    page["detectors"]["D5"].update(status="unknown", score=.5)
    page["screening_route"]["decisions"]["D5"].update(automatic_risk_candidate=True, review_required=True)
    result = SixDetectorAdapter().from_worker(raw, {"slide_count": 1}, "doc")
    assert result["claims"][4]["raw_status"] == "unknown"
    assert len(result["issues"]) == 1 and result["issues"][0]["defect_type"] == "excessive_whitespace"
    assert result["issues"][0]["visual_probability"] is None


def test_native_coordinate_and_clipped_display_bbox():
    raw = worker_result()
    page = raw["pages"][0]
    page["detectors"]["D4"]["evidence"] = [dict(object_ids=["shape-1"], bbox=dict(x=.9, y=.3, w=.2, h=.1), measurement_source="live_COM")]
    page["screening_route"]["decisions"]["D4"]["automatic_risk_candidate"] = True
    result = SixDetectorAdapter().from_worker(raw, {"slide_count": 1}, "doc")
    claim = result["claims"][3]
    assert claim["native_bbox"]["x"] == pytest.approx(9.)
    assert claim["native_bbox"]["w"] == pytest.approx(2.)
    assert claim["normalized_bbox"]["w"] == pytest.approx(.1)
    assert claim["raw_normalized_bbox"]["w"] == pytest.approx(.2)
    assert result["issues"][0]["coordinate_space"] == "inches"


def test_d6_cross_page_peers_and_a05_limitation():
    raw = worker_result()
    page = raw["pages"][0]
    page["detectors"]["D6"]["features"]["recall_components"] = dict(A01=dict(status="review_candidate", features=dict(matching_peers=[dict(page_index=3), dict(page_index=5)])), A05=dict(status="unsupported", score=None))
    page["screening_route"]["decisions"]["D6"]["automatic_risk_candidate"] = True
    result = SixDetectorAdapter().from_worker(raw, {"slide_count": 1}, "doc")
    assert result["claims"][5]["related_slides"] == [1, 3, 5]
    assert result["claims"][5]["subdetectors"]["A05"]["status"] == "unsupported"
    assert any("A05" in text for text in result["claims"][5]["limitations"])


@pytest.mark.parametrize("count", [0, 2])
def test_output_page_coverage_must_match_upload(count):
    with pytest.raises(ValueError, match="uniquely cover"):
        SixDetectorAdapter().from_worker(worker_result(), {"slide_count": count}, "doc")


def test_missing_detector_is_not_filled_normal():
    raw = worker_result()
    del raw["pages"][0]["detectors"]["D2"]
    with pytest.raises(ValueError, match="Missing detector"):
        SixDetectorAdapter().from_worker(raw, {"slide_count": 1}, "doc")


def test_partial_render_preserves_other_page_and_failed_page_scope():
    raw = worker_result()
    second = deepcopy(raw["pages"][0])
    second["slide_index"], second["slide_id"] = 2, "268"
    for detector in second["detectors"].values():
        detector.update(status="unknown", score=None)
    raw["pages"].append(second)
    raw["pages"][0]["screening_route"]["decisions"]["D4"]["automatic_risk_candidate"] = True
    result = SixDetectorAdapter().from_worker(raw, {"slide_count": 2}, "doc")
    assert len(result["claims"]) == 12 and len(result["issues"]) == 1
    assert all(c["raw_status"] == "unknown" for c in result["claims"] if c["slide_index"] == 2)
    assert all({"type", "title", "detail"}.issubset(r) for r in result["risks"])


def test_fully_outside_bbox_has_no_fabricated_page_overlay():
    assert _display_box(dict(x=1.1, y=.2, w=.3, h=.2)) is None


def test_source_identity_mismatch_stops_before_worker(tmp_path):
    import hashlib
    import json
    import sys
    source = tmp_path / "upload.pptx"
    source.write_bytes(b"fixture")
    renders = tmp_path / "renders"
    renders.mkdir()
    context = renders / "native_contexts.json"
    context.write_text(json.dumps([dict(source=dict(pptx_path=str(source), pptx_sha256="wrong"))]), encoding="utf-8")
    meta = dict(meta=dict(six_measurement=dict(status="complete", context_sha256=hashlib.sha256(context.read_bytes()).hexdigest())))
    result = SixDetectorAdapter(ml_python=sys.executable).analyze(source, tmp_path, {"slide_count": 1}, {}, meta, "doc")
    assert result["status"] == "unknown"
    assert "do not correspond" in result["claims"][0]["limitations"][0]
