"""Local-only, frozen C inference over actual uploaded PNGs; no fitting/evaluation.

The existing G-2 pure representation helpers and PNG10 extractor are reused.
No benchmark, annotation, dataset label, historical prediction or model-selection
runner is read. The application must explicitly enable this auxiliary step.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time

BACKEND=Path(__file__).resolve().parents[2]
PROJECT=BACKEND.parents[1]
ML=PROJECT/'ml_stage4'
PIN_PATH=BACKEND/'models/frozen_clip_manifest.json'
LABELS=('improper_font_sizing','occluded_content','cluttered_layout','content_overflow_cutoff')
MODES=('f1','high_recall','low_fpr')

def sha(path: Path) -> str:
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def load_module(name: str,path: Path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

def verify_assets(pins: dict,root: Path=ML) -> dict:
    actual={}
    for name,expected in pins['assets_sha256'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('模型工件路径越界')
        actual[name]=sha(path)
        if actual[name]!=expected:raise ValueError('冻结CLIP工件SHA不匹配：'+name)
    if tuple(pins['labels'])!=LABELS:raise ValueError('冻结标签顺序不匹配')
    for mode in MODES:
        values=pins['thresholds'][mode]
        if len(values)!=4 or any(not isinstance(x,(int,float)) or not 0<=x<=1 for x in values):
            raise ValueError('冻结阈值缺失/非法')
    return actual

def install_boundary():
    blocked=[]
    def audit(event,args):
        if event=='socket.connect':
            blocked.append('network_connect');raise PermissionError('Frozen CLIP inference is offline-only')
        if event!='open' or not args or not isinstance(args[0],(str,bytes,os.PathLike)):return
        try:path=Path(os.fsdecode(args[0])).resolve()
        except (OSError,ValueError):return
        if not path.is_relative_to(ML):return
        mode=args[1] if len(args)>1 else None;flags=args[2] if len(args)>2 else 0
        write=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
        denied=write or path.is_relative_to(ML/'data') or path.is_relative_to(ML/'reports') or path.is_relative_to(ML/'outputs')
        denied |= path.is_relative_to(ML/'features') and path!=ML/'features/png_layout_recipe.json'
        denied |= any(p.startswith('benchmark') for p in path.relative_to(ML).parts)
        if denied:blocked.append(str(path));raise PermissionError('CLIP live inference forbids benchmark/data access or research writes: '+str(path))
    sys.addaudithook(audit)
    return blocked

def validate_bundle(bundle: dict,pins: dict):
    import numpy as np
    if tuple(bundle['slugs'])!=LABELS or bundle['clip_revision']!=pins['clip_revision']:
        raise ValueError('冻结bundle标签或revision不匹配')
    if bundle['png_recipe_payload_sha256']!=pins['png_recipe_payload_sha256'] or bundle['frozen_selection_sha256']!=pins['frozen_selection_sha256']:
        raise ValueError('冻结特征/选择摘要不匹配')
    if len(bundle['models']['C'])!=4 or bundle['png_scalers']['10'].n_features_in_!=10:
        raise ValueError('冻结C分类器或Scaler维度错误')
    for model in bundle['models']['C']:
        if model.n_features_in_!=522 or set(model.classes_)!={0,1} or not np.isfinite(model.coef_).all():
            raise ValueError('冻结C分类器不完整')
    for mode in MODES:
        saved=[float(row['thresholds'][mode]['threshold']) for row in bundle['selections']['C']['selections']]
        if saved!=pins['thresholds'][mode]:raise ValueError('工作点与冻结bundle不匹配')

def classify_c(bundle: dict,raw_clip,raw_png,helpers):
    import numpy as np
    # This is exactly the frozen G-2 representation: float64 row-L2 CLIP,
    # train-fitted PNG scaler divided by sqrt(10), no concatenated-vector L2.
    features=helpers.representations(raw_clip,raw_png,bundle['png_scalers'])['C']
    probabilities=np.column_stack([helpers.positive_class_probability(model.predict_proba(features),model.classes_)
                                  for model in bundle['models']['C']])
    if probabilities.shape!=(len(raw_clip),4) or not np.isfinite(probabilities).all() or np.any((probabilities<0)|(probabilities>1)):
        raise ValueError('CLIP辅助概率非法')
    return features,probabilities

def run(request_path: Path,output_path: Path):
    started=time.perf_counter()
    pins=json.loads(PIN_PATH.read_text(encoding='utf8'))
    versions=verify_assets(pins)
    blocked=install_boundary()
    request=json.loads(request_path.read_text(encoding='utf8'));pages=request['pages'];mode=request.get('work_point','f1')
    if mode not in MODES:raise ValueError('未知冻结工作点')
    if not 1<=len(pages)<=200:raise ValueError('页面数量非法')
    identities=[(str(p['document_id']),int(p['slide_index'])) for p in pages]
    if len(identities)!=len(set(identities)):raise ValueError('重复页面身份')
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
        HF_HUB_DISABLE_IMPLICIT_TOKEN='1',HF_HOME=str(ML/'models/huggingface'),TOKENIZERS_PARALLELISM='false')
    import joblib
    import numpy as np
    import torch
    from PIL import Image
    from transformers import CLIPVisionModelWithProjection
    try:from transformers import CLIPImageProcessorPil as Processor
    except ImportError:from transformers import CLIPImageProcessor as Processor
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    def no_fit(*args,**kwargs):raise RuntimeError('Runtime fitting is forbidden')
    LogisticRegression.fit=no_fit
    for method in ('fit','fit_transform','partial_fit'):setattr(StandardScaler,method,no_fit)
    bundle=joblib.load(ML/'models/fusion_routing_candidate.joblib');validate_bundle(bundle,pins)
    sys.path.insert(0,str(ML/'scripts'))
    helpers=load_module('app_frozen_g2_feature_helpers',ML/'scripts/evaluate_pptx_g2.py')
    extractor=load_module('app_frozen_png10',ML/'scripts/extract_png_layout_features.py')
    recipe=json.loads((ML/'features/png_layout_recipe.json').read_text(encoding='utf8'))
    if recipe['payload_sha256']!=pins['png_recipe_payload_sha256'] or recipe['feature_names']!=list(extractor.FEATURE_NAMES):
        raise ValueError('PNG10特征定义顺序不匹配')
    snapshot=ML/'models/huggingface/models--openai--clip-vit-base-patch32/snapshots'/pins['clip_revision']
    processor=Processor.from_pretrained(str(snapshot),local_files_only=True)
    model,loading=CLIPVisionModelWithProjection.from_pretrained(str(snapshot),local_files_only=True,
        torch_dtype=torch.float32,use_safetensors=False,output_loading_info=True)
    if any(loading.get(k) for k in ('missing_keys','mismatched_keys','error_msgs')):
        raise ValueError('CLIP预训练视觉权重加载不完整')
    model.requires_grad_(False);model.eval()
    device='cuda:0' if torch.cuda.is_available() else 'cpu';recoveries=[]
    try:model.to(device)
    except RuntimeError as exc:
        if device=='cpu':raise
        model.to('cpu');torch.cuda.empty_cache();device='cpu'
        recoveries.append({'stage':'model_to_device','error':str(exc)})
    results=[];embeddings=[];png_vectors=[];valid_indices=[];forwards=0
    with torch.inference_mode():
        for index,page in enumerate(pages):
            page_started=time.perf_counter()
            row={'document_id':str(page['document_id']),'slide_index':int(page['slide_index']),
                'status':'error','probabilities':{},'alerts':{},'model_version':pins['version'],'work_point':mode,
                'clip_revision':pins['clip_revision'],'model_bundle_sha256':versions['models/fusion_routing_candidate.joblib'],
                'input_recipe':pins['input_recipe'],'score_kind':'frozen_model_probability_not_confirmation',
                'review_role':'four_label_auxiliary_not_final_verdict','error':None}
            try:
                path=Path(page['png_path']);data=path.read_bytes();input_sha=hashlib.sha256(data).hexdigest()
                with Image.open(io.BytesIO(data)) as opened:
                    if opened.format!='PNG':raise ValueError('输入不是原始PNG')
                    opened.load()
                    if 'A' in opened.getbands() or 'transparency' in opened.info:
                        if opened.convert('RGBA').getchannel('A').getextrema()!=(255,255):
                            raise ValueError('透明PNG不符合冻结G-2管线，保留未知而不更改预处理')
                    rgb=opened.convert('RGB')
                try:
                    png=helpers.png_vector(extractor,rgb)
                    pixels=None
                    try:
                        pixels=processor(images=rgb,return_tensors='pt')['pixel_values'].to(device=device,dtype=torch.float32)
                        forwards+=1
                        embedding=model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
                    except RuntimeError as exc:
                        if device=='cpu' or not isinstance(exc,torch.OutOfMemoryError) and 'cuda' not in str(exc).lower():raise
                        del pixels;model.to('cpu');torch.cuda.empty_cache();device='cpu'
                        recoveries.append({'stage':'vision_forward','slide_index':page['slide_index'],'error':str(exc)})
                        pixels=processor(images=rgb,return_tensors='pt')['pixel_values'].to(device=device,dtype=torch.float32)
                        forwards+=1
                        embedding=model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
                    if embedding.shape!=(512,) or not np.isfinite(embedding).all() or np.linalg.norm(embedding)<=1e-12:
                        raise ValueError('CLIP特征非有限、维度错误或零向量')
                    if png.shape!=(10,) or not np.isfinite(png).all():raise ValueError('PNG10特征非法')
                    if sha(path)!=input_sha:raise ValueError('输入PNG在推理期间改变')
                    embeddings.append(embedding);png_vectors.append(png);valid_indices.append(index)
                    row.update(status='feature_ready',input_png_sha256=input_sha,input_size=list(rgb.size),device=device,
                        feature_dimension=522,clip_dimension=512,png_dimension=10)
                    del pixels
                finally:rgb.close()
            except Exception as exc:
                row.update(status='error',error=str(exc),error_code='clip_page_failed')
            row['latency_ms']=round((time.perf_counter()-page_started)*1000,2);results.append(row)
    if valid_indices:
        clip=np.asarray(embeddings,dtype=np.float32);png=np.asarray(png_vectors,dtype=np.float64)
        features,probabilities=classify_c(bundle,clip,png,helpers)
        for position,index in enumerate(valid_indices):
            row=results[index];p=probabilities[position]
            row.update(status='ok',probabilities=dict(zip(LABELS,map(float,p))),
                thresholds=dict(zip(LABELS,pins['thresholds'][mode])),
                alerts={label:int(p[j]>=pins['thresholds'][mode][j]) for j,label in enumerate(LABELS)},
                work_points={m:{'thresholds':dict(zip(LABELS,pins['thresholds'][m])),
                    'alerts':{label:int(p[j]>=pins['thresholds'][m][j]) for j,label in enumerate(LABELS)}} for m in MODES},
                limitations=['页面级辅助证据，不能证明或否决对象级缺陷。','概率来自历史冻结分类器，未对新文稿校准，不能作为缺陷确认。','冻结CLIP中心裁剪会省略宽屏页面两侧；为保持一致未改输入。','D5/D6不属于此四标签模型范围。'])
        np.savez_compressed(output_path.with_name('features.npz'),clip=clip,png10=png,features=features,
            slide_index=np.asarray([results[i]['slide_index'] for i in valid_indices]),
            document_id=np.asarray([results[i]['document_id'] for i in valid_indices]))
    if blocked:raise ValueError('Unexpected benchmark read or research write')
    output_path.write_text(json.dumps({'status':'complete' if all(r['status']=='ok' for r in results) else 'partial',
        'version':pins['version'],'created_at':datetime.now().astimezone().isoformat(),'results':results,
        'clip_revision':pins['clip_revision'],'frozen_assets_sha256':versions,'work_point':mode,
        'device':device,'batch_size':1,'model_training':model.training,'torch_inference_mode':True,
        'clip_model_loads':1,'vision_forward_attempts':forwards,'successful_embeddings':len(valid_indices),
        'classifier_fits':0,'LLM_calls':0,'network_downloads':0,'benchmark_files_read':0,
        'blocked_reads':blocked,'cpu_fallbacks':recoveries,'runtime_s':round(time.perf_counter()-started,3)},
        ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--request',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();output=Path(args.output)
    try:
        run(Path(args.request),output)
        return 0
    except Exception as exc:
        output.write_text(json.dumps({'status':'failed','error':str(exc),'error_type':type(exc).__name__},ensure_ascii=False,indent=2),encoding='utf8')
        return 1

if __name__=='__main__':raise SystemExit(main())
