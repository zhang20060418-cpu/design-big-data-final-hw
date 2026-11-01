"""Independent integration regressions: identity, scope and evidence survival.

All fixtures are synthetic. No benchmark labels, images or model predictions
are used to exercise merge behavior or configure thresholds.
"""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.config import DEFECT_TYPES
from app.schemas import DetectorClaim
from app.services.merge import merge
from app.services.visual_classifier import VisualClassifierService


D2 = "occluded_content"
D6 = "generative_design_anomaly"


def claim(cid="native-1", *, status="candidate", slide=1, defect=D2, obj="shape-1", box=None, **extra):
    return dict(candidate_id=cid, document_id="doc-fixture", slide_id=f"slide-{slide}", slide_index=slide,
        defect_type=defect, subtype=None, detector_version="synthetic-contract-v1", raw_status=status,
        score=.7 if status == "candidate" else None, score_kind="ordinal_not_calibrated_probability",
        object_ids=[obj] if obj else [], native_bbox=box, normalized_bbox=None, related_slides=[slide],
        evidence=[dict(kind="synthetic_witness", object_ids=[obj] if obj else [], measurement_source="fixture")],
        coverage="partial", limitations=["Synthetic geometry only"], input_version={}, **extra)


def issue(cid="native-1", *, slide=1, defect=D2, obj="shape-1", box=None, source="six_detector", related=None):
    return dict(issue_id=cid, candidate_id=cid, document_id="doc-fixture", slide_index=slide,
        defect_type=defect, title="合成定位候选", evidence_source=source, rule_id="fixture-rule",
        evidence_summary="Synthetic evidence", element_id=obj, bbox=box,
        coordinate_space="inches" if box else None, normalized_bbox=None, severity="medium", confidence="candidate",
        review_status="unreviewed", review_result=None, recommendation="Inspect fixture evidence",
        execution_status="candidate", related_slides=related or [slide])


def review(cid, *, judgement="rejected", slide=1, defect=D2, **extra):
    return dict(status="ok", slide_index=slide, model="synthetic-mock", mock=True,
        verdicts=[dict(candidate_id=cid, defect_type=defect, judgement=judgement,
            evidence="Independent synthetic review evidence", recommendation="Preserve scope", **extra)])


def test_different_nonoverlapping_objects_are_not_merged():
    left, right = dict(x=.2, y=.2, w=1., h=1.), dict(x=5., y=4., w=1., h=1.)
    raw = [issue("left", obj="left-object", box=left), issue("right", obj="right-object", box=right)]
    claims = [claim("left", obj="left-object", box=left), claim("right", obj="right-object", box=right)]
    findings, summary = merge(raw, [], [], 1, claims=claims)
    assert len(findings) == 2 and summary["issue_count"] == 2
    assert {f["candidate_id"] for f in findings} == {"left", "right"}


def test_same_object_multisource_is_merged_without_losing_candidate_identity():
    box = dict(x=1., y=1., w=2., h=.6)
    raw = [issue("native", box=box), issue("legacy-rule", box=box, source="structure_rule")]
    claims = [claim("native", box=box), claim("legacy-rule", box=box)]
    findings, _ = merge(raw, [], [], 1, claims=claims)
    assert len(findings) == 1
    assert {"native", "legacy-rule"}.issubset(set(findings[0]["merged_candidate_ids"]))
    assert {s["source"] for s in findings[0]["sources"]} == {"six_detector", "structure_rule"}
    assert {c["candidate_id"] for c in findings[0]["claims"]} == {"native", "legacy-rule"}


def test_different_defect_types_on_same_object_remain_distinct():
    raw = [issue("occlusion", defect=D2), issue("clutter", defect="cluttered_layout")]
    findings, _ = merge(raw, [], [], 1, claims=[claim("occlusion"), claim("clutter", defect="cluttered_layout")])
    assert len(findings) == 2


