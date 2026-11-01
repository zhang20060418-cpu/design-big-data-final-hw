"""Feature-only role hypotheses with explicit ambiguity; no author-intent truth.

Coordinates are page-normalized and fonts are measured points. This module never
opens PPTX/PNG files. It consumes the shared feature context once, and returns
updates without changing its inputs. It is deliberately independent of labels,
generator recipes, template identifiers, and previous model scores.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from statistics import median


VERSION = "role-adapter-m1-fixed-mechanical"
_FIELDS = ("x", "y", "w", "h")
_NOTE = re.compile(r"^\s*(?:说明|注(?:释)?|备注|来源|数据来源|note|notes|source)\s*[:：]", re.I)
_NAMED_SECTION = re.compile(
    r"^\s*(?:chapter\s+\d{1,2}|part\s+[0-9ivx]{1,4}|第[一二三四五六七八九十百0-9]{1,4}(?:章|节|部分))(?:\s|[:：.、—-]|$)", re.I
)
_NUMBER_SECTION = re.compile(r"^\s*(?:0?[1-9]|[1-9][0-9])\s*[.、]?\s*$")


def load_config(path=None):
    path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "roles.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("version") != VERSION:
        raise ValueError("Unexpected role configuration version")
    return config


def _box(obj):
    value = obj.get("bbox")
    if not isinstance(value, dict):
        return None
    try:
        box = {k: float(value[k]) for k in _FIELDS}
    except (KeyError, ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in box.values()) or box["w"] < 0 or box["h"] < 0:
        return None
    return box


def _area(box):
    return box["w"] * box["h"] if box else 0.0


def _gap(a, b):
    dx = max(0.0, a["x"] - b["x"] - b["w"], b["x"] - a["x"] - a["w"])
    dy = max(0.0, a["y"] - b["y"] - b["h"], b["y"] - a["y"] - a["h"])
    return math.hypot(dx, dy)


def _text(obj):
    return str(obj.get("text") or "").strip()


def _font(obj, height, cfg):
    measured = [p.get("font_pt") for p in obj.get("paragraphs", []) if p.get("text_chars", 0)]
    measured = [v for v in measured if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0]
    if not measured:
        measured = [v for v in obj.get("font_sizes_pt", [])
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]
    return median(measured) / height * cfg["font_reference_height_pt"] if measured else None


def _is_page_number(obj, cfg):
    b, text = _box(obj), _text(obj)
    return bool(b and b["y"] >= cfg["page_number_y_min"] and
                re.fullmatch(r"\s*[-—]?\s*\d{1," + str(cfg["page_number_max_digits"]) + r"}\s*[-—]?\s*", text))


def _result(role, source, evidence, confidence=None, ambiguity=None):
    return dict(role=role, role_source=source, role_confidence=confidence,
                role_status="unknown" if role == "unknown" else "inferred_not_human_confirmed",
                evidence=evidence, ambiguous_roles=ambiguity or [])


def _page_role(ctx, cfg):
    page, objects = ctx.get("page", {}), ctx.get("objects", [])
    height = float(page["height_pt"])
    native_text = [o for o in objects if _text(o) and o.get("kind") != "cell" and not _is_page_number(o, cfg)]
    count = len(native_text)
    total = sum(len(_text(o)) for o in native_text)
    big_images = [o for o in objects if o.get("kind") == "image" and _area(_box(o)) >= cfg["large_image_area"]]
    content_modules = [o for o in objects if o.get("kind") in {"chart", "table"}]
    evidence = [dict(kind="native_structure_summary", text_objects=count, text_characters=total,
                     substantial_images=len(big_images), chart_or_table_objects=len(content_modules))]
    if page.get("content_type") == "image_only":
        return _result("image", "full_page_raster_native_semantics_unavailable", evidence, .9)
    headings = [o for o in native_text if _box(o) and _box(o)["y"] < .30]
    heading = " ".join(_text(o) for o in headings).lower()
    if re.search(r"(?:^|\s)(?:contents|agenda)(?:\s|$)|目录|议程", heading):
        return _result("contents", "explicit_native_heading_keyword", evidence, .8)
    if re.search(r"thank\s+you|谢谢|感谢聆听|结束语|questions\?", heading):
        return _result("ending", "explicit_native_ending_keyword", evidence, .8)

    # A numeric divider is not inferred from blankness. Typography, position,
    # companion title, and absence of large data/image content must all agree.
    weak_markers = []
    if count <= cfg["section_max_text_objects"] and total <= cfg["section_max_characters"]:
        for obj in native_text:
            b, text, f = _box(obj), _text(obj), _font(obj, height, cfg)
            named, numeric = bool(_NAMED_SECTION.match(text)), bool(_NUMBER_SECTION.fullmatch(text))
            if not (named or numeric) or not b or b["y"] > cfg["section_marker_y_max"]:
                continue
            min_font = cfg["section_named_marker_font_min"] if named else cfg["section_numeric_marker_font_min"]
            companions = [o for o in native_text if o["id"] != obj["id"] and
                          len(_text(o)) <= cfg["section_title_max_characters"] and
                          (_font(o, height, cfg) or 0) >= cfg["section_title_font_min"]]
            # A named section may include its title in the same measured object.
            inline_title = named and len(text) > len(_NAMED_SECTION.match(text).group(0)) + 1
            reasons = dict(marker_object_id=obj["id"], marker_kind="named" if named else "numeric",
                           normalized_font=f, required_font=min_font,
                           companion_title_ids=[o["id"] for o in companions], inline_title=inline_title,
                           no_substantial_media=not big_images and not content_modules)
            if (f is not None and f >= min_font and (companions or inline_title)
                    and not big_images and not content_modules
                    and count - 1 - len(companions) <= cfg["section_max_explanation_objects"]):
                evidence.append(dict(kind="section_multi_cue", **reasons))
                return _result("section", "explicit_marker_typography_and_short_title_structure", evidence, .85 if named else .8)
            weak_markers.append(reasons)
    if weak_markers:
        evidence.append(dict(kind="incomplete_section_evidence", candidates=weak_markers))

    fonts = [(o, _font(o, height, cfg)) for o in native_text]
    measured = [(o, f) for o, f in fonts if f is not None]
    title = max(measured, key=lambda v: (v[1], -len(_text(v[0]))), default=None)
    body_paragraphs = [o["id"] for o, f in fonts if len(_text(o)) >= cfg["body_sentence_characters"]
                       and f is not None and f <= cfg["body_font_max_for_single_paragraph"]]
    if (ctx.get("page_index") == 1 and count <= cfg["cover_max_text_objects"]
            and total <= cfg["cover_max_characters"] and not content_modules and not big_images
            and not body_paragraphs
            and title and title[1] >= cfg["cover_title_font_min"]
            and len(_text(title[0])) <= cfg["title_max_characters"]):
        evidence.append(dict(kind="cover_dominant_title", object_id=title[0]["id"], normalized_font=title[1],
                             title_position=_box(title[0])))
        return _result("cover", "first_page_dominant_measured_title_not_top_kicker", evidence, .75)
    if content_modules:
        return _result("chart", "native_chart_or_table_present", evidence, .8)
    if sum(_area(_box(o)) for o in big_images) >= cfg["image_showcase_area"] and total < 100:
        return _result("image", "major_raster_showcase_hypothesis", evidence, .65)
    # A sufficiently long modest-size paragraph is body evidence even if it is
    # badly parked in a corner. It must not be protected as a sparse divider.
    if body_paragraphs or count >= cfg["body_text_objects_min"] or total >= cfg["body_total_characters_min"]:
        evidence.append(dict(kind="body_information_evidence", paragraph_ids=body_paragraphs))
        return _result("body", "readable_paragraph_or_multiple_information_blocks", evidence, .7)
    if weak_markers:
        return _result("unknown", "section_marker_without_sufficient_independent_cues", evidence, None, ["section", "body"])
    return _result("unknown", "sparse_content_without_demonstrable_page_intent", evidence, None,
                   ["emphasis", "body", "section"] if native_text else ["empty", "image", "section"])


def _navigation_keys(contexts, cfg):
    keys = Counter()
    q = cfg["repeat_position_quantum"]
    for ctx in contexts:
        seen = set()
        for obj in ctx.get("objects", []):
            text, b = _text(obj), _box(obj)
            font = _font(obj, ctx["page"]["height_pt"], cfg)
            if not text or not b or len(text) > cfg["navigation_max_characters"] or font is None:
                continue
            if font <= cfg["navigation_font_max"] and (b["y"] < cfg["navigation_margin_band"] or b["y"] > 1 - cfg["navigation_margin_band"]):
                seen.add((text.casefold(), *(round(b[k] / q) for k in _FIELDS)))
        keys.update(seen)
    return keys


def _object_roles(ctx, page_update, cfg, repeats, doc_size):
    objects, height = ctx.get("objects", []), ctx["page"]["height_pt"]
    texts = [o for o in objects if _text(o)]
    fs = [_font(o, height, cfg) for o in texts if o.get("kind") != "cell"]
    dominant = max([v for v in fs if v is not None], default=None)
    top_fs = [_font(o, height, cfg) for o in texts if _box(o) and _box(o)["y"] < cfg["title_top_y_max"] and o.get("kind") != "cell"]
    top_dominant = max([v for v in top_fs if v is not None], default=None)
    targets = [o for o in objects if o.get("kind") in {"chart", "table", "image"} and _box(o)]
    updates = {}
    role = page_update["role"]
    q = cfg["repeat_position_quantum"]
    for obj in objects:
        text, b, kind = _text(obj), _box(obj), obj.get("kind")
        f = _font(obj, height, cfg)
        if not text:
            if kind in {"text", "cell"} or str(obj.get("shape_type", "")).startswith("TEXT_BOX"):
                updates[obj["id"]] = dict(text_role="unknown", role_source="empty_native_text_no_reading_role",
                                          role_status="not_applicable", evidence=[], ambiguous_roles=[],
                                          content_class="decoration", classification_source="empty_text_no_information",
                                          role_subtype="empty_text")
            continue
        evidence = [dict(kind="measured_role_cues", normalized_font=f, text_characters=len(text), bbox=b)]
        tr, source, subtype, ambiguity = "unknown", "insufficient_role_evidence", None, ["body", "annotation"]
        if kind == "cell":
            tr, source = "table_text", "native_table_cell_identity"
        elif _is_page_number(obj, cfg):
            tr, source = "page_number", "short_numeric_bottom_margin_not_section_marker"
        elif b:
            navkey = (text.casefold(), *(round(b[k] / q) for k in _FIELDS))
            if repeats[navkey] >= max(cfg["minimum_navigation_repetitions"], math.ceil(doc_size / 2)):
                tr, source = "navigation", "small_same_position_margin_text_repeated_within_document"
            elif b["y"] >= cfg["footnote_y_min"] and f is not None and f <= cfg["footnote_font_max"] and len(text) <= cfg["footnote_max_characters"]:
                tr, source = "footnote", "small_bottom_margin_note_hypothesis"
            elif (role in {"cover", "section"} and f is not None and dominant is not None
                  and f >= dominant * cfg["title_dominant_ratio"] and len(text) <= cfg["title_max_characters"]):
                tr, source = "title", "page_role_and_dominant_measured_font_including_centered_title"
                if _NUMBER_SECTION.fullmatch(text) or _NAMED_SECTION.match(text):
                    subtype = "section_marker"
            elif (role == "section" and f is not None and f >= cfg["section_title_font_min"]
                  and len(text) <= cfg["section_title_max_characters"]):
                tr, source, subtype = "title", "short_title_companion_to_independently_inferred_section", "section_title"
            elif (b["y"] < cfg["title_top_y_max"] and f is not None and top_dominant is not None
                  and f >= top_dominant * cfg["title_dominant_ratio"] and len(text) <= cfg["title_max_characters"]
                  and (dominant is None or f >= dominant * cfg["title_dominant_ratio"])):
                tr, source = "title", "top_dominant_font_with_document_page_max_guard"
            elif _NOTE.match(text) and any(_gap(b, _box(target)) <= cfg["annotation_gap_max"] for target in targets):
                tr, source, subtype = "footnote", "explicit_note_prefix_and_nearby_data_or_image_object", "local_annotation"
            elif (role == "chart" and len(text) <= cfg["chart_label_max_characters"] and f is not None
                  and f <= cfg["chart_label_font_max"]
                  and any(_gap(b, _box(target)) <= cfg["chart_label_gap_max"] for target in targets)):
                tr, source = "chart_label", "short_measured_text_near_native_data_object"
            elif role == "body" or len(text) >= cfg["body_sentence_characters"]:
                tr, source = "body", "page_body_context_or_substantial_paragraph"
            elif role == "section":
                tr, source, subtype = "body", "section_companion_explanatory_text", "section_explanation"
            elif role == "cover" and dominant is not None and f is not None and f < dominant * cfg["title_dominant_ratio"]:
                if (b["y"] < cfg["cover_kicker_y_max"] and f < dominant * cfg["cover_kicker_font_ratio_max"]
                        and len(text) < cfg["cover_kicker_characters_max"]):
                    tr, source, subtype = "unknown", "cover_kicker_reading_importance_unconfirmed", "cover_kicker"
                    ambiguity = ["navigation", "body"]
                else:
                    tr, source, subtype = "body", "cover_subtitle_not_main_title", "cover_supporting_text"
        if tr != "unknown":
            ambiguity = []
        is_auxiliary = tr in {"page_number", "navigation"} or (tr == "footnote" and subtype != "local_annotation")
        updates[obj["id"]] = dict(text_role=tr, role_source=source,
                                   role_status="unknown" if tr == "unknown" else "inferred_not_human_confirmed",
                                   evidence=evidence, ambiguous_roles=ambiguity, role_subtype=subtype,
                                   content_class="auxiliary" if is_auxiliary else "information",
                                   classification_source="role_adapter+measured_nonempty_text")
    return updates


def infer_roles(contexts, config=None):
    """Return immutable page/object role updates and same-document references.

    Configuration is fixed before dataset scoring. Missing actual fonts remain
    unavailable; no 18pt or other fallback is introduced. Distinct documents are
    grouped independently. Duplicate sample/object identities are rejected.
    """
    cfg = load_config() if config is None else deepcopy(config)
    by_doc = defaultdict(list)
    seen = set()
    for ctx in contexts:
        sid = ctx.get("sample_id")
        if not sid or sid in seen:
            raise ValueError("Role adapter needs unique nonempty sample_id")
        seen.add(sid)
        height = ctx.get("page", {}).get("height_pt")
        if not isinstance(height, (int, float)) or not math.isfinite(height) or height <= 0:
            raise ValueError("Role adapter needs finite positive page height")
        ids = [o.get("id") for o in ctx.get("objects", [])]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("Role adapter needs unique nonempty object IDs per page")
        if not ctx.get("document_id"):
            raise ValueError("Role adapter needs document_id")
        by_doc[ctx["document_id"]].append(ctx)
    pages, objects, references = {}, {}, {}
    for document_id, doc in sorted(by_doc.items()):
        repeats = _navigation_keys(doc, cfg)
        per_role = defaultdict(list)
        for ctx in sorted(doc, key=lambda c: (c.get("page_index", 0), c["sample_id"])):
            sid = ctx["sample_id"]
            pages[sid] = _page_role(ctx, cfg)
            objects[sid] = _object_roles(ctx, pages[sid], cfg, repeats, len(doc))
            for obj in ctx.get("objects", []):
                update = objects[sid].get(obj["id"])
                if not update or update["text_role"] == "unknown" or update.get("role_subtype") == "section_marker":
                    continue
                actual = [p.get("font_pt") for p in obj.get("paragraphs", []) if p.get("text_chars", 0)]
                actual = [v for v in actual if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]
                if not actual:
                    actual = [v for v in obj.get("font_sizes_pt", []) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]
                per_role[update["text_role"]].extend(actual)
        references[document_id] = dict(role_font_reference_pt={r: median(v) for r, v in sorted(per_role.items()) if v},
                                       role_font_reference_source="same_document_measured_fonts_after_m_role_hypotheses_no_labels",
                                       excluded_reference_subtypes=["section_marker"],
                                       role_reference_counts={r: len(v) for r, v in sorted(per_role.items())})
    return dict(version=VERSION, page_updates=pages, object_updates=objects,
                document_references=references, configuration=cfg,
                limitations=["Roles are feature-only engineering hypotheses, not human intent labels.",
                             "Sparse emphasis versus poorly placed body content can remain ambiguous.",
                             "Chart-label, note and navigation identities need independent role review.",
                             "No generator reference or six-label prediction is used by this adapter."])


def apply_role_updates(contexts, updates):
    """Copy contexts, attach explicit provenance, and preserve content envelopes.

    Rebuild spatial masks/relations after applying this function. It deliberately
    does not rasterize, reopen files, or reuse stale layout-dependent masks.
    """
    result = deepcopy(contexts)
    if set(updates["page_updates"]) != {ctx["sample_id"] for ctx in result}:
        raise ValueError("Page role update identity mismatch")
    for ctx in result:
        sid = ctx["sample_id"]
        page = updates["page_updates"][sid]
        ctx["page"].update({k: page[k] for k in ("role", "role_source", "role_confidence", "role_status")})
        ctx["page"]["role_evidence"] = deepcopy(page["evidence"])
        ctx["page"]["role_ambiguity"] = list(page["ambiguous_roles"])
        for obj in ctx.get("objects", []):
            value = updates["object_updates"][sid].get(obj["id"])
            if value is None:
                continue
            obj.update({k: value[k] for k in ("text_role", "role_source", "role_status", "role_subtype", "content_class", "classification_source")})
            obj["role_evidence"] = deepcopy(value["evidence"])
            obj["role_ambiguity"] = list(value["ambiguous_roles"])
            if value["role_subtype"] == "empty_text":
                obj["content_regions"] = []
            elif not obj.get("content_regions"):
                # A role can be inferred from native identity while the actual
                # paragraph geometry is unresolved (notably table COM frames).
                # Do not turn that measurement gap into a known-empty region.
                obj["content_class"] = "unknown"
                obj["classification_source"] = "role_hypothesis_but_actual_content_region_unavailable"
                obj["role_evidence"].append(dict(kind="content_region_unavailable_not_normal"))
        ctx.setdefault("document", {}).update(deepcopy(updates["document_references"][ctx["document_id"]]))
        ctx.setdefault("availability", {})["role_adapter"] = VERSION
    return result
