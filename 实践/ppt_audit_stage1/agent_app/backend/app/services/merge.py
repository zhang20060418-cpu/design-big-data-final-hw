"""Layer 4 — evidence integration.

The merge keeps sources distinguishable.  It never invents a combined
confidence score: an issue records which sources support it, and an AI verdict
is attached as a page-level review of that specific defect type.
"""
from __future__ import annotations

from typing import Any
from copy import deepcopy

from ..config import DEFECT_LABELS_ZH, DEFECT_TYPES
from .verdicts import is_dismissed, is_visible

DEFAULT_RECOMMENDATION = {
    "improper_font_sizing": "把关键正文提升到 12pt 以上，必要时拆页或删减文字。",
    "occluded_content": "调整图层顺序或移动遮挡对象，确保正文完整可见。",
    "cluttered_layout": "按信息层级拆页，增大分组间距，减少同层并列元素。",
    "content_overflow_cutoff": "把对象移回画布安全区，或缩短文本、缩小字号。",
    "excessive_whitespace": "结合页面用途核对有效内容的位置、尺度和留白；确认后再调整信息模块。",
    "generative_design_anomaly": "结合相关页面核对重复结构、装饰和素材可读性，保留必要的模板一致性。",
}


def _find_group(index: dict, slide: int, defect: str) -> list[dict]:
    return index.setdefault((slide, defect), [])


