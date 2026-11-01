"""AI repair API: one record per run, files kept beside the task.

The endpoints only orchestrate; the edits happen in ``services.repair`` on a
copy of the source deck.  Nothing here rewrites the task's own report or the
original upload.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..services import repair as repair_service
from ..storage import settings_store, store_for
from .common import ID_RE, ensure_local, get_task, task_dir

router = APIRouter(prefix="/api", tags=["repair"])

Mode = str


def _task(task_id: str, mode: str) -> dict:
    return get_task(task_id, mode)


def _report_payload(directory: Path) -> dict:
    path = directory / "report" / "report.json"
    if not path.is_file():
        raise HTTPException(status_code=409, detail={
            "code": "report_missing", "message": "任务还没有生成报告，无法修复。"})
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=409, detail={
            "code": "report_unreadable", "message": "报告读取失败，请重新生成报告后再修复。"})


@router.get("/repairs")
def list_repair_projects(mode: str = Query("real")):
    """Every completed task with its newest repair record, for the AI repair home."""
    store = store_for("demo" if mode == "demo" else "real")
    items = []
    for task in store.list(limit=100, offset=0):
        if task["status"] not in ("completed", "completed_with_errors"):
            continue
        directory = task_dir(task)
        latest = repair_service.latest(directory)
        items.append({
            "task": {
                "id": task["id"], "name": task["name"], "status": task["status"],
                "slide_count": task.get("slide_count"), "mode": task.get("mode"),
                "aspect_label": (task.get("document") or {}).get("aspect_label"),
                "finished_at": task.get("finished_at"), "created_at": task.get("created_at"),
                "summary": task.get("summary"),
            },
            "repair": latest and {
                "repair_id": latest["repair_id"], "status": latest["status"],
                "progress": latest.get("progress"), "summary": latest.get("summary"),
                "verify": latest.get("verify"), "created_at": latest.get("created_at"),
                "finished_at": latest.get("finished_at"),
            },
        })
    return {"items": items}


@router.get("/tasks/{task_id}/repairs")
def list_task_repairs(task_id: str, mode: str = Query("real")):
    task = _task(task_id, mode)
    directory = task_dir(task)
    return {"items": [repair_service._public(item) for item in repair_service.list_repairs(directory)]}


@router.get("/tasks/{task_id}/repairs/{repair_id}")
def get_repair(task_id: str, repair_id: str, mode: str = Query("real")):
    task = _task(task_id, mode)
    directory = task_dir(task)
    status = repair_service.load_status(directory, repair_id)
    if not status:
        raise HTTPException(status_code=404, detail={"code": "repair_not_found", "message": "修复记录不存在。"})
    return repair_service._public(status)


@router.post("/tasks/{task_id}/repairs")
def start_repair(task_id: str, request: Request, mode: str = Query("real"),
                 backend: str = Query("api"), review: int = Query(0),
                 rounds: int = Query(0)):
    """Kick off a repair run; returns the record immediately.

    ``review=1`` turns on the multi-pass mode: each pass re-checks the deck and
    plans another round from whatever still fires.  ``rounds`` caps the passes
    (defaults to ``repair.REVIEW_MAX_ROUNDS``), because every extra round is
    another model call.
    """
    ensure_local(request)
    task = _task(task_id, mode)
    if task["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=409, detail={
            "code": "task_not_completed", "message": "任务还没有完成审查，无法修复。"})
    directory = task_dir(task)
    running = [item for item in repair_service.list_repairs(directory)
               if item.get("status") in ("queued", "running")]
    if running:
        return repair_service._public(running[0])
    payload = _report_payload(directory)
    settings = settings_store.all()
    api_key = settings_store.get_api_key() if backend != "rule" else ""
    status = repair_service.start(directory, payload, settings, api_key,
                                  backend="api" if backend != "rule" else "rule",
                                  review=bool(review),
                                  max_rounds=rounds or repair_service.REVIEW_MAX_ROUNDS)
    return repair_service._public(status)


@router.get("/tasks/{task_id}/repairs/{repair_id}/file")
def download_repair(task_id: str, repair_id: str, mode: str = Query("real")):
    task = _task(task_id, mode)
    directory = task_dir(task)
    if not ID_RE.match(task_id) or not repair_id.startswith("r-"):
        raise HTTPException(status_code=404, detail={"code": "repair_not_found", "message": "修复记录不存在。"})
    path = repair_service.repairs_root(directory) / repair_id / "fixed.pptx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "repair_file_missing", "message": "修复文件尚未生成。"})
    name = (Path((task.get("document") or {}).get("file_name") or "slides.pptx").stem) + "_已修复.pptx"
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        filename=name)


@router.get("/tasks/{task_id}/repair-hints")
def get_repair_hints(task_id: str, mode: str = Query("real")):
    """Human notes per page, shared by every repair run of this task."""
    task = _task(task_id, mode)
    return {"hints": repair_service.load_hints(task_dir(task))}


@router.put("/tasks/{task_id}/repair-hints")
async def put_repair_hint(task_id: str, request: Request, mode: str = Query("real")):
    ensure_local(request)
    task = _task(task_id, mode)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_body", "message": "请求体不是合法 JSON。"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_body", "message": "请求体必须是对象。"})
    try:
        slide = int(body.get("slide_index") or 0)
    except (TypeError, ValueError):
        slide = 0
    if not 1 <= slide <= 999:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_slide", "message": "页码不正确。"})
    hints = repair_service.save_hint(task_dir(task), slide, str(body.get("text") or ""))
    return {"hints": hints}


@router.get("/tasks/{task_id}/repairs/{repair_id}/slides/{slide_index}/annot")
def repair_slide_annot(task_id: str, repair_id: str, slide_index: int, mode: str = Query("real")):
    """The original render with numbered finding boxes, drawn once per run."""
    task = _task(task_id, mode)
    directory = task_dir(task)
    if not 1 <= slide_index <= 999 or not repair_id.startswith("r-"):
        raise HTTPException(status_code=404, detail={"code": "slide_not_found", "message": "页码超出范围。"})
    path = repair_service.repairs_root(directory) / repair_id / "annots" / f"s{slide_index:04d}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "annot_missing", "message": "该页没有生成标注图。"})
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@router.get("/tasks/{task_id}/repairs/{repair_id}/brief/{slide_index}")
def repair_brief(task_id: str, repair_id: str, slide_index: int, mode: str = Query("real")):
    """One page's copy-paste prompt: findings, coordinates, what was applied."""
    task = _task(task_id, mode)
    directory = task_dir(task)
    status = repair_service.load_status(directory, repair_id)
    if not status:
        raise HTTPException(status_code=404, detail={"code": "repair_not_found", "message": "修复记录不存在。"})
    if not 1 <= slide_index <= 999:
        raise HTTPException(status_code=400, detail={"code": "invalid_slide", "message": "页码不正确。"})
    payload = _report_payload(directory)
    return repair_service.page_brief(payload, slide_index, status.get("ops") or [],
                                     repair_service.load_hints(directory),
                                     repair_service.page_layout(
                                         directory / "source" / "source.pptx", slide_index))


