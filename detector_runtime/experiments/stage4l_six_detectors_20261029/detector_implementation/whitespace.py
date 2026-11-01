"""D5: interpretable, role-gated whitespace candidates, not probabilities.

No image or PPTX is reparsed here.  The shared context owns semantic content
classification and measured paragraph masks.  Unknown mask pixels are kept as
unknown and also block the *known-empty* rectangle search.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np


DEFAULT_CONFIG = {
    "version": "whitespace-ordinal-v1",
    "low_occupancy_upper": 0.16,
    "tiny_occupancy_upper": 0.06,
    "largest_empty_ratio": 0.45,
    "off_center_distance": 0.38,
    "compact_bounds_area": 0.16,
    "grid_peak_share": 0.65,
    "unknown_area_max": 0.05,
    "extreme_half_imbalance": 0.85,
    "eligible_roles": ["body", "chart", "contents"],
    "protected_minimal_roles": ["cover", "section", "ending"],
    "multiscale_grids": [2, 3, 4],
}


def largest_empty_rectangle(occupied: np.ndarray) -> dict:
    """Exact maximum empty rectangle on a raster using O(H*W) time/O(W) memory.

    ``True`` cells are unavailable (information OR unknown).  This computes
    semantic empty space, not necessarily white RGB pixels. Equal maxima use
    deterministic top/left/bottom/right lexicographic order.
    """
    cells = np.asarray(occupied)
    if cells.ndim != 2 or cells.dtype != np.bool_ or not cells.size:
        raise ValueError("occupied must be a nonempty 2D boolean mask")
    height, width = cells.shape
    histogram = np.zeros(width, dtype=np.int64)
    best_area = 0
    best_box = None
    for row in range(height):
        histogram = np.where(cells[row], 0, histogram + 1)
        stack = []
        for col in range(width + 1):
            bar_height = int(histogram[col]) if col < width else 0
            left = col
            while stack and stack[-1][1] > bar_height:
                start, old_height = stack.pop()
                area = old_height * (col - start)
                box = (row - old_height + 1, start, row + 1, col)
                if area > best_area or (
                    area == best_area and area and (best_box is None or box < best_box)
                ):
                    best_area, best_box = area, box
                left = start
            if bar_height and (not stack or stack[-1][1] < bar_height):
                stack.append((left, bar_height))
    if best_box is None:
        return {"area_cells": 0, "area_ratio": 0.0, "bbox": None}
    top, left, bottom, right = best_box
    return {
        "area_cells": int(best_area),
        "area_ratio": float(best_area / cells.size),
        "bbox": {
            "x": left / width, "y": top / height,
            "w": (right - left) / width, "h": (bottom - top) / height,
        },
    }


def _valid_mask(value):
    return (
        isinstance(value, np.ndarray) and value.ndim == 2
        and value.size > 0 and value.dtype == np.bool_
    )


def _clip(value):
    return float(min(1.0, max(0.0, value)))


def _multiscale(mask, grids):
    height, width = mask.shape
    count = int(mask.sum())
    result = []
    for grid in grids:
        if grid < 1 or grid > min(height, width):
            continue
        y_edges = np.linspace(0, height, grid + 1, dtype=int)
        x_edges = np.linspace(0, width, grid + 1, dtype=int)
        masses, densities = [], []
        for row in range(grid):
            for col in range(grid):
                block = mask[y_edges[row]:y_edges[row + 1], x_edges[col]:x_edges[col + 1]]
                mass = int(block.sum())
                masses.append(mass)
                densities.append(float(mass / block.size))
        shares = [mass / count for mass in masses] if count else [0.0] * len(masses)
        entropy = -sum(v * math.log(v) for v in shares if v > 0)
        entropy /= math.log(grid * grid) if grid > 1 else 1
        result.append({
            "grid": grid, "peak_content_share": max(shares),
            "normalized_entropy": float(entropy),
            "cell_content_shares_row_major": shares,
            "cell_occupancy_row_major": densities,
        })
    return result


def spatial_metrics(content, unknown, grids):
    """Raster measurements; occupancy interval preserves unknown semantics."""
    height, width = content.shape
    # A measured information pixel remains information even when a conservative
    # unknown envelope happens to overlap it. Count uncertainty on the remainder.
    unknown_only = unknown & ~content
    count = int(content.sum())
    unknown_count = int(unknown_only.sum())
    occupancy = count / content.size
    y, x = np.nonzero(content)
    centroid = None
    bounds = None
    offset = None
    bounds_area = 0.0
    horizontal_imbalance = vertical_imbalance = None
    if count:
        centroid = [float((x.mean() + 0.5) / width), float((y.mean() + 0.5) / height)]
        offset = math.hypot(centroid[0] - 0.5, centroid[1] - 0.5) / math.sqrt(0.5)
        bounds = {
            "x": float(x.min() / width), "y": float(y.min() / height),
            "w": float((x.max() - x.min() + 1) / width),
            "h": float((y.max() - y.min() + 1) / height),
        }
        bounds_area = bounds["w"] * bounds["h"]
        # Pixel-centre assignment works reproducibly on odd as well as even grids.
        left = int(np.count_nonzero((x + 0.5) / width < 0.5))
        top = int(np.count_nonzero((y + 0.5) / height < 0.5))
        horizontal_imbalance = abs(2 * left - count) / count
        vertical_imbalance = abs(2 * top - count) / count
    multiscale = _multiscale(content, grids)
    nine = next((item for item in multiscale if item["grid"] == 3), None)
    return {
        "mask_shape_hw": [height, width],
        "content_pixels": count,
        "unknown_pixels": unknown_count,
        "content_occupancy_lower": float(occupancy),
        "content_occupancy_upper": float((count + unknown_count) / content.size),
        "known_semantic_empty_fraction": float(1.0 - (count + unknown_count) / content.size),
        "unknown_area_fraction": float(unknown_count / content.size),
        "content_centroid": centroid,
        "centroid_offset_half_diagonal": float(offset) if offset is not None else None,
        "content_bounds": bounds,
        "content_bounds_area_ratio": float(bounds_area),
        "horizontal_half_imbalance": horizontal_imbalance,
        "vertical_half_imbalance": vertical_imbalance,
        "largest_known_empty_rectangle": largest_empty_rectangle(content | unknown),
        "nine_grid_peak_content_share": nine["peak_content_share"] if nine else None,
        "multiscale": multiscale,
    }


class WhitespaceDetector:
    """Detect explainable engineering candidates with fixed compound rules."""

    def __init__(self, config=None):
        provided = config or {}
        if isinstance(provided, Mapping) and "D5" in provided:
            provided = provided["D5"] or {}
        self.config = {**DEFAULT_CONFIG, **provided}
        for key in (
            "low_occupancy_upper", "tiny_occupancy_upper", "largest_empty_ratio",
            "off_center_distance", "compact_bounds_area", "grid_peak_share",
            "unknown_area_max", "extreme_half_imbalance",
        ):
            value = self.config[key]
            if not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ValueError(f"invalid D5 fraction {key}")

    def detect(self, ctx):
        page = ctx.get("page", {})
        spatial = ctx.get("spatial", {})
        role = page.get("role", "unknown")
        result = {
            "detector": "D5", "label": "excessive_whitespace",
            "version": self.config["version"], "score": None,
            "score_kind": "ordinal_heuristic_not_probability",
            "status": "unknown", "coverage": "partial",
            "features": {
                "page_role": role, "page_role_source": page.get("role_source", "unknown"),
                "page_role_confidence": page.get("role_confidence"),
                "content_type": page.get("content_type", "unknown"),
                "mask_source": spatial.get("mask_source", "unknown"),
                "triggered_rules": [], "thresholds": dict(self.config),
            },
            "evidence": [],
            "limitations": [
                "Semantic content uses paragraph layout envelopes and object classification, not exact glyph ink.",
                "Page role is an engineering inference; whitespace intent requires human judgment.",
                "Ordinal engineering score is not a calibrated probability or a six-label human truth.",
            ],
        }
        content, unknown = spatial.get("content_mask"), spatial.get("unknown_mask")
        proxy = spatial.get("png_foreground_mask")
        if _valid_mask(proxy):
            result["features"]["png_foreground_proxy_occupancy"] = float(proxy.mean())
            result["features"]["png_proxy_usage"] = "diagnostic_only_not_semantic_content"
        if not _valid_mask(content) or not _valid_mask(unknown) or content.shape != unknown.shape:
            result["limitations"].append("Missing, malformed, or misaligned boolean content/unknown mask.")
            return result
        metrics = spatial_metrics(content, unknown, self.config["multiscale_grids"])
        result["features"].update(metrics)
        content_type = page.get("content_type", "unknown")
        native_state = ctx.get("availability", {}).get("native", "unknown")
        if content_type == "image_only" or role == "image" or native_state in {
            "unavailable", "unsupported", "missing", "failed", "not_available"
        }:
            result.update(status="unsupported", coverage="unsupported")
            result["limitations"].append(
                "Native masks cannot establish informative regions inside raster-only/image showcase content. "
                "The separate PNG foreground proxy is not substituted for semantic content."
            )
            return result
        if metrics["unknown_area_fraction"] > self.config["unknown_area_max"]:
            result["limitations"].append("Too much unclassified region to judge semantic whitespace safely.")
            return result
        com_state = ctx.get("availability", {}).get("COM", "unknown")
        complete = (
            content_type == "native_editable" and metrics["unknown_area_fraction"] == 0
            and native_state == "available" and com_state in {"cached_verified", "fresh_measured"}
        )
        result["coverage"] = "full" if complete else "partial"
        if not complete:
            result["limitations"].append("Mixed/unclassified elements or unverified COM measurements keep this result partial.")

        cfg = self.config
        occupancy = metrics["content_occupancy_upper"]
        low = occupancy < cfg["low_occupancy_upper"]
        tiny = occupancy < cfg["tiny_occupancy_upper"]
        empty = metrics["largest_known_empty_rectangle"]["area_ratio"] >= cfg["largest_empty_ratio"]
        off = metrics["centroid_offset_half_diagonal"]
        compact = metrics["content_bounds_area_ratio"] < cfg["compact_bounds_area"]
        peak = metrics["nine_grid_peak_content_share"]
        concentrated = peak is not None and peak >= cfg["grid_peak_share"]
        imbalance = max(
            metrics["horizontal_half_imbalance"] or 0.0,
            metrics["vertical_half_imbalance"] or 0.0,
        )
        low_severity = _clip((cfg["low_occupancy_upper"] - occupancy) / cfg["low_occupancy_upper"])
        compact_severity = _clip((cfg["compact_bounds_area"] - metrics["content_bounds_area_ratio"]) / cfg["compact_bounds_area"])
        raw_score = _clip(
            0.4 * low_severity + 0.3 * (off or 0.0)
            + 0.15 * metrics["largest_known_empty_rectangle"]["area_ratio"]
            + 0.15 * compact_severity
        )
        result["features"]["ungated_composition_score"] = raw_score
        if role in cfg["protected_minimal_roles"]:
            result.update(score=0.0, status="no_candidate_in_available_scope")
            result["features"]["role_gate"] = "protected_minimal_design_role"
            result["limitations"].append("Cover/section/ending designs are protected from this body-layout heuristic.")
            return result
        if role not in cfg["eligible_roles"]:
            result["score"] = raw_score
            result["features"]["role_gate"] = "unknown_or_ineligible_role"
            result["limitations"].append("No supported body/chart/contents role; intent-sensitive D5 conclusion remains unknown.")
            return result
        result["features"]["role_gate"] = "eligible_information_role"
        triggers = []
        if metrics["content_pixels"] == 0:
            triggers.append("no_information_content_in_measured_scope")
        if low and off is not None and off >= cfg["off_center_distance"] and concentrated and empty:
            triggers.append("low_occupancy_off_center_concentration")
        # A centred tiny chart splits one large empty region into four strips;
        # requiring a single >=45% rectangle therefore misses an obvious scale
        # case. This independent scale rule uses existing .06 occupancy/.16
        # bounds, i.e. >94% known semantic empty space, not a new fitted cutoff.
        if role == "chart" and tiny and compact:
            triggers.append("chart_abnormally_small_relative_to_canvas")
        if low and compact and empty and concentrated and imbalance >= cfg["extreme_half_imbalance"] and off is not None and off >= cfg["off_center_distance"]:
            triggers.append("compact_information_with_extreme_spatial_imbalance")
        result["features"]["triggered_rules"] = triggers
        result["score"] = 1.0 if not metrics["content_pixels"] else raw_score
        result["status"] = "candidate" if triggers else "no_candidate_in_available_scope"
        if triggers:
            information_ids = [
                obj.get("id") for obj in ctx.get("objects", [])
                if obj.get("id") and obj.get("content_class") == "information"
            ]
            result["evidence"].append({
                "kind": "semantic_empty_region_composition",
                "object_ids": information_ids,
                "bbox": metrics["largest_known_empty_rectangle"]["bbox"],
                "content_bbox": metrics["content_bounds"],
                "content_centroid": metrics["content_centroid"],
                "measurement_source": spatial.get("mask_source", "unknown"),
                "triggered_rules": triggers,
                "measurements": {
                    key: metrics[key] for key in (
                        "content_occupancy_lower", "content_occupancy_upper",
                        "centroid_offset_half_diagonal", "content_bounds_area_ratio",
                        "nine_grid_peak_content_share", "unknown_area_fraction",
                        "known_semantic_empty_fraction",
                    )
                },
            })
        return result
