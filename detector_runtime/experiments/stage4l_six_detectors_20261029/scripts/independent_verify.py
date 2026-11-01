"""Independent 4.3L boundary/results checks; never calls COM, CLIP or a model.

Context and result validators are also used by integration fixtures. The final
cached-run verifier is deliberately separate from detector implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import numpy as np


EXP = Path(__file__).resolve().parents[1]
LABELS = {
    "D1": "improper_font_sizing", "D2": "occluded_content",
    "D3": "cluttered_layout", "D4": "content_overflow_cutoff",
    "D5": "excessive_whitespace", "D6": "generative_design_anomaly",
}
FORBIDDEN_INPUT_KEYS = {
    "response", "deficiency_type", "reference_label", "reference_labels",
    "human_label", "human_labels", "labels", "label", "annotation_status",
    "human_reviewed", "reviewer_id", "reviewed_at", "review_notes",
    "modification", "modification_type", "template", "template_id",
    "generator_reference", "expected_label", "expected_labels", "true_label",
    "y_true", "bounding_boxes", *LABELS.values(),
}
STATUSES = {"candidate", "no_candidate_in_available_scope", "unknown", "unsupported"}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value, path="root"):
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        raise ValueError(f"nonfinite numeric value at {path}")
    if isinstance(value, np.ndarray):
        if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
            raise ValueError(f"nonfinite array at {path}")
    elif isinstance(value, dict):
        for key, nested in value.items():
            _finite(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _finite(nested, f"{path}[{index}]")


def _forbidden(value, path="root"):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_INPUT_KEYS:
                raise ValueError(f"forbidden supervised/manifest field at {path}.{key}")
            _forbidden(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _forbidden(nested, f"{path}[{index}]")


def _nonempty_id(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")


def _bbox(box, field):
    if not isinstance(box, dict) or any(k not in box for k in ("x", "y", "w", "h")):
        raise ValueError(f"missing bbox at {field}")
    try:
        values = [float(box[k]) for k in ("x", "y", "w", "h")]
    except (TypeError, ValueError):
        raise ValueError(f"nonnumeric bbox at {field}") from None
    if not all(math.isfinite(v) for v in values) or values[2] < 0 or values[3] < 0:
        raise ValueError(f"invalid bbox at {field}")


def validate_contexts(contexts, require_spatial=True):
    """Fail closed before unsupported IDs, NaNs or supervision reach detectors."""
    contexts = list(contexts)
    sample_ids, document_pages = set(), set()
    object_total = paragraph_total = 0
    for context in contexts:
        _finite(context)
        _forbidden(context)
        sid = context.get("sample_id")
        _nonempty_id(sid, "sample_id")
        _nonempty_id(context.get("document_id"), f"{sid}.document_id")
        if sid in sample_ids:
            raise ValueError(f"duplicate sample_id {sid}")
        sample_ids.add(sid)
        page_index = context.get("page_index")
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 1:
            raise ValueError(f"invalid page_index {sid}")
        docpage = (context["document_id"], page_index)
        if docpage in document_pages:
            raise ValueError(f"duplicate document/page mapping {docpage}")
        document_pages.add(docpage)
        page = context.get("page", {})
        for dimension in ("width_pt", "height_pt"):
            value = page.get(dimension)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"invalid page dimension {sid}.{dimension}")
        ids, paragraphs = set(), set()
        for obj in context.get("objects", []):
            oid = obj.get("id")
            _nonempty_id(oid, f"{sid}.object.id")
            if oid in ids:
                raise ValueError(f"duplicate object_id {sid}/{oid}")
            ids.add(oid)
            _bbox(obj.get("bbox"), f"{sid}/{oid}")
            for font in obj.get("font_sizes_pt", []):
                if not isinstance(font, (float, int)) or isinstance(font, bool) or font <= 0:
                    raise ValueError(f"invalid font {sid}/{oid}")
            for paragraph in obj.get("paragraphs", []):
                pid = paragraph.get("id")
                _nonempty_id(pid, f"{sid}/{oid}.paragraph.id")
                if pid in paragraphs:
                    raise ValueError(f"duplicate paragraph_id {sid}/{pid}")
                paragraphs.add(pid)
                _bbox(paragraph.get("bbox"), f"{sid}/{pid}")
        object_total += len(ids)
        paragraph_total += len(paragraphs)
        if require_spatial:
            spatial = context.get("spatial", {})
            content, unknown = spatial.get("content_mask"), spatial.get("unknown_mask")
            for name, mask in (("content", content), ("unknown", unknown)):
                if not isinstance(mask, np.ndarray) or mask.dtype != np.bool_ or mask.ndim != 2 or mask.size == 0:
                    raise ValueError(f"invalid {name} bool mask {sid}")
            if content.shape != unknown.shape:
                raise ValueError(f"misaligned masks {sid}")
    return {"contexts": len(contexts), "objects": object_total, "paragraphs": paragraph_total,
            "unique_sample_ids": True, "finite": True, "supervised_fields_absent": True}


def validate_results(contexts, page_results):
    contexts = list(contexts)
    expected = {context["sample_id"] for context in contexts}
    if set(page_results) != expected:
        raise ValueError("result sample IDs differ from context IDs")
    checks = 0
    statuses = Counter()
    for context in contexts:
        sid = context["sample_id"]
        local_ids = {obj["id"] for obj in context.get("objects", [])}
        detectors = page_results[sid]
        if set(detectors) != set(LABELS):
            raise ValueError(f"missing or extra detector {sid}")
        for detector, result in detectors.items():
            _finite(result)
            if result.get("detector") != detector or result.get("label") != LABELS[detector]:
                raise ValueError(f"wrong detector/label mapping {sid}/{detector}")
            if result.get("status") not in STATUSES:
                raise ValueError(f"invalid status {sid}/{detector}")
            if result.get("coverage") not in {"full", "partial", "unsupported"}:
                raise ValueError(f"invalid coverage {sid}/{detector}")
            score = result.get("score")
            if score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1):
                raise ValueError(f"invalid ordinal score {sid}/{detector}")
            if result.get("score_kind") != "ordinal_heuristic_not_probability":
                raise ValueError(f"probability semantics falsely assigned {sid}/{detector}")
            if result["status"] == "unsupported" and score is not None:
                raise ValueError(f"unsupported scope given numeric score {sid}/{detector}")
            if result["status"] == "candidate" and not result.get("evidence"):
                raise ValueError(f"candidate lacks evidence {sid}/{detector}")
            for evidence in result.get("evidence", []):
                if not isinstance(evidence.get("object_ids"), list):
                    raise ValueError(f"missing evidence object scope {sid}/{detector}")
                if not set(evidence["object_ids"]) <= local_ids:
                    raise ValueError(f"evidence references foreign object {sid}/{detector}")
                if evidence.get("bbox") is not None:
                    _bbox(evidence["bbox"], f"{sid}/{detector}.evidence")
                if not evidence.get("measurement_source"):
                    raise ValueError(f"missing evidence provenance {sid}/{detector}")
            statuses[detector + ":" + result["status"]] += 1
            checks += 1
    return {"detector_results": checks, "statuses": dict(statuses), "finite": True,
            "evidence_ids_local": True, "ordinal_not_probability": True}


def binary_metrics(truth, prediction, scores):
    """Independent exact confusion/AP calculation (no sklearn metric imports)."""
    truth = np.asarray(truth, dtype=np.int64)
    prediction = np.asarray(prediction, dtype=np.int64)
    scores = np.asarray(scores, dtype=float)
    if truth.ndim != 1 or truth.shape != prediction.shape or truth.shape != scores.shape:
        raise ValueError("metric shapes differ")
    if not np.isin(truth, [0, 1]).all() or not np.isin(prediction, [0, 1]).all() or not np.isfinite(scores).all():
        raise ValueError("invalid metric values")
    tp = int(np.sum((truth == 1) & (prediction == 1)))
    fp = int(np.sum((truth == 0) & (prediction == 1)))
    tn = int(np.sum((truth == 0) & (prediction == 0)))
    fn = int(np.sum((truth == 1) & (prediction == 0)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp else (0.0 if truth.sum() else None)
    # AP groups equal scores exactly. A tie group earns its recall increment
    # times precision after the whole group, matching step-wise PR integration.
    ap = None
    positives = int(truth.sum())
    if positives:
        order = np.argsort(-scores, kind="stable")
        total_seen = cumulative_positive = 0
        ap = 0.0
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and scores[order[end]] == scores[order[start]]:
                end += 1
            group_positive = int(truth[order[start:end]].sum())
            cumulative_positive += group_positive
            total_seen += end - start
            ap += group_positive / positives * cumulative_positive / total_seen
            start = end
    return {"TP": tp, "FP": fp, "TN": tn, "FN": fn, "positives": positives,
            "negatives": int(len(truth) - positives), "precision": precision,
            "recall": recall, "f1": f1, "AP": ap}


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _number(value):
    if value in (None, "", "N/A", "null"):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite cached scalar")
    return number


def _binary(value):
    number = _number(value)
    if number is not None and number not in (0, 1):
        raise ValueError("invalid cached binary scalar")
    return None if number is None else int(number)


def load_cached_contexts(directory):
    directory = Path(directory)
    contexts = _json(directory / "context_features.json")
    with np.load(directory / "spatial_masks.npz", allow_pickle=False) as masks:
        ids = masks["sample_ids"].tolist()
        if len(ids) != len(set(ids)) or len(ids) != len(contexts):
            raise ValueError("mask sample IDs are duplicated or incomplete")
        for name in ("content", "unknown", "png"):
            if masks[name].dtype != np.bool_ or masks[name].shape != (len(contexts), 96, 128):
                raise ValueError("invalid cached mask shape or dtype")
        for context in contexts:
            index = context["spatial"]["mask_index"]
            if not isinstance(index, int) or ids[index] != context["sample_id"]:
                raise ValueError("context row and mask index/ID do not match")
            for key, target in (("content", "content_mask"), ("unknown", "unknown_mask"), ("png", "png_foreground_mask")):
                context["spatial"][target] = masks[key][index].copy()
    return contexts


def independent_largest_empty_area(occupied):
    """Independent O(H^2*W) row-band oracle, not the detector's stack code."""
    occupied = np.asarray(occupied, dtype=bool)
    height, width = occupied.shape
    best = 0
    for top in range(height):
        covered_columns = np.zeros(width, dtype=bool)
        for bottom in range(top, height):
            covered_columns |= occupied[bottom]
            separators = np.flatnonzero(np.concatenate(([True], covered_columns, [True])))
            longest_free_run = int(np.diff(separators).max() - 1)
            best = max(best, longest_free_run * (bottom - top + 1))
    return best