@router.get("/tasks/{task_id}/repairs/{repair_id}/bundle")
def download_bundle(task_id: str, repair_id: str, mode: str = Query("real")):
    """Zip: source deck + annotated pages + prompts + fixed copy."""
    task = _task(task_id, mode)
    directory = task_dir(task)
    status = repair_service.load_status(directory, repair_id)
    if not status:
        raise HTTPException(status_code=404, detail={"code": "repair_not_found", "message": "修复记录不存在。"})
    payload = _report_payload(directory)
    path = repair_service.build_bundle(directory, repair_id, payload, status.get("ops") or [])
    stem = Path((task.get("document") or {}).get("file_name") or "slides.pptx").stem
    return FileResponse(path, media_type="application/zip", filename=f"{stem}_修改任务包.zip")


@router.get("/tasks/{task_id}/repairs/{repair_id}/slides/{slide_index}/image")
def repair_slide_image(task_id: str, repair_id: str, slide_index: int, mode: str = Query("real")):
    task = _task(task_id, mode)
    directory = task_dir(task)
    if not 1 <= slide_index <= 999 or not repair_id.startswith("r-"):
        raise HTTPException(status_code=404, detail={"code": "slide_not_found", "message": "页码超出范围。"})
    path = repair_service.repairs_root(directory) / repair_id / "renders" / f"s{slide_index:04d}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "slide_not_found", "message": "该页没有修复后的渲染图。"})
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})
