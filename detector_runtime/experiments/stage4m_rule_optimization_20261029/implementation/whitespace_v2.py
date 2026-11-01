"""D5 development rule: layered measured content and continuous empty regions.

Only a shared context is consumed. IDs are emitted as evidence, never used as
features. Unknown pixels block empty-space measurements and remain unknown.
The score is an engineering severity index, not a calibrated probability.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np


VERSION = "whitespace-layered-continuous-v2.1"
DEFAULT_CONFIG = {
    "version": VERSION,
    "unknown_area_max": .05,
    "canvas_margin_x": .04,
    "canvas_margin_top": .04,
    "canvas_bottom": .92,
    "header_top_max": .18,
    "header_bottom_max": .28,
    "header_gap": .04,
    "body_occupancy_max": .16,
    "cluster_bounds_area_max": .24,
    "cluster_offset_min": .30,
    "cluster_empty_min": .30,
    "row_occupancy_max": .09,
    "row_side_empty_min": .24,
    "row_span_x_min": .50,
    "row_span_x_max": .75,
    "row_anchor_margin_max": .12,
    "row_centroid_side_max": .42,
    "row_bands_min": 3,
    "row_band_gap": .025,
    "eligible_roles": ("body", "chart", "contents"),
    "protected_roles": ("cover", "section", "ending"),
}


def _valid_mask(value):
    return (isinstance(value, np.ndarray) and value.ndim == 2
            and value.dtype == np.bool_ and bool(value.size))


def _box(value):
    if not isinstance(value, Mapping):
        return None
    try:
        x, y, w, h = (float(value[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or min(w, h) <= 0:
        return None
    left, top, right, bottom = max(0., x), max(0., y), min(1., x + w), min(1., y + h)
    if right <= left or bottom <= top:
        return None
    return dict(x=left, y=top, w=right - left, h=bottom - top)


def _paint(mask, box):
    height, width = mask.shape
    left, top = math.floor(box["x"] * width), math.floor(box["y"] * height)
    right = math.ceil((box["x"] + box["w"]) * width)
    bottom = math.ceil((box["y"] + box["h"]) * height)
    mask[max(0, top):min(height, bottom), max(0, left):min(width, right)] = True


def largest_empty_rectangle(blocked):
    """Exact maximal rectangle on a boolean raster, without grid-cell gates."""
    if not _valid_mask(blocked):
        raise ValueError("blocked must be a nonempty two-dimensional boolean mask")
    height, width = blocked.shape
    heights = np.zeros(width, dtype=int)
    area, best = 0, None
    for row in range(height):
        heights = np.where(blocked[row], 0, heights + 1)
        stack = []
        for col in range(width + 1):
            bar = int(heights[col]) if col < width else 0
            left = col
            while stack and stack[-1][1] > bar:
                start, old = stack.pop()
                candidate = (row - old + 1, start, row + 1, col)
                size = old * (col - start)
                if size > area or (size == area and size and (best is None or candidate < best)):
                    area, best = size, candidate
                left = start
            if bar and (not stack or stack[-1][1] < bar):
                stack.append((left, bar))
    if best is None:
        return dict(area_ratio=0., bbox=None)
    top, left, bottom, right = best
    return dict(area_ratio=float(area / blocked.size),
                bbox=dict(x=left / width, y=top / height,
                          w=(right - left) / width, h=(bottom - top) / height))


def _layers(ctx, shape, cfg):
    layers = {name: np.zeros(shape, dtype=bool) for name in ("body", "title", "auxiliary", "unknown")}
    ids = {name: [] for name in layers}
    body_boxes, header_boxes, missing = [], [], []
    for obj in ctx.get("objects", []):
        content_class, role = obj.get("content_class", "unknown"), obj.get("text_role", "unknown")
        semantic = obj.get("semantic_layer")
        regions = [b for value in obj.get("content_regions", []) if (b := _box(value))]
        # Auxiliary content is absent from the original semantic content mask;
        # measured paragraph envelopes can still document this separate layer.
        if semantic in {"footer", "auxiliary"} or content_class == "auxiliary" or (semantic not in {"body", "title"} and (role in {"page_number", "navigation"} or (role == "footnote" and obj.get("role_subtype") != "local_annotation"))):
            layer = "auxiliary"
            if not regions:
                regions = [b for p in obj.get("paragraphs", []) if p.get("text_chars", 0) and (b := _box(p.get("bbox")))]
        elif content_class in {"background", "decoration"}:
            continue
        elif content_class == "unknown" or semantic == "unknown":
            layer = "unknown"
        elif semantic == "title" or (semantic != "body" and role == "title"):
            layer = "title"
        elif content_class == "information":
            layer = "body"
        else:
            layer = "unknown"

        if not regions and layer != "auxiliary":
            # Unmeasured text frames are uncertainty envelopes, never content
            # rectangles and never evidence of semantic emptiness.
            box = _box(obj.get("bbox"))
            if box:
                _paint(layers["unknown"], box)
            missing.append(obj.get("id"))
            continue
        # A short top kicker belongs with the header, even if the original
        # role adapter labelled it body. This uses only measured geometry.
        if layer == "body" and obj.get("kind") in {"text", "cell"} and regions:
            if max(r["y"] + r["h"] for r in regions) <= cfg["header_top_max"]:
                layer = "title"
        for region in regions:
            _paint(layers[layer], region)
            if layer == "body":
                body_boxes.append(region)
            if layer == "title" and region["y"] <= cfg["header_top_max"] and region["y"] + region["h"] <= cfg["header_bottom_max"]:
                header_boxes.append(region)
        if regions:
            ids[layer].append(obj.get("id"))
    return layers, ids, body_boxes, header_boxes, missing


def _bands(boxes, gap):
    bands = []
    for box in sorted(boxes, key=lambda b: b["y"]):
        top, bottom = box["y"], box["y"] + box["h"]
        if bands and top <= bands[-1][1] + gap:
            bands[-1][1] = max(bottom, bands[-1][1])
        else:
            bands.append([top, bottom])
    return bands


def _measure(mask, unknown):
    y, x = np.nonzero(mask)
    occupied = int(mask.sum())
    height, width = mask.shape
    result = dict(occupancy_lower=float(mask.mean()),
                  occupancy_upper=float((mask | unknown).mean()),
                  unknown_fraction=float((unknown & ~mask).mean()),
                  largest_known_empty_rectangle=largest_empty_rectangle(mask | unknown),
                  bounds=None, centroid=None, centroid_offset_half_diagonal=None)
    if occupied:
        centroid = [(float(x.mean()) + .5) / width, (float(y.mean()) + .5) / height]
        result.update(bounds=dict(x=float(x.min() / width), y=float(y.min() / height),
                                  w=float((x.max() - x.min() + 1) / width),
                                  h=float((y.max() - y.min() + 1) / height)),
                      centroid=centroid,
                      centroid_offset_half_diagonal=float(math.hypot(centroid[0] - .5, centroid[1] - .5) / math.sqrt(.5)))
    return result


def _to_page(box, roi):
    if box is None:
        return None
    return dict(x=roi["x"] + box["x"] * roi["w"], y=roi["y"] + box["y"] * roi["h"],
                w=box["w"] * roi["w"], h=box["h"] * roi["h"])


class WhitespaceDetector:
    """Fixed development hypotheses; no fitted, generator, or model features."""

    def __init__(self, config=None):
        provided = config or {}
        if isinstance(provided, Mapping) and "D5" in provided:
            provided = provided["D5"] or {}
        self.config = {**DEFAULT_CONFIG, **provided}
        for key, default in DEFAULT_CONFIG.items():
            if isinstance(default, float):
                value = self.config[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
                    raise ValueError(f"invalid D5 fraction {key}")
        if self.config["canvas_bottom"] <= self.config["canvas_margin_top"]:
            raise ValueError("invalid D5 body canvas")

    def detect(self, ctx):
        cfg, page, spatial = self.config, ctx.get("page", {}), ctx.get("spatial", {})
        result = dict(detector="D5", label="excessive_whitespace", version=cfg["version"],
                      status="unknown", score=None, score_kind="ordinal_heuristic_not_probability", coverage="partial",
                      features=dict(page_role=page.get("role", "unknown"),
                                    page_role_source=page.get("role_source", "unknown"),
                                    content_type=page.get("content_type", "unknown"),
                                    mask_source=spatial.get("mask_source", "unknown"),
                                    triggered_rules=[], thresholds=dict(cfg)), evidence=[],
                      limitations=["Measured paragraph envelopes approximate layout, not exact glyph ink.",
                                   "Page and object roles are engineering hypotheses; intended whitespace still needs review.",
                                   "Development rules and ordinal scores are not independent generalization evidence or calibrated probabilities."])
        native = ctx.get("availability", {}).get("native", "unknown")
        if page.get("content_type") == "image_only" or page.get("role") == "image" or native in {"unavailable", "unsupported", "missing", "failed", "not_available"}:
            result.update(status="unsupported", coverage="unsupported")
            result["limitations"].append("Raster-only or unavailable native content cannot establish semantic whitespace.")
            return result
        content, unknown = spatial.get("content_mask"), spatial.get("unknown_mask")
        if not _valid_mask(content) or not _valid_mask(unknown) or content.shape != unknown.shape:
            result["limitations"].append("Missing, malformed, or misaligned shared boolean content/unknown masks.")
            return result
        layers, ids, body_boxes, headers, missing = _layers(ctx, content.shape, cfg)
        # Shared uncertainty is never cleared by a local hypothesis. Unassigned
        # information pixels are also uncertain instead of becoming empty.
        known = layers["body"] | layers["title"] | layers["auxiliary"]
        uncertain = unknown | layers["unknown"] | (content & ~known)
        unknown_fraction = float((uncertain & ~known).mean())
        result["features"].update(layer_occupancy={k: float(v.mean()) for k, v in layers.items()},
                                  layer_object_ids=ids, missing_content_region_object_ids=missing,
                                  unknown_area_fraction=unknown_fraction,
                                  continuous_geometry_source="measured_object_content_regions_on_shared_raster")
        if unknown_fraction > cfg["unknown_area_max"]:
            result["limitations"].append("Too much unresolved content to decide D5; unknown pixels are not normal or known empty.")
            return result
        complete = (unknown_fraction == 0 and page.get("content_type") == "native_editable"
                    and native == "available" and ctx.get("availability", {}).get("COM") in {"cached_verified", "fresh_measured"}
                    and not missing)
        result["coverage"] = "full" if complete else "partial"
        role = page.get("role", "unknown")
        if role in cfg["protected_roles"]:
            result.update(status="no_candidate_in_available_scope", score=0.)
            result["features"]["role_gate"] = "protected_minimal_design_role"
            return result
        if role not in cfg["eligible_roles"]:
            result["limitations"].append("No supported information-page role; page intent remains unknown.")
            return result
        if not body_boxes or not layers["body"].any():
            result["limitations"].append("No measured body/data layer; an empty or title-only page is not assumed defective or normal.")
            return result

        top = max(cfg["canvas_margin_top"], max((b["y"] + b["h"] for b in headers), default=0.) + cfg["header_gap"])
        height, width = content.shape
        x0, x1 = math.floor(cfg["canvas_margin_x"] * width), math.ceil((1 - cfg["canvas_margin_x"]) * width)
        y0, y1 = math.floor(top * height), math.ceil(cfg["canvas_bottom"] * height)
        if y1 <= y0 or x1 <= x0:
            result["limitations"].append("No supported measured body canvas remains below the header.")
            return result
        roi = dict(x=x0 / width, y=y0 / height, w=(x1 - x0) / width, h=(y1 - y0) / height)
        body = layers["body"][y0:y1, x0:x1]
        # Titles are blockers if they appear inside the body canvas, while
        # auxiliary footers do not establish body span or move its centroid.
        blocked_unknown = uncertain[y0:y1, x0:x1] | layers["title"][y0:y1, x0:x1]
        metrics = _measure(body, blocked_unknown)
        if metrics["bounds"] is None:
            result["limitations"].append("No measured body region intersects the supported body canvas.")
            return result
        bounds, centroid = metrics["bounds"], metrics["centroid"]
        empty = metrics["largest_known_empty_rectangle"]
        empty["bbox_page"] = _to_page(empty["bbox"], roi)
        bounds_page = _to_page(bounds, roi)
        bands = _bands([b for b in body_boxes if b["y"] + b["h"] > roi["y"] and b["y"] < roi["y"] + roi["h"]], cfg["row_band_gap"])
        # A lateral empty corridor must extend over the complete body canvas;
        # isolated white corners do not satisfy this second hypothesis.
        unavailable = body | blocked_unknown
        used_columns = np.flatnonzero(unavailable.any(axis=0))
        left_void = int(used_columns.min()) / body.shape[1] if used_columns.size else 1.
        right_void = (body.shape[1] - int(used_columns.max()) - 1) / body.shape[1] if used_columns.size else 1.
        side_void = max(left_void, right_void)
        side_centroid = centroid[0] if right_void >= left_void else 1 - centroid[0]
        anchor = bounds["x"] if right_void >= left_void else 1 - bounds["x"] - bounds["w"]
        bounds_area = bounds["w"] * bounds["h"]
        result["features"].update(body_canvas=roi, body_metrics=metrics, body_bounds_page=bounds_page,
                                  body_band_count=len(bands), body_band_intervals_page=bands,
                                  lateral_known_empty_corridor_fraction=side_void,
                                  lateral_known_empty_corridor_side="right" if right_void >= left_void else "left")
        triggers = []
        if (metrics["occupancy_upper"] <= cfg["body_occupancy_max"]
                and bounds_area <= cfg["cluster_bounds_area_max"]
                and metrics["centroid_offset_half_diagonal"] >= cfg["cluster_offset_min"]
                and empty["area_ratio"] >= cfg["cluster_empty_min"]):
            triggers.append("compact_off_center_body_with_large_known_empty_region")
        if (metrics["occupancy_upper"] <= cfg["row_occupancy_max"]
                and cfg["row_span_x_min"] <= bounds["w"] <= cfg["row_span_x_max"]
                and side_void >= cfg["row_side_empty_min"]
                and anchor <= cfg["row_anchor_margin_max"]
                and side_centroid <= cfg["row_centroid_side_max"]
                and len(bands) >= cfg["row_bands_min"]):
            triggers.append("sparse_body_rows_with_continuous_lateral_void")
        severity = (.35 * max(0., 1 - metrics["occupancy_upper"] / cfg["body_occupancy_max"])
                    + .35 * metrics["centroid_offset_half_diagonal"] + .30 * empty["area_ratio"])
        result.update(status="candidate" if triggers else "no_candidate_in_available_scope", score=float(min(1., max(0., severity))))
        result["features"]["triggered_rules"] = triggers
        if triggers:
            result["evidence"].append(dict(kind="layered_body_continuous_whitespace", object_ids=ids["body"],
                                           bbox=empty["bbox_page"], content_bbox=bounds_page,
                                           measurement_source=spatial.get("mask_source", "unknown"),
                                           triggered_rules=triggers, measurements=dict(body_occupancy_upper=metrics["occupancy_upper"],
                                           body_centroid_offset_half_diagonal=metrics["centroid_offset_half_diagonal"],
                                           body_bounds_area_ratio=bounds_area, largest_empty_ratio=empty["area_ratio"],
                                           lateral_void_fraction=side_void, unknown_area_fraction=unknown_fraction)))
        return result


def detect(ctx, config=None):
    return WhitespaceDetector(config).detect(ctx)


detect_whitespace = detect
