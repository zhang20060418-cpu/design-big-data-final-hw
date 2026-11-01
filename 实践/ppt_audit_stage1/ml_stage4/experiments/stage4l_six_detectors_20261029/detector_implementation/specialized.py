"""D1--D4 engineering detectors over a single, measured slide context.

No label, modification name, model score, G-2 artifact or PPTX parser enters this
module. A candidate is an auditable hypothesis, never an automatic human label.
Scores are ordered rule strength, not calibrated defect probabilities.
"""
from __future__ import annotations

from collections import Counter
import math
from statistics import median

VERSION = "specialized-engineering-v1"
LABELS = {
    "D1": "improper_font_sizing", "D2": "occluded_content",
    "D3": "cluttered_layout", "D4": "content_overflow_cutoff",
}
DEFAULTS = {
    "D1": {"minimum_characters": 8,
           "role_min_pt_at_540": {"title": 20, "body": 12, "chart_label": 10, "table_text": 10, "footnote": 7, "navigation": 8, "page_number": 7},
           "role_hard_min_pt_at_540": {"title": 12, "body": 9, "chart_label": 6, "table_text": 6, "footnote": 5, "navigation": 5, "page_number": 5},
           "relative_role_ratio": .65, "low_rendered_line_height_pt_at_540": 11,
           "title_to_body_ratio": .85},
    "D2": {"local_coverage_threshold": .45, "minimum_page_area_ratio": None,
           "text_intersection_requires_pixel_confirmation": True},
    "D3": {"scales": [1., .5, .25], "minimum_local_text_objects": 8,
           "maximum_median_characters": 32, "minimum_text_count_per_page_area": 80,
           "near_gap_normalized": .012, "minimum_close_neighbor_fraction": .6,
           "minimum_local_occupancy": .22, "minimum_overlap_pair_fraction": .15},
    "D4": {"page_overflow_tolerance_pt": 2.88, "containment_tolerance_pt": 2.88,
           "minimum_information_object_page_area": .003,
           "pixel_clipping_required_for_confirmation": True},
}


def _box(value):
    if not isinstance(value, dict):
        return None
    try:
        b = {k: float(value[k]) for k in ("x", "y", "w", "h")}
        return b if all(math.isfinite(v) for v in b.values()) and b["w"] >= 0 and b["h"] >= 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def _area(b):
    return max(0., b["w"]) * max(0., b["h"]) if b else 0.


def _intersect(a, b):
    x, y = max(a["x"], b["x"]), max(a["y"], b["y"])
    w = min(a["x"] + a["w"], b["x"] + b["w"]) - x
    h = min(a["y"] + a["h"], b["y"] + b["h"]) - y
    return {"x": x, "y": y, "w": max(0., w), "h": max(0., h)}


