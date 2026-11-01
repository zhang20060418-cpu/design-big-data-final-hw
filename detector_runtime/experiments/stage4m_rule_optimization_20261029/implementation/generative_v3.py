"""One bounded D6 mechanism revision: local icon units and background peers.

V2 remains immutable. V2 thresholds and A01/A05 boundaries are retained. A03
adds an explicitly limited unattached translucent-shape pile hypothesis, without
rewriting semantic classes or masks. No annotations, notes or names are read.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import importlib.util
import math
from pathlib import Path


_PATH = Path(__file__).with_name("generative_v2.py")
_SPEC = importlib.util.spec_from_file_location("_fixed_d6_v2_for_v3", _PATH)
v2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(v2)
_box, _bounds, _sub, _ev = v2._box, v2._bounds, v2._sub, v2._ev


def _area(b):
    return b["w"]*b["h"] if b else 0.


def _intersection(a, b):
    return (max(0., min(a["x"]+a["w"], b["x"]+b["w"])-max(a["x"], b["x"])) *
            max(0., min(a["y"]+a["h"], b["y"]+b["h"])-max(a["y"], b["y"])))


def _contains(outer, inner):
    return _intersection(outer, inner)/max(_area(inner), 1e-12) >= .95


def _luminance(rgb):
    channels = [int(rgb[k:k+2], 16)/255 for k in (0, 2, 4)]
    linear = [c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in channels]
    return sum(c*w for c, w in zip(linear, (.2126, .7152, .0722)))


def _background(ctx):
    """Only a nearly full-page, opaque, measured native background is usable."""
    unknown = {"status": "unknown", "brightness_class": None, "relative_luminance": None,
               "object_id": None, "fill_rgb": None,
               "source": "large_native_background_solid_fill_only_not_font_foreground"}
    if not v2._native_available(ctx):
        return unknown
    candidates = []
    for o in ctx.get("objects", []):
        b = _box(o)
        if (b and o.get("content_class") == "background" and o.get("kind") == "shape"
                and _intersection(b, {"x": 0., "y": 0., "w": 1., "h": 1.}) >= .85):
            candidates.append(o)
    if not candidates:
        return unknown
    # A topmost unresolved layer prevents claiming that a known lower fill is
    # the effective background. Equal-z conflicting fills are also unresolved.
    highest = max(o.get("z_order", 0) for o in candidates)
    tops = [o for o in candidates if o.get("z_order", 0) == highest]
    measurements = []
    for o in tops:
        n = o.get("native_shape", {})
        rgb, alpha = v2._color(n.get("fill_rgb")), n.get("fill_alpha")
        if (n.get("has_visible_fill") is not True or not rgb or not isinstance(alpha, (int, float))
                or isinstance(alpha, bool) or not math.isfinite(alpha) or alpha < .95):
            return {**unknown, "object_id": o.get("id"), "reason": "top_background_fill_not_measured_opaque_RGB"}
        measurements.append((o, rgb, _luminance(rgb)))
    if len({rgb for _, rgb, _ in measurements}) != 1:
        return {**unknown, "reason": "conflicting_top_background_fills"}
    o, rgb, luminance = measurements[0]
    brightness = "dark" if luminance <= .25 else "light" if luminance >= .5 else None
    return {**unknown, "status": "measured" if brightness else "unknown",
            "brightness_class": brightness, "relative_luminance": luminance,
            "object_id": o.get("id"), "fill_rgb": rgb,
            "reason": "opaque_native_background" if brightness else "intermediate_luminance_not_assigned_to_light_or_dark"}


def _caption_cost(image, text):
    a, b = _box(image), _box(text)
    chars = len(v2._normalize_text(text.get("text", "")))
    if not a or not b or not 2 <= chars <= 80 or b["w"] > .35 or b["h"] > .16:
        return None
    gap = v2._gap(a, b)
    if gap > .055:
        return None
    ax, ay = a["x"]+a["w"]/2, a["y"]+a["h"]/2
    bx, by = b["x"]+b["w"]/2, b["y"]+b["h"]/2
    lateral = abs(ay-by) <= max(.045, b["h"]/2) and b["x"] >= a["x"]+a["w"]-.01
    below = abs(ax-bx) <= max(a["w"], b["w"])/2 and b["y"] >= a["y"]+a["h"]-.01
    if not lateral and not below:
        return None
    return gap+.2*math.hypot(ax-bx, ay-by)


def _fine_icon_context(ctx, icon_ids):
    """Recover smallest actual cards, then mutually attached local captions.

    Fine icon units are inserted before any residual broad grid. This matters
    because a single broad grid can overlap all icons at zero geometric gap.
    """
    by_id = {o.get("id"): o for o in ctx.get("objects", [])}
    images = [by_id[i] for i in icon_ids if i in by_id]
    texts = v2._body_objects(ctx)
    containers = [o for o in ctx.get("objects", []) if _box(o) and o.get("kind") == "shape"
                  and (o.get("semantic_role") == "container" or o.get("decoration_role") == "container")
                  and (o.get("native_shape", {}).get("has_visible_fill") is True
                       or o.get("native_shape", {}).get("has_visible_line") is True)]
    records, claimed_text, claimed_icons = [], set(), set()
    # Sort on geometry only. Neither ID syntax nor object enumeration is a feature.
    images = sorted(images, key=lambda o: (_box(o)["y"], _box(o)["x"], _box(o)["w"], _box(o)["h"]))
    for image in images:
        choices = []
        for card in containers:
            cb = _box(card)
            support = [t for t in texts if _contains(cb, _box(t))]
            enclosed_icons = [i for i in images if _contains(cb, _box(i))]
            if _contains(cb, _box(image)) and support and len(enclosed_icons) == 1:
                choices.append((card, support))
        if not choices:
            continue
        card, support = min(choices, key=lambda item: (_area(_box(item[0])), _box(item[0])["y"], _box(item[0])["x"]))
        member_ids = [image.get("id")]+[t.get("id") for t in support]
        records.append({"id": "", "bbox": _box(card), "object_ids": member_ids,
                        "source": "minimum_native_card_containing_one_icon_and_body_caption",
                        "semantic_kind": "icon_caption_card", "container_ids": [card.get("id")]})
        claimed_text.update(t.get("id") for t in support)
        claimed_icons.add(image.get("id"))
    free_images = [o for o in images if o.get("id") not in claimed_icons]
    free_texts = [t for t in texts if t.get("id") not in claimed_text]
    costs = {(i.get("id"), t.get("id")): cost for i in free_images for t in free_texts
             if (cost := _caption_cost(i, t)) is not None}
    for image in free_images:
        options = sorted((cost, tid) for (iid, tid), cost in costs.items() if iid == image.get("id"))
        if not options or (len(options) > 1 and abs(options[0][0]-options[1][0]) < 1e-9):
            continue
        cost, tid = options[0]
        reverse = sorted((value, iid) for (iid, textid), value in costs.items() if textid == tid)
        if reverse[0][1] != image.get("id") or (len(reverse) > 1 and abs(reverse[0][0]-reverse[1][0]) < 1e-9):
            continue
        text = by_id[tid]
        records.append({"id": "", "bbox": _bounds([_box(image), _box(text)]),
                        "object_ids": [image.get("id"), tid], "semantic_kind": "icon_caption_pair",
                        "source": "mutual_unique_nearest_local_caption_attachment", "attachment_cost": cost})
        claimed_text.add(tid)
        claimed_icons.add(image.get("id"))
    if not records:
        return ctx, {"refined": False, "fine_icon_module_count": 0, "protected_icon_count": 0}
    copies = deepcopy(ctx)
    claimed = claimed_text | claimed_icons
    for n, record in enumerate(records, 1):
        record["id"] = "local-icon-module-"+str(n)
    residual = []
    for original in copies.get("modules", []) or []:
        members = [oid for oid in original.get("object_ids", []) if oid not in claimed and oid in by_id]
        if members:
            original["object_ids"] = members
            original["bbox"] = _bounds([_box(by_id[oid]) for oid in members])
            residual.append(original)
    copies["modules"] = records+residual
    membership = {oid: m["id"] for m in records for oid in m["object_ids"]}
    for o in copies["objects"]:
        if o.get("id") in membership:
            o["module_id"] = membership[o["id"]]
    return copies, {"refined": True, "fine_icon_module_count": len(records),
                    "protected_icon_count": len(claimed_icons),
                    "fine_module_sources": dict(Counter(m["source"] for m in records)),
                    "residual_shared_module_count": len(residual)}


class GenerativeDesignAnomalyDetector(v2.GenerativeDesignAnomalyDetector):
    def __init__(self, config=None):
        supplied = dict((config or {}).get("D6", config or {}))
        supplied.setdefault("version", "d6-rule-development-v3-20261029")
        super().__init__(supplied)

    def _icons(self, ctx):
        coarse = super()._icons(ctx)
        ids = coarse["features"]["candidate_icon_ids"]
        if not ids or not v2._native_available(ctx):
            coarse["features"]["icon_module_refinement"] = {"refined": False, "fine_icon_module_count": 0, "protected_icon_count": 0}
            return coarse
        fine_ctx, audit = _fine_icon_context(ctx, ids)
        result = super()._icons(fine_ctx) if audit["refined"] else coarse
        result["features"]["icon_module_refinement"] = audit
        result["features"]["coarse_shared_information_module_count"] = coarse["features"]["information_module_count"]
        result["features"]["module_definition"] = "smallest_native_icon_card_or_mutual_local_caption_then_residual_shared_modules"
        # Evidence was created before the added fields; refresh its measurements.
        for e in result["evidence"]:
            e["measurements"] = result["features"].copy()
        return result

    def _style(self, ctx, contexts):
        previous = super()._style(ctx, contexts)
        cfg, f = self.config, previous["features"]
        background = _background(ctx)
        role, category = ctx.get("page", {}).get("role", "unknown"), background["brightness_class"]
        peers = [p for p in contexts if p is not ctx and p.get("document_id") == ctx.get("document_id")
                 and p.get("page", {}).get("role", "unknown") == role and v2._native_available(p)
                 and category is not None and _background(p)["brightness_class"] == category]
        comparable = (v2._native_available(ctx) and role not in v2._EXCLUDED_ROLES | {"unknown"}
                      and category is not None and len(peers) >= cfg["style_min_peer_pages"])
        refs = {r: Counter() for r in ("title", "body")}
        for peer in peers:
            for r in refs:
                refs[r].update({c for o in v2._text_objects(peer) if v2._style_role(o) == r for c in v2._foreground_colors(o)})
        consensus = {r: {value for value, count in counts.items()
                         if count >= max(2, math.ceil(len(peers)*cfg["style_consensus_fraction"]))}
                     for r, counts in refs.items()}
        measured, novel_objects, novel_values = [], [], set()
        novel_fonts = []
        for o in v2._text_objects(ctx):
            r = v2._style_role(o)
            fonts = {str(x).strip().casefold() for x in o.get("font_families", []) or [] if str(x).strip()}
            font_consensus = set(f["same_text_role_consensus"][r]["fonts"])
            if f["relative_reference_available"] and fonts and font_consensus and fonts-font_consensus:
                novel_fonts.append(o)
            colors = v2._foreground_colors(o)
            if comparable and colors and consensus[r]:
                measured.append(o)
                novel = {c for c in colors if all(v2._color_distance(c, pc) >= cfg["color_novel_distance_min"] for pc in consensus[r])}
                if novel:
                    novel_objects.append(o)
                    novel_values.update(novel)
        fraction = len(novel_objects)/len(measured) if measured else None
        color_trigger = (len(measured) >= cfg["style_min_measured_objects"] and len(novel_values) >= 2
                         and fraction >= cfg["style_novel_fraction_min"])
        font_available = f["font_measured_text_object_count"] >= cfg["style_min_measured_objects"]
        color_available = len(measured) >= cfg["style_min_measured_objects"]
        color_scope = ("unknown_background" if category is None else "unknown_insufficient_same_background_peers"
                       if not comparable else "measured" if color_available else "unknown_foreground_or_peer_consensus")
        for r in consensus:
            f["same_text_role_consensus"][r]["colors"] = sorted(consensus[r])
        f.update(background_reference=background, same_role_same_background_peer_count=len(peers),
                 color_reference_available=comparable, color_scope_status=color_scope,
                 foreground_color_measured_object_count=len(measured), novel_color_object_count=len(novel_objects),
                 role_foreground_color_distance=fraction, novel_foreground_colors=sorted(novel_values),
                 foreground_color_anomaly=bool(color_trigger) if measured else None,
                 font_scope_status="measured" if font_available else "unknown_insufficient_font_observations_or_peers",
                 both_style_branches_available=bool(font_available and color_available),
                 font_reference_scope="same_document_same_page_role_same_text_role",
                 color_reference_scope="same_document_same_page_role_same_text_role_same_measured_background_brightness",
                 shape_fill_colors_used_as_font_colors=False)
        trigger = f["font_anomaly"] or color_trigger
        available = font_available and color_available
        evidence_objects = list({o.get("id"): o for o in novel_fonts+novel_objects}.values())
        return _sub("candidate" if trigger else "no_candidate_in_available_scope" if available else "unknown",
                    .8 if trigger else 0. if available else None, f,
                    [_ev("relative_font_or_background_conditioned_foreground_outlier", evidence_objects, f,
                         "native_font_properties_and_text_RGB_with_separate_native_background_reference")] if trigger else [],
                    ["Font peers retain V2 same-role scope; text colors additionally require measured matching background brightness.",
                     "Unknown or intermediate background leaves color comparison unknown; background RGB is never a font color.",
                     "Intentional font/palette changes still require human review; image illustration style is unsupported."])

    def _decoration(self, ctx, icons):
        result = super()._decoration(ctx, icons)
        unknown_ids = set(result["features"]["unresolved_native_shape_ids"])
        regions, unmeasured_text_ids = [], []
        for text in v2._text_objects(ctx):
            boxes = text.get("content_regions") or []
            valid_regions = [b for raw in boxes if (b := _box({"bbox": raw}))]
            regions.extend(valid_regions)
            if not valid_regions:
                unmeasured_text_ids.append(text.get("id"))
        rois, detached = [], []
        module_boxes = [b for m in ctx.get("modules", []) or [] if (b := _box(m))]
        if v2._native_available(ctx):
            for o in ctx.get("objects", []):
                n, b = o.get("native_shape", {}), _box(o)
                alpha = n.get("fill_alpha")
                if (o.get("id") in unknown_ids and b and n.get("has_visible_fill") is True
                        and isinstance(alpha, (int, float)) and not isinstance(alpha, bool)
                        and math.isfinite(alpha) and 0 < alpha < 1):
                    gap = min((v2._gap(b, t) for t in regions), default=None)
                    module_intersection = sum(_intersection(b, m) for m in module_boxes)
                    rois.append({"object_id": o.get("id"), "bbox": b,
                                 "preset_geometry": n.get("preset_geometry"), "fill_alpha": alpha,
                                 "fill_rgb": v2._color(n.get("fill_rgb")), "has_visible_line": n.get("has_visible_line"),
                                 "module_id": o.get("module_id"), "parent_id": o.get("parent_id"),
                                 "connection_endpoint_count": len(n.get("connection_shape_ids", []) or []),
                                 "minimum_text_region_gap": gap, "information_module_intersection_area": module_intersection,
                                 "text_region_intersection_area": v2._union_area([
                                     {"x": max(b["x"], t["x"]), "y": max(b["y"], t["y"]),
                                      "w": min(b["x"]+b["w"], t["x"]+t["w"])-max(b["x"], t["x"]),
                                      "h": min(b["y"]+b["h"], t["y"]+t["h"])-max(b["y"], t["y"])}
                                     for t in regions if _intersection(b, t) > 0])})
                    excluded = v2._ICON_EXCLUSIONS | {"container", "background", "data_graphic", "bullet_marker", "divider", "chart", "table"}
                    if (o.get("kind") == "shape" and not str(o.get("text", "")).strip()
                            and o.get("content_class") == "unknown" and o.get("semantic_role") not in excluded
                            and n.get("fill_state") == "explicit_solidFill" and n.get("has_visible_line") is False
                            and isinstance(n.get("connection_shape_ids"), list) and not n["connection_shape_ids"]
                            and "parent_id" in o and o["parent_id"] is None and not o.get("module_id")
                            and not o.get("is_master_fixed") and not unmeasured_text_ids
                            and _area(b) <= self.config["icon_max_area"] and gap is not None and gap > .025
                            and module_intersection == 0):
                        detached.append(o)
        result["features"]["unresolved_translucent_shape_review"] = {
            "status": "review_only_no_decoration_inference" if rois else "no_measured_review_rois",
            "count": len(rois), "rois": rois,
            "claim_boundary": "Transparency and missing text attachment do not establish decoration semantics or author intent."}
        count, area = len(detached), v2._union_area([_box(o) for o in detached])
        module_count = icons["features"]["information_module_count"]
        ratio = count/module_count if module_count else None
        measurable = (v2._native_available(ctx) and bool(regions) and not unmeasured_text_ids
                      and bool(module_count))
        trigger = (measurable and count >= self.config["decoration_count_min"]
                   and area >= self.config["decoration_area_min"]
                   and ratio >= self.config["decoration_per_module_min"])
        hypothesis = {"status": "candidate" if trigger else "no_candidate_in_available_scope" if measurable else "unknown",
                      "eligible_object_count": count if measurable else None,
                      "object_ids": [o.get("id") for o in detached], "union_area_ratio": area if measurable else None,
                      "objects_per_information_module": ratio, "measured_text_region_count": len(regions),
                      "unmeasured_information_text_object_ids": unmeasured_text_ids,
                      "thresholds": {k: self.config[k] for k in ("decoration_count_min", "decoration_area_min", "decoration_per_module_min", "icon_max_area")},
                      "minimum_text_region_gap_required": .025,
                      "semantic_class_rewritten": False, "spatial_unknown_mask_rewritten": False,
                      "claim_boundary": "Unattached translucent small-shape pile is a review hypothesis, not decoration ground truth or author intent."}
        result["features"]["unattached_translucent_shape_pile_hypothesis"] = hypothesis
        if trigger:
            result["status"], result["score"] = "candidate", max(result["score"] or 0., min(.95, .6+area))
            result["evidence"].append(_ev("unattached_translucent_small_shape_pile_hypothesis", detached, hypothesis,
                                             "explicit_native_solidFill_alpha_noLine_no_connections_and_measured_text_region_module_exclusion"))
        result["limitations"].append("Unattached translucent shapes can still serve unrecognized information functions; native unknown membership and masks are preserved.")
        return result

    def detect_document(self, contexts):
        result = super().detect_document(contexts)
        for page in result["page_results"].values():
            page["implementation_revision"] = "v3-local-icon-units-and-background-conditioned-color"
        result["document_result"]["implementation_revision"] = "v3-local-icon-units-and-background-conditioned-color"
        return result


def detect_document(contexts, config=None):
    return GenerativeDesignAnomalyDetector(config).detect_document(contexts)
