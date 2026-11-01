"""Agent-facing contract for SlideSentinel.

The backend report is intentionally rich (several hundred KB, with evidence,
claims and page images).  An agent usually needs a smaller, stable shape:
counts, visible findings, page index, module state and limitations.

This module owns that projection.  It has **no dependency on the backend
package**; it only reads the JSON returned by ``GET /api/tasks/{id}/report/data``.
"""
from __future__ import annotations

from typing import Any

TOOL_NAME = "slidesentinel"
TOOL_VERSION = "1.0.0"
CONTRACT_VERSION = "1.0"

# The four review profiles are the only place where CLI/MCP defaults live.
# ``quick`` still renders pages because the existing pipeline renders before
# the structure stage; it only skips the six-detector and LLM stages.
PROFILES: dict[str, dict[str, Any]] = {
    "quick": {
        "label": "快速初筛",
        "description": "结构规则初筛；跳过六类检测与 LLM 复核，页面仍会渲染。",
        "options": {
            "enable_structure": True,
            "enable_visual": False,
            "enable_mllm": False,
            "mllm_user_choice": False,
            "review_mode": "off",
            "max_review_pages": 0,
            "max_api_calls": 0,
            "enable_six_detectors": False,
        },
    },
    "standard": {
        "label": "标准审查",
        "description": "结构规则 + 六类专用检测；不调用 CLIP 与 LLM，适合无 API Key 的完整跑通。",
        "options": {
            "enable_structure": True,
            "enable_visual": False,
            "enable_mllm": False,
            "mllm_user_choice": False,
            "review_mode": "off",
            "max_review_pages": 0,
            "max_api_calls": 0,
            "enable_six_detectors": True,
        },
    },
    "deep": {
        "label": "深度复核",
        "description": "在标准审查上叠加 CLIP 辅助与 LLM 按需复核；需要已配置的多模态 API。",
        "options": {
            "enable_structure": True,
            "enable_visual": True,
            "enable_mllm": True,
            "mllm_user_choice": True,
            "review_mode": "risk_only",
            "max_review_pages": 6,
            "max_api_calls": 6,
            "enable_six_detectors": True,
        },
    },
}

DEFECT_LABELS_ZH = {
    "improper_font_sizing": "字号不合理",
    "occluded_content": "内容遮挡",
    "cluttered_layout": "布局拥挤",
    "content_overflow_cutoff": "内容溢出或裁切",
    "excessive_whitespace": "过分留白",
    "generative_design_anomaly": "异常设计风险",
}

TERMINAL_TASK_STATUSES = {"completed", "completed_with_errors", "failed", "canceled"}
TERMINAL_REPAIR_STATUSES = {"completed", "failed", "canceled"}

ISSUE_MODES = ("none", "summary", "all")


class ContractError(ValueError):
    """Raised when a caller asks for an unknown profile or issue mode."""


def profile_options(name: str) -> dict[str, Any]:
    """Return a fresh options dict for ``name``.

    A fresh copy matters: the caller may add task-specific fields without
    mutating the module-level defaults.
    """
    key = str(name or "").strip().lower()
    profile = PROFILES.get(key)
    if profile is None:
        raise ContractError(f"未知 profile：{name!r}；可选：{', '.join(PROFILES)}")
    return dict(profile["options"])


def profile_catalog() -> list[dict[str, Any]]:
    return [{"name": key, **value} for key, value in PROFILES.items()]