def test_same_numeric_geometry_in_different_coordinate_spaces_is_not_merged():
    box = dict(x=.2, y=.2, w=.3, h=.3)
    a, b = issue("inches", obj=None, box=box), issue("normal", obj=None, box=box)
    b["coordinate_space"] = "normalized"
    findings, _ = merge([a, b], [], [], 1, claims=[])
    assert len(findings) == 2


def test_rejecting_specific_candidate_does_not_reject_same_type_elsewhere():
    raw = [issue("one", obj="one"), issue("two", obj="two")]
    findings, summary = merge(raw, [], [review("one")], 1, claims=[claim("one", obj="one"), claim("two", obj="two")])
    by_id = {f["candidate_id"]: f for f in findings}
    assert by_id["one"]["review_status"] == "rejected"
    assert by_id["two"]["review_status"] == "unreviewed"
    assert summary["rejected_count"] == 1 and summary["visible_issue_count"] == 1


def test_review_for_merged_alias_targets_only_its_shared_object():
    raw = [issue("primary"), issue("alias", source="structure_rule"), issue("other", obj="separate")]
    findings, _ = merge(raw, [], [review("alias")], 1,
        claims=[claim("primary"), claim("alias"), claim("other", obj="separate")])
    assert len(findings) == 2
    assert next(f for f in findings if f["candidate_id"] == "primary")["review_status"] == "rejected"
    assert next(f for f in findings if f["candidate_id"] == "other")["review_status"] == "unreviewed"


@pytest.mark.parametrize("state", ["unknown", "unsupported", "not_checked", "no_candidate_in_available_scope"])
def test_unassessed_claims_are_immutable_and_conflicting_scope_survives(state):
    raw = [issue("legacy", source="structure_rule")]
    claims = [claim("native", status=state)]
    before_raw, before_claims = deepcopy(raw), deepcopy(claims)
    findings, summary = merge(raw, [], [], 1, claims=claims)
    assert raw == before_raw and claims == before_claims
    assert summary["detection_status_counts"][state] == 1
    assert summary["unknown_is_normal"] is False
    assert findings[0]["claims"][0]["raw_status"] == state
    assert any(c["raw_status"] == state for c in findings[0]["source_conflicts"])


def test_candidate_rejection_never_erases_original_observation():
    claims = [claim("one")]
    findings, _ = merge([issue("one")], [], [review("one")], 1, claims=claims)
    assert findings[0]["raw_status"] == "candidate"
    assert findings[0]["claims"][0]["raw_status"] == "candidate"
    assert findings[0]["review_history"][0]["judgement"] == "rejected"
    assert claims[0]["raw_status"] == "candidate"


def test_failed_review_does_not_clear_or_confirm_candidate():
    failed = review("one")
    failed["status"] = "error"
    findings, _ = merge([issue("one")], [], [failed], 1, claims=[claim("one")])
    assert findings[0]["review_status"] == "unreviewed"
    assert findings[0]["review_result"] is None
    assert findings[0]["raw_status"] == "candidate"


def test_no_candidate_id_does_not_broadcast_in_six_claim_path():
    unscoped = review("one")
    del unscoped["verdicts"][0]["candidate_id"]
    findings, _ = merge([issue("one"), issue("two", obj="other")], [], [unscoped], 1,
        claims=[claim("one"), claim("two", obj="other")])
    assert all(f["review_status"] == "unreviewed" for f in findings)


def test_d6_cross_page_candidate_keeps_relations_without_page_broadcast():
    raw = [issue("cluster", defect=D6, related=[1, 2, 3]), issue("other-page", slide=2, defect=D6, obj="another")]
    claims = [claim("cluster", defect=D6), claim("other-page", slide=2, defect=D6, obj="another")]
    findings, _ = merge(raw, [], [review("cluster", defect=D6)], 3, claims=claims)
    by_id = {f["candidate_id"]: f for f in findings}
    assert by_id["cluster"]["related_slides"] == [1, 2, 3]
    assert by_id["cluster"]["review_status"] == "rejected"
    assert by_id["other-page"]["review_status"] == "unreviewed"


