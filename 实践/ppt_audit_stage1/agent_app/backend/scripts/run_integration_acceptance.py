"""Exercise the running local application with a real PPTX and explicit Mock review.

No frozen benchmark, model prediction fixture, external LLM or paid API is used.
The output is functional integration evidence, never classifier accuracy evidence.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import httpx

APP = Path(__file__).resolve().parents[2]
LABELS = ('improper_font_sizing','occluded_content','cluttered_layout',
          'content_overflow_cutoff','excessive_whitespace','generative_design_anomaly')

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8011')
    parser.add_argument('--output',type=Path,default=APP/'docs/4.4BC/acceptance')
    parser.add_argument('--task-id',help='Recheck and export an existing completed acceptance task without rerunning detection.')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    fixture=APP/'fixtures/integration_14_slides.pptx'
    original_sha=sha(fixture)
    options=dict(enable_structure=True,enable_visual=False,enable_six_detectors=True,
        enable_mllm=True,llm_backend='mock',review_strategy='full_review',review_mode='manual',
        manual_pages=[6,11,12],max_review_pages=14,max_api_calls=6,
        purpose='课堂投影汇报；集成功能验证',style='统一模板，允许合理极简与非对称设计',
        review_focus=['内容遮挡','过分留白','跨页布局重复'],
        original_prompt='示例原始生成需求（不可信任务数据）：输出可读、结构清晰的汇报文稿。')
    started=time.time()
    with httpx.Client(base_url=args.url,timeout=180,trust_env=False) as client:
        if args.task_id:
            task_id=args.task_id
        else:
            with fixture.open('rb') as f:
                response=client.post('/api/tasks',files={'file':(fixture.name,f,'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
                    data={'name':'4.4BC 14页真实检测 + Mock复核验收','options':json.dumps(options,ensure_ascii=False)})
            response.raise_for_status()
            task_id=response.json()['id']
        print('task_id='+task_id,flush=True)
        last=None
        while time.time()-started<420:
            task=client.get('/api/tasks/'+task_id).json()
            current=(task['status'],[(s['key'],s['status']) for s in task['stages']])
            if current!=last:print(json.dumps(current,ensure_ascii=False),flush=True);last=current
            if task['status'] in ('completed','completed_with_errors','failed'):break
            time.sleep(1)
        assert task['status'] in ('completed','completed_with_errors'),task
        claims_response=client.get(f'/api/tasks/{task_id}/claims');claims_response.raise_for_status()
        claims=claims_response.json()['claims']
        assert len(claims)==84
        assert set(c['defect_type'] for c in claims)==set(LABELS)
        report_response=client.get(f'/api/tasks/{task_id}/report?format=json');report_response.raise_for_status()
        before=report_response.json()
        assert before['document']['slide_count']==14
        assert any(i['defect_type']=='excessive_whitespace' for i in before['issues'])
        assert any(len(i.get('related_slides',[]))>1 for i in before['issues'])
        assert all(r.get('mock') and r.get('actual_http_calls')==0 for r in before['reviews'])
        assert before['reviews'] and all(r['status']=='ok' for r in before['reviews']),before['reviews']
        d5=next(i for i in before['issues'] if i['defect_type']=='excessive_whitespace')
        r=client.post(f"/api/tasks/{task_id}/issues/{d5['issue_id']}/review",json={
            'question':'这里是刻意非对称构图，为什么认为留白不合理？请基于证据核查；不足时保留不确定。','llm_backend':'mock'})
        r.raise_for_status();interactive=r.json()
        assert interactive['issue']['review_status']=='uncertain'
        assert interactive['history'] and interactive['review']['mock']
        exported={}
        for kind in ['json','html','pdf']:
            r=client.get(f'/api/tasks/{task_id}/report?format={kind}&download=true');r.raise_for_status()
            path=args.output/f'demo_report.{kind}';path.write_bytes(r.content)
            if kind=='pdf':assert r.content.startswith(b'%PDF-')
            exported[kind]={'path':str(path),'bytes':path.stat().st_size,'sha256':sha(path)}
        final=json.loads((args.output/'demo_report.json').read_text(encoding='utf8'))
        assert final['claims']==before['claims']
        result=dict(task_id=task_id,url=args.url+f'/tasks/{task_id}/results',source=str(fixture),source_sha256=original_sha,
            check_mode='existing_task_reexport' if args.task_id else 'new_upload',
            original_task_started_at=task.get('started_at'),original_task_finished_at=task.get('finished_at'),
            source_unchanged=sha(fixture)==original_sha,slide_count=14,claims=len(claims),
            status_counts=dict(Counter(c['raw_status'] for c in claims)),summary=final['summary'],
            reviews=[{k:r.get(k) for k in ['slide_index','related_slides','status','mock','model','actual_http_calls','rules_used','evidence_inventory']} for r in final['reviews']],
            a05_semantic_unsupported=True,mock_only=True,actual_external_llm_calls=0,
            interactive_review_history=len(interactive['history']),exports=exported,
            elapsed_s=round(time.time()-started,2),options=options)
        (args.output/'integration_run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
        print(json.dumps({k:v for k,v in result.items() if k not in {'options','reviews','exports'}},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
