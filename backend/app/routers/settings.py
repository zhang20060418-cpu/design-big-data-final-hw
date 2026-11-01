"""API configuration endpoints.  Local-only, key never returned."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import ConnectionTestRequest, SettingsUpdate
from ..services import mllm
from ..storage import now_iso, settings_store
from .common import ensure_local

router = APIRouter(prefix="/api/settings", tags=["settings"])

PRIVATE_FIELDS = {"api_key"}


def public_settings() -> dict:
    values = {k: v for k, v in settings_store.all().items() if k not in PRIVATE_FIELDS}
    values["api_key_set"] = bool(settings_store.get_api_key())
    values["api_key_masked"] = settings_store.masked_key()
    return values


@router.get("")
def get_settings(request: Request):
    ensure_local(request)
    return public_settings()


@router.put("")
def update_settings(payload: SettingsUpdate, request: Request):
    ensure_local(request)
    previous = settings_store.all()
    data = payload.model_dump(exclude={"api_key"})
    base = data.get("api_base_url", "").strip()
    if base:
        try:
            data["api_base_url"] = mllm.validate_base_url(
                base,
                bool(data.get("allow_private_base_url")),
                bool(data.get("allow_insecure_base_url")),
            )
        except mllm.ApiConfigError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": exc.message})
    else:
        data["api_base_url"] = ""
    if payload.api_key is not None:
        settings_store.set_api_key(payload.api_key.strip())
    changed = (previous.get("api_base_url") != data.get("api_base_url")
               or previous.get("api_model") != data.get("api_model"))
    if changed:
        data["vision_verified"] = False
    settings_store.save(data)
    return public_settings()


@router.post("/test-connection")
def test_connection(payload: ConnectionTestRequest, request: Request):
    ensure_local(request)
    values = settings_store.all()
    key = (payload.api_key or settings_store.get_api_key() or "").strip()
    if not values.get("api_base_url"):
        raise HTTPException(400, detail={"code": "not_configured", "message": "请先保存 API Base URL。"})
    if not values.get("api_model"):
        raise HTTPException(400, detail={"code": "not_configured", "message": "请先保存模型名称。"})
    result = mllm.test_connection(
        values["api_base_url"], key, values["api_model"],
        float(values.get("api_timeout_s") or 60), payload.mode,
        bool(values.get("allow_private_base_url")),
        bool(values.get("allow_insecure_base_url")),
    )
    summary = {k: v for k, v in result.items() if k not in ("sample",)}
    summary["mode"] = payload.mode
    summary["tested_at"] = now_iso()
    summary["model"] = values["api_model"]
    settings_store.save({"last_test": summary})
    if payload.mode == "vision" and result["status"] == "ok":
        settings_store.save({"vision_verified": True})
    return {"result": summary, "settings": public_settings()}


@router.delete("/api-key")
def clear_key(request: Request):
    ensure_local(request)
    settings_store.set_api_key("")
    return public_settings()
