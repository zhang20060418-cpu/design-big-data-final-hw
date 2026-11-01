"""D5 evidence-only recall candidate; historical rules remain immutable.

The earlier recall rule mixed a previous candidate bit into its continuous
geometry score at 0.55.  That made historical false positives impossible to
withdraw regardless of current measured layout.  This adapter recomputes the
same geometry without that inherited decision.  It adds no image-ID, label,
template, filename, or source-specific exceptions.

Unknown pixels remain blocked. A finite score measures known empty space only;
it does not imply that unresolved objects were found harmless. The caller must
report uncertainty independently and calibrate any operating threshold on the
declared reused-development cohorts.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path


_FROZEN = Path(__file__).resolve().parents[2] / "d5d6_recall_diagnostics_20261030" / "implementation" / "d5_recall_v2.py"
_SPEC = importlib.util.spec_from_file_location("recall_allsets_frozen_d5_allocated_r2", _FROZEN)
BASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BASE)

VERSION = "d5-evidence-only-known-layout-recall-v3"
CONFIG = {
    "base_geometry": BASE.VERSION,
    "inherited_candidate_score_enabled": False,
    "unknown_pixels": "retain_as_blocked_no_negative_imputation",
    "partial_concentration": "cap_by_independently_proven_known_empty_rectangle",
    "png_statistics": "not_used_for_threshold_or_semantic_relabelling",
    "threshold_selection": "external_one_common_reused_development_threshold",
    "score_kind": "ordinal_geometry_not_probability",
    "base_geometry_config": deepcopy(BASE.CONFIG),
}


def propose(ctx, previous_result=None):
    """Return geometry evidence without inheriting a historical alarm bit.

    ``previous_result`` is retained only as trace metadata; changing a previous
    decision or score cannot change current scores, masks, or evidence. Inputs
    are read-only and no filesystem image is read inside this detector.
    """
    result = BASE.propose(ctx, previous_result=None)
    result["version"] = VERSION
    features = result.setdefault("features", {})
    features["evidence_only_config"] = deepcopy(CONFIG)
    features["previous_decision_trace"] = {
        "status": (previous_result or {}).get("status"),
        "score": (previous_result or {}).get("score"),
        "used_as_score_or_evidence": False,
    }
    components = result.get("score_components")
    if components is not None:
        removed = components.pop("retained_previous_candidate", 0.0)
        if removed != 0.0:
            raise AssertionError("Historical candidate unexpectedly entered evidence-only scoring")
        # Concentration of *known* body objects is not concentration of the
        # entire page when another native object is semantically unresolved.
        # That unresolved image may be precisely the missing right-hand body.
        # Retain independently demonstrated void evidence, but do not inflate
        # it using the centroid/bounds of a partial known subset.
        concentration_key = "compact_displaced_allocated_body"
        raw_concentration = components.get(concentration_key, 0.0)
        if features.get("unknown_area_fraction", 0.0) > 0:
            components[concentration_key] = min(
                raw_concentration,
                components.get("known_empty_rectangle_outside_allocated_modules", 0.0),
            )
        features["partial_concentration_audit"] = {
            "raw_known_subset_concentration": raw_concentration,
            "supported_concentration_component": components.get(concentration_key, 0.0),
            "unresolved_area_present": features.get("unknown_area_fraction", 0.0) > 0,
            "unknown_region_relabelled": False,
        }
        result["score"] = float(max(components.values(), default=0.0))
        # Base status is attached at its historical default threshold. Recheck
        # that same default after removing unsupported score contributions;
        # the externally calibrated operating threshold is applied by caller.
        default = BASE.R1.CONFIG["default_threshold"]
        uncertain = features.get("unknown_area_fraction", 0.0) > 0 or bool(
            features.get("missing_content_region_object_ids")
        )
        result["status"] = (
            "candidate" if result["score"] >= default else
            "unknown" if uncertain else "no_candidate_in_available_scope"
        )
        result["review_required"] = result["status"] in {"candidate", "unknown"}
        if result["status"] != "candidate":
            features["triggered_rules"] = []
        features["score_components"] = deepcopy(components)
        for evidence in result.get("evidence", []):
            metrics = evidence.get("metrics", {})
            if "score_components" in metrics:
                metrics["score_components"] = deepcopy(components)
        features["scoring_contract"] = (
            "max(current_measured_known_void_outside_allocated_modules,"
            "current_supported_compact_displacement,current_local_container_underfill,"
            "current_sparse_anchored_rows); no prior candidate-bit floor"
        )
    result.setdefault("limitations", []).append(
        "All reused H150/G150 pages are development diagnostics. Zero observed "
        "misses at a calibrated point does not guarantee zero future misses; "
        "unresolved native regions remain explicitly unassessed."
    )
    return result


detect = propose
