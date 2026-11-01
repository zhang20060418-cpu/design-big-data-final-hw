"""Reuse must prove identical evidence; a saved status alone grants no trust."""
from copy import deepcopy
import json

from PIL import Image

from app.services import recovery_runtime, review_agent
from tests.test_review_agent import claim, pages, options


def fixture(directory):
    opts=options(review_strategy='full_review',review_mode='all')
    plan=review_agent.plan_review([], [claim()], pages(directory,2), opts,{})
    results=review_agent.review_plan(plan,directory,opts,{},'')
    assert len(results)==2
    data={'action':'continue','parent_task_id':'parent','source_sha256':'source',
        'screening_reusable':True,'cached_review_plan':plan,'cached_reviews':results[:1]}
    (directory/'analysis').mkdir()
    (directory/'analysis/recovery_context.json').write_text(json.dumps(data),encoding='utf8')
    return opts,plan,results,data


def test_only_failed_or_unfinished_call_is_executed_and_good_result_reused(tmp_path):
    opts,plan,results,data=fixture(tmp_path)
    pending,cached,stats=recovery_runtime.split_reviews(tmp_path,plan,opts)
    assert len(pending['calls'])==len(cached)==1
    assert cached[0]['cached_result'] and cached[0]['reused_from_task_id']=='parent'
    assert cached[0]['actual_http_calls']==cached[0]['attempts']==0
    assert stats['reused_review_calls']==stats['pending_review_calls']==1
    executed=review_agent.review_plan(pending,tmp_path,opts,{},'')
    merged=recovery_runtime.merge_review_attempt(plan,pending,cached,executed)
    assert len(merged)==2 and all(r['status']=='ok' for r in merged)
    assert all(i['outcome']=='reviewed' for i in plan['items'] if i.get('selected'))
    assert data['cached_reviews'][0].get('cached_result') is None


def test_changed_image_is_not_silently_reused(tmp_path):
    opts,plan,results,_=fixture(tmp_path)
    page=results[0]['slide_index']
    Image.new('RGB',(400,225),'red').save(tmp_path/f'renders/s{page:04d}.png')
    pending,cached,stats=recovery_runtime.split_reviews(tmp_path,plan,opts)
    assert not cached and len(pending['calls'])==2
    assert stats['cache_rejections'][0]['reason']=='image_evidence_hash_changed'


def test_changed_native_evidence_is_not_reused(tmp_path):
    opts,plan,results,_=fixture(tmp_path)
    changed=deepcopy(plan)
    cid=results[0]['verdicts'][0]['candidate_id']
    item=next(i for i in changed['items'] if i['candidate_id']==cid)
    item['raw_status']='candidate' if item['raw_status']!='candidate' else 'unknown'
    pending,cached,stats=recovery_runtime.split_reviews(tmp_path,changed,opts)
    assert not cached and len(pending['calls'])==2
    assert stats['cache_rejections'][0]['reason']=='candidate_evidence_changed'


def test_saved_ok_with_invalid_citation_does_not_bypass_schema(tmp_path):
    opts,plan,_,data=fixture(tmp_path)
    data['cached_reviews'][0]['verdicts'][0]['evidence_refs']=['invented-evidence']
    (tmp_path/'analysis/recovery_context.json').write_text(json.dumps(data),encoding='utf8')
    pending,cached,stats=recovery_runtime.split_reviews(tmp_path,plan,opts)
    assert not cached and len(pending['calls'])==2
    assert stats['cache_rejections']


def test_fresh_task_keeps_full_plan_without_recovery(tmp_path):
    opts=options()
    plan=review_agent.plan_review([], [claim()], pages(tmp_path), opts,{})
    pending,cached,stats=recovery_runtime.split_reviews(tmp_path,plan,opts)
    assert pending==plan and not cached
    assert stats['pending_review_calls']==1


def test_skip_after_partial_reuse_keeps_completed_and_records_pending_reason(tmp_path):
    opts,plan,_,_=fixture(tmp_path)
    pending,cached,_=recovery_runtime.split_reviews(tmp_path,plan,opts)
    recovery_runtime.record_unexecuted(plan,pending,'user_skipped')
    completed={v['candidate_id'] for r in cached for v in r['verdicts']}
    assert all(i['outcome']=='reviewed' for i in plan['items'] if i['candidate_id'] in completed)
    assert all(i['outcome']=='not_checked' and i['execution_trace'][-1]['reason']=='user_skipped'
               for i in plan['items'] if i.get('selected') and i['candidate_id'] not in completed)
