"""High-level workflows shared by the CLI and the MCP server.

Everything here is a client of the running FastAPI service.  Nothing imports
``backend.app``; the only contract is HTTP plus the report JSON shape.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .client import ApiError, Client, InputError, ToolError
from .contract import (
    TERMINAL_REPAIR_STATUSES,
    TERMINAL_TASK_STATUSES,
    normalize_report,
    pending_result,
    profile_options,
    visible_issues,
)

Progress = Callable[[dict[str, Any], dict[str, Any] | None], None]


def _emit(progress: Progress | None, detail: dict[str, Any]) -> None:
    if progress is None:
        return
    stage = next((item for item in detail.get("stages") or []
                  if item.get("status") == "running"), None)
    progress(detail, stage)


def _output_dir(root: str | Path | None, task_id: str) -> Path | None:
    if root is None or str(root).strip() == "":
        return None
    path = Path(root).expanduser() / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _artifacts(client: Client, task_id: str, mode: str, directory: Path | None,
               *, download: bool = True) -> dict[str, Any]:
    urls = {
        "report_json_url": client.report_url(task_id, "json", mode),
        "report_html_url": client.report_url(task_id, "html", mode),
    }
    if directory is None or not download:
        return urls
    json_path = directory / "report.json"
    html_path = directory / "report.html"
    client.download_report(task_id, "json", json_path, mode)
    client.download_report(task_id, "html", html_path, mode)
    return {
        **urls,
        "report_json": str(json_path),
        "report_html": str(html_path),
    }


def wait_for_task(
    client: Client,
    task_id: str,
    *,
    mode: str = "real",
    timeout: float = 900.0,
    progress: Progress | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout))
    interval = 1.0
    while True:
        detail = client.get_task(task_id, mode)
        _emit(progress, detail)
        status = detail.get("status")
        if status in TERMINAL_TASK_STATUSES:
            return detail
        if time.monotonic() >= deadline:
            return {**detail, "timed_out": True}
        time.sleep(interval)
        interval = min(5.0, interval + 0.5)


def _finish_task(
    client: Client,
    detail: dict[str, Any],
    *,
    profile: str | None,
    mode: str,
    output_dir: str | Path | None,
    issue_mode: str,
) -> dict[str, Any]:
    task_id = str(detail.get("id") or "")
    status = detail.get("status")
    if status in ("failed", "canceled"):
        result = pending_result(detail, profile=profile)
        result["ok"] = False
        result["error"] = detail.get("error") or f"任务状态：{status}"
        return result

    directory = _output_dir(output_dir, task_id)
    try:
        report = client.report_data(task_id, mode)
    except ApiError as exc:
        result = pending_result(detail, profile=profile)
        result["ok"] = False
        result["error"] = exc.as_dict()
        result["next"] = f"ppt-audit status {task_id}"
        return result

    artifacts = _artifacts(client, task_id, mode, directory)
    result = normalize_report(
        report, detail=detail, issue_mode=issue_mode, artifacts=artifacts,
    )
    result["profile"] = profile
    if directory is not None:
        try:
            (directory / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            result["artifacts"]["result_json"] = str(directory / "result.json")
        except OSError:
            pass
    return result


def run_audit(
    client: Client,
    file_path: str | Path,
    *,
    profile: str = "standard",
    name: str = "",
    mode: str = "real",
    wait: bool = True,
    wait_timeout: float = 900.0,
    output_dir: str | Path | None = "slidesentinel-output",
    issue_mode: str = "summary",
    progress: Progress | None = None,
) -> dict[str, Any]:
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise InputError(f"文件不存在：{path}")
    if path.suffix.lower() != ".pptx":
        raise InputError("只支持 .pptx 文件；旧版 .ppt 请先另存为 .pptx。")

    started = time.monotonic()
    options = profile_options(profile)
    created = client.create_task(path, name=name or path.stem, options=options)
    task_id = str(created.get("id") or "")
    if not task_id:
        raise ToolError("服务没有返回任务 ID，上传可能失败。")

    if not wait:
        result = pending_result(created, profile=profile)
        result["next"] = f"ppt-audit status {task_id}"
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        return result

    detail = wait_for_task(client, task_id, mode=mode, timeout=wait_timeout,
                           progress=progress)
    result = _finish_task(client, detail, profile=profile, mode=mode,
                          output_dir=output_dir, issue_mode=issue_mode)
    result["elapsed_s"] = round(time.monotonic() - started, 1)
    return result


def status_result(
    client: Client,
    task_id: str,
    *,
    mode: str = "real",
    issue_mode: str = "summary",
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    detail = client.get_task(task_id, mode)
    if detail.get("report_ready"):
        return _finish_task(client, detail, profile=None, mode=mode,
                            output_dir=output_dir, issue_mode=issue_mode)
    result = pending_result(detail)
    if detail.get("status") in TERMINAL_TASK_STATUSES:
        result["ok"] = False
        result["error"] = detail.get("error") or "任务已结束但没有生成报告。"
    return result


def list_result(client: Client, *, mode: str = "real", limit: int = 30,
                offset: int = 0) -> dict[str, Any]:
    data = client.list_tasks(mode=mode, limit=limit, offset=offset)
    items = []
    for task in data.get("items") or []:
        items.append({
            "id": task.get("id"),
            "name": task.get("name"),
            "status": task.get("status"),
            "progress": task.get("progress"),
            "slide_count": task.get("slide_count"),
            "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
            "summary": task.get("summary"),
            "demo_scenario": task.get("demo_scenario"),
        })
    return {"ok": True, "mode": mode, "items": items, "total": data.get("total")}


def run_demo(
    client: Client,
    *,
    scenario: str = "full",
    name: str = "",
    wait: bool = True,
    wait_timeout: float = 300.0,
    output_dir: str | Path | None = "slidesentinel-output",
    issue_mode: str = "summary",
    progress: Progress | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    created = client.create_demo(scenario=scenario, name=name)
    task_id = str(created.get("id") or "")
    if not task_id:
        raise ToolError("服务没有返回演示任务 ID。")
    if not wait:
        result = pending_result(created, profile="demo")
        result["next"] = f"ppt-audit status {task_id} --mode demo"
        return result
    detail = wait_for_task(client, task_id, mode="demo", timeout=wait_timeout,
                           progress=progress)
    result = _finish_task(client, detail, profile="demo", mode="demo",
                          output_dir=output_dir, issue_mode=issue_mode)
    result["elapsed_s"] = round(time.monotonic() - started, 1)
    return result


def _repair_result(client: Client, task_id: str, record: dict[str, Any],
                   *, output_dir: str | Path | None, download_fixed: bool) -> dict[str, Any]:
    repair_id = str(record.get("repair_id") or "")
    status = record.get("status")
    result: dict[str, Any] = {
        "ok": status == "completed",
        "tool": "slidesentinel",
        "task_id": task_id,
        "repair_id": repair_id,
        "status": status,
        "message": record.get("message"),
        "plan_source": record.get("plan_source"),
        "model": record.get("model"),
        "degraded": record.get("degraded"),
        "summary": record.get("summary") or {},
        "verify": record.get("verify") or {},
        "review": record.get("review") or {},
        "artifacts": {},
    }
    directory = _output_dir(output_dir, task_id)
    if directory is not None:
        repair_dir = directory / "repairs" / repair_id
        repair_dir.mkdir(parents=True, exist_ok=True)
        try:
            (repair_dir / "status.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            result["artifacts"]["repair_status"] = str(repair_dir / "status.json")
        except OSError:
            pass
        if download_fixed and status == "completed":
            target = repair_dir / "fixed.pptx"
            client.download_repair_file(task_id, repair_id, target)
            result["artifacts"]["fixed_pptx"] = str(target)
    result["artifacts"]["bundle_url"] = client.repair_bundle_url(task_id, repair_id)
    return result


def run_repair(
    client: Client,
    task_id: str,
    *,
    mode: str = "real",
    backend: str = "api",
    review: bool = False,
    rounds: int = 0,
    wait: bool = True,
    wait_timeout: float = 900.0,
    output_dir: str | Path | None = "slidesentinel-output",
    download_fixed: bool = False,
    progress: Progress | None = None,
) -> dict[str, Any]:
    record = client.start_repair(task_id, mode=mode, backend=backend,
                                 review=review, rounds=rounds)
    repair_id = str(record.get("repair_id") or "")
    if not repair_id:
        raise ToolError("服务没有返回修复记录 ID。")
    if not wait or record.get("status") in TERMINAL_REPAIR_STATUSES:
        return _repair_result(client, task_id, record, output_dir=output_dir,
                              download_fixed=download_fixed)

    deadline = time.monotonic() + max(1.0, float(wait_timeout))
    interval = 1.0
    current = record
    while True:
        current = client.get_repair(task_id, repair_id, mode)
        if progress is not None:
            progress(current, None)
        if current.get("status") in TERMINAL_REPAIR_STATUSES:
            break
        if time.monotonic() >= deadline:
            current = {**current, "message": "等待修复超时，记录仍在后台运行。"}
            break
        time.sleep(interval)
        interval = min(5.0, interval + 0.5)
    result = _repair_result(client, task_id, current, output_dir=output_dir,
                            download_fixed=download_fixed)
    if result.get("status") not in TERMINAL_REPAIR_STATUSES:
        result["ok"] = False
        result["timed_out"] = True
        result["next"] = f"ppt-audit repair {task_id} --no-wait  # 或查询已有记录"
    return result


def explain_issue(client: Client, task_id: str, issue_id: str,
                  *, mode: str = "real") -> dict[str, Any]:
    report = client.report_data(task_id, mode)
    issues = report.get("issues") or []
    issue = next((item for item in issues
                  if isinstance(item, dict) and str(item.get("issue_id")) == str(issue_id)), None)
    if issue is None:
        return {
            "ok": False,
            "task_id": task_id,
            "issue_id": issue_id,
            "error": {"code": "issue_not_found", "message": "报告中没有这个 issue_id。"},
        }
    slide_index = issue.get("slide_index")
    visible = issue in visible_issues(report)
    return {
        "ok": True,
        "task_id": task_id,
        "issue_id": issue_id,
        "visible": visible,
        "issue": issue,
        "page_image_url": client.image_url(task_id, int(slide_index), mode=mode)
        if slide_index else None,
        "note": "原始检测观察保留；visible=false 表示已被人工排除或 AI 否决。",
    }
