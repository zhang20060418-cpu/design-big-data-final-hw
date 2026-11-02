#!/usr/bin/env python3
"""4.3D: train/validation-only center-crop versus full-slide CLIP comparison.

The old feature array is memory-mapped and only explicitly selected train/val
rows are copied. No test PNGs, test annotations, saved LR model, old evaluation
report, or test predictions are opened. Models remain in memory. The export
option reconstructs eight already-selected classifiers and verifies the saved
validation CSV against the existing report without repeating model selection.
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
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression

# Import pure helpers only. The imported module has an if-main guard; none of
# load_inputs/main/save_report/save_bundle or its test-reading paths is called.
from train_clip_lr import (
    BASE, COUNTS, LABELS, SLUGS, SEED, MODEL_HASHES, MODEL_ID, REVISION, SPLIT_PATH,
    SPLIT_SHA256, check_environment, l2_rows, now, read_csv, require, sha256,
    summary_metrics, validation_selection,
)
from extract_full_slide_features import RECIPE, install_test_guard, json_digest

INDEX_PATH = BASE / "features/full_slide_feature_index.csv"
FEATURE_PATH = BASE / "features/clip_embeddings_full_slide_trainval.npy"
BASELINE_INDEX_PATH = BASE / "features/feature_index.csv"
BASELINE_FEATURE_PATH = BASE / "features/clip_embeddings.npy"
ANNOTATION_DIR = BASE / "data/slideaudit/data/annotations"
METADATA_PATH = BASE / "data/slideaudit/data/metadata.csv"
READINESS_PATH = BASE / "outputs/train_readiness.md"
REPORT_PATH = BASE / "reports/full_slide_comparison.md"
PREDICTION_PATH = BASE / "reports/validation_predictions.csv"
PREPROCESSOR_PATH = (BASE / "models/clip_encoder/preprocessor_config.json")
SUBSETS = ("train", "validation")
POSITIVES = {"train": (234, 244, 173, 161), "validation": (49, 47, 37, 35)}
MIXED_GROUPS = (29, 33, 26, 22)
MIXED_PAIRS = (97, 107, 85, 69)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def manifest_sha(rows):
    values = [{"slide_id": row["slide_id"], "image_sha256": row["image_sha256"]}
              for row in rows]
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def array_sha(array):
    """Digest selected row values only, never the old full feature file."""
    return hashlib.sha256(np.ascontiguousarray(array).tobytes(order="C")).hexdigest()


def check_readiness_trainval():
    text = READINESS_PATH.read_text(encoding="utf-8")
    for j, label in enumerate(LABELS):
        matches = [line for line in text.splitlines()
                   if line.startswith(f"| {label} |")
                   and re.search(r"\|\s*\d+\s*/\s*\d+", line)]
        require(len(matches) == 1, f"Cannot identify approved 4.3A label row: {label}")
        cells = [part.strip() for part in matches[0].strip().strip("|").split("|")]
        # Deliberately inspect only train and validation cells, not test labels.
        for k, subset in enumerate(SUBSETS, start=1):
            found = re.search(r"(\d+)\s*/\s*(\d+)", cells[k])
            expected = (POSITIVES[subset][j], COUNTS[subset] - POSITIVES[subset][j])
            require(found is not None and tuple(map(int, found.groups())) == expected,
                    f"4.3A train/validation approved count changed: {label}/{subset}")


def load_inputs():
    check_environment()
    require(sha256(SPLIT_PATH) == SPLIT_SHA256, "Frozen split changed; do not regenerate it")
    split = read_csv(SPLIT_PATH)
    install_test_guard(split)
    require(len(split) == 2400 and len({r["slide_id"] for r in split}) == 2400,
            "Frozen split is incomplete or has duplicate slide IDs")
    require(Counter(r["split"] for r in split) == Counter(COUNTS), "Frozen subset counts changed")
    grouped = defaultdict(list)
    for row in split:
        require(re.fullmatch(r"slide_\d{4}", row["slide_id"]) is not None, "Invalid slide ID")
        require((row["split"] == "excluded") == (not row["group_id"]), "Invalid group/excluded metadata")
        if row["group_id"]:
            grouped[row["group_id"]].append(row)
    require(len(grouped) == 541, "Trusted group count changed")
    require(all(len(rows) == 4 and len({r["split"] for r in rows}) == 1
                for rows in grouped.values()), "A group is incomplete or crosses subsets")
    target = sorted([r for r in split if r["split"] in SUBSETS], key=lambda r: int(r["slide_id"][6:]))
    target_by_id = {r["slide_id"]: r for r in target}
    require(len(target) == 1840, "Expected exactly 1840 train/validation pages")
    check_readiness_trainval()

    # Old index is provenance metadata only. No old test reports/model are read.
    old_index = read_csv(BASELINE_INDEX_PATH)
    require(len(old_index) == 2164, "Unexpected baseline feature index length")
    require([int(r["row_index"]) for r in old_index] == list(range(2164)),
            "Baseline feature index row mapping changed")
    old_selected = [r for r in old_index if r["split"] in SUBSETS]
    require(len(old_selected) == 1840 and len({r["slide_id"] for r in old_selected}) == 1840,
            "Baseline train/validation index is incomplete or duplicated")
    old_by_id = {r["slide_id"]: r for r in old_selected}
    require(set(old_by_id) == set(target_by_id), "Baseline train/validation IDs disagree with split")

    index = read_csv(INDEX_PATH)
    require(len(index) == 1840, "Full-slide index must contain 1840 rows")
    required_fields = {"row_index", "baseline_row_index", "slide_id", "group_id", "split",
                       "image_sha256", "model_revision", "feature_sha256", "input_manifest_sha256",
                       "recipe_sha256", "padding_rgb", "interpolation", "extraction_device",
                       "feature_dimension", "dtype", "feature_definition", "original_width", "original_height",
                       "content_width", "content_height", "pad_left", "pad_top", "pad_right", "pad_bottom",
                       *MODEL_HASHES}
    require(required_fields.issubset(index[0]), "New feature index lacks required provenance")
    require([r["slide_id"] for r in index] == [r["slide_id"] for r in target],
            "Full-slide IDs are missing, duplicated, excluded/test, or not explicitly ordered")
    require([int(r["row_index"]) for r in index] == list(range(1840)), "Invalid new row indexes")
    require({r["model_revision"] for r in index} == {REVISION}, "Mixed/unapproved new model revisions")
    require({r["feature_dimension"] for r in index} == {"512"}
            and {r["dtype"] for r in index} == {"float32"}, "Unexpected new feature definition")
    require({r["feature_definition"] for r in index} == {"unnormalized projected image_embeds"},
            "Expected unnormalized projected image embeddings")
    require(all("bicubic" in r["interpolation"].lower() for r in index), "Expected bicubic resizing")
    require(all(tuple(map(int, re.findall(r"\d+", r["padding_rgb"]))) == (255, 255, 255)
                for r in index), "Full-slide padding must remain fixed white RGB")
    for field, expected in MODEL_HASHES.items():
        require({r[field] for r in index} == {expected}, f"New CLIP artifact differs: {field}")
    for row in index:
        sid = row["slide_id"]
        approved, old = target_by_id[sid], old_by_id[sid]
        require(row["split"] in SUBSETS, "Test/excluded row in new features")
        require((row["group_id"], row["split"]) == (approved["group_id"], approved["split"]),
                f"New feature/split disagreement: {sid}")
        require((old["group_id"], old["split"]) == (approved["group_id"], approved["split"]),
                f"Baseline feature/split disagreement: {sid}")
        require(int(row["baseline_row_index"]) == int(old["row_index"]),
                f"Baseline row mapping disagrees: {sid}")
        require(row["image_sha256"] == old["image_sha256"]
                and SHA256_PATTERN.fullmatch(row["image_sha256"]) is not None,
                f"Two input PNG identities disagree: {sid}")
        require(old["model_id"] == MODEL_ID and old["model_revision"] == REVISION,
                f"Baseline CLIP provenance changed: {sid}")
        require(old["feature_definition"] == "unnormalized projected image_embeds"
                and old["feature_dimension"] == "512" and old["dtype"] == "float32",
                f"Baseline feature definition changed: {sid}")
        for field, expected in MODEL_HASHES.items():
            require(old[field] == expected, f"Baseline CLIP artifact mismatch: {sid}/{field}")
    for field in ("recipe_sha256", "input_manifest_sha256", "feature_sha256"):
        values = {r[field] for r in index}
        require(len(values) == 1 and SHA256_PATTERN.fullmatch(next(iter(values))) is not None,
                f"Missing/inconsistent new feature digest: {field}")
    require({r["recipe_sha256"] for r in index} == {json_digest(RECIPE)},
            "Full-slide recipe differs from the frozen extraction implementation")
    require({r["input_manifest_sha256"] for r in index} == {manifest_sha(index)},
            "New input image manifest digest mismatch")
    new_feature_sha = sha256(FEATURE_PATH)  # New file contains train/validation only.
    require({r["feature_sha256"] for r in index} == {new_feature_sha}, "New feature file hash mismatch")

    # Slice before any finite/norm/summary check: no test row value is accessed.
    selected_positions = np.array([int(r["baseline_row_index"]) for r in index], dtype=np.int64)
    old_mmap = np.load(BASELINE_FEATURE_PATH, mmap_mode="r", allow_pickle=False)
    require(old_mmap.shape == (2164, 512) and old_mmap.dtype == np.float32, "Unexpected baseline array header")
    require(len(set(selected_positions.tolist())) == 1840
            and (selected_positions >= 0).all() and (selected_positions < 2164).all(), "Invalid baseline selected positions")
    baseline = np.array(old_mmap[selected_positions], dtype=np.float32, copy=True)
    del old_mmap
    full_slide = np.load(FEATURE_PATH, allow_pickle=False)
    for name, x in (("center_crop", baseline), ("full_slide", full_slide)):
        require(x.shape == (1840, 512) and x.dtype == np.float32, f"Unexpected {name} feature shape/dtype")
        require(np.isfinite(x).all(), f"NaN/Inf in {name} train/validation features")
        norms = np.linalg.norm(x.astype(np.float64), axis=1)
        require(np.isfinite(norms).all() and (norms > 1e-12).all(), f"Invalid {name} feature norms")

    y = np.zeros((1840, 4), dtype=np.uint8)
    annotation_digest = hashlib.sha256()
    for i, row in enumerate(index):
        sid = row["slide_id"]
        # By construction, only train/validation paths can reach this point.
        require(row["split"] in SUBSETS, f"Refusing annotation outside train/validation: {sid}")
        path = ANNOTATION_DIR / f"{sid}.json"
        raw = path.read_bytes()
        annotation_digest.update(path.name.encode("utf-8") + b"\0" + raw + b"\0")
        data = json.loads(raw)
        require(type(data.get("slide_id")) is int and data["slide_id"] == int(sid[6:]), f"Annotation ID mismatch: {sid}")
        require(isinstance(data.get("annotations"), list), f"Invalid annotations: {sid}")
        responses = defaultdict(list)
        for entry in data["annotations"]:
            require(isinstance(entry, dict), f"Invalid annotation entry: {sid}")
            if entry.get("design_deficiency") in LABELS:
                responses[entry["design_deficiency"]].append(entry.get("response"))
        for j, label in enumerate(LABELS):
            values = responses[label]
            require(len(values) == 1 and type(values[0]) is bool, f"Missing/duplicate/illegal response: {sid}/{label}")
            y[i, j] = int(values[0] is True)
    masks = {subset: np.array([r["split"] == subset for r in index]) for subset in SUBSETS}
    for subset, mask in masks.items():
        require(int(mask.sum()) == COUNTS[subset], f"Wrong page count: {subset}")
        require(tuple(y[mask].sum(axis=0).tolist()) == POSITIVES[subset],
                f"Label positives disagree with approved 4.3A counts: {subset}; stopping without split changes")
    require(sha256(PREPROCESSOR_PATH) == MODEL_HASHES["preprocessor_sha256"], "Pinned preprocessing config changed")
    # Original HF config uses legacy scalar size/crop_size and omits rescale
    # defaults. Instantiate the identical local processor to inspect its actual
    # canonical rules rather than assume the raw JSON already has modern keys.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", HF_HOME=str(BASE / ".cache/huggingface"))
    try:
        from transformers import CLIPImageProcessorPil as CLIPImageProcessor
    except ImportError:
        from transformers import CLIPImageProcessor
    processor = CLIPImageProcessor.from_pretrained(str(PREPROCESSOR_PATH.parent),
                                                  local_files_only=True).to_dict()
    require(processor["do_resize"] is True and processor["do_center_crop"] is True
            and processor["size"] == {"shortest_edge": 224}
            and processor["crop_size"] == {"height": 224, "width": 224}, "Baseline crop configuration changed")
    metadata = {f"slide_{r['id']}": r for r in read_csv(METADATA_PATH)
                if f"slide_{r['id']}" in target_by_id}
    require(len(metadata) == 1840, "Missing train/validation metadata")
    for row in index:
        meta = metadata[row["slide_id"]]
        w, h = int(meta["image_width"]), int(meta["image_height"])
        require((int(row["original_width"]), int(row["original_height"])) == (w, h),
                f"Full-slide input geometry differs from metadata: {row['slide_id']}")
        scale = 224 / max(w, h)
        cw, ch = round(w * scale), round(h * scale)
        left, top = (224 - cw) // 2, (224 - ch) // 2
        geometry = tuple(int(row[field]) for field in ("content_width", "content_height", "pad_left", "pad_top", "pad_right", "pad_bottom"))
        require(geometry == (cw, ch, left, top, 224-cw-left, 224-ch-top),
                f"Full-slide fit/padding geometry mismatch: {row['slide_id']}")
    size_counts = Counter((int(metadata[r["slide_id"]]["image_width"]),
                           int(metadata[r["slide_id"]]["image_height"])) for r in index)
    provenance = {
        "model_id": MODEL_ID, "model_revision": REVISION, **MODEL_HASHES,
        "split_sha256": SPLIT_SHA256, "new_feature_sha256": new_feature_sha,
        "new_feature_index_sha256": sha256(INDEX_PATH),
        "input_trainval_manifest_sha256": manifest_sha(index),
        "baseline_selected_trainval_values_sha256": array_sha(baseline),
        "baseline_selected_values_digest_rule": "1840 numeric-slide-ID rows, float32 C-order raw bytes; not the .npy file",
        "baseline_index_sha256": sha256(BASELINE_INDEX_PATH),
        "recipe_sha256": index[0]["recipe_sha256"],
        "trainval_annotations_sha256": annotation_digest.hexdigest(),
        "annotation_digest_rule": "numeric-slide-ID order; UTF-8 filename + NUL + raw JSON bytes + NUL",
        "counts": {s: int(mask.sum()) for s, mask in masks.items()},
        "positives": {s: y[mask].sum(axis=0).tolist() for s, mask in masks.items()},
        "feature_shape": list(full_slide.shape), "feature_dtype": str(full_slide.dtype),
        "cross_subset_groups": 0, "test_feature_values_accessed": 0,
        "test_images_opened": 0, "test_annotations_opened": 0,
        "extraction_devices": dict(Counter(r["extraction_device"] for r in index)),
        "trainval_image_sizes": [{"width": w, "height": h, "count": count}
                                  for (w, h), count in sorted(size_counts.items())],
        "processor_config": processor,
    }
    return baseline, full_slide, y, index, masks, provenance


def mixed_group_diagnostics(y, probabilities, predictions, index):
    """Compare positive vs negative augmented pages within each mixed group.

    This is a conditional ranking diagnostic, not 82 independent-page APs. Each
    eligible positive/negative pair gets 1/0.5/0 for right/tie/wrong score order.
    """
    groups = defaultdict(list)
    for i, row in enumerate(index):
        require(row["split"] == "validation", "Mixed-group diagnostic requires validation only")
        groups[row["group_id"]].append(i)
    require(len(groups) == 82 and all(len(rows) == 4 for rows in groups.values()),
            "Validation must contain 82 intact four-page groups")
    results = {}
    for j, label in enumerate(LABELS):
        total_pairs = ties = correctly_ranked = pair_both_correct = exact_groups = 0
        aucs, gaps = [], []
        details = []
        for gid, positions in sorted(groups.items()):
            positions = np.array(positions, dtype=np.int64)
            truth = y[positions, j]
            if truth.min() == truth.max():
                continue
            scores = probabilities[positions, j]
            predicted = predictions[positions, j]
            p, n = scores[truth == 1], scores[truth == 0]
            comparisons = p[:, None] - n[None, :]
            pairs = int(comparisons.size)
            wins = int((comparisons > 0).sum())
            tied = int((comparisons == 0).sum())
            both = int(((predicted[truth == 1][:, None] == 1)
                        & (predicted[truth == 0][None, :] == 0)).sum())
            exact = bool(np.array_equal(predicted, truth))
            auc = (wins + .5 * tied) / pairs
            gap = float(p.mean() - n.mean())
            total_pairs += pairs
            ties += tied
            correctly_ranked += wins
            pair_both_correct += both
            exact_groups += int(exact)
            aucs.append(auc)
            gaps.append(gap)
            details.append({"group_id": gid, "positive_count": int(truth.sum()),
                            "negative_count": int((truth == 0).sum()), "pairs": pairs,
                            "ranking_auc": auc, "positive_minus_negative_mean_probability": gap,
                            "pair_both_correct": both, "all_four_predictions_correct": exact})
        require(len(aucs) == MIXED_GROUPS[j] and total_pairs == MIXED_PAIRS[j],
                f"Approved mixed-group count/pairs changed for {label}")
        results[label] = {
            "mixed_groups": len(aucs), "positive_negative_pairs": total_pairs,
            "ranking_wins": correctly_ranked, "ranking_ties": ties,
            "pooled_within_group_ranking_auc": (correctly_ranked + .5 * ties) / total_pairs,
            "equal_group_ranking_auc": float(np.mean(aucs)),
            "equal_group_mean_positive_minus_negative_probability": float(np.mean(gaps)),
            "pair_both_classified_correctly": pair_both_correct,
            "pair_both_correct_rate": pair_both_correct / total_pairs,
            "mixed_group_all_four_correct_count": exact_groups,
            "mixed_group_all_four_correct_rate": exact_groups / len(aucs),
            "groups": details,
        }
    return results


def run_method(name, x, y, index, masks):
    models, choices, trials, metrics = validation_selection(x, y, masks)
    require(len(trials) == 24 and len(models) == 4, "Unexpected training grid")
    valid_rows = [r for r, use in zip(index, masks["validation"]) if use]
    valid_y = y[masks["validation"]]
    valid_x = l2_rows(x[masks["validation"]])
    probabilities = np.column_stack([model.predict_proba(valid_x)[:, 1] for model in models])
    require(probabilities.shape == (328, 4) and np.isfinite(probabilities).all(), "Invalid validation probabilities")
    chosen, predictions = summary_metrics(valid_y, probabilities, [c["threshold"] for c in choices])
    require(chosen == metrics["selected_thresholds"], "Validation metric recomputation mismatch")
    result = {"name": name, "fit_count": len(trials), "selections": choices,
              "metrics": metrics, "trials": trials,
              "mixed_groups": mixed_group_diagnostics(valid_y, probabilities, predictions, valid_rows)}
    print(json.dumps({"event": "validation_method_completed", "method": name,
                      "macro_f1": chosen["macro"]["f1"], "micro_f1": chosen["micro"]["f1"],
                      "macro_average_precision": chosen["macro_average_precision"]}), flush=True)
    return result


def metric_row(name, label, m):
    return (f"| {name} | {label} | {m['threshold']:.2f} | {m['precision']:.4f} | {m['recall']:.4f} | "
            f"{m['f1']:.4f} | {m['average_precision']:.4f} | {m['tp']} | {m['fp']} | {m['fn']} |")


def save_report(baseline, full, provenance, started):
    methods = (("中心裁剪", baseline), ("完整页补边", full))
    base_metrics = baseline["metrics"]["selected_thresholds"]
    full_metrics = full["metrics"]["selected_thresholds"]
    delta_f1 = full_metrics["macro"]["f1"] - base_metrics["macro"]["f1"]
    delta_ap = full_metrics["macro_average_precision"] - base_metrics["macro_average_precision"]
    positive_f1 = sum(full_metrics["per_label"][label]["f1"] > base_metrics["per_label"][label]["f1"] for label in LABELS)
    positive_ap = sum(full_metrics["per_label"][label]["average_precision"] > base_metrics["per_label"][label]["average_precision"] for label in LABELS)
    cfg = provenance["processor_config"]
    lines = [
        "# 第四阶段 4.3D：完整页面保留控制实验", "",
        f"实际执行：{started} 至 {now()}。仅使用冻结划分的 train 1,512 页 / validation 328 页（378/82 个完整四页组），共 1,840 页。未打开 test PNG、test annotations、test 特征行、旧模型、正式测试报告或 4.3C test_predictions.csv；本次没有测试集评价。", "",
        "## 1. 实际预处理与几何损失", "",
        f"模型 `{MODEL_ID}`，revision `{REVISION}`；权重及预处理配置 SHA256 与 4.3B/4.3C 一致。官方配置实际为 `do_resize={cfg['do_resize']}`、`size={cfg['size']}`、`resample={cfg['resample']}`（Pillow bicubic）、`do_center_crop={cfg['do_center_crop']}`、`crop_size={cfg['crop_size']}`，最终 224×224。短边等比缩到 224 后中心裁剪。", "",
        "| train/validation 原尺寸 | 页数 | 原方案 Resize 后 | 中心裁剪内容保留面积 | 横向内容损失 | 新方案完整页缩放 | 固定白色补边 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for size in provenance["trainval_image_sizes"]:
        w, h = size["width"], size["height"]
        require(w >= h, "Update geometry report explicitly for portrait inputs")
        resized_w = int(w * 224 / h)
        # Same integer output-size rule as the pinned official PIL processor.
        retained = 224 / resized_w
        fitted_h = round(h * 224 / w)
        pad_top = (224 - fitted_h) // 2
        pad_bottom = 224 - fitted_h - pad_top
        lines.append(f"| {w}×{h} | {size['count']} | {resized_w}×224 | {retained:.4%} | {1-retained:.4%} | 224×{fitted_h} | 上/下 {pad_top}/{pad_bottom}px |")
    lines += ["", "宽屏 1600×900 先变为 398×224，再只取中间 224 列，左右共丢失 174/398=43.7186% 的页面横向覆盖（每侧约 21.86%）；4:3 页面变为 298×224，左右共丢失 74/298=24.8322%。因此边缘的文字、裁切和遮挡证据可能未进入旧视觉输入；此处度量是几何覆盖，不是声称这些区域必然有缺陷。", "",
              "新方案先 RGB，保持长宽比以 Pillow BICUBIC 缩放到 224×224 范围，再居中补固定 `(255,255,255)` 白边；官方处理器显式 `do_resize=False, do_center_crop=False`，沿用相同 rescale 和 mean/std。整页 100% 几何内容都进入视觉编码器，无后续二次裁切；16:9/4:3 页面有效内容高度分别 126/168px，小字体也会变得更小。", "",
              f"官方 mean={cfg['image_mean']}、std={cfg['image_std']}、rescale_factor={cfg['rescale_factor']}；颜色、插值、尺寸和模型 pin 在特征索引与 recipe 摘要中记录。只使用冻结视觉编码器及图像投影、eval、inference_mode，无反向传播或文本特征。", "",
              "随机 train/validation 对照（原图、官方中心裁剪、完整页白边；固定 seed=42，各抽3页，不按标签挑图，不使用 test）：", "",
              "![train/validation 预处理对照](full_slide_preprocessing_comparison.png)", "",
              "## 2. 对齐、监督与相同的模型选择", "",
              "两方案以相同数值 slide_id 升序一一对应，X 仅为 512 维投影向量，不包含 slide_id、split、标注或几何字段。旧特征仅以 mmap 按已筛选的 1,840 个 train/validation 行号拷贝；未对整个旧数组执行摘要、有限性检查或加载。新特征为独立 float32 `(1840,512)` 文件，全部有限且无零向量；旧特征、正式模型、正式报告均未覆盖。", "",
              "官方 annotations 恰好一条/目标，严格布尔 `response is True` 为正；不按条目存在与否标正。训练前重新核对 4.3A train/validation 表格和批准计数，全部一致；excluded 和 test 不进入 X/y。来源组全部完整且不跨集合。", "",
              "| 标签 | train 正/负 | validation 正/负 |", "| --- | --- | --- |"]
    for j, label in enumerate(LABELS):
        lines.append(f"| {label} | {POSITIVES['train'][j]}/{COUNTS['train']-POSITIVES['train'][j]} | {POSITIVES['validation'][j]}/{COUNTS['validation']-POSITIVES['validation'][j]} |")
    lines += ["", "与原实验相同：逐行固定 L2 归一化到 float64，无需 fit 的预处理；每标签 C∈{0.1,1,10}×class_weight∈{None,balanced}，LogisticRegression(lbfgs,max_iter=3000,tol=1e-6,random_state=42)。每方案 24 次 train-only 拟合，共 48 次；未用 validation 重拟合，全部收敛。", "",
              "每配置在 validation 的 0.05–0.95（步长0.01）选 F1 最大阈值；同分选距0.5最近、再选较大阈值。配置依次按 validation F1、AP、更小C、优先None、距0.5最近、较大阈值决胜，直接复用旧脚本纯 helper。两方案独立遵守相同选择预算；此比较衡量“输入方案加相同调参流程”的整体差异。分类器不写入磁盘，逐页预测可通过 --export-validation-predictions 按本报告冻结配置重建并校验后另存。", "",
              "| 方案 | 标签 | C | class_weight | 阈值 |", "| --- | --- | --- | --- | --- |"]
    for name, method in methods:
        for choice in method["selections"]:
            lines.append(f"| {name} | {choice['label']} | {choice['C']:g} | {choice['class_weight']} | {choice['threshold']:.2f} |")
    lines += ["", "## 3. Validation 比较", "",
              "以下为 validation 页面级结果，所有负例均保留。AP=Average Precision，按连续概率计算；不是梯形 PR 积分，也不随阈值改变。P/R/F1 的零分母记0。", "",
              "验证集选择阈值：", "",
              "| 方案 | 标签 | 阈值 | Precision | Recall | F1 | AP | TP | FP | FN |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods:
        lines += [metric_row(name, label, method["metrics"]["selected_thresholds"]["per_label"][label]) for label in LABELS]
    lines += ["", "同批已选配置的默认0.5阈值对照：", "",
              "| 方案 | 标签 | 阈值 | Precision | Recall | F1 | AP | TP | FP | FN |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods:
        lines += [metric_row(name, label, method["metrics"]["default_0_5"]["per_label"][label]) for label in LABELS]
    lines += ["", "| 方案/阈值 | Macro P | Macro R | Macro F1 | Micro P | Micro R | Micro F1 | Macro AP |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods:
        for threshold_key, threshold_name in (("selected_thresholds", "选择阈值"), ("default_0_5", "0.5")):
            m = method["metrics"][threshold_key]
            values = [m[average][metric] for average in ("macro", "micro") for metric in ("precision", "recall", "f1")] + [m["macro_average_precision"]]
            lines.append(f"| {name}/{threshold_name} | " + " | ".join(f"{v:.4f}" for v in values) + " |")
    lines += ["", "完整页减去中心裁剪（各自相同规则选出的配置/阈值）：", "",
              "| 标签 | ΔPrecision | ΔRecall | ΔF1 | ΔAP |", "| --- | --- | --- | --- | --- |"]
    for label in LABELS:
        old, new = base_metrics["per_label"][label], full_metrics["per_label"][label]
        lines.append(f"| {label} | " + " | ".join(f"{new[k]-old[k]:+.4f}" for k in ("precision", "recall", "f1", "average_precision")) + " |")
    lines += ["", "## 4. 同一来源四个增强版本的区分能力", "",
              "仅对 validation 的正负混合四页组检查。标签顺序对应混合组29/33/26/22组、正负页对97/107/85/69对。组内正例概率>负例计1、相等计0.5、反序计0；pooled AUC按全部组内正负页对平均，组等权AUC先逐组计算再平均。概率差为逐组（正例均值−负例均值）再组等权平均。双页都对使用各方案 validation 阈值，整组全对要求同组四页均判断正确。", "",
              "| 方案 | 标签 | 混合组 | 正负页对 | pooled组内AUC | 组等权AUC | 平均正负概率差 | 双页都对 | 整组全对 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods:
        for label in LABELS:
            m = method["mixed_groups"][label]
            lines.append(f"| {name} | {label} | {m['mixed_groups']} | {m['positive_negative_pairs']} | {m['pooled_within_group_ranking_auc']:.4f} | {m['equal_group_ranking_auc']:.4f} | {m['equal_group_mean_positive_minus_negative_probability']:+.4f} | {m['pair_both_classified_correctly']}/{m['positive_negative_pairs']} ({m['pair_both_correct_rate']:.1%}) | {m['mixed_group_all_four_correct_count']}/{m['mixed_groups']} ({m['mixed_group_all_four_correct_rate']:.1%}) |")
    old_fp = sum(base_metrics["per_label"][label]["fp"] for label in LABELS)
    new_fp = sum(full_metrics["per_label"][label]["fp"] for label in LABELS)
    overflow = full_metrics["per_label"][LABELS[3]]
    new_group_aucs = [full["mixed_groups"][label]["pooled_within_group_ranking_auc"] for label in LABELS]
    old_occluded_auc = baseline["mixed_groups"][LABELS[1]]["pooled_within_group_ranking_auc"]
    lines += ["", "组内诊断控制了大量共同页面内容，可检查模型是否只识别“这个页面大致是什么”。它不证明因果机制，正负页对共享页面且不独立，不能当作额外独立样本数。", "",
              f"本次补边后字号、杂乱、溢出的组内 pooled AUC 分别为 {new_group_aucs[0]:.4f}/{new_group_aucs[2]:.4f}/{new_group_aucs[3]:.4f}，接近随机排序的0.5；遮挡从 {old_occluded_auc:.4f} 降为 {new_group_aucs[1]:.4f}。没有观察到一致的增强版本级区分改善。", "",
              f"召回率上升伴随四标签误报事件总数 {old_fp}→{new_fp}（一个页面可计多个标签事件）；例如完整页方案将 {overflow['predicted_positive']}/328 页判为溢出。高召回不能单独说明识别能力增强。", "",
              "## 5. 结论与限制", "",
              f"本次完整页输入相对中心裁剪的 validation Macro-F1 差值为 **{delta_f1:+.4f}**，Macro-AP 差值为 **{delta_ap:+.4f}**；四类中 F1 提高 {positive_f1}/4 类、AP 提高 {positive_ap}/4 类。"]
    for label in LABELS:
        old, new = base_metrics["per_label"][label], full_metrics["per_label"][label]
        lines.append(f"- {label}：F1 {old['f1']:.4f}→{new['f1']:.4f}，AP {old['average_precision']:.4f}→{new['average_precision']:.4f}；组内 pooled 排序AUC {baseline['mixed_groups'][label]['pooled_within_group_ranking_auc']:.4f}→{full['mixed_groups'][label]['pooled_within_group_ranking_auc']:.4f}。")
    if delta_f1 > 0 and delta_ap > 0:
        conclusion = "两个宏观指标都提高，对本验证集而言存在完整页方案有利的探索性证据；是否各标签一致，应以逐类表和组内诊断为准。"
    elif delta_f1 <= 0 and delta_ap <= 0:
        conclusion = "两个宏观指标均未提高，本验证集不支持将完整页白边输入视为整体改进。中心裁剪确实丢内容，但补边方案同时改变内容尺度和背景；本实验未证明移除中心裁剪即可改善识别，不能将差异完全归因于是否裁剪。"
    else:
        conclusion = "F1 与 AP 方向不一致，本验证集证据混合，不能把阈值后的改善等同于概率排序能力全面提高。"
    lines += ["", conclusion, "",
              "validation 同时用于参数/阈值选择与方案比较，指标有选择偏差；82个来源组及每类35–49个正例样本较少，同源增强版本相关，因此页面级结果不等同于独立原始页面级结果。本次不做显著性或泛化断言，也不据此修改已冻结的正式测试结果。", "",
              "仍值得在后续独立阶段研究固定多区域或局部特征：完整页提供布局覆盖，局部区域保留文字尺寸、边缘裁切与遮挡的细节。它是后续假设，不是本次已验证的方案；后续应先冻结设计和验收规则，避免反复使用已有测试集调参。本次未提取多区域特征、未增加模型、未接入智能体。", "",
              "## 6. 复现记录", "",
              "执行 `detector_runtime/.venv/Scripts/python.exe detector_runtime/scripts/evaluate_full_slide.py`。`--validate-only` 只核对 train/validation；如有意重复同一验证实验，使用 `--overwrite-report` 覆盖本阶段新报告，正式4.3C文件仍不会被改写。", "",
              "新特征：`features/clip_embeddings_full_slide_trainval.npy`；新索引：`features/full_slide_feature_index.csv`；补边/中心裁剪对照图保存在本报告旁。两方法的分类器只保留于本次进程内存。", ""]
    for field in ("model_revision", "checkpoint_sha256", "config_sha256", "preprocessor_sha256", "split_sha256",
                  "new_feature_sha256", "new_feature_index_sha256", "input_trainval_manifest_sha256",
                  "baseline_selected_trainval_values_sha256", "baseline_index_sha256", "recipe_sha256", "trainval_annotations_sha256"):
        lines.append(f"- {field}: `{provenance[field]}`")
    env = {"python": platform.python_version(), "executable": sys.executable,
           **{p: importlib.metadata.version(p) for p in ("numpy", "scikit-learn", "torch", "transformers", "Pillow")}}
    lines += ["", f"运行环境：`{json.dumps(env, ensure_ascii=False, sort_keys=True)}`。", "",
              "<details><summary>完整48次 validation 配置记录（★为各方案每标签选中配置）</summary>", "",
              "| 方案 | 标签 | C | class_weight | 阈值 | P | R | F1 | AP | F1@0.5 | 迭代数 | 选中 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, method in methods:
        for trial in method["trials"]:
            m = trial["validation_selected_threshold"]
            lines.append(f"| {name} | {trial['label']} | {trial['C']:g} | {trial['class_weight']} | {m['threshold']:.2f} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['average_precision']:.4f} | {trial['validation_default_0_5']['f1']:.4f} | {trial['n_iter']} | {'★' if trial['selected'] else ''} |")
    lines += ["", "</details>", "", "<details><summary>机器可读审计摘要（仅 train/validation，不含测试记录）</summary>", "", "```json"]
    compact_methods = {}
    for name, method in (("center_crop", baseline), ("full_slide", full)):
        compact_methods[name] = {
            "selections": method["selections"], "metrics": method["metrics"],
            "mixed_groups": {label: {k: v for k, v in metrics.items() if k != "groups"}
                             for label, metrics in method["mixed_groups"].items()},
        }
    audit = {"provenance": provenance, "methods": compact_methods,
             "fit_count": 48, "test_evaluation_count": 0, "clip_training_performed": False,
             "full_slide_vs_center_crop_macro_f1": delta_f1,
             "full_slide_vs_center_crop_macro_average_precision": delta_ap}
    lines += [json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
              "```", "", "</details>", ""]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".md.part")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, REPORT_PATH)


def require_nested_close(actual, expected, location="value"):
    """Check saved/reloaded statistics, including every confusion count."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), f"Keys differ: {location}")
        for key in expected:
            require_nested_close(actual[key], expected[key], f"{location}.{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, (list, tuple)) and len(actual) == len(expected), f"List differs: {location}")
        for i, (a, e) in enumerate(zip(actual, expected)):
            require_nested_close(a, e, f"{location}[{i}]")
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        require(isinstance(actual, (int, float)) and np.isfinite(actual)
                and np.isclose(actual, expected, rtol=0, atol=1e-12), f"Number differs: {location}")
    else:
        require(actual == expected, f"Value differs: {location}")


def verify_validation_csv(path, valid_rows, valid_y, methods):
    """Recompute all final-model statistics from the CSV, not model objects."""
    rows = read_csv(path)
    require(len(rows) == 328 and len({r["slide_id"] for r in rows}) == 328, "CSV must have 328 unique pages")
    require([(r["slide_id"], r["group_id"], r["split"]) for r in rows]
            == [(r["slide_id"], r["group_id"], "validation") for r in valid_rows],
            "CSV IDs/groups/order/subsets disagree with validation")
    truth = np.array([[int(r[f"true_{slug}"]) for slug in SLUGS] for r in rows], dtype=np.int64)
    require(np.array_equal(truth, valid_y), "CSV labels differ from official validation labels")
    for name in ("center_crop", "full_slide"):
        probabilities = np.array([[float(r[f"{name}_prob_{s}"]) for s in SLUGS] for r in rows])
        thresholds = np.array([[float(r[f"{name}_threshold_{s}"]) for s in SLUGS] for r in rows])
        predictions = np.array([[int(r[f"{name}_pred_{s}"]) for s in SLUGS] for r in rows], dtype=np.int64)
        expected = methods[name]
        chosen = [choice["threshold"] for choice in expected["selections"]]
        require(np.isfinite(probabilities).all() and ((0 <= probabilities) & (probabilities <= 1)).all(),
                f"CSV invalid probabilities: {name}")
        require(np.array_equal(thresholds, np.broadcast_to(chosen, (328, 4))), f"CSV thresholds changed: {name}")
        metrics, recomputed = summary_metrics(truth, probabilities, chosen)
        default, _ = summary_metrics(truth, probabilities, [.5] * 4)
        require(np.array_equal(predictions, recomputed), f"CSV threshold predictions disagree: {name}")
        require_nested_close({"selected_thresholds": metrics, "default_0_5": default}, expected["metrics"], name)
        groups = mixed_group_diagnostics(truth, probabilities, predictions, rows)
        compact = {label: {k: v for k, v in result.items() if k != "groups"} for label, result in groups.items()}
        require_nested_close(compact, expected["mixed_groups"], f"{name}.mixed_groups")


def export_validation_predictions(baseline, full_slide, y, index, masks, provenance):
    """Restore fixed choices, not the search, to supplement the prior report."""
    report = REPORT_PATH.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", report, re.S)
    require(match is not None, "Comparison report lacks the original machine-readable audit")
    audit = json.loads(match.group(1))
    require(audit["fit_count"] == 48 and audit["test_evaluation_count"] == 0,
            "Expected the completed train/validation-only experiment")
    require_nested_close(provenance, audit["provenance"], "original_input_provenance")
    valid_rows = [r for r, use in zip(index, masks["validation"]) if use]
    train_y, valid_y = y[masks["train"]], y[masks["validation"]]
    restored = {}
    for name, x in (("center_crop", baseline), ("full_slide", full_slide)):
        choices = audit["methods"][name]["selections"]
        require([c["label"] for c in choices] == list(LABELS), "Saved label order differs")
        train_x, valid_x = l2_rows(x[masks["train"]]), l2_rows(x[masks["validation"]])
        probabilities = []
        for j, choice in enumerate(choices):
            require(choice["C"] in (.1, 1, 10) and choice["class_weight"] in (None, "balanced"),
                    "Saved classifier outside the original search grid")
            classifier = LogisticRegression(C=choice["C"], class_weight=choice["class_weight"],
                                            solver="lbfgs", max_iter=3000, tol=1e-6, random_state=SEED)
            with warnings.catch_warnings():
                warnings.simplefilter("error", ConvergenceWarning)
                classifier.fit(train_x, train_y[:, j])
            require(classifier.classes_.tolist() == [0, 1], "Restored binary class order differs")
            probabilities.append(classifier.predict_proba(valid_x)[:, 1])
        scores = np.column_stack(probabilities)
        thresholds = [c["threshold"] for c in choices]
        metrics, predictions = summary_metrics(valid_y, scores, thresholds)
        default, _ = summary_metrics(valid_y, scores, [.5] * 4)
        require_nested_close({"selected_thresholds": metrics, "default_0_5": default},
                             audit["methods"][name]["metrics"], f"restored.{name}")
        restored[name] = dict(probabilities=scores, predictions=predictions, thresholds=thresholds)
    fields = ["slide_id", "group_id", "split"] + [f"true_{s}" for s in SLUGS]
    fields += [f"{name}_{kind}_{s}" for name in restored for kind in ("prob", "pred", "threshold") for s in SLUGS]
    temporary = PREDICTION_PATH.with_suffix(".csv.part")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for i, row in enumerate(valid_rows):
                record = {k: row[k] for k in ("slide_id", "group_id", "split")}
                for j, slug in enumerate(SLUGS):
                    record[f"true_{slug}"] = int(valid_y[i, j])
                    for name, result in restored.items():
                        record[f"{name}_prob_{slug}"] = format(float(result["probabilities"][i, j]), ".17g")
                        record[f"{name}_pred_{slug}"] = int(result["predictions"][i, j])
                        record[f"{name}_threshold_{slug}"] = repr(float(result["thresholds"][j]))
                writer.writerow(record)
        verify_validation_csv(temporary, valid_rows, valid_y, audit["methods"])
        if PREDICTION_PATH.exists():
            require(PREDICTION_PATH.read_bytes() == temporary.read_bytes(), "Existing validation CSV differs; refusing replacement")
        else:
            os.replace(temporary, PREDICTION_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()
    verify_validation_csv(PREDICTION_PATH, valid_rows, valid_y, audit["methods"])
    csv_hash = sha256(PREDICTION_PATH)
    audit["validation_predictions_csv"] = {
        "path": str(PREDICTION_PATH.relative_to(BASE)), "sha256": csv_hash,
        "rows": 328, "groups": 82, "split": "validation", "columns": fields,
        "label_slugs": dict(zip(SLUGS, LABELS)), "prediction_rule": "probability >= recorded per-label threshold",
        "thresholds": {name: result["thresholds"] for name, result in restored.items()},
        "selected_threshold_metrics_verified": True, "default_0_5_metrics_verified": True,
        "mixed_group_statistics_verified": True, "export_reconstruction_fits": 8,
        "parameter_or_threshold_selection_repeated": False, "export_test_evaluation_count": 0,
    }
    report = report[:match.start(1)] + json.dumps(audit, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")) + report[match.end(1):]
    report = report.replace("没有保存新 LR 模型或逐页预测副本。",
                            "分类器不写入磁盘；两方案 validation 逐页预测已补存于 validation_predictions.csv。")
    report = report.replace("正式测试报告或预测 CSV", "正式测试报告或 4.3C test_predictions.csv")
    section = "\n\n## 7. Validation 逐页预测与互证\n"
    report = report.split(section)[0].rstrip()
    label_mapping = "、".join(f"`{s}`={label}" for s, label in zip(SLUGS, LABELS))
    report += section + f"\n已保存 [validation_predictions.csv](validation_predictions.csv)：328 个唯一 validation 页面、82 个完整四页组，31 列，不含 train/test/excluded 行。\n\n"
    report += "包含 slide_id、group_id、split、四个 `true_{slug}`；`center_crop` 表示原中心裁剪方案，`full_slide` 表示完整页补边方案，各含四个 `{方案}_prob_{slug}`、`{方案}_pred_{slug}`、`{方案}_threshold_{slug}`。概率使用17位有效数字，阈值逐行记录，预测规则为 `probability >= threshold`。\n\n"
    report += f"标签后缀对应：{label_mapping}。\n\n"
    report += "本次仅按原报告冻结的 C/class_weight 重建8个 train-only 分类器，以恢复 validation 概率；没有重新选择参数或阈值，没有再次搜索48配置，也未重新提取 CLIP 特征。全部原指标与结论保持不变。\n\n"
    for name, result in restored.items():
        report += f"- {name} 阈值（上述标签顺序）：`{result['thresholds']}`。\n"
    report += "\n从落盘 CSV 重新计算的各标签 P/R/F1/AP、TP/FP/TN/FN、正例数、预测正例数、Macro/Micro 指标、默认0.5对照，以及混合四页组的排序AUC/概率差/分类正确率，均与本报告机读摘要逐字段一致（浮点容差1e-12）。48个候选配置的搜索表记录选择过程，不能由最终选中模型的预测反推。\n\n"
    report += f"CSV SHA256：`{csv_hash}`。复现补存：`.venv/Scripts/python.exe -B scripts/evaluate_full_slide.py --export-validation-predictions`（工作目录 detector_runtime）；已存在且字节相同的CSV会复核后复用，存在差异则停止。\n"
    temporary_report = REPORT_PATH.with_suffix(".md.part")
    temporary_report.write_text(report, encoding="utf-8")
    os.replace(temporary_report, REPORT_PATH)
    print(json.dumps({"event": "validation_predictions_exported", "path": str(PREDICTION_PATH),
                      "rows": 328, "groups": 82, "sha256": csv_hash, "restored_classifiers": 8,
                      "all_report_metrics_verified": True, "test_evaluation_count": 0}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true", help="Check train/validation inputs without any fitting")
    parser.add_argument("--overwrite-report", action="store_true", help="Repeat validation-only comparison and replace only the new 4.3D report")
    parser.add_argument("--export-validation-predictions", action="store_true",
                        help="Restore the report's eight frozen train-only classifiers and export validation CSV")
    args = parser.parse_args()
    require(not args.export_validation_predictions or not (args.validate_only or args.overwrite_report),
            "Use the CSV export option alone; it never repeats model selection")
    require(args.validate_only or args.overwrite_report or args.export_validation_predictions or not REPORT_PATH.exists(),
            "New comparison report already exists; inspect it or explicitly use --overwrite-report")
    started = now()
    baseline, full_slide, y, index, masks, provenance = load_inputs()
    print(json.dumps({"event": "trainval_checks_passed", "shape": list(full_slide.shape),
                      "counts": provenance["counts"], "positives": provenance["positives"],
                      "test_feature_values_accessed": 0, "test_annotations_opened": 0}), flush=True)
    if args.validate_only:
        return
    if args.export_validation_predictions:
        export_validation_predictions(baseline, full_slide, y, index, masks, provenance)
        return
    center = run_method("center_crop", baseline, y, index, masks)
    full = run_method("full_slide", full_slide, y, index, masks)
    require(sha256(SPLIT_PATH) == SPLIT_SHA256, "Frozen split changed during execution")
    require(sha256(FEATURE_PATH) == provenance["new_feature_sha256"], "New feature file changed during comparison")
    require(sha256(INDEX_PATH) == provenance["new_feature_index_sha256"], "New feature index changed during comparison")
    save_report(center, full, provenance, started)
    print(json.dumps({"event": "completed", "report": str(REPORT_PATH), "fit_count": 48,
                      "test_evaluation_count": 0,
                      "center_crop_validation": center["metrics"]["selected_thresholds"],
                      "full_slide_validation": full["metrics"]["selected_thresholds"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
