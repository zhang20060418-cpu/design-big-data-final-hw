"""Isolated D5 candidate: separate known side voids from body ink density.

The locked V3 detector is unchanged. This wrapper consumes only its measured
context/result, preserves unavailable decisions, and never changes D6. The new
hypothesis concerns a continuous empty lateral half-canvas, not low glyph ink.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import importlib.util
import math
from pathlib import Path


VERSION = "sixmix-spatial-axis-void-v1"
THRESHOLDS = {
    "lateral_known_empty_min": .50,
    "centroid_offset_min": .30,
    "measured_body_text_objects_min": 3,
    "unknown_area_max": .05,
}
_LEGACY = None


def _legacy_detector():
    global _LEGACY
    if _LEGACY is None:
        path = (Path(__file__).resolve().parents[2] /
                "stage4m_rule_optimization_20261029/implementation/whitespace_v2.py")
        spec = importlib.util.spec_from_file_location("sixmix_unchanged_v3_whitespace", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LEGACY = module.WhitespaceDetector()
    return _LEGACY


def _fraction(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def _navigation_protection(ctx):
    """Use an existing, independently measured navigation-role hypothesis.

    A contents word/role alone is insufficient. The upstream evidence must also
    establish numbered/short aligned navigation rows without data/media modules
    or long explanatory paragraphs. IDs are only provenance/identity joins.
    """
    page = ctx.get("page", {})
    if page.get("role") != "contents":
        return None
    for evidence in page.get("role_evidence", []):
        if evidence.get("kind") != "contents_navigation_structure" or evidence.get("accepted") is not True:
            continue
        pairs = evidence.get("numbered_label_pairs", [])
        inline = evidence.get("inline_numbered_ids", [])
        rows = evidence.get("aligned_short_rows", 0)
        if (not isinstance(pairs, list) or not isinstance(inline, list)
                or not isinstance(rows, int) or isinstance(rows, bool)):
            continue
        if (len(pairs) < 3 and len(inline) < 3 and rows < 4):
            continue
        if (evidence.get("substantial_paragraph_ids") or evidence.get("substantial_media_ids")
                or evidence.get("drawn_table_like")):
            continue
        return dict(source="shared_native_navigation_structure_not_generator_metadata",
                    numbered_pair_count=len(pairs), inline_numbered_count=len(inline),
                    aligned_short_rows=rows)
    return None


def _body_text_count(ctx, features):
    body_ids = features.get("layer_object_ids", {}).get("body", [])
    body_ids = set(body_ids) if isinstance(body_ids, (list, tuple)) else set()
    return sum(bool(str(o.get("text", "")).strip()) and bool(o.get("content_regions"))
               and o.get("id") in body_ids for o in ctx.get("objects", []))


def _strip_bbox(canvas, side, fraction):
    if not isinstance(canvas, Mapping) or any(not _fraction(canvas.get(k)) for k in ("x", "y", "w", "h")):
        return None
    if canvas["w"] <= 0 or canvas["h"] <= 0:
        return None
    return dict(x=canvas["x"] + (canvas["w"] * (1 - fraction) if side == "right" else 0),
                y=canvas["y"], w=canvas["w"] * fraction, h=canvas["h"])


def detect_whitespace(ctx, previous_result=None):
    """Return a standard D5 result without mutating context or prior output.

    With no prior result, invoke the unchanged V3 D5 on the supplied context.
    Existing unknown/unsupported decisions are retained. No filesystem assets,
    names, split, labels, templates or notes are inspected for prediction.
    """
    previous = previous_result if previous_result is not None else _legacy_detector().detect(ctx)
    if not isinstance(previous, Mapping) or previous.get("detector") != "D5":
        raise ValueError("A standard D5 previous_result is required")
    result = deepcopy(previous)
    result["version"] = VERSION
    features = result.setdefault("features", {})
    audit = dict(version=VERSION, previous_version=previous.get("version"),
                 previous_status=previous.get("status"), changed=False,
                 mechanism="independent_axis_void_and_measured_navigation_intent",
                 thresholds=dict(THRESHOLDS), decision="unchanged")
    features["spatial_refinement"] = audit
    if previous.get("status") in {"unknown", "unsupported"}:
        audit["decision"] = "preserve_unknown_or_unsupported"
        return result
    if previous.get("status") not in {"candidate", "no_candidate_in_available_scope"}:
        audit["decision"] = "preserve_unrecognized_status"
        return result

    navigation = _navigation_protection(ctx)
    if navigation:
        audit.update(navigation=navigation, decision="protect_measured_navigation_role")
        features["role_gate"] = "protected_measured_navigation_structure"
        if previous["status"] == "candidate":
            audit.update(changed=True, previous_triggered_rules=deepcopy(features.get("triggered_rules", [])))
            result.update(status="no_candidate_in_available_scope", score=0., evidence=[])
            features["triggered_rules"] = []
            result.setdefault("limitations", []).append(
                "Measured navigation structure is protected from body whitespace rules; this does not establish whole-page normality.")
        return result
    if previous["status"] == "candidate":
        return result
    if ctx.get("page", {}).get("role") not in {"body", "chart", "contents"}:
        return result

    body = features.get("body_metrics", {})
    side = features.get("lateral_known_empty_corridor_side")
    fraction = features.get("lateral_known_empty_corridor_fraction")
    offset = body.get("centroid_offset_half_diagonal")
    uncertainty = features.get("unknown_area_fraction")
    text_count = _body_text_count(ctx, features)
    audit["measured_body_text_objects"] = text_count
    # A large known-empty side corridor can coexist with dense text/table
    # inside a narrow column. Requiring low total occupancy AND a compact
    # two-dimensional body box incorrectly couples these independent facts.
    if (side not in {"left", "right"} or not _fraction(fraction) or not _fraction(offset)
            or not _fraction(uncertainty) or uncertainty > THRESHOLDS["unknown_area_max"]
            or fraction < THRESHOLDS["lateral_known_empty_min"]
            or offset < THRESHOLDS["centroid_offset_min"]
            or text_count < THRESHOLDS["measured_body_text_objects_min"]):
        return result
    empty_box = _strip_bbox(features.get("body_canvas"), side, fraction)
    if empty_box is None:
        audit["decision"] = "missing_valid_body_canvas"
        return result
    rule = "large_continuous_lateral_void_independent_of_body_density"
    result.update(status="candidate", score=max(float(result.get("score") or 0), fraction))
    features.setdefault("triggered_rules", []).append(rule)
    audit.update(changed=True, decision=rule, lateral_known_empty_fraction=fraction,
                 centroid_offset_half_diagonal=offset, unknown_area_fraction=uncertainty,
                 empty_corridor_bbox=empty_box,
                 body_occupancy_upper=body.get("occupancy_upper"))
    result.setdefault("evidence", []).append(dict(
        kind="known_lateral_half_canvas_void", bbox=empty_box,
        object_ids=deepcopy(features.get("layer_object_ids", {}).get("body", [])),
        measurement_source=features.get("mask_source", "unknown"), triggered_rules=[rule],
        measurements=dict(lateral_known_empty_fraction=fraction,
                          centroid_offset_half_diagonal=offset,
                          measured_body_text_objects=text_count,
                          unknown_area_fraction=uncertainty)))
    result.setdefault("limitations", []).append(
        "A continuous unused lateral half-canvas is an engineering review candidate; semantic layout intent is not confirmed.")
    return result


detect = detect_whitespace
