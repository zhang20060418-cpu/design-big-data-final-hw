"""Read-only native feature bridge, shared with the existing render COM session.

Only the uploaded OpenXML objects and their live PowerPoint layout are read.
Research batch entrypoints, annotations, benchmark manifests and cached results
are never invoked. Groups/rotation and unsupported cell coordinates stay unknown.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

PROJECT = Path(__file__).resolve().parents[4]
EXPERIMENTS = PROJECT / "ml_stage4" / "experiments"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def extract_native_contexts(source: Path, presentation: Any, out_dir: Path,
                            png_entries: list[dict], office_version: str) -> dict:
    """Measure a presentation already opened read-only by render_worker."""
    from pptx import Presentation
    from PIL import Image

    source, out_dir = Path(source).resolve(), Path(out_dir).resolve()
    before = sha(source)
    ingest = module("app_live_L_ingest", EXPERIMENTS / "stage4l_six_detectors_20261029/detector_implementation/extract_contexts.py")
    native = module("app_live_K_native", EXPERIMENTS / "stage4k_constructed_diagnostics_20261029/scripts/run_native_diagnostics.py")
    coord = module("app_live_M_coordinate", EXPERIMENTS / "stage4m_role_coordinate_graph_20261029/implementation/coordinate_adapter.py")
    proof_path = EXPERIMENTS / "stage4m_role_coordinate_graph_20261029/results/coordinate_transform_proof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    j = native.load_j()
    prs = Presentation(str(source))
    if len(prs.slides) != int(presentation.Slides.Count):
        raise ValueError("COM/OpenXML page count differs")
    width, height = float(presentation.PageSetup.SlideWidth), float(presentation.PageSetup.SlideHeight)
    rendered = {int(e["index"]): e for e in png_entries}
    contexts, errors, cells = [], [], []
    for index, py_slide in enumerate(prs.slides, 1):
        measured, regions, pending = {}, {}, []
        try:
            slide = presentation.Slides.Item(index)
            com_by_id = {int(slide.Shapes.Item(n).Id): slide.Shapes.Item(n)
                         for n in range(1, int(slide.Shapes.Count) + 1)}
            for order, shape in enumerate(py_slide.shapes):
                sid = f"s{index:04d}-shapeid{shape.shape_id}"
                if int(shape.shape_type) == 6 or abs(float(shape.rotation) % 360) > 1e-6:
                    pending.append(dict(object_id=sid, kind="unsupported_geometry", reason="group_or_rotation_not_resolved"))
                    continue
                com = com_by_id.get(shape.shape_id)
                if com is None:
                    pending.append(dict(object_id=sid, kind="unmatched_native_object"))
                    continue
                frame = native.rect(com.Left, com.Top, com.Width, com.Height)
                regions[sid] = dict(bbox_pt=frame, opacity=native.native_opaque_region(shape, j))
                try:
                    if getattr(shape, "has_table", False):
                        for row in range(1, int(com.Table.Rows.Count) + 1):
                            for column in range(1, int(com.Table.Columns.Count) + 1):
                                cell = com.Table.Cell(row, column).Shape
                                cell_id = sid + f"-r{row}c{column}"
                                cell_box = native.rect(cell.Left, cell.Top, cell.Width, cell.Height)
                                actual = native.measure_text(cell, cell_id, order, cell_box,
                                    cell=dict(row=row, column=column, parent_object_id=sid))
                                if actual is None:
                                    continue
                                actual["untranslated_paragraphs"] = deepcopy(actual["paragraphs"])
                                kept = []
                                tf = cell.TextFrame2
                                for paragraph in actual["paragraphs"]:
                                    info = dict(rotation=float(shape.rotation), group_path=[], shape_frame_bbox_pt=cell_box,
                                        vertical_anchor=int(tf.VerticalAnchor), orientation=int(tf.Orientation),
                                        margins_pt=dict(top=float(tf.MarginTop), bottom=float(tf.MarginBottom)),
                                        textframe2_bbox_pt=paragraph["bbox_pt"])
                                    contract = coord.cell_layout_to_slide(info, proof) if office_version in proof["office_versions"] else {
                                        "status": "unknown", "reason": "Office_version_outside_verified_cell_coordinate_scope"}
                                    cells.append(dict(page_index=index, object_id=cell_id, paragraph_index=paragraph["index"], contract=contract))
                                    if contract["status"] == "measured":
                                        kept.append({**paragraph, "bbox_pt": contract["page_bbox_pt"]})
                                    else:
                                        actual["unresolved"].append(dict(kind="cell_coordinate", reason=contract))
                                actual["paragraphs"] = kept
                                measured[cell_id] = actual
                    elif getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                        actual = native.measure_text(com, sid, order, frame, bool(shape.is_placeholder))
                        if actual:
                            measured[sid] = actual
                except Exception as exc:
                    pending.append(dict(object_id=sid, kind="COM_object_measurement_failed", reason=type(exc).__name__ + ": " + str(exc)))
            com_status = "fresh_measured"
        except Exception as exc:
            com_status = "failed"
            errors.append(dict(page_index=index, error=str(exc)))
            pending.append(dict(kind="COM_page_measurement_unavailable", reason=str(exc)))

        objects = []
        for order, shape in enumerate(py_slide.shapes):
            obj = ingest.object_from_shape(shape, index, order, width, height, measured, regions, j)
            objects.append(obj)
            if getattr(shape, "has_table", False):
                y = float(shape.top) / 12700
                for row, tablerow in enumerate(shape.table.rows, 1):
                    x = float(shape.left) / 12700
                    for column, cell in enumerate(tablerow.cells, 1):
                        cw, ch = float(shape.table.columns[column - 1].width) / 12700, float(tablerow.height) / 12700
                        if cell.text.strip():
                            sid = obj["id"] + f"-r{row}c{column}"
                            actual = measured.get(sid)
                            families, sizes, _ = ingest.explicit_style(cell.text_frame)
                            cell_box = actual["frame_bbox_pt"] if actual else dict(x=x, y=y, w=cw, h=ch)
                            child = {**deepcopy(obj), "id": sid, "parent_id": obj["id"], "kind": "cell",
                                "cell": dict(row=row, column=column), "text": cell.text,
                                "text_chars": len("".join(cell.text.split())), "bbox": ingest.norm(cell_box, width, height),
                                "paragraphs": [] if actual is None else [dict(id=sid + f'-p{p["index"]}',
                                    bbox=ingest.norm(p["bbox_pt"], width, height), font_pt=p["effective_font_pt"],
                                    text_chars=p["text_chars"], line_boxes=None,
                                    measurement_source="live_COM_M_verified_cell_anchor") for p in actual["paragraphs"]],
                                "font_families": families,
                                "font_sizes_pt": sorted(set(sizes + ([] if actual is None else actual["effective_font_sizes_pt"]))),
                                "text_role": "table_text", "role_source": "native_table_cell", "fill_rgb": None,
                                "opacity": dict(state="partial_or_unknown", source="cell_fill_not_resolved"),
                                "explicit_colors": ingest.colors_from_xml(cell._tc), "image_sha256": None,
                                "available": {**obj["available"], "paragraph_layout": "available" if actual and actual["paragraphs"] else "not_measured_or_coordinate_unverified"}}
                            objects.append(child)
                        x += cw
                    y += float(tablerow.height) / 12700
        entry = rendered.get(index, {})
        png = out_dir / entry["file"] if entry.get("status") == "rendered" and entry.get("file") else None
        png_width = png_height = None
        if png:
            with Image.open(png) as image:
                image.load()
                png_width, png_height = image.size
        for obj in objects:
            if obj["paragraphs"] and png_height:
                obj["rendered_text_height_px"] = min(p["bbox"]["h"] * png_height for p in obj["paragraphs"])
            if abs(obj["rotation"]) > 1e-6 or obj["kind"] == "group":
                obj["paragraphs"] = []
                obj["available"]["paragraph_layout"] = "unsupported_transform"
        contexts.append(dict(schema_version="slide-feature-context-v1", sample_id=f"s{index:04d}", document_id="live-upload",
            page_index=index, native_slide_id=int(py_slide.slide_id),
            page=dict(width_pt=width, height_pt=height, aspect=width / height, role="unknown", role_source="not_inferred_yet",
                role_confidence=0., content_type="mixed" if any(o["kind"] == "image" for o in objects) else "native_editable",
                png_path=str(png) if png else None, png_sha256=sha(png) if png else None, png_width=png_width, png_height=png_height),
            source=dict(pptx_path=str(source), pptx_sha256=before), objects=objects, spatial={}, relations={}, document={},
            availability=dict(native="available", COM=com_status, COM_mode="readonly_shared_render_session",
                pixel_glyph_masks="not_implemented", line_boxes="not_measured", ocr="not_installed"), warnings=pending))
    after = sha(source)
    if before != after:
        raise ValueError("Source PPTX changed during readonly measurement")
    output = out_dir / "native_contexts.json"
    output.write_text(json.dumps(contexts, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    report = dict(status="complete" if not errors else "partial", pages=len(contexts), objects=sum(len(c["objects"]) for c in contexts),
        measured_paragraphs=sum(len(o["paragraphs"]) for c in contexts for o in c["objects"]), errors=errors,
        pending_warnings=sum(len(c["warnings"]) for c in contexts), cell_coordinate_contracts=cells,
        source_sha256_before=before, source_sha256_after=after, context_sha256=sha(output),
        native_contexts_path=str(output), Office_version=office_version, shared_render_COM_session=True,
        source_saves=0, label_files_read=0, source_bridge_sha256=sha(Path(__file__)), coordinate_proof_sha256=sha(proof_path))
    (out_dir / "native_measurement_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    return report