class EqualityAudit:
    def __init__(self):
        self.checks = 0
        self.max_abs_error = 0.0

    def equal(self, actual, expected, name):
        self.checks += 1
        if expected is None:
            if actual not in (None, "", "N/A", "null"):
                raise ValueError(f"{name}: expected N/A, found {actual}")
            return
        a, b = float(actual), float(expected)
        error = abs(a - b)
        self.max_abs_error = max(self.max_abs_error, error)
        if not math.isfinite(a) or error > 2e-12 * max(1.0, abs(b)):
            raise ValueError(f"{name}: {a} != {b}")


def verify_spatial_measurements(contexts, page_results, audit):
    for context in contexts:
        sid = context["sample_id"]
        content = context["spatial"]["content_mask"]
        unknown = context["spatial"]["unknown_mask"]
        if np.any(content & unknown):
            raise ValueError("known and unknown masks overlap " + sid)
        audit.equal(context["spatial"]["content_occupancy"], content.mean(), sid + ".shared_occupancy")
        audit.equal(context["spatial"]["unknown_fraction"], unknown.mean(), sid + ".shared_unknown")
        features = page_results[sid]["D5"]["features"]
        audit.equal(features["content_occupancy_lower"], content.mean(), sid + ".D5_occupancy_lower")
        audit.equal(features["content_occupancy_upper"], (content | unknown).mean(), sid + ".D5_occupancy_upper")
        audit.equal(features["known_semantic_empty_fraction"], 1 - (content | unknown).mean(), sid + ".D5_known_empty")
        expected_area = independent_largest_empty_area(content | unknown)
        empty = features["largest_known_empty_rectangle"]
        audit.equal(empty["area_cells"], expected_area, sid + ".D5_max_rectangle_cells")
        audit.equal(empty["area_ratio"], expected_area / content.size, sid + ".D5_max_rectangle_ratio")
        if empty["bbox"] is not None:
            box = empty["bbox"]
            height, width = content.shape
            left, top = round(box["x"] * width), round(box["y"] * height)
            right, bottom = round((box["x"] + box["w"]) * width), round((box["y"] + box["h"]) * height)
            if np.any((content | unknown)[top:bottom, left:right]):
                raise ValueError("declared empty region crosses known/unknown content " + sid)
            audit.equal((right - left) * (bottom - top), expected_area, sid + ".D5_rectangle_bbox_area")
        rows, columns = np.nonzero(content)
        if len(rows):
            centroid = [(float(columns.mean()) + .5) / content.shape[1], (float(rows.mean()) + .5) / content.shape[0]]
            for axis in (0, 1):
                audit.equal(features["content_centroid"][axis], centroid[axis], sid + ".D5_centroid")
        elif features["content_centroid"] is not None:
            raise ValueError("empty information mask has non-null centroid " + sid)
    return {"pages": len(contexts), "independent_rectangle_algorithm": "row_band_union_O(H^2W)",
            "all_maximum_rectangle_areas_verified": True}


