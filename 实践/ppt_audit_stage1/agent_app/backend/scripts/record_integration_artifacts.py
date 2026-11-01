"""Record local integration deliverables and verify the pre-change research SHA list.

No model calls, training, database writes or benchmark modifications occur here.
Secrets, virtual environments, runtime databases and model weights are excluded.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

APP=Path(__file__).resolve().parents[2]
PROJECT=APP.parent
OUT=APP/'docs/4.4BC/acceptance'
CHECKPOINT=PROJECT/'_local_checkpoints/4.4BC_20261030_215120'

def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def write(path: Path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

def main():
    sources={}
    extensions={'.py','.ts','.tsx','.mjs','.json','.j2','.css','.md','.ps1','.toml','.ini','.txt','.html'}
    excluded={'.venv','node_modules','dist','data','.run','.tools','__pycache__','.pytest_cache','.git','acceptance'}
    for current,dirs,files in os.walk(APP):
        dirs[:]=[d for d in dirs if d not in excluded]
        for filename in files:
            path=Path(current)/filename
            if filename.startswith('.env') or 'secret' in filename.lower():continue
            if path.suffix in extensions or filename=='.gitignore':
                sources[str(path.relative_to(PROJECT))]=sha(path)
    before=read(CHECKPOINT/'source_sha256.json')
    modified=[];missing=[]
    for name,old in before.items():
        path=PROJECT/name
        if not path.is_file():missing.append(name);continue
        actual=sha(path)
        if actual!=old:modified.append({'path':name,'before_sha256':old,'after_sha256':actual})
    changes={'checkpoint':str(CHECKPOINT),'modified':modified,'missing':missing,
             'added_sources':sorted(set(sources)-set(before)),'source_sha256':sources}
    write(OUT/'source_changes.json',changes)
    protected=read(CHECKPOINT/'research_protected_sha256.json')
    changed=[];absent=[];size=0
    for name,expected in protected.items():
        path=Path(name)
        if not path.is_file():absent.append(name);continue
        size+=path.stat().st_size
        if sha(path)!=expected:changed.append(name)
    preservation={'checked':len(protected)-len(absent),'bytes_checked':size,'changed':changed,
                  'missing':absent,'all_unchanged':not(changed or absent)}
    write(OUT/'research_preservation.json',preservation)
    assert preservation['all_unchanged'],preservation
    assert not missing,missing
    suite=ET.parse(OUT/'backend-tests.xml').getroot().find('testsuite')
    assert suite is not None
    backend={k:int(suite.get(k,'0')) for k in ('tests','failures','errors','skipped')}
    browser=read(OUT/'playwright-results.json')['stats']
    assert backend['tests']==118 and not(backend['failures'] or backend['errors'] or backend['skipped'])
    assert browser['expected']==3 and browser['unexpected']==0 and browser['skipped']==0
    assert read(OUT/'final-ui-smoke.json')['status']=='passed'
    run=read(OUT/'integration_run.json');task_id=run['task_id']
    task_root=OUT/'runtime/tasks'/task_id
    payload=read(task_root/'report/report.json')
    assert run['claims']==84==len(payload['claims'])
    assert sha(APP/'fixtures/integration_14_slides.pptx')==run['source_sha256']
    assert all(r['mock'] and r.get('actual_http_calls')==0 for r in payload['reviews'])
    assert all(v['judgement']=='uncertain' for r in payload['reviews'] for v in r['verdicts'])
    # Export copies must refer to exactly the same current application report.
    for kind in ('json','html','pdf'):
        assert sha(OUT/f'demo_report.{kind}')==sha(task_root/f'report/report.{kind}')
    proof_files=[APP/'fixtures/integration_14_slides.pptx']
    proof_files += sorted(p for p in OUT.iterdir() if p.is_file() and p.name!='artifact_manifest.json')
    proof_files += sorted(p for p in (APP/'docs/4.4BC').glob('*.md'))
    for sub in ('source','renders','analysis','six_detector','reviews','report'):
        for path in sorted((task_root/sub).rglob('*')):
            if path.is_file() and 'pdf_browser_profile' not in path.parts:proof_files.append(path)
    records={str(p.relative_to(APP)):{'sha256':sha(p),'bytes':p.stat().st_size} for p in proof_files}
    raw=read(task_root/'six_detector/raw_detection.json')
    result={'schema_version':'4.4BC-artifacts-1','recorded_at':datetime.now().astimezone().isoformat(),
       'checkpoint':str(CHECKPOINT),'task_id':task_id,'review_backend':'mock',
       'actual_external_llm_calls':0,'research_preservation':preservation,'backend_tests':backend,
       'browser_tests':{k:browser[k] for k in ('expected','unexpected','skipped','flaky')},
       'python':sys.version,'packages':{n:importlib.metadata.version(n) for n in ('fastapi','pydantic','python-pptx','Pillow','pywin32','pytest','httpx')},
       'source_count':len(sources),'source_changes':'source_changes.json','files':records,
       'detector_version':payload['claims'][0]['detector_version'],
       'detector_execution':{k:raw.get(k) for k in ('frozen_thresholds','benchmark_files_read','classifier_fits','CLIP_forwards','LLM_calls','sources_unchanged','A05_semantic_status')}}
    write(OUT/'artifact_manifest.json',result)
    print(json.dumps({'backend_tests':backend,'browser_passed':browser['expected'],
        'research_files_unchanged':preservation['checked'],'modified_sources':len(modified),
        'added_sources':len(changes['added_sources']),'artifact_count':len(records),
        'manifest_sha256':sha(OUT/'artifact_manifest.json')},ensure_ascii=False))

if __name__=='__main__':main()