@pytest.mark.parametrize("judgement", ["supported", "rejected", "uncertain"])
def test_d6_child_review_is_recorded_but_never_overwrites_parent_verdict(judgement):
    parent = issue("parent-D6", defect=D6, related=[1, 2, 3])
    original = claim("parent-D6", defect=D6)
    child_review = review("child-A05", defect=D6, judgement=judgement,
        parent_candidate_id="parent-D6", subtype="A05")
    findings, _ = merge([parent], [], [child_review], 3, claims=[original])
    aggregate = next(f for f in findings if f["candidate_id"] == "parent-D6")
    assert aggregate["review_status"] == "unreviewed" and aggregate["review_result"] is None
    assert aggregate["raw_status"] == "candidate" and aggregate["claims"][0] == original
    assert aggregate["related_slides"] == [1, 2, 3]
    assert any(v.get("candidate_id") == "child-A05" and v.get("judgement") == judgement
               for v in aggregate["review_history"])
    subtype = aggregate.get("subtype_reviews", [])
    subtype = list(subtype.values()) if isinstance(subtype, dict) else subtype
    assert any(v.get("candidate_id") == "child-A05" for v in subtype)


def test_d6_different_children_do_not_overwrite_each_other():
    reviews = [review("child-A01", defect=D6, judgement="supported", parent_candidate_id="parent-D6", subtype="A01"),
               review("child-A05", defect=D6, judgement="rejected", parent_candidate_id="parent-D6", subtype="A05")]
    findings, _ = merge([issue("parent-D6", defect=D6)], [], reviews, 3, claims=[claim("parent-D6", defect=D6)])
    parent = next(f for f in findings if f["candidate_id"] == "parent-D6")
    assert parent["review_status"] == "unreviewed"
    history = {v["candidate_id"]: v["judgement"] for v in parent["review_history"]}
    assert history == {"child-A01": "supported", "child-A05": "rejected"}


@pytest.mark.parametrize("state", ["candidate", "no_candidate_in_available_scope", "unknown", "unsupported", "not_checked"])
def test_detector_claim_schema_preserves_five_states(state):
    result = DetectorClaim.model_validate(claim(status=state))
    assert result.raw_status == state


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_detector_claim_schema_rejects_nonfinite_score(score):
    data = claim()
    data["score"] = score
    with pytest.raises(ValidationError):
        DetectorClaim.model_validate(data)


def test_detector_claim_schema_rejects_normal_alias_for_unknown():
    with pytest.raises(ValidationError):
        DetectorClaim.model_validate(claim(status="normal"))


def test_legacy_four_label_visual_normalization_never_invents_d5_d6_probabilities():
    service = VisualClassifierService()
    service._manifest = dict(labels=DEFECT_TYPES[:4], thresholds={k: .5 for k in DEFECT_TYPES[:4]})
    raw = {label: .4 for label in DEFECT_TYPES[:4]}
    probs, alerts = service._normalise(raw)
    assert set(probs) == set(DEFECT_TYPES[:4]) == set(alerts)
    assert not set(DEFECT_TYPES[4:]) & set(probs)
    assert not set(DEFECT_TYPES[4:]) & set(alerts)


def test_legacy_four_label_visual_merge_does_not_create_d5_d6_findings():
    visual = [dict(status="ok", slide_index=1, probabilities={k: .8 for k in DEFECT_TYPES[:4]},
                   alerts={k: 1 for k in DEFECT_TYPES[:4]}, model_version="synthetic-four-only")]
    findings, summary = merge([], visual, [], 1, claims=[])
    assert {f["defect_type"] for f in findings} == set(DEFECT_TYPES[:4])
    assert all(summary["by_type"][label] == 0 for label in DEFECT_TYPES[4:])


def test_legacy_visual_missing_declared_label_is_an_error_not_zero():
    service = VisualClassifierService()
    service._manifest = dict(labels=DEFECT_TYPES[:4])
    with pytest.raises(ValueError, match="缺少标签"):
        service._normalise({DEFECT_TYPES[0]: .5})
