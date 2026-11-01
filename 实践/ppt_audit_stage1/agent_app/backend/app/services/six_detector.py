"""Application adapter for the existing six-class research candidate.

The backend measures uploads in the rendering worker; numpy inference runs in
the existing isolated ML environment. Failure and unassessed scope remain
visible claims. Ordinal detector scores are never named defect probabilities.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any

PROJECT = Path(__file__).resolve().parents[4]
WORKER = Path(__file__).with_name("six_inference_worker.py")
DEFAULT_ML_PYTHON = PROJECT / "ml_stage4/.venv/Scripts/python.exe"
LABELS = {
    "D1": "improper_font_sizing", "D2": "occluded_content", "D3": "cluttered_layout",
    "D4": "content_overflow_cutoff", "D5": "excessive_whitespace", "D6": "generative_design_anomaly",
}
TITLES = {"D1": "字号不合理", "D2": "内容遮挡", "D3": "布局拥挤", "D4": "内容溢出或裁切",
          "D5": "过分留白", "D6": "异常设计风险"}
STATES = {"candidate", "no_candidate_in_available_scope", "unknown", "unsupported", "not_checked"}
VERSION = "six-detector-evidence-routing-r2"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bbox(value) -> dict | None:
    if not isinstance(value, dict) or not all(_finite(value.get(k)) for k in ("x", "y", "w", "h")):
        return None
    return dict(value) if value["w"] > 0 and value["h"] > 0 else None


def _union(boxes) -> dict | None:
    boxes = [b for value in boxes if (b := _bbox(value))]
    if not boxes:
        return None
    x, y = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
    return dict(x=x, y=y, w=max(b["x"] + b["w"] for b in boxes) - x,
                h=max(b["y"] + b["h"] for b in boxes) - y)


def _display_box(box) -> dict | None:
    if not box:
        return None
    x, y = max(0., box["x"]), max(0., box["y"])
    right, bottom = min(1., box["x"] + box["w"]), min(1., box["y"] + box["h"])
    return dict(x=x, y=y, w=right - x, h=bottom - y) if right > x and bottom > y else None


def _strings(values) -> list[str]:
    return list(dict.fromkeys(str(v) for v in (values or []) if v is not None))


class SixDetectorAdapter:
    def __init__(self, enabled: bool = True, timeout_s: int = 300,
                 ml_python: Path | str | None = None) -> None:
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.ml_python = Path(ml_python) if ml_python else DEFAULT_ML_PYTHON

    def fallback(self, document: dict, document_id: str, reason: str,
                 raw_status: str = "not_checked") -> dict:
        if raw_status not in STATES - {"candidate", "no_candidate_in_available_scope"}:
            raise ValueError("Fallback must preserve an unassessed state")
        claims = []
        for index in range(1, int(document.get("slide_count", 0)) + 1):
            for code, label in LABELS.items():
                claims.append(dict(candidate_id=f"six-{document_id}-s{index:04d}-{code}", document_id=document_id,
                    slide_id=f"s{index:04d}", slide_index=index, defect_type=label, subtype=None,
                    detector_code=code, detector_version=VERSION, raw_status=raw_status, score=None,
                    score_kind="ordinal_not_calibrated_probability", object_ids=[], native_bbox=None,
                    normalized_bbox=None, related_slides=[index], evidence=[], coverage="unassessed",
                    limitations=[reason], input_version={}, screening_decision=dict(automatic_risk_candidate=False,
                        review_required=True, critical_review_reasons=[reason], semantic_unresolved=[]),
                    subdetectors={"A05": {"status": "unsupported", "score": None}} if code == "D6" else {}))
        return dict(status=raw_status, claims=claims, issues=[], risks=[dict(code="six_detection_unassessed",
            message=reason, severity="warning", scope="all_six_labels", type="structure_limited",
            title="六类能力范围", detail=reason)], capability=dict(
                version=VERSION, status=raw_status, realtime_upload_inference=False,
                A05_semantic_status="unsupported", LLM_executed=False, zero_miss_guarantee=False), artifacts={})

    def analyze(self, source_path: Path, task_dir: Path, document: dict, detail: dict,
                render_result: dict, document_id: str) -> dict:
        if not self.enabled:
            return self.fallback(document, document_id, "six_detector_disabled", "not_checked")
        source_path, task_dir = Path(source_path).resolve(), Path(task_dir).resolve()
        if not self.ml_python.is_file():
            return self.fallback(document, document_id, "隔离 ML Python 不可用，六类检查未执行。", "unsupported")
        context_path = task_dir / "renders/native_contexts.json"
        native_meta = render_result.get("meta", {}).get("six_measurement", {})
        if not context_path.is_file() or native_meta.get("status") not in {"complete", "partial"}:
            return self.fallback(document, document_id,
                "实时 PowerPoint 排版测量不可用：" + str(native_meta.get("error", native_meta.get("status", "not_checked"))), "unknown")
        try:
            expected_source = _sha(source_path)
            contexts = json.loads(context_path.read_text(encoding="utf-8"))
            if len(contexts) != int(document["slide_count"]):
                raise ValueError("Native feature page count differs from upload")
            if native_meta.get("context_sha256") != _sha(context_path):
                raise ValueError("Native measurement context hash differs")
            if any(c["source"].get("pptx_sha256") != expected_source or
                   Path(c["source"]["pptx_path"]).resolve() != source_path for c in contexts):
                raise ValueError("Feature contexts do not correspond to this uploaded PPTX")
            for context in contexts:
                png_name = context["page"].get("png_path")
                if png_name and not Path(png_name).resolve().is_relative_to(task_dir / "renders"):
                    raise ValueError("Original task render outside task directory")
            out_dir = task_dir / "six_detector"
            out_dir.mkdir(parents=True, exist_ok=True)
            raw_path = out_dir / "raw_detection.json"
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
            completed = subprocess.run([str(self.ml_python), "-B", str(WORKER), "--contexts", str(context_path),
                "--out", str(raw_path), "--document-id", document_id], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_s, env=env,
                creationflags=0x08000000 if os.name == "nt" else 0)
            if not raw_path.is_file():
                raise RuntimeError("六类推理进程未产生结果：" + completed.stderr[-1200:])
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if completed.returncode != 0 or raw.get("status") != "complete":
                raise RuntimeError("六类推理失败：" + str(raw.get("error", completed.stderr[-800:])))
            if _sha(source_path) != expected_source:
                raise ValueError("Uploaded source changed during inference")
            result = self.from_worker(raw, document, document_id)
            result["artifacts"] = {"raw_detection": str(raw_path), "raw_detection_sha256": _sha(raw_path),
                "native_contexts": str(context_path), "native_contexts_sha256": _sha(context_path)}
            result_path = out_dir / "adapter_result.json"
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
            result["artifacts"]["adapter_result"] = str(result_path)
            return result
        except subprocess.TimeoutExpired:
            return self.fallback(document, document_id, f"六类推理超过 {self.timeout_s} 秒，范围仍未判定。", "unknown")
        except Exception as exc:
            return self.fallback(document, document_id, str(exc), "unknown")

    def from_worker(self, raw: dict, document: dict, document_id: str) -> dict:
        pages = raw.get("pages", [])
        expected = set(range(1, int(document.get("slide_count", 0)) + 1))
        actual = [int(page["slide_index"]) for page in pages]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Six detector output does not uniquely cover uploaded pages")
        claims, issues, risks = [], [], []
        for page in pages:
            index, metadata = int(page["slide_index"]), page["page"]
            decision = page.get("screening_route", {})
            for code, label in LABELS.items():
                result = page.get("detectors", {}).get(code)
                if not result:
                    raise ValueError("Missing detector output: " + code)
                status = result.get("status", "unknown")
                if status == "review_candidate":
                    status = "candidate"
                if status not in STATES:
                    raise ValueError("Unsupported detector state: " + str(status))
                evidence = deepcopy(result.get("evidence", []))
                objects = _strings([oid for item in evidence for oid in item.get("object_ids", [])])
                box = _union([result.get("bbox"), *[e.get("bbox") for e in evidence]])
                native = ({"x": box["x"] * metadata["width_pt"] / 72,
                    "y": box["y"] * metadata["height_pt"] / 72,
                    "w": box["w"] * metadata["width_pt"] / 72,
                    "h": box["h"] * metadata["height_pt"] / 72} if box else None)
                features = result.get("features", {})
                components = features.get("recall_components", features.get("subdetectors", {})) if code == "D6" else {}
                related = {index}
                if code == "D6":
                    for peer in components.get("A01", {}).get("features", {}).get("matching_peers", []):
                        if peer.get("page_index") is not None:
                            related.add(int(peer["page_index"]))
                limitations = _strings(result.get("limitations", []))
                if code == "D6":
                    limitations.append("A05 素材语义伪影检测未实现；局部 ROI 只支持进一步复核，不能证明 AI 伪文字。")
                selected = decision.get("decisions", {}).get(code, {})
                claim = dict(candidate_id=f"six-{document_id}-s{index:04d}-{code}", document_id=document_id,
                    slide_id=str(page.get("slide_id", f"s{index:04d}")), slide_index=index, defect_type=label,
                    detector_code=code, subtype=None, detector_version=result.get("version", VERSION), raw_status=status,
                    score=result.get("score") if _finite(result.get("score")) else None,
                    score_kind=result.get("score_kind", "ordinal_not_calibrated_probability"), object_ids=objects,
                    native_bbox=native, coordinate_space="inches", normalized_bbox=_display_box(box), raw_normalized_bbox=box,
                    related_slides=sorted(related), evidence=evidence, coverage=str(result.get("coverage", "partial")),
                    limitations=limitations, input_version=dict(pptx_sha256=raw.get("input_source_sha256"),
                        png_sha256=metadata.get("png_sha256"), context_sha256=raw.get("input_context_sha256"),
                        detector_bundle_sha256=raw.get("candidate_bundle_sha256")), features=features,
                    subdetectors=components, unresolved=deepcopy(result.get("unresolved", [])),
                    screening_decision=deepcopy(selected), global_review_reasons=decision.get("global_review_reasons", []),
                    material_review=deepcopy(page.get("material", {})) if code == "D6" else None)
                claims.append(claim)
                if selected.get("automatic_risk_candidate"):
                    subtypes = [k for k, v in components.items() if k != "A05" and v.get("status") in {"candidate", "review_candidate"}]
                    title = TITLES[code] + "候选"
                    issues.append(dict(issue_id=claim["candidate_id"], candidate_id=claim["candidate_id"],
                        document_id=document_id, slide_index=index, defect_type=label, title=title,
                        evidence_source="six_detector", rule_id=code, rule_name=title,
                        evidence_summary=f"{code} 在可判范围发现风险候选；需要结合用途、原始页面和定位证据复核。",
                        element_id=objects[0] if objects else None, bbox=native, coordinate_space="inches",
                        visual_probability=None, severity="medium", confidence="medium", review_status="unreviewed",
                        review_result=None, recommendation="先检查定位对象及原始页面，结合使用场景确认后给出调整建议。",
                        execution_status="candidate", detector_claim_ids=[claim["candidate_id"]],
                        related_slides=sorted(related), normalized_bbox=claim["normalized_bbox"],
                        details=dict(detector_claim=claim, subtypes=subtypes, ordinal_risk_score=claim["score"],
                            score_kind=claim["score_kind"], calibrated_probability=False)))
                if status in {"unknown", "unsupported", "not_checked"} or selected.get("critical_review_reasons"):
                    risks.append(dict(code="six_scope_" + code, slide_index=index, defect_type=label,
                        candidate_id=claim["candidate_id"], status=status, message=f"{TITLES[code]}存在未判定范围。",
                        reasons=selected.get("critical_review_reasons", []), severity="warning", type="structure_limited",
                        title=f"{TITLES[code]}的能力范围", detail=f"第{index}页：" + "；".join(
                            selected.get("critical_review_reasons", []) or limitations or ["现有证据不能确认全部范围正常。"])))
        return dict(status="complete", claims=claims, issues=issues, risks=risks,
            capability=dict(version=VERSION, status="available", realtime_upload_inference=True,
                pages=len(pages), source="uploaded_PPTX_plus_shared_live_COM_layout_and_original_render",
                A05_semantic_status="unsupported", classifier_fits=0, LLM_executed=False,
                score_semantics="ordinal_not_calibrated_probability", zero_miss_guarantee=False,
                frozen_thresholds=raw.get("frozen_thresholds", {}), source_sha256=raw.get("source_sha256", {}),
                input_context_sha256=raw.get("input_context_sha256"), runtime=raw.get("runtime", {})), artifacts={})
