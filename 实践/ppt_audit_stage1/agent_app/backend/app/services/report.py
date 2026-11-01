"""Report generation: structured JSON plus a self-contained HTML report."""
from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from PIL import Image

from ..config import DEFECT_LABELS_ZH, DEFECT_TYPES, TEMPLATE_DIR
from . import advice as advice_service
from . import merge, verdicts

STATUS_LABELS = {
    "queued": "排队中",
    "running": "审查中",
    "completed": "已完成",
    "completed_with_errors": "已完成（含失败模块）",
    "failed": "失败",
}
STAGE_STATUS_LABELS = {
    "pending": "等待中", "running": "运行中", "done": "已完成",
    "skipped": "已跳过", "unsupported": "未接入", "failed": "失败",
}
SOURCE_LABELS = {
    "structure_rule": "结构规则",
    "visual_model": "视觉模型",
    "mllm_review": "AI 复核",
    "six_detector": "六类专用检测",
}


def _thumb_b64(path: Path, width: int = 380) -> str | None:
    if not path.exists():
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            ratio = width / img.size[0]
            if ratio < 1:
                img = img.resize((width, max(1, round(img.size[1] * ratio))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=78, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _limitations(document: dict, stages: list[dict], summary: dict, is_demo: bool) -> list[str]:
    limits = [
        "结构规则基于 PPTX 对象几何与显式字号做出保守估算，输出的是“候选”，不等同于已确认缺陷或人工标注真值。",
        "旧结构规则的文字包络为字符数与字号估算；六类专用检测额外使用可取得的PowerPoint实际段落测量。测量失败范围保持未知，特殊字体、艺术字和可见字形仍需核对渲染图。",
    ]
    limits.extend([
        "六类专用检测保留实际测量、候选、未知、不支持与未执行范围；有序风险强度不等于缺陷概率。",
        "D6-A05尚不具备完整素材语义判定能力：局部ROI提示不等于已经识别出AI伪文字。",
        "未被选择或因预算跳过的范围仍未复核；按需调用不构成零漏检保证。",
    ])
    stage_map = {s["key"]: s for s in stages}
    visual = stage_map.get("visual", {})
    if visual.get("status") == "unsupported":
        limits.append("历史CLIP视觉分类模型尚未接入：本次没有产生CLIP四标签分类概率；"
                      "六类专用检测的候选与未知范围另行记录，未启用CLIP不代表这些检查未运行。")
    elif visual.get('status')=='skipped':
        limits.append('CLIP未启用：本次没有执行历史四标签辅助复核；六类专用检测结果单独保留。')
    mllm = stage_map.get("mllm", {})
    if mllm.get("status") in ("skipped", "unsupported"):
        limits.append("本次自动多模态复检未执行；未复核范围仍为候选，可能存在误诊或漏检。"
                      "后续局部追问请查看独立复核历史；新任务可配置API后保留默认复检。")
    if document.get("image_only_pages"):
        limits.append(f"{document['image_only_pages']} 页为整页位图，对象层没有可编辑文本，"
                      "结构规则对这类页面基本失效，只能依赖视觉观察。")
    if document.get("missing_fonts"):
        limits.append("文稿引用了本机未安装的字体，渲染时会被替换，可能导致换行、溢出与设计稿不一致。")
    if summary.get("ai_reviewed_pages"):
        limits.append("AI 复核结果是辅助判断，不是人工标签，不得写入独立测试集 ground truth。")
    if is_demo:
        limits.append("本报告为演示模式：文稿是系统生成的 fixture，视觉与复核结果为演示占位数据，"
                      "不可用于模型评估或研究结论。")
    limits.append("本系统不提供未经定义与验证的综合质量分数；请按类别、证据与来源分别阅读。")
    return limits


def build_payload(*, task: dict, document: dict, stages: list[dict], issues: list[dict],
                  reviews: list[dict], risks: list[dict], summary: dict,
                  module_status: dict, task_dir: Path, is_demo: bool) -> dict:
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    doc = dict(document)
    doc["size_mb"] = round((task.get("size_bytes") or 0) / (1024 * 1024), 2)
    doc["sha256_short"] = (task.get("source_sha256") or "")[:16]

    # A human "this is a misjudgement" verdict is projected here so every
    # downstream surface — counts, page list, advice, prompt — sees one truth.
    verdict_map = verdicts.load(task_dir)
    if verdict_map or any(i.get("user_verdict") for i in issues):
        issues = verdicts.annotate(deepcopy(issues), verdict_map)
        recomputed = merge.summarize_issues(issues, int(document.get("slide_count") or 0), reviews)
        summary = {**summary, **recomputed,
                   "detection_status_counts": summary.get("detection_status_counts") or {},
                   "visual_pages_scored": summary.get("visual_pages_scored", 0)}

    stage_map = {s["key"]: s for s in stages}
    visual_stage = stage_map.get("visual", {})
    mllm_stage = stage_map.get("mllm", {})

    def state_of(stage: dict, done_label: str = "已运行") -> tuple[str, str]:
        status = stage.get("status")
        if status == "done":
            return "done", done_label
        if status == "failed":
            return "fail", "失败"
        if status in ("unsupported", "skipped"):
            return "off", ("未接入" if status == "unsupported" else "已跳过")
        return "off", "未运行"

    modules = []
    for name, stage, done_label in (
        ("结构规则引擎", stage_map.get("structure", {}), "已运行"),
        ("视觉分类模型", visual_stage, "已运行"),
        ("多模态按需复核", mllm_stage, "已完成"),
    ):
        state_class, state_label = state_of(stage, done_label)
        modules.append({
            "name": name,
            "state_class": state_class,
            "state_label": state_label,
            "detail": stage.get("message") or "未运行",
        })

    max_count = max([1] + [summary["by_type"][d] for d in DEFECT_TYPES])
    distribution = [
        {"label": DEFECT_LABELS_ZH[d], "count": summary["by_type"][d],
         "pct": round(summary["by_type"][d] / max_count * 100, 1)}
        for d in DEFECT_TYPES
    ]

    by_page: dict[int, list[dict]] = {}
    for issue in issues:
        if not verdicts.is_visible(issue):
            continue
        by_page.setdefault(issue["slide_index"], []).append(issue)

    page_meta = {s["index"]: s for s in document.get("slides", [])}
    pages = []
    for index in sorted(by_page):
        prepared = []
        for issue in by_page[index]:
            sources = {s["source"] for s in issue.get("sources", [])}
            primary = "mllm_review" if "mllm_review" in sources else (
                "six_detector" if "six_detector" in sources else (
                "structure_rule" if "structure_rule" in sources else "visual_model"))
            coords = None
            if issue.get("bbox"):
                b = issue["bbox"]
                coords = (f"坐标（英寸）：x={b['x']:.2f} · y={b['y']:.2f} · "
                          f"w={b['w']:.2f} · h={b['h']:.2f}")
            review = issue.get("review_result") or {}
            review_badge = {
                "supported": "AI 复核支持", "rejected": "AI 复核不成立", "uncertain": "AI 复核不确定",
            }.get(issue.get("review_status"), "")
            if review_badge and (review.get("mock") or review.get("demo")):
                review_badge = "Mock 模拟复核 · 非真实模型判断"
            prepared.append({
                **issue,
                "source_label": SOURCE_LABELS[primary] + ("（多源）" if len(sources) > 1 else ""),
                "source_class": {"structure_rule": "rule", "six_detector": "rule", "visual_model": "model",
                                 "mllm_review": "ai"}[primary],
                "severity_label": {"high": "高风险", "medium": "中风险", "low": "低风险"}.get(
                    issue.get("severity"), "提示"),
                "severity_class": "high" if issue.get("severity") == "high" else "",
                "review_badge": review_badge,
                "ai_evidence": review.get("evidence") if review_badge else None,
                "coords": coords,
                "demo": bool(issue.get("demo")),
            })
        mode = page_meta.get(index, {}).get("content_mode", "editable")
        pages.append({
            "index": index,
            "issue_count": len(prepared),
            "issues": prepared,
            "mode_label": {"editable": "可编辑页", "mixed": "混合页", "image_only": "整页位图"}.get(mode, mode),
            "thumb": _thumb_b64(task_dir / "renders" / "thumbs" / f"s{index:04d}.jpg"),
        })

    risk_tags = {"structure_limited": "能力", "font_substitution": "字体", "render_issue": "渲染"}
    prepared_risks = [{**r, "tag": risk_tags.get(r.get("type", "structure_limited"), "提示"),
                       "title": r.get("title", "检测范围提示"), "detail": r.get("detail", r.get("message", "未判定范围"))} for r in risks]

    payload = {
        "schema_version": "4.4BC-report-1",
        "generated_at": generated,
        "is_demo": is_demo,
        "task": {
            "id": task["id"],
            "name": task["name"],
            "status": task["status"],
            "status_label": STATUS_LABELS.get(task["status"], task["status"]),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "mode": task.get("mode"),
            "options": task.get("options"),
        },
        "document": doc,
        "summary": summary,
        "modules": modules,
        "module_status": module_status,
        "distribution": distribution,
        "pages": pages,
        "issues": issues,
        "reviews": reviews,
        "risks": prepared_risks,
        "stages": [{**s, "status_label": STAGE_STATUS_LABELS.get(s["status"], s["status"])}
                   for s in stages],
        "limitations": _limitations(document, stages, summary, is_demo),
        "status_label": STATUS_LABELS.get(task["status"], task["status"]),
    }
    payload["advice"] = advice_service.overlay(advice_service.compose(payload), task_dir)
    return payload


def write_reports(payload: dict, task_dir: Path) -> dict[str, Path]:
    reports_dir = task_dir / "report"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "report.json"
    html_path = reports_dir / "report.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # This template ends in .html.j2, so extension-based defaults would
        # silently disable escaping for untrusted page text and user prompts.
        autoescape=True,
        trim_blocks=True, lstrip_blocks=True,
    )
    env.globals.update(defect_labels=DEFECT_LABELS_ZH, detection_states={
        'candidate': '候选问题', 'no_candidate_in_available_scope': '可判范围未检出',
        'unknown': '未知', 'unsupported': '不支持', 'not_checked': '未检查'})
    template = env.get_template("report.html.j2")
    html_path.write_text(template.render(report=payload), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def verification_pipeline(options: dict, stages: list[dict], visual_results: list[dict],
                          reviews: list[dict], plan: dict | None, *, gate: dict | None = None) -> dict:
    """Describe execution and outstanding review, never certify defect absence."""
    stage_map={s['key']:s for s in stages}
    clip_enabled=bool(options.get('enable_visual'))
    enabled=(bool(options.get('enable_mllm')) and options.get('review_mode','off')!='off') or bool(reviews)
    mode=('mock' if all(r.get('mock') for r in reviews) else 'api') if reviews else options.get('llm_backend','api') if enabled else 'disabled'
    remaining=sum(i.get('outcome')!='reviewed' for i in (plan or {}).get('items',[]))
    uncertain=sum(v.get('judgement')=='uncertain' for r in reviews if r.get('status')=='ok' for v in r.get('verdicts',[]))
    clip_incomplete=clip_enabled and (stage_map.get('visual',{}).get('status')!='done' or not visual_results or any(r.get('status')!='ok' for r in visual_results))
    six_incomplete=not options.get('enable_six_detectors',True) or stage_map.get('structure',{}).get('status')!='done'
    failures=any(r.get('status')!='ok' for r in reviews) or clip_incomplete or six_incomplete
    if (gate or {}).get('skipped_by_user'):final='user_skipped'
    elif not enabled:final='not_requested'
    elif not reviews:final='not_checked'
    elif mode=='mock':final='mock_only'
    elif failures or remaining:final='partial'
    elif uncertain:final='reviewed_with_uncertainty'
    else:final='planned_scope_reviewed'
    return {'clip_enabled':clip_enabled,'ordered_layers':[
        {'key':'six_detector','label':'R2 六类首筛','status':stage_map.get('structure',{}).get('status','not_checked') if options.get('enable_six_detectors',True) else 'not_checked','scope':'six_labels_with_explicit_unknowns'},
        {'key':'clip_four_label','label':'CLIP 四类辅助复核','status':stage_map.get('visual',{}).get('status','not_checked') if clip_enabled else 'skipped','scope':'original_four_labels_page_level'},
        {'key':'mllm','label':'LLM 按需复检','status':stage_map.get('mllm',{}).get('status','not_checked'),'scope':'selected_candidates_and_supplementary_scope'}],
        'clip_pages_scored':sum(r.get('status')=='ok' for r in visual_results),
        'clip_pages_failed':sum(r.get('status')!='ok' for r in visual_results),
        'llm_mode':mode,'llm_call_records':len(reviews),
        'llm_actual_http_calls':sum(int(r.get('actual_http_calls',0) or 0) for r in reviews if not r.get('mock')),
        'llm_unreviewed_items':remaining,'llm_uncertain_verdicts':uncertain,'final_review_status':final,
        'user_skipped_review':bool((gate or {}).get('skipped_by_user')),
        'skip_warning':(gate or {}).get('warning'),
        'note':'CLIP只提供四类页级辅助证据，阴性不否决R2，D5/D6不由CLIP复核。LLM关闭、失败、模拟、预算跳过和不确定范围均不能称为完整最终确认。'}
