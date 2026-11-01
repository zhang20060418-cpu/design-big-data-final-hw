"""D6 rule-development candidate; measured anomalies, never AI authorship.

Reuses the frozen L geometry/evidence helpers without editing that experiment.
Feature inputs are native context geometry/text/style and shared semantic
hypotheses. IDs only join/output records; names, notes and labels are not read.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import importlib.util
import itertools
import math
from pathlib import Path
import re


_L_PATH = (Path(__file__).resolve().parents[2] /
           "stage4l_six_detectors_20261029/detector_implementation/generative.py")
_SPEC = importlib.util.spec_from_file_location("_frozen_L_generative_for_v2", _L_PATH)
_L = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_L)
_box, _bounds, _union_area, _ev, _sub = _L._box, _L._bounds, _L._union_area, _L._ev, _L._sub
layout_similarity = _L.layout_similarity

DEFAULTS = {
    "version": "d6-rule-development-v2-20261029",
    "layout_similarity_min": .92, "role_conflict_similarity_min": .97,
    "layout_min_objects": 3, "repeat_cluster_min_pages": 4,
    "repeat_run_min_pages": 4,
    "icon_max_area": .035, "icon_max_side": .22,
    "icon_aspect_min": .5, "icon_aspect_max": 2.,
    "icon_count_min": 8, "icon_per_module_min": 2.,
    "icon_excess_min": 4, "icon_orphan_fraction_min": .5,
    "decoration_count_min": 8, "decoration_area_min": .025,
    "decoration_per_module_min": 2.,
    "style_min_peer_pages": 3, "style_consensus_fraction": .6,
    "style_min_measured_objects": 3, "style_novel_fraction_min": .5,
    "color_novel_distance_min": .15,
    "text_duplicate_min_chars": 12, "text_duplicate_min_pairs": 2,
    "text_duplicate_fraction_min": .4,
}
_AUX = {"page_number", "navigation", "footnote", "footer"}
_EXCLUDED_ROLES = {"cover", "section", "ending"}
_ICON_EXCLUSIONS = {"logo", "bullet", "legend", "legend_marker", "flow_node",
                    "flow_connector", "navigation", "chart_marker", "data_marker"}
_ORNAMENTS = {"heart", "sun", "moon", "lightningBolt", "smileyFace"}


def _native_available(ctx):
    return (bool(ctx.get("objects")) and
            ctx.get("page", {}).get("content_type") != "image_only" and
            ctx.get("availability", {}).get("native") not in
            {"unsupported", "unavailable", "failed"})


def _text_objects(ctx):
    return [o for o in ctx.get("objects", []) if _box(o) and str(o.get("text", "")).strip()
            and o.get("text_role") not in _AUX
            and o.get("semantic_layer") not in {"footer", "auxiliary"}
            and o.get("content_class") not in {"background", "auxiliary", "decoration"}]


def _body_objects(ctx):
    return [o for o in _text_objects(ctx) if o.get("text_role") != "title"
            and o.get("semantic_layer") != "title"]


def _normalize_text(text):
    return re.sub(r"[^\w\u3400-\u9fff]", "", str(text).casefold())


def _gap(a, b):
    return math.hypot(max(0., a["x"]-b["x"]-b["w"], b["x"]-a["x"]-a["w"]),
                      max(0., a["y"]-b["y"]-b["h"], b["y"]-a["y"]-a["h"]))


def _contains_center(outer, inner, tolerance=.006):
    x, y = inner["x"]+inner["w"]/2, inner["y"]+inner["h"]/2
    return (outer["x"]-tolerance <= x <= outer["x"]+outer["w"]+tolerance and
            outer["y"]-tolerance <= y <= outer["y"]+outer["h"]+tolerance)


def _information_modules(ctx, icon_ids):
    """Prefer shared modules; fallback joins card contents and adjacent text.

    A paragraph/text object is never automatically a separate module. Unresolved
    empty shapes and small images are excluded from the module denominator.
    """
    by_id = {o.get("id"): o for o in ctx.get("objects", [])}
    content = [o for o in _body_objects(ctx) if o.get("id") not in icon_ids]
    content += [o for o in ctx.get("objects", []) if _box(o) and o.get("id") not in icon_ids
                and o.get("kind") in {"chart", "table", "image"}
                and o.get("content_class") == "information"]
    content_ids = {o.get("id") for o in content}
    shared, covered = [], set()
    for module in ctx.get("modules", []) or []:
        mids = [i for i in module.get("object_ids", []) if i in by_id]
        info_ids = [i for i in mids if i in content_ids]
        if not info_ids or module.get("semantic_kind") in {"unknown", "auxiliary", "decoration", "background"}:
            continue
        shared.append({"id": module.get("id"), "bbox": module.get("bbox") or
                       _bounds([_box(by_id[i]) for i in mids]), "object_ids": mids,
                       "source": module.get("source", "shared_geometric_semantic_hypothesis"),
                       "text_chars": sum(len(_normalize_text(by_id[i].get("text", ""))) for i in info_ids),
                       "semantic_kind": module.get("semantic_kind", "information_hypothesis")})
        covered.update(info_ids)
    remaining = [o for o in content if o.get("id") not in covered]
    groups = []
    for o in remaining:
        explicit = o.get("module_id")
        if explicit:
            found = next((g for g in groups if g["key"] == ("declared", explicit)), None)
            if found is None:
                found = {"key": ("declared", explicit), "objects": [], "source": "object_module_id"}
                groups.append(found)
            found["objects"].append(o)
            continue
        containers = [p for p in ctx.get("objects", []) if _box(p)
                      and p.get("decoration_role") == "container"
                      and _contains_center(_box(p), _box(o))]
        if containers:
            container = min(containers, key=lambda p: _box(p)["w"]*_box(p)["h"])
            key = ("container", container.get("id"))
            found = next((g for g in groups if g["key"] == key), None)
            if found is None:
                found = {"key": key, "objects": [], "container": container,
                         "source": "native_container_content_attachment"}
                groups.append(found)
            found["objects"].append(o)
        else:
            groups.append({"key": ("geometry", len(groups)), "objects": [o],
                           "source": "geometric_text_group_fallback"})
    # Connected nearby text shares a module; do not cross container boundaries.
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(groups):
            if a["key"][0] != "geometry":
                continue
            aa = _bounds([_box(o) for o in a["objects"]])
            for j in range(i+1, len(groups)):
                b = groups[j]
                if b["key"][0] != "geometry" or any(o.get("kind") not in {"text", "cell", "shape"} for o in a["objects"]+b["objects"]):
                    continue
                bb = _bounds([_box(o) for o in b["objects"]])
                overlap = max(0., min(aa["x"]+aa["w"], bb["x"]+bb["w"])-max(aa["x"], bb["x"]))
                if overlap/max(min(aa["w"], bb["w"]), 1e-8) >= .55 and _gap(aa, bb) <= .045:
                    a["objects"].extend(b["objects"])
                    groups.pop(j)
                    changed = True
                    break
            if changed:
                break
    for n, g in enumerate(groups):
        objs = g["objects"]
        shared.append({"id": "fallback-module-"+str(n),
                       "bbox": _box(g["container"]) if "container" in g else _bounds([_box(o) for o in objs]),
                       "object_ids": [o.get("id") for o in objs], "source": g["source"],
                       "text_chars": sum(len(_normalize_text(o.get("text", ""))) for o in objs),
                       "semantic_kind": "information_geometry_hypothesis"})
    return shared


def _color(value):
    if not isinstance(value, str):
        return None
    value = value.upper().lstrip("#")
    return value if re.fullmatch(r"[0-9A-F]{6}", value) else None


def _foreground_colors(o):
    # explicit_colors mixes fill/line/text, so is intentionally not used.
    values = o.get("text_foreground_colors")
    if values is None:
        values = o.get("native_shape", {}).get("text_foreground_colors", [])
    if isinstance(values, str):
        values = [values]
    return {c for v in values or [] if (c := _color(v))}


def _color_distance(a, b):
    return math.sqrt(sum((int(a[k:k+2], 16)-int(b[k:k+2], 16))**2 for k in (0, 2, 4)))/(255*math.sqrt(3))


def _style_role(o):
    role = o.get("semantic_layer")
    if role not in {"title", "body"}:
        role = "title" if o.get("text_role") == "title" else "body"
    return role


class GenerativeDesignAnomalyDetector:
    def __init__(self, config=None):
        config = config or {}
        self.config = {**DEFAULTS, **config.get("D6", config)}
        self.legacy = _L.GenerativeDesignAnomalyDetector()

    def _icons(self, ctx):
        cfg, icons, missing_scale = self.config, [], []
        page = ctx.get("page", {})
        aspect = page.get("aspect")
        if aspect is None and page.get("width_pt") and page.get("height_pt"):
            aspect = page["width_pt"]/page["height_pt"]
        for o in ctx.get("objects", []):
            b = _box(o)
            if (not b or o.get("kind") != "image" or o.get("content_class") in {"background", "auxiliary"}
                    or o.get("semantic_role") in _ICON_EXCLUSIONS or o.get("text_role") in _AUX):
                continue
            if aspect is None or not math.isfinite(float(aspect)) or float(aspect) <= 0:
                missing_scale.append(o.get("id"))
                continue
            physical = b["w"]*float(aspect)/b["h"]
            if b["w"]*b["h"] <= cfg["icon_max_area"] and max(b["w"], b["h"]) <= cfg["icon_max_side"] and cfg["icon_aspect_min"] <= physical <= cfg["icon_aspect_max"]:
                icons.append(o)
        ids = {o.get("id") for o in icons}
        modules = _information_modules(ctx, ids)
        counts, attached, orphan = Counter(), [], []
        for o in icons:
            options = []
            for i, m in enumerate(modules):
                if not m.get("bbox"):
                    continue
                gap = _gap(_box(o), m["bbox"])
                declared_attachment = bool(o.get("module_id")) and o.get("module_id") == m.get("id")
                if o.get("id") in m["object_ids"] or declared_attachment or gap <= .055:
                    options.append((gap, i))
            if options:
                _, idx = min(options)
                counts[idx] += 1
                attached.append(o)
            else:
                orphan.append(o)
        repeat_hashes = [o.get("image_sha256") for o in icons if o.get("image_sha256")]
        repeated = (len(repeat_hashes)-len(set(repeat_hashes)))/len(repeat_hashes) if repeat_hashes else None
        excess = sum(max(0, n-1) for n in counts.values())+len(orphan)
        ratio = len(icons)/len(modules) if modules else None
        orphan_fraction = len(orphan)/len(icons) if icons else 0.
        # One useful symbol per module is not icon overuse, even for 16 modules.
        trigger = (len(icons) >= cfg["icon_count_min"] and excess >= cfg["icon_excess_min"] and
                   ((ratio is not None and ratio >= cfg["icon_per_module_min"])
                    or (orphan_fraction >= cfg["icon_orphan_fraction_min"] and len(modules) >= 1)))
        measured = _native_available(ctx) and not missing_scale and (bool(modules) or not icons)
        f = {"candidate_icon_count": len(icons), "candidate_icon_ids": [o.get("id") for o in icons],
             "information_module_count": len(modules) if modules or not icons else None,
             "information_modules": modules, "module_definition": "shared_semantic_geometric_modules_or_attached_native_content_groups",
             "icons_per_module": ratio, "attached_icon_count": len(attached), "orphan_icon_count": len(orphan),
             "orphan_icon_fraction": orphan_fraction, "icons_beyond_one_per_module": excess,
             "module_icon_counts": {modules[i]["id"]: count for i, count in counts.items()},
             "repeat_fraction": repeated, "image_hash_available_count": len(repeat_hashes),
             "icon_union_area_ratio": _union_area([_box(o) for o in icons]),
             "unclassified_image_ids_missing_page_scale": missing_scale,
             "semantic_relation_to_content": "native_attachment_hypothesis_not_image_understanding"}
        return _sub("candidate" if trigger and measured else "no_candidate_in_available_scope" if measured else "unknown",
                    min(.95, .6+.02*excess) if trigger and measured else 0. if measured else None, f,
                    [_ev("icons_exceed_attached_information_modules", icons, f)] if trigger and measured else [],
                    ["Icon semantics are not recognized; native module attachment is a review hypothesis.",
                     "Distinct icon hashes do not prove usefulness; repeated hashes do not prove abuse."])

    def _decoration(self, ctx, icons):
        cfg, objects, unresolved = self.config, [], []
        for o in ctx.get("objects", []):
            if o.get("kind") not in {"shape", "line"} or not _box(o) or str(o.get("text", "")).strip():
                continue
            if o.get("decoration_role") in {"container", "background", "divider", "empty_text"} or o.get("semantic_role") in _ICON_EXCLUSIONS:
                continue
            native = o.get("native_shape", {})
            geometry = native.get("preset_geometry") or o.get("geometry_type")
            visible = native.get("has_visible_fill") is True or native.get("has_visible_line") is True
            semantic = o.get("semantic_role") == "decorative_ornament" and bool(o.get("semantic_evidence"))
            primitive = bool(geometry in _ORNAMENTS or (isinstance(geometry, str) and geometry.startswith("star")))
            # A star beside information can be a bullet/rating. Keep it unresolved.
            attached = any(_gap(_box(o), _box(t)) <= .035 for t in _text_objects(ctx))
            proof = visible and (semantic or (primitive and not attached))
            if proof:
                objects.append(o)
            elif o.get("content_class") in {None, "unknown"}:
                unresolved.append(o.get("id"))
        modules = icons["features"]["information_module_count"]
        ratio = len(objects)/modules if modules else None
        area = _union_area([_box(o) for o in objects])
        trigger = (_native_available(ctx) and len(objects) >= cfg["decoration_count_min"] and area >= cfg["decoration_area_min"]
                   and ratio is not None and ratio >= cfg["decoration_per_module_min"])
        measured = (_native_available(ctx) and (bool(objects) or not unresolved)
                    and (not objects or bool(modules)))
        f = {"decoration_count": len(objects), "union_area_ratio": area,
             "decorations_per_information_module": ratio, "proven_ornament_ids": [o.get("id") for o in objects],
             "unresolved_native_shape_ids": unresolved,
             "classification_sources": dict(Counter(str(o.get("classification_source", "native_geometry_ornament")) for o in objects)),
             "membership_rule": "visible_native_ornament_geometry_with_no_information_attachment_or_shared_semantic_evidence"}
        return _sub("candidate" if trigger else "no_candidate_in_available_scope" if measured else "unknown",
                    min(.95, .6+area) if trigger else 0. if measured else None, f,
                    [_ev("native_ornament_redundancy", objects, f, "native_preset_geometry_visibility_and_shared_attachment_evidence")] if trigger else [],
                    ["Rectangles, ellipses, rings and unidentified blank shapes remain unresolved.",
                     "Ornament membership is a native geometry hypothesis, not known author intent."])

    def _style(self, ctx, contexts):
        cfg, role = self.config, ctx.get("page", {}).get("role", "unknown")
        peers = [p for p in contexts if p is not ctx and p.get("document_id") == ctx.get("document_id")
                 and p.get("page", {}).get("role", "unknown") == role and _native_available(p)]
        comparable = (_native_available(ctx) and role not in _EXCLUDED_ROLES | {"unknown"}
                      and len(peers) >= cfg["style_min_peer_pages"])
        refs = {r: {"fonts": Counter(), "colors": Counter()} for r in ("title", "body")}
        for p in peers:
            for r in refs:
                objs = [o for o in _text_objects(p) if _style_role(o) == r]
                refs[r]["fonts"].update({str(f).strip().casefold() for o in objs for f in o.get("font_families", []) or [] if str(f).strip()})
                refs[r]["colors"].update({c for o in objs for c in _foreground_colors(o)})
        consensus = {r: {k: {v for v, n in counts.items() if n >= max(2, math.ceil(len(peers)*cfg["style_consensus_fraction"]))}
                         for k, counts in categories.items()} for r, categories in refs.items()}
        measured_fonts, novel_fonts, measured_colors, novel_colors = [], [], [], []
        observed_fonts, observed_colors, novel_color_values = set(), set(), set()
        for o in _text_objects(ctx):
            fonts = {str(f).strip().casefold() for f in o.get("font_families", []) or [] if str(f).strip()}
            colors, r = _foreground_colors(o), _style_role(o)
            observed_fonts.update(fonts)
            observed_colors.update(colors)
            if comparable and fonts and consensus[r]["fonts"]:
                measured_fonts.append(o)
                if fonts-consensus[r]["fonts"]:
                    novel_fonts.append(o)
            if comparable and colors and consensus[r]["colors"]:
                measured_colors.append(o)
                novel = {c for c in colors if all(_color_distance(c, pc) >= cfg["color_novel_distance_min"] for pc in consensus[r]["colors"])}
                if novel:
                    novel_colors.append(o)
                    novel_color_values.update(novel)
        font_fraction = len(novel_fonts)/len(measured_fonts) if measured_fonts else None
        color_fraction = len(novel_colors)/len(measured_colors) if measured_colors else None
        font_trigger = (len(measured_fonts) >= cfg["style_min_measured_objects"] and len(novel_fonts) >= 2
                        and font_fraction >= cfg["style_novel_fraction_min"])
        color_trigger = (len(measured_colors) >= cfg["style_min_measured_objects"] and len(novel_color_values) >= 2
                         and color_fraction >= cfg["style_novel_fraction_min"])
        trigger, measured = font_trigger or color_trigger, bool(measured_fonts or measured_colors)
        f = {"page_role": role, "same_role_peer_count": len(peers), "relative_reference_available": comparable,
             "observed_font_families": sorted(observed_fonts), "font_family_count": len(observed_fonts),
             "observed_text_foreground_colors": sorted(observed_colors),
             "same_text_role_consensus": {r: {k: sorted(v) for k, v in categories.items()} for r, categories in consensus.items()},
             "font_measured_text_object_count": len(measured_fonts), "novel_font_object_count": len(novel_fonts),
             "role_font_distance": font_fraction, "foreground_color_measured_object_count": len(measured_colors),
             "novel_color_object_count": len(novel_colors), "role_foreground_color_distance": color_fraction,
             "novel_foreground_colors": sorted(novel_color_values), "font_anomaly": bool(font_trigger),
             "foreground_color_anomaly": bool(color_trigger),
             "shape_fill_colors_used_as_font_colors": False}
        evidence_objects = list({o.get("id"): o for o in novel_fonts+novel_colors}.values())
        return _sub("candidate" if trigger else "no_candidate_in_available_scope" if measured else "unknown",
                    .8 if trigger else 0. if measured else None, f,
                    [_ev("relative_same_role_font_or_foreground_color_outlier", evidence_objects, f,
                         "native_font_families_and_text_foreground_RGB_against_document_peer_consensus")] if trigger else [],
                    ["Requires a stable same-role document reference; isolated pages cannot establish style consistency.",
                     "Intentional font/palette changes still require human review; image illustration style is unsupported."])

    def _local_text_redundancy(self, ctx):
        cfg = self.config
        if not _native_available(ctx):
            return {"status": "unknown", "eligible_body_text_objects": None, "duplicate_pairs": [],
                    "duplicate_object_fraction": None, "duplicate_object_ids": []}
        objs = [o for o in _body_objects(ctx) if len(_normalize_text(o.get("text"))) >= cfg["text_duplicate_min_chars"]]
        module_ids = {oid: module.get("id") for module in ctx.get("modules", []) or []
                      for oid in module.get("object_ids", [])}
        redundant, pairs = set(), []
        for a, b in itertools.combinations(objs, 2):
            ma, mb = a.get("module_id") or module_ids.get(a.get("id")), b.get("module_id") or module_ids.get(b.get("id"))
            if ma and ma == mb:
                continue
            # One text object duplicated by extraction would overlap, not form two modules.
            if _gap(_box(a), _box(b)) < .015:
                continue
            ta, tb = _normalize_text(a.get("text")), _normalize_text(b.get("text"))
            ga, gb = {ta[i:i+3] for i in range(len(ta)-2)}, {tb[i:i+3] for i in range(len(tb)-2)}
            similarity = len(ga & gb)/len(ga | gb) if ga or gb else 0.
            if similarity >= .88:
                redundant.update((a.get("id"), b.get("id")))
                pairs.append({"object_ids": [a.get("id"), b.get("id")], "text_ngram_similarity": similarity})
        fraction = len(redundant)/len(objs) if objs else None
        trigger = len(pairs) >= cfg["text_duplicate_min_pairs"] and fraction is not None and fraction >= cfg["text_duplicate_fraction_min"]
        return {"status": "candidate" if trigger else "no_candidate_in_available_scope",
                "eligible_body_text_objects": len(objs), "duplicate_pairs": pairs,
                "duplicate_object_fraction": fraction, "duplicate_object_ids": sorted(redundant)}

    def detect_document(self, contexts):
        contexts = list(contexts)
        ids = [c.get("sample_id") for c in contexts]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise ValueError("D6 requires unique nonempty sample_id values")
        cfg, pages, layouts, by_id = self.config, {}, {}, {c["sample_id"]: c for c in contexts}
        for ctx in contexts:
            sid = ctx["sample_id"]
            icons = self._icons(ctx)
            ornaments = self._decoration(ctx, icons)
            layouts[sid] = _L._layout_objects(ctx)
            eligible = (_native_available(ctx) and ctx.get("document_id") is not None
                        and len(layouts[sid]) >= cfg["layout_min_objects"]
                        and ctx.get("page", {}).get("role") not in _EXCLUDED_ROLES)
            a01 = _sub("no_candidate_in_available_scope" if eligible else "unknown", 0. if eligible else None,
                       {"layout_object_count": len(layouts[sid]), "layout_fingerprint": _L._fingerprint(layouts[sid]),
                        "cluster_ids": [], "repetition_detected": False, "soft_role_conflicts": [],
                        "local_text_redundancy": self._local_text_redundancy(ctx)},
                       limitations=["Template repetition and content difference alone never trigger A01."])
            subs = {"A01": a01, "A02": icons, "A03": ornaments, "A04": self._style(ctx, contexts),
                    "A05": self.legacy._material(ctx)}
            status = "no_candidate_in_available_scope" if _native_available(ctx) else "unknown"
            pages[sid] = {"detector": "D6", "label": "generative_design_anomaly", "version": cfg["version"],
                          "implementation_revision": "v2-semantic-modules-relative-style", "status": status,
                          "score": 0. if status != "unknown" else None, "score_kind": "ordinal_heuristic_not_probability",
                          "coverage": "partial", "features": {"subdetectors": subs}, "evidence": [],
                          "limitations": ["Development anomaly candidate, not AI-origin detection or confirmed human defect.",
                                          "A05 unsupported; a partial no-candidate result never rules out semantic material artifacts."]}
        clusters = []
        docs = defaultdict(list)
        for ctx in contexts:
            if ctx.get("document_id") is not None:
                docs[ctx["document_id"]].append(ctx)
        for docid, members in docs.items():
            eligible = [c for c in members if pages[c["sample_id"]]["features"]["subdetectors"]["A01"]["score"] is not None]
            sims, links = {}, {c["sample_id"]: set() for c in eligible}
            for a, b in itertools.combinations(eligible, 2):
                sa, sb = a["sample_id"], b["sample_id"]
                sim = layout_similarity(layouts[sa], layouts[sb])
                sims[frozenset((sa, sb))] = sim
                roles_differ = a.get("page", {}).get("role") != b.get("page", {}).get("role")
                minimum = cfg["role_conflict_similarity_min"] if roles_differ else cfg["layout_similarity_min"]
                if sim is not None and sim >= minimum:
                    links[sa].add(sb)
                    links[sb].add(sa)
            # Geometry-only order removes IDs as a clustering tie-break feature.
            order = sorted(eligible, key=lambda c: (c.get("page_index", 0), _L._fingerprint(layouts[c["sample_id"]])))
            refined = []
            for ctx in order:
                sid = ctx["sample_id"]
                group = next((g for g in refined if all(other in links[sid] for other in g)), None)
                if group is None:
                    refined.append([sid])
                else:
                    group.append(sid)
            for component in refined:
                if len(component) < cfg["repeat_cluster_min_pages"]:
                    continue
                pairs = list(itertools.combinations(component, 2))
                diffs = [_L._text_difference(_L._text_ngrams(by_id[a]), _L._text_ngrams(by_id[b])) for a, b in pairs]
                diffs = [d for d in diffs if d is not None]
                difference = sum(diffs)/len(diffs) if diffs else None
                run = _L._longest_run([int(by_id[s].get("page_index", 0)) for s in component])
                repetition = run >= cfg["repeat_run_min_pages"]
                roles = Counter(by_id[s].get("page", {}).get("role", "unknown") for s in component)
                redundancy = {}
                for sid in component:
                    subs = pages[sid]["features"]["subdetectors"]
                    reasons = [k for k in ("A02", "A03") if subs[k]["status"] == "candidate"]
                    if subs["A01"]["features"]["local_text_redundancy"]["status"] == "candidate":
                        reasons.append("separated_near_duplicate_body_text")
                    redundancy[sid] = reasons
                clusterid = "d6-layout-"+hashlib.sha256((str(docid)+"|"+"|".join(sorted(component))).encode()).hexdigest()[:12]
                cluster = {"cluster_id": clusterid, "document_id": docid, "role": roles.most_common(1)[0][0],
                           "role_counts": dict(roles), "role_policy": "soft_conflict_stricter_geometry_no_keyword_boundary",
                           "sample_ids": sorted(component), "page_count": len(component),
                           "minimum_pairwise_layout_similarity": min(sims[frozenset(p)] for p in pairs),
                           "mean_text_content_difference": difference, "longest_consecutive_run": run,
                           "repetition_detected": repetition, "redundancy_page_ids": [s for s in component if redundancy[s]],
                           "anomaly_candidate": bool(repetition and any(redundancy.values()))}
                clusters.append(cluster)
                for sid in component:
                    sub = pages[sid]["features"]["subdetectors"]["A01"]
                    sub["features"].update({"cluster_ids": [clusterid], "repetition_detected": repetition,
                                            "minimum_pairwise_layout_similarity": cluster["minimum_pairwise_layout_similarity"],
                                            "mean_text_content_difference": difference, "longest_consecutive_run": run,
                                            "independent_redundancy_sources": redundancy[sid],
                                            "soft_role_conflicts": list(roles) if len(roles) > 1 else []})
                    if repetition and redundancy[sid]:
                        sub["status"], sub["score"] = "candidate", .8
                        sub["evidence"] = [_ev("repeated_geometry_with_independent_local_redundancy", layouts[sid],
                                                 {"cluster": cluster, "local_redundancy_sources": redundancy[sid]},
                                                 "cross_page_native_geometry_and_local_native_redundancy")]
        for ctx in contexts:
            result = pages[ctx["sample_id"]]
            subs = result["features"]["subdetectors"]
            triggers = [k for k, sub in subs.items() if sub["status"] == "candidate"] if _native_available(ctx) else []
            result["features"]["composite_triggers"] = triggers
            result["features"]["capability_status"] = {k: sub["status"] for k, sub in subs.items()}
            if triggers:
                result["status"] = "candidate"
                result["score"] = max(subs[k]["score"] for k in triggers)
                result["evidence"] = [e for k in triggers for e in subs[k]["evidence"]]
            elif all(sub["status"] in {"unknown", "unsupported"} for sub in subs.values()):
                result["status"], result["score"] = "unknown", None
        document_result = {"detector": "D6", "version": cfg["version"], "page_count": len(contexts),
                           "document_ids": sorted({str(c.get("document_id")) for c in contexts}),
                           "layout_clusters": clusters, "layout_cluster_count": len(clusters),
                           "candidate_page_ids": [sid for sid, r in pages.items() if r["status"] == "candidate"],
                           "A05_status": "unsupported", "human_validation_status": "unavailable",
                           "claim_boundary": "G150-informed rule development; independent classification accuracy unavailable.",
                           "status": "candidate" if any(r["status"] == "candidate" for r in pages.values()) else
                                     "no_candidate_in_available_scope" if any(r["status"] == "no_candidate_in_available_scope" for r in pages.values()) else "unknown",
                           "score_kind": "ordinal_heuristic_not_probability", "score": None,
                           "features": {"A05_status": "unsupported", "page_count": len(contexts)}, "evidence": []}
        available_scores = [r["score"] for r in pages.values() if r["score"] is not None]
        document_result["score"] = max(available_scores) if available_scores else None
        return {"page_results": pages, "document_result": document_result}


def detect_document(contexts, config=None):
    return GenerativeDesignAnomalyDetector(config).detect_document(contexts)
