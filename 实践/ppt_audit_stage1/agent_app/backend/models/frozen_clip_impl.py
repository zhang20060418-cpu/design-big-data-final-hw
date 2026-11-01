"""Small application bridge; heavy CLIP inference stays in the existing ML env.

Import/check_available never imports torch, numpy, sklearn or model weights.
Only an explicitly enabled pipeline calls predict_batch, once per document.
"""
from __future__ import annotations
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BACKEND=Path(__file__).resolve().parents[1]
PROJECT=BACKEND.parents[1]
ML=PROJECT/'ml_stage4'
ML_PYTHON=ML/'.venv/Scripts/python.exe'
PINS=Path(__file__).with_name('frozen_clip_manifest.json')
WORKER=BACKEND/'app/services/clip_inference_worker.py'

def check_available() -> dict:
    pins=json.loads(PINS.read_text(encoding='utf8'))
    packages=ML/'.venv/Lib/site-packages'
    required=[ML_PYTHON,WORKER,*[ML/n for n in pins['assets_sha256']],
              *[packages/n/'__init__.py' for n in ('torch','transformers','numpy','joblib','sklearn')]]
    missing=[str(p) for p in required if not p.is_file()]
    if missing:
        return {'available':False,'state':'error','reason':'冻结CLIP运行文件缺失：'+'；'.join(missing),
                'model_loaded':False,'CLIP_forwards':0}
    # Small manifests/source hashes establish the selected version without
    # importing or deserializing models. Full weight SHA is verified on use.
    mismatched=[name for name,expected in pins['assets_sha256'].items() if not name.endswith('.bin')
                and hashlib.sha256((ML/name).read_bytes()).hexdigest()!=expected]
    if mismatched:
        return {'available':False,'state':'error','reason':'冻结CLIP配置/源码版本不匹配：'+'；'.join(mismatched),
                'model_loaded':False,'CLIP_forwards':0}
    return {'available':True,'state':'ready','reason':'冻结C四标签辅助接口可用，需用户手动开启。',
            'model_version':pins['version'],'clip_revision':pins['clip_revision'],
            'model_loaded':False,'CLIP_forwards':0}

def _error(page: dict,message: str,code: str) -> dict:
    return {'status':'error','document_id':str(page.get('document_id','')),'slide_index':int(page['slide_index']),
            'probabilities':{},'alerts':{},'error':message,'error_code':code,'model_version':'f2c-C-frozen-53a56f3b'}

def predict_batch(pages: list[dict],config: dict | None=None) -> list[dict]:
    if not pages:return []
    config=config or {}
    state=check_available()
    if not state['available']:
        return [_error(p,state['reason'],'clip_files_missing') for p in pages]
    mode=config.get('work_point') or 'f1'
    if mode not in {'f1','high_recall','low_fpr'}:
        raise ValueError('只能使用已有冻结工作点，禁止临时推导阈值。')
    if len(pages)>200:raise ValueError('CLIP文稿批次超过200页。')
    identities=[(str(p.get('document_id','')),int(p['slide_index'])) for p in pages]
    if len(identities)!=len(set(identities)) or any(i<1 for _,i in identities):
        raise ValueError('CLIP页面身份必须唯一且页号为正。')
    task_dir=Path(config['task_dir']).resolve() if config.get('task_dir') else None
    rows=[]
    for page in pages:
        path=Path(page.get('png_path') or page.get('png') or '').resolve()
        if not path.is_file() or path.suffix.lower()!='.png':raise ValueError('CLIP输入必须是存在的原始PNG。')
        if task_dir and not path.is_relative_to(task_dir/'renders'):
            raise ValueError('CLIP只接受当前任务renders下的原始页面，禁止跨任务读取。')
        rows.append({'png_path':str(path),'document_id':str(page.get('document_id','')),
                     'slide_index':int(page['slide_index'])})
    context=nullcontext(str(task_dir/'analysis/clip')) if task_dir else tempfile.TemporaryDirectory(prefix='ppt_clip_live_')
    with context as directory:
        folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
        request=folder/'request.json';output=folder/'results.json'
        request.write_text(json.dumps({'work_point':mode,'pages':rows},ensure_ascii=False,indent=2),encoding='utf8')
        # Avoid stale success being read if a later subprocess fails early.
        output.unlink(missing_ok=True)
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1','HF_HUB_OFFLINE':'1',
             'TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1',
             'HF_HOME':str(ML/'models/huggingface'),'TOKENIZERS_PARALLELISM':'false'}
        timeout=max(30,min(1800,int(config.get('timeout_s',120+len(rows)*10))))
        try:
            process=subprocess.run([str(ML_PYTHON),'-B',str(WORKER),'--request',str(request),'--output',str(output)],
                capture_output=True,text=True,encoding='utf8',errors='replace',timeout=timeout,env=env,
                creationflags=0x08000000 if os.name=='nt' else 0)
            (folder/'worker.log').write_text(process.stdout+'\n'+process.stderr,encoding='utf8')
            payload=json.loads(output.read_text(encoding='utf8')) if output.is_file() else {}
            if process.returncode!=0 or payload.get('status') not in {'complete','partial'}:
                message=str(payload.get('error') or ('CLIP推理未成功：'+process.stderr[-1600:]))
                return [_error(p,message,'clip_inference_failed') for p in pages]
            results=payload.get('results',[])
            actual=[(str(p.get('document_id','')),int(p['slide_index'])) for p in results]
            if actual!=identities:raise ValueError('CLIP结果页面身份/顺序不匹配，拒绝合并。')
            if task_dir:
                for result in results:
                    result['artifacts']={'request':str(request),'results':str(output),
                        'result_sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
                    if (folder/'features.npz').is_file():result['artifacts']['features']=str(folder/'features.npz')
            return results
        except subprocess.TimeoutExpired:
            return [_error(p,f'CLIP超过{timeout}秒，未形成辅助结论。','clip_timeout') for p in pages]
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return [_error(p,str(exc),'clip_invalid_result') for p in pages]

def predict_slide(png_path: Path,config: dict | None=None) -> dict:
    config=config or {}
    return predict_batch([{'png_path':png_path,'document_id':config.get('document_id',''),
        'slide_index':int(config.get('slide_index',1))}],config)[0]
