"""Task orchestration: validate → parse → render → structure → visual → MLLM → merge → report."""
from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import (
    DATA_DIR, DEFECT_LABELS_ZH, DEFECT_TYPES, DEMO_TASKS_DIR, TASKS_DIR,
    MAX_CONCURRENT_TASKS, RENDER_LONG_EDGE, RENDER_TIMEOUT_S,
)
from ..storage import settings_store, store_for
from . import demo, merge, report, structure_rules, review_gate, recovery_runtime, task_lifecycle
from .mllm import review_pages
from .pptx_parser import InvalidPptxError, parse_document, sha256_file, validate_package
from .renderer import render_pptx
from .visual_classifier import VisualClassifierService

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS, thread_name_prefix="audit")
_running: set[str] = set()
_lock = threading.Lock()
_classifier = VisualClassifierService()

TERMINAL = {"done", "skipped", "unsupported", "failed"}


def task_dir_for(task: dict) -> Path:
    base = DEMO_TASKS_DIR if task["mode"] == "demo" else TASKS_DIR
    return base / task["id"]


def submit(task_id: str, mode: str) -> None:
    with _lock:
        if task_id in _running:
            return
        _running.add(task_id)
    task = store_for(mode).get(task_id)
    if task:
        task_lifecycle.write_owner(task_dir_for(task), phase='queued')
    _executor.submit(_runner, task_id, mode)


def _runner(task_id: str, mode: str, prepare_only: bool = False) -> None:
    try:
        if prepare_only:
            prepare_task(task_id, mode)
        else:
            run_task(task_id, mode)
    except Exception:
        store = store_for(mode)
        store.update(task_id, status="failed", error=traceback.format_exc()[-1500:])
    finally:
        with _lock:
            _running.discard(task_id)


def submit_prepare(task_id: str, mode: str) -> None:
    with _lock:
        if task_id in _running:
            return
        _running.add(task_id)
    task = store_for(mode).get(task_id)
    if task:
        task_lifecycle.write_owner(task_dir_for(task), phase='preparing')
    _executor.submit(_runner, task_id, mode, True)


class _Ctx:
    def __init__(self, task: dict) -> None:
        self.store = store_for(task["mode"])
        self.task_id = task["id"]
        self.task = task
        self.dir = task_dir_for(task)
        self.stages: list[dict] = task["stages"]

    def stage(self, key: str, status: str, message: str = "", progress: float | None = None,
              detail: dict | None = None) -> None:
        from ..storage import now_iso
        for st in self.stages:
            if st["key"] != key:
                continue
            if status == "running" and not st.get("started_at"):
                st["started_at"] = now_iso()
            st["status"] = status
            if message:
                st["message"] = message
            if progress is not None:
                st["progress"] = round(max(0.0, min(1.0, progress)), 3)
            if status in TERMINAL:
                st["finished_at"] = now_iso()
                st["progress"] = 1.0 if status in ("done", "skipped", "unsupported") else st.get("progress", 0)
            if detail:
                st["detail"] = detail
            break
        self.store.set_stage_bulk(self.task_id, self.stages)
        overall = sum(float(s.get("progress") or 0) for s in self.stages) / max(1, len(self.stages))
        self.store.update(self.task_id, progress=round(overall, 3))

    def message(self, text: str, progress: float | None = None) -> None:
        """Update the currently running stage without changing its status."""
        for st in self.stages:
            if st["status"] == "running":
                st["message"] = text
                if progress is not None:
                    st["progress"] = max(st.get("progress", 0), min(1.0, progress))
                break
        self.store.set_stage_bulk(self.task_id, self.stages)


def _wait_for_review(ctx: _Ctx, eligible: bool, reason: str | None) -> dict:
    gate=review_gate.prepare(ctx.dir,eligible,reason)
    if gate['state']=='waiting':
        ctx.stage('mllm','running','首轮检测已完成，等待5秒后自动LLM复检；可在进度页跳过。',.01,
                  {'review_gate':gate})
    previous=[None]
    def tick(current):
        seconds=(int(current.get('remaining_ms',0))+999)//1000
        if seconds!=previous[0]:
            previous[0]=seconds
            ctx.message(f'首轮已完成，{seconds}秒后自动LLM复检；可跳过查看未复检结果。',.01)
    return review_gate.wait_and_claim(ctx.dir,eligible,reason,on_tick=tick)