def _union_box(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    x, y = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
    return {"x": x, "y": y,
            "w": max(b["x"] + b["w"] for b in boxes) - x,
            "h": max(b["y"] + b["h"] for b in boxes) - y}


def _gap(a, b):
    dx = max(0., a["x"] - b["x"] - b["w"], b["x"] - a["x"] - a["w"])
    dy = max(0., a["y"] - b["y"] - b["h"], b["y"] - a["y"] - a["h"])
    return math.hypot(dx, dy)


def _page(ctx):
    page = ctx.get("page", {})
    try:
        w, h = float(page["width_pt"]), float(page["height_pt"])
        if w <= 0 or h <= 0 or not math.isfinite(w + h):
            raise ValueError
        return w, h
    except (KeyError, TypeError, ValueError):
        return None, None


def _cfg(config, key):
    return {**DEFAULTS[key], **((config or {}).get(key) or {})}


def _evidence(ids, kind, source, bbox=None, **metrics):
    return {"object_ids": list(ids), "kind": kind, "measurement_source": source,
            "bbox": bbox, **metrics}


def _result(key, evidence, unknown, features, *, applicable=True, limitations=()):
    status = "candidate" if evidence else "unknown" if unknown else "no_candidate_in_available_scope" if applicable else "unsupported"
    score = max((float(e.get("ordinal_strength", .5)) for e in evidence), default=None if unknown or not applicable else 0.)
    return {"detector": key, "label": LABELS[key], "version": VERSION,
            "score": score, "score_kind": "ordinal_heuristic_not_probability",
            "status": status, "coverage": "partial" if applicable else "unsupported",
            "features": features, "evidence": evidence,
            "unresolved": unknown, "limitations": list(dict.fromkeys(limitations))}


def _paragraphs(obj):
    return [p for p in obj.get("paragraphs", []) if _area(_box(p.get("bbox"))) > 0 and p.get("text_chars", 0) > 0]


def _font_values(obj):
    values = []
    for p in obj.get("paragraphs", []):
        try:
            value = float(p.get("font_pt"))
            if math.isfinite(value) and value > 0:
                values.append(value)
        except (TypeError, ValueError):
            pass
    if not values:
        for v in obj.get("font_sizes_pt", []):
            try:
                value = float(v)
                if math.isfinite(value) and value > 0:
                    values.append(value)
            except (TypeError, ValueError):
                pass
    return values


def detect_font(ctx, config=None):
    c = _cfg(config, "D1"); _, height = _page(ctx)
    if height is None:
        return _result("D1", [], [{"kind": "page_dimensions_unavailable"}], {}, applicable=False)
    evidence, unknown, measurements = [], [], []
    refs = ctx.get("document", {}).get("role_font_reference_pt", {})
    for o in ctx.get("objects", []):
        if not o.get("text_chars", 0):
            continue
        role = o.get("text_role", "unknown")
        if role not in c["role_min_pt_at_540"]:
            unknown.append({"object_id": o.get("id"), "kind": "text_role_unresolved"}); continue
        source = str(o.get("font_source", "unknown"))
        values = _font_values(o)
        if not values or any(word in source.lower() for word in ("fabricated", "fallback_default")):
            unknown.append({"object_id": o.get("id"), "kind": "effective_font_unresolved", "source": source}); continue
        # Font/page ratios make this invariant to a uniform physical page scale.
        font = min(values); font540 = font / height * 540.
        reference = refs.get(role)
        try:
            reference = float(reference)
            if reference <= 0 or not math.isfinite(reference): reference = None
        except (TypeError, ValueError): reference = None
        ratio = font / reference if reference else None
        line_heights = []
        for p in _paragraphs(o):
            lines = p.get("line_boxes")
            boxes = lines if lines else [p["bbox"]] if len(o.get("paragraphs", [])) == 1 else []
            # A whole multiline paragraph is an upper envelope, never a glyph size.
            for b in boxes:
                b = _box(b)
                if b and b["h"] > 0: line_heights.append(b["h"] * 540.)
        low_rendered = bool(line_heights and min(line_heights) < c["low_rendered_line_height_pt_at_540"])
        major = role in ("title", "body", "chart_label", "table_text")
        enough_chars = o.get("text_chars", 0) >= c["minimum_characters"] or role == "title"
        hard = font540 < c["role_hard_min_pt_at_540"][role]
        relative = ratio is not None and ratio < c["relative_role_ratio"]
        small = font540 < c["role_min_pt_at_540"][role]
        triggers = []
        if enough_chars and hard: triggers.append("role_specific_hard_readability_floor")
        if enough_chars and small and major and (relative or low_rendered): triggers.append("small_major_text_with_relative_or_layout_support")
        if enough_chars and relative and role not in ("page_number", "navigation"):
            triggers.append("same_document_same_role_font_outlier")
        body_ref = refs.get("body")
        if role == "title" and isinstance(body_ref, (float, int)) and body_ref > 0 and median(values) < body_ref * c["title_to_body_ratio"]:
            triggers.append("title_below_body_typographic_hierarchy")
        metrics = {"object_id": o.get("id"), "role": role, "min_effective_font_pt": font,
                   "min_font_pt_at_540_height": font540, "role_reference_pt": reference,
                   "same_role_ratio": ratio, "font_source": source,
                   "minimum_line_or_paragraph_envelope_height_pt_at_540": min(line_heights) if line_heights else None,
                   "line_height_semantics": "line_envelope_if_available_else_single_paragraph_upper_envelope_not_glyph_height",
                   "triggers": triggers}
        measurements.append(metrics)
        if triggers:
            evidence.append(_evidence([o.get("id")], "role_adaptive_font_candidate", source,
                                      _union_box([_box(p["bbox"]) for p in _paragraphs(o)]) or _box(o.get("bbox")),
                                      metrics=metrics, ordinal_strength=.8 if hard else .65))
    native = ctx.get("availability", {}).get("native")
    return _result("D1", evidence, unknown, {"objects": measurements, "role_reference_pt": refs},
                   applicable=bool(measurements or unknown or native not in (None, "unavailable", "unsupported")),
                   limitations=["Text-role and viewing-context inference are engineering heuristics, not author intent.",
                                "Paragraph/line envelopes are not measured glyph height; unknown inherited fonts are not assigned 18pt.",
                                "No human-calibrated D1 classifier or population probability is available."])


def detect_occlusion(ctx, config=None):
    c = _cfg(config, "D2"); evidence, unknown, pairs = [], [], []
    objects = ctx.get("objects", []); text_objects = [o for o in objects if o.get("text_chars", 0)]
    for text in text_objects:
        paragraphs = _paragraphs(text)
        if not paragraphs:
            unknown.append({"object_id": text.get("id"), "kind": "actual_text_layout_unavailable"}); continue
        for upper in objects:
            if upper.get("id") == text.get("id") or upper.get("z_order", -1) <= text.get("z_order", -1): continue
            # Cells sharing a parent are not independent layer covers.
            if text.get("cell") and upper.get("cell") and text.get("parent_id") == upper.get("parent_id"): continue
            ub = _box(upper.get("bbox"))
            if _area(ub) <= 0: continue
            state = (upper.get("opacity") or {}).get("state", "unknown")
            rotated = abs(float(upper.get("rotation", 0) or 0)) % 360 > 1e-6
            for p in paragraphs:
                pb = _box(p["bbox"]); overlap = _intersect(pb, ub); ratio = _area(overlap) / _area(pb)
                if ratio <= 0: continue
                item = {"target_object_id": text.get("id"), "upper_object_id": upper.get("id"),
                        "paragraph_id": p.get("id"), "local_coverage": ratio,
                        "cover_page_area_ratio": _area(ub), "opacity_state": state,
                        "bbox": overlap, "rotation_unresolved": rotated}
                pairs.append(item)
                if ratio >= c["local_coverage_threshold"]:
                    if state == "fully_opaque" and not rotated:
                        evidence.append(_evidence([text.get("id"), upper.get("id")], "opaque_cover_over_actual_paragraph",
                                                  "COM_nonempty_paragraph_layout+validated_native_opacity+z_order", overlap,
                                                  metrics=item, glyph_coverage_confirmed=False,
                                                  ordinal_strength=.8 if ratio >= .7 else .65))
                    elif state != "transparent":
                        unknown.append({**item, "kind": "local_cover_opacity_or_geometry_unresolved"})
        # Text foregrounds can occlude even when their box fill is transparent.
        for other in text_objects:
            if str(other.get("id")) <= str(text.get("id")): continue
            if text.get("cell") and other.get("cell") and text.get("parent_id") == other.get("parent_id"): continue
            overlaps = [_intersect(_box(p["bbox"]), _box(q["bbox"]))
                        for p in paragraphs for q in _paragraphs(other)]
            overlaps = [b for b in overlaps if _area(b) > 0]
            if overlaps:
                unknown.append({"object_ids": [text.get("id"), other.get("id")],
                                "kind": "text_foreground_layout_intersection_requires_glyph_confirmation",
                                "bbox": _union_box(overlaps), "glyph_overlap_confirmed": False})
        for p in paragraphs:
            lines = [_box(b) for b in (p.get("line_boxes") or [])]; lines = [b for b in lines if b]
            for i, a in enumerate(lines):
                for b in lines[i + 1:]:
                    overlap = _intersect(a, b)
                    if _area(overlap) > 0:
                        unknown.append({"object_id": text.get("id"), "paragraph_id": p.get("id"),
                                        "kind": "within_textbox_line_envelope_intersection_requires_glyph_confirmation",
                                        "bbox": overlap, "glyph_overlap_confirmed": False})
    return _result("D2", evidence, unknown, {"local_cover_pairs": pairs,
                   "local_coverage_threshold": c["local_coverage_threshold"], "page_area_hard_gate": None},
                   applicable=bool(text_objects), limitations=[
                       "The historical 45% local envelope coverage threshold is retained without a 3% page-area veto.",
                       "Opaque paragraph-envelope coverage is a candidate, not proof of invisible glyphs or unreadability.",
                       "Text-text, line-line and partial-opacity cases remain unresolved without foreground/pixel evidence."])


def _windows(scale):
    if scale == 1.: return [{"x": 0., "y": 0., "w": 1., "h": 1.}]
    step = scale / 2.; count = int(round((1. - scale) / step))
    return [{"x": i * step, "y": j * step, "w": scale, "h": scale}
            for i in range(count + 1) for j in range(count + 1)]


def detect_clutter(ctx, config=None):
    c = _cfg(config, "D3"); evidence, unknown = [], []
    blocks = []
    for o in ctx.get("objects", []):
        if o.get("kind") not in ("text",) or o.get("text_role") in ("page_number", "navigation", "footnote") or o.get("content_class") in ("auxiliary", "background", "decoration"):
            continue
        if not o.get("text_chars", 0): continue
        ps = _paragraphs(o); box = _union_box([_box(p["bbox"]) for p in ps])
        if box is None:
            unknown.append({"object_id": o.get("id"), "kind": "text_layout_unavailable_for_local_density"}); continue
        blocks.append({"id": o.get("id"), "bbox": box, "chars": o.get("text_chars", 0),
                       "parent_id": o.get("parent_id")})
    shared_nearest = {}
    for n in ctx.get("relations", {}).get("nearest", []):
        if n.get("a") is not None and n.get("b") is not None:
            try: shared_nearest[(n["a"], n["b"])] = float(n["gap"])
            except (KeyError, TypeError, ValueError): pass
    regions = []
    mask = ctx.get("spatial", {}).get("content_mask")
    shared_windows = [(m.get("scale"), _box(m.get("bbox")))
                      for m in ctx.get("spatial", {}).get("multiscale", [])]
    shared_windows = [(scale, box) for scale, box in shared_windows if box and _area(box)]
    windows = shared_windows or [(scale, box) for scale in c["scales"] for box in _windows(float(scale))]
    for scale, window in windows:
            selected = [b for b in blocks if _area(_intersect(b["bbox"], window)) / max(_area(b["bbox"]), 1e-12) >= .5]
            if len(selected) < c["minimum_local_text_objects"]: continue
            n = len(selected); gaps = []; overlap_pairs = 0
            for i, a in enumerate(selected):
                values = []
                for j, b in enumerate(selected):
                    if i == j: continue
                    # Shared nearest edges are reused; other pairs are cheap rectangle relations.
                    values.append(shared_nearest.get((a["id"], b["id"]), _gap(a["bbox"], b["bbox"])))
                    if j > i and _area(_intersect(a["bbox"], b["bbox"])) > 0: overlap_pairs += 1
                gaps.append(min(values))
            close = sum(g <= c["near_gap_normalized"] for g in gaps) / n
            pair_fraction = overlap_pairs / max(1, n * (n - 1) / 2)
            if mask is not None:
                mh, mw = mask.shape
                x0, y0 = max(0, int(window["x"] * mw)), max(0, int(window["y"] * mh))
                x1, y1 = min(mw, int((window["x"] + window["w"]) * mw)), min(mh, int((window["y"] + window["h"]) * mh))
                occupancy = float(mask[y0:y1, x0:x1].mean()) if x1 > x0 and y1 > y0 else None
                occupancy_source = "shared_actual_layout_content_union_mask"
            else:
                # An explicit conservative upper bound, never labelled union occupancy.
                occupancy = min(1., sum(_area(_intersect(b["bbox"], window)) for b in selected) / _area(window))
                occupancy_source = "sum_envelope_area_upper_bound_without_union_mask"
            chars = median(b["chars"] for b in selected)
            density = n / _area(window)
            crowded = (chars <= c["maximum_median_characters"] and density >= c["minimum_text_count_per_page_area"]
                       and close >= c["minimum_close_neighbor_fraction"]
                       and (occupancy is not None and occupancy >= c["minimum_local_occupancy"]
                            or pair_fraction >= c["minimum_overlap_pair_fraction"]))
            metric = {"bbox": window, "scale": scale, "text_count": n,
                      "text_count_per_page_area": density, "median_characters_per_object": chars,
                      "close_neighbor_fraction": close, "nearest_gap_median": median(gaps),
                      "overlap_pair_fraction": pair_fraction, "local_occupancy": occupancy,
                      "occupancy_source": occupancy_source, "triggered": crowded,
                      "object_ids": [b["id"] for b in selected]}
            regions.append(metric)
            if crowded:
                evidence.append(_evidence(metric["object_ids"], "multiscale_short_text_spatial_congestion",
                                          "shared_actual_paragraph_layout+native_object_relations", window,
                                          metrics=metric, ordinal_strength=.75))
    # Descriptive geometry is retained even when it does not trigger a rule.
    # Alignment is a continuous error, so no extra threshold is silently tuned.
    align_x, align_y, within_group, between_group = [], [], [], []
    for i, a in enumerate(blocks):
        ab = a["bbox"]
        for b in blocks[i + 1:]:
            bb = b["bbox"]
            align_x.append(min(abs(ab["x"] - bb["x"]),
                               abs(ab["x"] + ab["w"] - bb["x"] - bb["w"]),
                               abs(ab["x"] + ab["w"] / 2 - bb["x"] - bb["w"] / 2)))
            align_y.append(min(abs(ab["y"] - bb["y"]),
                               abs(ab["y"] + ab["h"] - bb["y"] - bb["h"]),
                               abs(ab["y"] + ab["h"] / 2 - bb["y"] - bb["h"] / 2)))
            gap = _gap(ab, bb)
            if a.get("parent_id") is not None and a.get("parent_id") == b.get("parent_id"):
                within_group.append(gap)
            elif a.get("parent_id") is not None and b.get("parent_id") is not None:
                between_group.append(gap)
    chart_boxes = [_box(o.get("bbox")) for o in ctx.get("objects", []) if o.get("kind") in ("chart", "table")]
    chart_boxes = [b for b in chart_boxes if b]
    text_chart = [min(_gap(b["bbox"], cb) for cb in chart_boxes) for b in blocks] if chart_boxes else []
    relation_features = {
        "median_vertical_edge_or_center_alignment_error": median(align_x) if align_x else None,
        "median_horizontal_edge_or_center_alignment_error": median(align_y) if align_y else None,
        "same_native_group_pair_gap_median": median(within_group) if within_group else None,
        "different_native_group_pair_gap_median": median(between_group) if between_group else None,
        "text_to_chart_or_table_nearest_gap_median": median(text_chart) if text_chart else None,
        "maximum_local_text_count_per_page_area": max((r["text_count_per_page_area"] for r in regions), default=None),
        "group_semantics": "native_parent_group_only; no inferred rhetorical grouping or ungrouped-as-independent assumption",
    }
    # One maximal spatial witness per connected object set keeps overlays interpretable.
    keep = []
    for e in sorted(evidence, key=lambda v: (_area(v["bbox"]), -len(v["object_ids"]))):
        ids = set(e["object_ids"])
        if not any(ids <= set(k["object_ids"]) or set(k["object_ids"]) <= ids for k in keep): keep.append(e)
    return _result("D3", keep, unknown, {"text_block_count": len(blocks), "regions": regions,
                   "shared_multiscale_measurements": ctx.get("spatial", {}).get("multiscale", []),
                   "shared_nearest_gap_stats": ctx.get("spatial", {}).get("nearest_gap_stats", {}),
                   "table_cells_excluded_from_independent_text_count": True,
                   "object_relation_features": relation_features},
                   applicable=bool(blocks or unknown), limitations=[
                       "Fixed spatial engineering conjunction; no generator-reference labels were fitted.",
                       "A dense table is a single information module; cells are not counted as independent crowded textboxes.",
                       "Hierarchy, grouping intent and readability need human validation; well-aligned dense layouts may still trigger."])


def _overflow_pt(b, width, height, outer=None):
    if outer is None: outer = {"x": 0., "y": 0., "w": 1., "h": 1.}
    return max(0., (outer["x"] - b["x"]) * width, (outer["y"] - b["y"]) * height,
               (b["x"] + b["w"] - outer["x"] - outer["w"]) * width,
               (b["y"] + b["h"] - outer["y"] - outer["h"]) * height)


def detect_cutoff(ctx, config=None):
    c = _cfg(config, "D4"); width, height = _page(ctx)
    if width is None:
        return _result("D4", [], [{"kind": "page_dimensions_unavailable"}], {}, applicable=False)
    evidence, unknown, geometry, actual, containment = [], [], [], [], []
    for o in ctx.get("objects", []):
        frame = _box(o.get("bbox")); rotation = float(o.get("rotation", 0) or 0) % 360
        is_rotated = min(rotation, 360 - rotation) > 1e-6
        ps = _paragraphs(o)
        if o.get("text_chars", 0) and not ps:
            unknown.append({"object_id": o.get("id"), "kind": "actual_text_layout_unavailable"})
        for p in ps:
            pb = _box(p["bbox"]); amount = _overflow_pt(pb, width, height)
            metric = {"object_id": o.get("id"), "paragraph_id": p.get("id"), "bbox": pb,
                      "page_overflow_pt": amount, "rotation": rotation}
            actual.append(metric)
            if amount > c["page_overflow_tolerance_pt"] + 1e-9:
                if not is_rotated:
                    evidence.append(_evidence([o.get("id")], "actual_paragraph_layout_outside_page",
                                              "COM_nonempty_paragraph_layout", pb, metrics=metric,
                                              pixel_clipping_confirmed=False, ordinal_strength=.85))
                else:
                    unknown.append({**metric, "kind": "rotated_text_layout_coordinate_semantics_unverified"})
            if frame:
                amount_frame = _overflow_pt(pb, width, height, frame)
                contain = {"object_id": o.get("id"), "paragraph_id": p.get("id"),
                           "frame_overflow_pt": amount_frame, "bbox": pb,
                           "word_wrap": o.get("word_wrap"), "auto_size": o.get("auto_size"), "kind": o.get("kind")}
                containment.append(contain)
                if amount_frame > c["containment_tolerance_pt"] + 1e-9:
                    if o.get("kind") == "cell" or o.get("cell"):
                        evidence.append(_evidence([o.get("id")], "table_cell_content_containment_failure_candidate",
                                                  "COM_cell_frame+actual_nonempty_paragraph_layout", pb,
                                                  metrics=contain, pixel_clipping_confirmed=False, ordinal_strength=.7))
                    elif "TEXT_TO_FIT" in str(o.get("auto_size", "")).upper() or str(o.get("auto_size")) == "2":
                        evidence.append(_evidence([o.get("id")], "autofit_layout_containment_inconsistency_candidate",
                                                  "native_autofit_setting+COM_actual_layout", pb,
                                                  metrics=contain, pixel_clipping_confirmed=False, ordinal_strength=.6))
                    else:
                        unknown.append({**contain, "kind": "textframe_overflow_without_proven_clipping"})
        if frame:
            overflow = _overflow_pt(frame, width, height)
            geometry.append({"object_id": o.get("id"), "frame_overflow_pt": overflow, "rotation": rotation})
            # Empty text frames and intentional full-bleed art are not content clipping.
            if o.get("kind") in ("image", "chart", "table") and o.get("content_class") == "information" and _area(frame) >= c["minimum_information_object_page_area"] and overflow > c["page_overflow_tolerance_pt"] + 1e-9:
                if is_rotated:
                    unknown.append({"object_id": o.get("id"), "kind": "rotated_information_object_boundary_requires_transformed_visible_mask", "bbox": frame, "frame_overflow_pt": overflow})
                else:
                    evidence.append(_evidence([o.get("id")], "important_information_object_frame_outside_page",
                                              "native_object_geometry", frame,
                                              overflow_pt=overflow, visible_content_clipped=False,
                                              ordinal_strength=.6))
            elif is_rotated and o.get("content_class") == "information":
                unknown.append({"object_id": o.get("id"), "kind": "rotated_visible_envelope_not_proven", "bbox": frame})
        crop = o.get("image_crop")
        if o.get("kind") == "image" and isinstance(crop, dict):
            values = []
            for k, v in crop.items():
                if any(s in k.lower() for s in ("left", "right", "top", "bottom")):
                    try: values.append(float(v))
                    except (TypeError, ValueError): pass
            if any(abs(v) > 1e-6 for v in values):
                unknown.append({"object_id": o.get("id"), "kind": "image_crop_subject_and_intent_unknown", "crop": crop, "bbox": frame})
    return _result("D4", evidence, unknown, {"actual_layout": actual, "frame_geometry": geometry,
                   "containment": containment, "page_overflow_tolerance_pt": c["page_overflow_tolerance_pt"]},
                   applicable=bool(ctx.get("objects")), limitations=[
                       "Object frame, actual layout envelope and visible pixel clipping remain three distinct evidence levels.",
                       "Historical >0.04in COM paragraph-page overflow boundary is retained.",
                       "Cell/autofit containment witnesses do not prove unreadable pixels; image subject crop and rotated visible shape masks remain unresolved."])


def detect_specialized(context, config=None):
    """Run all four specialized candidates without reparsing or fitting data."""
    return {"D1": detect_font(context, config), "D2": detect_occlusion(context, config),
            "D3": detect_clutter(context, config), "D4": detect_cutoff(context, config)}
