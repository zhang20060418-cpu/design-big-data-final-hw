"""Conservative, feature-only semantic refinement for the D5/D6 experiment.

This is an additive adapter over the cached M contexts.  It never reads human
labels, notes, shape names, generator recipes, templates, or previous scores.
The only optional external input is the original PPTX package, opened read-only
to recover explicit fill/line/geometry/text-foreground properties.  Unresolved
properties and functional roles remain unknown.  Inputs are never modified.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
from statistics import median
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile
import posixpath

import numpy as np

VERSION = "context-refinement-r1-native-semantics"
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_EXACT_CONTENTS = re.compile(r"^(?:contents?|table\s+of\s+contents|agenda|目录|目\s*录|议程)(?:\s*[/|｜·•—-]\s*(?:contents?|agenda|目录|议程))?$", re.I)
_NUMBER = re.compile(r"^\s*(?:\d{1,2}|[一二三四五六七八九十])\s*[.、）)]?\s*$")
_NUMBERED = re.compile(r"^\s*(?:\d{1,2}|[一二三四五六七八九十])[.、）)]\s*\S+")
_RECTS = {"rect", "roundRect", "round1Rect", "round2SameRect", "round2DiagRect"}
_ORNAMENTS = {"star4", "star5", "star6", "star7", "star8", "star10", "star12", "star16", "star24", "star32", "sun", "moon", "heart", "lightningBolt", "smileyFace"}


def _box(obj):
    value = obj.get("bbox") if isinstance(obj, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        result = {k: float(value[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    return result if all(math.isfinite(v) for v in result.values()) and result["w"] >= 0 and result["h"] >= 0 else None


def _area(b):
    return b["w"] * b["h"] if b else 0.


def _intersection(a, b):
    if not a or not b:
        return 0.
    return max(0., min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])) * max(0., min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))


def _contains(a, b, fraction=.94):
    return bool(_area(b) > 0 and _intersection(a, b) / _area(b) >= fraction)


def _bounds(boxes):
    boxes = [b for b in boxes if b and _area(b) > 0]
    if not boxes:
        return None
    x, y = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
    return dict(x=x, y=y, w=max(b["x"] + b["w"] for b in boxes) - x,
                h=max(b["y"] + b["h"] for b in boxes) - y)


def _gap(a, b):
    if not a or not b:
        return math.inf
    dx = max(0., a["x"] - b["x"] - b["w"], b["x"] - a["x"] - a["w"])
    dy = max(0., a["y"] - b["y"] - b["h"], b["y"] - a["y"] - a["h"])
    return math.hypot(dx, dy)


def _text(o):
    return str(o.get("text") or "").strip()


def _font(o, height=540.):
    values = [p.get("font_pt") for p in o.get("paragraphs", []) if p.get("text_chars", 0)]
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]
    if not values:
        values = [v for v in o.get("font_sizes_pt", []) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]
    return median(values) / height * 540. if values and height > 0 else None


def _regions(o):
    return [b for v in o.get("content_regions", []) if (b := _box({"bbox": v})) and _area(b) > 0]


def _cluster(values, tolerance):
    clusters = []
    for value in sorted(values):
        if clusters and abs(value - median(clusters[-1])) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return clusters


def _grid_evidence(objects):
    """Repeated row/column starts distinguish a drawn data grid from a menu."""
    texts = [o for o in objects if _text(o) and (b := _box(o)) and b["y"] >= .22 and b["y"] < .85 and b["w"] < .45 and b["h"] < .12]
    rows = _cluster([_box(o)["y"] for o in texts], .014)
    cols = _cluster([_box(o)["x"] for o in texts], .018)
    dense_rows = [v for v in rows if len(v) >= 3]
    dense_cols = [v for v in cols if len(v) >= 3]
    return dict(drawn_table_like=len(dense_rows) >= 3 and len(dense_cols) >= 3,
                repeated_rows=len(dense_rows), repeated_columns=len(dense_cols))


def _navigation_evidence(ctx):
    objects = ctx.get("objects", [])
    texts = [o for o in objects if _text(o) and _box(o) and o.get("kind") != "cell" and o.get("text_role") != "page_number"]
    headings = [o for o in texts if _box(o)["y"] < .3 and _EXACT_CONTENTS.fullmatch(_text(o))]
    body = [o for o in texts if .22 <= _box(o)["y"] < .9]
    number_pairs = []
    for number in body:
        if not _NUMBER.fullmatch(_text(number)):
            continue
        b = _box(number)
        labels = [o for o in body if o["id"] != number["id"] and 1 < len(_text(o)) <= 50
                  and not _NUMBER.fullmatch(_text(o)) and _box(o)["x"] >= b["x"] + b["w"] - .01
                  and abs(_box(o)["y"] - b["y"]) <= .025]
        if labels:
            number_pairs.append([number["id"], min(labels, key=lambda o: _box(o)["x"])["id"]])
    inline = [o["id"] for o in body if _NUMBERED.match(_text(o)) and len(_text(o)) <= 60]
    short = [o for o in body if len(_text(o)) <= 45 and not _NUMBER.fullmatch(_text(o)) and _box(o)["h"] <= .12]
    columns = defaultdict(list)
    for o in short:
        columns[round(_box(o)["x"] / .035)].append(o)
    # A lexical heading alone is insufficient; unnumbered agendas need a clear
    # aligned list of at least four short items and no substantial paragraph.
    aligned = max((len(_cluster([_box(o)["y"] for o in os], .03)) for os in columns.values()), default=0)
    substantial = [o["id"] for o in body if len(_text(o)) > 65 or _box(o)["h"] > .17]
    media = [o["id"] for o in objects if o.get("kind") in {"chart", "table"} or
             (o.get("kind") == "image" and _area(_box(o)) >= .08)]
    grid = _grid_evidence(objects)
    structure = len(number_pairs) >= 3 or len(inline) >= 3 or (aligned >= 4 and not substantial)
    accepted = bool(headings and structure and not media and not grid["drawn_table_like"] and not substantial)
    return dict(kind="contents_navigation_structure", accepted=accepted,
                exact_heading_ids=[o["id"] for o in headings], numbered_label_pairs=number_pairs,
                inline_numbered_ids=inline, aligned_short_rows=aligned,
                substantial_paragraph_ids=substantial, substantial_media_ids=media, **grid)


def _refine_page_role(ctx):
    page = ctx.setdefault("page", {})
    before = page.get("role", "unknown")
    nav = _navigation_evidence(ctx)
    if before != "contents" and not nav["accepted"]:
        return dict(before=before, after=before, changed=False, evidence=[nav])
    if nav["accepted"]:
        role, reason, confidence = "contents", "explicit_heading_and_native_navigation_list_without_body_graphics", .85
    else:
        texts = [o for o in ctx.get("objects", []) if _text(o) and o.get("kind") != "cell" and o.get("text_role") not in {"page_number", "navigation", "footnote"}]
        height = float(page.get("height_pt") or 540.)
        measured = [(o, _font(o, height)) for o in texts]
        measured = [(o, f) for o, f in measured if f is not None]
        title = max(measured, key=lambda t: t[1], default=None)
        data = [o for o in ctx.get("objects", []) if o.get("kind") in {"table", "chart"}]
        large_images = [o for o in ctx.get("objects", []) if o.get("kind") == "image" and _area(_box(o)) >= .10]
        total = sum(len(_text(o)) for o in texts)
        long_body = [o for o, f in measured if len(_text(o)) >= 60 and f <= 22]
        cover = (ctx.get("page_index") == 1 and len(texts) <= 6 and total <= 240 and not data
                 and not large_images and not long_body and title and title[1] >= 26 and len(_text(title[0])) <= 120)
        if cover:
            role, reason, confidence = "cover", "first_page_dominant_measured_title_after_rejected_contents_keyword", .75
        elif data:
            role, reason, confidence = "chart", "native_chart_or_table_after_rejected_contents_keyword", .8
        elif long_body or len(texts) >= 4 or total >= 100 or nav["drawn_table_like"]:
            role, reason, confidence = "body", "substantial_native_information_after_rejected_contents_keyword", .7
        else:
            role, reason, confidence = "unknown", "contents_keyword_without_navigation_or_sufficient_alternative_role", None
    page.update(role=role, role_source=reason, role_confidence=confidence,
                role_status="unknown" if role == "unknown" else "inferred_not_human_confirmed")
    page["role_evidence"] = list(page.get("role_evidence", [])) + [nav]
    page["role_ambiguity"] = ["body", "contents", "emphasis"] if role == "unknown" else []
    return dict(before=before, after=role, changed=before != role, source=reason, evidence=[nav])


def _rgb(node, theme):
    if node is None:
        return None
    color = next((c for c in node if c.tag.rsplit("}", 1)[-1] in {"srgbClr", "schemeClr", "sysClr"}), None)
    if color is None:
        return None
    tag = color.tag.rsplit("}", 1)[-1]
    raw = color.get("val") if tag == "srgbClr" else color.get("lastClr") if tag == "sysClr" else theme.get(color.get("val"))
    if not raw or not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return None
    channels = [int(raw[i:i + 2], 16) / 255. for i in (0, 2, 4)]
    for child in color:
        local = child.tag.rsplit("}", 1)[-1]
        if local in {"alpha", "alphaMod", "alphaOff"}:
            continue
        try:
            value = int(child.get("val", "")) / 100000.
        except ValueError:
            return None
        if local == "tint":
            channels = [v + (1 - v) * value for v in channels]
        elif local == "shade":
            channels = [v * value for v in channels]
        else:
            return None  # Unsupported transforms are not guessed as RGB.
    return "#" + "".join(f"{round(min(1., max(0., v)) * 255):02X}" for v in channels)


def _alpha(node):
    if node is None:
        return None
    values = [child for color in node for child in color if child.tag.rsplit("}", 1)[-1] in {"alpha", "alphaMod", "alphaOff"}]
    try:
        alpha = 1.
        for value in values:
            number = int(value.get("val")) / 100000.
            local = value.tag.rsplit("}", 1)[-1]
            alpha = number if local == "alpha" else alpha * number if local == "alphaMod" else alpha + number
        return min(1., max(0., alpha))
    except (TypeError, ValueError):
        return None


def _paint(parent, theme):
    if parent is None:
        return dict(visible=None, alpha=None, rgb=None, state="inherited_or_unavailable")
    if parent.find("a:noFill", NS) is not None:
        return dict(visible=False, alpha=0., rgb=None, state="explicit_noFill")
    solid = parent.find("a:solidFill", NS)
    if solid is not None:
        alpha = _alpha(solid)
        return dict(visible=alpha > 0 if alpha is not None else None, alpha=alpha, rgb=_rgb(solid, theme), state="explicit_solidFill")
    other = [parent.find("a:" + name, NS) for name in ("gradFill", "pattFill", "blipFill")]
    if any(v is not None for v in other):
        return dict(visible=None, alpha=None, rgb=None, state="complex_fill_visibility_unresolved")
    return dict(visible=None, alpha=None, rgb=None, state="inherited_or_unavailable")


def _package_properties(path):
    """Return only allowlisted properties indexed by (slide number, shape ID)."""
    result = {}
    with ZipFile(path) as package:
        names = set(package.namelist())

        def relationships(part):
            relpath = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            if relpath not in names:
                return []
            return [(r.get("Type", "").rsplit("/", 1)[-1],
                     r.get("Target", "").lstrip("/") if r.get("Target", "").startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(part), r.get("Target", ""))))
                    for r in ET.fromstring(package.read(relpath)) if r.get("TargetMode") != "External"]

        themes = {}

        def read_theme(theme_path):
            if theme_path in themes:
                return themes[theme_path].copy()
            result = {}
            if theme_path not in names:
                return result
            root = ET.fromstring(package.read(theme_path))
            scheme = root.find("a:themeElements/a:clrScheme", NS)
            if scheme is not None:
                for color in scheme:
                    child = next(iter(color), None)
                    if child is not None:
                        result[color.tag.rsplit("}", 1)[-1]] = child.get("val") if child.tag.endswith("srgbClr") else child.get("lastClr")
            themes[theme_path] = result
            return result.copy()

        def slide_theme(slide_path, slide):
            layout_path = next((target for kind, target in relationships(slide_path) if kind == "slideLayout"), None)
            master_path = next((target for kind, target in relationships(layout_path) if kind == "slideMaster"), None) if layout_path else None
            theme_path = next((target for kind, target in relationships(master_path) if kind == "theme"), None) if master_path else None
            if theme_path is None:
                theme_paths = [n for n in names if re.fullmatch(r"ppt/theme/theme\d+\.xml", n)]
                # A single theme is safe fallback for minimal packages; with
                # several themes a missing relationship remains unresolved.
                theme_path = theme_paths[0] if len(theme_paths) == 1 else None
            theme = read_theme(theme_path)
            mapping = dict(tx1="dk1", tx2="dk2", bg1="lt1", bg2="lt2")
            for part in (master_path, layout_path):
                if part in names:
                    tree = ET.fromstring(package.read(part))
                    clrmap = tree.find("p:clrMap", NS)
                    override = tree.find("p:clrMapOvr/a:overrideClrMapping", NS)
                    if clrmap is not None:
                        mapping.update(clrmap.attrib)
                    if override is not None:
                        mapping.update(override.attrib)
            override = slide.find("p:clrMapOvr/a:overrideClrMapping", NS)
            if override is not None:
                mapping.update(override.attrib)
            return {**theme, **{key: theme.get(value) for key, value in mapping.items()}}
        slide_paths = []
        if "ppt/presentation.xml" in names and "ppt/_rels/presentation.xml.rels" in names:
            rels = {r.get("Id"): r.get("Target", "").lstrip("/") if r.get("Target", "").startswith("/") else posixpath.normpath(posixpath.join("ppt", r.get("Target", ""))) for r in ET.fromstring(package.read("ppt/_rels/presentation.xml.rels")) if r.get("TargetMode") != "External"}
            presentation = ET.fromstring(package.read("ppt/presentation.xml"))
            slide_paths = [rels.get(n.get("{" + NS["r"] + "}id")) for n in presentation.findall("p:sldIdLst/p:sldId", NS)]
        if not slide_paths:
            slide_paths = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"slide(\d+)", n).group(1)))
        for slide_number, slide_path in enumerate(slide_paths, 1):
            if slide_path not in names:
                continue
            slide = ET.fromstring(package.read(slide_path))
            theme = slide_theme(slide_path, slide)
            for node in slide.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if local not in {"sp", "cxnSp", "pic", "graphicFrame", "grpSp"}:
                    continue
                nvpath = {"sp": "p:nvSpPr", "cxnSp": "p:nvCxnSpPr", "pic": "p:nvPicPr", "graphicFrame": "p:nvGraphicFramePr", "grpSp": "p:nvGrpSpPr"}[local]
                nv = node.find(nvpath, NS)
                identity = nv.find("p:cNvPr", NS) if nv is not None else None
                # Shape names, titles and descriptions are deliberately unread.
                shape_id = identity.get("id") if identity is not None else None
                if not shape_id:
                    continue
                sp = node.find("p:spPr", NS)
                fill = _paint(sp, theme)
                line = _paint(sp.find("a:ln", NS) if sp is not None else None, theme)
                geom = sp.find("a:prstGeom", NS) if sp is not None else None
                colors = set()
                for text_body in node.findall("p:txBody", NS):
                    for props_name in ("rPr", "defRPr", "endParaRPr"):
                        for props in text_body.findall(".//a:" + props_name, NS):
                            paint = _paint(props, theme)
                            rgb = paint["rgb"] if paint["visible"] is True else None
                            if rgb:
                                colors.add(rgb)
                connections = []
                for connector in (nv.findall(".//a:stCxn", NS) + nv.findall(".//a:endCxn", NS)) if nv is not None else []:
                    if connector.get("id"):
                        connections.append(connector.get("id"))
                placeholder = nv.find("p:nvPr/p:ph", NS) if nv is not None else None
                result[(slide_number, shape_id)] = dict(
                    has_visible_fill=fill["visible"], fill_alpha=fill["alpha"], fill_rgb=fill["rgb"], fill_state=fill["state"],
                    has_visible_line=line["visible"], line_alpha=line["alpha"], line_rgb=line["rgb"], line_state=line["state"],
                    preset_geometry=geom.get("prst") if geom is not None else None,
                    text_foreground_colors=sorted(colors), text_foreground_source="explicit_native_text_properties_linked_theme_resolved" if colors else "unavailable_not_inferred_from_shape_fill",
                    connection_shape_ids=connections,
                    placeholder_type=placeholder.get("type") if placeholder is not None else None,
                    placeholder_index=placeholder.get("idx") if placeholder is not None else None,
                    source="readonly_PPTX_OpenXML_allowlist_no_shape_names_no_notes")
    return result


def _attach_native(contexts, roots=None, enabled=True):
    audit = dict(enabled=enabled, packages_read=0, objects_enriched=0, missing_sources=[], hash_mismatches=[], errors=[], input_allowlist=["native_fill", "native_line", "preset_geometry", "text_foreground_colors", "connection_endpoint_ids", "placeholder_identity"])
    if not enabled:
        return audit
    by_source = defaultdict(list)
    for ctx in contexts:
        source = ctx.get("source", {})
        by_source[(source.get("pptx_path"), source.get("pptx_sha256"))].append(ctx)
    candidates = None
    for (raw_path, expected_hash), pages in by_source.items():
        path = Path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            if roots and expected_hash:
                if candidates is None:
                    candidates = [p for root in roots for p in Path(root).rglob("*.pptx")]
                path = next((p for p in candidates if hashlib.sha256(p.read_bytes()).hexdigest() == expected_hash), None)
            if path is None or not path.is_file():
                audit["missing_sources"].append(dict(sample_ids=[p.get("sample_id") for p in pages], source_path=str(raw_path)))
                continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            audit["hash_mismatches"].append(dict(source_path=str(path), sample_ids=[p.get("sample_id") for p in pages]))
            continue
        try:
            properties = _package_properties(path)
        except (OSError, ValueError, ET.ParseError, BadZipFile) as error:
            audit["errors"].append(dict(source_path=str(path), error=type(error).__name__))
            continue
        audit["packages_read"] += 1
        for ctx in pages:
            ctx.setdefault("availability", {})["native_semantic_properties"] = "partial_explicit_OpenXML_allowlist"
            for obj in ctx.get("objects", []):
                identity = re.search(r"(?:^|-)shapeid(\d+)(?:$|-)", str(obj.get("id", "")))
                props = properties.get((ctx.get("page_index"), identity.group(1))) if identity else None
                if props is None:
                    continue
                obj["native_shape"] = deepcopy(props)
                obj["text_foreground_colors"] = list(props["text_foreground_colors"])
                # Empty means unavailable, not black or shape-fill color.
                obj["text_foreground_source"] = props["text_foreground_source"]
                audit["objects_enriched"] += 1
    return audit


def _semantic_layers(ctx):
    objects = ctx.get("objects", [])
    height = float(ctx.get("page", {}).get("height_pt") or 540.)
    texts = [o for o in objects if _text(o)]
    max_font = max((_font(o, height) or 0 for o in texts), default=0.)
    # Repair title reading roles only where the page role actually changed.
    if ctx.get("_role_changed"):
        for o in texts:
            if o.get("text_role") in {"page_number", "footnote", "navigation", "table_text"}:
                continue
            f, b = _font(o, height), _box(o)
            if b and f is not None and max_font > 0 and f >= .9 * max_font and len(_text(o)) <= 120 and (b["y"] < .28 or ctx["page"]["role"] == "cover"):
                o.update(text_role="title", role_source="refined_page_role_and_dominant_measured_font")
            elif ctx["page"]["role"] == "body" or len(_text(o)) >= 60:
                o.update(text_role="body", role_source="substantial_text_after_page_role_refinement")
    titles = [o for o in texts if o.get("text_role") == "title" and _box(o)]
    sidebar_support = set()
    for title in titles:
        tb = _box(title)
        if not _NUMBER.fullmatch(_text(title)) or (_font(title, height) or 0) < 30 or tb["y"] >= .32:
            continue
        panels = [o for o in objects if not _text(o) and (pb := _box(o)) and pb["w"] <= .42
                  and pb["h"] >= .65 and _contains(pb, tb)
                  and (o.get("geometry_type") in _RECTS or o.get("native_shape", {}).get("preset_geometry") in _RECTS)]
        for panel in panels:
            members = [o for o in texts if _contains(_box(panel), _box(o)) and o.get("text_role") != "page_number"]
            if (2 <= len(members) <= 7 and all(len(_text(o)) <= 60 for o in members)
                    and any(o["id"] != title["id"] and (_font(o, height) or 0) >= 18 for o in members)):
                sidebar_support.update(o["id"] for o in members if o["id"] != title["id"])
    layer_ids = {"title": [], "body": [], "footer": [], "unknown": []}
    for o in objects:
        b, text, role = _box(o), _text(o), o.get("text_role")
        evidence = []
        layer, semantic_role = "unknown", "unknown"
        if text:
            if role == "title":
                layer, semantic_role = "title", "title"
                evidence.append(dict(kind="existing_measured_title_role", source=o.get("role_source")))
            elif o["id"] in sidebar_support:
                layer, semantic_role = "title", "section_sidebar_support"
                evidence.append(dict(kind="short_companion_inside_narrow_chapter_marker_panel"))
            elif role in {"page_number", "footnote"} or (role == "navigation" and b and b["y"] >= .75):
                layer, semantic_role = "footer", "local_annotation" if o.get("role_subtype") == "local_annotation" else "footer"
                evidence.append(dict(kind="existing_auxiliary_reading_role", text_role=role))
            elif b and b["y"] < .22 and len(text) <= 40 and any(
                    _box(t)["y"] >= b["y"] and _box(t)["y"] - (b["y"] + b["h"]) <= .065
                    and abs(_box(t)["x"] - b["x"]) <= .04 and (_font(o, height) or math.inf) <= (_font(t, height) or 0) * .72 for t in titles):
                layer, semantic_role = "title", "title_kicker"
                evidence.append(dict(kind="short_smaller_text_immediately_above_aligned_measured_title", title_ids=[t["id"] for t in titles]))
            elif b and b["y"] >= .78 and re.match(r"^(?:注(?:释)?|说明|备注|来源|数据来源|note|source)\s*[:：]", text, re.I):
                layer, semantic_role = "footer", "footer_note"
                evidence.append(dict(kind="explicit_note_prefix_in_bottom_margin"))
            elif b and b["y"] >= .84 and (_font(o, height) or math.inf) <= 11 and len(text) <= 180:
                layer, semantic_role = "footer", "footer_note"
                evidence.append(dict(kind="small_bottom_margin_text_without_title_or_data_identity"))
            elif role in {"body", "table_text", "chart_label"} or (o.get("content_class") == "information" and _regions(o)):
                layer, semantic_role = "body", role if role in {"body", "table_text", "chart_label"} else "body_text_hypothesis"
                evidence.append(dict(kind="nonempty_native_information_text", text_role=role, measured_regions=bool(_regions(o))))
        elif o.get("kind") in {"chart", "table", "image"} and o.get("content_class") == "information":
            layer, semantic_role = "body", o.get("kind")
            evidence.append(dict(kind="existing_primary_native_content_identity"))
        o["semantic_layer"], o["semantic_role"], o["semantic_evidence"] = layer, semantic_role, evidence
        layer_ids[layer].append(o.get("id"))
    return layer_ids


def _shape_hypotheses(ctx):
    os = ctx.get("objects", [])
    texts = [o for o in os if _text(o) and _box(o) and o.get("semantic_layer") == "body"]
    body_objects = [o for o in os if o.get("semantic_layer") == "body" and _box(o)]
    hypotheses = []
    for o in os:
        if _text(o) or o.get("kind") not in {"shape", "line"} or not (b := _box(o)):
            continue
        native = o.get("native_shape", {})
        geometry = native.get("preset_geometry") or o.get("geometry_type")
        visible = native.get("has_visible_fill") is True or native.get("has_visible_line") is True
        invisible = native.get("has_visible_fill") is False and native.get("has_visible_line") is False
        enclosed = [t for t in texts if _contains(b, _box(t))]
        role, evidence = "unknown", []
        classification = None
        if o.get("content_class") == "background":
            role = "background"
            evidence = [dict(kind="retained_M_background_hypothesis", source=o.get("classification_source"))]
        elif invisible:
            role, classification = "invisible_native_shape", "decoration"
            evidence = [dict(kind="native_explicit_no_visible_fill_and_no_visible_line", source=native.get("source"))]
        elif enclosed and geometry in _RECTS and _area(b) < .78:
            role, classification = "container", "decoration"
            evidence = [dict(kind="rectangular_panel_enclosing_body_information", object_ids=[t["id"] for t in enclosed], native_geometry=geometry)]
        elif native.get("connection_shape_ids"):
            role = "flow_connector"
            evidence = [dict(kind="native_connection_endpoints", shape_ids=native["connection_shape_ids"])]
        elif "arrow" in str(geometry).lower() and visible:
            attached = [t["id"] for t in body_objects if _gap(b, _box(t)) < .045]
            if len(attached) >= 2:
                role = "flow_connector"
                evidence = [dict(kind="native_arrow_between_multiple_information_objects", object_ids=attached, native_geometry=geometry)]
        elif visible and geometry in _ORNAMENTS and not any(_gap(b, _box(t)) <= .025 or _intersection(b, _box(t)) > 0 for t in body_objects):
            role, classification = "decorative_ornament", "decoration"
            evidence = [dict(kind="native_geometry_ornament_without_information_attachment", preset_geometry=geometry, source=native.get("source"))]
        elif visible and _area(b) <= .00018 and .17 <= b["y"] < .88:
            neighbors = [t for t in texts if 0 <= _box(t)["x"] - (b["x"] + b["w"]) <= .03 and abs((_box(t)["y"] + _box(t)["h"] / 2) - (b["y"] + b["h"] / 2)) <= .025]
            if neighbors:
                role, classification = "bullet_marker", "decoration"
                evidence = [dict(kind="small_visible_marker_immediately_left_of_body_text", object_ids=[t["id"] for t in neighbors])]
        elif (min(b["w"], b["h"]) < .008 and max(b["w"], b["h"]) >= .10
              and ((b["y"] < .22 or b["y"] > .9) or o.get("decoration_role") == "divider")):
            role, classification = "divider", "decoration"
            evidence = [dict(kind="thin_native_rule_retained_as_layout_hypothesis", native_geometry=geometry)]
        if role == "unknown" and o.get("decoration_role") == "container":
            # M's any-text containment can include chart bars/diagram nodes.
            # Do not silently promote that old generic guess to confirmed decor.
            o["content_class"] = "unknown"
            o["classification_source"] = "unconfirmed_container_without_rectangular_body_panel_evidence"
            o["decoration_role"] = None
        if classification:
            o["content_class"] = classification
            o["content_regions"] = []
            o["classification_source"] = "context_refinement+" + evidence[0]["kind"]
            o["decoration_role"] = "ornament" if role == "decorative_ornament" else role
        if role != "unknown":
            o["semantic_role"], o["semantic_evidence"] = role, evidence
        hypotheses.append(dict(object_id=o.get("id"), semantic_role=role, evidence=evidence, status="unknown" if role == "unknown" else "inferred_not_human_confirmed"))
    return hypotheses


def _quantitative_graphics(ctx):
    """Recover repeated labelled graphic bars that are not merely panels.

    A blank rectangle alone proves nothing. Here at least three visible shapes
    must share an axis, carry explicit numerical text, and vary in size. This
    captures funnels and bar stacks without declaring arbitrary shapes data.
    """
    os = ctx.get("objects", [])
    texts = [o for o in os if _text(o) and o.get("semantic_layer") == "body" and _box(o)]
    candidates = []
    for o in os:
        b, native = _box(o), o.get("native_shape", {})
        if _text(o) or not b or o.get("semantic_role") != "container" or _area(b) > .15 or b["h"] > .25:
            continue
        if native.get("has_visible_fill") is not True:
            continue
        enclosed = [t for t in texts if _contains(b, _box(t))]
        numeric = [t for t in enclosed if re.search(r"\d+(?:[.,]\d+)?\s*(?:%|％|次|人|件|个|万|千|亿|项|[kKmM])?(?:\s|$)", _text(t))]
        if numeric and len(enclosed) >= 2:
            candidates.append((o, enclosed))
    groups = []
    for item in sorted(candidates, key=lambda item: _box(item[0])["y"]):
        b = _box(item[0])
        match = next((g for g in groups if abs((_box(g[0][0])["x"] + _box(g[0][0])["w"] / 2) - (b["x"] + b["w"] / 2)) <= .025
                      and min(_box(g[0][0])["h"], b["h"]) / max(_box(g[0][0])["h"], b["h"], 1e-9) >= .75), None)
        if match is None:
            groups.append([item])
        else:
            match.append(item)
    recovered = []
    for group in groups:
        widths = [_box(o)["w"] for o, _ in group]
        monotonic = all(a <= b for a, b in zip(widths, widths[1:])) or all(a >= b for a, b in zip(widths, widths[1:]))
        if len(group) < 3 or min(widths) / max(widths) > .85 or not monotonic:
            continue
        ids = [o["id"] for o, _ in group]
        for o, enclosed in group:
            o.update(semantic_layer="body", semantic_role="data_graphic", content_class="information",
                     content_regions=[deepcopy(_box(o))], decoration_role=None,
                     classification_source="repeated_visible_quantitative_graphic_with_numeric_labels")
            o["semantic_evidence"] = [dict(kind="repeated_axis_aligned_variable_width_graphic_with_numeric_labels",
                                            related_shape_ids=ids, object_ids=[t["id"] for t in enclosed], source=o.get("native_shape", {}).get("source"))]
            recovered.append(o["id"])
    return recovered


def _modules(ctx):
    """Group real information attachments; ungrouped labels stay unassigned."""
    os = ctx.get("objects", [])
    for obj in os:
        # Module membership is rebuilt from attachments, never inherited from
        # a generator-assigned or stale cached grouping.
        obj["module_id"] = None
    by_id = {o["id"]: o for o in os}
    info = [o for o in os if o.get("semantic_layer") == "body" and _box(o) and o.get("content_class") == "information"]
    parent = {o["id"]: o["id"] for o in info}
    reasons = defaultdict(set)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(ids, source):
        ids = [i for i in ids if i in parent]
        if len(ids) < 2:
            return
        root = find(ids[0])
        for identity in ids[1:]:
            parent[find(identity)] = root
        for identity in ids:
            reasons[identity].add(source)

    # Actual native group/table identity is stronger than geometric proximity.
    groups = defaultdict(list)
    for o in info:
        if o.get("parent_id"):
            groups[o["parent_id"]].append(o["id"])
    for ids in groups.values():
        union(ids, "native_shared_parent")
    containers = [o for o in os if o.get("semantic_role") == "container" and _box(o)]
    attachments = {}
    for container in sorted(containers, key=lambda o: _area(_box(o))):
        enclosed = [o["id"] for o in info if _contains(_box(container), _box(o))]
        union(enclosed, "rectangular_information_container")
        attachments[container["id"]] = enclosed
    # Nearby aligned text belongs to one block only when widths agree and the
    # gap is smaller than normal inter-panel spacing. Titles never join body.
    texts = [o for o in info if _text(o) and o.get("kind") != "cell"]
    for i, a in enumerate(texts):
        ba = _box(a)
        for b in texts[i + 1:]:
            bb = _box(b)
            xover = max(0., min(ba["x"] + ba["w"], bb["x"] + bb["w"]) - max(ba["x"], bb["x"]))
            vertical_gap = max(ba["y"], bb["y"]) - min(ba["y"] + ba["h"], bb["y"] + bb["h"])
            if (0 <= vertical_gap <= .026 and abs(ba["x"] - bb["x"]) <= .025
                    and xover / max(min(ba["w"], bb["w"]), 1e-9) >= .8
                    and min(ba["w"], bb["w"]) / max(ba["w"], bb["w"], 1e-9) >= .68):
                union([a["id"], b["id"]], "tight_aligned_text_block")
    # A drawn table has repeated rows *and* columns; it is one information
    # module, not a module per text cell or every object on the page.
    grid = _grid_evidence(info)
    if grid["drawn_table_like"]:
        grid_texts = [o for o in texts if .22 <= _box(o)["y"] < .85 and _box(o)["w"] < .45 and _box(o)["h"] < .12]
        # Limit to the largest recurrent column system, avoiding side captions.
        columns = _cluster([_box(o)["x"] for o in grid_texts], .018)
        starts = [median(col) for col in columns if len(col) >= 3]
        grid_ids = [o["id"] for o in grid_texts if any(abs(_box(o)["x"] - x) <= .018 for x in starts)]
        union(grid_ids, "drawn_repeated_row_column_grid")
    for shape in os:
        if shape.get("semantic_role") == "data_graphic":
            related = shape.get("semantic_evidence", [{}])[0].get("related_shape_ids", [])
            enclosed = [o["id"] for o in info if _contains(_box(shape), _box(o))]
            union(related + enclosed, "repeated_quantitative_native_graphic")
        if shape.get("semantic_role") == "flow_connector":
            targets = shape.get("native_shape", {}).get("connection_shape_ids", [])
            ids = [o["id"] for o in info if any(re.search(r"(?:^|-)shapeid" + re.escape(sid) + r"(?:$|-)", o["id"]) for sid in targets)]
            union(ids, "native_connector_endpoints")
    components = defaultdict(list)
    for o in info:
        components[find(o["id"])].append(o["id"])
    result = []
    for ids in components.values():
        sources = sorted(set().union(*(reasons[i] for i in ids)))
        primary = [by_id[i] for i in ids if by_id[i].get("kind") in {"image", "chart", "table"}]
        if len(ids) < 2 and not primary:
            continue
        box = _bounds([_bounds(_regions(by_id[i])) or _box(by_id[i]) for i in ids])
        if box is None:
            continue
        result.append(dict(id="", bbox=box, object_ids=sorted(ids), source="+".join(sources) or "native_primary_object",
                           semantic_kind="data_grid" if "drawn_repeated_row_column_grid" in sources else "flow" if "native_connector_endpoints" in sources else "information_panel" if "rectangular_information_container" in sources else "information_block",
                           confidence="conservative_hypothesis", container_ids=[cid for cid, members in attachments.items() if set(members) & set(ids)]))
    result.sort(key=lambda m: (m["bbox"]["y"], m["bbox"]["x"], m["object_ids"]))
    for index, module in enumerate(result, 1):
        module["id"] = f"information-module-{index:03d}"
        for identity in module["object_ids"]:
            by_id[identity]["module_id"] = module["id"]
        for cid in module["container_ids"]:
            by_id[cid]["module_id"] = module["id"]
    ctx["modules"] = result
    return result


def _add_rect(mask, b):
    if not b or _area(b) <= 0:
        return
    height, width = mask.shape
    x0, x1 = max(0, min(width, math.floor(b["x"] * width))), max(0, min(width, math.ceil((b["x"] + b["w"]) * width)))
    y0, y1 = max(0, min(height, math.floor(b["y"] * height))), max(0, min(height, math.ceil((b["y"] + b["h"]) * height)))
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = True


def _spatial(ctx):
    previous = ctx.get("spatial", {})
    cached = previous.get("content_mask")
    shape = cached.shape if isinstance(cached, np.ndarray) and cached.ndim == 2 else (270, 480)
    content, unknown = np.zeros(shape, bool), np.zeros(shape, bool)
    for o in ctx.get("objects", []):
        if o.get("content_class") == "information":
            for b in _regions(o):
                _add_rect(content, b)
        elif o.get("content_class") == "unknown":
            _add_rect(unknown, _box({"bbox": o.get("visible_envelope")}) or _box(o))
    unknown &= ~content
    ys, xs = np.nonzero(content)
    spatial = deepcopy(previous)
    spatial.update(content_mask=content, unknown_mask=unknown, content_occupancy=float(content.mean()),
                   unknown_fraction=float(unknown.mean()), mask_source="refined_M_native_measured_regions_and_explicit_unknown_envelopes",
                   content_bounds=dict(x=float(xs.min() / shape[1]), y=float(ys.min() / shape[0]), w=float((xs.max() - xs.min() + 1) / shape[1]), h=float((ys.max() - ys.min() + 1) / shape[0])) if len(xs) else None,
                   content_centroid=[float((xs.mean() + .5) / shape[1]), float((ys.mean() + .5) / shape[0])] if len(xs) else None,
                   mask_grid_error_bound_normalized=dict(x=1 / shape[1], y=1 / shape[0]))
    # Cached multiscale object counts describe the earlier class partition.
    # Detectors should use refined regions/modules rather than stale summaries.
    spatial["multiscale"] = []
    spatial["multiscale_status"] = "invalidated_after_semantic_refinement_use_regions_or_masks"
    ctx["spatial"] = spatial


def refine_contexts(contexts, *, pptx_roots=None, read_native=True):
    """Return ``(deep_copied_contexts, audit)`` for feature-only D5/D6 use.

    ``contexts`` are already adapted by M; their measured paragraph geometry
    and coordinate audit remain authoritative. Cached masks may be attached by
    the caller. ``pptx_roots`` is an optional fallback for moved source packages;
    a package is accepted only if its SHA256 matches the cached source hash.
    """
    copies = deepcopy(list(contexts))
    native_audit = _attach_native(copies, roots=pptx_roots, enabled=read_native)
    audit = dict(version=VERSION, pages=len(copies), role_changes=[], semantic_layer_counts={}, module_count=0,
                 unknown_object_count=0, native_attributes=native_audit,
                 feature_sources=["cached_M_measured_context", "optional_readonly_native_PPTX_allowlist"],
                 excluded_inputs=["human_labels", "notes", "shape_names", "template_ids", "generator_recipes", "previous_scores"],
                 limitations=["Semantic roles and module boundaries are engineering hypotheses, not author-intent truth.",
                              "Unknown fill inheritance, nonrectangular data symbols and ungrouped labels remain unresolved.",
                              "Explicit font foreground colors do not claim complete theme/master/run inheritance resolution."])
    by_doc = defaultdict(list)
    layer_counts = Counter()
    for ctx in copies:
        role_change = _refine_page_role(ctx)
        ctx["_role_changed"] = role_change["changed"]
        layers = _semantic_layers(ctx)
        hypotheses = _shape_hypotheses(ctx)
        quantitative = _quantitative_graphics(ctx)
        if quantitative:
            # Expose the final post-graphic partition, not stale earlier layers.
            layers = {key: [o["id"] for o in ctx.get("objects", []) if o.get("semantic_layer") == key]
                      for key in ("title", "body", "footer", "unknown")}
            for hypothesis in hypotheses:
                if hypothesis["object_id"] in quantitative:
                    obj = next(o for o in ctx["objects"] if o["id"] == hypothesis["object_id"])
                    hypothesis.update(semantic_role=obj["semantic_role"], evidence=deepcopy(obj["semantic_evidence"]))
        modules = _modules(ctx)
        unknowns = [o["id"] for o in ctx.get("objects", []) if o.get("semantic_role") == "unknown" or o.get("content_class") == "unknown"]
        ctx["_context_refinement"] = dict(version=VERSION, role_change=role_change, semantic_layers=layers,
                                          information_modules=deepcopy(modules), object_hypotheses=hypotheses,
                                          unknowns=unknowns, attribute_read_audit=dict(source="readonly_native_PPTX_allowlist", available=ctx.get("availability", {}).get("native_semantic_properties", "unavailable")))
        ctx.pop("_role_changed", None)
        _spatial(ctx)
        ctx.setdefault("availability", {})["context_refinement"] = VERSION
        if role_change["changed"]:
            audit["role_changes"].append(dict(sample_id=ctx.get("sample_id"), **role_change))
        audit["module_count"] += len(modules)
        audit["unknown_object_count"] += len(unknowns)
        layer_counts.update({layer: len(ids) for layer, ids in layers.items()})
        by_doc[ctx.get("document_id")].append(ctx)
    # Distinct documents have independent measured text-layer references.
    for doc in by_doc.values():
        fonts = defaultdict(list)
        for ctx in doc:
            for o in ctx.get("objects", []):
                if _text(o) and o.get("semantic_layer") in {"title", "body", "footer"}:
                    values = [p.get("font_pt") for p in o.get("paragraphs", []) if p.get("text_chars", 0)] or o.get("font_sizes_pt", [])
                    fonts[o["semantic_layer"]].extend(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0)
        for ctx in doc:
            ctx.setdefault("document", {})["semantic_layer_font_reference_pt"] = {key: median(values) for key, values in sorted(fonts.items()) if values}
            ctx["document"]["semantic_layer_reference_source"] = "same_document_native_measured_fonts_after_conservative_semantic_layers"
    audit["semantic_layer_counts"] = dict(layer_counts)
    return copies, audit


def refine_context(context, **kwargs):
    """Single-context convenience wrapper; document references use this page."""
    return refine_contexts([context], **kwargs)[0][0]
