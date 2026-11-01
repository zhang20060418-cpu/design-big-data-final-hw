"""One registered independent scoring pass; no fitting, thresholding search or data selection.

Run --fixture first, then --run. An exclusive formal_scoring_lock.json refuses
another scoring pass. Reports can consume saved probabilities without inference.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import sys
from datetime import datetime

import numpy as np
from pptx_g2_metrics import LABELS, positive_class_probability, validate_feature_matrix

ML = Path(__file__).resolve().parents[1]
MODELS = ['A', 'C', 'H', 'C9']
MODES = ['f1', 'high_recall', 'low_fpr']
PNG_COLUMNS = ['aspect_ratio','edge_density','flat_region_ratio','gray_std','grid_edge_cv','border_edge_ratio',
               'horizontal_projection_spread','vertical_projection_spread','mean_gray','mean_color_range']
META = ['sample_id', 'cohort_id', 'document_id', 'group_id', 'source_family_id',
        'natural_or_controlled', 'source_class', 'source_scope', 'source_content_type',
        'variant_id', 'modification_type', 'original_slide_index', 'png_sha256', 'pptx_sha256']


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value, exclusive=False):
    with Path(path).open('x' if exclusive else 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def writer(path, fields, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
        w.writeheader()
        w.writerows(rows)


def representations(raw_clip, raw_png, scalers):
    n = len(raw_clip)
    validate_feature_matrix(raw_clip, n, 512)
    validate_feature_matrix(raw_png, n, 10)
    norm = np.linalg.norm(raw_clip.astype(np.float64), axis=1, keepdims=True)
    if not np.all(norm > 1e-12):
        raise ValueError('Zero CLIP feature')
    a = raw_clip.astype(np.float64)/norm
    c = np.concatenate([a, scalers['10'].transform(raw_png)/np.sqrt(10)], axis=1)
    c9 = np.concatenate([a, scalers['9'].transform(raw_png[:, 1:])/np.sqrt(9)], axis=1)
    for x, d in [(a,512),(c,522),(c9,521)]:
        validate_feature_matrix(x, n, d)
    return {'A': a, 'C': c, 'C9': c9}


def png_vector(module, rgb):
    values = module.extract_features(rgb)
    if list(values) != PNG_COLUMNS:
        raise ValueError('Frozen PNG feature order mismatch')
    return np.asarray([values[name] for name in PNG_COLUMNS], dtype=np.float64)


def classify(bundle, reps):
    """Call each frozen base classifier once; H reuses existing probabilities."""
    probabilities, calls = {}, []
    for name in ['A','C','C9']:
        cols = []
        for slug, estimator in zip(LABELS, bundle['models'][name]):
            if reps[name].shape[1] != estimator.n_features_in_:
                raise ValueError('Classifier feature dimension mismatch')
            p = positive_class_probability(estimator.predict_proba(reps[name]), estimator.classes_)
            cols.append(p)
            calls.append({'model':name,'label':slug,'rows':len(p),'calls':1})
        probabilities[name] = np.column_stack(cols)
    routes = [bundle['routing'][label]['selected'] for label in bundle['labels']]
    if routes != ['A','C','C','C']:
        raise ValueError('Frozen H route changed')
    probabilities['H'] = np.column_stack([probabilities[route][:,j] for j,route in enumerate(routes)])
    for p in probabilities.values():
        if p.shape != (len(reps['A']),4) or not np.isfinite(p).all() or np.any((p<0)|(p>1)):
            raise ValueError('Invalid probabilities')
    return {m: probabilities[m] for m in MODELS}, calls


def write_scores(directory, samples, probabilities, thresholds):
    prob_rows=[]
    for i, sample in enumerate(samples):
        r={key:sample.get(key,'') for key in META}
        r.update({'true_'+label: int(sample[label]) for label in LABELS})
        r.update({name+'_prob_'+label:float(probabilities[name][i,j]) for name in MODELS for j,label in enumerate(LABELS)})
        prob_rows.append(r)
    fields=[*META,*['true_'+x for x in LABELS],*[m+'_prob_'+x for m in MODELS for x in LABELS]]
    writer(directory/'probabilities.csv',fields,prob_rows)
    alert_fields=[*META,'model','mode',*[prefix+'_'+x for prefix in ['true','prob','threshold','pred'] for x in LABELS]]
    def alert_rows():
        for i, sample in enumerate(samples):
            for name in MODELS:
                for mode in MODES:
                    t=np.asarray(thresholds[name][mode])
                    p=probabilities[name][i]
                    r={key:sample.get(key,'') for key in META}
                    r.update(model=name,mode=mode)
                    for j,label in enumerate(LABELS):
                        r['true_'+label]=int(sample[label]);r['prob_'+label]=float(p[j])
                        r['threshold_'+label]=float(t[j]);r['pred_'+label]=int(p[j]>=t[j])
                    yield r
    writer(directory/'alerts.csv',alert_fields,alert_rows())


def fixture():
    """Synthetic feature/classifier/CSV contract; never opens a formal model/image."""
    import tempfile
    class Scaler:
        def transform(self,x):return np.asarray(x)*2
    class Classifier:
        classes_=np.array([1,0])
        def __init__(self,d,p):self.n_features_in_=d;self.p=p;self.calls=0
        def predict_proba(self,x):
            self.calls+=1
            return np.column_stack([np.full(len(x),self.p),np.full(len(x),1-self.p)])
    x=np.arange(1,3*512+1,dtype=np.float32).reshape(3,512)
    z=np.arange(30,dtype=np.float64).reshape(3,10)/100
    reps=representations(x,z,{'10':Scaler(),'9':Scaler()})
    assert [reps[m].shape[1] for m in ['A','C','C9']]==[512,522,521]
    np.testing.assert_allclose(np.linalg.norm(reps['A'],axis=1),1)
    np.testing.assert_allclose(reps['C'][:,512:],2*z/np.sqrt(10))
    from PIL import Image
    spec=importlib.util.spec_from_file_location('g2_fixture_png',ML/'scripts/extract_png_layout_features.py')
    png=importlib.util.module_from_spec(spec);spec.loader.exec_module(png)
    with Image.new('RGB',(64,32),(255,255,255)) as synthetic:
        vector=png_vector(png,synthetic)
        assert vector.shape==(10,) and vector.dtype==np.float64 and np.isfinite(vector).all()
        assert vector[0]==2.0 and vector[1]==0.0
        try:from transformers import CLIPImageProcessorPil as Processor
        except ImportError:from transformers import CLIPImageProcessor as Processor
        processor=Processor.from_pretrained(str(ML/'models/huggingface/models--openai--clip-vit-base-patch32/snapshots/3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268'),local_files_only=True)
        pixels=processor(images=synthetic,return_tensors='np')['pixel_values']
        assert pixels.shape==(1,3,224,224) and np.isfinite(pixels).all()
        np.testing.assert_allclose(pixels[0,:,112,112],(1-np.array(processor.image_mean))/np.array(processor.image_std),rtol=1e-6)
    fake={'labels':list(LABELS),'routing':{label:{'selected':route} for label,route in zip(LABELS,['A','C','C','C'])},
          'models':{m:[Classifier(d,p) for _ in LABELS] for m,d,p in [('A',512,.13),('C',522,.57),('C9',521,.24)]}}
    p,calls=classify(fake,reps)
    assert len(calls)==12 and all(e.calls==1 for group in fake['models'].values() for e in group)
    np.testing.assert_allclose(p['A'],.13);np.testing.assert_allclose(p['C9'],.24)
    np.testing.assert_allclose(p['H'][:,0],p['A'][:,0]);np.testing.assert_allclose(p['H'][:,1:],p['C'][:,1:])
    samples=[dict(sample_id=f'fixture_{i}',document_id=f'd{i//2}',group_id=f'g{i}',**{k:str(int((i+j)%2)) for j,k in enumerate(LABELS)}) for i in range(3)]
    thresholds={m:{mode:[float(p[m][0,j]) for j in range(4)] for mode in MODES} for m in MODELS}
    with tempfile.TemporaryDirectory(prefix='pptx_g2_synthetic_') as tmp:
        output=Path(tmp);write_scores(output,samples,p,thresholds)
        with (output/'probabilities.csv').open(encoding='utf-8-sig',newline='') as f:rr=list(csv.DictReader(f))
        with (output/'alerts.csv').open(encoding='utf-8-sig',newline='') as f:aa=list(csv.DictReader(f))
        assert len(rr)==3 and len(aa)==36 and all(r['pred_'+k]=='1' for r in aa for k in LABELS)
        assert all(rr[i]['true_'+k]==samples[i][k] for i in range(3) for k in LABELS)
    result={'status':'passed','at':now(),'formal_data_or_models_used':False,'checks':['512/522/521 feature shapes','fixedL2/scaler divisors','frozen PNG extractor ordered dict on synthetic image','pinned processor synthetic RGB 224 shape/mean/std without vision model','reverse classes class1','12 calls once and H reuse','CSV rows/labels/probabilities','threshold equality positive'],
            'script_sha256':sha(__file__)}
    (ML/'.cache/pptx_g2_fixture').mkdir(parents=True,exist_ok=True)
    save(ML/'.cache/pptx_g2_fixture/runner_fixture_results.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


def install_boundary():
    blocked=[]
    def audit(event,args):
        if event!='open' or not args or not isinstance(args[0],(str,bytes,os.PathLike)):return
        try:p=Path(os.fsdecode(args[0])).resolve()
        except (OSError,ValueError):return
        denied=(p.is_relative_to(ML/'data') or (p.is_relative_to(ML/'features') and p!=ML/'features/png_layout_recipe.json') or p.name=='test_predictions.csv')
        if denied:
            blocked.append(str(p));raise PermissionError('Official SlideAudit inputs forbidden in G-2: '+str(p))
    sys.addaudithook(audit)
    return blocked


def run():
    preflight=load(ML/'outputs/pptx_g2_preflight.json')
    fixtures={name:load(ML/'.cache/pptx_g2_fixture'/name) for name in ['metrics_fixture_results.json','runner_fixture_results.json','analysis_fixture_results.json']}
    if preflight['status']!='passed' or preflight['blockers']:
        raise ValueError('Preflight blocked; formal scoring not started')
    runner=fixtures['runner_fixture_results.json']
    if runner['status']!='passed' or runner['script_sha256']!=sha(__file__):raise ValueError('Runner fixture stale')
    # The metrics unit fixture uses unittest result fields; require its explicit success.
    metrics_fixture=fixtures['metrics_fixture_results.json']
    if metrics_fixture.get('success') is not True or metrics_fixture['failures'] or metrics_fixture['errors']:
        raise ValueError('Synthetic metrics fixture failed')
    for name,expected in metrics_fixture['source_sha256'].items():
        if sha(ML/'scripts'/name)!=expected:raise ValueError('Stale metrics fixture source')
    analysis_fixture=fixtures['analysis_fixture_results.json']
    if analysis_fixture['status']!='passed' or analysis_fixture['script_sha256']!=sha(ML/'scripts/analyze_pptx_g2.py'):
        raise ValueError('Stale analysis fixture')
    samples=preflight['ordered_input_index']
    assert len(samples)==352 and len({r['sample_id'] for r in samples})==352
    from pptx_g2_preflight import load_ordered_samples, digest as index_digest
    fresh=load_ordered_samples(ML)
    if len(fresh)!=len(samples) or any(any(row[k]!=actual[k] for k in row) for row,actual in zip(samples,fresh)):
        raise ValueError('Cached preflight row order/labels differ from frozen manifests')
    if index_digest(samples)!=preflight['ordered_input_index_sha256']:
        raise ValueError('Preflight ordered index digest mismatch')
    input_hashes=dict(preflight['registered_input_sha256'])
    for sample in samples:
        for key in ['png','pptx']:
            filename=sample[key+'_absolute_path'];expected=sample[key+'_sha256']
            if filename in input_hashes and input_hashes[filename]!=expected:raise ValueError('Conflicting frozen artifact identity')
            input_hashes[filename]=expected
    for filename,expected in input_hashes.items():
        if sha(filename)!=expected:raise ValueError('Input changed since preflight: '+filename)
    source_files=['evaluate_pptx_g2.py','pptx_g2_metrics.py','test_pptx_g2_metrics.py','pptx_g2_preflight.py','analyze_pptx_g2.py','extract_png_layout_features.py']
    source_hashes={f:sha(ML/'scripts'/f) for f in source_files}
    blocked_reads=install_boundary()
    import torch
    import joblib
    import sklearn.linear_model
    import sklearn.preprocessing
    def no_fit(*args,**kwargs):raise RuntimeError('Fitting is prohibited in G-2')
    sklearn.linear_model.LogisticRegression.fit=no_fit
    for method in ['fit','fit_transform','partial_fit']:
        setattr(sklearn.preprocessing.StandardScaler,method,no_fit)
    bundle=joblib.load(ML/'models/fusion_routing_candidate.joblib')
    thresholds={name:{mode:[row['thresholds'][mode]['threshold'] for row in bundle['selections'][name]['selections']] for mode in MODES} for name in ['A','C','C9']}
    thresholds['H']=bundle['hybrid_thresholds']
    thresholds={m:{mode:np.asarray(thresholds[m][mode],dtype=np.float64).tolist() for mode in MODES} for m in MODELS}
    batch_id='pptx-g2-'+datetime.now().strftime('%Y%m%dT%H%M%S')
    parent=ML/'outputs/pptx_g2';parent.mkdir(exist_ok=True)
    output=parent/batch_id
    lock_path=parent/'formal_scoring_lock.json'
    save(lock_path,{'batch_id':batch_id,'registered_at':now(),'one_formal_scoring_run':True,'output':str(output)},exclusive=True)
    output.mkdir()
    manifest={'schema_version':'pptx-g2-single-pass-v1','batch_id':batch_id,'registered_at':now(),'phase':'registered_before_any_inference',
              'formal_score_passes':1,'protocol_sha256':sha(ML/'reports/pptx_independent_evaluation_protocol.md'),
              'freeze_registry_sha256':sha(ML/'reports/pptx_independent_evaluation_freeze_registry.json'),
              'preflight_sha256':sha(ML/'outputs/pptx_g2_preflight.json'),'fixture_results':fixtures,
              'script_sha256':source_hashes,
              'registered_input_sha256':input_hashes,
              'input_index_sha256':preflight['ordered_input_index_sha256'],
              'environment':preflight['environment'],'torch_cuda_available':torch.cuda.is_available(),
              'planned_device':'cuda:0' if torch.cuda.is_available() else 'cpu',
              'cuda_device_name':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
              'model_bundle_sha256':sha(ML/'models/fusion_routing_candidate.joblib'),'label_order':list(LABELS),
              'thresholds':thresholds,'H_routes':['A','C','C','C'],'clip_revision':bundle['clip_revision'],
              'sample_count':352,'primary_E_A':282,'bootstrap':{'B':2000,'seed':42,'primary':'source_family_id','sensitivity':'document_id','global_paired_cluster_draws':True},
              'planned_outputs':['execution_manifest.json','scoring_log.jsonl','features.npz','probabilities.csv','alerts.csv','point_metrics.json','stratified_label_metrics.csv','stratified_summary_metrics.csv','document_metrics.csv','source_family_metrics.csv','bootstrap_summary.csv','bootstrap_family_replicates.npz','bootstrap_document_replicates.npz','controlled_pairs.csv','controlled_summary.csv','figures/*.svg','artifact_sha256.csv','failures.json'],
              'report_path':str(ML/'reports/pptx_g2_independent_evaluation.md')}
    save(output/'execution_manifest.json',manifest,exclusive=True)
    # Write this pointer before scoring; it is never overwritten by another batch.
    save(ML/'reports/pptx_g2_execution_manifest.json',{'batch_id':batch_id,'execution_manifest':str(output/'execution_manifest.json'),'registered_manifest_sha256':sha(output/'execution_manifest.json')},exclusive=True)
    def event(kind,**kwargs):
        record=dict(at=now(),event=kind,**kwargs)
        with (output/'scoring_log.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        print(json.dumps(record,ensure_ascii=False),flush=True)
    try:
        os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1',HF_HOME=str(ML/'models/huggingface'),TOKENIZERS_PARALLELISM='false')
        from PIL import Image
        from transformers import CLIPVisionModelWithProjection
        try:from transformers import CLIPImageProcessorPil as CLIPImageProcessor
        except ImportError:from transformers import CLIPImageProcessor
        snapshot=ML/'models/huggingface/models--openai--clip-vit-base-patch32/snapshots'/bundle['clip_revision']
        processor=CLIPImageProcessor.from_pretrained(str(snapshot),local_files_only=True)
        model,loading=CLIPVisionModelWithProjection.from_pretrained(str(snapshot),local_files_only=True,torch_dtype=torch.float32,use_safetensors=False,output_loading_info=True)
        if any(loading.get(k) for k in ['missing_keys','mismatched_keys','error_msgs']):raise ValueError('Incomplete CLIP pretrained load')
        model.requires_grad_(False);model.eval()
        device=manifest['planned_device'];oom=[]
        try:model.to(device)
        except torch.OutOfMemoryError:
            model.to('cpu');torch.cuda.empty_cache();device='cpu';oom.append({'stage':'model_to_device'})
        spec=importlib.util.spec_from_file_location('g2_frozen_png',ML/'scripts/extract_png_layout_features.py')
        png_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(png_module)
        raw_clip=np.empty((352,512),dtype=np.float32);raw_png=np.empty((352,10),dtype=np.float64)
        devices=[];forward_attempts=0
        event('feature_extraction_started',batch_size=1,device=device,processor=type(processor).__name__,model_training=model.training)
        with torch.inference_mode():
            for i,sample in enumerate(samples):
                data=Path(sample['png_absolute_path']).read_bytes()
                if hashlib.sha256(data).hexdigest()!=sample['png_sha256']:raise ValueError('Frozen PNG changed')
                with Image.open(io.BytesIO(data)) as opened:
                    opened.load();rgb=opened.convert('RGB')
                try:
                    raw_png[i]=png_vector(png_module,rgb)
                    pixels=None
                    try:
                        pixels=processor(images=rgb,return_tensors='pt')['pixel_values'].to(device=device,dtype=torch.float32)
                        forward_attempts+=1
                        embedding=model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
                    except torch.OutOfMemoryError:
                        if device=='cpu':raise
                        del pixels;model.to('cpu');torch.cuda.empty_cache();device='cpu'
                        oom.append({'sample_id':sample['sample_id'],'stage':'vision_forward'})
                        pixels=processor(images=rgb,return_tensors='pt')['pixel_values'].to(device=device,dtype=torch.float32)
                        forward_attempts+=1
                        embedding=model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
                    raw_clip[i]=embedding;devices.append(device)
                    if not np.isfinite(embedding).all() or np.linalg.norm(embedding)<=0:raise ValueError('Invalid CLIP feature')
                    del pixels
                finally:rgb.close()
                if (i+1)%32==0 or i+1==352:event('feature_progress',completed=i+1,total=352,device=device)
        validate_feature_matrix(raw_clip,352,512);validate_feature_matrix(raw_png,352,10)
        np.savez_compressed(output/'features.npz',clip=raw_clip,png=raw_png,sample_id=np.array([r['sample_id'] for r in samples]))
        reps=representations(raw_clip,raw_png,bundle['png_scalers'])
        probabilities,calls=classify(bundle,reps)
        write_scores(output,samples,probabilities,thresholds)
        for filename,expected in input_hashes.items():
            if sha(filename)!=expected:raise ValueError('Protected input changed during formal scoring')
        if any(sha(ML/'scripts'/name)!=expected for name,expected in source_hashes.items()):
            raise ValueError('Registered execution source changed during scoring')
        assert not blocked_reads
        save(output/'score_completion.json',{'completed_at':now(),'batch_id':batch_id,'execution_manifest_sha256':sha(output/'execution_manifest.json'),'formal_score_passes':1,'sample_count':352,'vision_forward_attempts':forward_attempts,'successful_embeddings':352,'base_classifier_calls':calls,'H_additional_classifier_calls':0,'fit_calls':0,'oom_recoveries':oom,'row_devices':devices,'probabilities_sha256':sha(output/'probabilities.csv'),'alerts_sha256':sha(output/'alerts.csv'),'features_sha256':sha(output/'features.npz'),'protected_inputs_unchanged':True,'registered_execution_scripts_unchanged':True,'blocked_official_data_accesses':blocked_reads})
        save(output/'failures.json',{'scoring_failures':[],'technical_oom_recoveries':oom,'sample_exclusions':[]})
        event('single_formal_scoring_completed',samples=352,base_classifier_calls=len(calls),H_reuses_probabilities=True,output=str(output))
    except BaseException as exc:
        save(output/'failures.json',{'failed_at':now(),'exception_type':type(exc).__name__,'message':str(exc),'blocked_official_data_accesses':blocked_reads,'formal_run_is_locked':True})
        event('formal_scoring_failed',error=str(exc));raise
    print('SAVED_PROBABILITIES '+str(output),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--fixture',action='store_true');group.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if not os.path.samefile(sys.executable,ML/'.venv/Scripts/python.exe'):
        raise RuntimeError('Use the isolated ML environment Python')
    if args.fixture:fixture()
    else:run()
