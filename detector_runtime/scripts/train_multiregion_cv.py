#!/usr/bin/env python3
"""4.3E: fixed, train-only grouped CV for three CLIP representations.

Only train/validation rows of the frozen features are accessed. All model,
representation and threshold choices are frozen from train OOF predictions
before the single exploratory validation comparison. Nothing opens test image,
test annotation, saved classifier or old test-result paths. No model is saved.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from threadpoolctl import threadpool_limits

# This input helper explicitly selects train/validation feature rows before
# copying any values and installs an audit guard against test image/label I/O.
from evaluate_full_slide import (METADATA_PATH, load_inputs, manifest_sha,
                                 require_nested_close)
from train_clip_lr import (BASE, CLASS_WEIGHTS, CS, LABELS, MODEL_HASHES, MODEL_ID,
                           REVISION, SEED, SLUGS, binary_metrics,
                           choose_threshold, now, read_csv, require, sha256,
                           summary_metrics)

FEATURE_PATH = BASE / "features/multiregion_embeddings_trainval.npy"
INDEX_PATH = BASE / "features/multiregion_feature_index.csv"
REPORT_PATH = BASE / "reports/multiregion_cv_report.md"
PREDICTION_PATH = BASE / "reports/multiregion_validation_predictions.csv"
NAMES = {"A": "原中心裁剪", "B": "left+center+right", "C": "left+center+right+完整页补边"}
DIMENSIONS = {"A": 512, "B": 1536, "C": 2048}
PROTOCOL = {
    "seed": SEED, "folds": 3, "splitter": "GroupKFold(shuffle=True, random_state=42)",
    "C": list(CS), "class_weight": list(CLASS_WEIGHTS),
    "solver": "lbfgs", "max_iter": 3000, "tol": 1e-6,
    "normalization": "float64 L2 per 512-d block, concatenate, divide by sqrt(block_count)",
    "configuration_selection": "max pooled train OOF Average Precision per label; ties: lower C, None before balanced",
    "representation_selection": "max four-label Macro OOF AP; ties: lower dimension A before B before C",
    "threshold_selection": "train OOF F1 max on 0.05..0.95 step 0.01; ties: closest 0.5, then larger threshold",
    "validation_rule": "after every choice is frozen, fit A and selected representation on all train; predict each validation row once per classifier",
    "test_rule": "never access test image, label, or feature values",
}


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def array_digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes(order="C")).hexdigest()


def normalize_blocks(*blocks):
    normalized = []
    for block in blocks:
        values = np.array(block, dtype=np.float64, copy=True)
        require(values.ndim == 2 and values.shape[1] == 512, "Expected a 512-dimensional block")
        norm = np.linalg.norm(values, axis=1, keepdims=True)
        require(np.isfinite(values).all() and np.isfinite(norm).all() and (norm > 1e-12).all(),
                "Non-finite or zero image feature block")
        normalized.append(values / norm)
    result = np.concatenate(normalized, axis=1) / np.sqrt(len(normalized))
    require(np.isfinite(result).all() and np.allclose(np.linalg.norm(result, axis=1), 1, rtol=0, atol=1e-12),
            "Representation normalization failed")
    return result


def load_multiregion(index, baseline, full_slide):
    rows = read_csv(INDEX_PATH)
    require(len(rows) == 1840, "Multiregion index must contain 1840 train/validation rows")
    required = {"row_index", "baseline_row_index", "slide_id", "group_id", "split", "image_sha256", "model_revision",
                "feature_sha256", "input_manifest_sha256", "recipe_sha256", "dtype", "region_count", "feature_dimension",
                "feature_shape", "feature_definition", "region_order", "interpolation", *MODEL_HASHES}
    require(required.issubset(rows[0]), "Multiregion index lacks provenance fields")
    require([int(row["row_index"]) for row in rows] == list(range(1840)), "Multiregion row indexes differ")
    for old, row in zip(index, rows):
        require(all(row[field] == old[field] for field in ("baseline_row_index", "slide_id", "group_id", "split", "image_sha256")),
                "Multiregion input identity/order differs from approved train/validation index")
        require(row["split"] in ("train", "validation"), "Test/excluded row in multiregion features")
    require({row["model_revision"] for row in rows} == {REVISION}, "Unapproved multiregion model revision")
    require({row["dtype"] for row in rows} == {"float32"}, "Multiregion features must be float32")
    for field, expected in (("region_count", "3"), ("feature_dimension", "512"), ("feature_shape", "3x512"),
                            ("feature_definition", "unnormalized projected image_embeds"),
                            ("region_order", "left|center|right"), ("interpolation", "Pillow.Resampling.BICUBIC")):
        require({row[field] for row in rows} == {expected}, f"Unexpected multiregion definition: {field}")
    for field, expected in MODEL_HASHES.items():
        require({row[field] for row in rows} == {expected}, f"Pinned CLIP artifact changed: {field}")
    feature_hash = sha256(FEATURE_PATH)  # This independent file contains no test rows.
    require({row["feature_sha256"] for row in rows} == {feature_hash}, "Multiregion feature SHA256 mismatch")
    require({row["input_manifest_sha256"] for row in rows} == {manifest_sha(index)}, "Multiregion image manifest differs")
    recipe_hashes = {row["recipe_sha256"] for row in rows}
    require(len(recipe_hashes) == 1 and re.fullmatch(r"[0-9a-f]{64}", next(iter(recipe_hashes))) is not None,
            "Multiregion recipe is missing or inconsistent")
    # Importing the extraction script does not load the model or images. It
    # supplies the frozen, inspectable crop/resize/normalization recipe only.
    from extract_multiregion_clip import RECIPE
    require(recipe_hashes == {json_digest(RECIPE)}, "Extraction implementation/recorded recipe differ")
    regions = np.load(FEATURE_PATH, allow_pickle=False)
    require(regions.shape == (1840, 3, 512) and regions.dtype == np.float32,
            "Expected multiregion float32 shape (1840,3,512)")
    require(np.isfinite(regions).all() and (np.linalg.norm(regions.astype(np.float64), axis=2) > 1e-12).all(),
            "Multiregion features contain NaN/Inf/zero vectors")
    representations = {
        "A": normalize_blocks(baseline),
        "B": normalize_blocks(*(regions[:, region, :] for region in range(3))),
        "C": normalize_blocks(*(regions[:, region, :] for region in range(3)), full_slide),
    }
    require(all(x.shape == (1840, DIMENSIONS[name]) for name, x in representations.items()),
            "Unexpected representation shapes")
    provenance = {"feature_path": str(FEATURE_PATH.relative_to(BASE)),
                  "feature_sha256": feature_hash, "feature_index_sha256": sha256(INDEX_PATH),
                  "feature_shape": list(regions.shape), "feature_dtype": str(regions.dtype),
                  "input_manifest_sha256": manifest_sha(index),
                  "recipe_sha256": next(iter(recipe_hashes)), "recipe": RECIPE,
                  "representations": {name: {"shape": list(x.shape), "normalized_values_sha256": array_digest(x)}
                                      for name, x in representations.items()}}
    return representations, provenance


def source_types(index):
    ids = {row["slide_id"] for row in index}
    # metadata source_type is used only for report stratification, never X.
    metadata = {f"slide_{r['id']}": r for r in read_csv(METADATA_PATH) if f"slide_{r['id']}" in ids}
    require(len(metadata) == 1840, "Train/validation source metadata is incomplete")
    result = np.array([metadata[row["slide_id"]]["source_type"] for row in index], dtype=object)
    require(all(isinstance(value, str) and value for value in result), "Missing source_type")
    return result


def data_quality(index, y, masks, sources):
    """Recheck only approved train/validation PNGs and summarize fixed labels."""
    issues, sizes, image_manifest = [], Counter(), hashlib.sha256()
    for i, row in enumerate(index):
        sid = row["slide_id"]
        require(row["split"] in ("train", "validation"), "Refusing PNG outside train/validation")
        path = BASE / "data/images" / f"{sid}.png"
        try:
            digest = sha256(path)
            require(digest == row["image_sha256"], "PNG SHA256 differs from frozen input identity")
            with Image.open(path) as image:
                require(image.format == "PNG", "Image is not PNG")
                image.verify()
            with Image.open(path) as image:
                image.load()
                width, height = image.size
            require((width, height) == (int(row["original_width"]), int(row["original_height"])),
                    "PNG dimensions differ from metadata/feature index")
            sizes[f"{width}x{height}"] += 1
            image_manifest.update(sid.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\0")
        except (OSError, ValueError) as error:
            issues.append({"slide_id": sid, "error": str(error)})
        if (i + 1) % 256 == 0:
            print(json.dumps({"event": "trainval_quality_progress", "checked": i + 1, "issues": len(issues)}), flush=True)
    require(not issues, f"PNG validation failed; stop without modifying samples: {issues}")
    result = {"labels": list(LABELS), "verified_pages": len(index), "image_issues": issues,
              "image_manifest_sha256": image_manifest.hexdigest(),
              "image_manifest_rule": "numeric slide_id order; UTF-8 slide_id + NUL + lowercase SHA256 + NUL",
              "subsets": {}, "sizes": dict(sorted(sizes.items()))}
    all_duplicates = defaultdict(list)
    for i, row in enumerate(index):
        all_duplicates[row["image_sha256"]].append(i)
    result["cross_train_validation_identical_png_sets"] = sum(
        len({index[i]["split"] for i in positions}) > 1 for positions in all_duplicates.values())
    for subset in ("train", "validation"):
        positions = np.flatnonzero(masks[subset])
        truth = y[positions]
        duplicate, groups = defaultdict(list), defaultdict(list)
        for i in positions:
            duplicate[index[i]["image_sha256"]].append(int(i))
            groups[index[i]["group_id"]].append(int(i))
        duplicate_sets = [(digest, p) for digest, p in duplicate.items() if len(p) > 1]
        conflicts = []
        for digest, p in duplicate_sets:
            if len({tuple(y[i].tolist()) for i in p}) > 1:
                conflicts.append({"sha256": digest, "rows": [
                    {"slide_id": index[i]["slide_id"], "group_id": index[i]["group_id"], "labels": y[i].tolist()} for i in p]})
        mixed_groups, mixed_pairs = [], []
        for j in range(4):
            counts = [int(y[p, j].sum()) for p in groups.values()]
            mixed_groups.append(sum(0 < count < 4 for count in counts))
            mixed_pairs.append(sum(count * (4 - count) for count in counts))
        by_source = {}
        for source in sorted(set(sources[positions])):
            source_y = y[masks[subset] & (sources == source)]
            positive = source_y.sum(axis=0)
            by_source[str(source)] = {"pages": len(source_y), "positives": positive.tolist(),
                                      "negatives": (len(source_y) - positive).tolist(),
                                      "positive_rates": (positive / len(source_y)).tolist()}
        result["subsets"][subset] = {"pages": len(positions), "groups": len(groups),
            "positives": truth.sum(axis=0).tolist(), "negatives": (len(truth) - truth.sum(axis=0)).tolist(),
            "any_target_positive": int((truth.sum(axis=1) > 0).sum()),
            "all_target_negative": int((truth.sum(axis=1) == 0).sum()),
            "mixed_label_groups": mixed_groups, "mixed_label_positive_negative_pairs": mixed_pairs,
            "duplicate_png_sets": len(duplicate_sets), "duplicate_png_pages": sum(len(p) for _, p in duplicate_sets),
            "conflicting_png_sets": len(conflicts), "conflicting_png_pages": sum(len(c["rows"]) for c in conflicts),
            "conflicts": conflicts, "sources": by_source}
        if subset == "train":
            result["train_identical_png_cross_group_sets"] = sum(
                len({index[i]["group_id"] for i in p}) > 1 for _, p in duplicate_sets)
    return result


def make_folds(y_train, train_rows):
    groups = np.array([row["group_id"] for row in train_rows], dtype=object)
    grouped = Counter(groups)
    require(len(groups) == 1512 and len(grouped) == 378 and set(grouped.values()) == {4},
            "Expected 1512 train pages in 378 intact four-page groups")
    folds, row_folds, summaries, group_map = [], np.full(1512, -1, dtype=np.int8), [], {}
    splitter = GroupKFold(n_splits=3, shuffle=True, random_state=SEED)
    for fold_id, (fit_positions, holdout_positions) in enumerate(splitter.split(np.zeros((1512, 1)), y_train, groups)):
        fit_groups, holdout_groups = set(groups[fit_positions]), set(groups[holdout_positions])
        require(not fit_groups.intersection(holdout_groups), "Same-source train group crosses CV fold")
        require(set(np.unique(y_train[fit_positions])) == {0, 1}, "Non-binary train labels")
        require(all(set(np.unique(y_train[fit_positions, j])) == {0, 1} for j in range(4)),
                f"A label lacks positive/negative examples in training fold {fold_id}")
        require((row_folds[holdout_positions] == -1).all(), "OOF row assigned more than once")
        row_folds[holdout_positions] = fold_id
        folds.append((fit_positions, holdout_positions))
        group_map[str(fold_id)] = sorted(holdout_groups)
        summaries.append({"fold": fold_id, "fit_pages": len(fit_positions), "fit_groups": len(fit_groups),
                          "holdout_pages": len(holdout_positions), "holdout_groups": len(holdout_groups),
                          "fit_positive_counts": y_train[fit_positions].sum(axis=0).tolist(),
                          "holdout_positive_counts": y_train[holdout_positions].sum(axis=0).tolist()})
    require((row_folds >= 0).all() and all(len(set(row_folds[groups == gid])) == 1 for gid in grouped),
            "OOF assignment incomplete or split within a group")
    fold_manifest = [{"slide_id": row["slide_id"], "group_id": row["group_id"], "fold": int(row_folds[i])}
                     for i, row in enumerate(train_rows)]
    return folds, row_folds, {"folds": summaries, "holdout_groups_by_fold": group_map,
                              "row_fold_manifest_sha256": json_digest(fold_manifest)}


def fit_lr(x, y, c, class_weight):
    model = LogisticRegression(C=c, class_weight=class_weight, solver="lbfgs", max_iter=3000,
                               tol=1e-6, random_state=SEED)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(x, y)
    require(model.classes_.tolist() == [0, 1], "Unexpected binary classifier class order")
    require(np.isfinite(model.coef_).all() and np.isfinite(model.intercept_).all(), "Invalid fitted coefficients")
    return model


def mixed_group_metrics(y, probabilities, predictions, rows):
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row["group_id"]].append(i)
    require(all(len(positions) == 4 for positions in groups.values()), "Expected complete four-page diagnostic groups")
    result = {}
    for j, label in enumerate(LABELS):
        pairs = wins = ties = pair_both = all_four = 0
        aucs, gaps = [], []
        for positions in groups.values():
            positions = np.asarray(positions, dtype=np.int64)
            truth, scores, preds = y[positions, j], probabilities[positions, j], predictions[positions, j]
            if truth.min() == truth.max():
                continue
            differences = scores[truth == 1][:, None] - scores[truth == 0][None, :]
            n, w, t = differences.size, int((differences > 0).sum()), int((differences == 0).sum())
            pairs += n
            wins += w
            ties += t
            aucs.append((w + .5 * t) / n)
            gaps.append(float(scores[truth == 1].mean() - scores[truth == 0].mean()))
            pair_both += int(((preds[truth == 1][:, None] == 1) & (preds[truth == 0][None, :] == 0)).sum())
            all_four += int(np.array_equal(preds, truth))
        result[label] = {"mixed_groups": len(aucs), "positive_negative_pairs": int(pairs),
                         "ranking_wins": wins, "ranking_ties": ties,
                         "pooled_within_group_ranking_auc": (wins + .5 * ties) / pairs if pairs else None,
                         "equal_group_ranking_auc": float(np.mean(aucs)) if aucs else None,
                         "equal_group_mean_positive_minus_negative_probability": float(np.mean(gaps)) if gaps else None,
                         "pair_both_classified_correctly": pair_both,
                         "pair_both_correct_rate": pair_both / pairs if pairs else None,
                         "mixed_group_all_four_correct_count": all_four}
    return result


def stratified_metrics(y, probabilities, thresholds, sources):
    result = {}
    for source in sorted(set(sources)):
        mask = sources == source
        values = {"pages": int(mask.sum()), "labels": {}}
        for j, label in enumerate(LABELS):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                m = binary_metrics(y[mask, j], probabilities[mask, j], thresholds[j])
            if not y[mask, j].sum():
                m["average_precision"] = None  # No positive is a genuine undefined AP.
            values["labels"][label] = m
        result[str(source)] = values
    return result


def run_cv(name, train_x, train_y, train_rows, sources, folds):
    choices, trials, selected_scores = [], [], np.empty((1512, 4), dtype=np.float64)
    fit_count = 0
    for j, label in enumerate(LABELS):
        candidates = []
        for c in CS:
            for class_weight in CLASS_WEIGHTS:
                scores = np.full(1512, np.nan, dtype=np.float64)
                iterations = []
                for fit_positions, holdout_positions in folds:
                    model = fit_lr(train_x[fit_positions], train_y[fit_positions, j], c, class_weight)
                    scores[holdout_positions] = model.predict_proba(train_x[holdout_positions])[:, 1]
                    iterations.append(int(model.n_iter_[0]))
                    fit_count += 1
                require(np.isfinite(scores).all() and ((scores >= 0) & (scores <= 1)).all(), "Incomplete/invalid OOF scores")
                default = binary_metrics(train_y[:, j], scores, .5)
                trial = {"representation": name, "label": label, "C": c, "class_weight": class_weight,
                         "oof_average_precision": default["average_precision"], "oof_default_0_5": default,
                         "fold_n_iter": iterations, "oof_probability_sha256": array_digest(scores), "selected": False}
                key = (default["average_precision"], -c, 0 if class_weight is None else -1)
                candidates.append((key, scores, trial))
                trials.append(trial)
        _, scores, selected = max(candidates, key=lambda entry: entry[0])
        selected["selected"] = True
        threshold = choose_threshold(train_y[:, j], scores)
        choices.append({"label": label, "C": selected["C"], "class_weight": selected["class_weight"],
                        "threshold": threshold["threshold"], "oof_average_precision": selected["oof_average_precision"],
                        "oof_selected_threshold_metrics": threshold, "fold_n_iter": selected["fold_n_iter"]})
        selected_scores[:, j] = scores
        print(json.dumps({"event": "train_oof_choice", "representation": name, **choices[-1]}, ensure_ascii=False), flush=True)
    thresholds = [choice["threshold"] for choice in choices]
    selected_metrics, predictions = summary_metrics(train_y, selected_scores, thresholds)
    default_metrics, _ = summary_metrics(train_y, selected_scores, [.5] * 4)
    result = {"representation": name, "dimension": DIMENSIONS[name], "cv_fit_count": fit_count,
              "selections": choices, "trials": trials,
              "metrics": {"selected_thresholds": selected_metrics, "default_0_5": default_metrics},
              "mixed_groups": mixed_group_metrics(train_y, selected_scores, predictions, train_rows),
              "by_source_type": stratified_metrics(train_y, selected_scores, thresholds, sources),
              "selected_oof_probability_sha256": array_digest(selected_scores)}
    require(fit_count == 72 and len(trials) == 24, "Unexpected CV training budget")
    return result


def compare_validation(name, x, y, index, masks, sources, selection):
    # This is the first and only validation probability evaluation for this
    # frozen representation. No callback can alter any OOF-selected choice.
    train_x, valid_x = x[masks["train"]], x[masks["validation"]]
    train_y, valid_y = y[masks["train"]], y[masks["validation"]]
    probabilities, iterations = [], []
    for j, choice in enumerate(selection["selections"]):
        model = fit_lr(train_x, train_y[:, j], choice["C"], choice["class_weight"])
        probabilities.append(model.predict_proba(valid_x)[:, 1])
        iterations.append(int(model.n_iter_[0]))
    scores = np.column_stack(probabilities)
    require(scores.shape == (328, 4) and np.isfinite(scores).all(), "Invalid validation scores")
    thresholds = [choice["threshold"] for choice in selection["selections"]]
    chosen, predictions = summary_metrics(valid_y, scores, thresholds)
    default, _ = summary_metrics(valid_y, scores, [.5] * 4)
    rows = [r for r, use in zip(index, masks["validation"]) if use]
    result = {"representation": name, "selections": selection["selections"], "fit_count": 4,
              "n_iter": iterations, "metrics": {"selected_thresholds": chosen, "default_0_5": default},
              "mixed_groups": mixed_group_metrics(valid_y, scores, predictions, rows),
              "by_source_type": stratified_metrics(valid_y, scores, thresholds, sources[masks["validation"]]),
              "validation_probability_sha256": array_digest(scores)}
    print(json.dumps({"event": "exploratory_validation_once", "representation": name,
                      "macro_ap": chosen["macro_average_precision"], "macro_f1": chosen["macro"]["f1"]}), flush=True)
    return result, scores, predictions


def save_and_verify_predictions(best, y, index, masks, sources, validation):
    rows = [row for row, keep in zip(index, masks["validation"]) if keep]
    truth, valid_sources = y[masks["validation"]], sources[masks["validation"]]
    fields = ["slide_id", "group_id", "split", "source_type", "selected_representation"]
    fields += [f"true_{slug}" for slug in SLUGS]
    for prefix in ("scheme_a", "selected"):
        fields += [f"{prefix}_{kind}_{slug}" for kind in ("prob", "pred", "threshold") for slug in SLUGS]
    temporary = PREDICTION_PATH.with_suffix(".csv.part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i, row in enumerate(rows):
            record = {"slide_id": row["slide_id"], "group_id": row["group_id"], "split": "validation",
                      "source_type": valid_sources[i], "selected_representation": best}
            record.update({f"true_{slug}": int(truth[i, j]) for j, slug in enumerate(SLUGS)})
            for prefix, name in (("scheme_a", "A"), ("selected", best)):
                result, scores, predictions = validation[name]
                for j, slug in enumerate(SLUGS):
                    record[f"{prefix}_prob_{slug}"] = format(float(scores[i, j]), ".17g")
                    record[f"{prefix}_pred_{slug}"] = int(predictions[i, j])
                    record[f"{prefix}_threshold_{slug}"] = format(result["selections"][j]["threshold"], ".17g")
            writer.writerow(record)
    # Independent reload ensures the exported probabilities and thresholds can
    # reproduce every validation statistic in the report exactly.
    reloaded = read_csv(temporary)
    require(len(reloaded) == 328 and len({r["slide_id"] for r in reloaded}) == 328, "CSV IDs incomplete/duplicated")
    require([(r["slide_id"], r["group_id"], r["split"]) for r in reloaded]
            == [(r["slide_id"], r["group_id"], "validation") for r in rows], "CSV validation identity mismatch")
    restored_y = np.array([[int(r[f"true_{slug}"]) for slug in SLUGS] for r in reloaded], dtype=np.uint8)
    require(np.array_equal(restored_y, truth), "CSV official labels differ")
    require([r["source_type"] for r in reloaded] == valid_sources.tolist(), "CSV sources differ")
    require({r["selected_representation"] for r in reloaded} == {best}, "CSV selected representation differs")
    for prefix, name in (("scheme_a", "A"), ("selected", best)):
        expected = validation[name][0]
        probabilities = np.array([[float(r[f"{prefix}_prob_{slug}"]) for slug in SLUGS] for r in reloaded])
        thresholds = [choice["threshold"] for choice in expected["selections"]]
        saved_thresholds = np.array([[float(r[f"{prefix}_threshold_{slug}"]) for slug in SLUGS] for r in reloaded])
        require(np.array_equal(saved_thresholds, np.broadcast_to(thresholds, (328, 4))), "CSV thresholds differ")
        chosen, predictions = summary_metrics(restored_y, probabilities, thresholds)
        default, _ = summary_metrics(restored_y, probabilities, [.5] * 4)
        saved_predictions = np.array([[int(r[f"{prefix}_pred_{slug}"]) for slug in SLUGS] for r in reloaded])
        require(np.array_equal(saved_predictions, predictions), "CSV thresholded predictions differ")
        require_nested_close({"selected_thresholds": chosen, "default_0_5": default}, expected["metrics"], prefix)
        require_nested_close(mixed_group_metrics(restored_y, probabilities, predictions, rows), expected["mixed_groups"], prefix)
        require_nested_close(stratified_metrics(restored_y, probabilities, thresholds, valid_sources), expected["by_source_type"], prefix)
    os.replace(temporary, PREDICTION_PATH)
    return sha256(PREDICTION_PATH)


def nfmt(value):
    return "n/a" if value is None else f"{value:.4f}"


def metric_table(lines, title, methods):
    lines += ["", title, "", "| 方案 | 标签 | AP | P | R | F1 | TP | FP | TN | FN | 阈值 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods.items():
        for label in LABELS:
            m = method["metrics"]["selected_thresholds"]["per_label"][label]
            lines.append(f"| {name} | {label} | {m['average_precision']:.4f} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['tp']} | {m['fp']} | {m['tn']} | {m['fn']} | {m['threshold']:.2f} |")
    lines += ["", "| 方案/阈值 | Macro AP | Macro P | Macro R | Macro F1 | Micro P | Micro R | Micro F1 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods.items():
        for key, text in (("selected_thresholds", "OOF选阈值"), ("default_0_5", "0.5")):
            m = method["metrics"][key]
            values = [m["macro_average_precision"]] + [m[average][metric] for average in ("macro", "micro") for metric in ("precision", "recall", "f1")]
            lines.append(f"| {name}/{text} | " + " | ".join(f"{v:.4f}" for v in values) + " |")


def report(quality, provenance, region_provenance, fold_summary, cv, best, validation, csv_hash, started, completed=None):
    completed = completed or now()
    selected_validation = {name: values[0] for name, values in validation.items()}
    lines = ["# 第四阶段 4.3E：多区域 CLIP 分组交叉验证", "",
             f"实际执行：{started} 至 {completed}。只使用 train 1,512页/378组与 validation 328页/82组；excluded不参与。未读取 test 图片、test annotations、test 特征值、旧模型或正式测试结果。", "",
             "## 1. 固定方案与特征来源", "",
             f"CLIP `{MODEL_ID}`，revision `{REVISION}`；冻结视觉编码器及投影、RGB、eval、inference_mode，无反向传播。新增区域特征 float32 `(1840,3,512)`，全部有限且非零，与完整页索引按数值 slide_id 升序严格对齐。", "",
             "| 方案 | 输入 | 维度 |", "| --- | --- | --- |", "| A | 复用4.3C官方中心裁剪 | 512 |",
             "| B | left+center+right | 1536 |", "| C | left+center+right+复用4.3D白色完整页补边 | 2048 |", "",
             "各512维向量分别以float64固定L2归一化，再拼接并除以块数平方根；所有行的L2长度均为1，无需fit。区域权重相等，无标签、source_type、group_id、split、slide_id或descriptions进入X。新增区域规则及预处理完整记录见文末recipe。", "",
             "区域按left/center/right排列。横向页裁剪边长=原图高度h，x=0、floor((w-h)/2)、w-h，y=0，三个正方形均覆盖全高；各自Pillow BICUBIC缩至224×224。官方处理器显式关闭resize和center_crop，保持原有rescale与mean/std。极宽w>3h时拒绝运行以避免覆盖间隙；非横向图预先居中补白到h×h，三个区域均为同一保留整页的正方形。实际1,840页全部横向，没有触发非横向/极宽规则。C直接复用4.3D固定白色补边特征，没有再提取全页向量。", "",
             "## 2. 数据质量简查", "",
             "以下只报告问题，不删除样本、不改官方布尔标签、不改变冻结划分；正例必须 response is True。train/validation四标签计数再次与4.3A完全一致。", "",
             "| 标签 | train正/负 | validation正/负 |", "| --- | --- | --- |"]
    for j, label in enumerate(LABELS):
        p, v = provenance["positives"]["train"][j], provenance["positives"]["validation"][j]
        lines.append(f"| {label} | {p}/{1512-p} | {v}/{328-v} |")
    if quality is not None:
        tq, vq = quality["subsets"]["train"], quality["subsets"]["validation"]
        lines += ["", f"实际逐文件核验 {quality['verified_pages']} 张PNG，缺失、SHA256、解码和尺寸异常共 {len(quality['image_issues'])} 张。train存在 {tq['duplicate_png_sets']} 套完全相同PNG、共 {tq['duplicate_png_pages']} 页；其中 **{tq['conflicting_png_sets']} 套/{tq['conflicting_png_pages']} 页的四目标标签互相矛盾**。validation重复 {vq['duplicate_png_sets']} 套/{vq['duplicate_png_pages']} 页，无冲突。train/validation完全相同PNG跨集合 {quality['cross_train_validation_identical_png_sets']} 套；train重复PNG跨可信组 {quality['train_identical_png_cross_group_sets']} 套，跨CV折 {fold_summary['train_identical_png_cross_fold_sets']} 套。所有样本和官方标签均保留。", "",
                  "| train冲突PNG（四位标签顺序同本文） | group_id | 标签 |", "| --- | --- | --- |"]
        for conflict in tq["conflicts"]:
            for row in conflict["rows"]:
                lines.append(f"| {row['slide_id']} | {row['group_id']} | {row['labels']} |")
        lines += ["", "完全相同图像无法由纯图像分类器区分上述相反目标；这里只标记监督不一致，不推断哪条标签正确。可信同源组本来允许增强后缺陷不同，普通正负混合不等于标注错误。", "",
                  "| 标签 | train混合组/正负页对 | validation混合组/正负页对 |", "| --- | --- | --- |"]
        for j, label in enumerate(LABELS):
            lines.append(f"| {label} | {tq['mixed_label_groups'][j]}/{tq['mixed_label_positive_negative_pairs'][j]} | {vq['mixed_label_groups'][j]}/{vq['mixed_label_positive_negative_pairs'][j]} |")
        lines += ["", "来源标签分布（每格为正例数/页面数与比例，未作来源均衡或样本删减）：", "",
                  "| 集合 | source_type | 页数 | 字号 | 遮挡 | 杂乱 | 溢出 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for subset, qa in quality["subsets"].items():
            for source, item in qa["sources"].items():
                cells = [f"{item['positives'][j]}/{item['pages']} ({item['positive_rates'][j]:.1%})" for j in range(4)]
                lines.append(f"| {subset} | {source} | {item['pages']} | " + " | ".join(cells) + " |")
        lines += ["", "不同来源正例比例明显不均衡，例如train杂乱正例在Gemini只有7/464（1.5%），gdcdataset为108/516（20.9%）；遮挡和溢出也随来源变化。因此总体指标可能利用来源/内容分布，必须同时查看分来源和组内排序诊断。"]
        narrative = quality.get("report_markdown") if isinstance(quality, dict) else None
        if isinstance(narrative, str):
            lines += ["", narrative]
        lines += ["", "<details><summary>数据质量检查原始摘要（仅train/validation）</summary>", "", "```json",
                  json.dumps(quality, ensure_ascii=False, sort_keys=True, indent=2), "```", "", "</details>"]
    else:
        lines += ["", "未传入独立图片质量检查摘要；本报告的特征身份检查不替代逐PNG校验。"]
    lines += ["", "## 3. 训练内分组交叉验证与冻结选择", "",
              "3折GroupKFold(shuffle=True,random_state=42)，每个四页组完整分配到一折。三种表示、四标签及全部参数使用完全相同折；所有训练折每标签都有正负例。模型为独立LogisticRegression(lbfgs,max_iter=3000,tol=1e-6,random_state=42)，C={0.1,1,10}×class_weight={None,balanced}，每表示72次CV拟合，总216次；全部收敛。", "",
              "每标签以全部1512个OOF概率的 pooled Average Precision 选配置，同分优先更小C、再优先None。每方案的四个已选标签AP取平均，按最高Macro OOF AP选统一表示，同分优先低维。阈值仅从相应train OOF概率以F1最大选择0.05–0.95步长0.01，同分选距0.5最近、再较大阈值。validation不参与任何选择。", "",
              "这不是嵌套交叉验证：相同OOF预测参与参数、表示和阈值选择，因此选中OOF AP/F1有选择乐观偏差，阈值后的指标尤其如此。OOF概率来自三个不同训练折模型，概率校准差异也可能影响pooled AP/阈值的稳定性。", "",
              "| 折 | fit页/组 | holdout页/组 | fit四标签正例 | holdout四标签正例 |", "| --- | --- | --- | --- | --- |"]
    for fold in fold_summary["folds"]:
        lines.append(f"| {fold['fold']} | {fold['fit_pages']}/{fold['fit_groups']} | {fold['holdout_pages']}/{fold['holdout_groups']} | {fold['fit_positive_counts']} | {fold['holdout_positive_counts']} |")
    metric_table(lines, "### Train OOF（各方案独立选择配置；阈值仅来自OOF）", cv)
    lines += ["", f"统一最佳表示：**{best}（{NAMES[best]}，{DIMENSIONS[best]}维）**，仅按Macro OOF AP选出。", "",
              "| 方案 | 标签 | C | class_weight | OOF阈值 | OOF AP |", "| --- | --- | --- | --- | --- | --- |"]
    for name, method in cv.items():
        for choice in method["selections"]:
            lines.append(f"| {name} | {choice['label']} | {choice['C']:g} | {choice['class_weight']} | {choice['threshold']:.2f} | {choice['oof_average_precision']:.4f} |")
    lines += ["", "### 组内正负增强版本排序诊断", "",
              "仅对同一可信四页组中正负混合的组，将每个正负页对按正例概率更高/相等/更低计1/0.5/0。pooled组内AUC按页对加权，组等权AUC先逐组平均。OOF同组四页来自同一holdout折。页对不独立，不当作额外样本；阈值无关的组内AUC可以检查是否识别了版本间变化。", "",
              "| 数据/方案 | 标签 | 混合组 | 页对 | pooled组内AUC | 组等权AUC | 正负平均概率差 | 双页都对 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for subset, methods in (("OOF", cv), ("validation", selected_validation)):
        for name, method in methods.items():
            for label in LABELS:
                m = method["mixed_groups"][label]
                lines.append(f"| {subset}/{name} | {label} | {m['mixed_groups']} | {m['positive_negative_pairs']} | {nfmt(m['pooled_within_group_ranking_auc'])} | {nfmt(m['equal_group_ranking_auc'])} | {nfmt(m['equal_group_mean_positive_minus_negative_probability'])} | {m['pair_both_classified_correctly']}/{m['positive_negative_pairs']} |")
    lines += ["", "### 不同source_type的OOF表现", "",
              "source_type只用于下表诊断；n为页面数，pos为该标签正例数。标签/来源不均衡及较小来源子集使跨来源比较不稳定；某子集无正例时AP记n/a。", "",
              "| 方案 | source_type | n | 标签 | pos | AP | P | R | F1 | FP | FN |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in cv.items():
        for source, result in method["by_source_type"].items():
            for label, m in result["labels"].items():
                lines.append(f"| {name} | {source} | {result['pages']} | {label} | {m['actual_positive']} | {nfmt(m['average_precision'])} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['fp']} | {m['fn']} |")
    lines += ["", "## 4. 一次探索性validation比较", "",
              "在所有OOF选择冻结后，仅将方案A与统一最佳方案各自的四个分类器在全部train上拟合，使用OOF阈值分别预测validation一次；若最佳方案就是A，则只拟合/预测A一次并复用相同结果。不合并validation到fit，不依据validation再调参，不保存新模型。", "",
              "这里A也采用新的train-OOF选择规则，不能与4.3D使用validation调参所得数字直接混为同一实验。validation已参与前几轮研究，其结果属于探索性比较，不是全新独立测试结论。"]
    metric_table(lines, "### Validation（OOF冻结规则）", selected_validation)
    base, chosen = selected_validation["A"]["metrics"]["selected_thresholds"], selected_validation[best]["metrics"]["selected_thresholds"]
    lines += ["", "最佳方案减A的validation变化（负ΔFP/ΔFN分别表示误报/漏检减少）：", "",
              "| 标签 | ΔAP | ΔF1 | ΔP | ΔR | ΔTP | ΔFP | ΔFN |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label in LABELS:
        a, b = base["per_label"][label], chosen["per_label"][label]
        lines.append(f"| {label} | " + " | ".join(f"{b[k]-a[k]:+.4f}" for k in ("average_precision", "f1", "precision", "recall")) + " | " + " | ".join(f"{b[k]-a[k]:+d}" for k in ("tp", "fp", "fn")) + " |")
    lines += ["", "## 5. 结论与下一步", ""]
    for name in ("B", "C"):
        m, a = cv[name]["metrics"]["selected_thresholds"], cv["A"]["metrics"]["selected_thresholds"]
        lines.append(f"- {name}相对A：OOF Macro AP {a['macro_average_precision']:.4f}→{m['macro_average_precision']:.4f}（{m['macro_average_precision']-a['macro_average_precision']:+.4f}）；OOF Macro F1 {a['macro']['f1']:.4f}→{m['macro']['f1']:.4f}（{m['macro']['f1']-a['macro']['f1']:+.4f}）。")
    total_fp_a = sum(base["per_label"][label]["fp"] for label in LABELS)
    total_fn_a = sum(base["per_label"][label]["fn"] for label in LABELS)
    total_fp_b = sum(chosen["per_label"][label]["fp"] for label in LABELS)
    total_fn_b = sum(chosen["per_label"][label]["fn"] for label in LABELS)
    lines += [f"- 统一最佳{best}相对A的validation Macro AP差{chosen['macro_average_precision']-base['macro_average_precision']:+.4f}、Macro F1差{chosen['macro']['f1']-base['macro']['f1']:+.4f}；四标签事件总FP {total_fp_a}→{total_fp_b}、FN {total_fn_a}→{total_fn_b}。每页可计多个标签事件，必须结合逐类表判断误报与漏检，不能以单个F1概括全部收益。"]
    for label in LABELS:
        a = cv["A"]["metrics"]["selected_thresholds"]["per_label"][label]
        b = cv[best]["metrics"]["selected_thresholds"]["per_label"][label]
        lines.append(f"- {label}：OOF AP {a['average_precision']:.4f}→{b['average_precision']:.4f}，F1 {a['f1']:.4f}→{b['f1']:.4f}；最佳方案validation AP/F1为{chosen['per_label'][label]['average_precision']:.4f}/{chosen['per_label'][label]['f1']:.4f}。")
    if best != "A":
        oof_a = cv["A"]["metrics"]["selected_thresholds"]
        oof_best = cv[best]["metrics"]["selected_thresholds"]
        oof_ap_gain = oof_best["macro_average_precision"] - oof_a["macro_average_precision"]
        lines += ["", f"{best}是按预设Macro OOF AP规则选中的方案，但其相对A的优势只有{ oof_ap_gain:+.6f}；OOF Macro F1为{oof_a['macro']['f1']:.4f}→{oof_best['macro']['f1']:.4f}。这点OOF AP优势很小，且来自非嵌套选择，不能直接称为多区域表示有效或稳定改善。"]
        if (chosen["macro_average_precision"] <= base["macro_average_precision"]
                and chosen["macro"]["f1"] <= base["macro"]["f1"]):
            lines += ["", f"探索性validation的Macro AP和Macro F1均下降，未支持稳定的总体改善。仍保留{best}作为本轮预设规则的选中方案，不依据validation改选A或追加参数/区域搜索；本轮也不更改正式测试结果。"]
        else:
            lines += ["", "validation变化应与OOF的小幅优势、非嵌套选择偏差和有限样本一起解释，不作稳定改善断言，也不据validation反选表示或追加参数/区域搜索。"]
        lines += ["", "validation误报/漏检的逐类权衡："]
        simultaneous_reductions = []
        for label in LABELS:
            a, b = base["per_label"][label], chosen["per_label"][label]
            if b["fp"] < a["fp"] and b["fn"] < a["fn"]:
                simultaneous_reductions.append(label)
            lines.append(f"- {label}：FP {a['fp']}→{b['fp']}，FN {a['fn']}→{b['fn']}。")
        if not simultaneous_reductions:
            lines += ["", "没有任何一类同时减少FP和FN。误报总数下降伴随漏检总数增加，不能概括为同时改善了误报与漏检；例如字号/遮挡/溢出的误报减少而漏检增加，杂乱则相反。"]
        else:
            lines += ["", "同时减少FP与FN的类别为：" + "、".join(simultaneous_reductions) + "；其他类别仍需按表中的权衡解释。"]
        fine_labels = (LABELS[0], LABELS[2], LABELS[3])
        auc_text = "、".join(f"{label} {cv[best]['mixed_groups'][label]['pooled_within_group_ranking_auc']:.4f}"
                            for label in fine_labels)
        lines += ["", f"{best}的OOF组内排序AUC：{auc_text}。这些值接近随机排序0.5，显示页面级全局AP与同源增强版本区分能力并不等价；结合来源标签分布差异，应警惕内容/来源线索带来的影响。这只是诊断，不能据此因果断言模型依赖了哪种线索。", "",
                  "对多区域表示，当前只有很弱的探索性线索，可保留为后续对照；结果不支持扩大无约束的区域组合搜索，也不足以宣称模型已能稳定识别细粒度缺陷。"]
    else:
        lines += ["", "按预先固定的主要指标，原中心裁剪仍最佳，本轮没有整体多区域收益证据。多区域的局部覆盖不自动等于更强缺陷区分，不能从个别标签或validation偶然升高另选未经预先设计的表示。"]
    lines += ["", "字号、遮挡、裁切仍可能受224px区域分辨率、CLIP语义预训练及线性决策边界限制；三个裁剪虽覆盖整页，但局部小文字仍会缩小，覆盖不等于保留全部细节。来源不均衡、完全相同PNG上的标签冲突（如质量检查发现）及同源增强相关性也限制上限，不能单凭低分认定标注或程序错误。", "",
              "下一步值得做预先固定的小规模结构特征融合研究：文字字号/数量、相交与页面边界等需从实际PPT结构或可部署的无标签解析取得，而非含答案的descriptions/annotation。应先冻结特征清单、分组CV与比较规则，保持预算有限，检验它是否补充CLIP的细粒度限制；当前尚未验证融合收益，本轮未提取、拟合或融合任何结构特征。", "",
              "所有页面级指标受到同源增强相关性影响，不能等同于独立原始幻灯片级指标。未进行显著性或泛化保证。", "",
              "## 6. 复现与审计", "",
              "执行：`.venv/Scripts/python.exe scripts/train_multiregion_cv.py --quality-json <临时质量检查摘要>`。`--validate-only`只检查输入，不拟合或预测validation。默认拒绝覆盖本轮报告/预测CSV；旧4.3C/4.3D文件从不写入。", "",
              f"逐页validation预测：`reports/multiregion_validation_predictions.csv`，SHA256 `{csv_hash}`。含真实标签、A/最佳方案概率、预测结果及每标签OOF阈值，另含group_id和来源便于复核。脚本已重新读取CSV独立重算全部validation指标、默认0.5对照、组内诊断、分来源指标，与报告完全一致。", "",
              "<details><summary>全部72个配置的OOF记录（★为各标签所选配置）</summary>", "",
              "| 方案 | 标签 | C | class_weight | OOF AP | F1@0.5 | P@0.5 | R@0.5 | 三折迭代数 | 选中 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in cv.items():
        for trial in method["trials"]:
            m = trial["oof_default_0_5"]
            lines.append(f"| {name} | {trial['label']} | {trial['C']:g} | {trial['class_weight']} | {trial['oof_average_precision']:.4f} | {m['f1']:.4f} | {m['precision']:.4f} | {m['recall']:.4f} | {trial['fold_n_iter']} | {'★' if trial['selected'] else ''} |")
    env = {"python": platform.python_version(), "executable": sys.executable,
           **{p: importlib.metadata.version(p) for p in ("numpy", "scikit-learn", "torch", "transformers", "Pillow")}}
    audit = {"started": started, "completed": completed, "protocol": PROTOCOL, "protocol_sha256": json_digest(PROTOCOL),
             "environment": env, "inherited_trainval_provenance": provenance, "multiregion_provenance": region_provenance,
             "fold_assignment": fold_summary, "quality_check": quality, "cv": cv,
             "selected_representation": best, "validation": selected_validation,
             "validation_prediction_csv_sha256": csv_hash, "csv_metrics_recomputed_verified": True,
             "cv_fit_count": 216, "final_train_fit_count": 4 * len(validation),
             "test_evaluation_count": 0, "test_image_reads": 0, "test_annotation_reads": 0,
             "test_feature_values_accessed": 0, "clip_training_performed": False,
             "selection_frozen_before_validation_prediction": True}
    lines += ["", "</details>", "", "<details><summary>机器可读完整审计（含组到折映射及所有指标）</summary>", "", "```json",
              json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False), "```", "", "</details>", ""]
    temporary = REPORT_PATH.with_suffix(".md.part")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, REPORT_PATH)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--quality-json", type=Path, help="Independent train/validation-only image QA summary (temporary input)")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly repeat this frozen experiment and replace only its new report/CSV")
    args = parser.parse_args()
    if not args.validate_only:
        require(args.overwrite or not (REPORT_PATH.exists() or PREDICTION_PATH.exists()),
                "4.3E outputs already exist; refusing unrequested repeated validation/overwrite")
    started = now()
    baseline, full_slide, y, index, masks, provenance = load_inputs()
    representations, region_provenance = load_multiregion(index, baseline, full_slide)
    sources = source_types(index)
    train_rows = [row for row, keep in zip(index, masks["train"]) if keep]
    train_y = y[masks["train"]]
    folds, row_folds, fold_summary = make_folds(train_y, train_rows)
    # Describe exact-image cross-fold repeats without changing mandated grouping.
    duplicate_rows = defaultdict(list)
    for i, row in enumerate(train_rows):
        duplicate_rows[row["image_sha256"]].append(i)
    duplicate_sets = [positions for positions in duplicate_rows.values() if len(positions) > 1]
    fold_summary["train_identical_png_sets"] = len(duplicate_sets)
    fold_summary["train_identical_png_cross_fold_sets"] = sum(len(set(row_folds[p].tolist())) > 1 for p in duplicate_sets)
    fold_summary["train_identical_png_target_conflict_sets"] = sum(len({tuple(train_y[i].tolist()) for i in p}) > 1 for p in duplicate_sets)
    quality = data_quality(index, y, masks, sources)
    if args.quality_json:
        independent_quality = json.loads(args.quality_json.read_text(encoding="utf-8-sig"))
        require_nested_close(quality, independent_quality, "independent_trainval_quality_check")
    if args.validate_only:
        print(json.dumps({"status": "inputs_valid", "counts": provenance["counts"], "shapes": {k: list(x.shape) for k, x in representations.items()},
                          "fold_summary": fold_summary, "test_feature_values_accessed": 0}, ensure_ascii=False), flush=True)
        return
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cv = {}
    with threadpool_limits(limits=1):
        for name, x in representations.items():
            cv[name] = run_cv(name, x[masks["train"]], train_y, train_rows, sources[masks["train"]], folds)
        best = max(cv, key=lambda name: (cv[name]["metrics"]["selected_thresholds"]["macro_average_precision"], -DIMENSIONS[name]))
        # Freeze all selections before any validation model probability call.
        choice_digest = json_digest({"selected_representation": best, "choices": {name: method["selections"] for name, method in cv.items()}})
        print(json.dumps({"event": "all_choices_frozen_before_validation", "selected_representation": best,
                          "choices_sha256": choice_digest, "macro_oof_ap": {name: method["metrics"]["selected_thresholds"]["macro_average_precision"] for name, method in cv.items()}}), flush=True)
        validation = {}
        for name in dict.fromkeys(("A", best)):
            validation[name] = compare_validation(name, representations[name], y, index, masks, sources, cv[name])
    require(choice_digest == json_digest({"selected_representation": best, "choices": {name: method["selections"] for name, method in cv.items()}}),
            "Frozen OOF selections changed during validation")
    csv_hash = save_and_verify_predictions(best, y, index, masks, sources, validation)
    report(quality, provenance, region_provenance, fold_summary, cv, best, validation, csv_hash, started)
    print(json.dumps({"status": "completed", "selected_representation": best,
                      "macro_oof_ap": {name: method["metrics"]["selected_thresholds"]["macro_average_precision"] for name, method in cv.items()},
                      "cv_fit_count": 216, "final_train_fit_count": 4 * len(validation), "test_evaluation_count": 0,
                      "report": str(REPORT_PATH), "predictions": str(PREDICTION_PATH)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
