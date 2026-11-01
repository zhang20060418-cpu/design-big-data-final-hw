#!/usr/bin/env python3
"""4.3C: frozen CLIP image vectors -> four independent logistic regressions.

Run only with ml_stage4/.venv/Scripts/python.exe. --validate-only checks frozen
inputs without fitting or evaluating. A normal run refuses existing experiment
artifacts: the held-out test set is evaluated only after all choices are saved.
No downloads, CLIP fitting, split edits, text features, or other classifiers.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, confusion_matrix,
                             precision_recall_fscore_support)

BASE = Path(__file__).resolve().parents[1]
SPLIT_PATH = BASE / "reports/dataset_split.csv"
READINESS_PATH = BASE / "outputs/train_readiness.md"
FEATURE_PATH = BASE / "features/clip_embeddings.npy"
INDEX_PATH = BASE / "features/feature_index.csv"
MODEL_PATH = BASE / "models/clip_lr_4labels.joblib"
REPORT_PATH = BASE / "reports/clip_lr_results.md"
PREDICTION_PATH = BASE / "reports/test_predictions.csv"
ANNOTATION_DIR = BASE / "data/slideaudit/data/annotations"
MODEL_ID = "openai/clip-vit-base-patch32"
REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
MODEL_HASHES = {
    "config_sha256": "b575ef3c36f2a057fa19e221650105052d61cc9c1a972ec15019c6261ec98770",
    "preprocessor_sha256": "910e70b3956ac9879ebc90b22fb3bc8a75b6a0677814500101a4c072bd7857bd",
    "checkpoint_sha256": "a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f",
}
LABELS = ("Improper Font Sizing", "Occluded Content", "Cluttered Layout", "Content Overflow/Cut-off")
SLUGS = ("improper_font_sizing", "occluded_content", "cluttered_layout", "content_overflow_cutoff")
SUBSETS = ("train", "validation", "test")
COUNTS = {"train": 1512, "validation": 328, "test": 324, "excluded": 236}
POSITIVES = {"train": (234, 244, 173, 161), "validation": (49, 47, 37, 35), "test": (43, 46, 46, 44)}
CS = (0.1, 1.0, 10.0)
CLASS_WEIGHTS = (None, "balanced")
THRESHOLDS = tuple(n / 100 for n in range(5, 96))
SEED = 42


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_environment():
    require(Path(sys.prefix).resolve() == (BASE / ".venv").resolve(),
            "Use the isolated ml_stage4/.venv Python; no other environment is accepted")


def check_readiness_counts():
    """Compare the actual historical 4.3A table, not merely constants in code."""
    text = READINESS_PATH.read_text(encoding="utf-8")
    for j, label in enumerate(LABELS):
        matching = [line for line in text.splitlines()
                    if line.startswith(f"| {label} |") and re.search(r"\|\s*\d+\s*/\s*\d+", line)]
        require(len(matching) == 1, f"4.3A count table missing or duplicated: {label}")
        cells = [part.strip() for part in matching[0].strip().strip("|").split("|")]
        for i, subset in enumerate(SUBSETS, start=1):
            found = re.search(r"(\d+)\s*/\s*(\d+)", cells[i])
            require(found is not None, f"Cannot parse 4.3A label counts: {label}/{subset}")
            expected = (POSITIVES[subset][j], COUNTS[subset] - POSITIVES[subset][j])
            require(tuple(map(int, found.groups())) == expected,
                    f"4.3A report no longer agrees with approved counts: {label}/{subset}")


def load_inputs():
    check_environment()
    require(sha256(SPLIT_PATH) == SPLIT_SHA256, "Frozen dataset_split.csv changed; refusing training")
    split_rows = read_csv(SPLIT_PATH)
    require(len(split_rows) == 2400, "Split must contain 2400 pages")
    by_id = {row["slide_id"]: row for row in split_rows}
    require(len(by_id) == 2400, "Duplicate slide_id in dataset_split.csv")
    require(Counter(row["split"] for row in split_rows) == Counter(COUNTS), "Split counts changed")
    groups = defaultdict(list)
    for row in split_rows:
        require(re.fullmatch(r"slide_\d{4}", row["slide_id"]) is not None, "Invalid slide_id")
        require((row["split"] == "excluded") == (not row["group_id"]), "Invalid excluded/group relationship")
        if row["group_id"]:
            groups[row["group_id"]].append(row)
    require(len(groups) == 541, "Trusted group count must remain 541")
    for gid, rows in groups.items():
        require(len(rows) == 4 and len({r["split"] for r in rows}) == 1,
                f"Same-source group crosses subsets or is incomplete: {gid}")
    check_readiness_counts()

    index = read_csv(INDEX_PATH)
    require(len(index) == 2164, "Feature index must contain exactly 2164 rows")
    required_fields = {"row_index", "slide_id", "group_id", "split", "image_sha256", "model_id", "model_revision", "feature_definition",
                       "feature_sha256", "input_manifest_sha256", "feature_dimension", "dtype", *MODEL_HASHES}
    require(required_fields.issubset(index[0]), "Feature index lacks required provenance fields")
    formal_ids = {sid for sid, row in by_id.items() if row["split"] != "excluded"}
    ids = [row["slide_id"] for row in index]
    require(len(set(ids)) == 2164 and set(ids) == formal_ids, "Feature IDs are duplicated/missing or contain excluded pages")
    require(ids == sorted(ids, key=lambda sid: int(sid[6:])), "Feature row order must be explicit numeric slide_id order")
    require([int(row["row_index"]) for row in index] == list(range(2164)), "Non-contiguous feature row_index")
    require({row["model_revision"] for row in index} == {REVISION}, "Mixed/unapproved CLIP revisions")
    require({row["model_id"] for row in index} == {MODEL_ID}, "Unapproved CLIP model ID")
    require({row["feature_definition"] for row in index} == {"unnormalized projected image_embeds"},
            "Expected raw, unnormalized CLIP image projection features")
    require({row["feature_dimension"] for row in index} == {"512"}, "Unexpected feature dimensions")
    require({row["dtype"] for row in index} == {"float32"}, "Features must be float32")
    feature_hash = sha256(FEATURE_PATH)
    require({row["feature_sha256"] for row in index} == {feature_hash}, "Feature file hash mismatch")
    for field, expected in MODEL_HASHES.items():
        require({row[field] for row in index} == {expected}, f"CLIP 4.3B provenance mismatch: {field}")
    manifest_values = {row["input_manifest_sha256"] for row in index}
    require(len(manifest_values) == 1 and re.fullmatch(r"[0-9a-f]{64}", next(iter(manifest_values))) is not None,
            "Missing or inconsistent input image manifest digest")
    manifest_hash = next(iter(manifest_values))
    manifest = [{"slide_id": row["slide_id"], "image_sha256": row["image_sha256"]} for row in index]
    manifest_payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    require(hashlib.sha256(manifest_payload).hexdigest() == manifest_hash, "Input image manifest digest mismatch")
    identical_images = defaultdict(list)
    for row in index:
        identical_images[row["image_sha256"]].append(row)
    duplicate_sets = [members for members in identical_images.values() if len(members) > 1]
    cross_split_duplicates = [members for members in duplicate_sets if len({r["split"] for r in members}) > 1]
    require(not cross_split_duplicates, "Exact same PNG appears in multiple subsets")

    # Input images and labels are audit keys/supervision only; only X below enters fit().
    y = np.zeros((2164, 4), dtype=np.uint8)
    annotation_digest = hashlib.sha256()
    for i, row in enumerate(index):
        sid = row["slide_id"]
        expected = by_id[sid]
        require((row["group_id"], row["split"]) == (expected["group_id"], expected["split"]),
                f"Feature/split index disagreement: {sid}")
        require(row["split"] in SUBSETS, f"Excluded page in features: {sid}")
        image_path = BASE / "data/images" / f"{sid}.png"
        require(sha256(image_path) == row["image_sha256"], f"PNG changed since extraction: {sid}")
        path = ANNOTATION_DIR / f"{sid}.json"
        raw = path.read_bytes()
        annotation_digest.update(path.name.encode() + b"\0" + raw + b"\0")
        data = json.loads(raw)
        require(type(data.get("slide_id")) is int and data["slide_id"] == int(sid[6:]), f"Annotation ID mismatch: {sid}")
        targets = defaultdict(list)
        require(isinstance(data.get("annotations"), list), f"Invalid annotation list: {sid}")
        for entry in data["annotations"]:
            if entry.get("design_deficiency") in LABELS:
                targets[entry["design_deficiency"]].append(entry.get("response"))
        for j, label in enumerate(LABELS):
            entries = targets[label]
            require(len(entries) == 1 and type(entries[0]) is bool,
                    f"Missing, repeated or non-boolean response: {sid}/{label}")
            y[i, j] = int(entries[0] is True)

    x = np.load(FEATURE_PATH, mmap_mode="r", allow_pickle=False)
    require(x.shape == (2164, 512) and x.dtype == np.float32, "Expected float32 features with shape (2164, 512)")
    require(np.isfinite(x).all(), "NaN or infinity in CLIP features")
    norms = np.linalg.norm(np.asarray(x, dtype=np.float64), axis=1)
    require(np.isfinite(norms).all() and (norms > 1e-12).all(), "Zero/non-finite CLIP feature norm")
    masks = {subset: np.array([r["split"] == subset for r in index], dtype=bool) for subset in SUBSETS}
    for subset, mask in masks.items():
        require(int(mask.sum()) == COUNTS[subset], f"Wrong feature count: {subset}")
        require(tuple(y[mask].sum(axis=0).tolist()) == POSITIVES[subset],
                f"Label positives disagree with 4.3A: {subset}; training stopped, split unchanged")
    provenance = {
        "feature_file": str(FEATURE_PATH.relative_to(BASE)), "feature_sha256": feature_hash,
        "feature_index_sha256": sha256(INDEX_PATH), "input_manifest_sha256": manifest_hash,
        "split_sha256": SPLIT_SHA256, "readiness_report_sha256": sha256(READINESS_PATH),
        "formal_annotations_sha256": annotation_digest.hexdigest(),
        "formal_annotation_manifest_rule": "numeric slide_id order; filename UTF-8 + NUL + raw bytes + NUL",
        "feature_shape": list(x.shape), "feature_dtype": str(x.dtype), "feature_count": len(index),
        "trusted_group_count": len(groups), "same_group_cross_subset_count": 0, "excluded_feature_count": 0,
        "identical_png_sets": len(duplicate_sets), "pages_in_identical_png_sets": sum(map(len, duplicate_sets)),
        "cross_subset_identical_png_sets": len(cross_split_duplicates),
        "counts": {subset: int(mask.sum()) for subset, mask in masks.items()},
        "positive_counts": {subset: y[mask].sum(axis=0).tolist() for subset, mask in masks.items()},
        "model_id": MODEL_ID, "model_revision": REVISION, **MODEL_HASHES,
    }
    return x, y, index, masks, provenance


def l2_rows(x):
    """Stateless per-image L2 normalization, identical for every subset."""
    values = np.array(x, dtype=np.float64, copy=True)
    values /= np.linalg.norm(values, axis=1, keepdims=True)
    require(np.isfinite(values).all(), "Non-finite normalized features")
    return values


def binary_metrics(y_true, scores, threshold):
    predictions = (scores >= threshold).astype(np.uint8)
    tn, fp, fn, tp = map(int, confusion_matrix(y_true, predictions, labels=[0, 1]).ravel())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "average_precision": float(average_precision_score(y_true, scores)),
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "actual_positive": int(np.sum(y_true)), "predicted_positive": int(predictions.sum()),
            "threshold": float(threshold)}


def choose_threshold(y_true, probabilities):
    # Threshold grid and tie-breaking are specified before any test evaluation.
    candidates = [binary_metrics(y_true, probabilities, threshold) for threshold in THRESHOLDS]
    return max(candidates, key=lambda m: (m["f1"], -round(abs(m["threshold"] - .5), 12), m["threshold"]))


def summary_metrics(y_true, probabilities, thresholds):
    predictions = (probabilities >= np.asarray(thresholds)[None, :]).astype(np.uint8)
    result = {"per_label": {label: binary_metrics(y_true[:, j], probabilities[:, j], thresholds[j])
                             for j, label in enumerate(LABELS)}}
    for average in ("macro", "micro"):
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_true, predictions, average=average, zero_division=0)
        result[average] = {"precision": float(precision), "recall": float(recall), "f1": float(f1)}
    result["macro_average_precision"] = float(np.mean([m["average_precision"] for m in result["per_label"].values()]))
    return result, predictions


def save_bundle(bundle):
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_PATH.with_suffix(".joblib.part")
    joblib.dump(bundle, temporary, compress=3)
    os.replace(temporary, MODEL_PATH)


def validation_selection(x, y, masks):
    train_x, valid_x = l2_rows(x[masks["train"]]), l2_rows(x[masks["validation"]])
    train_y, valid_y = y[masks["train"]], y[masks["validation"]]
    models, selections, trials = [], [], []
    chosen_valid_probabilities = np.empty((COUNTS["validation"], 4), dtype=np.float64)
    for j, label in enumerate(LABELS):
        candidates = []
        for c in CS:
            for class_weight in CLASS_WEIGHTS:
                classifier = LogisticRegression(C=c, class_weight=class_weight, solver="lbfgs",
                                                max_iter=3000, tol=1e-6, random_state=SEED)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ConvergenceWarning)
                    classifier.fit(train_x, train_y[:, j])
                convergence = [str(w.message) for w in caught if issubclass(w.category, ConvergenceWarning)]
                require(not convergence, f"LR failed to converge: {label}, C={c}, class_weight={class_weight}")
                require(np.isfinite(classifier.coef_).all() and np.isfinite(classifier.intercept_).all(),
                        f"Non-finite fitted LR coefficients: {label}")
                require(classifier.classes_.tolist() == [0, 1], f"Wrong classifier class order: {label}")
                probabilities = classifier.predict_proba(valid_x)[:, 1]
                chosen = choose_threshold(valid_y[:, j], probabilities)
                default = binary_metrics(valid_y[:, j], probabilities, .5)
                trial = {"label": label, "C": c, "class_weight": class_weight,
                         "validation_selected_threshold": chosen, "validation_default_0_5": default,
                         "n_iter": classifier.n_iter_.tolist(), "selected": False}
                key = (chosen["f1"], chosen["average_precision"], -c,
                       0 if class_weight is None else -1,
                       -round(abs(chosen["threshold"] - .5), 12), chosen["threshold"])
                candidates.append((key, classifier, probabilities, trial))
                trials.append(trial)
        _, selected_model, probabilities, selected = max(candidates, key=lambda entry: entry[0])
        selected["selected"] = True
        models.append(selected_model)
        selections.append({"label": label, "C": selected["C"], "class_weight": selected["class_weight"],
                           "threshold": selected["validation_selected_threshold"]["threshold"],
                           "validation": selected["validation_selected_threshold"],
                           "validation_default_0_5": selected["validation_default_0_5"]})
        chosen_valid_probabilities[:, j] = probabilities
        print(json.dumps({"event": "validation_choice_frozen", **selections[-1]}, ensure_ascii=False), flush=True)
    thresholds = [selection["threshold"] for selection in selections]
    chosen, _ = summary_metrics(valid_y, chosen_valid_probabilities, thresholds)
    default, _ = summary_metrics(valid_y, chosen_valid_probabilities, [.5] * 4)
    return models, selections, trials, {"selected_thresholds": chosen, "default_0_5": default}


def save_predictions(index, test_mask, y_true, probabilities, predictions):
    fields = ["slide_id", "group_id"] + [f"{kind}_{slug}" for kind in ("true", "prob", "pred") for slug in SLUGS]
    temporary = PREDICTION_PATH.with_suffix(".csv.part")
    PREDICTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i, row in enumerate([r for r, use in zip(index, test_mask) if use]):
            record = {"slide_id": row["slide_id"], "group_id": row["group_id"]}
            for j, slug in enumerate(SLUGS):
                record[f"true_{slug}"] = int(y_true[i, j])
                record[f"prob_{slug}"] = format(float(probabilities[i, j]), ".17g")
                record[f"pred_{slug}"] = int(predictions[i, j])
            writer.writerow(record)
    os.replace(temporary, PREDICTION_PATH)


def metric_row(label, metric):
    return (f"| {label} | {metric['precision']:.4f} | {metric['recall']:.4f} | {metric['f1']:.4f} | "
            f"{metric['average_precision']:.4f} | {metric['tp']} | {metric['fp']} | {metric['tn']} | {metric['fn']} | "
            f"{metric['actual_positive']} | {metric['predicted_positive']} |")


def save_report(bundle):
    provenance, selected = bundle["provenance"], bundle["selections"]
    require(sha256(INDEX_PATH) == provenance["feature_index_sha256"], "Saved feature index changed before report rendering")
    feature_rows = read_csv(INDEX_PATH)
    require(len(feature_rows) == provenance["feature_count"], "Saved feature index count changed before report rendering")
    extraction_counts = Counter((row["extraction_device"], row["batch_size"]) for row in feature_rows)
    extraction_summary = "；".join(
        f"{'GPU ' if device.startswith('cuda') else ''}`{device}`、batch_size={batch_size}、{count:,} 张"
        for (device, batch_size), count in sorted(extraction_counts.items()))
    test_selected = bundle["test_metrics"]["selected_thresholds"]
    test_default = bundle["test_metrics"]["default_0_5"]
    test_labels = test_selected["per_label"]
    ranked = sorted(LABELS, key=lambda label: test_labels[label]["f1"], reverse=True)
    font, occluded, cluttered, overflow = [test_labels[label] for label in LABELS]
    lines = [
        "# 第四阶段 4.3C：CLIP 四标签逻辑回归首次实验", "",
        f"完成时间：{bundle['completed_at']}。实际执行 2,164 张 PNG 特征提取、24 次 train-only 二分类拟合、验证集选择，以及冻结后的测试集一次正式评估。", "",
        "## 1. 数据、特征与监督核对", "",
        "沿用已验收的 541 个可信四页组。train/validation/test 为 1,512/328/324 页，来源组为 378/82/81；236 页 excluded 未提取特征、未参与拟合、选择或评估。同源 group_id 跨集合数为 0。", "",
        f"CLIP：`{MODEL_ID}`，固定 revision `{REVISION}`。沿用 4.3B 相同权重和官方预处理器，仅使用冻结的视觉编码器与图像投影，RGB、eval、inference_mode；无文本输入、无反向传播或微调。", "",
        "全部 2,164 张图像提取成功，特征形状 **(2164, 512)**、float32，所有数值有限且无零向量。feature_index 显式记录行号、slide_id 和来源摘要；X/y 按相同索引构造，非文件系统遍历顺序。标签、缺陷名、bbox、source_type、split 和 slide_id 均未进入 X。", "",
        f"feature_index 中实际设备与批量记录：{extraction_summary}；全部成功。此处读取已保存索引汇总，没有重新提取特征。", "",
        "标签仅来自官方 annotations，每个目标恰好一条严格布尔 response；true=1、false=0。训练前再次核对 4.3A 报告及固定计数，全部一致：", "",
        "| 标签 | train 正/负 | validation 正/负 | test 正/负 |", "| --- | --- | --- | --- |",
    ]
    for j, label in enumerate(LABELS):
        cells = [f"{POSITIVES[s][j]}/{COUNTS[s]-POSITIVES[s][j]}" for s in SUBSETS]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "## 2. 模型拟合与验证集选择", "",
              "每个标签独立比较 C∈{0.1,1,10} × class_weight∈{None,balanced}，共 6 配置/标签、24 次拟合。使用 scikit-learn LogisticRegression、L2 正则、lbfgs、max_iter=3000、tol=1e-6、random_state=42。全部收敛，无额外分类器。", "",
              "特征先转换为 float64，逐行除以该行 L2 范数；这是固定、无拟合的归一化，三集合一致。未在全量数据拟合标准化、PCA 或其他预处理。所有分类器只在 1,512 页 train 上 fit，选中的是已拟合候选，不在 train+validation 上重拟合。", "",
              "预先固定阈值网格 0.05–0.95，步长 0.01；预测规则为 probability≥threshold。每配置选 validation F1 最大阈值，同分选距 0.5 更近、再选较大阈值。跨配置按 validation F1、validation Average Precision、更小 C、优先 None、阈值距 0.5 更近、较大阈值依次决定。没有读取测试预测来选择参数、阈值或标签。", "",
              "四个模型与阈值全部写入模型文件冻结后，才进行 test predict_proba；该次概率同时用于最终阈值和同模型默认 0.5 对照。测试未参与任何拟合或选择。本脚本默认拒绝覆盖已有正式结果，避免重测调参。", "",
              "| 标签 | C | class_weight | 最终阈值 | validation F1 | validation AP |", "| --- | --- | --- | --- | --- | --- |"]
    for choice in selected:
        lines.append(f"| {choice['label']} | {choice['C']:g} | {choice['class_weight']} | {choice['threshold']:.2f} | {choice['validation']['f1']:.4f} | {choice['validation']['average_precision']:.4f} |")
    lines += ["", "完整小范围选择记录（仅 validation；★为该标签选中配置）：", "",
              "| 标签 | C | class_weight | 选定阈值 | val P | val R | val F1 | val AP | val F1@0.5 | 选中 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for trial in bundle["validation_trials"]:
        metric = trial["validation_selected_threshold"]
        lines.append(f"| {trial['label']} | {trial['C']:g} | {trial['class_weight']} | {metric['threshold']:.2f} | {metric['precision']:.4f} | {metric['recall']:.4f} | {metric['f1']:.4f} | {metric['average_precision']:.4f} | {trial['validation_default_0_5']['f1']:.4f} | {'★' if trial['selected'] else ''} |")
    lines += ["", "## 3. 测试集一次正式评估", "",
              "以下为 324 页 test 的页面级结果；所有困难负例与四标签全负页面均保留。PR-AUC 使用 **Average Precision (AP)** 定义，按连续预测概率计算，不是梯形积分；AP 不随分类阈值改变。分母为 0 的 Precision/Recall/F1 记 0。", "",
              "验证集选择阈值：", "",
              "| 标签 | Precision | Recall | F1 | AP | TP | FP | TN | FN | 实际正例 | 预测正例 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(metric_row(label, test_labels[label]) for label in LABELS)
    lines += ["", "同一批冻结模型，默认阈值 0.5 对照：", "",
              "| 标签 | Precision | Recall | F1 | AP | TP | FP | TN | FN | 实际正例 | 预测正例 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(metric_row(label, test_default["per_label"][label]) for label in LABELS)
    lines += ["", "| 集合/阈值 | Macro P | Macro R | Macro F1 | Micro P | Micro R | Micro F1 | Macro AP |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, result in (("validation/选择阈值", bundle["validation_metrics"]["selected_thresholds"]),
                         ("validation/0.5", bundle["validation_metrics"]["default_0_5"]),
                         ("test/选择阈值", test_selected), ("test/0.5", test_default)):
        values = [result["macro"][m] for m in ("precision", "recall", "f1")] + [result["micro"][m] for m in ("precision", "recall", "f1")] + [result["macro_average_precision"]]
        lines.append(f"| {name} | " + " | ".join(f"{value:.4f}" for value in values) + " |")
    lines += ["", "混淆矩阵约定为 `[[TN, FP], [FN, TP]]`，各标签表中给出完整统计。", "", "## 4. 结果解释、限制与后续集成判断", "",
              f"本次 test F1 从高到低为：" + "、".join(f"{label}={test_labels[label]['f1']:.4f}" for label in ranked) + "。这是对冻结实验结果的描述，不用于重新选择模型。",
              f"Cluttered Layout 的 F1={cluttered['f1']:.4f}、Precision={cluttered['precision']:.4f}、Recall={cluttered['recall']:.4f}，在本次四类中相对最好，但仍有 {cluttered['fp']} 页误报、{cluttered['fn']} 页漏检。Content Overflow/Cut-off 的 F1={overflow['f1']:.4f}，{overflow['actual_positive']} 个正例仍漏掉 {overflow['fn']} 个，尚不足以可靠发现边缘裁切。", "",
              f"Improper Font Sizing 的 Precision={font['precision']:.4f}、AP={font['average_precision']:.4f}，接近其 test 正例率 {font['actual_positive']/COUNTS['test']:.4f}，且误报 {font['fp']} 页，当前全局图像特征对这一标签的识别能力很弱。Occluded Content 的 Recall={occluded['recall']:.4f}，{occluded['actual_positive']} 个正例漏掉 {occluded['fn']} 个，漏检问题突出。", "",
              "四个最终阈值下的 F1 均低于 0.5，整体表现不足以支持可靠自动审查。默认 0.5 对照中部分标签的 test F1 更高，这只是同一批冻结概率的事后对照；本次仍保留 validation 选定的模型与阈值，未据此重选或重训。", "",
              "同源增强版本相互相关，虽然组不跨集合，这些页面级指标仍不能等同于 324 个独立原始页面的指标；test 仅有 81 个来源组，四标签正例分别为 43/46/46/44 页，验证正例也少，阈值和指标有采样波动。本次未将标签计数均衡化，未删除困难负例。", "",
              f"正式图像还有 {provenance['identical_png_sets']} 组完全相同 PNG，涉及 {provenance['pages_in_identical_png_sets']} 页；跨集合精确重复为 0。保留这些已冻结的增强样本，不去重、更不据此删除负例；它们进一步说明页面样本之间并非全部独立。", "",
              "官方图像处理器将图像短边缩放至 224 像素，再中心裁剪为 224×224；对于宽屏 PPT，左右边缘可能被裁去，小字号细节也会缩小。全局 512 维 CLIP 向量对小字号、边缘裁切、局部遮挡和细粒度排版可能不敏感。相关标签表现较弱时，应把此表征限制作为后续研究假设，不直接断定标注或程序错误；本次未据测试结果更改实验规则。", "",
              "**集成判断：技术链路已打通，但现有准确性不足以支持可靠自动审查；最多作为未来人工复核辅助原型的研究基础，当前不接入第三阶段智能体。** 后续应在独立阶段预先设计验收标准，检查误报/漏检与独立数据泛化；在新实验中研究细粒度视觉信息，同时保留已有规则和按需复核机制。测试集已用于本次正式评估，今后不可反复据其分数调整本实验。", "",
              "## 5. 保存与复现", "",
              "- 模型：`models/clip_lr_4labels.joblib`；含四模型、标签顺序、阈值、固定归一化规则、CLIP revision/文件摘要、24 配置验证记录、测试评估状态和指标。",
              "- 特征：`features/clip_embeddings.npy`、`features/feature_index.csv`；没有重复图片或模型权重。",
              "- 测试逐页结果：`reports/test_predictions.csv`；四个 true/prob/pred 列按标签缩写对应。",
              "- `--validate-only` 可重新核对特征、标签、固定划分，既不拟合也不重评 test；正常执行默认拒绝已有模型/报告/预测文件。", "",
              "| CSV 列后缀 | 官方标签 |", "| --- | --- |"]
    lines.extend(f"| {slug} | {label} |" for slug, label in zip(SLUGS, LABELS))
    lines += ["", "复现摘要：", ""]
    for field in ("model_revision", "checkpoint_sha256", "config_sha256", "preprocessor_sha256", "input_manifest_sha256", "feature_sha256", "feature_index_sha256", "split_sha256", "formal_annotations_sha256", "readiness_report_sha256"):
        lines.append(f"- {field}: `{provenance[field]}`")
    lines += [f"- saved_model_sha256: `{sha256(MODEL_PATH)}`", f"- test_predictions_sha256: `{sha256(PREDICTION_PATH)}`",
              "- 输入清单摘要：数值 slide_id 升序的 [{slide_id,image_sha256}]，UTF-8 JSON（sort_keys=True、ensure_ascii=False、紧凑逗号/冒号）后 SHA256；feature_index 中逐页 PNG SHA256 已重新验证。",
              "- annotations 摘要：按数值 slide_id 排序，每页追加 UTF-8 文件名、NUL、原始 JSON 字节、NUL。", "",
              "实际运行环境：`" + json.dumps(bundle["environment"], ensure_ascii=False, sort_keys=True) + "`。", ""]
    temporary = REPORT_PATH.with_suffix(".md.part")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, REPORT_PATH)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true", help="Check frozen X/y only; do not train or evaluate")
    args = parser.parse_args()
    if not args.validate_only:
        existing = [str(path.relative_to(BASE)) for path in (MODEL_PATH, REPORT_PATH, PREDICTION_PATH) if path.exists()]
        require(not existing, "Existing experiment artifacts prevent another official test evaluation: " + ", ".join(existing))
    started = now()
    x, y, index, masks, provenance = load_inputs()
    print(json.dumps({"event": "supervision_checks_passed", **provenance}, ensure_ascii=False), flush=True)
    if args.validate_only:
        return
    models, selections, trials, validation = validation_selection(x, y, masks)
    bundle = {
        "format_version": 1, "experiment": "4.3C-first-clip-lr", "started_at": started,
        "model_type": "four independent sklearn.linear_model.LogisticRegression", "models": models,
        "label_order": list(LABELS), "csv_label_slugs": list(SLUGS),
        "thresholds": [s["threshold"] for s in selections], "positive_class": 1,
        "prediction_rule": "probability >= per-label threshold; predict_proba class 1",
        "preprocessing": {"name": "fixed_per_sample_l2", "input": "raw float32 CLIP projected image_embeds",
                          "operation": "cast float64; x / sqrt(sum(x*x, axis=1, keepdims=True))",
                          "fitted": False, "normalized_dtype": "float64", "input_dimension": 512,
                          "zero_vectors": "error"},
        "provenance": provenance, "selections": selections, "validation_trials": trials,
        "validation_metrics": validation,
        "selection_rule": {"C_grid": list(CS), "class_weight_grid": list(CLASS_WEIGHTS),
                           "threshold_grid": list(THRESHOLDS), "objective": "validation F1",
                           "within_config_ties": "closest to 0.5, then larger threshold",
                           "between_config_ties": "validation AP, smaller C, None, closest to 0.5, larger threshold"},
        "fit_subsets": ["train"], "parameter_selection_subsets": ["validation"], "test_selection_used": False,
        "pre_training_test_label_access": "aggregate label-count audit required by 4.3A; not used in selection",
        "refit_on_validation": False, "clip_training_performed": False, "classifier_fit_count": len(trials),
        "random_seed": SEED, "test_evaluation_status": "choices_frozen_before_test",
        "test_evaluation_count": 0,
        "environment": {"python": platform.python_version(), "executable": sys.executable,
                        **{package: importlib.metadata.version(package) for package in ("numpy", "scikit-learn", "joblib", "torch", "transformers")}},
    }
    save_bundle(bundle)  # Persist selected train-fitted models before accessing test predictions.
    bundle["test_evaluation_status"] = "evaluation_started_do_not_repeat"
    bundle["test_evaluation_started_at"] = now()
    bundle["test_evaluation_count"] = 1
    save_bundle(bundle)  # Durable guard if interrupted after test inference begins.
    test_x = l2_rows(x[masks["test"]])
    test_y = y[masks["test"]]
    probabilities = np.column_stack([model.predict_proba(test_x)[:, 1] for model in models])
    require(probabilities.shape == (324, 4) and np.isfinite(probabilities).all(), "Invalid test probabilities")
    selected, predictions = summary_metrics(test_y, probabilities, bundle["thresholds"])
    default, _ = summary_metrics(test_y, probabilities, [.5] * 4)
    bundle["test_metrics"] = {"selected_thresholds": selected, "default_0_5": default}
    bundle["test_evaluation_status"] = "completed_once_choices_unchanged"
    bundle["completed_at"] = now()
    save_bundle(bundle)
    save_predictions(index, masks["test"], test_y, probabilities, predictions)
    save_report(bundle)
    require(sha256(SPLIT_PATH) == SPLIT_SHA256, "Frozen split changed during execution")
    print(json.dumps({"event": "completed", "test_evaluation_count": 1,
                      "macro_f1": selected["macro"]["f1"], "micro_f1": selected["micro"]["f1"],
                      "test_metrics": selected, "model": str(MODEL_PATH), "report": str(REPORT_PATH)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
