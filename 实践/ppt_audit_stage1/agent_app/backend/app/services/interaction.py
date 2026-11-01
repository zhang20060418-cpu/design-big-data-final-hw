"""Finding-scoped re-review. Original claims and every prior judgement survive."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json,threading,uuid
from . import merge,report,verdicts

_locks=defaultdict(threading.RLock)

def history_for(directory: Path,issue_id: str | None=None) -> list[dict]:
    path=directory/'reviews/history.jsonl'
    if not path.exists():return []
    entries=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]
    return [e for e in entries if issue_id is None or e['issue_id']==issue_id]


def _rewrite(task: dict,directory: Path,payload: dict) -> dict:
    """Rebuild summary/reports from a stored payload without touching history."""
    base=dict(payload.get('summary') or {})
    issues=payload.get('issues') or []
    summary=merge.summarize_issues(issues,int(payload['document']['slide_count']),
        payload.get('reviews') or [],payload.get('claims') or [])
    summary['visual_pages_scored']=base.get('visual_pages_scored',0)
    summary={**base,**summary}
    updated=report.build_payload(task=task,document=payload['document'],stages=payload['stages'],
        issues=issues,reviews=payload.get('reviews') or [],risks=payload.get('risks') or [],
        summary=summary,module_status=payload.get('module_status') or {},task_dir=directory,
        is_demo=bool(payload.get('is_demo')))
    for key in ['claims','review_plan','user_requirements','mock_review','review_gate','recovery',
                'lineage','visual_results','verification_pipeline','interaction_history']:
        if key in payload:updated[key]=payload[key]
    report.write_reports(updated,directory)
    return updated


def rebuild(task: dict,directory: Path) -> dict:
    """Re-emit the report so cached advice and verdicts take effect."""
    with _locks[task['id']]:
        payload=json.loads((directory/'report/report.json').read_text(encoding='utf8'))
        return _rewrite(task,directory,payload)


def set_verdict(task: dict,directory: Path,issue_id: str,status: str,note: str='') -> dict:
    """Record or clear a human verdict, then rebuild every derived surface."""
    with _locks[task['id']]:
        payload=json.loads((directory/'report/report.json').read_text(encoding='utf8'))
        if not any(i.get('issue_id')==issue_id for i in payload.get('issues') or []):
            raise KeyError('问题ID不存在。')
        verdicts.record(directory,issue_id,status,note)
        updated=_rewrite(task,directory,payload)
        issue=next((i for i in updated['issues'] if i.get('issue_id')==issue_id),None)
        return dict(issue=issue,summary=updated['summary'],advice=updated.get('advice'),
                    verdicts=verdicts.load(directory))

def review_finding(task: dict,directory: Path,issue_id: str,question: str,backend: str,
                   settings: dict,api_key: str) -> dict:
    from .review_agent import review_single
    with _locks[task['id']]:
        path=directory/'report/report.json';payload=json.loads(path.read_text(encoding='utf8'))
        issue=next((i for i in payload['issues'] if i['issue_id']==issue_id),None)
        if issue is None:raise KeyError('问题ID不存在。')
        if backend=='api' and not all([api_key,settings.get('api_base_url'),settings.get('api_model')]):
            raise ValueError('尚未配置真实模型服务，请先配置或显式选择模拟复核。')
        options={**task.get('options',{}),'enable_mllm':True,'review_mode':'risk_only','review_strategy':'selective_review','llm_backend':backend,'max_api_calls':1,'max_review_pages':max(1,len(issue.get('related_slides',[])))}
        old_reviews=payload.get('reviews',[])
        spent=sum(int(r.get('actual_http_calls',r.get('http_calls',r.get('attempts',0))) or 0) for r in old_reviews if not r.get('mock'))
        task_cap=max(0,int(task.get('options',{}).get('max_api_calls',6)))
        settings_cap=max(0,int(settings.get('max_calls_per_task',8)))
        effective_cap=min(task_cap,settings_cap)
        if backend=='api' and spent>=effective_cap:
            raise ValueError(f'该任务的真实API调用预算已用完（累计 {spent}/{effective_cap}；任务上限 {task_cap}，配置上限 {settings_cap}）；原始候选与复核历史仍保留。')
        # A fresh one-call re-review cannot enlarge the lifetime real-HTTP cap.
        # Explicit Mock uses a separate single simulation slot and spends no
        # real calls even when the user's real-HTTP budget is zero.
        review_settings={**settings,'max_calls_per_task':1} if backend=='mock' else settings
        render_data=json.loads((directory/'renders/slides.json').read_text(encoding='utf8'))
        rendered=render_data.get('result',{}).get('slides',[])
        original=deepcopy(issue)
        response=review_single(issue,payload.get('claims',[]),question,rendered,directory,options,review_settings,api_key,
                               visual_results=payload.get('visual_results') or [])
        results=response.get('reviews',[]) if isinstance(response,dict) else response
        if not isinstance(results,list):raise ValueError('复核服务返回格式错误。')
        matches=[]
        ids={issue.get('candidate_id'),issue_id,*issue.get('merged_candidate_ids',[])}
        for result in results:
            for verdict in result.get('verdicts',[]):
                if result.get('status')=='ok' and (verdict.get('candidate_id') in ids or verdict.get('parent_candidate_id') in ids):matches.append((result,verdict))
        for result,verdict in matches:
            item={**deepcopy(verdict),'model':result.get('model'),'mock':bool(result.get('mock')),'rule_version':result.get('rule_version'),'question':question,'reviewed_at':datetime.now().astimezone().isoformat()}
            scoped_subtype=verdict.get('candidate_id') not in ids and verdict.get('parent_candidate_id') in ids
            item['scoped_subtype_review']=scoped_subtype
            # Rejecting A01 cannot reject A02..A05 or the parent D6 candidate.
            # The original parent verdict remains the authoritative broad scope.
            if not scoped_subtype:
                issue['review_status']=verdict['judgement'];issue['review_result']=item
            issue.setdefault('review_history',[]).append(item)
            issue.setdefault('sources',[]).append(dict(source='mllm_review',summary='用户补充意图后的复核：'+verdict['judgement'],model=result.get('model'),mock=bool(result.get('mock'))))
            if verdict.get('recommendation') and not scoped_subtype:issue['recommendation']=verdict['recommendation']
        if not matches:
            # Transport/JSON/budget failure is observable and never clears an alert.
            issue.setdefault('review_history',[]).append(dict(status='failed_or_not_checked',question=question,results=results))
        entry=dict(review_id=uuid.uuid4().hex,issue_id=issue_id,candidate_id=issue.get('candidate_id'),question=question,
            requested_backend=backend,created_at=datetime.now().astimezone().isoformat(),before=original,
            after=deepcopy(issue),results=results,plan=response.get('plan',{}) if isinstance(response,dict) else {},original_claims_preserved=True,
            budget=dict(task_max_api_calls=task_cap,settings_max_calls_per_task=settings_cap,effective_real_call_cap=effective_cap,
                        real_calls_before=spent,independent_mock=backend=='mock'))
        if matches:
            direct=[m for m in matches if m[1].get('candidate_id') in ids]
            last_result,last_verdict=(direct or matches)[-1]
            entry.update({**last_verdict,'model':last_result.get('model'),'mock':bool(last_result.get('mock')),
                          'rule_version':last_result.get('rule_version'),'reviewed_at':entry['created_at']})
        reviewdir=directory/'reviews';reviewdir.mkdir(exist_ok=True)
        initial=directory/'analysis/initial_report.json'
        if not initial.exists():initial.parent.mkdir(exist_ok=True);initial.write_text(path.read_text(encoding='utf8'),encoding='utf8')
        with (reviewdir/'history.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+'\n')
        updated_reviews=old_reviews+results
        summary=merge.summarize_issues(payload['issues'],int(payload['document']['slide_count']),updated_reviews,payload.get('claims',[]))
        summary['visual_pages_scored']=sum(r.get('status')=='ok' for r in (payload.get('visual_results') or []))
        updated=report.build_payload(task={**task,'status':task['status']},document=payload['document'],stages=payload['stages'],issues=payload['issues'],reviews=updated_reviews,
            risks=payload.get('risks',[]),summary=summary,module_status=payload.get('module_status',{}),task_dir=directory,is_demo=payload.get('is_demo',False))
        for key in ['claims','review_plan','user_requirements','mock_review','review_gate','recovery','lineage']:updated[key]=payload.get(key)
        if updated.get('recovery'):
            updated['recovery']=dict(updated['recovery'],actual_new_http_calls=sum(
                int(r.get('actual_http_calls',0) or 0) for r in updated_reviews))
        updated['visual_results']=payload.get('visual_results') or []
        updated['interaction_history']=history_for(directory)
        updated['mock_review']=bool(updated.get('mock_review') or backend=='mock')
        updated['verification_pipeline']=report.verification_pipeline(task.get('options',{}),payload['stages'],
            payload.get('visual_results') or [],updated_reviews,payload.get('review_plan'),gate=payload.get('review_gate'))
        report.write_reports(updated,directory)
        return dict(issue=issue,review=results[0] if results else dict(status='not_checked'),history=history_for(directory,issue_id),summary=summary)
