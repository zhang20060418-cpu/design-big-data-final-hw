"""Pixel-only, local bitmap stroke evidence for D6 material review.

This does not decide whether marks are readable language, AI artifacts, or
semantically correct. A response only prioritizes a human review of an existing
bitmap. No response cannot certify the bitmap normal. No OCR/network/model is
used. Original render pixels and normalized native image envelopes are inputs.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage


VERSION = "local-bitmap-stroke-review-v1-20261030"
DEFAULT_CONFIG = {
    "max_roi_side": 512,
    "minimum_page_area": .025,
    "minimum_roi_side_px": 64,
    "minimum_flat_fraction": .80,
    "minimum_stroke_components": 8,
    "minimum_row_groups": 2,
}


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _rect(bbox):
    if not isinstance(bbox, dict) or not all(_finite(bbox.get(k)) for k in ("x", "y", "w", "h")):
        return None
    x, y, w, h = [float(bbox[k]) for k in ("x", "y", "w", "h")]
    if w <= 0 or h <= 0:
        return None
    x0, y0, x1, y1 = max(0., x), max(0., y), min(1., x+w), min(1., y+h)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _row_groups(components):
    """Group repeated stroke-sized blobs by vertical center; not text reading."""
    rows = []
    for component in sorted(components, key=lambda c: c["cy"]):
        choices = [(abs(c["cy"]-component["cy"]), c) for c in rows
                   if abs(c["cy"]-component["cy"]) <= .55*max(c["height"], component["height"])]
        if choices:
            _, row = min(choices, key=lambda x: x[0])
            row["items"].append(component)
            row["cy"] = float(np.median([c["cy"] for c in row["items"]]))
            row["height"] = float(np.median([c["height"] for c in row["items"]]))
        else:
            rows.append({"cy": component["cy"], "height": component["height"], "items": [component]})
    return [{"component_count": len(r["items"]), "cy": r["cy"], "height": r["height"],
             "left": min(c["x"] for c in r["items"]),
             "right": max(c["x"]+c["w"] for c in r["items"])}
            for r in rows if len(r["items"]) >= 3]


def measure_roi(image, max_side=512):
    """Deterministic low-level measurements, including at most 100 blob boxes."""
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((int(max_side), int(max_side)), Image.Resampling.BICUBIC)
    rgb = np.asarray(image, dtype=np.float32)/255.
    h, w = rgb.shape[:2]
    if min(h, w) < 3:
        raise ValueError("ROI too small for pixel measurements")
    gray = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
    # Flat neighboring patches distinguish clean diagrams from textured photos;
    # it does not distinguish valid text from pseudo-text.
    dx = np.max(np.abs(rgb[:, 1:] - rgb[:, :-1]), axis=2)
    dy = np.max(np.abs(rgb[1:] - rgb[:-1]), axis=2)
    flat = np.zeros((h, w), dtype=bool)
    flat[:-1, :-1] = (dx[:-1] < .018) & (dy[:, :-1] < .018)
    # Local contrast keeps dark text on pale/color fields. Light-on-dark glyphs
    # are included by the symmetric local residual. Uniform fields stay empty.
    local = ndimage.gaussian_filter(gray, sigma=max(2., min(h, w)/50.))
    residual = np.abs(local-gray)
    mask = (residual > .075) & (np.abs(gray-np.median(gray)) > .10)
    # Join anti-aliased fragments within a glyph, not across words/line gaps.
    mask = ndimage.binary_closing(mask, structure=np.ones((2, 2), dtype=bool))
    labels, _ = ndimage.label(mask)
    objects = ndimage.find_objects(labels)
    sizes = np.bincount(labels.ravel())
    components = []
    for number, slices in enumerate(objects, 1):
        if slices is None:
            continue
        sy, sx = slices
        bh, bw, area = sy.stop-sy.start, sx.stop-sx.start, int(sizes[number])
        # Loose stroke-like geometry, not a font/character recognizer. Long
        # chart lines and tiny photo noise are filtered for queue prioritizing.
        if (5 <= bh <= .20*h and 3 <= bw <= .25*w and
                .12 <= bw/bh <= 4. and .10 <= area/(bh*bw) <= .90 and area >= 12):
            components.append({"x": sx.start/w, "y": sy.start/h, "w": bw/w, "h": bh/h,
                               "cy": (sy.start+sy.stop)/2/h, "height": bh/h,
                               "fill_fraction": area/(bh*bw), "area_px": area})
    rows = _row_groups(components)
    return {"analysis_width": w, "analysis_height": h,
            "flat_neighbor_fraction": float(flat.mean()),
            "local_stroke_mask_fraction": float(mask.mean()),
            "stroke_component_count": len(components), "row_group_count": len(rows),
            "row_groups": rows, "stroke_component_boxes": components[:100],
            "foreground_is_semantic_text": "unverified"}


def analyze_page(ctx, png_path=None, config=None):
    """Inspect bitmap ROIs; safe for deployment without manifest/label fields.

    ``ctx`` needs objects[].id/kind/bbox and page.png_path. Path is used only
    to read original pixels; it is never a feature. A05 semantic availability
    remains unsupported even when targeted_review_candidate is returned.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    images = [o for o in ctx.get("objects", []) if o.get("kind") == "image"]
    answer = {"version": VERSION, "status": "no_targeted_review_candidate", "score": 0.,
              "score_kind": "ordinal_review_priority_not_defect_probability",
              "semantic_artifact_status": "unsupported", "coverage": "partial",
              "bitmap_count": len(images), "rois": [], "evidence": [],
              "requires_unresolved_material_review": bool(images), "config": cfg,
              "limitations": ["Stroke statistics do not confirm unreadable text or AI authorship.",
                              "Non-triggering photos, icons and diagrams can still contain artifacts.",
                              "The caller must count targeted review burden separately from fully unresolved material review."]}
    if not images:
        return answer
    path = png_path or ctx.get("page", {}).get("png_path")
    if not path:
        answer.update(status="unknown", score=None, error="original_png_path_unavailable")
        return answer
    try:
        with Image.open(Path(path)) as raw:
            raw.load()
            full = raw.convert("RGB")
    except (OSError, ValueError) as exc:
        answer.update(status="unknown", score=None, error=f"original_png_unreadable:{type(exc).__name__}")
        return answer
    for obj in images:
        rect = _rect(obj.get("bbox"))
        entry = {"object_id": obj.get("id"), "status": "not_prioritized",
                 "semantic_artifact_status": "uninspected"}
        if rect is None:
            entry.update(status="unknown", reason="invalid_image_envelope")
            answer["rois"].append(entry)
            continue
        x0, y0, x1, y1 = rect
        box = (int(math.floor(x0*full.width)), int(math.floor(y0*full.height)),
               int(math.ceil(x1*full.width)), int(math.ceil(y1*full.height)))
        entry.update(bbox={"x": x0, "y": y0, "w": x1-x0, "h": y1-y0},
                     pixel_box=list(box), source="existing_original_render_native_image_envelope",
                     normalized_area=(x1-x0)*(y1-y0))
        if (entry["normalized_area"] < cfg["minimum_page_area"] or
                min(box[2]-box[0], box[3]-box[1]) < cfg["minimum_roi_side_px"]):
            entry["reason"] = "small_material_not_prioritized_semantics_still_unchecked"
            answer["rois"].append(entry)
            continue
        measurements = measure_roi(full.crop(box), cfg["max_roi_side"])
        trigger = (measurements["flat_neighbor_fraction"] >= cfg["minimum_flat_fraction"] and
                   measurements["stroke_component_count"] >= cfg["minimum_stroke_components"] and
                   measurements["row_group_count"] >= cfg["minimum_row_groups"])
        entry.update(status="targeted_review_candidate" if trigger else "not_prioritized",
                     measurements=measurements,
                     reason="repeated_stroke_like_marks_on_flat_diagram_request_readability_check" if trigger
                     else "pixel_hypothesis_not_triggered_semantics_still_unchecked")
        answer["rois"].append(entry)
        if trigger:
            score = min(.95, .50 + .01*measurements["stroke_component_count"])
            answer["score"] = max(answer["score"], score)
            answer["evidence"].append({"mechanism": "local_bitmap_stroke_readability_review",
                                       "object_ids": [obj.get("id")], "bbox": entry["bbox"],
                                       "measurements": measurements})
    if answer["evidence"]:
        answer["status"] = "targeted_review_candidate"
    elif any(r["status"] == "unknown" for r in answer["rois"]):
        answer.update(status="unknown", score=None)
    return answer