def merge(structure_issues: list[dict], visual_results: list[dict],
          reviews: list[dict], slide_count: int, claims: list[dict] | None = None,
          review_plan: dict | None = None) -> tuple[list[dict], dict[str, Any]]:
    if claims is not None:
        return _merge_six(structure_issues, visual_results, reviews, slide_count, claims, review_plan)
    index: dict[tuple[int, str], list[dict]] = {}

    for issue in structure_issues:
        issue["sources"] = [{
            "source": "structure_rule",
            "rule_id": issue.get("rule_id"),
            "summary": issue.get("evidence_summary"),
        }]
        issue["review_scope"] = "element" if issue.get("bbox") else "page"
        index.setdefault((issue["slide_index"], issue["defect_type"]), []).append(issue)

    # ------------------------------------------------------------ visual model
    visual_used = 0
    for result in visual_results:
        if result.get("status") != "ok":
            continue
        visual_used += 1
        slide = int(result["slide_index"])
        for defect in DEFECT_TYPES:
            if not result.get("alerts", {}).get(defect):
                continue
            prob = float(result["probabilities"].get(defect, 0))
            group = index.get((slide, defect))
            if group:
                for issue in group:
                    issue["sources"].append({
                        "source": "visual_model",
                        "probability": prob,
                        "model_version": result.get("model_version"),
                        "summary": f"视觉模型输出 p={prob:.2f}，仅页面级，无元素坐标。",
                    })
                    issue["visual_probability"] = prob
                    issue["confidence"] = "multi_source"
                    issue["demo"] = bool(result.get("demo"))
            else:
                issue = {
                    "issue_id": f"VM-s{slide:04d}-{defect[:6]}",
                    "document_id": None,
                    "slide_index": slide,
                    "defect_type": defect,
                    "title": f"视觉模型告警：{DEFECT_LABELS_ZH[defect]}",
                    "evidence_source": "visual_model",
                    "rule_id": None,
                    "rule_name": None,
                    "evidence_summary": f"视觉模型整页输出 p={prob:.2f}（阈值见模型工作点），"
                                        "仅页面级风险，模型不提供元素坐标。",
                    "element_id": None,
                    "bbox": None,
                    "coordinate_space": None,
                    "visual_probability": prob,
                    "severity": "high" if prob >= 0.8 else "medium",
                    "confidence": "model_only",
                    "review_status": "unreviewed",
                    "review_result": None,
                    "recommendation": DEFAULT_RECOMMENDATION[defect],
                    "execution_status": "alert",
                    "details": {"model_version": result.get("model_version"),
                                "work_point": result.get("work_point")},
                    "sources": [{
                        "source": "visual_model",
                        "probability": prob,
                        "model_version": result.get("model_version"),
                        "summary": f"视觉模型 p={prob:.2f}（页面级）",
                    }],
                    "review_scope": "page",
                    "demo": bool(result.get("demo")),
                }
                index[(slide, defect)] = [issue]

    # ------------------------------------------------------------- AI review
    reviewed = supported = rejected = 0
    for review in reviews:
        slide = int(review.get("slide_index", 0))
        if review.get("status") != "ok":
            continue
        reviewed += 1
        for verdict in review.get("verdicts", []):
            defect = verdict["defect_type"]
            group = index.get((slide, defect)) or []
            if group:
                for issue in group:
                    issue["review_status"] = (
                        "supported" if verdict["judgement"] == "supported"
                        else "rejected" if verdict["judgement"] == "rejected"
                        else "uncertain"
                    )
                    issue["review_result"] = {
                        "judgement": verdict["judgement"],
                        "evidence": verdict.get("evidence"),
                        "recommendation": verdict.get("recommendation"),
                        "confidence": verdict.get("confidence"),
                        "model": review.get("model"),
                        "demo": bool(review.get("demo")),
                    }
                    issue["sources"].append({
                        "source": "mllm_review",
                        "summary": f"AI 复核：{verdict['judgement']}",
                        "model": review.get("model"),
                    })
                    if verdict.get("recommendation"):
                        issue["recommendation"] = verdict["recommendation"]
                if verdict["judgement"] == "supported":
                    supported += 1
                elif verdict["judgement"] == "rejected":
                    rejected += 1
            elif verdict["judgement"] == "supported":
                issue = {
                    "issue_id": f"AI-s{slide:04d}-{defect[:6]}",
                    "document_id": None,
                    "slide_index": slide,
                    "defect_type": defect,
                    "title": f"AI 复核提出：{DEFECT_LABELS_ZH[defect]}",
                    "evidence_source": "mllm_review",
                    "rule_id": None,
                    "rule_name": None,
                    "evidence_summary": verdict.get("evidence") or "AI 复核认为存在该缺陷。",
                    "element_id": None,
                    "bbox": None,
                    "coordinate_space": None,
                    "visual_probability": None,
                    "severity": "medium",
                    "confidence": "ai_only",
                    "review_status": "supported",
                    "review_result": {
                        "judgement": "supported",
                        "evidence": verdict.get("evidence"),
                        "recommendation": verdict.get("recommendation"),
                        "confidence": verdict.get("confidence"),
                        "model": review.get("model"),
                        "demo": bool(review.get("demo")),
                    },
                    "recommendation": verdict.get("recommendation") or DEFAULT_RECOMMENDATION[defect],
                    "execution_status": "ai_reported",
                    "details": {"note": "该问题没有结构规则或视觉模型证据，仅由 AI 复核提出。"},
                    "sources": [{"source": "mllm_review", "summary": "AI 复核：supported",
                                 "model": review.get("model")}],
                    "review_scope": "page",
                    "demo": bool(review.get("demo")),
                }
                index[(slide, defect)] = [issue]
                supported += 1

    issues: list[dict] = []
    for group in index.values():
        issues.extend(group)
    issues.sort(key=lambda i: (i["slide_index"], i["defect_type"], i.get("issue_id", "")))

    pages_with_issues = sorted({i["slide_index"] for i in issues if i["review_status"] != "rejected"})
    high_risk = sorted({i["slide_index"] for i in issues
                        if i["severity"] == "high" and i["review_status"] != "rejected"})
    by_type = {d: sum(1 for i in issues if i["defect_type"] == d and i["review_status"] != "rejected")
               for d in DEFECT_TYPES}
    summary = {
        "slide_count": slide_count,
        "issue_count": len(issues),
        "visible_issue_count": sum(1 for i in issues if i["review_status"] != "rejected"),
        "rejected_count": sum(1 for i in issues if i["review_status"] == "rejected"),
        "pages_with_issues": len(pages_with_issues),
        "high_risk_pages": high_risk,
        "by_type": by_type,
        "by_source": {
            "structure_rule": sum(1 for i in issues if any(s["source"] == "structure_rule" for s in i["sources"])),
            "visual_model": sum(1 for i in issues if any(s["source"] == "visual_model" for s in i["sources"])),
            "mllm_review": sum(1 for i in issues if any(s["source"] == "mllm_review" for s in i["sources"])),
        },
        "ai_reviewed_pages": reviewed,
        "ai_supported": supported,
        "ai_rejected": rejected,
        "visual_pages_scored": visual_used,
        "unreviewed_count": sum(1 for i in issues if i["review_status"] == "unreviewed"),
    }
    return issues, summary


