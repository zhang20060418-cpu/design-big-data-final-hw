"""Shared helpers for API routers."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from fastapi import HTTPException, Request

from ..config import DATA_DIR
from ..storage import store_for

ID_RE = re.compile(r"^[a-f0-9]{16}$")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def ensure_local(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        raise HTTPException(status_code=403, detail={
            "code": "local_only", "message": "该接口仅允许本机访问。"})


def get_task(task_id: str, mode: str = "real") -> dict:
    if not ID_RE.match(task_id):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "任务不存在。"})
    task = store_for(mode).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "任务不存在。"})
    return task


def task_dir(task: dict) -> Path:
    base = "demo_tasks" if task["mode"] == "demo" else "tasks"
    return DATA_DIR / base / task["id"]


def remove_task_files(task: dict) -> bool:
    path = task_dir(task).resolve()
    root = (DATA_DIR / ("demo_tasks" if task["mode"] == "demo" else "tasks")).resolve()
    if root not in path.parents:
        return False
    if path.exists():
        try:
            shutil.rmtree(path, ignore_errors=False)
        except (Exception, SystemExit):
            # A locked file or an environment-level delete guard must not turn a
            # task deletion into a 500.  The DB row is already gone; leftover
            # artefacts are reported and can be cleaned by the operator.
            return False
    return True


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def render_map(task: dict) -> dict[int, dict]:
    data = load_json(task_dir(task) / "renders" / "slides.json", {})
    result = (data.get("result") or {}) if isinstance(data, dict) else {}
    return {int(s["index"]): s for s in result.get("slides", []) if s.get("index")}


def checked_by(task: dict) -> dict[str, bool]:
    stages = {s["key"]: s["status"] for s in task.get("stages", [])}
    return {
        "structure": stages.get("structure") == "done",
        "visual": stages.get("visual") == "done",
        "mllm": stages.get("mllm") == "done",
    }
