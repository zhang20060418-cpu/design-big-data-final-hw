"""System capabilities, module status and health."""
from __future__ import annotations

from datetime import datetime
import json

from fastapi import APIRouter

from ..config import (APP_DIR, DEFECT_LABELS_ZH, DEFECT_TYPES, MAX_SLIDES, MAX_UPLOAD_BYTES,
                      RENDER_LONG_EDGE, frontend_build_id, settings)
from ..services import orchestrator
from ..storage import demo_store, real_store, settings_store
from .demo import SCENARIOS

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health():
    return {"ok": True, "time": datetime.now().astimezone().isoformat(timespec="seconds")}


@router.get("/capabilities")
def capabilities():
    app_settings = settings_store.all()
    visual = orchestrator.classifier_status()
    key_set = bool(settings_store.get_api_key())
    mllm_state = "ready" if (key_set and app_settings.get("api_base_url")
                            and app_settings.get("api_model")) else "not_configured"
    from ..services.six_detector import DEFAULT_ML_PYTHON, PROJECT, VERSION
    try:
        pins = json.loads((APP_DIR / "models/six_runtime_manifest.json").read_text(encoding="utf-8"))
        root = (PROJECT / "detector_runtime").resolve()
        assets = [(root / name).resolve() for name in pins["assets_sha256"]]
        six_dependencies = DEFAULT_ML_PYTHON.is_file() and bool(assets) and all(
            path.is_relative_to(root) and path.is_file() for path in assets)
    except (OSError, ValueError, KeyError, TypeError):
        six_dependencies = False
    return {
        "app": {
            "name": settings.app_name,
            "subtitle": settings.app_subtitle,
            "version": settings.version,
            "build": frontend_build_id(),
        },
        "modules": {
            "six_detector": {
                "state": "ready" if six_dependencies else "unsupported",
                "name": "六类专用检测",
                "version": VERSION,
                "selection_role": "primary_candidate_screening",
                "detail": "使用上传原稿、原始PNG和同次只读COM排版测量；实际可判范围以每页Claim为准。",
                "requires": ["Windows PowerPoint", "既有隔离ML环境", "冻结R2研究实现"],
                "A05_semantic_status": "unsupported",
                "zero_miss_guarantee": False,
                "score_semantics": "ordinal_not_calibrated_probability",
            },
            "structure": {
                "state": "ready",
                "name": "结构规则引擎",
                "detail": "对象越界、文本溢出、字号、遮挡与密度候选，全部给出可解释证据。",
                "rules": ["R01 对象越界", "R02 文本溢出", "R03 字号偏小", "R04 密度偏高", "R05 遮挡候选"],
            },
            "visual": {**visual, "name": "历史四标签 CLIP（可选）"},
            "mllm": {
                "state": mllm_state,
                "name": "多模态按需复核",
                "model": app_settings.get("api_model") or None,
                "base_url_set": bool(app_settings.get("api_base_url")),
                "key_set": key_set,
                "vision_verified": bool(app_settings.get("vision_verified")),
                "max_calls_per_task": app_settings.get("max_calls_per_task"),
                "detail": ("已配置，新任务默认复检；首轮完成后等待5秒，可关闭或跳过。" if mllm_state == "ready"
                           else "尚未完成 API 配置；不配置也可以使用其他检查。"),
            },
        },
        "labels": [{"key": k, "label": DEFECT_LABELS_ZH[k]} for k in DEFECT_TYPES],
        "limits": {
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "max_slides": MAX_SLIDES,
            "render_long_edge": RENDER_LONG_EDGE,
        },
        "demo": {
            "available": True,
            "scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()],
            "task_count": demo_store.count(),
        },
        "task_count": real_store.count(),
        "privacy": "未配置API时不外发；配置完整后新任务默认LLM复检，首轮完成后等待5秒，随后向所配置的API发送选中的页面图与结构/CLIP证据。可在配置关闭，或在倒计时内跳过复检。",
    }
