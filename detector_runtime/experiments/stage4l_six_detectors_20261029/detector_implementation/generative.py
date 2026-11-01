"""Transparent, unfitted D6 engineering detector. No AI authorship classifier.

All scores are ordered heuristic severities, never probabilities. A05 deliberately
does not claim semantic artifact detection; it provides reproducible review ROIs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import itertools
import json
import math
import re

import numpy as np
from scipy.optimize import linear_sum_assignment


DEFAULTS = {
    "version": "d6-engineering-v1-20261029",
    "layout_similarity_min": .92, "layout_min_objects": 3,
    "repeat_cluster_min_pages": 4, "repeat_run_min_pages": 4,
    "repeat_content_difference_min": .60,
    "icon_max_area": .035, "icon_max_side": .22,
    "icon_aspect_min": .5, "icon_aspect_max": 2.,
    "icon_count_min": 10, "icon_per_module_min": 2.,
    "icon_repeat_fraction_min": .5,
    "decoration_count_min": 16, "decoration_area_min": .12,
    "decoration_per_module_min": 2.5,
    "style_min_text_objects": 6, "style_font_families_min": 6,
    "style_fill_colors_min": 8, "style_min_peer_pages": 4,
    "style_role_distance_min": .75,
    "style_internal_minority_fraction_min": .35,
}
_AUX = {"page_number", "navigation", "footnote"}
_NON_CONTENT = {"auxiliary", "background", "decoration"}
_ICON_SEMANTIC_EXCLUSIONS = {"logo", "bullet", "legend", "flow_node", "navigation"}


def _box(obj):
    b = obj.get("bbox")
    if not isinstance(b, dict):
        return None
    vals = [b.get(k) for k in ("x", "y", "w", "h")]
    if any(v is None or not math.isfinite(float(v)) for v in vals):
        return None
    if float(vals[2]) <= 0 or float(vals[3]) <= 0:
        return None
    return dict(zip(("x", "y", "w", "h"), map(float, vals)))


def _union_area(boxes):
    """Exact union of axis-aligned clipped boxes; overlapping decorations count once."""
    rects = []
    for b in boxes:
        if not b:
            continue
        x0, y0 = max(0., b["x"]), max(0., b["y"])
        x1, y1 = min(1., b["x"]+b["w"]), min(1., b["y"]+b["h"])
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    edges = sorted({r[i] for r in rects for i in (0, 2)})
    area = 0.
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((r[1], r[3]) for r in rects if r[0] < right and r[2] > left)
        if not intervals:
            continue
        start, end = intervals[0]
        covered = 0.
        for low, high in intervals[1:]:
            if low > end:
                covered += end-start
                start, end = low, high
            else:
                end = max(end, high)
        area += (right-left)*(covered+end-start)
    return float(area)


def _sub(status, score, features, evidence=None, limitations=None):
    return {"status": status, "score": score,
            "score_kind": "ordinal_heuristic_not_probability",
            "features": features, "evidence": evidence or [],
            "limitations": limitations or []}


def _ev(kind, objs, measurements, source="native_object_geometry_and_declared_properties"):
    return {"kind": kind, "object_ids": [o.get("id") for o in objs],
            "bbox": _bounds([_box(o) for o in objs]),
            "measurement_source": source, "measurements": measurements}


def _bounds(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    x, y = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
    return {"x": x, "y": y,
            "w": max(b["x"]+b["w"] for b in boxes)-x,
            "h": max(b["y"]+b["h"] for b in boxes)-y}


def _layout_objects(ctx):
    answer = []
    for obj in ctx.get("objects", []):
        if (obj.get("content_class") in _NON_CONTENT or obj.get("text_role") in _AUX
                or obj.get("is_master_fixed") or not _box(obj)):
            continue
        if obj.get("kind") not in {"text", "cell", "image", "chart", "table", "shape"}:
            continue
        if obj.get("kind") in {"text", "cell"} and not obj.get("text", "").strip():
            continue
        if obj.get("kind") == "shape" and not obj.get("text", "").strip():
            # Blank uncertain shapes are not assumed to be information modules.
            continue
        answer.append(obj)
    return answer


def layout_similarity(left, right):
    """Hungarian matching, exact identity = 1, unmatched or kind mismatch penalized."""
    if not left or not right:
        return None
    cost = np.ones((len(left), len(right)), dtype=np.float64)
    for i, a in enumerate(left):
        aa = _box(a)
        for j, b in enumerate(right):
            bb = _box(b)
            geom = sum(abs(aa[k]-bb[k]) for k in ("x", "y", "w", "h"))
            cost[i, j] = min(1., geom + (.8 if a.get("kind") != b.get("kind") else 0.))
    rows, cols = linear_sum_assignment(cost)
    unmatched = abs(len(left)-len(right))
    return float(max(0., 1.-(float(cost[rows, cols].sum())+unmatched)/max(len(left), len(right))))


def _fingerprint(objs):
    rows = sorted((str(o.get("kind")), *(round(_box(o)[k], 3) for k in ("x", "y", "w", "h"))) for o in objs)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _text_ngrams(ctx):
    text = "".join(o.get("text", "") for o in _layout_objects(ctx))
    text = re.sub(r"[^\w\u3400-\u9fff]", "", text.lower())
    return {text[i:i+3] for i in range(max(0, len(text)-2))}


def _text_difference(a, b):
    if not a or not b:
        return None
    return float(1.-len(a & b)/len(a | b))


def _longest_run(indices):
    best = run = 0
    previous = None
    for idx in sorted(set(indices)):
        run = run+1 if previous is not None and idx == previous+1 else 1
        best = max(best, run)
        previous = idx
    return best


def _style(ctx):
    objs = [o for o in ctx.get("objects", [])
            if o.get("text", "").strip() and o.get("text_role") not in _AUX
            and o.get("content_class") not in {"background", "auxiliary"}]
    # A missing/inherited family is not a zero or a new font.
    fonts = Counter()
    for obj in objs:
        names = {n.strip().casefold() for n in obj.get("font_families", []) if isinstance(n, str) and n.strip()}
        for name in names:
            fonts[name] += 1
    colors = {str(o["fill_rgb"]).upper().lstrip("#") for o in ctx.get("objects", [])
              if o.get("fill_rgb") and o.get("content_class") != "background"}
    minority = 1.-max(fonts.values())/sum(fonts.values()) if fonts else None
    return {"fonts": fonts, "colors": colors, "text_objects": objs,
            "minority": minority,
            "font_measured_objects": sum(bool(o.get("font_families")) for o in objs)}


class GenerativeDesignAnomalyDetector:
    """Cross-page, icon/decor redundancy and observed-style research candidates."""

    def __init__(self, config=None):
        config = config or {}
        self.config = {**DEFAULTS, **config.get("D6", config)}

    def _icons(self, ctx):
        cfg = self.config
        icons, unknown_groups, unclassified_images = [], [], []
        for obj in ctx.get("objects", []):
            b = _box(obj)
            if not b or obj.get("content_class") in {"auxiliary", "background"}:
                continue
            if obj.get("semantic_role") in _ICON_SEMANTIC_EXCLUSIONS or obj.get("text_role") in _AUX:
                continue
            if obj.get("kind") == "group":
                unknown_groups.append(obj.get("id"))
            if obj.get("kind") != "image":
                continue
            page_aspect = ctx.get("page", {}).get("aspect")
            if page_aspect is None:
                page = ctx.get("page", {})
                page_aspect = (page.get("width_pt")/page.get("height_pt")) if page.get("height_pt") and page.get("width_pt") else None
            # Normalized x/y use different physical page scales. A square icon
            # on a widescreen slide does NOT have normalized w == h.
            if page_aspect is None or not math.isfinite(float(page_aspect)) or float(page_aspect) <= 0:
                unclassified_images.append(obj.get("id"))
                continue
            aspect = b["w"]*float(page_aspect)/b["h"]
            if (b["w"]*b["h"] <= cfg["icon_max_area"]
                    and max(b["w"], b["h"]) <= cfg["icon_max_side"]
                    and cfg["icon_aspect_min"] <= aspect <= cfg["icon_aspect_max"]):
                icons.append(obj)
        icon_ids = {o.get("id") for o in icons}
        modules = [o for o in _layout_objects(ctx) if o.get("id") not in icon_ids
                   and (o.get("text_role") not in {"title", "unknown"} or o.get("text_chars", 0) >= 12
                        or o.get("kind") in {"chart", "table", "image"})]
        hashes = [o.get("image_sha256") for o in icons if o.get("image_sha256")]
        duplicate_fraction = ((len(hashes)-len(set(hashes)))/len(hashes)) if hashes else None
        ratio = len(icons)/max(1, len(modules))
        measured = len(hashes) == len(icons)
        trigger = (len(icons) >= cfg["icon_count_min"] and ratio >= cfg["icon_per_module_min"]
                   and measured and duplicate_fraction is not None
                   and duplicate_fraction >= cfg["icon_repeat_fraction_min"])
        features = {"candidate_icon_count": len(icons), "candidate_icon_ids": sorted(icon_ids),
                    "information_module_count": len(modules), "icons_per_module": ratio,
                    "image_hash_available_count": len(hashes), "repeat_fraction": duplicate_fraction,
                    "icon_union_area_ratio": _union_area([_box(o) for o in icons]),
                    "unresolved_group_ids": unknown_groups,
                    "unclassified_image_ids_missing_page_scale": unclassified_images,
                    "semantic_relation_to_content": "unavailable_no_semantic_model",
                    "candidate_definition": "small roughly square embedded images; not semantic icon recognition"}
        score = min(1., .45+.15*min(len(icons)/max(cfg["icon_count_min"], 1), 2)
                     + .2*(duplicate_fraction or 0)) if trigger else 0.
        unresolved = bool(unclassified_images) or (len(icons) >= cfg["icon_count_min"] and not measured)
        return _sub("candidate" if trigger else ("unknown" if unresolved else "no_candidate_in_available_scope"),
                    score if trigger or not unresolved else None, features,
                    [_ev("repeated_small_image_candidates", icons, features)] if trigger else [],
                    ["Small images can be useful legends or process nodes; unsupported semantic roles require human review.",
                     "Ordinary shapes, bullets and unidentified vector groups are not silently counted as icons."])

    def _decoration(self, ctx, icon_result):
        cfg = self.config
        objs = [o for o in ctx.get("objects", []) if o.get("content_class") == "decoration" and _box(o)
                and o.get("decoration_role") not in {"container", "background", "empty_text"}]
        module_count = icon_result["features"]["information_module_count"]
        area = _union_area([_box(o) for o in objs])
        ratio = len(objs)/max(1, module_count)
        signatures = [o.get("image_sha256") or (str(o.get("kind")), o.get("fill_rgb"),
                      round(_box(o)["w"], 3), round(_box(o)["h"], 3)) for o in objs]
        repeated = (len(signatures)-len(set(signatures)))/len(signatures) if signatures else None
        trigger = len(objs) >= cfg["decoration_count_min"] and area >= cfg["decoration_area_min"] and ratio >= cfg["decoration_per_module_min"]
        features = {"decoration_count": len(objs), "union_area_ratio": area,
                    "decorations_per_information_module": ratio, "repeated_signature_fraction": repeated,
                    "classification_sources": dict(Counter(str(o.get("classification_source", "unknown")) for o in objs)),
                    "unknown_class_object_count": sum(o.get("content_class") in {None, "unknown"} for o in ctx.get("objects", []))}
        unknown_membership = features["unknown_class_object_count"] > 0 and not objs
        return _sub("candidate" if trigger else ("unknown" if unknown_membership else "no_candidate_in_available_scope"),
                    min(1., .5+area) if trigger else (None if unknown_membership else 0.), features,
                    [_ev("decoration_redundancy_candidate", objs, features)] if trigger else [],
                    ["Decoration membership comes from the common layer's stated heuristic, not known author intent.",
                     "Visual salience and necessity are not established by object count or geometric area."])

    def _material(self, ctx):
        rois = [{"object_id": o.get("id"), "bbox": _box(o), "image_sha256": o.get("image_sha256"),
                 "reason": "embedded image: candidate material inspection ROI, not detected artifact"}
                for o in ctx.get("objects", []) if o.get("kind") == "image" and _box(o)
                and o.get("content_class") not in {"background", "auxiliary"}]
        return _sub("unsupported", None,
                    {"artifact_review_rois": rois, "roi_count": len(rois),
                     "ocr_availability": ctx.get("availability", {}).get("ocr", "unknown"),
                     "semantic_material_artifact_detector": "not_implemented_no_validated_dependency_or_truth"},
                    limitations=["No pseudo-text, graph-label consistency or image/text semantic artifact judgment is implemented.",
                                 "No OCR confidence is interpreted as an artifact label."])

    def _style_result(self, ctx, styles, all_contexts):
        cfg = self.config
        sid = ctx["sample_id"]
        current = styles[sid]
        role = ctx.get("page", {}).get("role", "unknown")
        peers = [p for p in all_contexts if p["sample_id"] != sid
                 and p.get("document_id") == ctx.get("document_id")
                 and p.get("page", {}).get("role", "unknown") == role
                 and role not in {"unknown", "section", "cover", "ending"}]
        peer_fonts = Counter()
        for p in peers:
            peer_fonts.update(styles[p["sample_id"]]["fonts"].keys())
        # Consensus requires at least half the same-role peers; single chapter changes are not penalized.
        consensus = {name for name, count in peer_fonts.items() if count >= max(2, math.ceil(len(peers)/2))}
        fontset = set(current["fonts"])
        role_distance = len(fontset-consensus)/len(fontset) if fontset and consensus and len(peers) >= cfg["style_min_peer_pages"] else None
        internal = (len(current["text_objects"]) >= cfg["style_min_text_objects"]
                    and len(fontset) >= cfg["style_font_families_min"]
                    and (current["minority"] or 0.) >= cfg["style_internal_minority_fraction_min"])
        trigger = internal and (len(current["colors"]) >= cfg["style_fill_colors_min"]
                               or (role_distance is not None and role_distance >= cfg["style_role_distance_min"]))
        available = bool(fontset)
        features = {"observed_font_families": sorted(fontset), "font_family_count": len(fontset),
                    "font_measured_text_object_count": current["font_measured_objects"],
                    "text_object_count": len(current["text_objects"]),
                    "font_minority_fraction": current["minority"], "observed_fill_colors": sorted(current["colors"]),
                    "fill_color_count": len(current["colors"]), "same_role_peer_count": len(peers),
                    "same_role_consensus_fonts": sorted(consensus), "role_font_distance": role_distance,
                    "font_foreground_color_consistency": "unavailable_in_shared_schema"}
        return _sub("candidate" if trigger else ("no_candidate_in_available_scope" if available else "unknown"),
                    .75 if trigger else (0. if available else None), features,
                    [_ev("observed_style_mixture_candidate", current["text_objects"], features,
                         "explicit_font_families_and_shape_fill_colors")] if trigger else [],
                    ["Font/color variety is not proof of an aesthetic defect or AI generation.",
                     "Same-role comparison excludes cover, section and ending pages; intentional changes still require review.",
                     "Illustration/icon visual style, palette hierarchy and inherited/unmeasured font families are outside this measurement."])

    def detect_document(self, contexts):
        contexts = list(contexts)
        ids = [c.get("sample_id") for c in contexts]
        if any(not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("D6 requires unique nonempty sample_id values")
        cfg = self.config
        styles = {c["sample_id"]: _style(c) for c in contexts}
        pages = {}
        layout = {c["sample_id"]: _layout_objects(c) for c in contexts}
        for ctx in contexts:
            sid = ctx["sample_id"]
            icons = self._icons(ctx)
            deco = self._decoration(ctx, icons)
            style = self._style_result(ctx, styles, contexts)
            native = ctx.get("availability", {}).get("native")
            image_only = ctx.get("page", {}).get("content_type") == "image_only"
            a01 = _sub("unknown", None, {"layout_fingerprint": _fingerprint(layout[sid]),
                        "layout_object_count": len(layout[sid]), "cluster_ids": [], "repetition_detected": False},
                        limitations=["Repetition alone is visual consistency, not a confirmed design defect."])
            subs = {"A01": a01, "A02": icons, "A03": deco,
                    "A04": style, "A05": self._material(ctx)}
            status = "unknown" if image_only or not ctx.get("objects") or native in {"unsupported", "unavailable", "failed"} else "no_candidate_in_available_scope"
            pages[sid] = {"detector": "D6", "label": "generative_design_anomaly", "version": cfg["version"],
                          "score": None if status == "unknown" else 0.,
                          "score_kind": "ordinal_heuristic_not_probability", "status": status,
                          "coverage": "partial", "implementation_revision": "v1.1-empty-text-not-visible-decoration", "features": {"subdetectors": subs}, "evidence": [],
                          "limitations": ["D6 is an unfitted anomaly candidate system, not an AI-origin detector or confirmed defect label.",
                                          "A05 semantic artifact recognition is unsupported; native image-only pages lack object-layout semantics."]}
        clusters = []
        # Document and role boundaries are hard constraints, never pooled across source decks.
        partitions = defaultdict(list)
        for ctx in contexts:
            partitions[(ctx.get("document_id"), ctx.get("page", {}).get("role", "unknown"))].append(ctx)
        for (docid, role), members in sorted(partitions.items(), key=lambda x: str(x[0])):
            eligible = [c for c in members if len(layout[c["sample_id"]]) >= cfg["layout_min_objects"]
                        and c.get("page", {}).get("content_type") != "image_only"]
            if role in {"cover", "section", "ending", "unknown"}:
                continue
            links = {c["sample_id"]: set() for c in eligible}
            similarity_map = {}
            for a, b in itertools.combinations(eligible, 2):
                sa, sb = a["sample_id"], b["sample_id"]
                sim = layout_similarity(layout[sa], layout[sb])
                similarity_map[tuple(sorted((sa, sb)))] = sim
                if sim is not None and sim >= cfg["layout_similarity_min"]:
                    links[sa].add(sb)
                    links[sb].add(sa)
            seen = set()
            for start in sorted(links):
                if start in seen:
                    continue
                stack, component = [start], []
                while stack:
                    sid = stack.pop()
                    if sid in seen:
                        continue
                    seen.add(sid)
                    component.append(sid)
                    stack.extend(links[sid]-seen)
                if len(component) < cfg["repeat_cluster_min_pages"]:
                    continue
                # Complete-link refinement prevents chains of gradually changing layouts
                # being described as an identical-layout cluster.
                remaining = sorted(component)
                refined = []
                for sid in remaining:
                    placed = False
                    for group in refined:
                        if all(similarity_map.get(tuple(sorted((sid, other))), 0.) >= cfg["layout_similarity_min"] for other in group):
                            group.append(sid)
                            placed = True
                            break
                    if not placed:
                        refined.append([sid])
                by_id = {c["sample_id"]: c for c in eligible}
                for component in refined:
                    if len(component) < cfg["repeat_cluster_min_pages"]:
                        continue
                    pairs = list(itertools.combinations(component, 2))
                    differences = [_text_difference(_text_ngrams(by_id[a]), _text_ngrams(by_id[b])) for a, b in pairs]
                    differences = [d for d in differences if d is not None]
                    content_diff = float(np.mean(differences)) if differences else None
                    run = _longest_run([int(by_id[s].get("page_index", 0)) for s in component])
                    repetition = (run >= cfg["repeat_run_min_pages"] and content_diff is not None
                                  and content_diff >= cfg["repeat_content_difference_min"])
                    redundant = [s for s in component if pages[s]["features"]["subdetectors"]["A02"]["status"] == "candidate"
                                 or pages[s]["features"]["subdetectors"]["A03"]["status"] == "candidate"]
                    clusterid = "d6-layout-"+hashlib.sha256((str(docid)+"|"+"|".join(sorted(component))).encode()).hexdigest()[:12]
                    cluster = {"cluster_id": clusterid, "document_id": docid, "role": role,
                               "sample_ids": sorted(component), "page_count": len(component),
                               "minimum_pairwise_layout_similarity": min(similarity_map[tuple(sorted(p))] for p in pairs),
                               "mean_text_content_difference": content_diff, "longest_consecutive_run": run,
                               "repetition_detected": bool(repetition), "redundancy_page_ids": sorted(redundant),
                               "anomaly_candidate": bool(repetition and redundant)}
                    clusters.append(cluster)
                    for sid in component:
                        sub = pages[sid]["features"]["subdetectors"]["A01"]
                        sub["features"]["cluster_ids"].append(clusterid)
                        sub["features"]["repetition_detected"] = bool(repetition)
                        sub["features"].update({k: cluster[k] for k in ("mean_text_content_difference", "longest_consecutive_run", "minimum_pairwise_layout_similarity")})
                        # Only the pages with additional localized redundancy are candidates;
                        # a cluster does not contaminate all of its otherwise normal members.
                        trigger = repetition and sid in redundant
                        sub["status"] = "candidate" if trigger else "no_candidate_in_available_scope"
                        sub["score"] = .8 if trigger else 0.
                        if trigger:
                            sub["evidence"] = [_ev("repetition_plus_local_redundancy", layout[sid], cluster,
                                                   "cross_page_Hungarian_geometry_matching_and_native_text_ngrams")]
        for ctx in contexts:
            result = pages[ctx["sample_id"]]
            subs = result["features"]["subdetectors"]
            if (subs["A01"]["status"] == "unknown" and len(layout[ctx["sample_id"]]) >= cfg["layout_min_objects"]
                    and ctx.get("page", {}).get("content_type") != "image_only"
                    and ctx.get("page", {}).get("role", "unknown") != "unknown"):
                subs["A01"]["status"] = "no_candidate_in_available_scope"
                subs["A01"]["score"] = 0.
            triggers = []
            if subs["A01"]["status"] == "candidate":
                triggers.append("mechanical_repetition_with_local_redundancy")
            if subs["A02"]["status"] == "candidate" and subs["A03"]["status"] == "candidate":
                triggers.append("combined_icon_and_decoration_redundancy")
            if subs["A04"]["status"] == "candidate":
                triggers.append("observed_font_palette_mixture")
            result["features"]["composite_triggers"] = triggers
            if triggers:
                result["status"] = "candidate"
                result["score"] = max(s["score"] for s in subs.values() if s["status"] == "candidate" and s["score"] is not None)
                result["evidence"] = [e for sub in subs.values() if sub["status"] == "candidate" for e in sub["evidence"]]
        return {"page_results": pages,
                "document_result": {"version": cfg["version"], "implementation_revision": "v1.1-empty-text-not-visible-decoration", "page_count": len(contexts),
                                    "document_ids": sorted({str(c.get("document_id")) for c in contexts}),
                                    "layout_clusters": clusters,
                                    "layout_cluster_count": len(clusters),
                                    "candidate_page_ids": sorted(s for s, r in pages.items() if r["status"] == "candidate"),
                                    "A05_status": "unsupported", "human_validation_status": "unavailable",
                                    "claim_boundary": "Measured repetition/redundancy only; no D6 classification or cluster accuracy without independent human truth."}}
