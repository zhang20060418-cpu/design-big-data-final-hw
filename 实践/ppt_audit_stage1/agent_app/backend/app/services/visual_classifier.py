"""Layer 2 — visual quality classifier integration point.

The optional application plugin uses local frozen assets. Loading capability
metadata never runs image inference; a task must explicitly enable this layer.

Integration contract
--------------------
Place a manifest at ``backend/models/visual_model.json``::

    {
      "adapter": "python_module",
      "module": "visual_model_impl",          # importable from backend/models
      "callable": "predict_slide",            # predict_slide(png_path, config) -> dict
      "model_version": "g2-final-2026xxxx",
      "work_point": "thresholds from 4.3G-2",
      "labels": ["improper_font_sizing", "occluded_content",
                 "cluttered_layout", "content_overflow_cutoff"],
      "thresholds": {"improper_font_sizing": 0.42, "...": 0.5},
      "input_recipe": "e.g. full-slide PNG, long edge 1920, RGB"
    }

The callable must return either ``{"probabilities": {...}, "alerts": {...}}`` or
a flat ``{label: probability}`` mapping.  Anything else is rejected.
"""
from __future__ import annotations

import importlib.util
import json
import time
import traceback
from pathlib import Path
from typing import Any

from ..config import APP_DIR, DEFECT_TYPES

MODEL_DIR = APP_DIR / "models"
MANIFEST_PATH = MODEL_DIR / "visual_model.json"


class VisualModelNotConfigured(RuntimeError):
    pass


class VisualModelUnavailable(RuntimeError):
    pass


