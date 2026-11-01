"""Pure evaluation functions for the preregistered independent PPTX assessment.

This module opens no datasets or model files and never fits or predicts a model.
All truth/probability arrays must be supplied by the caller.  Four labels, in
LABELS order, are mandatory.  Undefined metrics are Python ``None`` in point
results and NaN with reason bit flags in bootstrap arrays.

Public API
----------
evaluate(y, probabilities, thresholds, mask=None, weights=None)
    One model/workpoint/stratum. ``weights`` are optional nonnegative row
    multiplicities; normal evaluation uses unit weights.
evaluate_strata(y, probabilities_by_model, thresholds_by_model, strata)
    Returns a list of {stratum, model, mode, metrics} records. Thresholds have
    the structure {model: {mode: four_thresholds}}; strata are {name: bool_mask}.
paired_cluster_bootstrap(y, probabilities_by_model, thresholds_by_model,
                         strata, cluster_ids, B=2000, seed=42, ...)
    Uses one globally shared, sorted-cluster draw matrix for every model,
    workpoint, contrast and stratum. Returns JSON-serializable ``summary`` rows,
    ``draw_counts`` (B by K), sorted ``cluster_ids``, ``reason_definitions`` and
    ``replicates``. Each replicate entry contains a length-B float64 ``values``
    array and uint8 ``reason_codes``. Save arrays with np.savez_compressed,
    retaining summary.replicate_key, to preserve every invalid repeat without
    an unnecessarily large JSON. A callback(summary, values, reasons) can
    stream entries; set keep_replicates=False to avoid retaining them in RAM.

AP is mathematically the weighted, tie-aware sklearn average_precision_score,
computed in batches to avoid thousands of Python/sklearn calls. No bootstrap
draw is replaced when it loses a class. Source-family and document sensitivity
analyses call the same function separately with their respective cluster IDs.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable, Mapping, Sequence

import numpy as np


LABELS = (
    "improper_font_sizing",
    "occluded_content",
    "cluttered_layout",
    "content_overflow_cutoff",
)
N_LABELS = 4

# Bit masks preserve several simultaneous causes, including missing classes in
# different labels of a fixed four-label macro. Per-label arrays retain detail.
REASON_BITS = {
    1: "no_positive_support",
    2: "no_negative_support",
    4: "no_predicted_positive",
    8: "empty_stratum",
    16: "no_all_negative_pages",
    32: "no_defect_pages",
    64: "constituent_metric_undefined",
}


def decode_reason(code: int) -> list[str]:
    return [name for bit, name in REASON_BITS.items() if int(code) & bit]


def validate_feature_matrix(features, n_expected: int, dimension: int) -> np.ndarray:
    """Validate caller-supplied features without reading or generating them."""
    values = np.asarray(features)
    if values.shape != (n_expected, dimension):
        raise ValueError(f"Feature shape {values.shape} != {(n_expected, dimension)}")
    if not np.issubdtype(values.dtype, np.number) or not np.isfinite(values).all():
        raise ValueError("Features must be numeric and finite")
    if np.any(np.linalg.norm(values.astype(np.float64), axis=1) == 0):
        raise ValueError("Zero feature vector")
    return values


def positive_class_probability(probabilities, classes) -> np.ndarray:
    """Extract class=1 by its explicit class identity, never column position."""
    values = np.asarray(probabilities, dtype=np.float64)
    class_values = np.asarray(classes)
    if class_values.ndim != 1 or values.ndim != 2:
        raise ValueError("Expected 2-D probabilities and 1-D classes")
    if values.shape[1] != len(class_values):
        raise ValueError("Probability columns do not match classes")
    positive = np.flatnonzero(class_values == 1)
    if len(positive) != 1 or len(np.unique(class_values)) != len(class_values):
        raise ValueError("Classes must identify class=1 exactly once")
    if not np.isfinite(values).all() or np.any((values < 0) | (values > 1)):
        raise ValueError("Probabilities must be finite in [0,1]")
    return values[:, int(positive[0])].copy()


def _validate(y, probabilities, thresholds=None):
    truth = np.asarray(y)
    probability = np.asarray(probabilities, dtype=np.float64)
    if truth.ndim != 2 or truth.shape[1] != N_LABELS:
        raise ValueError("Truth must have shape (n,4)")
    if probability.shape != truth.shape:
        raise ValueError("Probability shape must match truth (n,4)")
    if not np.isin(truth, [0, 1]).all():
        raise ValueError("Truth contains missing/unknown/illegal values")
    if not np.isfinite(probability).all() or np.any((probability < 0) | (probability > 1)):
        raise ValueError("Probabilities must be finite in [0,1]")
    if thresholds is None:
        return truth.astype(np.int8), probability
    threshold = np.asarray(thresholds, dtype=np.float64)
    if threshold.shape != (N_LABELS,) or not np.isfinite(threshold).all():
        raise ValueError("Exactly four finite thresholds are required")
    if np.any((threshold < 0) | (threshold > 1)):
        raise ValueError("Thresholds must be in [0,1]")
    return truth.astype(np.int8), probability, threshold


def _mask_weights(n: int, mask=None, weights=None):
    selected = np.ones(n, dtype=bool) if mask is None else np.asarray(mask)
    if selected.shape != (n,) or selected.dtype.kind != "b":
        raise ValueError("Stratum mask must be a boolean vector of length n")
    weight = np.ones(n, dtype=np.float64) if weights is None else np.asarray(weights, dtype=np.float64)
    if weight.shape != (n,) or not np.isfinite(weight).all() or np.any(weight < 0):
        raise ValueError("Weights must be finite nonnegative row multiplicities")
    return selected, weight * selected


def _ap_point(y: np.ndarray, p: np.ndarray, weight: np.ndarray) -> float:
    order = np.argsort(-p, kind="mergesort")
    ys, ps, ws = y[order], p[order], weight[order]
    ends = np.r_[np.flatnonzero(ps[:-1] != ps[1:]), len(ps) - 1]
    cumulative_positive = np.cumsum(ws * ys)[ends]
    cumulative_all = np.cumsum(ws)[ends]
    increments = np.diff(np.r_[0.0, cumulative_positive])
    precision = np.divide(cumulative_positive, cumulative_all,
                          out=np.zeros_like(cumulative_positive), where=cumulative_all > 0)
    return float(np.sum(increments * precision) / cumulative_positive[-1])


def _finite_or_none(value):
    return float(value) if np.isfinite(value) else None


def _fixed_mean(values):
    return float(np.mean(values)) if all(value is not None for value in values) else None


def evaluate(y, probabilities, thresholds, mask=None, weights=None) -> dict:
    """Apply all class-support and denominator rules from protocol section 5."""
    truth, probability, threshold = _validate(y, probabilities, thresholds)
    _, weight = _mask_weights(len(truth), mask, weights)
    active = weight > 0
    prediction = probability >= threshold
    n_pages = float(weight.sum())
    labels = []
    for j, label in enumerate(LABELS):
        positive = truth[:, j].astype(bool)
        alert = prediction[:, j]
        tp = float(weight[positive & alert].sum())
        fp = float(weight[~positive & alert].sum())
        tn = float(weight[~positive & ~alert].sum())
        fn = float(weight[positive & ~alert].sum())
        n_pos, n_neg, pp = tp + fn, fp + tn, tp + fp
        class_reason = (1 if n_pos == 0 else 0) | (2 if n_neg == 0 else 0)
        if n_pages == 0:
            class_reason |= 8
        ap = _ap_point(truth[active, j], probability[active, j], weight[active]) if not class_reason else None
        precision = tp / pp if not class_reason and pp else None
        recall = tp / n_pos if not class_reason else None
        f1 = 2 * tp / (2 * tp + fp + fn) if not class_reason else None
        fpr = fp / n_neg if n_neg else None
        codes = {"ap": class_reason, "precision": class_reason or (4 if pp == 0 else 0),
                 "recall": class_reason, "f1": class_reason,
                 "fpr": (2 | (8 if n_pages == 0 else 0)) if n_neg == 0 else 0}
        labels.append({"label": label, "n_pos": n_pos, "n_neg": n_neg,
                       "predicted_positive": pp, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
                       "ap": ap, "precision": precision, "recall": recall, "f1": f1, "fpr": fpr,
                       "undefined_reasons": {key: decode_reason(code) for key, code in codes.items() if code}})
    macro, available = {}, {}
    for metric in ("ap", "precision", "recall", "f1"):
        values = [row[metric] for row in labels]
        macro[metric] = _fixed_mean(values)
        usable = [j for j, value in enumerate(values) if value is not None]
        available[metric] = {"value": float(np.mean([values[j] for j in usable])) if usable else None,
                             "labels": [LABELS[j] for j in usable], "k": len(usable), "of": N_LABELS}
    counts = {key: float(sum(row[key] for row in labels)) for key in ("tp", "fp", "tn", "fn")}
    all_supported = all(row["n_pos"] > 0 and row["n_neg"] > 0 for row in labels)
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    micro = {**counts, "class_support_valid": all_supported,
             "precision": tp / (tp + fp) if all_supported and tp + fp else None,
             "recall": tp / (tp + fn) if all_supported else None,
             "f1": 2 * tp / (2 * tp + fp + fn) if all_supported else None}
    any_alert = prediction.any(axis=1)
    all_negative = ~truth.any(axis=1)
    defective = ~all_negative
    correct_hit = (prediction & truth.astype(bool)).any(axis=1)
    n_all_negative, n_defective = float(weight[all_negative].sum()), float(weight[defective].sum())
    n_triggered = float(weight[any_alert].sum())
    n_false_alarm_pages = float(weight[all_negative & any_alert].sum())
    n_correct_hit_pages = float(weight[defective & correct_hit].sum())
    alerts = float(np.dot(weight, prediction.sum(axis=1)))
    page = {"n_pages": n_pages, "n_triggered": n_triggered, "n_all_negative": n_all_negative,
            "n_defective": n_defective, "n_false_alarm_pages": n_false_alarm_pages,
            "n_correct_hit_pages": n_correct_hit_pages, "n_alerts": alerts,
            "review_trigger_rate": n_triggered / n_pages if n_pages else None,
            "all_negative_page_fpr": n_false_alarm_pages / n_all_negative if n_all_negative else None,
            "defect_page_hit_rate": n_correct_hit_pages / n_defective if n_defective else None,
            "mean_alerts_per_page": alerts / n_pages if n_pages else None}
    return {"labels": labels, "macro": macro, "macro_available": available, "micro": micro, "page": page}


def _validate_collection(y, probabilities_by_model, thresholds_by_model, strata):
    if not probabilities_by_model or set(probabilities_by_model) != set(thresholds_by_model):
        raise ValueError("Probability and threshold model names must match and be nonempty")
    truth = None
    for model, probabilities in probabilities_by_model.items():
        truth, _ = _validate(y, probabilities)
        if not thresholds_by_model[model]:
            raise ValueError("Every model requires at least one frozen workpoint")
        for threshold in thresholds_by_model[model].values():
            _validate(y, probabilities, threshold)
    if not strata:
        raise ValueError("At least one explicit stratum mask is required")
    for mask in strata.values():
        _mask_weights(len(truth), mask)
    return truth


def evaluate_strata(y, probabilities_by_model, thresholds_by_model, strata, weights=None) -> list[dict]:
    _validate_collection(y, probabilities_by_model, thresholds_by_model, strata)
    return [{"stratum": name, "model": model, "mode": mode,
             "metrics": evaluate(y, probabilities, threshold, mask=mask, weights=weights)}
            for name, mask in strata.items()
            for model, probabilities in probabilities_by_model.items()
            for mode, threshold in thresholds_by_model[model].items()]


def _divide(numerator, denominator, invalid_code=0):
    result = np.divide(numerator, denominator, out=np.full_like(numerator, np.nan, dtype=np.float64),
                       where=denominator > 0)
    reason = np.where(denominator > 0, 0, invalid_code).astype(np.uint8)
    return result, reason


def _apply_class_support(values, reasons, class_reasons):
    values = values.copy()
    values[class_reasons != 0] = np.nan
    return values, np.bitwise_or(reasons, class_reasons)


def _macro_series(series):
    values = np.stack([item[0] for item in series], axis=1)
    reasons = np.bitwise_or.reduce(np.stack([item[1] for item in series], axis=1), axis=1)
    reasons = np.where(reasons != 0, reasons | 64, 0).astype(np.uint8)
    return np.mean(values, axis=1), reasons


def _batched_metrics(truth, probabilities, modes, mask, cluster_index, draw_counts, base_weights):
    """Return (mode,label,metric)->(values,reasons); every vector has B+1 rows.

    The first draw uses one copy of every cluster and is the exact point value.
    Subsequent rows are the pre-drawn bootstrap repetitions, never new draws.
    """
    k = draw_counts.shape[1]
    base = base_weights * mask

    def aggregate(vectors):
        vectors = np.asarray(vectors, dtype=np.float64)
        one_dim = vectors.ndim == 1
        if one_dim:
            vectors = vectors[:, None]
        cluster_totals = np.zeros((k, vectors.shape[1]), dtype=np.float64)
        np.add.at(cluster_totals, cluster_index, vectors * base[:, None])
        result = draw_counts @ cluster_totals
        return result[:, 0] if one_dim else result

    n_pages = aggregate(np.ones(len(truth)))
    n_pos = aggregate(truth)
    n_neg = n_pages[:, None] - n_pos
    class_reasons = ((n_pos == 0).astype(np.uint8) | ((n_neg == 0).astype(np.uint8) * 2)
                     | ((n_pages == 0).astype(np.uint8)[:, None] * 8))
    result = {}
    ap_series = []
    active = base > 0
    for j, label in enumerate(LABELS):
        order = np.flatnonzero(active)[np.argsort(-probabilities[active, j], kind="mergesort")]
        if len(order):
            ps = probabilities[order, j]
            groups = np.r_[0, np.cumsum(ps[1:] != ps[:-1])]
            group_count = int(groups[-1]) + 1
            totals = np.zeros((group_count, k), dtype=np.float64)
            positives = np.zeros_like(totals)
            np.add.at(totals, (groups, cluster_index[order]), base[order])
            np.add.at(positives, (groups, cluster_index[order]), base[order] * truth[order, j])
            group_weights = draw_counts @ totals.T
            group_positives = draw_counts @ positives.T
            cumulative_all = np.cumsum(group_weights, axis=1)
            cumulative_positive = np.cumsum(group_positives, axis=1)
            precision = np.divide(cumulative_positive, cumulative_all, out=np.zeros_like(cumulative_all),
                                  where=cumulative_all > 0)
            ap = np.divide(np.sum(precision * group_positives, axis=1), n_pos[:, j],
                           out=np.full(len(draw_counts), np.nan), where=n_pos[:, j] > 0)
        else:
            ap = np.full(len(draw_counts), np.nan)
        ap[class_reasons[:, j] != 0] = np.nan
        series = (ap, class_reasons[:, j])
        result[("shared", label, "ap")] = series
        ap_series.append(series)
    result[("shared", "macro", "ap")] = _macro_series(ap_series)
    all_negative = ~truth.any(axis=1)
    n_all_negative = aggregate(all_negative)
    n_defective = n_pages - n_all_negative
    for mode, threshold in modes.items():
        alert = probabilities >= np.asarray(threshold)
        tp = aggregate(truth * alert)
        fp = aggregate((1 - truth) * alert)
        fn, tn = n_pos - tp, n_neg - fp
        label_series = {"precision": [], "recall": [], "f1": []}
        for j, label in enumerate(LABELS):
            precision = _apply_class_support(*_divide(tp[:, j], tp[:, j] + fp[:, j], 4), class_reasons[:, j])
            recall = _apply_class_support(*_divide(tp[:, j], n_pos[:, j], 1), class_reasons[:, j])
            f1 = _apply_class_support(*_divide(2 * tp[:, j], 2 * tp[:, j] + fp[:, j] + fn[:, j], 1), class_reasons[:, j])
            fpr = _divide(fp[:, j], n_neg[:, j], 2)
            fpr = (fpr[0], fpr[1] | ((n_pages == 0).astype(np.uint8) * 8))
            for metric, series in (("precision", precision), ("recall", recall), ("f1", f1), ("fpr", fpr)):
                result[(mode, label, metric)] = series
                if metric in label_series:
                    label_series[metric].append(series)
        for metric in label_series:
            result[(mode, "macro", metric)] = _macro_series(label_series[metric])
        micro_class_reason = np.bitwise_or.reduce(class_reasons, axis=1)
        micro_class_reason = np.where(micro_class_reason != 0, micro_class_reason | 64, 0).astype(np.uint8)
        total_tp, total_fp, total_fn = tp.sum(axis=1), fp.sum(axis=1), fn.sum(axis=1)
        for metric, numerator, denominator, code in (
                ("precision", total_tp, total_tp + total_fp, 4),
                ("recall", total_tp, total_tp + total_fn, 1),
                ("f1", 2 * total_tp, 2 * total_tp + total_fp + total_fn, 1)):
            result[(mode, "micro", metric)] = _apply_class_support(
                *_divide(numerator, denominator, code), micro_class_reason)
        any_alert = alert.any(axis=1)
        correct_hit = (alert & truth.astype(bool)).any(axis=1)
        for metric, numerator, denominator, code in (
                ("review_trigger_rate", aggregate(any_alert), n_pages, 8),
                ("all_negative_page_fpr", aggregate(any_alert & all_negative), n_all_negative, 16),
                ("defect_page_hit_rate", aggregate(correct_hit), n_defective, 32),
                ("mean_alerts_per_page", aggregate(alert.sum(axis=1)), n_pages, 8)):
            result[(mode, "page", metric)] = _divide(numerator, denominator, code)
    return result


def paired_cluster_bootstrap(
    y, probabilities_by_model: Mapping[str, np.ndarray],
    thresholds_by_model: Mapping[str, Mapping[str, Sequence[float]]],
    strata: Mapping[str, np.ndarray], cluster_ids: Sequence[str],
    *, B: int = 2000, seed: int = 42,
    contrasts: Sequence[tuple[str, str]] | None = None,
    weights=None, keep_replicates: bool = True,
    callback: Callable[[dict, np.ndarray, np.ndarray], None] | None = None,
) -> dict:
    """Paired whole-cluster percentile bootstrap, including invalid repetitions.

    Clusters are drawn from the full fixed evaluation population and strata are
    intersected afterwards, as preregistered. This is NOT a separate redraw for
    each stratum. All bootstrap repetitions remain in their original positions.
    ``contrasts`` contains (new, reference) model pairs and never chooses a model.
    """
    truth = _validate_collection(y, probabilities_by_model, thresholds_by_model, strata)
    if not isinstance(B, int) or B < 1:
        raise ValueError("B must be a positive integer")
    clusters = np.asarray(cluster_ids)
    if clusters.shape != (len(truth),) or not all(isinstance(x, (str, np.str_)) and x for x in clusters):
        raise ValueError("Cluster IDs must be nonempty strings, one per row")
    cluster_names, cluster_index = np.unique(clusters, return_inverse=True)
    if not len(cluster_names):
        raise ValueError("The global bootstrap population must be nonempty")
    _, base_weights = _mask_weights(len(truth), weights=weights)
    if base_weights.sum() <= 0:
        raise ValueError("At least one row must have positive weight")
    rng = np.random.default_rng(seed)
    chosen = rng.integers(0, len(cluster_names), size=(B, len(cluster_names)))
    draw_counts = np.zeros((B, len(cluster_names)), dtype=np.int32)
    np.add.at(draw_counts, (np.repeat(np.arange(B), len(cluster_names)), chosen.ravel()), 1)
    all_counts = np.vstack([np.ones((1, len(cluster_names)), dtype=np.int32), draw_counts])
    if contrasts is None:
        contrasts = [(new, reference) for new, reference in (("C", "A"), ("H", "A"), ("H", "C"), ("C9", "C"))
                     if new in probabilities_by_model and reference in probabilities_by_model]
    for new, reference in contrasts:
        if new not in probabilities_by_model or reference not in probabilities_by_model:
            raise ValueError("Contrast references a missing model")
    summaries, replicates = [], {}

    def save(stratum_name, model_name, key, series, n_clusters, is_contrast):
        mode, label, metric = key
        values, reasons = series
        values, reasons = np.asarray(values), np.asarray(reasons, dtype=np.uint8)
        if np.any(np.isfinite(values) != (reasons == 0)):
            raise ArithmeticError("Finite metric/reason-code mismatch")
        point = _finite_or_none(values[0])
        samples, failures = values[1:], reasons[1:]
        valid = failures == 0
        n_valid = int(valid.sum())
        if point is None:
            status = "undefined_point"
        elif n_clusters < 2:
            status = "insufficient_clusters"
        elif n_valid < 100:
            status = "insufficient_valid_replicates"
        elif n_valid / B < 0.80:
            status = "insufficient_valid_fraction"
        else:
            status = "exploratory_less_than_10_clusters" if n_clusters < 10 else "ok"
        ci = np.percentile(samples[valid], [2.5, 97.5]) if status in ("ok", "exploratory_less_than_10_clusters") else (None, None)
        reason_counts = Counter(int(code) for code in failures if code)
        replicate_key = f"endpoint_{len(summaries):06d}"
        row = {"stratum": stratum_name, "model": model_name, "contrast": bool(is_contrast),
               "mode": mode, "label": label, "metric": metric, "point": point,
               "ci_low": _finite_or_none(ci[0]) if ci[0] is not None else None,
               "ci_high": _finite_or_none(ci[1]) if ci[1] is not None else None,
               "n_clusters": n_clusters, "n_bootstrap": B, "n_valid": n_valid,
               "n_invalid": B - n_valid, "valid_fraction": n_valid / B,
               "ci_status": status, "exploratory": n_clusters < 10,
               "invalid_reason_counts": {str(code): {"count": count, "reasons": decode_reason(code)}
                                         for code, count in sorted(reason_counts.items())},
               "replicate_key": replicate_key}
        summaries.append(row)
        if keep_replicates:
            replicates[replicate_key] = {"values": samples.copy(), "reason_codes": failures.copy()}
        if callback is not None:
            callback(row, samples, failures)

    for stratum_name, raw_mask in strata.items():
        mask = np.asarray(raw_mask, dtype=bool)
        n_clusters = len(np.unique(clusters[mask & (base_weights > 0)]))
        model_series = {}
        for model, probability in probabilities_by_model.items():
            series = _batched_metrics(truth, np.asarray(probability), thresholds_by_model[model],
                                      mask, cluster_index, all_counts, base_weights)
            model_series[model] = series
            for key, values in series.items():
                save(stratum_name, model, key, values, n_clusters, False)
        for new, reference in contrasts:
            left, right = model_series[new], model_series[reference]
            # Only like-for-like fixed workpoints/metrics are paired.
            for key in sorted(left.keys() & right.keys()):
                values = left[key][0] - right[key][0]
                reasons = left[key][1] | right[key][1]
                reasons = np.where(reasons != 0, reasons | 64, 0).astype(np.uint8)
                save(stratum_name, f"{new}-{reference}", key, (values, reasons), n_clusters, True)
    return {"method": "paired_whole_cluster_percentile_bootstrap", "seed": seed, "B": B,
            "cluster_ids": cluster_names.tolist(), "draw_counts": draw_counts,
            "reason_definitions": {str(bit): name for bit, name in REASON_BITS.items()},
            "summary": summaries, "replicates": replicates,
            "notes": ["All models, workpoints and strata share the same global cluster draws.",
                      "Invalid repetitions are retained without redraw.",
                      "AP is shared across workpoints.",
                      "CIs require >=2 stratum clusters, >=100 valid draws, and >=80% valid draws."]}
