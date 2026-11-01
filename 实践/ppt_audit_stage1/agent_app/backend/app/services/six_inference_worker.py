"""Isolated numpy-runtime inference over one freshly measured upload.

This calls only the frozen, pure detector functions; no experiment runner,
benchmark manifest, reference labels, historical predictions or CLIP weights.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import traceback

PROJECT = Path(__file__).resolve().parents[4]
X = PROJECT / "ml_stage4/experiments"
L = X / "stage4l_six_detectors_20261029"
M = X / "stage4m_role_coordinate_graph_20261029"
V3 = X / "stage4m_rule_optimization_20261029"
ALL = X / "recall_allsets_20261030"
LAST = X / "d5d6_recall_diagnostics_20261030"
LOCAL = X / "sixmix_rule_diagnostics_20261030"
R2 = X / "recall_precision_refinement_20261030"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def write(path, value):
    import numpy as np
    def convert(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
        raise TypeError(type(obj).__name__)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False, default=convert), encoding="utf-8")


def run(context_path: Path, output: Path, document_id: str):
    import numpy as np
    started = time.perf_counter()
    bundle = read(R2 / "candidate_bundle.json")
    # Verify the actual reused implementation/configuration files, without
    # loading any historical result artifacts or benchmark data.
    paths = [L / "detector_implementation/specialized.py", L / "config/specialized.json",
        L / "config/shared_features.json", M / "implementation/pipeline.py", M / "config/roles.json",
        V3 / "implementation/context_refinement.py", LOCAL / "implementation/local_detectors.py",
        ALL / "implementation/d5_detector.py", ALL / "implementation/material_evidence.py",
        LAST / "implementation/d6_recall.py", R2 / "implementation/evidence_refinement.py",
        R2 / "implementation/evidence_refinement_v2.py", R2 / "implementation/routing.py", R2 / "implementation/routing_v2.py"]
    expected = {**bundle.get("read_only_dependency_sha256", {}), **bundle.get("source_sha256", {})}
    source_versions = {str(p): sha(p) for p in paths}
    for name, actual in source_versions.items():
        if name in expected and expected[name] != actual:
            raise ValueError("Frozen detector source SHA mismatch: " + name)
    contexts = read(context_path)
    if not contexts or len({c["sample_id"] for c in contexts}) != len(contexts):
        raise ValueError("Fresh contexts must contain unique pages")
    input_sha = sha(context_path)
    input_files = {}
    unavailable_renders = {}
    for context in contexts:
        context["document_id"] = document_id
        source = context["source"]
        page = context["page"]
        if not page.get("png_path") or not Path(page["png_path"]).is_file() or not page.get("png_sha256"):
            unavailable_renders[context["sample_id"]] = "original_page_render_unavailable"
        pairs = [(source["pptx_path"], source["pptx_sha256"])]
        if context["sample_id"] not in unavailable_renders:
            pairs.append((page["png_path"], page["png_sha256"]))
        for name, expected_sha in pairs:
            if not name or not expected_sha:
                raise ValueError("Actual uploaded source and original render identity required")
            if name not in input_files:
                input_files[name] = sha(name)
            if input_files[name] != expected_sha:
                raise ValueError("Live inference input SHA mismatch")
    pipeline = module("app_six_M_pipeline", M / "implementation/pipeline.py")
    pipeline.GATE.validate_contexts(contexts, require_spatial=False)
    shared_config = read(L / "config/shared_features.json")
    role_config = read(M / "config/roles.json")
    pipeline.SHARED.classify_document(contexts, shared_config)
    roles = pipeline.ROLE.infer_roles(contexts, role_config)
    originals = pipeline.ROLE.apply_role_updates(contexts, roles)
    for ctx in originals:
        for obj in ctx["objects"]:
            if obj["kind"] in {"text", "cell"}:
                obj["content_regions"] = pipeline.SHARED.text_boxes(obj) if obj["content_class"] == "information" else []
        if ctx["sample_id"] not in unavailable_renders:
            pipeline.SHARED.build_spatial(ctx, shared_config)
        else:
            ctx["availability"]["original_render"] = "unavailable"
    refine = module("app_six_native_refinement", V3 / "implementation/context_refinement.py")
    refined, refinement_audit = refine.refine_contexts(originals)
    specialized = module("app_six_special", L / "detector_implementation/specialized.py")
    local = module("app_six_local", LOCAL / "implementation/local_detectors.py")
    d5 = module("app_six_D5", ALL / "implementation/d5_detector.py")
    d6 = module("app_six_D6", LAST / "implementation/d6_recall.py")
    pixels = module("app_six_material", ALL / "implementation/material_evidence.py")
    evidence = module("app_six_evidence_R2", R2 / "implementation/evidence_refinement_v2.py")
    routing = module("app_six_routing_R2", R2 / "implementation/routing_v2.py")
    priors = d6.V3.GenerativeDesignAnomalyDetector().detect_document(refined)["page_results"]
    cfg = read(L / "config/specialized.json")
    original_map = {c["sample_id"]: c for c in originals}
    pages = []
    for ctx in refined:
        sid = ctx["sample_id"]
        original = original_map[sid]
        if sid in unavailable_renders:
            # Preserve the page, its actual native measurements and its place in
            # document context; only this page's unrendered detector scope is unknown.
            missing = unavailable_renders[sid]
            detectors = {code: dict(detector=code, version="live-adapter-scope-guard-v1", status="unknown", score=None,
                score_kind="ordinal_not_calibrated_probability", evidence=[], features={}, coverage="unassessed",
                unresolved=[dict(kind=missing)], limitations=["原始完整页面 PNG 未成功导出，未执行本页六类检测。"])
                for code in routing.LABELS}
            decision = routing.route(ctx, detectors, None)
            pages.append(dict(sample_id=sid, document_id=document_id,
                slide_id=str(ctx.get("native_slide_id", ctx["page_index"])), slide_index=ctx["page_index"],
                page=ctx["page"], detectors=detectors, screening_route=decision,
                material=dict(status="unknown", semantic_artifact_status="unsupported", reason=missing),
                background_audit={}, measurement_availability=ctx["availability"], measurement_warnings=ctx.get("warnings", [])))
            continue
        detectors = specialized.detect_specialized(original, cfg)
        detectors.update(local.detect_local(original, previous_results=detectors))
        detectors["D2"] = evidence.refine_occlusion(original, detectors["D2"])
        detectors["D3"] = evidence.refine_clutter(original, detectors["D3"])
        detectors["D5"] = d5.propose(ctx)
        detectors["D6"] = evidence.refine_decorations(ctx, d6.propose(ctx, priors[sid], refined))
        material = pixels.analyze_page(ctx)
        background, background_audit = evidence.background_variant(ctx, refined)
        if background_audit["background_ids"]:
            refine._modules(background)
            refine._spatial(background)
            alternative = d5.propose(background)
            if alternative.get("score") is not None and alternative["score"] > float(detectors["D5"].get("score") or 0):
                detectors["D5"] = alternative
                detectors["D5"]["features"]["background_hypothesis_used"] = background_audit
        decision = routing.route(ctx, detectors, material)
        pages.append(dict(sample_id=sid, document_id=document_id, slide_id=str(ctx.get("native_slide_id", ctx["page_index"])),
            slide_index=ctx["page_index"], page=ctx["page"], detectors=detectors, screening_route=decision,
            material=material, background_audit=background_audit, measurement_availability=ctx["availability"],
            measurement_warnings=ctx.get("warnings", [])))
    changed = [p for p, digest in input_files.items() if sha(p) != digest]
    changed_sources = [p for p, digest in source_versions.items() if sha(p) != digest]
    if changed or changed_sources or sha(context_path) != input_sha:
        raise ValueError("Read-only inference integrity failure")
    result = dict(status="complete", version="six-detector-evidence-routing-r2", pages=pages,
        created_at=datetime.now().astimezone().isoformat(), runtime=dict(python=sys.version, executable=sys.executable,
            numpy=np.__version__, seconds=time.perf_counter() - started),
        source_sha256=source_versions, input_context_sha256=input_sha,
        input_source_sha256=contexts[0]["source"]["pptx_sha256"],
        input_artifact_sha256=input_files, candidate_bundle_sha256=sha(R2 / "candidate_bundle.json"),
        refinement_audit=refinement_audit, frozen_thresholds=dict(routing.THRESHOLDS),
        labels_used_for_features=False, benchmark_files_read=0, classifier_fits=0, CLIP_forwards=0, LLM_calls=0,
        A05_semantic_status="unsupported", sources_unchanged=True)
    result["unavailable_render_pages"] = sorted(unavailable_renders)
    write(output, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contexts", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--document-id", required=True)
    args = parser.parse_args()
    try:
        run(Path(args.contexts), Path(args.out), args.document_id)
        return 0
    except Exception as exc:
        Path(args.out).write_text(json.dumps(dict(status="failed", error=str(exc), error_type=type(exc).__name__,
            traceback=traceback.format_exc()[-6000:]), ensure_ascii=False, indent=1), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