def reference_metrics(rows):
    known = [r for r in rows if r["reference"] is not None]
    evaluable = [r for r in known if r["prediction"] is not None]
    measured = binary_metrics([r["reference"] for r in evaluable], [r["prediction"] for r in evaluable], [0] * len(evaluable))
    both = measured["positives"] > 0 and measured["negatives"] > 0
    score_known = [r for r in known if r["score"] is not None]
    ap = None
    if score_known and len(score_known) == len(known) and len({r["reference"] for r in known}) == 2:
        ap = binary_metrics([r["reference"] for r in known], [0] * len(known), [r["score"] for r in known])["AP"]
    return {
        "pages": len(rows), "reference_known": len(known),
        "reference_positive": sum(r["reference"] for r in known),
        "reference_negative": sum(r["reference"] == 0 for r in known),
        "evaluable_pages": len(evaluable), "unresolved_reference_pages": len(known) - len(evaluable),
        "evaluable_positive": measured["positives"], "evaluable_negative": measured["negatives"],
        "alert_pages": sum(r["prediction"] == 1 for r in rows),
        **{key: measured[key] if known else None for key in ("TP", "FP", "TN", "FN")},
        "reference_precision": measured["precision"] if both else None,
        "reference_recall": measured["recall"] if both else None,
        "reference_f1": measured["f1"] if both else None,
        "reference_AP": ap,
        "candidate_per_100_pages": 100 * sum(r["prediction"] == 1 for r in rows) / len(rows) if rows else None,
    }


