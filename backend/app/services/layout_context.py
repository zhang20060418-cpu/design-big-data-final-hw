"""Live screenshot checks for geometric whitespace; frozen research rules stay intact.

Texture is evidence against a region being visually empty, not proof of good
design. Conflicts become unassessed visual-review scope rather than negatives.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

from PIL import Image, ImageStat

VERSION = "rendered-layout-context-v1"


def _box(value):
    if not isinstance(value, dict): return None
    if not all(isinstance(value.get(k), (float, int)) and math.isfinite(value[k]) for k in ("x", "y", "w", "h")): return None
    return value if value["w"] > 0 and value["h"] > 0 else None


def _contains(box, x, y):
    return box["x"]-.003 <= x <= box["x"]+box["w"]+.003 and box["y"]-.003 <= y <= box["y"]+box["h"]+.003


def inspect_void(path: Path, region: dict, context: dict) -> dict:
    """Measure local RGB variation outside actual nonempty text paragraphs.

    Uniform coloured fills and smooth gradients are not counted as content.
    No background semantics, glyph identity, quality score or labels are inferred.
    """
    text = [b for obj in context.get("objects", []) for p in obj.get("paragraphs", [])
            if p.get("text_chars", 0) and (b := _box(p.get("bbox")))]
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((480, 270), Image.Resampling.LANCZOS)
    width, height = image.size
    left, top = max(0, round(region["x"]*width)), max(0, round(region["y"]*height))
    right, bottom = min(width, round((region["x"]+region["w"])*width)), min(height, round((region["y"]+region["h"])*height))
    cells = textured = 0
    for y in range(top, bottom-10, 15):
        for x in range(left, right-10, 15):
            if any(_contains(b, (x+7.5)/width, (y+7.5)/height) for b in text): continue
            patch = image.crop((x, y, min(right, x+15), min(bottom, y+15)))
            cells += 1
            textured += max(ImageStat.Stat(patch).stddev) >= 12
    fraction = textured/max(1, cells)
    return {"sampled_cells": cells, "visually_structured_fraction": round(fraction, 4),
            "native_text_regions_excluded": len(text), "cell_stddev_reference": 12,
            "conflicts_with_empty_region": cells >= 16 and fraction >= .20,
            "semantic_content_confirmed": False, "measurement_source": "actual_full_page_render_local_RGB_variation"}


def refine_results(raw: dict, contexts: list[dict]) -> dict:
    """Apply a production context guard after immutable detector inference."""
    result = deepcopy(raw)
    index = {int(c["page_index"]): c for c in contexts}
    for page in result.get("pages", []):
        detector = page.get("detectors", {}).get("D5", {})
        route = page.get("screening_route", {})
        decision = route.get("decisions", {}).get("D5", {})
        if not decision.get("automatic_risk_candidate"): continue
        context = index.get(int(page["slide_index"]), {})
        path = (context.get("page") or {}).get("png_path")
        if not path: continue
        checks = []
        for evidence in detector.get("evidence", []):
            region = _box(evidence.get("bbox"))
            if not region: continue
            try: check = inspect_void(Path(path), region, context)
            except (OSError, ValueError): continue
            checks.append({"bbox": region, **check})
        conflicts = [c for c in checks if c["conflicts_with_empty_region"]]
        detector.setdefault("features", {})["render_context_guard"] = {"version": VERSION, "checks": checks,
            "native_geometry_score": detector.get("score"), "semantic_intent_confirmed": False}
        if not conflicts: continue
        reason = "rendered_visual_structure_in_claimed_empty_region"
        detector["status"] = "unknown"
        detector["version"] = str(detector.get("version", "")) + "+" + VERSION
        detector.setdefault("unresolved", []).append({"kind": reason, "bbox": conflicts[0]["bbox"]})
        detector.setdefault("limitations", []).append("原生几何判为空白的区域在渲染图中含背景、装饰或其他视觉结构；暂不列为留白缺陷，交由整页视觉复核。")
        detector.setdefault("evidence", []).append({"kind": reason, "bbox": conflicts[0]["bbox"],
            "object_ids": [], "measurement_source": conflicts[0]["measurement_source"], "metrics": conflicts[0]})
        decision.update(automatic_risk_candidate=False, review_required=True, source_status="unknown")
        decision.setdefault("critical_review_reasons", []).append(reason)
        decision.setdefault("semantic_unresolved", []).append({"kind": reason, "background_intent_unknown": True})
        route["candidate_labels"] = [k for k in route.get("candidate_labels", []) if k != "D5"]
        route["uncertain_labels"] = sorted(set(route.get("uncertain_labels", []) + ["D5"]))
        route.setdefault("semantic_unresolved", {})["D5"] = decision["semantic_unresolved"]
        route.update(review_required=True, route="llm_review")
    return result
