"""One-call, explicit live diagnostic; never prints credentials or reasoning text.

Uses an existing uploaded task's evidence without changing its report or plan.
Without --live it only prints the chosen call's input sizes.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.services import review_agent, mllm
from app.storage import real_store, settings_store
from app.config import TASKS_DIR


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--disable-thinking',action='store_true')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    task=real_store.get(args.task_id)
    if not task:raise ValueError('Unknown real task')
    directory=TASKS_DIR/args.task_id
    plan=deepcopy(json.loads((directory/'analysis/review_plan.json').read_text(encoding='utf8')))
    plan['calls']=plan['calls'][:1]
    call=plan['calls'][0]
    items=[i for i in plan['items'] if i['candidate_id'] in call['candidate_ids']]
    images=review_agent._images(call,items,directory)
    record={'task_id':args.task_id,'live':args.live,'calls_limit':1,
        'disable_thinking':args.disable_thinking,'candidate_count':len(items),
        'image_count':len(images),'image_uri_bytes':sum(len(i['uri']) for i in images),
        'checked_at':datetime.now().astimezone().isoformat()}
    if args.live:
        config=settings_store.all();key=settings_store.get_api_key()
        original=mllm._post_chat
        def capture(base,api_key,payload,timeout):
            if args.disable_thinking:payload['thinking']={'type':'disabled'}
            record['request']={k:payload.get(k) for k in ['model','max_tokens','response_format','thinking']}
            record['request']['serialized_bytes']=len(json.dumps(payload,ensure_ascii=False).encode('utf8'))
            status,body=original(base,api_key,payload,timeout)
            record['http_status']=status
            if isinstance(body,dict):
                record['usage']=body.get('usage')
                choices=body.get('choices') or []
                if choices:
                    choice=choices[0];message=choice.get('message') or {}
                    content=message.get('content') or ''
                    reasoning=message.get('reasoning_content') or ''
                    record['response']={'finish_reason':choice.get('finish_reason'),
                        'content_chars':len(content),'nonspace_content_chars':len(content.strip()),
                        'reasoning_chars':len(reasoning),'reasoning_present':bool(reasoning)}
            return status,body
        mllm._post_chat=capture
        options={**task['options'],'llm_backend':'api','enable_mllm':True}
        reviews=review_agent.review_plan(plan,directory,options,config,key)
        record['results']=[{k:r.get(k) for k in ['status','error','error_code','actual_http_calls','attempts','elapsed_ms']} | {'verdict_count':len(r.get('verdicts',[]))} for r in reviews]
        record['actual_http_calls']=sum(r.get('actual_http_calls',0) for r in reviews)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(record,ensure_ascii=False))


if __name__=='__main__':main()
