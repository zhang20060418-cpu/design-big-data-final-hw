"""Explicit, one-pending-call live continuation with strict cache preflight."""
import argparse
from pathlib import Path
import sys
import json
import hashlib

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
from app.storage import real_store
from app.services import recovery, recovery_runtime, review_agent, orchestrator, merge
from app.storage import settings_store


def digest(path):
    with path.open('rb') as fh:return hashlib.file_digest(fh,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--parent-task',required=True)
    args=parser.parse_args()
    parent=real_store.get(args.parent_task)
    assert parent and parent['status']=='completed_with_errors'
    source=recovery.directory(parent)
    protected={str(p.relative_to(source)):digest(p) for folder in ['source','parse','renders','analysis','report','six_detector']
               for p in (source/folder).rglob('*') if p.is_file()}
    response,created=recovery.create_attempt(parent,'continue',
        idempotency_key='verify_20261031_evidence_p6')
    child=real_store.get(response['task_id']);directory=recovery.directory(child)
    print('recovery_task='+child['id'],flush=True)
    if created:
        cached=recovery_runtime.screening(directory,child['document'],child['id'])
        assert cached is not None,'Refusing live calls without validated screening reuse'
        rendered=json.loads((directory/'renders/slides.json').read_text(encoding='utf8'))['result']['slides']
        plan=review_agent.plan_review(merge.attach_clip_reviews(cached['issues'],[],child['document']['slide_count']),
            cached['claims'],rendered,child['options'],settings_store.all(),visual_results=[])
        pending,cached_reviews,stats=recovery_runtime.split_reviews(directory,plan,child['options'])
        print(json.dumps({'preflight':stats},ensure_ascii=False),flush=True)
        assert len(cached_reviews)==5 and len(pending['calls'])==1,stats
        assert pending['calls'][0]['slide_index']==6
        orchestrator.run_task(child['id'],'real')
    final=real_store.get(child['id'])
    report=json.loads((directory/'report/report.json').read_text(encoding='utf8'))
    reviews=report['reviews']
    result={'task_id':child['id'],'parent_task_id':parent['id'],'status':final['status'],
        'recovery':report.get('recovery'),'review_summary':[{k:r.get(k) for k in [
            'slide_index','status','error','error_code','validation_errors','cached_result',
            'reused_from_task_id','actual_http_calls']} for r in reviews],
        'parent_preserved':all((source/name).is_file() and digest(source/name)==expected for name,expected in protected.items()),
        'parent_files_checked':len(protected),
        'new_http_calls':sum(r.get('actual_http_calls',0) for r in reviews),
        'reused_successes':sum(bool(r.get('cached_result')) for r in reviews)}
    out=BACKEND.parent/'docs/4.4BC/review_recovery_checks'
    (out/'real_recovery_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    assert result['parent_preserved']
    assert result['new_http_calls']==1 and result['reused_successes']==5
    assert final['status']=='completed' and all(r['status']=='ok' for r in reviews)


if __name__=='__main__':main()
