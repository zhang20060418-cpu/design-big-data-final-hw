"""Verify the selected default on a fresh isolated upload, with LLM disabled."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import httpx

APP=Path(__file__).resolve().parents[2]
OUT=APP/'docs/4.4BC/model_choice_checks'

def main():
    OUT.mkdir(exist_ok=True)
    fixture=APP/'fixtures/integration_14_slides.pptx'
    source_sha=hashlib.sha256(fixture.read_bytes()).hexdigest()
    capabilities={}
    with httpx.Client(trust_env=False,timeout=30) as client:
        for port in (8000,8011):
            r=client.get(f'http://127.0.0.1:{port}/api/system/capabilities');r.raise_for_status()
            caps=r.json();six=caps['modules']['six_detector'];visual=caps['modules']['visual']
            assert six['version']=='six-detector-evidence-routing-r2'
            assert six['selection_role']=='primary_candidate_screening'
            assert visual['available'] is False
            assert '4.3G-2' not in visual['reason']
            capabilities[port]={'six_detector':six,'legacy_visual':visual}
        base='http://127.0.0.1:8011'
        # Deliberately omit detector switches to exercise actual API defaults.
        with fixture.open('rb') as f:
            r=client.post(base+'/api/tasks',files={'file':(fixture.name,f,'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
                          data={'name':'R2 默认版本核查（无LLM）','options':json.dumps({'enable_mllm':False,'review_mode':'off'})})
        r.raise_for_status();task_id=r.json()['id'];print('task_id='+task_id,flush=True)
        started=time.monotonic()
        while time.monotonic()-started<300:
            task=client.get(base+f'/api/tasks/{task_id}').json()
            if task['status'] in ('completed','completed_with_errors','failed'):break
            time.sleep(.7)
        assert task['status']=='completed',task.get('error')
        assert task['options']['enable_six_detectors'] is True
        assert task['options']['enable_visual'] is False
        claims=client.get(base+f'/api/tasks/{task_id}/claims').json()['claims']
        assert len(claims)==84 and len({c['defect_type'] for c in claims})==6
        report=client.get(base+f'/api/tasks/{task_id}/report?format=json').json()
        assert report['module_status']['six_detector']['version']=='six-detector-evidence-routing-r2'
        assert not report['reviews']
        directory=APP/'docs/4.4BC/acceptance/runtime/tasks'/task_id
        raw=json.loads((directory/'six_detector/raw_detection.json').read_text(encoding='utf8'))
        assert raw['status']=='complete' and raw['CLIP_forwards']==0 and raw['classifier_fits']==0 and raw['LLM_calls']==0
        assert hashlib.sha256((directory/'source/source.pptx').read_bytes()).hexdigest()==source_sha
        assert hashlib.sha256(fixture.read_bytes()).hexdigest()==source_sha
        result={'status':'passed','checked_at':datetime.now().astimezone().isoformat(),'task_id':task_id,
          'url':base+f'/tasks/{task_id}/results','capabilities':capabilities,'source_sha256':source_sha,
          'source_unchanged':True,'slides':14,'claims':84,'claim_statuses':dict(Counter(c['raw_status'] for c in claims)),
          'options':task['options'],'actual_six_detector':report['module_status']['six_detector'],
          'LLM_calls':0,'CLIP_forwards':0,'classifier_fits':0,'elapsed_s':round(time.monotonic()-started,2)}
        (OUT/'online_default_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
        print(json.dumps({k:result[k] for k in ('status','task_id','slides','claims','claim_statuses','LLM_calls','CLIP_forwards','elapsed_s')},ensure_ascii=False))

if __name__=='__main__':main()