def _overlap(a: dict | None, b: dict | None) -> float:
    if not a or not b:
        return 0.
    hit = max(0., min(a['x'] + a['w'], b['x'] + b['w']) - max(a['x'], b['x'])) * max(0., min(a['y'] + a['h'], b['y'] + b['h']) - max(a['y'], b['y']))
    return hit / max(1e-12, min(a['w'] * a['h'], b['w'] * b['h']))


def summarize_issues(issues: list[dict], slide_count: int, reviews: list[dict] | None = None,
                     claims: list[dict] | None = None) -> dict:
    visible = [i for i in issues if is_visible(i)]
    return dict(slide_count=slide_count, issue_count=len(issues), visible_issue_count=len(visible),
        rejected_count=sum(i.get('review_status') == 'rejected' for i in issues),
        dismissed_count=sum(is_dismissed(i) for i in issues),
        pages_with_issues=len({i['slide_index'] for i in visible}),
        high_risk_pages=sorted({i['slide_index'] for i in visible if i.get('severity') == 'high'}),
        by_type={d:sum(i['defect_type']==d for i in visible) for d in DEFECT_TYPES},
        by_source={s:sum(any(v['source']==s for v in i.get('sources', [])) for i in issues) for s in ['structure_rule','six_detector','visual_model','mllm_review']},
        ai_reviewed_pages=len({r.get('slide_index') for r in reviews or [] if r.get('status')=='ok'}),
        ai_supported=sum(i.get('review_status')=='supported' for i in issues),
        ai_rejected=sum(i.get('review_status')=='rejected' for i in issues), visual_pages_scored=0,
        unreviewed_count=sum(i.get('review_status')=='unreviewed' for i in visible),
        detection_status_counts={s:sum(c.get('raw_status')==s for c in claims or []) for s in ['candidate','no_candidate_in_available_scope','unknown','unsupported','not_checked']},
        unknown_is_normal=False)