def verify_comparison_and_metrics(directory, contexts, page_results, audit):
    rows = _read_csv(directory / "comparison_predictions.csv")
    inventory = _read_csv(EXP.parents[1] / "benchmark_constructed_20261029" / "manifest_all.csv")
    references = {r["sample_id"]: r for r in inventory}
    by_id = {c["sample_id"]: c for c in contexts}
    historical_path = EXP.parent / "stage4k_constructed_diagnostics_20261029" / "outputs" / "reference_predictions.csv"
    historical_rows = [r for r in _read_csv(historical_path) if r["mode"] in {"f1", "fixed_rule"}]
    historical = {(r["sample_id"], r["method"]): r for r in historical_rows}
    if len(historical) != len(historical_rows):
        raise ValueError("ambiguous historical baseline workpoint")
    if set(references) != set(by_id):
        raise ValueError("cached model inputs differ from evaluation inventory")
    keys = set()
    first_four = list(LABELS.values())[:4]
    for row in rows:
        key = row["sample_id"], row["method"], row["label"]
        if key in keys:
            raise ValueError("duplicated cached comparison row")
        keys.add(key)
        sid = row["sample_id"]
        if sid not in by_id or row["document_id"] != by_id[sid]["document_id"] or row["page_role"] != by_id[sid]["page"]["role"]:
            raise ValueError("prediction/context document or role mapping differs")
        if _binary(row["human_label"]) is not None:
            raise ValueError("constructed reference promoted to human truth")
        for name in ("reference", "prediction"):
            row[name] = _binary(row[name])
        row["score"] = _number(row["score"])
        if row["label"] in first_four:
            expected_reference = _binary(references[sid].get(row["label"]))
            audit.equal(row["reference"], expected_reference, str(key) + ".reference")
        elif row["reference"] is not None:
            raise ValueError("D5/D6 missing truth filled with a label")
        if row["method"] == "L_specialized":
            detector = next(key for key, label in LABELS.items() if label == row["label"])
            source = page_results[sid][detector]
            if row["status"] != source["status"] or row["coverage"] != source["coverage"]:
                raise ValueError("CSV status/scope differs from cached detector output")
            audit.equal(row["score"], source["score"], str(key) + ".score")
            expected = int(source["status"] == "candidate") if source["status"] in {"candidate", "no_candidate_in_available_scope"} and source["score"] is not None else None
            audit.equal(row["prediction"], expected, str(key) + ".candidate")
        else:
            source = historical[(sid, row["method"])]
            audit.equal(row["prediction"], _binary(source["alert_" + row["label"]]), str(key) + ".historical_decision")
            expected_score = _number(source.get("visual_prob_" + row["label"])) if row["method"] in {"A", "C", "H", "C9"} else None
            audit.equal(row["score"], expected_score, str(key) + ".historical_score")
    label_metrics = _read_csv(directory / "reference_label_metrics.csv")
    summary = _read_csv(directory / "reference_summary_metrics.csv")
    for record in label_metrics:
        candidates = [r for r in rows if r["method"] == record["method"] and r["label"] == record["label"]
                      and (record["stratum_type"] == "all" or r[record["stratum_type"]] == record["stratum"])]
        expected = reference_metrics(candidates)
        for key, value in expected.items():
            audit.equal(_number(record[key]), value, str((record["stratum_type"], record["stratum"], record["method"], record["label"], key)))
    for record in summary:
        candidates = [r for r in rows if r["method"] == record["method"] and r["label"] in first_four
                      and (record["stratum_type"] == "all" or r[record["stratum_type"]] == record["stratum"])]
        pages = {}
        for row in candidates:
            pages.setdefault(row["sample_id"], {})[row["label"]] = row
        truth_complete = [p for p in pages.values() if len(p) == 4 and all(v["reference"] is not None for v in p.values())]
        pred_complete = [p for p in truth_complete if all(v["prediction"] is not None for v in p.values())]
        all0 = [p for p in truth_complete if all(v["reference"] == 0 for v in p.values())]
        positive = [p for p in truth_complete if any(v["reference"] == 1 for v in p.values())]
        zero_eligible = [p for p in all0 if all(v["prediction"] is not None for v in p.values())]
        positive_eligible = [p for p in positive if all(v["prediction"] is not None for v in p.values())]
        alerts = sum(any(v["prediction"] == 1 for v in p.values()) for p in pages.values())
        false_pages = sum(any(v["prediction"] == 1 for v in p.values()) for p in zero_eligible)
        hit_pages = sum(any(v["reference"] == 1 and v["prediction"] == 1 for v in p.values()) for p in positive_eligible)
        expected = {
            "pages": len(pages), "reference_positive_pages": len(positive), "reference_all0_pages": len(all0),
            "all_four_prediction_available_pages": len(pred_complete), "candidate_pages": alerts,
            "candidates_per_100_pages": 100 * alerts / len(pages) if pages else None,
            "reference_all0_evaluable_pages": len(zero_eligible), "reference_all0_alert_pages": false_pages,
            "reference_all0_alert_rate": false_pages / len(zero_eligible) if zero_eligible else None,
            "reference_defect_evaluable_pages": len(positive_eligible), "correct_label_hit_pages": hit_pages,
            "correct_label_page_hit_rate": hit_pages / len(positive_eligible) if positive_eligible else None,
        }
        # Full-inventory candidate events remain visible even when another
        # class is unresolved; these are not scored as complete predictions.
        full_inventory = {
            "candidate_correct_label_hit_all_reference_positive_pages": sum(
                any(v["reference"] == 1 and v["prediction"] == 1 for v in p.values()) for p in positive),
            "candidate_ref_all0_alert_pages_full_inventory": sum(
                any(v["prediction"] == 1 for v in p.values()) for p in all0),
            "reference_positive_pages_with_any_unknown": sum(
                any(v["prediction"] is None for v in p.values()) for p in positive),
            "reference_all0_pages_with_any_unknown": sum(
                any(v["prediction"] is None for v in p.values()) for p in all0),
        }
        expected.update({key: value for key, value in full_inventory.items() if key in record})
        per_label = {label: reference_metrics([r for r in candidates if r["label"] == label]) for label in first_four}
        for metric in ("reference_precision", "reference_recall", "reference_f1", "reference_AP"):
            values = [per_label[label][metric] for label in first_four]
            expected["macro4_" + metric] = sum(values) / 4 if len(pred_complete) == len(truth_complete) and all(v is not None for v in values) else None
        for key, value in expected.items():
            audit.equal(_number(record[key]), value, str((record["stratum_type"], record["stratum"], record["method"], key)))
    return {"comparison_rows": len(rows), "label_metric_rows": len(label_metrics), "summary_rows": len(summary),
            "unknown_preserved": True, "D5_D6_human_metrics_not_invented": True,
            "AP_algorithm": "independent_tie_grouped_precision_recall_sum_no_sklearn_metrics",
            "historical_baselines_match_saved_K_predictions": True,
            "historical_prediction_sha256": sha(historical_path)}


