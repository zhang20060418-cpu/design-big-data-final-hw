#!/usr/bin/env python3
"""Independently verify the 4.3F-2A PNG-only train/validation feature artifact.

This script never fits a model or opens annotations, descriptions, held-out PNGs,
or existing CLIP feature arrays. It verifies outputs; it does not rewrite them.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import re
import sys

import numpy as np
from PIL import Image, __version__ as PILLOW_VERSION

import extract_png_layout_features as extractor


BASE = Path(__file__).resolve().parents[1]
CSV_PATH = BASE / "features/png_layout_trainval.csv"
RECIPE_PATH = BASE / "features/png_layout_recipe.json"
RESULT_PATH = BASE / ".cache/png_layout_smoke/check_result.json"
EXPECTED_SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
EXPECTED_FEATURES = [
    "aspect_ratio", "edge_density", "flat_region_ratio", "gray_std",
    "grid_edge_cv", "border_edge_ratio", "horizontal_projection_spread",
    "vertical_projection_spread", "mean_gray", "mean_color_range",
]
KNOWN_IDENTICAL_PAIRS = [
    ["slide_0383", "slide_0459"],
    ["slide_0514", "slide_2390"],
    ["slide_1312", "slide_2227"],
]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def payload_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def numeric_id(slide_id):
    require(re.fullmatch(r"slide_\d{4}", slide_id) is not None,
            f"Invalid slide_id: {slide_id!r}")
    return int(slide_id.removeprefix("slide_"))


def summary(values):
    return {
        name: {
            "min": float(np.min(values[:, index])),
            "max": float(np.max(values[:, index])),
            "mean": float(np.mean(values[:, index], dtype=np.float64)),
            "std": float(np.std(values[:, index], dtype=np.float64, ddof=0)),
            "zero_count": int(np.count_nonzero(values[:, index] == 0)),
            "nonfinite_count": int(np.count_nonzero(~np.isfinite(values[:, index]))),
        }
        for index, name in enumerate(EXPECTED_FEATURES)
    }


def check_statistics(actual, recorded, label):
    require(set(actual) == set(recorded), f"{label}: statistic field mismatch")
    for name, expected in actual.items():
        require(set(expected) == set(recorded[name]),
                f"{label}/{name}: summary key mismatch")
        for key, value in expected.items():
            other = recorded[name][key]
            require(isinstance(other, (int, float)) and math.isfinite(other),
                    f"{label}/{name}/{key}: invalid recorded statistic")
            if key in ("zero_count", "nonfinite_count"):
                require(value == other, f"{label}/{name}/{key}: count differs")
            else:
                require(math.isclose(value, other, rel_tol=1e-14, abs_tol=1e-14),
                        f"{label}/{name}/{key}: {value!r} differs from {other!r}")


def check_bounds(matrix):
    require(matrix.dtype == np.float64, "CSV matrix must parse as float64")
    require(np.isfinite(matrix).all(), "CSV contains NaN or Inf")
    require((matrix[:, 0] > 0).all(), "aspect_ratio must be positive")
    # Every column except aspect ratio and grid CV has a known [0, 1] bound.
    for index, name in enumerate(EXPECTED_FEATURES):
        if index == 0:
            continue
        upper = math.sqrt(8) if name == "grid_edge_cv" else (
            0.5 if name in ("gray_std", "horizontal_projection_spread",
                            "vertical_projection_spread") else 1.0)
        require((matrix[:, index] >= -1e-14).all(), f"{name} is below zero")
        require((matrix[:, index] <= upper + 1e-14).all(),
                f"{name} exceeds theoretical bound {upper}")
    require((matrix[:, 1] + matrix[:, 2] <= 1 + 1e-14).all(),
            "Strict edge and flat masks overlap")


def scalar_oracle():
    """A hand-computable 6x6 split black/white image, without resizing."""
    pixels = np.zeros((6, 6, 3), dtype=np.uint8)
    pixels[:, 3:, :] = 255
    actual = extractor.extract_features(Image.fromarray(pixels))
    expected = {
        "aspect_ratio": 1.0,
        "edge_density": 1 / 6,
        "flat_region_ratio": 5 / 6,
        "gray_std": 0.5,
        "grid_edge_cv": math.sqrt(1 / 18) / (1 / 6 + 1e-6),
        "border_edge_ratio": 1 / 3,
        "horizontal_projection_spread": math.sqrt(7 / 60),
        "vertical_projection_spread": 0.0,
        "mean_gray": 0.5,
        "mean_color_range": 0.0,
    }
    require(list(actual) == EXPECTED_FEATURES, "Core function changed feature order")
    for name, value in expected.items():
        require(math.isclose(actual[name], value, rel_tol=1e-13, abs_tol=1e-13),
                f"Independent scalar oracle failed for {name}: {actual[name]} != {value}")
    return {"name": "6x6_black_left_white_right", "status": "passed",
            "features": actual}


def verify(sample_size=12):
    extractor.install_read_guard()
    require(list(extractor.FEATURE_NAMES) == EXPECTED_FEATURES,
            "Extractor feature names or order differ from fixed task")
    split_path = BASE / "reports/dataset_split.csv"
    split_sha = file_digest(split_path)
    require(split_sha == EXPECTED_SPLIT_SHA256, "Frozen data split SHA256 changed")
    with split_path.open(encoding="utf-8-sig", newline="") as stream:
        split_rows = list(csv.DictReader(stream))
    require(len(split_rows) == 2400, "Unexpected complete split row count")
    require(len({row["slide_id"] for row in split_rows}) == 2400,
            "Duplicate IDs in split")
    selected = [row for row in split_rows if row["split"] in ("train", "validation")]
    selected.sort(key=lambda row: numeric_id(row["slide_id"]))
    require(Counter(row["split"] for row in selected) == {"train": 1512, "validation": 328},
            "Incorrect train/validation scope")
    ids = [row["slide_id"] for row in selected]
    group_splits = defaultdict(set)
    group_counts = Counter()
    for row in selected:
        require(bool(row["group_id"]), "Selected sample has no group_id")
        group_splits[row["group_id"]].add(row["split"])
        group_counts[row["group_id"]] += 1
    require(all(len(values) == 1 for values in group_splits.values()),
            "A trusted group spans train and validation")
    require(all(value == 4 for value in group_counts.values()),
            "A selected trusted group does not contain exactly four pages")

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames == ["slide_id", *EXPECTED_FEATURES],
                f"CSV columns/order differ: {reader.fieldnames!r}")
        csv_rows = list(reader)
    require(len(csv_rows) == 1840, f"CSV row count is {len(csv_rows)}, expected 1840")
    require(all(set(row) == {"slide_id", *EXPECTED_FEATURES} for row in csv_rows),
            "Malformed CSV row or trailing fields")
    require([row["slide_id"] for row in csv_rows] == ids,
            "CSV IDs are not exactly the numerically sorted train/validation whitelist")
    require(len({row["slide_id"] for row in csv_rows}) == 1840, "Duplicate CSV IDs")
    require(all(row[name] not in (None, "") for row in csv_rows for name in EXPECTED_FEATURES),
            "Missing numeric feature")
    matrix = np.asarray([[float(row[name]) for name in EXPECTED_FEATURES]
                         for row in csv_rows], dtype=np.float64)
    require(matrix.shape == (1840, 10), "Invalid CSV matrix shape")
    check_bounds(matrix)

    with RECIPE_PATH.open(encoding="utf-8") as stream:
        recipe = json.load(stream)
    require(recipe["feature_names"] == EXPECTED_FEATURES, "Recipe feature order differs")
    require(recipe["algorithm"] == extractor.ALGORITHM_RECIPE,
            "Recipe algorithm differs from imported extraction implementation")
    require(recipe["split_sha256"] == split_sha, "Recipe split SHA256 mismatch")
    csv_sha = file_digest(CSV_PATH)
    require(recipe["csv_sha256"] == csv_sha, "CSV SHA256 mismatch")
    source_sha = file_digest(Path(extractor.__file__))
    require(recipe["source_sha256"] == source_sha, "Extraction source SHA256 mismatch")
    payload = {key: value for key, value in recipe.items() if key != "payload_sha256"}
    require(recipe["payload_sha256"] == payload_digest(payload), "Recipe payload SHA256 mismatch")
    require(recipe["versions"] == {"python": platform.python_version(),
                                   "numpy": np.__version__, "pillow": PILLOW_VERSION},
            "Runtime versions differ from the recorded extraction environment")
    require(recipe["no_training"] is True and recipe["smoke_status"] == "passed",
            "Recipe does not record a passed, training-free extraction")

    # Independently validate all original input bytes, decode, metadata sizes and
    # the previously frozen download identity through the extraction manifest.
    input_rows = extractor.build_input_rows(verify_images=True)
    require([row["slide_id"] for row in input_rows] == ids,
            "Extraction input list differs from the frozen split")
    expected_aspects = np.asarray([row["image_width"] / row["image_height"]
                                   for row in input_rows], dtype=np.float64)
    require(np.array_equal(matrix[:, 0], expected_aspects),
            "CSV aspect_ratio differs from verified original PNG dimensions")
    manifest = [{"slide_id": row["slide_id"], "image_sha256": row["image_sha256"]}
                for row in input_rows]
    inputs = recipe["inputs"]
    require(inputs["scope"] == ["train", "validation"], "Recipe contains an invalid scope")
    require(inputs["image_root"] == "data/images", "Recipe image root differs")
    require(inputs["count"] == 1840, "Recipe input count differs")
    require(inputs["split_counts"] == {"train": 1512, "validation": 328},
            "Recipe input split counts differ")
    require(inputs["manifest"] == manifest, "Recipe image manifest differs from actual PNGs")
    require(inputs["manifest_sha256"] == payload_digest(manifest),
            "Recipe image manifest SHA256 mismatch")
    actual_image_digests = {}
    for row in input_rows:
        actual = file_digest(row["path"])
        require(actual == row["image_sha256"], f"PNG hash mismatch: {row['slide_id']}")
        actual_image_digests[row["slide_id"]] = actual

    check_statistics(summary(matrix), recipe["feature_statistics"], "feature_statistics")
    by_source = defaultdict(list)
    for index, row in enumerate(input_rows):
        by_source[row["source_type"]].append(index)
    require(set(recipe["source_statistics"]) == set(by_source), "Source names differ")
    for source, indices in by_source.items():
        recorded = recipe["source_statistics"][source]
        require(recorded["count"] == len(indices), f"Source count differs: {source}")
        check_statistics(summary(matrix[indices]), recorded["feature_statistics"],
                         f"source_statistics/{source}")

    # Check every duplicate PNG cluster, not just the three known conflict pairs.
    digest_groups = defaultdict(list)
    row_by_id = {row["slide_id"]: row for row in input_rows}
    index_by_id = {slide_id: index for index, slide_id in enumerate(ids)}
    for slide_id in ids:
        digest_groups[actual_image_digests[slide_id]].append(slide_id)
    duplicates = sorted([values for values in digest_groups.values() if len(values) > 1],
                        key=lambda values: numeric_id(values[0]))
    for group in duplicates:
        first_bytes = Path(row_by_id[group[0]]["path"]).read_bytes()
        first_features = matrix[index_by_id[group[0]]]
        for slide_id in group[1:]:
            require(Path(row_by_id[slide_id]["path"]).read_bytes() == first_bytes,
                    f"SHA256 cluster not byte-identical: {group}")
            require(np.array_equal(matrix[index_by_id[slide_id]], first_features),
                    f"Identical PNGs have unequal features: {group}")
    for left, right in KNOWN_IDENTICAL_PAIRS:
        require(actual_image_digests[left] == actual_image_digests[right],
                f"Known identical PNG pair no longer matches: {left}/{right}")
        require(np.array_equal(matrix[index_by_id[left]], matrix[index_by_id[right]]),
                f"Known identical pair has unequal feature values: {left}/{right}")

    require(1 <= sample_size <= 1840, "sample_size must be in [1, 1840]")
    # One prespecified sample, no labels or performance-dependent sample changes.
    sample_ids = sorted(random.Random(42).sample(ids, sample_size), key=numeric_id)
    for slide_id in sample_ids:
        calculated = extractor.extract_features(row_by_id[slide_id]["path"])
        require(list(calculated) == EXPECTED_FEATURES,
                f"Core feature order differs: {slide_id}")
        values = np.asarray(list(calculated.values()), dtype=np.float64)
        require(np.array_equal(values, matrix[index_by_id[slide_id]]),
                f"Fixed-sample recomputation not bit-exact: {slide_id}")
        repeated = extractor.extract_features(row_by_id[slide_id]["path"])
        require(list(calculated.items()) == list(repeated.items()),
                f"Repeat extraction is not exactly deterministic: {slide_id}")

    oracle = scalar_oracle()
    return {
        "status": "passed", "rows": 1840, "feature_count": 10,
        "split_counts": {"train": 1512, "validation": 328},
        "group_count": len(group_splits), "excluded_or_test_rows": 0,
        "missing_or_duplicate_ids": 0, "nonfinite_values": 0,
        "all_png_sha256_verified": len(actual_image_digests),
        "identical_png_group_count": len(duplicates),
        "identical_png_groups": duplicates,
        "known_conflict_pairs_feature_equal": KNOWN_IDENTICAL_PAIRS,
        "sample_seed": 42, "recomputed_sample_ids": sample_ids,
        "sample_recompute_bit_exact": True, "repeat_extract_exact": True,
        "scalar_oracle": oracle, "csv_sha256": csv_sha,
        "recipe_payload_sha256": recipe["payload_sha256"],
        "input_manifest_sha256": inputs["manifest_sha256"],
        "split_sha256": split_sha, "extractor_source_sha256": source_sha,
        "feature_statistics_recomputed": True, "source_statistics_recomputed": True,
        "runtime_versions_match": True,
        "no_annotations_descriptions_or_feature_arrays_read": True,
        "no_held_out_pngs_read": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=12)
    parser.add_argument("--result-json", type=Path, default=RESULT_PATH,
                        help="Temporary verification result, never the feature CSV/recipe")
    args = parser.parse_args()
    require(args.result_json.resolve() not in (CSV_PATH.resolve(), RECIPE_PATH.resolve()),
            "Result path must not overwrite feature outputs")
    try:
        result = verify(args.sample_size)
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__,
                  "error": str(error)}
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    args.result_json.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