def _sources(issue: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in issue.get("sources") or []:
        if isinstance(item, dict):
            value = item.get("source")
        else:
            value = str(item)
        if value and value not in out:
            out.append(str(value))
    return out


def compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields an agent needs to explain and locate a finding."""
    return {
        "issue_id": issue.get("issue_id"),
        "slide_index": issue.get("slide_index"),
        "defect_type": issue.get("defect_type"),
        "defect_label": DEFECT_LABELS_ZH.get(issue.get("defect_type"), issue.get("defect_type")),
        "title": issue.get("title"),
        "severity": issue.get("severity"),
        "confidence": issue.get("confidence"),
        "review_status": issue.get("review_status"),
        "execution_status": issue.get("execution_status"),
        "evidence": issue.get("evidence_summary"),
        "recommendation": issue.get("recommendation"),
        "bbox": issue.get("bbox"),
        "normalized_bbox": issue.get("normalized_bbox"),
        "coordinate_space": issue.get("coordinate_space"),
        "sources": _sources(issue),
        "limitations": list(issue.get("limitations") or [])[:5],
    }


def visible_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror the backend's visibility rule without importing it.

    ``report['issues']`` retains every detector observation; dismissed and
    AI-rejected findings are excluded from counts and page lists.  The tool
    must use the same projection or its counts will disagree with the UI.
    """
    items = report.get("issues") or []
    return [
        item for item in items
        if isinstance(item, dict)
        and item.get("review_status") != "rejected"
        and item.get("user_verdict") != "dismissed"
    ]


def _module_states(report: dict[str, Any]) -> dict[str, Any]:
    states: dict[str, Any] = {}
    raw = report.get("module_status") or {}
    if not isinstance(raw, dict):
        return states
    for name, value in raw.items():
        if not isinstance(value, dict):
            states[name] = {"status": value}
            continue
        entry = {
            "status": value.get("status") or value.get("state"),
            "version": value.get("version"),
        }
        if value.get("reason"):
            entry["reason"] = value.get("reason")
        if value.get("enabled") is not None:
            entry["enabled"] = value.get("enabled")
        states[name] = entry
    return states


def _page_index(report: dict[str, Any], issue_mode: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for page in (report.get("pages") or [])[:200]:
        if not isinstance(page, dict):
            continue
        page_issues = page.get("issues") or []
        entry: dict[str, Any] = {
            "index": page.get("index"),
            "issue_count": page.get("issue_count"),
            "mode_label": page.get("mode_label"),
        }
        if issue_mode == "all":
            entry["issues"] = [compact_issue(item) for item in page_issues if isinstance(item, dict)]
        else:
            entry["issue_ids"] = [item.get("issue_id") for item in page_issues
                                  if isinstance(item, dict) and item.get("issue_id")]
        pages.append(entry)
    return pages


def _document(report: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    doc = report.get("document") or {}
    if not isinstance(doc, dict):
        doc = {}
    source = task.get("source_name") or doc.get("file_name")
    return {
        "file_name": doc.get("file_name") or source,
        "slide_count": doc.get("slide_count") or task.get("slide_count"),
        "aspect_label": doc.get("aspect_label"),
        "slide_width_in": doc.get("slide_width_in"),
        "slide_height_in": doc.get("slide_height_in"),
        "size_mb": doc.get("size_mb"),
        "sha256_short": doc.get("sha256_short"),
        "textboxes": doc.get("textboxes"),
        "pictures": doc.get("pictures"),
        "tables": doc.get("tables"),
        "charts": doc.get("charts"),
        "editable_pages": doc.get("editable_pages"),
        "image_only_pages": doc.get("image_only_pages"),
    }


def _risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in (report.get("risks") or [])[:20]:
        if not isinstance(item, dict):
            continue
        out.append({
            "type": item.get("type"),
            "tag": item.get("tag"),
            "title": item.get("title"),
            "detail": item.get("detail") or item.get("message"),
        })
    return out


def normalize_report(
    report: dict[str, Any],
    *,
    task: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    issue_mode: str = "summary",
    max_issues: int = 50,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a full report into the stable tool result."""
    if issue_mode not in ISSUE_MODES:
        raise ContractError(f"未知 issues 模式：{issue_mode!r}；可选：{', '.join(ISSUE_MODES)}")

    report_task = report.get("task") if isinstance(report.get("task"), dict) else {}
    task = {**report_task, **(task or {})}
    detail = detail or {}
    status = task.get("status") or detail.get("status") or "unknown"
    all_visible = visible_issues(report)

    if issue_mode == "none":
        issues: list[dict[str, Any]] = []
        truncated = 0
    elif issue_mode == "all":
        issues = [compact_issue(item) for item in all_visible[:500]]
        truncated = max(0, len(all_visible) - len(issues))
    else:
        issues = [compact_issue(item) for item in all_visible[:max_issues]]
        truncated = max(0, len(all_visible) - len(issues))

    return {
        "ok": status in ("completed", "completed_with_errors"),
        "contract": CONTRACT_VERSION,
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "task_id": task.get("id") or detail.get("id"),
        "mode": task.get("mode") or detail.get("mode") or "real",
        "status": status,
        "status_label": task.get("status_label") or detail.get("status_label"),
        "profile": detail.get("profile"),
        "document": _document(report, task),
        "summary": report.get("summary") or task.get("summary") or {},
        "modules": _module_states(report),
        "issues": issues,
        "issue_mode": issue_mode,
        "issues_total": len(all_visible),
        "issues_truncated": truncated,
        "pages": _page_index(report, issue_mode),
        "risks": _risks(report),
        "limitations": list(report.get("limitations") or []),
        "options": task.get("options") or {},
        "artifacts": artifacts or {},
        "warnings": list(detail.get("warnings") or []),
        "error": task.get("error") or detail.get("error"),
        "generated_at": report.get("generated_at"),
        "timed_out": bool(detail.get("timed_out")),
    }


def pending_result(
    task: dict[str, Any],
    *,
    profile: str | None = None,
    artifacts: dict[str, Any] | None = None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Result for a task that is queued/running or timed out before report."""
    status = task.get("status") or "unknown"
    return {
        "ok": False,
        "contract": CONTRACT_VERSION,
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "task_id": task.get("id"),
        "mode": task.get("mode") or "real",
        "status": status,
        "status_label": task.get("status_label"),
        "profile": profile,
        "progress": task.get("progress"),
        "document": {
            "file_name": task.get("source_name") or task.get("name"),
            "slide_count": task.get("slide_count"),
            "aspect_label": (task.get("document") or {}).get("aspect_label")
            if isinstance(task.get("document"), dict) else None,
        },
        "summary": task.get("summary") or {},
        "stages": [
            {
                "key": stage.get("key"),
                "name": stage.get("name"),
                "status": stage.get("status"),
                "message": stage.get("message"),
                "progress": stage.get("progress"),
            }
            for stage in (task.get("stages") or [])
        ],
        "artifacts": artifacts or {},
        "warnings": list(task.get("warnings") or []),
        "error": task.get("error"),
        "timed_out": bool(timed_out),
        "next": f"ppt-audit status {task.get('id')}",
    }


def human_summary(result: dict[str, Any]) -> str:
    """Short terminal summary; JSON remains the canonical output."""
    if not result.get("ok") and result.get("error") and not result.get("summary"):
        error = result.get("error")
        if isinstance(error, dict):
            return f"失败：{error.get('message') or error}"
        return f"失败：{error}"

    doc = result.get("document") or {}
    summary = result.get("summary") or {}
    name = doc.get("file_name") or result.get("task_id") or "任务"
    status = result.get("status_label") or result.get("status") or "unknown"
    lines = [
        f"{name} · {status}",
        f"页数 {doc.get('slide_count') or summary.get('slide_count') or '-'} · "
        f"可见问题 {summary.get('visible_issue_count', summary.get('issue_count', 0))} · "
        f"风险页 {len(summary.get('high_risk_pages') or [])}",
    ]
    by_type = summary.get("by_type") or {}
    typed = [f"{DEFECT_LABELS_ZH.get(k, k)} {v}" for k, v in by_type.items() if v]
    if typed:
        lines.append("类型：" + "，".join(typed))
    if result.get("issues"):
        lines.append(f"明细：{len(result['issues'])} 条（issues_total={result.get('issues_total')}）")
    if result.get("artifacts"):
        for label, path in result["artifacts"].items():
            if isinstance(path, str) and path:
                lines.append(f"{label}: {path}")
    if result.get("timed_out"):
        lines.append(f"未在等待时间内完成；继续查询：ppt-audit status {result.get('task_id')}")
    limitations = result.get("limitations") or []
    if limitations:
        lines.append("限制：" + str(limitations[0]))
    return "\n".join(lines)
