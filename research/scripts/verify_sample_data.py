"""Verify the small public subset, labels, original split and source hashes."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

LABELS = {
    "improper_font_sizing": "Improper Font Sizing",
    "occluded_content": "Occluded Content",
    "cluttered_layout": "Cluttered Layout",
    "content_overflow_cutoff": "Content Overflow/Cut-off",
}


def main() -> None:
    data = Path(__file__).resolve().parents[1] / "sample_data"
    with (data / "manifest.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    groups = {"train": set(), "validation": set()}
    hashes = set()
    counts = {split: {name: 0 for name in LABELS} for split in groups}
    for row in rows:
        split, sid = row["split"], row["slide_id"]
        assert split in groups and row["source_type"] == "Gemini"
        groups[split].add(row["group_id"])
        image = data / "images" / f"{sid}.png"
        annotation = data / "annotations" / f"{sid}.json"
        description = data / "descriptions" / f"{sid}.json"
        for path, column in [(image, "image_sha256"), (annotation, "annotation_sha256"), (description, "description_sha256")]:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row[column], path
        assert row["image_sha256"] not in hashes
        hashes.add(row["image_sha256"])
        values = {a["design_deficiency"]: a["response"] for a in json.loads(annotation.read_text(encoding="utf-8"))["annotations"]}
        for slug, label in LABELS.items():
            assert type(values[label]) is bool
            assert int(row[slug]) == int(values[label])
            counts[split][slug] += int(row[slug])
    assert not groups["train"] & groups["validation"]
    assert sum(r["split"] == "train" for r in rows) == 32
    assert sum(r["split"] == "validation" for r in rows) == 8
    print(json.dumps({"status": "passed", "pages": len(rows), "train": 32, "validation": 8,
                      "cross_split_groups": 0, "positive_counts": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