class VisualClassifierService:
    def __init__(self, manifest_path: Path | None = None) -> None:
        self.manifest_path = Path(manifest_path or MANIFEST_PATH)
        self._impl: Any = None
        self._batch_impl: Any = None
        self._manifest: dict[str, Any] | None = None
        self._load_error: str | None = None
        self._loaded = False
        self._availability: dict = {}

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        self._load()
        if self._load_error:
            return {
                "state": "error",
                "available": False,
                "reason": self._load_error,
                "labels": DEFECT_TYPES[:4],
                "model_version": None,
                "work_point": None,
                "input_recipe": None,
                "integration_hint": "检查 backend/models/visual_model.json 与实现模块。",
            }
        if not self._manifest:
            return {
                "state": "not_configured",
                "available": False,
                "reason": "历史四标签 CLIP 分类器未启用；六类专用检测可独立运行，具体结果请查看该任务的检查范围。",
                "labels": DEFECT_TYPES[:4],
                "model_version": None,
                "work_point": None,
                "input_recipe": None,
                "integration_hint": "保留为可选辅助接口；确认有额外补漏收益后再提供 visual_model.json 与对应实现。"
                                    "当前六类候选由专用检测器产生，不依赖此历史 CLIP 插件。",
            }
        return {
            "state": "ready",
            "available": True,
            "reason": "",
            "labels": self._manifest.get("labels", DEFECT_TYPES[:4]),
            "model_version": self._manifest.get("model_version"),
            "work_point": self._manifest.get("work_point"),
            "input_recipe": self._manifest.get("input_recipe"),
            "thresholds": self._manifest.get("thresholds", {}),
            "model_revision": self._manifest.get("model_revision") or self._availability.get('clip_revision'),
            "limitations": self._manifest.get("limitations", []),
            "default_enabled": False,
            "integration_hint": "",
        }

    # -------------------------------------------------------------------- load
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.manifest_path.exists():
            return
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            adapter = manifest.get("adapter", "python_module")
            if adapter != "python_module":
                raise ValueError(f"不支持的 adapter：{adapter}")
            module_name = manifest["module"]
            callable_name = manifest.get("callable", "predict_slide")
            module_path = MODEL_DIR / f"{module_name}.py"
            if not module_path.exists():
                raise FileNotFoundError(f"实现模块不存在：{module_path.name}")
            spec = importlib.util.spec_from_file_location(f"agent_visual_{module_name}", module_path)
            if spec is None or spec.loader is None:
                raise ImportError("无法加载视觉模型模块")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fn = getattr(module, callable_name, None)
            if not callable(fn):
                raise AttributeError(f"模块中没有可调用对象 {callable_name}")
            self._impl = fn
            batch_name = manifest.get("batch_callable")
            self._batch_impl = getattr(module, batch_name, None) if batch_name else None
            if batch_name and not callable(self._batch_impl):
                raise ValueError("视觉模型批量入口不可调用")
            check = getattr(module, "check_available", None)
            if callable(check):
                available = check()
                self._availability = available if isinstance(available,dict) else {}
                if isinstance(available, dict) and available.get("available") is False:
                    raise RuntimeError(available.get("reason") or "冻结模型依赖不可用")
            self._manifest = manifest
        except Exception as exc:
            self._load_error = f"视觉模型加载失败：{exc}"
            self._manifest = None
            self._impl = None
            self._batch_impl = None

    # ----------------------------------------------------------------- predict
    def predict_slide(self, png_path: Path | str, document_id: str, slide_index: int,
                      config: dict | None = None) -> dict[str, Any]:
        self._load()
        if self._load_error:
            raise VisualModelUnavailable(self._load_error)
        if not self._manifest or not self._impl:
            raise VisualModelNotConfigured("视觉分类模型尚未接入。")
        started = time.perf_counter()
        raw = self._impl(Path(png_path), {
            "document_id": document_id,
            "slide_index": slide_index,
            "labels": self._manifest.get("labels", DEFECT_TYPES[:4]),
            **(config or {}),
        })
        latency = (time.perf_counter() - started) * 1000
        return self._result(raw, document_id, slide_index, latency)

    def _result(self, raw: dict, document_id: str, slide_index: int, latency: float) -> dict:
        probs, alerts = self._normalise(raw)
        return {
            "status": "ok",
            "document_id": document_id,
            "slide_index": slide_index,
            "probabilities": probs,
            "alerts": alerts,
            "model_version": self._manifest.get("model_version"),
            "work_point": self._manifest.get("work_point"),
            "input_recipe": self._manifest.get("input_recipe"),
            "thresholds": (raw.get("thresholds") or self._manifest.get("thresholds", {})),
            "model_revision": raw.get("model_revision") or raw.get('clip_revision') or self._manifest.get("model_revision"),
            "model_bundle_sha256": raw.get('model_bundle_sha256'),
            "work_points": raw.get('work_points',{}),
            "input_png_sha256": raw.get('input_png_sha256'),
            "device": raw.get('device'),
            "feature_dimension": raw.get('feature_dimension'),
            "limitations": raw.get("limitations") or self._manifest.get("limitations", []),
            "runtime": raw.get("runtime"),
            "artifacts": raw.get("artifacts", {}),
            "scope": "page",
            "latency_ms": round(latency, 1),
            "error": None,
        }

    def predict_batch(self, pages: list[dict], config: dict | None = None) -> list[dict]:
        self._load()
        if self._batch_impl and self._manifest:
            started = time.perf_counter()
            normalized = [{"png_path": Path(p.get("png_path") or p["png"]),
                           "document_id": p["document_id"], "slide_index": int(p["slide_index"])} for p in pages]
            try:
                raw = self._batch_impl(normalized, {**self._manifest, **(config or {})})
                if not isinstance(raw, list) or len(raw) != len(normalized):
                    raise ValueError("CLIP批结果数量与输入页面不一致")
                indices = [int(r.get("slide_index", 0)) for r in raw]
                if len(set(indices)) != len(indices) or set(indices) != {p['slide_index'] for p in normalized}:
                    raise ValueError("CLIP批结果页面ID缺失、重复或不匹配")
                by_page = {int(r['slide_index']): r for r in raw}
                elapsed = (time.perf_counter() - started) * 1000
                results = []
                for page in normalized:
                    item = by_page[page['slide_index']]
                    if item.get('document_id', page['document_id']) != page['document_id']:
                        raise ValueError("CLIP批结果混入其他文稿ID")
                    if item.get('status', 'ok') != 'ok':
                        results.append({**item, 'document_id': page['document_id'], 'slide_index': page['slide_index'],
                                        'probabilities': {}, 'alerts': {}})
                    else:
                        results.append(self._result(item, page['document_id'], page['slide_index'], elapsed / max(1,len(pages))))
                return results
            except Exception as exc:
                return [{"status": "error", "document_id": p['document_id'], "slide_index": p['slide_index'],
                         "probabilities": {}, "alerts": {}, "error": str(exc)} for p in normalized]
        results = []
        for page in pages:
            try:
                results.append(self.predict_slide(page.get("png_path") or page["png"], page["document_id"],
                                                  page["slide_index"], config))
            except (VisualModelNotConfigured, VisualModelUnavailable) as exc:
                results.append({
                    "status": "unavailable", "slide_index": page.get("slide_index"),
                    "probabilities": {}, "alerts": {}, "error": str(exc),
                    "model_version": None, "work_point": None,
                })
            except Exception as exc:  # model bug must not kill the task
                results.append({
                    "status": "error", "slide_index": page.get("slide_index"),
                    "probabilities": {}, "alerts": {}, "error": str(exc),
                    "traceback": traceback.format_exc()[-1200:],
                    "model_version": self._manifest.get("model_version") if self._manifest else None,
                    "work_point": None,
                })
        return results

    def _normalise(self, raw: Any) -> tuple[dict[str, float], dict[str, int | None]]:
        thresholds = (self._manifest or {}).get("thresholds", {})
        if isinstance(raw, dict) and "probabilities" in raw:
            probs_raw = raw["probabilities"]
            alerts_raw = raw.get("alerts") or {}
        elif isinstance(raw, dict):
            probs_raw, alerts_raw = raw, {}
        else:
            raise ValueError("视觉模型返回值必须是字典。")
        probs: dict[str, float] = {}
        alerts: dict[str, int | None] = {}
        labels = (self._manifest or {}).get("labels", DEFECT_TYPES[:4])
        if not labels or any(label not in DEFECT_TYPES for label in labels):
            raise ValueError("视觉模型声明了无效标签范围。")
        for label in labels:
            if label not in probs_raw:
                raise ValueError(f"视觉模型输出缺少标签：{label}")
            try:
                prob = float(probs_raw[label])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"标签 {label} 的概率不是数值。") from exc
            if not 0.0 <= prob <= 1.0:
                raise ValueError(f"标签 {label} 的概率越界：{prob}")
            probs[label] = prob
            if label in alerts_raw:
                alerts[label] = int(alerts_raw[label])
            elif label in thresholds:
                alerts[label] = int(prob >= float(thresholds[label]))
            else:
                alerts[label] = None
        return probs, alerts
