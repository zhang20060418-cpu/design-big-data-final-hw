"""One real provider call through the isolated app; restore QA credentials.

Explicit --live is required. Reads the current daily configuration locally,
never prints a key, and does not modify the original user's failed task.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from datetime import datetime

import httpx

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
from app.storage import settings_store
from app.config import TASKS_DIR

FIELDS=('api_base_url','api_model','api_timeout_s','api_max_retries','api_max_concurrency',
    'api_temperature','max_calls_per_task','allow_private_base_url','allow_insecure_base_url',
    'response_format_json')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--source-task',required=True)
    args=parser.parse_args()
    source=TASKS_DIR/args.source_task/'source/source.pptx'
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    daily=settings_store.all();key=settings_store.get_api_key()
    assert key and daily.get('api_base_url') and daily.get('api_model')
    out=BACKEND.parent/'docs/4.4BC/llm_gate_fix_checks'
    with httpx.Client(base_url='http://127.0.0.1:8011',trust_env=False,timeout=180) as c:
        before=c.get('/api/settings').json()
        assert not before['api_key_set'],'Refusing to replace an existing QA key'
        saved={k:before[k] for k in FIELDS if k in before}
        try:
            c.put('/api/settings',json={**{k:daily[k] for k in FIELDS if k in daily},'api_key':key}).raise_for_status()
            options={'mllm_user_choice':None,'llm_backend':'api','review_strategy':'full_review',
                'review_mode':'manual','manual_pages':[3],'max_api_calls':1,'max_review_pages':6,
                'enable_visual':False}
            with source.open('rb') as fh:
                r=c.post('/api/tasks',files={'file':('T07_live_compatibility_QA.pptx',fh,
                    'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
                    data={'name':'真实API修复验证（隔离，一次调用）','options':json.dumps(options)})
            r.raise_for_status();tid=r.json()['id']
            print('live_QA_task='+tid,flush=True)
            deadline=time.monotonic()+420
            while time.monotonic()<deadline:
                task=c.get('/api/tasks/'+tid).json()
                if task['status'] in ['completed','completed_with_errors','failed']:break
                time.sleep(.5)
            payload=c.get(f'/api/tasks/{tid}/report?format=json').json()
            (out/'live_pipeline_report.json').write_text(json.dumps(payload,ensure_ascii=False,indent=1),encoding='utf8')
            reviews=payload.get('reviews',[])
            record={'checked_at':datetime.now().astimezone().isoformat(),'task_id':tid,
                'source_sha256':digest,'source_task_unchanged':args.source_task,
                'status':task['status'],'options':task['options'],'review_gate':task['review_gate'],
                'provider_model':daily['api_model'],
                'reviews':[{k:r.get(k) for k in ['slide_index','status','error','error_code','usage','response_metadata','actual_http_calls','attempts']} | {'verdict_count':len(r.get('verdicts',[]))} for r in reviews],
                'actual_http_calls':sum(r.get('actual_http_calls',0) for r in reviews),
                'pipeline':payload.get('verification_pipeline')}
            (out/'live_pipeline_check.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps(record,ensure_ascii=False),flush=True)
            assert task['options']['enable_mllm'] is True
            assert task['status']=='completed',task.get('error')
            assert reviews and all(r.get('status')=='ok' and not r.get('mock') for r in reviews)
            assert record['actual_http_calls']==1
            assert all(r['response_metadata']['finish_reason']=='stop' for r in reviews)
        finally:
            c.put('/api/settings',json={**saved,'api_key':''}).raise_for_status()
            assert not c.get('/api/settings').json()['api_key_set']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    print('live_pipeline_check=passed; QA credential restored',flush=True)


if __name__=='__main__':main()
