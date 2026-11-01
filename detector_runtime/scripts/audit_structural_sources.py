#!/usr/bin/env python3
"""Read-only 4.3F-1 audit of structural sources; never train or read held-out pages.

Run with this project's .venv Python and ``-B``.  The default is a short JSON
summary; ``--json`` emits the complete reproducible audit to stdout.  No data,
split, feature, model, report or auxiliary project file is written by this script.
JSON field presence is not evidence that its provenance is independent of labels.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib

BASE = Path(__file__).resolve().parents[1]
PROJECT = BASE.parent
REPO = BASE / "data/slideaudit"
DATA = REPO / "data"
SPLIT_PATH = BASE / "reports/dataset_split.csv"
SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
COMMIT = "642d490b7c1d2e78a50a631bfd359433397f3ecf"
LABELS = (
    "Improper Font Sizing", "Occluded Content", "Cluttered Layout",
    "Content Overflow/Cut-off",
)
EXPECTED_POSITIVES = {"train": [234, 244, 173, 161], "validation": [49, 47, 37, 35]}
KNOWN_CONFLICT_PAIRS = (
    ("slide_0383", "slide_0459"), ("slide_0514", "slide_2390"),
    ("slide_1312", "slide_2227"),
)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def json_hash(value):
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), reader.fieldnames


def json_kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def is_empty(value):
    return isinstance(value, (str, list, dict)) and len(value) == 0


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def numeric_summary(values):
    values = list(values)
    if not values:
        return {"count": 0}
    return {"count": len(values), "min": min(values), "max": max(values),
            "mean": sum(values) / len(values)}


class SchemaAudit:
    """Count field presence relative to observed applicable parent dictionaries.

    A field under $.elements[].table is measured against actual table dictionaries,
    not against every page or every non-table element.  Missing parents are reported
    separately at their own field path.  This avoids declaring font-free objects to
    have present font values, or declaring absent image fields to be universal gaps.
    """

    def __init__(self):
        self.parents = Counter()
        self.fields = {}
        self.array_items = defaultdict(Counter)
        self.keys = Counter()

    def visit(self, value, path="$"):
        if isinstance(value, dict):
            self.parents[path] += 1
            for key, child in value.items():
                self.keys[key] += 1
                child_path = path + "." + key
                item = self.fields.setdefault(child_path, {
                    "parent": path, "present": 0, "null": 0, "empty": 0,
                    "types": Counter(),
                })
                item["present"] += 1
                item["null"] += child is None
                item["empty"] += is_empty(child)
                item["types"][json_kind(child)] += 1
                self.visit(child, child_path)
        elif isinstance(value, list):
            for child in value:
                self.array_items[path][json_kind(child)] += 1
                self.visit(child, path + "[]")

    def result(self):
        fields = {}
        for path, item in sorted(self.fields.items()):
            denominator = self.parents[item["parent"]]
            missing = denominator - item["present"]
            fields[path] = {
                "applicable_parent_path": item["parent"],
                "applicable_parent_count": denominator,
                "present": item["present"], "missing": missing,
                "missing_fraction": missing / denominator if denominator else None,
                "null": item["null"], "empty": item["empty"],
                "nonempty_nonnull": item["present"] - item["null"] - item["empty"],
                "types": dict(sorted(item["types"].items())),
            }
        return {"parent_container_counts": dict(sorted(self.parents.items())),
                "fields": fields,
                "array_item_types": {p: dict(sorted(c.items())) for p, c in sorted(self.array_items.items())},
                "key_occurrences": dict(sorted(self.keys.items()))}


def install_guard(allowed):
    """Deny raw test/excluded/unknown pages and old test model/results at open()."""
    raw_directories = {
        os.path.normcase(os.path.abspath(BASE / "data/images")),
        os.path.normcase(os.path.abspath(DATA / "annotations")),
        os.path.normcase(os.path.abspath(DATA / "descriptions")),
    }
    denied = {
        os.path.normcase(os.path.abspath(BASE / name)) for name in (
            "reports/test_predictions.csv", "reports/clip_lr_results.md",
            "models/clip_lr_4labels.joblib",
        )
    }

    def guard(event, args):
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
        if path in denied:
            raise RuntimeError("Held-out model/result access forbidden: " + path)
        if os.path.dirname(path) in raw_directories and Path(path).stem not in allowed:
            raise RuntimeError("Raw page outside train/validation forbidden: " + path)
        if path.endswith((".npy", ".npz", ".joblib")):
            raise RuntimeError("This structural audit does not need model/feature arrays: " + path)

    sys.addaudithook(guard)


def load_scope():
    if sha256_file(SPLIT_PATH) != SPLIT_SHA256:
        raise ValueError("Frozen dataset split changed; refusing this audit")
    rows, _ = csv_rows(SPLIT_PATH)
    if len(rows) != 2400 or len({r["slide_id"] for r in rows}) != 2400:
        raise ValueError("Unexpected split identifiers")
    all_counts = Counter(r["split"] for r in rows)
    if all_counts != Counter(train=1512, validation=328, test=324, excluded=236):
        raise ValueError("Unexpected frozen split counts")
    allowed = {r["slide_id"]: r for r in rows if r["split"] in ("train", "validation")}
    install_guard(allowed)
    groups = defaultdict(list)
    for row in allowed.values():
        groups[row["group_id"]].append(row)
    if len(groups) != 460 or any(len(v) != 4 or len({r["split"] for r in v}) != 1 for v in groups.values()):
        raise ValueError("Expected 460 intact train/validation four-page groups")
    if Counter(v[0]["split"] for v in groups.values()) != Counter(train=378, validation=82):
        raise ValueError("Unexpected group counts")
    return allowed, groups, dict(all_counts)


def metadata_audit(allowed):
    path = DATA / "metadata.csv"
    all_rows, header = csv_rows(path)
    records = {}
    for row in all_rows:
        sid = "slide_" + row.get("id", "").zfill(4)
        if sid in allowed:
            if sid in records:
                raise ValueError("Duplicate allowed metadata identifier: " + sid)
            records[sid] = row
    if set(records) != set(allowed):
        raise ValueError("Missing train/validation metadata")
    fields = {}
    for key in header:
        values = [r.get(key) for r in records.values()]
        present = [v for v in values if v is not None]
        nonempty = [v for v in present if v != ""]
        counter = Counter(nonempty)
        number_values = []
        parsed_kinds = Counter()
        for value in nonempty:
            try:
                integer = int(value)
                parsed_kinds["integer-string"] += 1
                number_values.append(integer)
            except ValueError:
                try:
                    number_values.append(float(value))
                    parsed_kinds["number-string"] += 1
                except ValueError:
                    parsed_kinds["string"] += 1
        fields[key] = {
            "csv_type": "string", "applicable_rows": len(values),
            "present": len(present), "missing": len(values) - len(present),
            "empty": len(present) - len(nonempty),
            "missing_or_empty_fraction": (len(values) - len(nonempty)) / len(values),
            "parsed_value_kinds": dict(parsed_kinds), "distinct_values": len(counter),
        }
        if len(counter) <= 12:
            fields[key]["value_counts"] = dict(sorted(counter.items()))
        if number_values:
            fields[key]["numeric_summary"] = numeric_summary(number_values)
    dimensions = Counter((int(r["image_width"]), int(r["image_height"])) for r in records.values())
    ratios = Counter(f"{w}/{h}={w / h:.8f}" for (w, h), count in dimensions.items() for _ in range(count))
    return records, {"path": str(path), "sha256": sha256_file(path), "header": header,
                     "scope": "train/validation only after filtering metadata identifiers",
                     "rows_audited": len(records), "fields": fields,
                     "dimension_counts_px": {f"{w}x{h}": n for (w, h), n in sorted(dimensions.items())},
                     "aspect_ratio_counts": dict(sorted(ratios.items())),
                     "source_type_feature_policy": "audit strata only; excluded from model input",
                     "alteration_feature_policy": "augmentation identity; excluded from model input"}


def clipped_example(element):
    """Keep one real small example per element type, not a raw-data dump."""
    result = json.loads(json.dumps(element, ensure_ascii=False))
    if "textContent" in result and len(result["textContent"]) > 110:
        result["textContent"] = result["textContent"][:110] + " [truncated]"
    if "table" in result and "cells" in result["table"]:
        cells = result["table"]["cells"]
        result["table"]["cells"] = cells[:2]
        result["table"]["example_cells_shown"] = min(2, len(cells))
    return result


def collect_keys(value, path="$", result=None):
    result = [] if result is None else result
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + "." + key
            result.append((key, child_path))
            collect_keys(child, child_path, result)
    elif isinstance(value, list):
        for child in value:
            collect_keys(child, path + "[]", result)
    return result


def descriptions_audit(allowed, groups):
    schema = SchemaAudit()
    schema_by_type = defaultdict(SchemaAudit)
    types = Counter()
    page_metrics = defaultdict(list)
    pages_with = Counter()
    text_metrics = Counter()
    coords = Counter()
    coords_by_type = defaultdict(Counter)
    coord_ranges = defaultdict(list)
    examples = {}
    manifest = []
    signatures = {}
    found_terms = defaultdict(list)
    key_families = {
        "font_or_typeface": r"font|typeface",
        "text_style": r"bold|italic|underline|style|strike",
        "color": r"color|colour",
        "background_or_fill": r"background|fill",
        "rotation": r"rotat|angle",
        "z_order_or_stacking": r"z.?order|z.?index|stack",
        "label_like_keys": r"response|deficien|agreement|quality.?score|rating|annotation|bounding.?box",
    }
    family_pages = {key: set() for key in key_families}
    family_paths = {key: Counter() for key in key_families}
    table_counts = Counter()
    for sid in sorted(allowed):
        path = DATA / "descriptions" / (sid + ".json")
        payload = path.read_bytes()
        manifest.append([sid, sha256_bytes(payload)])
        doc = json.loads(payload.decode("utf-8-sig"))
        if doc.get("slide_id") != sid or not isinstance(doc.get("elements"), list):
            raise ValueError("Description identity/elements mismatch: " + sid)
        schema.visit(doc)
        for key, key_path in collect_keys(doc):
            for family, pattern in key_families.items():
                if re.search(pattern, key, re.IGNORECASE):
                    family_pages[family].add(sid)
                    family_paths[family][key_path] += 1
        elements = doc["elements"]
        counts = Counter(e.get("type", "<missing>") for e in elements)
        page_metrics["objects"].append(len(elements))
        for kind in ("TEXT_BOX", "IMAGE", "LINE", "TABLE"):
            page_metrics[kind].append(counts[kind])
            pages_with[kind] += bool(counts[kind])
        type_sequence, text_sequence, geo_sequence = [], [], []
        total_chars, nonempty_text = 0, 0
        for element in elements:
            if not isinstance(element, dict):
                raise ValueError("Non-object description element: " + sid)
            kind = element.get("type", "<missing>")
            types[kind] += 1
            schema_by_type[kind].visit(element)
            examples.setdefault(kind, {"slide_id": sid, "element": clipped_example(element)})
            type_sequence.append(kind)
            table_text = tuple(c.get("textContent") for c in element.get("table", {}).get("cells", []))
            text_sequence.append((element.get("textContent"), table_text))
            norm = element.get("positionAndSize", {}).get("normalized")
            geo_sequence.append(norm)
            kind_coords = coords_by_type[kind]
            kind_coords["objects"] += 1
            if isinstance(norm, dict):
                coords["normalized_container_present"] += 1
                kind_coords["normalized_container_present"] += 1
                values = [norm.get(key) for key in ("x", "y", "width", "height")]
                if all(finite_number(value) for value in values):
                    coords["all_four_finite_numeric"] += 1
                    kind_coords["all_four_finite_numeric"] += 1
                    x, y, width, height = values
                    for key, value in zip(("x", "y", "width", "height"), values):
                        coord_ranges[key].append(value)
                    coords["width_zero"] += width == 0
                    coords["height_zero"] += height == 0
                    coords["zero_width_or_height"] += width == 0 or height == 0
                    coords["negative_width_or_height"] += width < 0 or height < 0
                    outside = x < -1e-9 or y < -1e-9 or x + width > 1 + 1e-9 or y + height > 1 + 1e-9
                    coords["outside_unit_page_bbox"] += outside
                    for counter_key, value in (
                        ("width_zero", width == 0), ("height_zero", height == 0),
                        ("zero_width_or_height", width == 0 or height == 0),
                        ("negative_width_or_height", width < 0 or height < 0),
                        ("outside_unit_page_bbox", outside),
                    ):
                        kind_coords[counter_key] += value
                else:
                    coords["invalid_or_nonfinite_coordinates"] += 1
                    kind_coords["invalid_or_nonfinite_coordinates"] += 1
            else:
                coords["normalized_container_missing_or_invalid"] += 1
                kind_coords["normalized_container_missing_or_invalid"] += 1
            if kind == "TEXT_BOX":
                text = element.get("textContent")
                text_metrics["text_box_count"] += 1
                text_metrics["string_textContent"] += isinstance(text, str)
                if isinstance(text, str):
                    text_metrics["empty_textContent"] += text == ""
                    text_metrics["whitespace_only_including_empty"] += text.strip() == ""
                    text_metrics["characters"] += len(text)
                    total_chars += len(text)
                    nonempty_text += bool(text)
                    for label in LABELS:
                        if label.casefold() in text.casefold():
                            found_terms[label].append(sid)
            if kind == "TABLE":
                table = element.get("table", {})
                cells = table.get("cells", [])
                table_counts["tables"] += 1
                table_counts["cells"] += len(cells)
                table_counts["empty_cell_text"] += sum(c.get("textContent") == "" for c in cells)
                table_counts["cell_characters"] += sum(len(c["textContent"]) for c in cells if isinstance(c.get("textContent"), str))
                table_counts["rows_times_columns_mismatch"] += table.get("rows", 0) * table.get("columns", 0) != len(cells)
        page_metrics["text_box_characters"].append(total_chars)
        page_metrics["nonempty_text_boxes"].append(nonempty_text)
        signatures[sid] = {"types": json_hash(type_sequence), "text": json_hash(text_sequence),
                           "geometry": json_hash(geo_sequence)}
    invariance = Counter()
    per_group_changes = []
    for gid, rows in sorted(groups.items()):
        variant = {}
        for key in ("types", "text", "geometry"):
            distinct = len({signatures[row["slide_id"]][key] for row in rows})
            invariance[key + "_identical_across_four"] += distinct == 1
            invariance[key + "_varies_across_four"] += distinct > 1
            variant[key + "_distinct_sequences"] = distinct
        per_group_changes.append({"group_id": gid, **variant})
    family_stats = {
        family: {"page_count": len(family_pages[family]), "missing_page_count": len(allowed) - len(family_pages[family]),
                 "matched_key_paths": dict(sorted(family_paths[family].items()))}
        for family in key_families
    }
    pair_comparisons = []
    for first, second in KNOWN_CONFLICT_PAIRS:
        pair_comparisons.append({
            "slide_ids": [first, second],
            "type_sequence_equal": signatures[first]["types"] == signatures[second]["types"],
            "text_sequence_equal": signatures[first]["text"] == signatures[second]["text"],
            "normalized_geometry_sequence_equal": signatures[first]["geometry"] == signatures[second]["geometry"],
            "comparison_excludes": ["slide_id", "objectId", "image asset filename"],
            "text_comparison_includes": ["TEXT_BOX textContent", "table cell textContent"],
        })
    return {"directory": str(DATA / "descriptions"), "documents": len(allowed),
            "input_manifest_sha256": json_hash(manifest), "schema": schema.result(),
            "schema_by_element_type": {kind: item.result() for kind, item in sorted(schema_by_type.items())},
            "object_type_counts": dict(types), "object_count": sum(types.values()),
            "page_counts_with_object_type": dict(pages_with),
            "page_metric_summaries": {key: numeric_summary(values) for key, values in sorted(page_metrics.items())},
            "text_metrics": dict(text_metrics), "table_metrics": dict(table_counts),
            "coordinate_metrics": dict(coords),
            "coordinate_metrics_by_element_type": {kind: dict(counts) for kind, counts in sorted(coords_by_type.items())},
            "coordinate_ranges": {key: numeric_summary(values) for key, values in sorted(coord_ranges.items())},
            "coordinate_units": "nominal page-normalized coordinates per README/key name; x,width relative to page width and y,height to page height are the working interpretation for unit-page diagnostics, not verified against original PPTX or image-object alignment",
            "field_family_key_scan": family_stats, "small_real_examples": examples,
            "literal_target_name_occurrences_in_text_boxes": {key: sorted(set(value)) for key, value in sorted(found_terms.items())},
            "literal_scan_limit": "A zero target-name count cannot prove absence of semantic answer content or independence from annotations.",
            "same_group_sequence_invariance": {"groups": len(groups), **dict(invariance)},
            "known_conflict_pair_description_comparison": pair_comparisons,
            "schema_denominator_note": "Global element schema uses all element dictionaries; schema_by_element_type gives semantically applicable TEXT_BOX/IMAGE/LINE/TABLE denominators for their optional fields.",
            "provenance_status": "object-like JSON exists; exact extraction pipeline and independence from annotation generation are not established by field presence"}


def annotations_audit(allowed, groups, metadata):
    schema = SchemaAudit()
    labels = {}
    manifest = []
    entry_counts = Counter()
    bbox_by_label = defaultdict(Counter)
    source = defaultdict(lambda: {"pages": 0, "positive": [0] * len(LABELS)})
    split_counts = defaultdict(lambda: {"pages": 0, "positive": [0] * len(LABELS)})
    for sid in sorted(allowed):
        path = DATA / "annotations" / (sid + ".json")
        payload = path.read_bytes()
        manifest.append([sid, sha256_bytes(payload)])
        doc = json.loads(payload.decode("utf-8-sig"))
        if doc.get("slide_id") != int(sid.split("_")[1]):
            raise ValueError("Annotation identity mismatch: " + sid)
        schema.visit(doc)
        entries = doc.get("annotations")
        if not isinstance(entries, list):
            raise ValueError("Missing annotations: " + sid)
        entry_counts[len(entries)] += 1
        entries_by_label = defaultdict(list)
        for entry in entries:
            label = entry.get("design_deficiency")
            entries_by_label[label].append(entry)
            if label in LABELS:
                stats = bbox_by_label[label]
                stats["target_entries"] += 1
                stats["bbox_field_present"] += "bounding_boxes" in entry
                stats["bbox_empty_list"] += entry.get("bounding_boxes") == []
                stats["bbox_nonempty_list"] += isinstance(entry.get("bounding_boxes"), list) and bool(entry["bounding_boxes"])
                if isinstance(entry.get("bounding_boxes"), list):
                    stats["total_annotated_boxes"] += len(entry["bounding_boxes"])
        values = []
        for label in LABELS:
            matches = entries_by_label[label]
            if len(matches) != 1 or type(matches[0].get("response")) is not bool:
                raise ValueError("Missing/duplicated/non-boolean target response: " + sid + " / " + label)
            values.append(int(matches[0]["response"] is True))
        labels[sid] = values
        subset = allowed[sid]["split"]
        for stats in (split_counts[subset], source[(subset, metadata[sid]["source_type"])]):
            stats["pages"] += 1
            stats["positive"] = [a + b for a, b in zip(stats["positive"], values)]
    def finish_counts(stats):
        n = stats["pages"]
        return {"pages": n, "labels": {
            label: {"positive": positive, "negative": n - positive, "positive_fraction": positive / n}
            for label, positive in zip(LABELS, stats["positive"])}}
    for subset, expected in EXPECTED_POSITIVES.items():
        if split_counts[subset]["positive"] != expected:
            raise ValueError("Strict target labels disagree with accepted 4.3A counts: " + subset)
    mixed = defaultdict(Counter)
    for gid, rows in groups.items():
        subset = rows[0]["split"]
        for index, label in enumerate(LABELS):
            distinct = {labels[row["slide_id"]][index] for row in rows}
            mixed[subset][label] += len(distinct) == 2
    all_mixed = {label: sum(value[label] for value in mixed.values()) for label in LABELS}
    return labels, {"directory": str(DATA / "annotations"), "documents": len(allowed),
                    "input_manifest_sha256": json_hash(manifest), "schema": schema.result(),
                    "entries_per_document": dict(sorted(entry_counts.items())),
                    "target_bbox_presence": {key: dict(value) for key, value in sorted(bbox_by_label.items())},
                    "strict_label_rule": "Exactly one target entry; type(response) is bool; positive iff response is True",
                    "label_order": list(LABELS),
                    "split_label_counts": {key: finish_counts(value) for key, value in sorted(split_counts.items())},
                    "source_label_counts": [{"split": subset, "source_type": name, **finish_counts(value)}
                                            for (subset, name), value in sorted(source.items())],
                    "mixed_positive_negative_four_page_groups": {
                        "by_split": {key: dict(value) for key, value in sorted(mixed.items())},
                        "all_trainval": all_mixed,
                    },
                    "feature_policy": "Every annotation field (including bbox, agreement, deficiency, response and derived values) is forbidden as input; labels used only for this audit"}


def png_audit(allowed, labels, metadata):
    expected_rows, _ = csv_rows(BASE / "features/feature_index.csv")
    expected_hash = {row["slide_id"]: row["image_sha256"] for row in expected_rows if row["slide_id"] in allowed}
    plan_rows, _ = csv_rows(BASE / "outputs/image_download_plan.csv")
    plan = {row["slide_id"]: row for row in plan_rows if row["slide_id"] in allowed}
    if set(expected_hash) != set(allowed) or set(plan) != set(allowed):
        raise ValueError("Historical image provenance rows missing")
    manifest, by_sha, anomalies = [], defaultdict(list), []
    total_bytes = 0
    dimensions = Counter()
    missing = []
    for sid in sorted(allowed):
        path = BASE / "data/images" / (sid + ".png")
        if not path.is_file():
            missing.append(sid)
            continue
        payload = path.read_bytes()
        digest = sha256_bytes(payload)
        blob = hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
        manifest.append([sid, digest])
        by_sha[digest].append(sid)
        total_bytes += len(payload)
        if digest != expected_hash[sid]:
            anomalies.append({"slide_id": sid, "kind": "changed_since_4.3C", "sha256": digest})
        if len(payload) != int(plan[sid]["expected_size_bytes"]) or blob != plan[sid]["expected_git_blob_sha"]:
            anomalies.append({"slide_id": sid, "kind": "pinned_git_blob_or_size_mismatch"})
        if plan[sid]["source_commit"] != COMMIT:
            raise ValueError("Historical download plan source changed")
        if len(payload) < 33 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
            anomalies.append({"slide_id": sid, "kind": "PNG_IHDR_invalid"})
            continue
        width, height = struct.unpack(">II", payload[16:24])
        dimensions[(width, height)] += 1
        expected_dimensions = (int(metadata[sid]["image_width"]), int(metadata[sid]["image_height"]))
        if (width, height) != expected_dimensions:
            anomalies.append({"slide_id": sid, "kind": "metadata_dimensions_mismatch"})
        if zlib.crc32(payload[12:29]) & 0xffffffff != struct.unpack(">I", payload[29:33])[0]:
            anomalies.append({"slide_id": sid, "kind": "IHDR_CRC_invalid"})
    duplicate_sets, conflicts = [], []
    for digest, ids in sorted(by_sha.items(), key=lambda item: item[1]):
        train_ids = [sid for sid in ids if allowed[sid]["split"] == "train"]
        if len(train_ids) < 2:
            continue
        first_bytes = (BASE / "data/images" / (train_ids[0] + ".png")).read_bytes()
        byte_equal = all((BASE / "data/images" / (sid + ".png")).read_bytes() == first_bytes for sid in train_ids[1:])
        if not byte_equal:
            raise ValueError("Same SHA-256 but different PNG bytes")
        conflict_labels = [label for index, label in enumerate(LABELS)
                           if len({labels[sid][index] for sid in train_ids}) > 1]
        item = {"slide_ids": train_ids, "sha256": digest, "byte_identical": byte_equal,
                "group_ids": [allowed[sid]["group_id"] for sid in train_ids],
                "conflicting_labels": conflict_labels,
                "labels": {sid: labels[sid] for sid in train_ids}}
        duplicate_sets.append(item)
        if conflict_labels:
            conflicts.append(item)
    confirmed_pairs = []
    for first, second in KNOWN_CONFLICT_PAIRS:
        if first not in allowed or second not in allowed or any(allowed[sid]["split"] != "train" for sid in (first, second)):
            raise ValueError("Named conflict pair is not wholly train")
        matching = [item for item in conflicts if first in item["slide_ids"] and second in item["slide_ids"]]
        if len(matching) != 1:
            raise ValueError("A previously reported conflict pair could not be confirmed")
        confirmed_pairs.append(matching[0])
    return {"directory": str(BASE / "data/images"), "scope": "train/validation PNGs only",
            "checked": len(manifest), "missing": missing, "anomalies": anomalies,
            "image_manifest_sha256": json_hash(manifest), "total_bytes": total_bytes,
            "dimension_counts_px": {f"{w}x{h}": n for (w, h), n in sorted(dimensions.items())},
            "checks": ["SHA-256 matches historical feature index", "Git blob SHA-1 and byte size match pinned plan",
                       "PNG signature/IHDR/CRC and metadata dimensions", "duplicate PNGs compared byte-for-byte"],
            "decode_limit": "stdlib header/hash audit only; full image decode was checked during 4.3B and images are unchanged by hash",
            "train_duplicate_sets": len(duplicate_sets),
            "train_duplicate_pages": sum(len(item["slide_ids"]) for item in duplicate_sets),
            "train_duplicate_set_size_distribution": dict(sorted(Counter(len(item["slide_ids"]) for item in duplicate_sets).items())),
            "train_conflict_sets": len(conflicts), "train_conflict_pages": sum(len(item["slide_ids"]) for item in conflicts),
            "known_conflict_pairs": confirmed_pairs,
            "all_train_conflicts": conflicts,
            "conflict_policy": "Do not remove samples, change official labels, infer groups or change split. Consider a future train-only sensitivity analysis."}


def stage1_audit():
    path = PROJECT / "outputs/stage1_structure.json"
    if not path.is_file():
        return {"path": str(path), "available": False}
    payload = path.read_bytes()
    doc = json.loads(payload.decode("utf-8-sig"))
    shapes = [shape for deck in doc.get("decks", []) for slide in deck.get("slides", [])
              for shape in slide.get("shapes", [])]
    text = [shape for shape in shapes if shape.get("has_text") is True]
    geometry_keys = Counter(key for shape in shapes for key in shape.get("geometry", {}))
    shape_keys = Counter(key for shape in shapes for key in shape)
    dimensioned = sum(all(finite_number(deck.get(key)) for key in ("slide_width_emu", "slide_height_emu"))
                      for deck in doc.get("decks", []))
    code = {}
    for name in ("audit_stage1.py", "verify_stage1.py"):
        code_path = PROJECT / name
        if code_path.is_file():
            code[name] = {"path": str(code_path), "sha256": sha256_file(code_path)}
    return {"path": str(path), "sha256": sha256_bytes(payload), "available": True,
            "summary_from_file": doc.get("summary"), "decks_with_slide_dimensions": dimensioned,
            "shape_count_recounted": len(shapes), "text_shapes": len(text),
            "shape_type_counts": dict(Counter(shape.get("shape_type") for shape in shapes)),
            "shape_keys_presence": dict(sorted(shape_keys.items())),
            "geometry_keys_presence": dict(sorted(geometry_keys.items())),
            "shapes_with_geometry": sum(isinstance(shape.get("geometry"), dict) for shape in shapes),
            "shapes_with_bbox": sum(isinstance(shape.get("bbox"), dict) for shape in shapes),
            "text_shapes_with_nonempty_text": sum(isinstance(shape.get("text"), str) and bool(shape["text"]) for shape in text),
            "text_shapes_with_explicit_font_sizes": sum(bool(shape.get("explicit_font_sizes_pt")) for shape in text),
            "text_shapes_without_explicit_font_sizes": sum(not bool(shape.get("explicit_font_sizes_pt")) for shape in text),
            "explicit_font_unit": "points (pt); only directly specified run sizes, not resolved effective inherited font sizes",
            "geometry_units": "EMU and inch; 914400 EMU per inch; normalized comparison requires per-deck width/height",
            "source_files": code,
            "not_slideaudit": "These stage1 outputs describe six different local PPTX files, not SlideAudit source PPTX; no row-wise join is assumed."}


def repository_audit():
    environment = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    def run(*args):
        completed = subprocess.run(["git", "-C", str(REPO), *args], env=environment,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if completed.returncode:
            raise RuntimeError("Local git metadata unavailable without fetching: " + completed.stderr.strip())
        return completed.stdout
    try:
        head = run("rev-parse", "HEAD").strip()
        if head != COMMIT:
            raise ValueError("Local SlideAudit HEAD differs from fixed source commit")
        paths = run("ls-tree", "-r", "--name-only", COMMIT).splitlines()
        readme = run("cat-file", "-p", COMMIT + ":README.md")
        relevant_lines = [line for line in readme.splitlines()
                          if re.search(r"descri|annotat|pptx|powerpoint|structur|text|schema|license", line, re.I)]
        return {"path": str(REPO), "head": head, "expected_commit": COMMIT,
                "network_allowed": False, "git_no_lazy_fetch": True,
                "tracked_paths": len(paths),
                "pptx_paths": sum(path.lower().endswith(".pptx") for path in paths),
                "png_paths": sum(path.lower().endswith(".png") for path in paths),
                "description_json_paths": sum(path.startswith("data/descriptions/") and path.endswith(".json") for path in paths),
                "annotation_json_paths": sum(path.startswith("data/annotations/") and path.endswith(".json") for path in paths),
                "readme_sha256": sha256_bytes(readme.encode("utf-8")),
                "readme_relevant_excerpt": relevant_lines[:16],
                "source_pipeline_provenance": "Do not infer generator/extraction method from field names or objectId strings; inspect source documentation separately."}
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return {"path": str(REPO), "expected_commit": COMMIT,
                "network_allowed": False, "git_no_lazy_fetch": True,
                "metadata_error": str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print complete audit JSON (still stdout only)")
    args = parser.parse_args()
    allowed, groups, split_counts = load_scope()
    metadata, metadata_result = metadata_audit(allowed)
    descriptions = descriptions_audit(allowed, groups)
    labels, annotations = annotations_audit(allowed, groups, metadata)
    png = png_audit(allowed, labels, metadata)
    stage1 = stage1_audit()
    repository = repository_audit()
    result = {
        "audit": "4.3F-1 read-only structural feasibility/leakage audit",
        "scope": {"split_path": str(SPLIT_PATH), "split_sha256": sha256_file(SPLIT_PATH),
                  "frozen_split_counts": split_counts, "raw_pages_read": len(allowed),
                  "raw_train_pages": 1512, "raw_validation_pages": 328,
                  "raw_test_pages": 0, "raw_excluded_pages": 0,
                  "group_count": len(groups), "no_features_models_or_reports_written": True},
        "metadata": metadata_result, "descriptions": descriptions,
        "annotations_labels_only": annotations, "png_quality": png,
        "stage1_existing_output": stage1, "local_repository": repository,
        "candidate_field_policy": {
            "A_safe_reproducible_now": [
                "PNG width/height aspect ratio from the same rendering pipeline",
                "Prospective PNG-only edge density, fixed-threshold blank fraction, brightness/color distribution, grid density and projection summaries (not extracted in this audit)",
            ],
            "B_requires_provenance_and_semantic_validation": [
                "Description element and TEXT_BOX/IMAGE/TABLE/LINE counts",
                "Description objective text lengths (no semantic embeddings or quality conclusions)",
                "Normalized object rectangles and derived geometry, only after provenance/type/rotation/z-order coverage checks",
                "PPTX explicit font sizes available at deployment but absent from SlideAudit descriptions; not trainable as a matched feature here",
            ],
            "C_forbidden_or_unavailable": [
                "Any annotation field, agreement, human deficiency bbox, quality/defect verdict and label-derived value",
                "slide_id, group_id, split, source_type, alteration, objectId, asset filename or label-correlated IDs as classifier features",
                "Invented font sizes, styles, background, rotation or z-order values when no data field provides them",
            ],
        },
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = {"scope": result["scope"],
                   "metadata_fields": metadata_result["header"],
                   "description_objects": descriptions["object_type_counts"],
                   "description_field_families": descriptions["field_family_key_scan"],
                   "description_coordinates": descriptions["coordinate_metrics"],
                   "group_sequence_invariance": descriptions["same_group_sequence_invariance"],
                   "label_counts": annotations["split_label_counts"],
                   "duplicate_train_png_sets": png["train_duplicate_sets"],
                   "duplicate_train_png_pages": png["train_duplicate_pages"],
                   "conflicts": png["known_conflict_pairs"],
                   "missing_png": png["missing"], "png_anomalies": png["anomalies"],
                   "stage1_font_missing": stage1.get("text_shapes_without_explicit_font_sizes"),
                   "local_repository": repository,
                   "full_json": "Re-run with --json for field presence/types/missing fractions and detailed input digests."}
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