def prepare_task(task_id: str, mode: str) -> None:
    """Validate, parse and render only — used before the user confirms options."""
    store = store_for(mode)
    task = store.get(task_id)
    if not task or task["status"] not in ("draft", "queued"):
        return
    store.update(task_id, status="preparing", started_at=_now())
    task = store.get(task_id)  # type: ignore[assignment]
    ctx = _Ctx(task)
    task_lifecycle.write_owner(ctx.dir, phase='prepare')
    try:
        source = ctx.dir / "source" / "source.pptx"
        if not source.exists():
            raise FileNotFoundError("上传文件不存在。")
        ctx.stage("validate", "running", "校验文件容器与 OpenXML 结构…", 0.2)
        info = validate_package(source, 1024 * 1024 * 200, 200)
        digest = sha256_file(source)
        store.update(task_id, source_sha256=digest, size_bytes=info["size_bytes"],
                     slide_count=info["slide_count"])
        ctx.stage("validate", "done", f"OpenXML 校验通过，共 {info['slide_count']} 页，"
                                     f"{info['size_bytes']/1024/1024:.2f} MB。", 1.0)
        ctx.stage("parse", "running", "读取版式、文本框、字号与对象结构…", 0.2)
        document, detail = parse_document(source)
        document["file_name"] = task["source_name"] or document["file_name"]
        document["size_bytes"] = info["size_bytes"]
        (ctx.dir / "parse").mkdir(parents=True, exist_ok=True)
        (ctx.dir / "parse" / "slides_detail.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        document["sha256"] = digest
        store.update(task_id, document_json=document)
        ctx.stage("parse", "done",
                  f"解析完成：{document['slide_count']} 页，{document['aspect_label']}，"
                  f"{document['textboxes']} 个文本框、{document['pictures']} 张图片。", 1.0,
                  {"aspect": document["aspect_label"]})
        ctx.stage("render", "running", "按原始页面比例导出 PNG（长边 %d px）…" % RENDER_LONG_EDGE, 0.05)
        render_result = render_pptx(source, ctx.dir / "renders", RENDER_LONG_EDGE, RENDER_TIMEOUT_S)
        rendered = [x for x in render_result.get("slides", []) if x["status"] == "rendered"]
        failed = [x for x in render_result.get("slides", []) if x["status"] != "rendered"]
        (ctx.dir / "renders" / "slides.json").write_text(
            json.dumps({"result": render_result}, ensure_ascii=False, indent=1), encoding="utf-8")
        if not render_result.get("ok") and not rendered:
            ctx.stage("render", "failed",
                      render_result.get("error") or "PowerPoint 渲染失败，未生成页面图。", 0.0)
        else:
            ctx.stage("render", "done",
                      f"{len(rendered)} 页导出成功" + (f"，{len(failed)} 页失败。" if failed else "。"),
                      1.0, {"rendered": len(rendered), "failed": len(failed)})
        store.update(task_id, status="ready", progress=1.0)
    except InvalidPptxError as exc:
        _fail(ctx, exc.code, exc.message)
    except Exception as exc:
        _fail(ctx, "prepare_failed", f"文件准备失败：{exc}", traceback.format_exc())


def _prepared_artifacts(task: dict, directory: Path) -> dict | None:
    if not task.get("document"):
        return None
    if not recovery_runtime.prepared_is_valid(directory):
        return None
    slides_json = directory / "renders" / "slides.json"
    detail_json = directory / "parse" / "slides_detail.json"
    if not slides_json.exists() or not detail_json.exists():
        return None
    try:
        result = json.loads(slides_json.read_text(encoding="utf-8")).get("result") or {}
        detail = json.loads(detail_json.read_text(encoding="utf-8"))
        return {"document": task["document"], "detail": detail, "render": result}
    except (json.JSONDecodeError, OSError):
        return None


def run_task(task_id: str, mode: str) -> None:
    store = store_for(mode)
    task = store.get(task_id)
    if not task:
        return
    if task["status"] != "queued":
        return
    prepared = _prepared_artifacts(task, task_dir_for(task))
    store.update(task_id, status="running", started_at=task["started_at"] or _now(), error=None)
    task = store.get(task_id)  # type: ignore[assignment]
    ctx = _Ctx(task)
    task_lifecycle.write_owner(ctx.dir, phase='audit')
    options = task["options"]
    app_settings = settings_store.all()
    api_key = settings_store.get_api_key()
    is_demo = task["mode"] == "demo"
    scenario = task.get("demo_scenario") or "full"
    warnings: list[str] = []
    document: dict[str, Any] = {}
    detail: dict[str, Any] = {}
    issues: list[dict] = []
    risks: list[dict] = []
    visual_results: list[dict] = []
    reviews: list[dict] = []
    module_status: dict[str, Any] = {}
    claims: list[dict] = []
    review_plan: dict = {"items": []}
    six_result: dict = {}
    gate_token: str | None = None
    gate_decision: dict = {}

    try:
        if prepared:
            document = prepared["document"]
            detail = prepared["detail"]
            render_result = prepared["render"]
            rendered = [x for x in render_result.get("slides", []) if x["status"] == "rendered"]
            failed = [x for x in render_result.get("slides", []) if x["status"] != "rendered"]
            meta = render_result.get("meta") or {}
            for key in ("validate", "parse", "render"):
                st = next((x for x in ctx.stages if x["key"] == key), None)
                if st and st["status"] not in ("done", "failed"):
                    ctx.stage(key, "done", "复用上传准备阶段的结果。", 1.0)
            if not rendered:
                warnings.append("准备阶段渲染失败：视觉复核与页面预览不可用。")
        else:
            # -------------------------------------------------------------- validate
            ctx.stage("validate", "running", "校验文件容器与 OpenXML 结构…", 0.2)
            source = ctx.dir / "source" / "source.pptx"
            if not source.exists():
                raise FileNotFoundError("上传文件不存在（可能已被清理）。")
            info = validate_package(source, 1024 * 1024 * 200, 200)
            digest = sha256_file(source)
            if task.get("source_sha256") and digest != task["source_sha256"]:
                raise InvalidPptxError("sha_mismatch", "文件在审查前发生变化，已停止。")
            store.update(task_id, source_sha256=digest, size_bytes=info["size_bytes"],
                         slide_count=info["slide_count"])
            ctx.stage("validate", "done", f"OpenXML 校验通过，共 {info['slide_count']} 页，"
                                         f"{info['size_bytes']/1024/1024:.2f} MB。", 1.0)

            # ----------------------------------------------------------------- parse
            ctx.stage("parse", "running", "读取版式、文本框、字号与对象结构…", 0.15)
            document, detail = parse_document(source)
            document["file_name"] = task["source_name"] or document["file_name"]
            document["size_bytes"] = info["size_bytes"]
            detail_dir = ctx.dir / "parse"
            detail_dir.mkdir(parents=True, exist_ok=True)
            (detail_dir / "slides_detail.json").write_text(
                json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            document["sha256"] = digest
            store.update(task_id, document_json=document)
            ctx.stage("parse", "done",
                      f"解析完成：{document['slide_count']} 页，{document['aspect_label']}，"
                      f"{document['textboxes']} 个文本框、{document['pictures']} 张图片、"
                      f"{document['tables']} 个表格、{document['charts']} 个图表。", 1.0,
                      {"aspect": document["aspect_label"], "mode_counts": {
                          "editable": document["editable_pages"],
                          "mixed": document["mixed_pages"],
                          "image_only": document["image_only_pages"]}})

            # ---------------------------------------------------------------- render
            ctx.stage("render", "running", "按原始页面比例导出 PNG（长边 %d px）…" % RENDER_LONG_EDGE, 0.05)
            render_result = render_pptx(source, ctx.dir / "renders", RENDER_LONG_EDGE, RENDER_TIMEOUT_S)
            rendered = [s for s in render_result.get("slides", []) if s["status"] == "rendered"]
            failed = [s for s in render_result.get("slides", []) if s["status"] != "rendered"]
            meta = render_result.get("meta") or {}
            (ctx.dir / "renders" / "slides.json").write_text(
                json.dumps({"result": render_result}, ensure_ascii=False, indent=1), encoding="utf-8")
            if meta.get("source_unchanged") is False:
                warnings.append("渲染前后源文件哈希不一致，已记录异常。")
            if not render_result.get("ok") and not rendered:
                ctx.stage("render", "failed",
                          render_result.get("error") or "PowerPoint 渲染失败，未生成任何页面图。", 0.0,
                          {"error_code": render_result.get("error_code")})
                warnings.append("页面渲染失败：视觉检查与图片预览不可用。")
            elif failed:
                ctx.stage("render", "done",
                          f"{len(rendered)} 页导出成功，{len(failed)} 页失败；失败页在界面中标记为渲染失败。", 1.0,
                          {"rendered": len(rendered), "failed": len(failed),
                           "engine": meta.get("engine"), "pixel": [meta.get("pixel_width"), meta.get("pixel_height")]})
                warnings.append(f"{len(failed)} 页渲染失败。")
            else:
                ctx.stage("render", "done",
                          f"{len(rendered)} 页全部按比例导出（{meta.get('pixel_width')}×{meta.get('pixel_height')} px）。",
                          1.0, {"engine": meta.get("engine")})

        cached_screening = recovery_runtime.screening(ctx.dir, document, task_id)
        if cached_screening is not None:
            issues = cached_screening['issues']
            risks = cached_screening['risks']
            claims = cached_screening['claims']
            module_status['six_detector'] = cached_screening['capability']
            ctx.stage('structure', 'done', '复用已核验的首轮筛查与原始证据，不重复运行检测器。', 1.0,
                      {'reused_from_task_id': cached_screening['parent_task_id'], 'claims': len(claims)})
        else:
            # ------------------------------------------------------------- structure
            if options.get("enable_structure", True):
                ctx.stage("structure", "running", "运行对象越界、文本溢出、字号与遮挡规则…", 0.3)
                try:
                    native_contexts = json.loads((ctx.dir / "renders/native_contexts.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    native_contexts = []
                issues, risks = structure_rules.analyze(detail, document, native_contexts=native_contexts)
                ctx.stage("structure", "done",
                          f"产生 {len(issues)} 条候选、{len(risks)} 条结构提示。"
                          "所有候选均需人工或 AI 复核确认。", 1.0,
                          {"candidates": len(issues), "risks": len(risks)})
            else:
                ctx.stage("structure", "skipped", "用户关闭了结构规则检查。", 1.0)
                warnings.append("结构规则未运行，报告不包含几何候选。")

            # Real, isolated six-detector adapter uses this upload and its shared
            # render/measurement artefacts, never a research prediction CSV.
            from .six_detector import SixDetectorAdapter
            adapter = SixDetectorAdapter(enabled=bool(options.get("enable_six_detectors", True)))
            if options.get("enable_six_detectors", True):
                ctx.stage("structure", "running", "共享原稿、排版测量与原始页面图，执行六类专用检测…", .55)
                six_result = adapter.analyze(ctx.dir / "source" / "source.pptx", ctx.dir,
                                             document, detail, render_result, task_id)
                from ..schemas import DetectorClaim
                claims = [DetectorClaim.model_validate(claim).model_dump(mode="json") for claim in six_result.get("claims", [])]
                issues.extend(six_result.get("issues", []));risks.extend(six_result.get("risks", []))
                state = six_result.get("status", "unsupported")
                ctx.stage("structure", "done" if state in ("ok", "complete", "done", "partial") else "unsupported",
                          f"六类检查范围已记录：{len(claims)} 条原始观察；候选与未知分别保留。", 1.0,
                          {"six_status": state, "claims": len(claims), "candidates": len(issues)})
            else:
                six_result = adapter.fallback(document, task_id, "用户关闭六类专用检测。", "not_checked")
                claims = six_result.get("claims", [])
            module_status["six_detector"] = six_result.get("capability", {})
        analysis_dir = ctx.dir / "analysis";analysis_dir.mkdir(exist_ok=True)
        (analysis_dir / "claims.json").write_text(json.dumps({"claims": claims, "capability": module_status["six_detector"]}, ensure_ascii=False, indent=1), encoding="utf8")
        for issue in issues:
            issue.setdefault("candidate_id", issue["issue_id"])
        (analysis_dir / "observations.json").write_text(json.dumps(issues, ensure_ascii=False, indent=1), encoding="utf8")

        # ---------------------------------------------------------------- visual
        classifier_status = _classifier.status()
        module_status["visual"] = classifier_status
        cached_visual = recovery_runtime.visual(ctx.dir, classifier_status, int(document.get('slide_count') or 0)) if cached_screening and options.get('enable_visual') else None
        if cached_visual is not None:
            visual_results = [{**r, 'document_id': task_id} for r in cached_visual]
            ctx.stage('visual', 'done', '复用同版本、同图片的CLIP结果，不重复加载模型。', 1.0,
                      {'reused_from_task_id': cached_screening['parent_task_id']})
        elif is_demo:
            if not options.get("enable_visual", True):
                ctx.stage("visual", "skipped", "演示任务中关闭了视觉结果展示。", 1.0)
            else:
                ctx.stage("visual", "running", "载入演示用视觉结果（DEMO 占位）…", 0.5)
                visual_results = demo.visual_results(document["slide_count"], scenario)
                ctx.stage("visual", "done",
                          "演示模式：已载入固定演示概率（DEMO-FIXTURE，非真实模型）。", 1.0,
                          {"demo": True})
        elif not options.get("enable_visual", False):
            ctx.stage("visual", "skipped", "历史 CLIP 四类辅助复核未启用；默认使用 R2 六类筛查。", 1.0)
        elif not classifier_status["available"]:
            ctx.stage("visual", "unsupported", classifier_status["reason"], 1.0,
                      {"state": classifier_status["state"]})
            warnings.append("历史四标签 CLIP 分类器未启用，本次未产生 CLIP 分类预测；六类专用检测结果单独记录。")
        else:
            ctx.stage("visual", "running",
                      f"R2首筛后执行冻结 CLIP 四类辅助复核：{classifier_status['model_version']}…", 0.1)
            pages = [{"png": ctx.dir / "renders" / s["png"], "document_id": task_id,
                      "slide_index": s["index"]} for s in rendered]
            results = _classifier.predict_batch(pages, {"task_dir": str(ctx.dir)}) if pages else []
            results.extend({"status":"not_checked","slide_index":int(s['index']),"document_id":task_id,
                            "probabilities":{},"alerts":{},"error":"原始页面渲染失败，CLIP未检查。"} for s in failed)
            results.sort(key=lambda r:int(r['slide_index']))
            visual_results = results
            errors = [r for r in results if r["status"] != "ok"]
            ctx.stage("visual", "done" if results and not errors else "failed",
                      f"{len(results)-len(errors)} 页完成推理" +
                      (f"，{len(errors)} 页失败。" if errors else "。"), 1.0,
                      {"model_version": classifier_status.get("model_version")})

        # CLIP assistance must be available to the LLM, not attached only
        # after LLM execution. Preserve six-detector claims without alteration.
        issues = merge.attach_clip_reviews(issues, visual_results, int(document.get("slide_count") or 0))
        visual_state = next((s['status'] for s in ctx.stages if s['key']=='visual'), 'not_checked')
        (analysis_dir / "visual_results.json").write_text(json.dumps({
            "enabled": bool(options.get('enable_visual')), "results": visual_results,
            "status": "complete" if visual_results and all(r.get('status')=='ok' for r in visual_results) else
                      "partial" if visual_results else "not_checked", "stage_status": visual_state,
            "model": classifier_status}, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf8")

        recovery_runtime.save_screening(ctx.dir, task.get('source_sha256') or '', risks)

        # ------------------------------------------------------------------ MLLM
        mllm_enabled = bool(options.get("enable_mllm")) and options.get("review_mode", "off") != "off"
        mllm_state = {
            "enabled": mllm_enabled,
            "configured": bool(api_key and app_settings.get("api_base_url") and app_settings.get("api_model")),
            "model": app_settings.get("api_model") or "",
            "mode": options.get("review_mode", "off"),
        }
        module_status["mllm"] = mllm_state
        new_review_flow = bool(options.get("review_strategy") or options.get("llm_backend") == "mock" or
                               (options.get('enable_visual') and any(r.get('status')=='ok' for r in visual_results) and not is_demo))
        if new_review_flow:
            from .review_agent import plan_review, review_plan as execute_review_plan
            review_plan = plan_review(issues, claims, rendered, options, app_settings, visual_results=visual_results)
            (analysis_dir / "review_plan.json").write_text(json.dumps(review_plan, ensure_ascii=False, indent=1), encoding="utf8")

        execution_plan, cached_reviews, recovery_stats = recovery_runtime.split_reviews(ctx.dir, review_plan, options)
        reviews = cached_reviews
        if cached_reviews:
            mllm_state['reused_review_calls'] = len(cached_reviews)

        # The worker owns the persisted deadline and single execution claim.
        # Browser refresh/polling cannot start or repeat model calls.
        mock_review=options.get('llm_backend')=='mock'
        call_budget=min(int(options.get('max_api_calls',0)),int(app_settings.get('max_calls_per_task',8)))
        gate_reason=('review_disabled' if not mllm_enabled else
                     'api_not_configured' if not mock_review and not mllm_state['configured'] and not is_demo else
                     'demo_fixture' if is_demo and not new_review_flow else
                     'zero_budget' if call_budget<=0 or int(options.get('max_review_pages',0))<=0 else
                     'no_rendered_pages' if not rendered else
                     'no_eligible_review' if new_review_flow and not execution_plan.get('calls') else None)
        gate_decision=_wait_for_review(ctx,gate_reason is None,gate_reason)
        gate_token=gate_decision.get('execution_token')
        gate=gate_decision['gate']
        if gate.get('state') in ('executing','completed') and not gate_decision.get('execute'):
            raise RuntimeError('该任务已有LLM执行记录，未重复发送请求；请查看已有记录或创建新任务。')
        if gate.get('skipped_by_user'):
            mllm_enabled=False
            mllm_state['skipped_by_user']=True
            warnings.append(review_gate.WARNING)

        if new_review_flow:
            if not mllm_enabled:
                ctx.stage("mllm", "skipped", review_gate.WARNING if gate.get('skipped_by_user') else "未启用复核；候选、未决范围与调度原因已保留。", 1.0)
            elif options.get("llm_backend") != "mock" and not mllm_state["configured"]:
                ctx.stage("mllm", "skipped", "未配置真实模型服务；未发送页面。可显式选择模拟复核验证流程。", 1.0)
                warnings.append("复核服务未配置，调度计划仅记录，未调用外部服务。")
            elif not gate_decision.get('execute'):
                if cached_reviews and not execution_plan.get('calls'):
                    ctx.stage('mllm','done',f'已核验并复用 {len(cached_reviews)} 项成功复核，无需新增调用。',1.0)
                else:
                    ctx.stage('mllm','skipped','没有可执行的复检范围或预算；未完成LLM复检。',1.0)
            else:
                ctx.stage("mllm", "running", f"保留 {len(cached_reviews)} 项已完成复核，执行 {len(execution_plan.get('calls',[]))} 项待复核范围…", .1)
                executed_reviews = execute_review_plan(execution_plan, ctx.dir, options, app_settings, api_key)
                reviews = recovery_runtime.merge_review_attempt(review_plan, execution_plan, cached_reviews, executed_reviews)
                failures = [r for r in reviews if r.get("status") not in ("ok", "skipped", "not_checked")]
                review_errors=[{k:r.get(k) for k in ('slide_index','error','error_code')} for r in failures]
                failure_message=(f"{len(failures)}/{len(reviews)} 项复检失败：{failures[0].get('error') or failures[0].get('error_code') or '未知错误'}；失败范围未形成复核结论。" if failures else None)
                ctx.stage("mllm", "failed" if failures else "done",
                          failure_message or ("模拟复核链路完成，非真实模型判断。" if options.get("llm_backend") == "mock" else "按需复核执行完成。"), 1.0,
                          {"mock": options.get("llm_backend") == "mock", "records": len(reviews), "failed": len(failures), "review_errors":review_errors})
            if not gate_decision.get('execute'):
                recovery_runtime.record_unexecuted(review_plan,execution_plan,
                    'user_skipped' if gate.get('skipped_by_user') else 'review_disabled_or_unconfigured_or_zero_budget')
            (analysis_dir / "review_plan.json").write_text(json.dumps(review_plan, ensure_ascii=False, indent=1), encoding="utf8")
            (analysis_dir / "reviews.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=1), encoding="utf8")
        elif not mllm_enabled:
            ctx.stage("mllm", "skipped", review_gate.WARNING if gate.get('skipped_by_user') else "未启用云端复核，所有问题保持未复核状态。", 1.0)
        elif is_demo:
            if scenario == "module_missing":
                ctx.stage("mllm", "unsupported", "演示场景：多模态复核模块不可用。", 1.0)
            else:
                budget = min(int(options.get("max_api_calls") or 4),
                             int(options.get("max_review_pages") or 4), 4)
                selected = select_review_pages(options, detail, issues, visual_results,
                                               rendered, budget)
                pages = [{"slide_index": s["index"]} for s in selected]
                reviews = demo.reviews(pages, scenario)
                ok = sum(1 for r in reviews if r["status"] == "ok")
                ctx.stage("mllm", "done" if ok else "failed",
                          f"演示复核完成：{ok}/{len(reviews)} 页成功（DEMO 数据，未调用外部 API）。", 1.0,
                          {"demo": True, "pages": len(reviews)})
        elif not api_key or not app_settings.get("api_base_url") or not app_settings.get("api_model"):
            ctx.stage("mllm", "skipped", "未配置 API（Base URL / Key / 模型），已跳过云端复核。", 1.0)
            warnings.append("多模态复核已启用但 API 未配置，已跳过。")
        elif not gate_decision.get('execute'):
            ctx.stage('mllm','skipped','当前任务没有取得复检执行令牌，未发送模型请求。',1.0)
        else:
            ctx.stage("mllm", "running", "筛选需要复核的页面…", 0.05)
            budget = min(
                int(options.get("max_api_calls") or 0),
                int(options.get("max_review_pages") or 0),
                int(app_settings.get("max_calls_per_task") or 8),
            )
            selected = select_review_pages(options, detail, issues, visual_results,
                                           rendered, max(0, budget))
            if not selected:
                ctx.stage("mllm", "skipped", "按当前策略没有需要复核的页面（预算为 0 或无候选）。", 1.0)
            else:
                pages = build_review_payload(selected, detail, issues, visual_results, ctx.dir)
                ctx.stage("mllm", "running", f"正在复核 {len(pages)} 页…", 0.1)

                def _progress(done: int, total: int, result: dict) -> None:
                    ctx.message(f"AI 复核：{done}/{total} 页（第 {result.get('slide_index')} 页 "
                                f"{'成功' if result.get('status') == 'ok' else result.get('status')}）",
                                done / max(1, total))

                reviews = review_pages(
                    pages,
                    base_url=app_settings["api_base_url"], key=api_key,
                    model=app_settings["api_model"],
                    timeout=float(app_settings.get("api_timeout_s") or 60),
                    max_retries=int(app_settings.get("api_max_retries") or 0),
                    temperature=float(app_settings.get("api_temperature") or 0.2),
                    concurrency=int(app_settings.get("api_max_concurrency") or 2),
                    response_format_json=bool(app_settings.get("response_format_json", True)),
                    allow_private=bool(app_settings.get("allow_private_base_url", False)),
                    allow_insecure_http=bool(app_settings.get("allow_insecure_base_url", False)),
                    progress=_progress,
                )
                ok = sum(1 for r in reviews if r["status"] == "ok")
                failed_n = len(reviews) - ok
                ctx.stage("mllm", "done" if failed_n == 0 else "failed",
                          f"{ok} 页复核成功" + (f"，{failed_n} 页失败（已保留其他结果）。" if failed_n else "。"),
                          1.0, {"pages": len(reviews), "failed": failed_n})
                if failed_n:
                    warnings.append(f"{failed_n} 页 AI 复核失败，详见逐页记录。")

        if gate_token:
            mllm_stage=next(s for s in ctx.stages if s['key']=='mllm')
            outcome=('failed' if mllm_stage['status']=='failed' else 'mock_only' if mock_review else
                     'not_checked' if not reviews else 'completed')
            review_gate.complete(ctx.dir,gate_token,outcome)

        # ----------------------------------------------------------------- merge
        if not new_review_flow:
            # Old page-level API responses remain supported but are explicitly
            # scoped to one matching finding; they do not dismiss every local
            # anomaly of the same class on the page.
            for review in reviews:
                for verdict in review.get("verdicts", []):
                    target = next((i for i in issues if i["slide_index"] == review.get("slide_index") and i["defect_type"] == verdict.get("defect_type")), None)
                    if target:
                        verdict.setdefault("candidate_id", target["candidate_id"])
                    else:
                        verdict.setdefault("candidate_id", f"legacy-page-s{int(review.get('slide_index', 0)):04d}-{verdict.get('defect_type', 'unknown')}")
                    verdict["evidence_scope"] = "legacy_page_review"
        ctx.stage("merge", "running", "整合结构、视觉与复核证据…", 0.4)
        issues, summary = merge.merge(issues, visual_results, reviews,
                                      int(document.get("slide_count") or 0), claims=claims, review_plan=review_plan)
        for issue in issues:
            issue["document_id"] = task_id
        summary["render_failed"] = len(failed)
        summary["dpi_note"] = "renders are proportionally scaled to a 1920 px long edge"
        ctx.stage("merge", "done",
                  f"整合后 {len(issues)} 条问题，涉及 {summary['pages_with_issues']} 页；"
                  f"AI 支持 {summary['ai_supported']} 条、不成立 {summary['ai_rejected']} 条。", 1.0,
                  summary)

        # ---------------------------------------------------------------- report
        ctx.stage("report", "running", "生成 JSON 与 HTML 报告…", 0.4)
        task_now = store.get(task_id) or task
        task_now["options"] = options

        # Project the report stage as finished so the exported report does not
        # describe itself as "running" (it is written before the DB commit).
        projected = [dict(st) for st in ctx.stages]
        for st in projected:
            if st["key"] == "report":
                st.update({"status": "done", "progress": 1.0, "finished_at": _now(),
                           "message": "报告已生成：report.html / report.json。"})
        failed_stages = [st["label"] for st in projected if st["status"] == "failed"]
        final_status = "completed_with_errors" if failed_stages else "completed"
        task_now["status"] = final_status
        task_now["finished_at"] = _now()

        payload = report.build_payload(
            task=task_now, document=document, stages=projected, issues=issues,
            reviews=reviews, risks=risks, summary=summary, module_status=module_status,
            task_dir=ctx.dir, is_demo=is_demo,
        )
        recovery_info = recovery_runtime.context(ctx.dir)
        payload['lineage'] = recovery_runtime.lineage(ctx.dir)
        if recovery_info:
            actual_reused_stages=(['validate','parse','render'] if prepared else [])
            if cached_screening is not None:actual_reused_stages.append('structure')
            if cached_visual is not None:actual_reused_stages.append('visual')
            payload['recovery'] = {'parent_task_id': recovery_info.get('parent_task_id'),
                'action': recovery_info.get('action'), 'reused_stages': actual_reused_stages,
                'planned_reused_stages':recovery_info.get('reused_stages',[]),
                **recovery_stats, 'parent_usage': recovery_info.get('parent_usage'),
                'actual_new_http_calls': sum(int(r.get('actual_http_calls',0) or 0) for r in reviews)}
        payload["claims"] = claims
        payload["review_plan"] = review_plan
        payload["user_requirements"] = {k: options.get(k) for k in ("purpose", "style", "review_focus", "original_prompt")}
        payload["mock_review"] = options.get("llm_backend") == "mock"
        payload["visual_results"] = visual_results
        payload["review_gate"] = review_gate.read(ctx.dir)
        payload["verification_pipeline"] = report.verification_pipeline(options, projected, visual_results, reviews, review_plan,
                                                                       gate=payload['review_gate'])
        paths = report.write_reports(payload, ctx.dir)
        ctx.stage("report", "done",
                  f"报告已生成：{paths['html'].name} / {paths['json'].name}。", 1.0,
                  {"json": str(paths["json"].relative_to(DATA_DIR)),
                   "html": str(paths["html"].relative_to(DATA_DIR))})
        store.update(task_id, status=final_status, finished_at=_now(), progress=1.0,
                     summary_json=summary, warnings_json=warnings, error=None)
    except InvalidPptxError as exc:
        _fail(ctx, exc.code, exc.message)
    except Exception as exc:
        if gate_token:
            try:review_gate.complete(ctx.dir,gate_token,'failed',str(exc))
            except Exception:pass
        _fail(ctx, "internal_error", f"审查过程出错：{exc}", traceback.format_exc())


def _fail(ctx: _Ctx, code: str, message: str, tb: str | None = None) -> None:
    for st in ctx.stages:
        if st["status"] == "running":
            ctx.stage(st["key"], "failed", message, 0.0)
            break
    ctx.store.update(ctx.task_id, status="failed", finished_at=_now(),
                     error=f"{code}: {message}" + (f"\n{tb[-1200:]}" if tb else ""))


def _now() -> str:
    from ..storage import now_iso
    return now_iso()


def select_review_pages(options: dict, detail: dict, issues: list[dict],
                        visual_results: list[dict], rendered: list[dict],
                        budget: int) -> list[dict]:
    if budget <= 0:
        return []
    rendered_idx = {s["index"] for s in rendered}
    mode = options.get("review_mode", "risk_only")
    if mode == "manual":
        wanted = [p for p in options.get("manual_pages", []) if p in rendered_idx]
    elif mode == "all":
        wanted = sorted(rendered_idx)
    else:
        visual_alerts = {int(r["slide_index"]) for r in visual_results
                         if r.get("status") == "ok" and any(r.get("alerts", {}).values())}
        score: dict[int, int] = {}
        for issue in issues:
            weight = {"high": 3, "medium": 2, "low": 1}.get(issue.get("severity"), 1)
            score[issue["slide_index"]] = score.get(issue["slide_index"], 0) + weight
        for idx in visual_alerts:
            score[idx] = score.get(idx, 0) + 2
        wanted = [i for i, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))]
    return [{"index": i} for i in wanted[:budget]]


def build_review_payload(selected: list[dict], detail: dict, issues: list[dict],
                         visual_results: list[dict], task_dir: Path) -> list[dict]:
    visual_by_slide = {int(r["slide_index"]): r for r in visual_results if r.get("status") == "ok"}
    pages = []
    for item in selected:
        index = item["index"]
        candidates: dict[str, dict] = {}
        for issue in issues:
            if issue["slide_index"] != index:
                continue
            defect = issue["defect_type"]
            candidates.setdefault(defect, {
                "defect_type": defect,
                "evidence": issue["evidence_summary"][:160],
            })
        result = visual_by_slide.get(index)
        if result:
            for defect, alert in (result.get("alerts") or {}).items():
                if alert and defect not in candidates:
                    candidates[defect] = {
                        "defect_type": defect,
                        "evidence": f"视觉模型 p={result['probabilities'].get(defect, 0):.2f}（页面级）",
                    }
        pages.append({
            "slide_index": index,
            "png_path": task_dir / "renders" / f"s{index:04d}.png",
            "candidates": list(candidates.values())[:4],
        })
    return pages


def classifier_status() -> dict:
    return _classifier.status()
