"""Isolated measurement adapter; original rule thresholds are left unchanged.

No app integration, semantic defect labels, learned model or image inference.
Unknown inherited properties remain unknown. Fill and text-foreground evidence
are distinct, including for a fully transparent text-box background.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys

EXPERIMENT = Path(__file__).resolve().parents[1]
ML = EXPERIMENT.parents[1]
H = ML / "experiments/stage4h"
NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def baseline_modules():
    parser = load_module("j_measurement_frozen_parser", H / "structural/source_snapshot/pptx_parser.py")
    rules = load_module("j_measurement_frozen_rules", H / "structural/source_snapshot/structure_rules.py")
    return parser, rules


def opacity_from_sppr(sppr):
    """Classify a local fill only; no inference from bare fill type.

    An RGB solid without alpha has the local DrawingML opacity default. Theme
    colors, gradients, images, inherited fills and transforms are unresolved.
    The value describes the shape fill, never whether its foreground text paints.
    """
    if sppr is None:
        return dict(state="partial_or_unknown", alpha=None, source="no_local_spPr", reason="inherited_or_unmeasured_fill")
    tag = lambda name: "{" + NS + "}" + name
    if sppr.find(tag("noFill")) is not None:
        return dict(state="transparent", alpha=0.0, source="local_noFill", reason="fill_only_no_claim_about_text_foreground")
    solid = sppr.find(tag("solidFill"))
    if solid is None:
        kind = next((name for name in ("gradFill", "blipFill", "pattFill", "grpFill") if sppr.find(tag(name)) is not None), "unspecified")
        return dict(state="partial_or_unknown", alpha=None, source="local_"+kind, reason="unsupported_or_inherited_fill")
    colors = list(solid)
    if len(colors) != 1:
        return dict(state="partial_or_unknown", alpha=None, source="local_solidFill", reason="missing_or_ambiguous_color")
    color = colors[0]
    color_kind = color.tag.rsplit("}", 1)[-1]
    if color_kind not in {"srgbClr", "scrgbClr", "sysClr", "prstClr"}:
        return dict(state="partial_or_unknown", alpha=None, source="local_solid_"+color_kind, reason="theme_color_resolution_not_attempted")
    modifiers = [n for n in color if n.tag.rsplit("}",1)[-1].startswith("alpha")]
    if any(n.tag != tag("alpha") for n in modifiers) or len(modifiers) > 1:
        return dict(state="partial_or_unknown", alpha=None, source="local_solid_"+color_kind, reason="unsupported_alpha_transforms")
    try:
        raw = int(modifiers[0].get("val")) if modifiers else 100000
    except (ValueError, TypeError):
        return dict(state="partial_or_unknown", alpha=None, source="local_solid_"+color_kind, reason="invalid_alpha")
    if raw < 0 or raw > 100000:
        return dict(state="partial_or_unknown", alpha=None, source="local_solid_"+color_kind, reason="alpha_out_of_range")
    state = "transparent" if raw == 0 else ("fully_opaque" if raw == 100000 else "partial_or_unknown")
    return dict(state=state, alpha=raw/100000, source="explicit_alpha" if modifiers else "local_rgb_solid_default_alpha", reason="fill_only_no_claim_about_text_foreground")


def local_font_measurement(shape):
    """Resolve only a visible run's own size or its local paragraph defRPr.

    No layout/master/theme/default size is guessed. Missing visible text fonts
    produce a partial/unknown state even if some neighboring runs are known.
    """
    records = []
    if not getattr(shape, "has_text_frame", False):
        return dict(state="not_applicable", known_sizes_pt=[], visible_runs=0, unresolved_runs=0, runs=[])
    tag = lambda name: "{" + NS + "}" + name
    for p_index, paragraph in enumerate(shape.text_frame.paragraphs):
        ppr = paragraph._p.find(tag("pPr"))
        default = ppr.find(tag("defRPr")) if ppr is not None else None
        for r_index, run in enumerate(paragraph.runs):
            if not run.text.strip():
                continue
            rpr = run._r.find(tag("rPr"))
            raw = rpr.get("sz") if rpr is not None else None
            source = "run_rPr_sz"
            if raw is None:
                raw = default.get("sz") if default is not None else None
                source = "paragraph_pPr_defRPr_sz" if raw is not None else "unresolved_layout_master_theme_or_default"
            try:
                value = int(raw) / 100 if raw is not None and int(raw) > 0 else None
            except (ValueError, TypeError):
                value = None; source = "invalid_local_size"
            records.append(dict(paragraph_index=p_index, run_index=r_index, chars=len(run.text),
                                size_pt=value, source=source))
    unknown = sum(r["size_pt"] is None for r in records)
    state = "known_local" if records and not unknown else ("partially_known" if records and unknown < len(records) else "unknown")
    return dict(state=state, known_sizes_pt=sorted({r["size_pt"] for r in records if r["size_pt"] is not None}),
                visible_runs=len(records), unresolved_runs=unknown, runs=records)


def wrap_measurement(shape):
    if not getattr(shape, "has_text_frame", False):
        return dict(state="not_applicable", value=None, source="no_text_frame")
    value = shape.text_frame.word_wrap
    return dict(state="explicit" if value is not None else "unknown", value=value,
                source="local_bodyPr_wrap" if value is not None else "inherited_or_unspecified_not_resolved")


def adapted_parse(path, original_parser):
    from pptx import Presentation
    document, original = original_parser.parse_document(Path(path))
    adapted = copy.deepcopy(original)
    prs = Presentation(str(path))
    measurements = []
    for slide_detail, actual_slide in zip(adapted["slides"], prs.slides):
        leaves = list(original_parser._iter_leaves(actual_slide.shapes))
        if len(leaves) != len(slide_detail["elements"]):
            raise ValueError("Parser leaf alignment changed")
        blocks = {b["element_i"]: b for b in slide_detail["blocks"]}
        for el, leaf in zip(slide_detail["elements"], leaves):
            shape = leaf.shape
            stable = f"s{slide_detail['index']:04d}-shapeid{shape.shape_id}"
            opacity = opacity_from_sppr(getattr(shape._element, "spPr", None))
            fonts = local_font_measurement(shape)
            wrap = wrap_measurement(shape)
            el["source_object_id"] = stable
            el["fill_measurement"] = opacity
            el["opaque"] = opacity["state"] == "fully_opaque"
            if el["i"] in blocks:
                b = blocks[el["i"]]
                b["source_object_id"] = stable
                b["font_measurement"] = fonts; b["wrap_measurement"] = wrap
                b["sizes"] = fonts["known_sizes_pt"]
                b["word_wrap"] = wrap["value"]  # Retain None in measured output.
            measurements.append(dict(slide_index=slide_detail["index"], object_id=stable,
                                     name=el["name"], kind=el["kind"], bbox=el["bbox"],
                                     text_chars=el["chars"], opacity=opacity, fonts=fonts, wrap=wrap,
                                     baseline_opaque=original["slides"][slide_detail["index"]-1]["elements"][el["i"]]["opaque"]))
    return document, original, adapted, measurements


def evaluate_adapter(detail, document, original_rules):
    """Use original rules with explicit applicability gates around R02.

    Original R02 would treat None as False and missing sizes as 18pt. Those
    blocks are withheld only from R02 and placed in unresolved. Other original
    rule inputs remain available. No threshold is modified.
    """
    other = copy.deepcopy(detail)
    r02_only = copy.deepcopy(detail)
    unresolved = []
    for slide, r02_slide in zip(other["slides"], r02_only["slides"]):
        eligible = []
        for block in r02_slide["blocks"]:
            reasons = []
            if block["wrap_measurement"]["state"] != "explicit": reasons.append("word_wrap_unknown_not_false")
            if block["font_measurement"]["state"] != "known_local": reasons.append("font_size_incomplete_no_fabricated_18pt")
            if reasons:
                unresolved.append(dict(slide_index=slide["index"], rule_id="R02", label="content_overflow_cutoff",
                                       object_ids=[block["source_object_id"]], status="unknown", measurement_kind="text_layout_estimate",
                                       reasons=reasons, details=dict(wrap=block["wrap_measurement"],font=block["font_measurement"])))
            else:
                eligible.append(block)
        r02_slide["blocks"] = eligible
        # Prevent a false branch in incidental R02 execution. Its outputs are
        # discarded; R02 is obtained solely from the eligibility-gated pass.
        for block in slide["blocks"]:
            if block["word_wrap"] is None: block["word_wrap"] = True
            # This pass supplies every rule except R02. Bypass only R02 via
            # its own early-exit so discarded estimates cannot consume the
            # original final per-page eight-candidate cap before R05.
            block["auto_size"] = "TEXT_TO_FIT_SHAPE (adapter pass routing only)"
            if block["font_measurement"]["state"] != "known_local":
                unresolved.append(dict(slide_index=slide["index"], rule_id="R03", label="improper_font_sizing",
                                       object_ids=[block["source_object_id"]], status="unknown", measurement_kind="font_size",
                                       reasons=["some_visible_runs_require_unresolved_inheritance"], details=block["font_measurement"]))
        elements = slide["elements"]
        blocks = {b["element_i"]:b for b in slide["blocks"]}
        area = detail["slide_width_in"]*detail["slide_height_in"]
        for target in elements:
            if target["i"] not in blocks: continue
            for cover in elements[target["i"]+1:]:
                if cover["kind"] == "text" and cover["chars"] == 0: continue
                ix,iy = original_rules._intersection(target["bbox"],cover["bbox"])
                ratio = ix*iy/max(1e-6,target["bbox"]["w"]*target["bbox"]["h"])
                if ratio < .45 or cover["bbox"]["w"]*cover["bbox"]["h"] < area*.03: continue
                opacity = cover["fill_measurement"]
                common = dict(slide_index=slide["index"],rule_id="R05",label="occluded_content",
                              object_ids=[target["source_object_id"],cover["source_object_id"]], status="unknown")
                if opacity["state"] == "partial_or_unknown":
                    unresolved.append(dict(**common,measurement_kind="opaque_fill_overlap",reasons=["fill_opacity_not_known_fully_opaque"],details=dict(opacity=opacity,frame_coverage_ratio=ratio)))
                if cover["chars"] > 0:
                    unresolved.append(dict(**common,measurement_kind="text_foreground_overlap",reasons=["fill_opacity_does_not_measure_text_ink_overlap"],details=dict(opacity=opacity,frame_coverage_ratio=ratio)))
    issues, risks = original_rules.analyze(other, document)
    only_r02, _ = original_rules.analyze(r02_only, document)
    pool = [i for i in issues if i["rule_id"] != "R02"] + [i for i in only_r02 if i["rule_id"] == "R02"]
    final=[]
    for slide in detail["slides"]:
        ordered=sorted([i for i in pool if i["slide_index"]==slide["index"]],key=lambda x:(x["rule_id"],x["issue_id"]))
        final.extend(ordered[:8])
    return final, risks, unresolved


def scoped_candidate(issue, detail, sample_id):
    slide = detail["slides"][issue["slide_index"]-1]
    block = next((b for b in slide["blocks"] if b["element_id"] == issue.get("element_id")), None)
    ids = [block["source_object_id"]] if block else []
    if not ids and issue.get("details",{}).get("element"):
        matches=[e for e in slide["elements"] if e["name"]==issue["details"]["element"]]
        ids=[e["source_object_id"] for e in matches]
    cover_states=[]
    if issue["rule_id"] == "R05":
        covers=[e for e in slide["elements"] if e["name"]==issue.get("details",{}).get("covered_by")]
        ids.extend(e["source_object_id"] for e in covers)
        cover_states=[e.get("fill_measurement",{}).get("state","partial_or_unknown") for e in covers]
    return dict(rule_id=issue["rule_id"],label=issue["defect_type"],status="candidate",
                measurement_kind="opaque_fill_overlap" if issue["rule_id"]=="R05" else ("font_size" if issue["rule_id"]=="R03" else "geometry_or_layout_candidate"),
                scope=dict(kind="object" if ids else "page",sample_id=sample_id,object_ids=sorted(set(ids))),
                details=issue,unresolved_scope=not ids and issue["rule_id"]!="R04",
                fill_state=("fully_opaque" if cover_states and all(x=="fully_opaque" for x in cover_states) else "partial_or_unknown") if issue["rule_id"]=="R05" else None,
                font_measurement=block.get("font_measurement") if block is not None else None)
