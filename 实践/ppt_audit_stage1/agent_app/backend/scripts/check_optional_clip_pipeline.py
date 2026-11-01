"""Real on/off integration QA. CLIP is real; LLM is explicitly Mock only."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
import httpx

APP=Path(__file__).resolve().parents[2]
OUT=APP/'docs/4.4BC/clip_optional_checks'
RUNTIME=APP/'docs/4.4BC/acceptance/runtime/tasks'

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    OUT.mkdir(exist_ok=True)
    fixture=APP/'fixtures/integration_14_slides.pptx';before=digest(fixture)
    records={}
    with httpx.Client(base_url='http://127.0.0.1:8011',trust_env=False,timeout=180) as c:
        caps=c.get('/api/system/capabilities').json()
        assert caps['modules']['visual']['available'] is True
        for enabled in (False,True):
            name='on' if enabled else 'off'
            options=dict(review_mode='manual' if enabled else 'off',review_strategy='full_review',
                enable_mllm=enabled,llm_backend='mock',max_api_calls=3,max_review_pages=14,manual_pages=[4,6,12])
            # Off intentionally omits the switch to verify the real default.
            if enabled:options['enable_visual']=True
            with fixture.open('rb') as f:
                r=c.post('/api/tasks',files={'file':(fixture.name,f,'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
                    data={'name':f'可选CLIP三级链路核查 {name}','options':json.dumps(options)})
            r.raise_for_status();tid=r.json()['id'];print(f'{name}_task_id={tid}',flush=True)
            started=time.monotonic()
            while time.monotonic()-started<420:
                t=c.get(f'/api/tasks/{tid}').json()
                if t['status'] in ('completed','completed_with_errors','failed'):break
                time.sleep(1)
            assert t['status']=='completed',t.get('error')
            assert t['options']['enable_visual'] is enabled and t['options']['enable_six_detectors']
            visual=c.get(f'/api/tasks/{tid}/visual-results').json()
            report=c.get(f'/api/tasks/{tid}/report?format=json').json()
            assert len(report['claims'])==84
            assert digest(RUNTIME/tid/'source/source.pptx')==before
            assert report['verification_pipeline']['clip_enabled'] is enabled
            clipfolder=RUNTIME/tid/'analysis/clip'
            if not enabled:
                assert not visual['results'] and not clipfolder.exists()
                assert report['verification_pipeline']['final_review_status']=='not_requested'
                stats={'CLIP_forwards':0,'LLM_calls':0,'model_loaded':False}
            else:
                assert len(visual['results'])==14 and visual['status']=='complete'
                assert all(r['status']=='ok' and len(r['probabilities'])==4 for r in visual['results'])
                worker=json.loads((clipfolder/'results.json').read_text(encoding='utf8'))
                assert worker['clip_model_loads']==1 and worker['successful_embeddings']==14
                assert worker['classifier_fits']==worker['network_downloads']==worker['LLM_calls']==worker['benchmark_files_read']==0
                assert report['reviews'] and all(r.get('mock') and r.get('actual_http_calls')==0 and r['status']=='ok' for r in report['reviews'])
                assert all(v['judgement']=='uncertain' for r in report['reviews'] for v in r['verdicts'])
                assert any(r.get('clip_evidence') for r in report['reviews'])
                assert report['verification_pipeline']['final_review_status']=='mock_only'
                stages={s['key']:s for s in t['stages']}
                assert datetime.fromisoformat(stages['structure']['finished_at'])<=datetime.fromisoformat(stages['visual']['started_at'])
                assert datetime.fromisoformat(stages['visual']['finished_at'])<=datetime.fromisoformat(stages['mllm']['started_at'])
                assert all('clip_review' not in i for i in report['issues'] if i['defect_type'] in ('excessive_whitespace','generative_design_anomaly'))
                stats={k:worker.get(k) for k in ('clip_model_loads','vision_forward_attempts','successful_embeddings','classifier_fits','network_downloads','benchmark_files_read','device','runtime_s')}
                stats['actual_external_llm_calls']=0
                for kind in ('html','pdf'):
                    r=c.get(f'/api/tasks/{tid}/report?format={kind}&download=true');r.raise_for_status()
                    if kind=='pdf':assert r.content.startswith(b'%PDF-')
                    (OUT/f'{name}_report.{kind}').write_bytes(r.content)
            (OUT/f'{name}_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=1,allow_nan=False),encoding='utf8')
            records[name]={'task_id':tid,'options':t['options'],'status':t['status'],'visual_results':len(visual['results']),
                'pipeline':report['verification_pipeline'],'execution':stats,
                'claims_fingerprint':[(i['slide_index'],i['defect_type'],i['raw_status'],i.get('score')) for i in report['claims']],
                'url':f'http://127.0.0.1:8011/tasks/{tid}/results','elapsed_s':round(time.monotonic()-started,2)}
            print(json.dumps({'mode':name,'task_id':tid,'visual_results':len(visual['results']),'final_review':report['verification_pipeline']['final_review_status']},ensure_ascii=False),flush=True)
        assert records['on']['claims_fingerprint']==records['off']['claims_fingerprint']
        assert digest(fixture)==before
        result={'status':'passed','checked_at':datetime.now().astimezone().isoformat(),'source_sha256':before,
            'source_unchanged':True,'R2_original_claims_unchanged_by_CLIP':True,'tasks':records,
            'actual_external_llm_calls':0,'classification_performance_claimed':False}
        (OUT/'online_pipeline_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print('pipeline_checks=passed',flush=True)

if __name__=='__main__':main()
