"""Task lifecycle API (real and demo cohorts share the read endpoints)."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..config import DATA_DIR, DEFECT_LABELS_ZH, MAX_SLIDES, MAX_UPLOAD_BYTES
from ..schemas import (AdviceRequest, ReviewOptions, StartTaskRequest, InteractiveReviewRequest,
                       RecoveryRequest, VerdictRequest)
from ..services import orchestrator
from ..services import verdicts as verdict_store
from ..services.pptx_parser import InvalidPptxError, sha256_file, validate_package
from ..storage import default_stages, real_store, store_for, settings_store
from .common import (checked_by, get_task, load_json, remove_task_files, render_map, ensure_local,
                     task_dir)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

Mode = Literal["real", "demo"]
SAFE_DISPLAY = re.compile(r"[\x00-\x1f]")


def summary_row(task: dict) -> dict:
    doc = task.get("document") or {}
    return {
        "id": task["id"],
        "mode": task["mode"],
        "name": task["name"],
        "status": task["status"],
        "progress": task["progress"],
        "source_name": task["source_name"],
        "slide_count": task["slide_count"] or doc.get("slide_count"),
        "aspect_label": doc.get("aspect_label"),
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "error": task["error"],
        "summary": task.get("summary"),
        "demo_scenario": task.get("demo_scenario"),
    }


def all_issues(task: dict) -> list[dict]:
    data = load_json(task_dir(task) / "report" / "report.json", None)
    if data and isinstance(data.get("issues"), list):
        return data["issues"]
    return []


def _task_options_for_display(task: dict) -> dict:
    """Unstarted uploads with automatic choice follow current API availability.

    Completed/running tasks keep their historical choices. New uploads carry an
    explicit preference marker so a user's deliberate opt-out stays intact.
    """
    options = task.get('options') or {}
    if task['status'] in ('draft', 'preparing', 'ready') and options.get('mllm_user_choice') is None:
        from ..services.review_defaults import resolve_review_defaults
        return resolve_review_defaults(
            ReviewOptions.model_validate({**options, 'mllm_user_choice': None}),
            settings_store.all(), settings_store.get_api_key()).model_dump()
    return options


def _allows_early_review_skip(task: dict, options: dict) -> bool:
    # Guard old running tasks too: never claim to cancel an LLM call whose gate
    # is unavailable but whose stage has already started.
    review_stage = next((s for s in task.get('stages', []) if s.get('key') == 'mllm'), {})
    return (task['status'] in ('queued', 'running')
            and bool(options.get('enable_mllm'))
            and options.get('review_mode', 'risk_only') != 'off'
            and review_stage.get('status', 'pending') == 'pending')


def _task_review_gate(task: dict, options: dict) -> dict:
    """Public read-only projection; GET never creates the opt-out record."""
    from ..services import review_gate
    gate = review_gate.read(task_dir(task))
    early = gate['state'] == 'not_ready' and _allows_early_review_skip(task, options)
    return {**gate, 'can_skip': bool(gate.get('can_skip') or early),
            'skip_available_before_stage': early}


def _task_stages_for_display(task: dict) -> list[dict]:
    """Expose saved review errors for old tasks without rewriting history."""
    stages = [{**stage, 'detail': dict(stage.get('detail') or {})}
              for stage in task.get('stages', [])]
    failed = next((stage for stage in stages
                   if stage.get('key') == 'mllm' and stage.get('status') == 'failed'), None)
    if failed is None or failed['detail'].get('review_errors'):
        return stages
    records = load_json(task_dir(task) / 'analysis/reviews.json', [])
    if not isinstance(records, list):
        return stages
    errors = [{'slide_index': record.get('slide_index'), 'status': record.get('status'),
               'error_code': record.get('error_code'),
               'error': str(record.get('error') or '未记录详细错误')[:1000]}
              for record in records if isinstance(record, dict)
              and record.get('status') not in ('ok', 'skipped', 'not_checked')]
    if errors:
        failed['detail']['review_errors'] = errors
        failed['message'] = f"{len(errors)} 项复检失败：{errors[0]['error']}；失败范围未形成复核结论。"
    return stages


async def _save_upload(upload: UploadFile) -> tuple[Path, int]:
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.pptx"
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, detail={
                        "code": "file_too_large",
                        "message": f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限。"})
                out.write(chunk)
        validate_package(tmp_path, MAX_UPLOAD_BYTES, MAX_SLIDES)
    except InvalidPptxError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(400, detail={"code": exc.code, "message": exc.message})
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return tmp_path, total


@router.post("", status_code=201)
async def create_task(request: Request):
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(400, detail={"code": "invalid_request",
                                         "message": "请使用 multipart/form-data 上传 PPTX。"})
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile):
        raise HTTPException(400, detail={"code": "missing_file", "message": "缺少 file 字段。"})
    name = SAFE_DISPLAY.sub("", str(form.get("name") or "")).strip()[:120]
    prepare_only = str(form.get("prepare_only") or "").lower() in ("1", "true", "yes")
    try:
        options = ReviewOptions.model_validate_json(str(form.get("options") or "{}"))
        from ..services.review_defaults import resolve_review_defaults
        options = resolve_review_defaults(options, settings_store.all(), settings_store.get_api_key())
    except Exception as exc:
        raise HTTPException(400, detail={"code": "invalid_options", "message": f"审查选项无效：{exc}"})

    filename = Path(upload.filename or "").name
    if not filename.lower().endswith(".pptx"):
        raise HTTPException(400, detail={"code": "unsupported_format",
                                         "message": "仅支持 .pptx 格式（不支持旧版 .ppt）。"})

    tmp_path, total = await _save_upload(upload)
    task = real_store.create(
        name=name or Path(filename).stem[:80],
        options=options.model_dump(),
        source_name=filename,
        size_bytes=total,
    )
    directory = task_dir(task)
    (directory / "source").mkdir(parents=True, exist_ok=True)
    final = directory / "source" / "source.pptx"
    shutil.move(str(tmp_path), final)
    real_store.update(task["id"], source_sha256=sha256_file(final))
    if prepare_only:
        real_store.update(task["id"], status="draft")
        orchestrator.submit_prepare(task["id"], "real")
    else:
        orchestrator.submit(task["id"], "real")
    return summary_row(real_store.get(task["id"]))  # type: ignore[arg-type]


@router.post("/{task_id}/start")
def start_task(task_id: str, payload: StartTaskRequest, mode: Mode = "real"):
    task = get_task(task_id, mode)
    if task["status"] not in ("ready", "draft"):
        raise HTTPException(409, detail={
            "code": "invalid_state",
            "message": "任务当前状态不可开始审查（需要先完成文件准备）。"})
    if task["status"] == "draft":
        raise HTTPException(409, detail={
            "code": "preparing",
            "message": "文件仍在解析/渲染，请稍候。"})
    stages = task["stages"]
    for st in stages:
        if st["key"] in ("structure", "visual", "mllm", "merge", "report"):
            st.update({"status": "pending", "message": "", "progress": 0.0,
                       "started_at": None, "finished_at": None, "detail": {}})
    from ..services.review_defaults import resolve_review_defaults
    resolved_options=resolve_review_defaults(payload.options,settings_store.all(),settings_store.get_api_key())
    updates = dict(status="queued", options_json=resolved_options.model_dump(),
                   stages_json=stages, error=None, finished_at=None, progress=0.375)
    if payload.name and payload.name.strip():
        updates["name"] = payload.name.strip()[:120]
    store_for(mode).update(task_id, **updates)
    orchestrator.submit(task_id, mode)
    return summary_row(store_for(mode).get(task_id))  # type: ignore[arg-type]


@router.get("")
def list_tasks(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
               mode: Mode = "real"):
    store = store_for(mode)
    return {"items": [summary_row(t) for t in store.list(limit, offset)], "total": store.count()}


@router.get("/{task_id}")
def get_task_detail(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    directory = task_dir(task)
    from ..services.review_gate import GateStateError
    from ..services.recovery import availability
    options = _task_options_for_display(task)
    recovery_info = availability(task)
    try:
        gate=_task_review_gate(task, options)
    except GateStateError as exc:
        gate={"state":"invalid","can_skip":False,"remaining_ms":0,"warning":str(exc)}
    return {
        **summary_row(task),
        "options": options,
        "stages": _task_stages_for_display(task),
        "document": task.get("document"),
        "warnings": task.get("warnings", []),
        "api_snapshot": task.get("api_snapshot"),
        "report_ready": (directory / "report" / "report.html").exists(),
        "images_available": bool(render_map(task)),
        "review_gate":gate,
        "recovery": recovery_info,
        "lineage": recovery_info.get('lineage'),
    }


@router.get("/{task_id}/recovery")
def get_recovery_options(task_id: str, mode: Mode = "real"):
    from ..services.recovery import availability
    return availability(get_task(task_id, mode))


@router.post("/{task_id}/recover")
def recover_task(task_id: str, payload: RecoveryRequest, request: Request, mode: Mode = "real"):
    from ..services.recovery import create_attempt, RecoveryError
    ensure_local(request)
    parent = get_task(task_id, mode)
    try:
        response, created = create_attempt(parent, payload.action,
            idempotency_key=payload.idempotency_key, llm_backend=payload.llm_backend)
    except RecoveryError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)})
    except (OSError, ValueError) as exc:
        raise HTTPException(409, detail={'code': 'recovery_preparation_failed', 'message': str(exc)})
    if created:
        orchestrator.submit(response['task_id'], response['mode'])
    return JSONResponse(response, status_code=201 if created else 200)


@router.get("/{task_id}/stages")
def get_stages(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    return {"stages": _task_stages_for_display(task), "status": task["status"], "progress": task["progress"]}


@router.get("/{task_id}/review-gate")
def get_review_gate(task_id: str, mode: Mode = "real"):
    from ..services import review_gate
    task=get_task(task_id,mode)
    try:
        gate=_task_review_gate(task, _task_options_for_display(task))
    except review_gate.GateStateError as exc:
        raise HTTPException(409,detail={"code":"review_gate_invalid","message":str(exc)})
    return {**gate,"task_status":task['status'],
            "result_ready":task['status'] in ('completed','completed_with_errors') and (task_dir(task)/'report/report.html').is_file()}


@router.post("/{task_id}/skip-review")
def skip_llm_review(task_id: str, request: Request, mode: Mode = "real"):
    from ..services import review_gate
    ensure_local(request)
    task=get_task(task_id,mode)
    try:
        current=review_gate.read(task_dir(task))
        if current['state']!='skipped' and task['status'] not in ('running','queued'):
            raise review_gate.GateConflict('当前任务没有可跳过的LLM等待窗口。')
        options = _task_options_for_display(task)
        early = _allows_early_review_skip(task, options)
        if current['state'] == 'not_ready' and not early:
            raise review_gate.GateConflict('当前任务尚未启用可跳过的LLM复检，或复检阶段已经开始。')
        gate=review_gate.skip(task_dir(task), allow_before_ready=early)
    except review_gate.GateConflict as exc:
        raise HTTPException(409,detail={"code":"review_already_started","message":str(exc)})
    except review_gate.GateStateError as exc:
        raise HTTPException(409,detail={"code":"review_gate_invalid","message":str(exc)})
    return {**gate,"skip_available_before_stage":False,"task_status":task['status'],
            "result_ready":task['status'] in ('completed','completed_with_errors') and (task_dir(task)/'report/report.html').is_file()}


@router.get("/{task_id}/slides")
def get_slides(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    doc = task.get("document")
    if not doc:
        raise HTTPException(409, detail={"code": "not_parsed", "message": "文件尚未解析完成。"})
    renders = render_map(task)
    issues = all_issues(task)
    by_slide: dict[int, list[dict]] = {}
    for issue in issues:
        by_slide.setdefault(int(issue["slide_index"]), []).append(issue)
    flags = checked_by(task)
    suffix = "&mode=demo" if mode == "demo" else ""
    slides = []
    for slide in doc["slides"]:
        idx = int(slide["index"])
        render = renders.get(idx, {})
        slide_issues = [i for i in by_slide.get(idx, []) if verdict_store.is_visible(i)]
        slides.append({
            "index": idx,
            "content_mode": slide.get("content_mode"),
            "text_chars": slide.get("text_chars"),
            "textboxes": slide.get("text_shapes"),
            "pictures": slide.get("pictures"),
            "tables": slide.get("tables"),
            "charts": slide.get("charts"),
            "render_status": render.get("status", "unavailable"),
            "render_error": render.get("error"),
            "width": render.get("width"),
            "height": render.get("height"),
            "thumb_url": (f"/api/tasks/{task_id}/slides/{idx}/image?kind=thumb{suffix}"
                          if render.get("thumb") else None),
            "image_url": (f"/api/tasks/{task_id}/slides/{idx}/image?kind=render{suffix}"
                          if render.get("png") else None),
            "issue_count": len(slide_issues),
            "issue_types": sorted({i["defect_type"] for i in slide_issues}),
            "high_risk": any(i.get("severity") == "high" for i in slide_issues),
            "ai_reviewed": any(i.get("review_status") in ("supported", "rejected", "uncertain")
                               for i in slide_issues),
        })
    return {"slides": slides, "checked_by": flags, "slide_count": len(slides)}


@router.get("/{task_id}/slides/{slide_index}")
def get_slide(task_id: str, slide_index: int, mode: Mode = "real"):
    task = get_task(task_id, mode)
    doc = task.get("document") or {}
    if not 1 <= slide_index <= int(doc.get("slide_count") or 0):
        raise HTTPException(404, detail={"code": "slide_not_found", "message": "页码超出范围。"})
    slide = next((s for s in doc.get("slides", []) if int(s["index"]) == slide_index), None)
    render = render_map(task).get(slide_index, {})
    issues = [i for i in all_issues(task) if int(i["slide_index"]) == slide_index]
    suffix = "&mode=demo" if mode == "demo" else ""
    visual_data=load_json(task_dir(task)/"analysis/visual_results.json",{})
    visual_result=next((r for r in visual_data.get('results',[]) if r.get('slide_index')==slide_index),None)
    return {
        "index": slide_index,
        "slide": slide,
        "render": render,
        "issues": issues,
        "visual_result": visual_result,
        "image_url": f"/api/tasks/{task_id}/slides/{slide_index}/image?kind=render{suffix}",
        "thumb_url": f"/api/tasks/{task_id}/slides/{slide_index}/image?kind=thumb{suffix}",
        "issues_confirmed": sum(1 for i in issues if i.get("review_status") == "supported"),
        "issues_rejected": sum(1 for i in issues if i.get("review_status") == "rejected"),
        "issues_dismissed": sum(1 for i in issues if verdict_store.is_dismissed(i)),
    }


@router.get("/{task_id}/slides/{slide_index}/image")
def get_slide_image(task_id: str, slide_index: int, kind: str = Query("render"),
                    mode: Mode = "real"):
    task = get_task(task_id, mode)
    if not 1 <= slide_index <= 999:
        raise HTTPException(404, detail={"code": "slide_not_found", "message": "页码超出范围。"})
    base = task_dir(task) / "renders"
    path = base / ("thumbs" if kind == "thumb" else "") / (
        f"s{slide_index:04d}.jpg" if kind == "thumb" else f"s{slide_index:04d}.png")
    media = "image/jpeg" if kind == "thumb" else "image/png"
    if not path.exists():
        raise HTTPException(404, detail={"code": "image_not_found", "message": "该页渲染图不可用。"})
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{task_id}/issues")
def get_issues(task_id: str, defect_type: str | None = None, source: str | None = None,
               review_status: str | None = None, mode: Mode = "real"):
    task = get_task(task_id, mode)
    issues = all_issues(task)
    if defect_type:
        issues = [i for i in issues if i["defect_type"] == defect_type]
    if source:
        issues = [i for i in issues if any(s["source"] == source for s in i.get("sources", []))]
    if review_status:
        issues = [i for i in issues if i.get("review_status") == review_status]
    return {"items": issues, "total": len(issues), "labels": DEFECT_LABELS_ZH}


@router.get("/{task_id}/module-status")
def get_module_status(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    return {
        "stages": task["stages"],
        "visual": orchestrator.classifier_status(),
        "warnings": task.get("warnings", []),
    }


@router.get("/{task_id}/claims")
def get_claims(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    return load_json(task_dir(task)/"analysis/claims.json", {"claims": [], "capability": {"status": "not_checked"}})


@router.get("/{task_id}/review-plan")
def get_review_plan(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    return load_json(task_dir(task)/"analysis/review_plan.json", {"items": [], "enabled": False, "status": "not_checked"})


@router.get("/{task_id}/visual-results")
def get_visual_results(task_id: str, mode: Mode = "real"):
    task=get_task(task_id,mode)
    return load_json(task_dir(task)/"analysis/visual_results.json",{
        "results":[],"enabled":bool(task.get('options',{}).get('enable_visual')),"status":"not_checked"})


@router.get("/{task_id}/issues/{issue_id}/reviews")
def get_review_history(task_id: str, issue_id: str, mode: Mode = "real"):
    from ..services.interaction import history_for
    task = get_task(task_id, mode)
    if not any(i["issue_id"]==issue_id for i in all_issues(task)):
        raise HTTPException(404,detail={"code":"issue_not_found","message":"问题ID不存在。"})
    return {"history":history_for(task_dir(task),issue_id)}


@router.post("/{task_id}/issues/{issue_id}/review")
def interactive_review(task_id: str, issue_id: str, payload: InteractiveReviewRequest,
                       request: Request, mode: Mode = "real"):
    from ..services.interaction import review_finding
    ensure_local(request)
    task = get_task(task_id, mode)
    if task["status"] not in ("completed","completed_with_errors"):
        raise HTTPException(409,detail={"code":"task_active","message":"请等待当前任务完成后再复核。"})
    try:
        result=review_finding(task,task_dir(task),issue_id,payload.question,payload.llm_backend,settings_store.all(),settings_store.get_api_key())
    except KeyError:
        raise HTTPException(404,detail={"code":"issue_not_found","message":"问题ID不存在。"})
    except ValueError as exc:
        raise HTTPException(400,detail={"code":"review_unavailable","message":str(exc)})
    store_for(mode).update(task_id,summary_json=result["summary"])
    return result


@router.post("/{task_id}/issues/{issue_id}/verdict")
def set_issue_verdict(task_id: str, issue_id: str, payload: VerdictRequest,
                      request: Request, mode: Mode = "real"):
    """Human override: a finding the reviewer has verified as a misjudgement.

    The detector observation is preserved; only counting, the report, the
    advice and the exported prompt stop showing it.
    """
    from ..services.interaction import set_verdict
    ensure_local(request)
    task = get_task(task_id, mode)
    if task["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(409, detail={"code": "task_active",
                                         "message": "请等待当前任务完成后再修改缺陷认定。"})
    try:
        result = set_verdict(task, task_dir(task), issue_id, payload.status, payload.note)
    except KeyError:
        raise HTTPException(404, detail={"code": "issue_not_found", "message": "问题ID不存在。"})
    store_for(mode).update(task_id, summary_json=result["summary"])
    return result


@router.get("/{task_id}/advice")
def get_advice(task_id: str, mode: Mode = "real"):
    from ..services import advice as advice_service
    task = get_task(task_id, mode)
    data = load_json(task_dir(task) / "report" / "report.json", None)
    if not data:
        raise HTTPException(409, detail={"code": "report_not_ready", "message": "报告尚未生成。"})
    return data.get("advice") or advice_service.compose(data)


@router.post("/{task_id}/advice")
def refresh_advice(task_id: str, payload: AdviceRequest, request: Request, mode: Mode = "real"):
    from ..services import advice as advice_service
    from ..services.interaction import rebuild
    ensure_local(request)
    task = get_task(task_id, mode)
    if task["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(409, detail={"code": "task_active",
                                         "message": "请等待当前任务完成后再生成修改建议。"})
    directory = task_dir(task)
    data = load_json(directory / "report" / "report.json", None)
    if not data:
        raise HTTPException(409, detail={"code": "report_not_ready", "message": "报告尚未生成。"})
    result = advice_service.generate(task, directory, data, backend=payload.backend,
                                     settings=settings_store.all(),
                                     api_key=settings_store.get_api_key())
    updated = rebuild(task, directory)
    store_for(mode).update(task_id, summary_json=updated["summary"])
    return {"advice": updated.get("advice") or result, "summary": updated["summary"]}


@router.get("/{task_id}/prompt")
def get_fix_prompt(task_id: str, download: bool = False, mode: Mode = "real"):
    """Markdown prompt for an external editing agent; dismissed items excluded."""
    from fastapi.responses import PlainTextResponse

    from ..services import advice as advice_service
    task = get_task(task_id, mode)
    data = load_json(task_dir(task) / "report" / "report.json", None)
    if not data:
        raise HTTPException(409, detail={"code": "report_not_ready", "message": "报告尚未生成。"})
    text = advice_service.build_prompt(data)
    disposition = "attachment" if download else "inline"
    return PlainTextResponse(
        text, media_type="text/markdown",
        headers={"Content-Disposition": f'{disposition}; filename="ppt-fix-prompt-{task_id}.md"'},
    )


@router.get("/{task_id}/report")
def get_report(task_id: str, format: str = Query("html"), download: bool = False,
               mode: Mode = "real"):
    task = get_task(task_id, mode)
    fmt = format.lower()
    if fmt not in ("html", "json", "pdf"):
        raise HTTPException(400, detail={"code": "unsupported_format", "message": "format 只能是 html、json 或 pdf。"})
    if fmt == "pdf":
        from ..services.pdf_export import export_pdf
        try:
            path = export_pdf(task_dir(task))
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(503, detail={"code": "pdf_unavailable", "message": str(exc)})
        return FileResponse(path, media_type="application/pdf", filename=f"audit_{task_id}.pdf")
    path = task_dir(task) / "report" / f"report.{fmt}"
    if not path.exists():
        raise HTTPException(409, detail={"code": "report_not_ready", "message": "报告尚未生成。"})
    media = "text/html; charset=utf-8" if fmt == "html" else "application/json"
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path, media_type=media,
        headers={"Content-Disposition": f'{disposition}; filename="audit_{task_id}.{fmt}"'},
    )


@router.get("/{task_id}/report/data")
def get_report_data(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    data = load_json(task_dir(task) / "report" / "report.json", None)
    if not data:
        raise HTTPException(409, detail={"code": "report_not_ready", "message": "报告尚未生成。"})
    return JSONResponse(data)


@router.delete("/{task_id}")
def delete_task(task_id: str, mode: Mode = "real"):
    task = get_task(task_id, mode)
    if task["status"] in ("queued", "running"):
        raise HTTPException(409, detail={"code": "task_active", "message": "任务正在运行，无法删除。"})
    store_for(mode).delete(task_id)
    removed = remove_task_files(task)
    return {"ok": True, "removed_files": removed}