def verify_document_clusters(contexts, document_results):
    pages = {c["sample_id"]: c for c in contexts}
    count = 0
    for document in document_results:
        for cluster in document.get("layout_clusters", []):
            ids = cluster["sample_ids"]
            if len(ids) != len(set(ids)) or len(ids) != cluster["page_count"]:
                raise ValueError("invalid D6 cluster membership count")
            if any(pages[s]["document_id"] != cluster["document_id"] or pages[s]["page"]["role"] != cluster["role"] for s in ids):
                raise ValueError("D6 cluster crosses document or page role")
            positions = sorted({pages[s]["page_index"] for s in ids})
            longest = current = 0
            previous = None
            for position in positions:
                current = current + 1 if previous is not None and position == previous + 1 else 1
                longest = max(longest, current)
                previous = position
            if longest != cluster["longest_consecutive_run"]:
                raise ValueError("incorrect D6 consecutive run")
            if cluster["anomaly_candidate"] and not (cluster["repetition_detected"] and cluster["redundancy_page_ids"]):
                raise ValueError("template repetition alone promoted to anomaly")
            count += 1
    return {"documents": len(document_results), "clusters": count, "document_role_boundaries_respected": True}


def verify_coordinate_guard(directory, contexts, page_results, audit):
    registration = directory / "correction_registration.json"
    if not registration.exists():
        return {"status": "not_applicable_initial_run", "claim_boundary": "Arithmetic checks do not establish COM coordinate semantics."}
    registration = _json(registration)
    original_root = EXP / "results"
    if sha(original_root / "page_results.json") != registration["first_run_page_results_sha256"]:
        raise ValueError("original prediction version was overwritten")
    live = _json(original_root / "cell_coordinate_audit.json")
    if sha(original_root / "cell_coordinate_audit.json") != registration["live_coordinate_audit_sha256"]:
        raise ValueError("live coordinate evidence changed")
    if not live["source_sha_unchanged"] or live["source_sha256_before"] != live["source_sha256_after"]:
        raise ValueError("coordinate check modified source PPTX")
    verified = {(live["sample_id"], cell["object_id"]): cell for cell in live["cells"] if cell["status"] == "measured"}
    initial_contexts = {c["sample_id"]: c for c in _json(original_root / "context_features.json")}
    measured_cells = deferred_cells = 0
    for context in contexts:
        sid = context["sample_id"]
        initial_objects = {o["id"]: o for o in initial_contexts[sid]["objects"]}
        for obj in context["objects"]:
            if obj["kind"] != "cell":
                continue
            key = sid, obj["id"]
            if obj["font_sizes_pt"] != initial_objects[obj["id"]]["font_sizes_pt"]:
                raise ValueError("coordinate repair silently changed effective font observations")
            if key in verified:
                measured_cells += 1
                if len(obj["paragraphs"]) != 1:
                    raise ValueError("unexpected live-verified paragraph count")
                proof = verified[key]
                point_box = proof["paragraph_translated_by_cell_origin_bbox_pt"]
                expected = {"x": point_box["x"] / live["page_width_pt"],
                            "y": point_box["y"] / live["page_height_pt"],
                            "w": point_box["w"] / live["page_width_pt"],
                            "h": point_box["h"] / live["page_height_pt"]}
                for axis in ("x", "y", "w", "h"):
                    audit.equal(obj["paragraphs"][0]["bbox"][axis], expected[axis], sid + ".live_cell_translation." + axis)
                if "live_verified" not in obj["paragraphs"][0].get("coordinate_space", ""):
                    raise ValueError("translated cell lacks explicit live-coordinate provenance")
            else:
                deferred_cells += 1
                if obj["paragraphs"] or not obj.get("unresolved_paragraphs"):
                    raise ValueError("unverified local cell geometry remains usable or was discarded")
                if obj.get("available", {}).get("page_text_geometry") != "unknown":
                    raise ValueError("unverified cell geometry silently marked available")
                for detector in ("D2", "D4"):
                    for evidence in page_results[sid][detector]["evidence"]:
                        if obj["id"] in evidence["object_ids"]:
                            raise ValueError("unverified local cell used as page geometry evidence")
    if measured_cells != len(verified) or measured_cells != 2 or deferred_cells != 162:
        raise ValueError("coordinate guard coverage differs from registered two-cell evidence")
    first_run = _json(original_root / "run_metadata.json")
    for path, original_digest in first_run["config_sha256"].items():
        if sha(EXP / path) != original_digest:
            raise ValueError("engineering threshold/config changed after first 130-page run")
    return {"status": "passed", "live_verified_cells": measured_cells, "unverified_cells_deferred": deferred_cells,
            "local_boxes_and_font_observations_preserved": True, "all_initial_configs_unchanged": True,
            "initial_page_results_preserved": True, "no_human_coordinate_or_defect_truth_claim": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-fixture-only", action="store_true")
    parser.add_argument("--results", type=Path, default=EXP / "results")
    args = parser.parse_args()
    if args.check_fixture_only:
        print(json.dumps({"status": "fixture_helpers_available", "no_model_calls": True}))
        return
    directory = args.results.resolve()
    if not directory.is_relative_to((EXP / "results").resolve()):
        raise ValueError("Results must belong to this isolated experiment; no other benchmark is accepted")
    contexts = load_cached_contexts(directory)
    context_check = validate_contexts(contexts)
    pages = _json(directory / "page_results.json")
    mapping = {page["sample_id"]: page["detectors"] for page in pages}
    if len(pages) != len(mapping) or len(pages) != 130 or len({c["document_id"] for c in contexts}) != 11:
        raise ValueError("incomplete/duplicated formal 130-page diagnostic output")
    result_check = validate_results(contexts, mapping)
    audit = EqualityAudit()
    spatial = verify_spatial_measurements(contexts, mapping, audit)
    metrics = verify_comparison_and_metrics(directory, contexts, mapping, audit)
    clusters = verify_document_clusters(contexts, _json(directory / "document_results.json"))
    coordinate_guard = verify_coordinate_guard(directory, contexts, mapping, audit)
    extraction_path = directory / "context_extraction.json"
    if not extraction_path.exists():
        extraction_path = EXP / "results" / "context_extraction.json"
    extraction = _json(extraction_path)
    execution = _json(directory / "run_metadata.json")
    if extraction["PPTX_parse_calls"] != 11 or extraction["COM_cache_pages_reused"] != 130:
        raise ValueError("shared parse/cache reuse differs from recorded scope")
    for field in ("new_classifier_fit_calls", "new_CLIP_forwards", "new_frozen_visual_predictions", "new_supervised_fits"):
        if execution.get(field, 0) != 0:
            raise ValueError("unexpected fit/model-forward call in diagnostic run")
    core_files = ["context_features.json", "spatial_masks.npz", "page_results.json", "document_results.json",
                  "comparison_predictions.csv", "reference_label_metrics.csv", "reference_summary_metrics.csv",
                  "run_metadata.json"]
    record = {
        "status": "passed", "completed_at": datetime.now().astimezone().isoformat(),
        "scope": "independent cached arithmetic/geometry/provenance audit, not new defect ground truth",
        "context_contract": context_check, "result_contract": result_check,
        "spatial_measurements": spatial, "reference_metrics": metrics, "document_clusters": clusters,
        "coordinate_guard": coordinate_guard,
        "numeric_checks": audit.checks, "maximum_absolute_numeric_error": audit.max_abs_error,
        "new_COM_calls": 0, "new_CLIP_calls": 0, "new_model_predict_calls": 0,
        "input_sha256": {name: sha(directory / name) for name in core_files},
        "context_extraction_sha256": sha(extraction_path),
        "audit_script_sha256": sha(Path(__file__)),
        "known_test_oracle_correction": "Initial integration fixture allowed only unknown for failed native measurements; expanded to unknown/unsupported, both non-negative. No detector threshold was changed.",
    }
    (directory / "independent_verification.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: record[key] for key in ("status", "numeric_checks", "maximum_absolute_numeric_error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