def _merge_six(raw: list[dict], visual: list[dict], reviews: list[dict], slide_count: int,
               claims: list[dict], plan: dict | None) -> tuple[list[dict], dict]:
    """Keep immutable observations; merge only shared type/location witnesses.

    Candidate-level reviews never reject every issue of a type on a page.
    Source disagreements stay attached and do not erase detector observations.
    """
    issues=[]
    for original in raw:
        item=deepcopy(original)
        item.setdefault('candidate_id', item.get('issue_id'))
        item.setdefault('raw_status','candidate');item.setdefault('review_history',[])
        item.setdefault('initial_conclusion',dict(raw_status=item['raw_status'],evidence_summary=item.get('evidence_summary')))
        item.setdefault('sources',[dict(source=item.get('evidence_source','structure_rule'),rule_id=item.get('rule_id'),summary=item.get('evidence_summary'))])
        item.setdefault('related_slides',[item['slide_index']]);item.setdefault('review_status','unreviewed');item.setdefault('review_result',None)
        item.setdefault('confidence','candidate');item.setdefault('severity','medium');item.setdefault('recommendation',DEFAULT_RECOMMENDATION.get(item['defect_type'],''))
        item.setdefault('visual_probability',None);item.setdefault('bbox',None);item.setdefault('element_id',None)
        item.setdefault('rule_id',None);item.setdefault('evidence_summary','');item.setdefault('review_scope','element' if item.get('bbox') else 'page')
        witnesses=[c for c in claims if c.get('candidate_id')==item['candidate_id']]
        if not witnesses:
            witnesses=[c for c in claims if c.get('slide_index')==item['slide_index'] and c.get('defect_type')==item['defect_type']]
        item['claims']=deepcopy(witnesses)
        if witnesses and witnesses[0].get('candidate_id')==item['candidate_id']:
            item['raw_status']=witnesses[0]['raw_status']
            item.setdefault('detector_version',witnesses[0].get('detector_version'))
            item.setdefault('limitations',deepcopy(witnesses[0].get('limitations',[])))
        item.setdefault('source_conflicts',[])
        for witness in witnesses:
            if witness.get('raw_status') in {'no_candidate_in_available_scope','unknown','unsupported','not_checked'}:
                item['source_conflicts'].append(dict(kind='different_scope_or_evidence_strength',candidate_id=witness.get('candidate_id'),raw_status=witness.get('raw_status'),note='保留各自可判范围；未检出不等于正常。'))
        item.setdefault('locations', [dict(slide_index=item['slide_index'],bbox=item.get('bbox'),normalized_bbox=item.get('normalized_bbox'))])
        # Numeric geometry is compared only within the same coordinate space.
        previous=next((i for i in issues if i['slide_index']==item['slide_index'] and i['defect_type']==item['defect_type'] and (
            i['candidate_id']==item['candidate_id'] or
            bool(i.get('element_id') and i.get('element_id')==item.get('element_id')) or
            bool(i.get('bbox') and item.get('bbox') and i.get('coordinate_space')==item.get('coordinate_space') and _overlap(i['bbox'],item['bbox'])>=.6))),None)
        if previous:
            previous['sources'].extend(item['sources']);previous['claims'].extend(item['claims']);previous['locations'].extend(item['locations'])
            previous.setdefault('merged_candidate_ids',[previous['candidate_id']]).append(item['candidate_id'])
            previous['related_slides']=sorted(set(previous['related_slides']+item['related_slides']))
            previous.setdefault('source_conflicts',[])
            previous['source_conflicts'].extend([dict(kind='multiple_source_observations_preserved',candidate_id=item['candidate_id'],raw_status=item['raw_status'])])
        else:issues.append(item)
    # The legacy visual plugin is optional; do not substitute ordinal scores
    # for its calibrated probability interface or silently enable CLIP C.
    if visual:
        issues=attach_clip_reviews(issues,visual,slide_count)
    for review in reviews:
        if review.get('status')!='ok':continue
        for verdict in review.get('verdicts',[]):
            cid=verdict.get('candidate_id')
            if not cid:continue
            matching=[i for i in issues if cid in [i.get('candidate_id'),i.get('issue_id'),*i.get('merged_candidate_ids',[])]]
            parent_id=verdict.get('parent_candidate_id')
            subtype_only=False
            if not matching and parent_id:
                matching=[i for i in issues if parent_id in [i.get('candidate_id'),i.get('issue_id'),*i.get('merged_candidate_ids',[])]]
                subtype_only=bool(matching)
            if not matching and verdict.get('judgement')=='supported' and verdict.get('defect_type') in DEFECT_TYPES:
                matching=[dict(issue_id=cid,candidate_id=cid,document_id=None,slide_index=review['slide_index'],defect_type=verdict['defect_type'],
                    title='复核补充：'+DEFECT_LABELS_ZH[verdict['defect_type']],evidence_source='mllm_review',evidence_summary=verdict.get('evidence',''),
                    rule_id=None,element_id=None,bbox=None,normalized_bbox=None,visual_probability=None,severity=verdict.get('severity','medium'),confidence='ai_only',
                    recommendation=verdict.get('recommendation',''),review_history=[],claims=[],sources=[],related_slides=review.get('related_slides') or [review['slide_index']],raw_status='unknown')]
                issues.extend(matching)
            for item in matching:
                result={**deepcopy(verdict),'model':review.get('model'),'mock':bool(review.get('mock')),'demo':bool(review.get('demo')),'rule_version':review.get('rule_version'),'execution_trace':review.get('execution_trace',[])}
                if subtype_only:
                    # A05 uncertainty must not overwrite a separately reviewed
                    # A01 or the parent D6 finding. Preserve each child verdict.
                    item.setdefault('subtype_reviews',[]).append(result)
                    item['review_history'].append(result)
                    continue
                item['review_status']=verdict['judgement'];item['review_result']=result;item['review_history'].append(result)
                item['sources'].append(dict(source='mllm_review',summary=verdict['judgement'],model=review.get('model'),mock=bool(review.get('mock'))))
                if verdict.get('recommendation'):item['recommendation']=verdict['recommendation']
    issues.sort(key=lambda i:(i['slide_index'],i['defect_type'],i['issue_id']))
    summary=summarize_issues(issues,slide_count,reviews,claims)
    summary['visual_pages_scored']=sum(r.get('status')=='ok' for r in visual)
    if plan is not None:summary['review_planning']=dict(selected=sum(bool(i.get('selected')) for i in plan.get('items',[])),total=len(plan.get('items',[])))
    return issues,summary


