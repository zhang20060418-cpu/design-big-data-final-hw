"""Demonstration cohort API — physically isolated from real tasks."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..schemas import DemoTaskRequest
from ..services import demo, orchestrator
from ..services.pptx_parser import sha256_file
from ..storage import demo_store
from .common import task_dir
from .tasks import summary_row

router = APIRouter(prefix="/api/demo", tags=["demo"])

SCENARIOS = {
    "full": {
        "label": "完整演示",
        "description": "结构规则 + 演示视觉结果 + 演示 AI 复核，全部模块点亮。",
    },
    "risk": {
        "label": "高风险演示",
        "description": "演示概率整体抬升，用于展示高风险页面与筛选。",
    },
    "api_failure": {
        "label": "API 失败演示",
        "description": "模拟云端复核网络失败，验证其他结果仍然保留。",
    },
    "module_missing": {
        "label": "模块缺失演示",
        "description": "模拟视觉与复核模块未接入时的降级展示。",
    },
}


@router.get("/scenarios")
def list_scenarios():
    return {"items": [{"key": k, **v} for k, v in SCENARIOS.items()]}


@router.post("/tasks", status_code=201)
def create_demo_task(payload: DemoTaskRequest):
    fixture = demo.build_fixture()
    scenario = payload.scenario
    options = {
        "enable_structure": True,
        "enable_visual": True,
        "enable_mllm": scenario not in ("module_missing",),
        "review_mode": "risk_only",
        "max_review_pages": 4,
        "max_api_calls": 4,
        "manual_pages": [],
    }
    name = payload.name.strip()[:120] or f"演示审查 · {SCENARIOS[scenario]['label']}"
    task = demo_store.create(
        name=name,
        options=options,
        source_name="演示文稿（系统生成的 fixture）.pptx",
        size_bytes=fixture.stat().st_size,
        demo_scenario=scenario,
        api_snapshot={"mode": "demo", "note": "演示数据，不调用外部 API"},
    )
    directory = task_dir(task)
    (directory / "source").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, directory / "source" / "source.pptx")
    demo_store.update(task["id"], source_sha256=sha256_file(directory / "source" / "source.pptx"))
    orchestrator.submit(task["id"], "demo")
    return summary_row(demo_store.get(task["id"]))  # type: ignore[arg-type]


@router.get("/tasks")
def list_demo_tasks():
    items = [summary_row(t) for t in demo_store.list(30, 0)]
    return {"items": items, "total": demo_store.count()}


@router.delete("/tasks/{task_id}")
def delete_demo_task(task_id: str):
    from .tasks import delete_task
    return delete_task(task_id, mode="demo")
