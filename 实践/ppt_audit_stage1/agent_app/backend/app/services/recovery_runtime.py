"""Verified reuse for a new audit attempt; parent artefacts are read-only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..config import DEFECT_TYPES


def _load(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return fallback


def context(directory: Path) -> dict:
    data=_load(directory/'analysis/recovery_context.json',{})
    return data if isinstance(data,dict) and data.get('action')=='continue' else {}


def lineage(directory: Path) -> dict | None:
    data=_load(directory/'analysis/recovery_context.json',{})
    if not isinstance(data,dict) or data.get('action') not in ('continue','restart'):return None
    return {k:data.get(k) for k in ('parent_task_id','parent_mode','action','reused_stages')}


def _artifacts_match(directory: Path, ctx: dict, prefixes: tuple[str,...]) -> bool:
    for name,expected in ctx.get('artifact_sha256',{}).items():
        if not name.replace('\\','/').startswith(prefixes):continue
        path=(directory/name).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():return False
        with path.open('rb') as fh:
            if hashlib.file_digest(fh,'sha256').hexdigest()!=expected:return False
    return True


def prepared_is_valid(directory: Path) -> bool:
    ctx=context(directory)
    if not ctx:return True
    source=directory/'source/source.pptx'
    return (source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest()==ctx.get('source_sha256')
            and _artifacts_match(directory,ctx,('renders/','parse/')))


def record_unexecuted(plan: dict, execution_plan: dict, reason: str) -> None:
    pending={cid for call in execution_plan.get('calls',[]) for cid in call.get('candidate_ids',[])}
    for item in plan.get('items',[]):
        if item.get('candidate_id') in pending:
            item['outcome']='not_checked'
            item.setdefault('execution_trace',[]).append({'stage':'execution','status':'not_checked','reason':reason})


def screening(directory: Path, document: dict, task_id: str) -> dict | None:
    ctx=context(directory)
    if not ctx.get('screening_reusable') or 'structure' not in ctx.get('reused_stages',[]):
        return None
    source=directory/'source/source.pptx'
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=ctx.get('source_sha256'):
        return None
    if not _artifacts_match(directory,ctx,('renders/','parse/','analysis/claims.json','analysis/observations.json')):
        return None
    data=_load(directory/'analysis/claims.json',{})
    issues=_load(directory/'analysis/observations.json')
    claims=data.get('claims',[]) if isinstance(data,dict) else []
    expected={(s,d) for s in range(1,int(document.get('slide_count',0))+1) for d in DEFECT_TYPES}
    if not isinstance(issues,list) or len(claims)!=len(expected) or {(c.get('slide_index'),c.get('defect_type')) for c in claims}!=expected:
        return None
    from ..schemas import DetectorClaim
    try:
        claims=[DetectorClaim.model_validate({**c,'document_id':task_id}).model_dump(mode='json') for c in claims]
    except (ValueError,TypeError):
        return None
    snapshot=_load(directory/'analysis/screening_snapshot.json',{})
    risks=snapshot.get('risks',ctx.get('parent_risks',[]))
    return {'issues':issues,'claims':claims,'risks':risks,
            'capability':data.get('capability',{}),'parent_task_id':ctx.get('parent_task_id')}


def visual(directory: Path, model: dict, slide_count: int) -> list | None:
    ctx=context(directory)
    if 'visual' not in ctx.get('reused_stages',[]):return None
    if not _artifacts_match(directory,ctx,('analysis/visual_results.json',)):return None
    saved=_load(directory/'analysis/visual_results.json',{})
    old_model=saved.get('model',{})
    if not saved.get('enabled') or old_model.get('model_version')!=model.get('model_version'):
        return None
    results=saved.get('results',[])
    if len(results)!=slide_count or {r.get('slide_index') for r in results}!=set(range(1,slide_count+1)):
        return None
    if any(r.get('status')!='ok' for r in results):return None
    return deepcopy(results)


def save_screening(directory: Path, source_sha256: str, risks: list) -> None:
    (directory/'analysis/screening_snapshot.json').write_text(json.dumps({
        'version':'screening-snapshot-1','source_sha256':source_sha256,'risks':risks},
        ensure_ascii=False,indent=1),encoding='utf8')


def _signature(call: dict):
    return (call.get('slide_index'),tuple(sorted(call.get('candidate_ids',[]))),
            tuple(sorted(call.get('related_slides',[]))))


def split_reviews(directory: Path, plan: dict, options: dict) -> tuple[dict,list[dict],dict]:
    """Reuse only schema-valid results over identical candidate/image evidence."""
    from . import review_agent
    ctx=context(directory)
    pending=deepcopy(plan)
    stats={'reused_review_calls':0,'pending_review_calls':len(plan.get('calls',[])),
           'cache_rejections':[],'parent_task_id':ctx.get('parent_task_id')}
    if not ctx.get('screening_reusable'):return pending,[],stats
    old=ctx.get('cached_review_plan') or {}
    old_items={i['candidate_id']:i for i in old.get('items',[])}
    new_items={i['candidate_id']:i for i in plan.get('items',[])}
    old_calls={c['call_id']:c for c in old.get('calls',[])}
    available={_signature(old_calls[r['call_id']]):r for r in ctx.get('cached_reviews',[])
               if r.get('status')=='ok' and r.get('call_id') in old_calls}
    reused=[];remaining=[]
    fact_keys=('candidate_id','slide_index','defect_type','subtype','raw_status','object_ids',
               'normalized_bbox','evidence','roi_boxes','clip_reviews','parent_candidate_id')
    for call in plan.get('calls',[]):
        cached=available.get(_signature(call))
        if cached is None:
            remaining.append(call);continue
        try:
            if cached.get('rule_version')!=review_agent.RULE_VERSION:
                raise ValueError('rule_version_changed')
            if bool(cached.get('mock'))!=(options.get('llm_backend')=='mock'):
                raise ValueError('review_backend_changed')
            ids=call['candidate_ids'];items=[new_items[c] for c in ids]
            if any({k:new_items[c].get(k) for k in fact_keys}!={k:old_items.get(c,{}).get(k) for k in fact_keys} for c in ids):
                raise ValueError('candidate_evidence_changed')
            images=review_agent._images(call,items,directory)
            actual={i['evidence_id']:i['sha256'] for i in images}
            prior={i['evidence_id']:i['sha256'] for i in cached.get('evidence_inventory',[])}
            if actual!=prior or not actual:raise ValueError('image_evidence_hash_changed')
            verdicts=[{k:v for k,v in row.items() if k!='parent_candidate_id'} for row in cached.get('verdicts',[])]
            clean=review_agent.validate_review({'verdicts':verdicts,
                'overall_note':cached.get('overall_note',''),
                'task_alignment_note':cached.get('task_alignment_note','')},items,set(actual))
            result=deepcopy(cached)
            result.update(**clean,call_id=call['call_id'],cached_result=True,
                reused_from_task_id=ctx.get('parent_task_id'),
                original_http_calls=cached.get('original_http_calls',cached.get('actual_http_calls',0)),
                original_usage=cached.get('original_usage',cached.get('usage')),
                actual_http_calls=0,attempts=0,usage=None,cost=None,elapsed_ms=0,latency_ms=0)
            result.setdefault('execution_trace',[]).append({'stage':'recovery','status':'reused_verified_result','new_http_calls':0})
            reused.append(result)
            for item in plan.get('items',[]):
                if item['candidate_id'] in ids:
                    item['outcome']='reviewed'
                    item.setdefault('execution_trace',[]).append({'stage':'recovery','status':'reused_verified_result','parent_task_id':ctx.get('parent_task_id')})
        except (ValueError,KeyError,TypeError,OSError) as exc:
            stats['cache_rejections'].append({'call_id':call['call_id'],'reason':str(exc)[:300]})
            remaining.append(call)
    pending['calls']=deepcopy(remaining)
    pending['planned_calls']=len(remaining)
    stats.update(reused_review_calls=len(reused),pending_review_calls=len(remaining))
    return pending,reused,stats


def merge_review_attempt(plan: dict, execution_plan: dict, reused: list, executed: list) -> list:
    updates={i['candidate_id']:i for i in execution_plan.get('items',[]) if i.get('call_id') in {c['call_id'] for c in execution_plan.get('calls',[])}}
    for item in plan.get('items',[]):
        if item['candidate_id'] in updates:item.update(deepcopy(updates[item['candidate_id']]))
    order={c['call_id']:n for n,c in enumerate(plan.get('calls',[]))}
    return sorted(reused+executed,key=lambda r:order.get(r.get('call_id'),9999))