def attach_clip_reviews(raw: list[dict], visual: list[dict], slide_count: int) -> list[dict]:
    """Attach page-level four-label assistance before LLM planning, never veto.

    Negative CLIP evidence stays available for disagreement review. It cannot
    erase localized native claims or supply evidence for D5/D6. This is
    idempotent because final fusion may receive these observations again.
    """
    issues=deepcopy(raw)
    additions,_=merge([],visual,[],slide_count)
    for item in additions:
        if not any(i['slide_index']==item['slide_index'] and i['defect_type']==item['defect_type'] for i in issues):
            item['candidate_id']=item['issue_id']
            item['raw_status']='candidate'
            item['normalized_bbox']=None
            item['related_slides']=[item['slide_index']]
            item['initial_conclusion']={'raw_status':'candidate','evidence_summary':item['evidence_summary']}
            issues.append(item)
    for result in visual:
        if result.get('status')!='ok':continue
        for target in issues:
            defect=target['defect_type']
            if target['slide_index']!=int(result['slide_index']) or defect not in DEFECT_TYPES[:4]:continue
            if defect not in result.get('probabilities',{}):continue
            assistance={'status':'ok','defect_type':defect,'scope':'page','source':'visual_model',
                'probability':float(result['probabilities'][defect]),
                'threshold':result.get('thresholds',{}).get(defect),
                'alert':result.get('alerts',{}).get(defect),
                'model_version':result.get('model_version'),'model_revision':result.get('model_revision'),
                'work_point':result.get('work_point'),'limitations':deepcopy(result.get('limitations',[]))}
            target['clip_review']=assistance
            target['visual_probability']=assistance['probability']
            sources=target.setdefault('sources',[{'source':target.get('evidence_source','structure_rule'),
                        'summary':target.get('evidence_summary','')}])
            sources[:]=[s for s in sources if s.get('source')!='visual_model']
            sources.append({**deepcopy(assistance),'summary':f"CLIP四类辅助：p={assistance['probability']:.4f}；仅页级证据，不能否决原生候选。"})
            if assistance['alert'] in (False,0) and target.get('raw_status','candidate')=='candidate':
                target['evidence_conflict']=True
                disagreement={'kind':'native_candidate_clip_below_threshold','scope':'page_vs_native_evidence',
                              'note':'CLIP低于冻结阈值；保留原候选，交由LLM或人工核对，不视为正常。'}
                conflicts=target.setdefault('source_conflicts',[])
                if disagreement not in conflicts:conflicts.append(disagreement)
    return issues
