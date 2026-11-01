"""4.3F-2B: five preregistered PNG/CLIP LR controls, train-only grouped CV.

Run with ml_stage4/.venv Python. Test feature values are never materialized:
the original array is read-only mmap, indexed by the selected slide IDs first.
All selections are frozen before any validation prediction/label loading.
Existing artifacts are refused, preventing accidental repeat validation runs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import csv
import os
from pathlib import Path
import platform
import re
import sys
import warnings

import joblib
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

BASE = Path(__file__).resolve().parents[1]
SPLIT = BASE / 'reports/dataset_split.csv'
CLIP_INDEX = BASE / 'features/feature_index.csv'
CLIP_ARRAY = BASE / 'features/clip_embeddings.npy'
PNG_CSV = BASE / 'features/png_layout_trainval.csv'
PNG_RECIPE = BASE / 'features/png_layout_recipe.json'
ANNOTATIONS = BASE / 'data/slideaudit/data/annotations'
METADATA = BASE / 'data/slideaudit/data/metadata.csv'
REPORT = BASE / 'reports/png_fusion_cv_report.md'
CONFIG = BASE / 'reports/png_fusion_training_config.json'
OOF_CSV = BASE / 'reports/png_fusion_oof_predictions.csv'
VAL_CSV = BASE / 'reports/png_fusion_validation_predictions.csv'
MODEL = BASE / 'models/png_fusion_candidates.joblib'
CACHE = BASE / '.cache/png_fusion'
OUTPUTS = (REPORT, CONFIG, OOF_CSV, VAL_CSV, MODEL)
LABELS = ('Improper Font Sizing', 'Occluded Content', 'Cluttered Layout', 'Content Overflow/Cut-off')
SLUGS = ('improper_font_sizing', 'occluded_content', 'cluttered_layout', 'content_overflow_cutoff')
FEATURES = ('aspect_ratio', 'edge_density', 'flat_region_ratio', 'gray_std', 'grid_edge_cv',
            'border_edge_ratio', 'horizontal_projection_spread', 'vertical_projection_spread',
            'mean_gray', 'mean_color_range')
SCHEMES = ('A', 'B', 'C', 'B9', 'C9')
DIMENSIONS = {'A': 512, 'B': 10, 'C': 522, 'B9': 9, 'C9': 521}
EXPECTED_POS = {'train': [234, 244, 173, 161], 'validation': [49, 47, 37, 35]}
SPLIT_SHA = 'e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487'
PNG_SHA = '6c7a0299a0ec06451dca81934dc927085b71801e36447c7bb5d987c0237b9257'
RECIPE_PAYLOAD_SHA = '0d1fc0aae630960bcd2236eb11f93634ad4a1457f87195a0c5d5fc533c1f42a3'
FOLD_SHA = 'd0a08d9f6b37359fdceff92f24b83752f8ad134849c1a583ff4a61810ec296c2'
REVISION = '3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268'
MODEL_HASHES = {
    'config_sha256': 'b575ef3c36f2a057fa19e221650105052d61cc9c1a972ec15019c6261ec98770',
    'preprocessor_sha256': '910e70b3956ac9879ebc90b22fb3bc8a75b6a0677814500101a4c072bd7857bd',
    'checkpoint_sha256': 'a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f',
}
THRESHOLDS = tuple(i / 100 for i in range(5, 96))
PROTOCOL = {
    'version': 'png-fusion-groupcv-v1', 'seed': 42,
    'schemes': DIMENSIONS, 'labels': list(LABELS), 'png_feature_order': list(FEATURES),
    'splitter': 'GroupKFold(n_splits=3, shuffle=True, random_state=42)',
    'expected_fold_manifest_sha256': FOLD_SHA,
    'C': [0.1, 1.0, 10.0], 'class_weight': [None, 'balanced'],
    'solver': 'lbfgs', 'max_iter': 3000, 'tol': 1e-6, 'random_state': 42,
    'clip': 'float64 per-row L2; no fitting; only selected train/validation values',
    'png': 'StandardScaler fit on current training fold only; divide by sqrt(10 or 9)',
    'fusion': 'concatenate L2 CLIP and scaled PNG block; no final normalization/weight search',
    'zero_variance': 'StandardScaler uses scale_=1; preserve and report zero-variance columns',
    'selection': 'largest pooled train OOF AP per label; tie lower C, then None',
    'threshold': 'train OOF F1 max on .05..95 step .01; ties nearest .5, then larger; predict p>=t',
    'undefined_metrics': 'AP=None if either truth class absent; P=None if no predicted positive; R=None if no positive; F1=None only if 2TP+FP+FN=0; macros average defined labels and record counts',
    'bootstrap': {'replicates': 2000, 'seed': 42, 'unit': 'complete train group_id',
                  'paired': True, 'ci': '2.5/97.5 percentile; fixed selected OOF predictions, no refit/reselection'},
    'validation': 'freeze all 20 selections/thresholds, fit only train, predict once; load validation truth afterwards',
    'test': 'no test labels/images/predictions/feature values; no full original NPY hash',
    'convergence_warning': 'raise and stop, do not alter budget or continue silently',
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def file_sha(path):
    require(Path(path).resolve() != CLIP_ARRAY.resolve(), 'Never hash whole CLIP array containing test rows')
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def array_sha(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                                    allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def install_guard(rows):
    allowed_annotations = {str((ANNOTATIONS / (r['slide_id'] + '.json')).resolve()).casefold() for r in rows}
    annotation_root = str(ANNOTATIONS.resolve()).casefold() + os.sep
    image_root = str((BASE / 'data/images').resolve()).casefold() + os.sep
    def guard(event, args):
        if event != 'open' or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        mode = args[1] if len(args) > 1 else None
        if isinstance(mode, str) and any(k in mode for k in 'wax') and '+' not in mode:
            return
        folded = str(path).casefold()
        if 'descriptions' in {p.casefold() for p in path.parts}:
            raise PermissionError('Descriptions are not inputs to this experiment')
        if folded.startswith(annotation_root) and folded not in allowed_annotations:
            raise PermissionError('Blocked test/excluded annotation')
        if folded.startswith(image_root):
            raise PermissionError('No raw PNG reads required in this frozen-feature training')
        if path.suffix.lower() in ('.npy', '.npz') and path != CLIP_ARRAY.resolve():
            raise PermissionError('Only original center-crop CLIP array is permitted')
        if path.name.lower() in ('test_predictions.csv', 'clip_lr_4labels.joblib', 'clip_lr_results.md'):
            raise PermissionError('Blocked old formal test artifact')
    sys.addaudithook(guard)


def load_features():
    require(Path(sys.prefix).resolve() == (BASE / '.venv').resolve(), 'Use existing ML .venv Python')
    require(file_sha(SPLIT) == SPLIT_SHA, 'Frozen split changed')
    split = read_csv(SPLIT)
    require(len(split) == 2400 and len({r['slide_id'] for r in split}) == 2400, 'Invalid split IDs')
    require(Counter(r['split'] for r in split) == {'train': 1512, 'validation': 328, 'test': 324, 'excluded': 236}, 'Wrong split counts')
    all_groups = defaultdict(list)
    for row in split:
        if row['group_id']:
            all_groups[row['group_id']].append(row)
    require(len(all_groups) == 541 and all(len(v) == 4 and len({r['split'] for r in v}) == 1
                                         for v in all_groups.values()), 'Frozen groups cross subsets')
    rows = sorted((r for r in split if r['split'] in ('train', 'validation')),
                  key=lambda r: int(r['slide_id'][6:]))
    require(len(rows) == 1840, 'Wrong selected input count')
    install_guard(rows)
    ids = [r['slide_id'] for r in rows]
    idset = set(ids)
    old = [r for r in read_csv(CLIP_INDEX) if r['slide_id'] in idset]
    require(len(old) == len({r['slide_id'] for r in old}) == 1840, 'Missing/duplicate CLIP identities')
    old_by_id = {r['slide_id']: r for r in old}
    png = read_csv(PNG_CSV)
    require(len(png) == 1840 and list(png[0]) == ['slide_id', *FEATURES], 'Wrong PNG rows/column order')
    require([r['slide_id'] for r in png] == ids, 'PNG IDs/order differ from numeric selected IDs')
    recipe = json.loads(PNG_RECIPE.read_text(encoding='utf-8'))
    require(file_sha(PNG_CSV) == recipe['csv_sha256'] == PNG_SHA, 'Frozen PNG features changed')
    require(recipe['payload_sha256'] == digest({k:v for k,v in recipe.items() if k != 'payload_sha256'})
            == RECIPE_PAYLOAD_SHA, 'Frozen PNG recipe changed')
    require(recipe['split_sha256'] == SPLIT_SHA and recipe['feature_names'] == list(FEATURES), 'PNG recipe provenance mismatch')
    require(file_sha(BASE/'scripts/extract_png_layout_features.py') == recipe['source_sha256'], 'PNG extractor changed')
    manifest = []
    positions = []
    for row in rows:
        oldrow = old_by_id[row['slide_id']]
        require((oldrow['group_id'],oldrow['split']) == (row['group_id'],row['split']), 'CLIP identity/split mismatch')
        require(oldrow['model_id'] == 'openai/clip-vit-base-patch32' and oldrow['model_revision'] == REVISION,
                'CLIP model provenance mismatch')
        require(oldrow['feature_dimension'] == '512' and oldrow['dtype'] == 'float32'
                and oldrow['feature_definition'] == 'unnormalized projected image_embeds', 'CLIP feature definition changed')
        for key,value in MODEL_HASHES.items():
            require(oldrow[key] == value, f'CLIP {key} mismatch')
        require(re.fullmatch('[0-9a-f]{64}', oldrow['image_sha256']) is not None, 'Bad image digest')
        manifest.append({'slide_id':row['slide_id'], 'image_sha256':oldrow['image_sha256']})
        positions.append(int(oldrow['row_index']))
    require(manifest == recipe['inputs']['manifest'] and digest(manifest) == recipe['inputs']['manifest_sha256'], 'PNG/CLIP input identity mismatch')
    require(len(set(positions)) == 1840 and min(positions) >= 0 and max(positions) < 2164, 'Bad selected CLIP positions')
    mapping = np.load(CLIP_ARRAY, mmap_mode='r', allow_pickle=False)
    require(mapping.shape == (2164,512) and mapping.dtype == np.float32, 'Invalid original CLIP header')
    # The only operation on array VALUES before deleting the full mmap is this whitelist selection.
    raw = np.array(mapping[np.asarray(positions,dtype=np.int64)], dtype=np.float32, copy=True)
    del mapping
    norms = np.linalg.norm(raw.astype(np.float64), axis=1)
    require(raw.shape == (1840,512) and np.isfinite(raw).all() and np.isfinite(norms).all()
            and (norms > 1e-12).all(), 'Invalid selected CLIP features')
    clip = raw.astype(np.float64) / norms[:,None]
    require(np.allclose(np.linalg.norm(clip,axis=1),1,rtol=0,atol=1e-12), 'CLIP L2 failed')
    png_values = np.asarray([[float(row[n]) for n in FEATURES] for row in png], dtype=np.float64)
    require(png_values.shape == (1840,10) and np.isfinite(png_values).all(), 'Invalid PNG numeric values')
    for j,name in enumerate(FEATURES):
        bounds = recipe['algorithm']['theoretical_ranges'][name]
        require((png_values[:,j] > 0).all() if j == 0 else
                ((png_values[:,j] >= bounds[0]-1e-12)&(png_values[:,j] <= bounds[1]+1e-12)).all(), 'PNG value out of bounds')
    metadata = [r for r in read_csv(METADATA) if f"slide_{int(r['id']):04d}" in idset]
    require(len(metadata) == 1840, 'Missing source diagnostics')
    source_map = {f"slide_{int(r['id']):04d}":r['source_type'] for r in metadata}
    require(len(source_map) == 1840 and all(source_map.values()), 'Duplicate/empty source metadata')
    sources = np.asarray([source_map[sid] for sid in ids], dtype=object)
    claimed_hashes = {r['feature_sha256'] for r in old}
    require(len(claimed_hashes) == 1, 'Mixed original CLIP feature identities')
    provenance = {
        'split_sha256': file_sha(SPLIT), 'png_csv_sha256': file_sha(PNG_CSV),
        'png_recipe_file_sha256': file_sha(PNG_RECIPE), 'png_recipe_payload_sha256': recipe['payload_sha256'],
        'image_manifest_sha256': digest(manifest), 'feature_index_sha256': file_sha(CLIP_INDEX),
        'clip_revision': REVISION, 'clip_artifact_hashes_from_frozen_index': MODEL_HASHES,
        'original_clip_file_sha256_claimed_by_index_not_recomputed': next(iter(claimed_hashes)),
        'clip_selected_positions': positions, 'selected_clip_raw_values_sha256': array_sha(raw),
        'selected_clip_l2_float64_sha256': array_sha(clip), 'selected_png_values_sha256':array_sha(png_values),
        'clip_selected_shape':list(raw.shape),'clip_selected_norm_min':float(norms.min()),
        'clip_selected_norm_max':float(norms.max()), 'test_feature_values_materialized':0,
        'source_type_use':'diagnostic only; not an input column',
    }
    return rows, clip, png_values, sources, provenance, manifest


def load_labels(rows):
    y = np.empty((len(rows),4),dtype=np.uint8)
    h = hashlib.sha256()
    for i,row in enumerate(rows):
        require(row['split'] in ('train','validation'), 'Refuse held-out annotations')
        path = ANNOTATIONS / (row['slide_id']+'.json')
        raw = path.read_bytes()
        h.update(path.name.encode()+b'\0'+raw+b'\0')
        record = json.loads(raw)
        require(type(record.get('slide_id')) is int and record['slide_id'] == int(row['slide_id'][6:]), 'Annotation identity mismatch')
        responses = defaultdict(list)
        for entry in record['annotations']:
            if entry.get('design_deficiency') in LABELS:
                responses[entry['design_deficiency']].append(entry.get('response'))
        for j,label in enumerate(LABELS):
            require(len(responses[label]) == 1 and type(responses[label][0]) is bool, 'Missing/duplicate/non-bool target')
            y[i,j] = int(responses[label][0] is True)
    subset = rows[0]['split']
    require(all(r['split'] == subset for r in rows) and y.sum(axis=0).tolist() == EXPECTED_POS[subset], 'Target count differs from approved counts')
    return y,h.hexdigest()


def make_folds(y,rows):
    groups = np.asarray([r['group_id'] for r in rows], dtype=object)
    require(Counter(Counter(groups).values()) == {4:378}, 'Expected 378 four-page train groups')
    folds = list(GroupKFold(n_splits=3,shuffle=True,random_state=42).split(np.zeros((1512,1)),y,groups))
    numbers = np.full(1512,-1,dtype=np.int8)
    summaries = []
    for k,(fit,hold) in enumerate(folds):
        require(not(set(groups[fit])&set(groups[hold])), 'Group crosses fold')
        require((numbers[hold] == -1).all(), 'OOF double assignment')
        for j in range(4):
            require(set(y[fit,j]) == {0,1} and set(y[hold,j]) == {0,1}, 'Fold lacks a truth class')
        numbers[hold] = k
        summaries.append({'fold_id':k,'fit_pages':len(fit),'holdout_pages':len(hold),
                          'fit_groups':len(set(groups[fit])),'holdout_groups':len(set(groups[hold])),
                          'fit_positive_counts':y[fit].sum(axis=0).tolist(),'holdout_positive_counts':y[hold].sum(axis=0).tolist()})
    manifest = [{'slide_id':r['slide_id'],'group_id':r['group_id'],'fold':int(numbers[i])} for i,r in enumerate(rows)]
    require((numbers >= 0).all() and digest(manifest) == FOLD_SHA, 'CV fold manifest differs from 4.3E')
    return folds,numbers,{'row_fold_manifest_sha256':digest(manifest),'folds':summaries,'row_fold_manifest':manifest}


def scaler_record(scaler,dim):
    names = FEATURES if dim == 10 else FEATURES[1:]
    return {'feature_names':list(names),'mean':scaler.mean_.tolist(),'var':scaler.var_.tolist(),
            'scale':scaler.scale_.tolist(),'n_samples_seen':int(scaler.n_samples_seen_),
            'zero_variance_columns':[n for n,v in zip(names,scaler.var_) if v == 0],
            'fixed_divisor':float(np.sqrt(dim))}


def representation(name, clip, block):
    if name == 'A':
        x = clip
    elif name in ('B','B9'):
        x = block
    else:
        x = np.concatenate((clip,block),axis=1)
    require(x.ndim == 2 and x.shape[1] == DIMENSIONS[name] and np.isfinite(x).all(), 'Wrong representation shape/values')
    return x


def prepare_fold_features(clip,png,folds):
    prepared = {n:[] for n in SCHEMES}
    records = []
    for k,(fit,hold) in enumerate(folds):
        blocks = {}
        detail = {'fold_id':k,'scalers':{}}
        for dim,columns in ((10,slice(None)),(9,slice(1,None))):
            scaler = StandardScaler().fit(png[fit,columns])
            require(scaler.n_samples_seen_ == len(fit), 'Scaler fit includes wrong rows')
            blocks[dim] = (scaler.transform(png[fit,columns])/np.sqrt(dim),
                           scaler.transform(png[hold,columns])/np.sqrt(dim))
            detail['scalers'][str(dim)] = scaler_record(scaler,dim)
        for name in SCHEMES:
            dim = 9 if name.endswith('9') else 10
            train_block,hold_block = blocks[dim]
            prepared[name].append((representation(name,clip[fit],train_block),
                                   representation(name,clip[hold],hold_block)))
        records.append(detail)
    return prepared,records


def fit_model(x,y,c,weight):
    model = LogisticRegression(C=c,class_weight=weight,solver='lbfgs',max_iter=3000,tol=1e-6,random_state=42)
    with warnings.catch_warnings():
        warnings.simplefilter('error',ConvergenceWarning)
        model.fit(x,y)
    require(model.classes_.tolist() == [0,1] and np.isfinite(model.coef_).all()
            and np.isfinite(model.intercept_).all(), 'Invalid fitted LR')
    return model


def ratio(a,b):
    return float(a/b) if b else None


def binary(y,p,t):
    pred = p >= t
    tp,fp,tn,fn = (int(np.count_nonzero(mask)) for mask in
                  ((y==1)&pred,(y==0)&pred,(y==0)&~pred,(y==1)&~pred))
    positive,negative = int(y.sum()),len(y)-int(y.sum())
    return {'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn), 'f1':ratio(2*tp,2*tp+fp+fn),
            'average_precision':float(average_precision_score(y,p)) if positive and negative else None,
            'tp':tp,'fp':fp,'tn':tn,'fn':fn,'actual_positive':positive,'actual_negative':negative,
            'predicted_positive':tp+fp,'positive_rate':positive/len(y),'threshold':float(t)}


def summary(y,p,thresholds):
    result = {'per_label':{label:binary(y[:,j],p[:,j],thresholds[j]) for j,label in enumerate(LABELS)}}
    values = list(result['per_label'].values())
    result['macro'] = {}
    result['macro_defined_label_counts'] = {}
    for name in ('precision','recall','f1','average_precision'):
        valid = [m[name] for m in values if m[name] is not None]
        result['macro'][name] = float(np.mean(valid)) if valid else None
        result['macro_defined_label_counts'][name] = len(valid)
    tp,fp,tn,fn = (sum(m[name] for m in values) for name in ('tp','fp','tn','fn'))
    result['micro'] = {'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn),'f1':ratio(2*tp,2*tp+fp+fn),
                       'tp':tp,'fp':fp,'tn':tn,'fn':fn}
    return result


def choose_threshold(y,p):
    candidates = []
    for t in THRESHOLDS:
        pred = p >= t
        tp = np.count_nonzero((y==1)&pred); fp = np.count_nonzero((y==0)&pred); fn = np.count_nonzero((y==1)&~pred)
        f1 = float(2*tp/(2*tp+fp+fn)) if 2*tp+fp+fn else 0.0
        candidates.append((f1,-round(abs(t-.5),12),t))
    return max(candidates)[2]


def grouped_ranking(y,p,rows):
    groups = defaultdict(list)
    for i,row in enumerate(rows): groups[row['group_id']].append(i)
    require(all(len(v) == 4 for v in groups.values()), 'Incomplete ranking groups')
    result = {}
    for j,label in enumerate(LABELS):
        total=wins=ties=0; aucs=[]
        for positions in groups.values():
            truth,scores = y[positions,j],p[positions,j]
            if truth.min() == truth.max(): continue
            differences = scores[truth==1][:,None]-scores[truth==0][None,:]
            w=int((differences>0).sum());t=int((differences==0).sum());n=differences.size
            wins+=w;ties+=t;total+=n;aucs.append((w+.5*t)/n)
        result[label] = {'comparable_groups':len(aucs),'positive_negative_pairs':int(total),
                         'wins':wins,'ties':ties,'pooled_pair_auc':ratio(wins+.5*ties,total),
                         'equal_group_auc':float(np.mean(aucs)) if aucs else None}
    return result


def diagnostic(y,p,thresholds,rows,sources):
    return {'selected_threshold':summary(y,p,thresholds),'default_0_5':summary(y,p,[.5]*4),
            'within_group':grouped_ranking(y,p,rows),
            'by_source':{str(s):{'pages':int(np.sum(sources==s)),
                                'metrics':summary(y[sources==s],p[sources==s],thresholds)}
                         for s in sorted(set(sources))}}


def run_cv(name,prepared,y,folds):
    chosen = np.empty((1512,4),dtype=np.float64); selections=[]; trials=[]
    for j,label in enumerate(LABELS):
        best=None
        for c in PROTOCOL['C']:
            for weight in PROTOCOL['class_weight']:
                probability=np.full(1512,np.nan); iterations=[]
                for k,(fit,hold) in enumerate(folds):
                    fit_x,hold_x=prepared[name][k]
                    model=fit_model(fit_x,y[fit,j],c,weight)
                    probability[hold]=model.predict_proba(hold_x)[:,1]
                    iterations.append(int(model.n_iter_[0]))
                require(np.isfinite(probability).all() and ((probability>=0)&(probability<=1)).all(), 'Incomplete OOF')
                ap=float(average_precision_score(y[:,j],probability))
                trial={'label':label,'C':c,'class_weight':weight,'oof_ap':ap,'fold_n_iter':iterations,
                       'oof_probability_sha256':array_sha(probability),'selected':False}
                trials.append(trial)
                key=(ap,-c,0 if weight is None else -1)
                if best is None or key > best[0]: best=(key,probability,trial)
        _,probability,trial=best;trial['selected']=True
        threshold=choose_threshold(y[:,j],probability)
        selections.append({k:trial[k] for k in ('label','C','class_weight','oof_ap','fold_n_iter')}
                          | {'threshold':threshold})
        chosen[:,j]=probability
        print(json.dumps({'event':'oof_selected','scheme':name,**selections[-1]},ensure_ascii=True),flush=True)
    return chosen,{'selections':selections,'trials':trials,'cv_fits':72,'dimension':DIMENSIONS[name]}


def bootstrap(y,probabilities,rows):
    """Exact tied-score weighted AP; paired bootstrap counts whole groups."""
    groups=np.asarray([r['group_id'] for r in rows]); unique,inverse=np.unique(groups,return_inverse=True)
    require(len(unique)==378 and np.all(np.bincount(inverse)==4), 'Bad bootstrap groups')
    prepared={}
    for name in ('A','C','C9'):
        prepared[name]=[]
        for j in range(4):
            p=probabilities[name][:,j];order=np.argsort(-p,kind='stable')
            ends=np.r_[np.flatnonzero(np.diff(p[order])!=0),len(p)-1]
            prepared[name].append((order,ends,y[order,j]))
    def weighted_ap(item,weights):
        order,ends,truth=item; w=weights[order]
        positives=np.cumsum(w*truth,dtype=np.float64)[ends];total=np.cumsum(w,dtype=np.float64)[ends]
        require(positives[-1]>0 and total[-1]>positives[-1], 'Bootstrap missing class')
        precision=np.divide(positives,total,out=np.zeros_like(positives),where=total>0)
        return float(np.sum(np.diff(np.r_[0.0,positives])*precision)/positives[-1])
    # Independent library cross-check of the optimized formula, including ties/zero weights.
    test_weights=np.random.default_rng(17).integers(0,4,len(rows))
    for name in prepared:
        for j,item in enumerate(prepared[name]):
            expected=average_precision_score(y[:,j],probabilities[name][:,j],sample_weight=test_weights)
            require(abs(weighted_ap(item,test_weights)-expected)<1e-12,'Weighted AP implementation mismatch')
    rng=np.random.default_rng(PROTOCOL['bootstrap']['seed']); samples={'C':[],'C9':[]}
    for _ in range(PROTOCOL['bootstrap']['replicates']):
        counts=np.bincount(rng.integers(0,len(unique),len(unique)),minlength=len(unique))
        weights=counts[inverse]
        ap={name:np.array([weighted_ap(item,weights) for item in items]) for name,items in prepared.items()}
        for name in samples: samples[name].append(ap[name]-ap['A'])
    result={}
    for name,sample in samples.items():
        sample=np.asarray(sample); macro=sample.mean(axis=1)
        observed=np.array([average_precision_score(y[:,j],probabilities[name][:,j])-
                           average_precision_score(y[:,j],probabilities['A'][:,j]) for j in range(4)])
        result[name+'_minus_A']={'macro_ap_difference':float(observed.mean()),
            'macro_ap_95_percentile_interval':np.percentile(macro,[2.5,97.5]).tolist(),
            'per_label':{label:{'ap_difference':float(observed[j]),
                               'ap_95_percentile_interval':np.percentile(sample[:,j],[2.5,97.5]).tolist()}
                         for j,label in enumerate(LABELS)}}
    return {'protocol':PROTOCOL['bootstrap'],'results':result,'weighted_ap_oracle_passed':True,
            'limitation':'Conditional on selected OOF models; does not cover model/threshold selection, refitting uncertainty or CV dependence; exploratory only'}


def write_predictions(path,rows,sources,y,probabilities,selections,fold_ids=None):
    fields=['slide_id','group_id','split','source_type','fold_id']+[f'true_{s}' for s in SLUGS]
    fields += [f'{name}_{kind}_{s}' for name in SCHEMES for kind in ('prob','pred','threshold') for s in SLUGS]
    temporary=path.with_suffix('.csv.part')
    with temporary.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,lineterminator='\n');writer.writeheader()
        for i,row in enumerate(rows):
            record={k:row[k] for k in ('slide_id','group_id','split')}
            record.update(source_type=str(sources[i]),fold_id='' if fold_ids is None else int(fold_ids[i]))
            record.update({f'true_{s}':int(y[i,j]) for j,s in enumerate(SLUGS)})
            for name in SCHEMES:
                for j,s in enumerate(SLUGS):
                    prob=float(probabilities[name][i,j]); threshold=selections[name][j]['threshold']
                    record[f'{name}_prob_{s}']=format(prob,'.17g')
                    record[f'{name}_pred_{s}']=int(prob>=threshold)
                    record[f'{name}_threshold_{s}']=format(threshold,'.17g')
            writer.writerow(record)
    os.replace(temporary,path)
    restored=read_csv(path)
    require([(r['slide_id'],r['group_id'],r['split']) for r in restored] ==
            [(r['slide_id'],r['group_id'],r['split']) for r in rows], 'Prediction CSV identity mismatch')
    require([r['source_type'] for r in restored] == [str(s) for s in sources], 'CSV source diagnostic mismatch')
    require([r['fold_id'] for r in restored] ==
            (['']*len(rows) if fold_ids is None else [str(int(k)) for k in fold_ids]), 'CSV fold identity mismatch')
    require(np.array_equal(np.array([[int(r[f'true_{s}']) for s in SLUGS] for r in restored]),y), 'Prediction truth mismatch')
    for name in SCHEMES:
        p=np.array([[float(r[f'{name}_prob_{s}']) for s in SLUGS] for r in restored])
        pred=np.array([[int(r[f'{name}_pred_{s}']) for s in SLUGS] for r in restored])
        thresholds=np.array([c['threshold'] for c in selections[name]])
        exported_thresholds=np.array([[float(r[f'{name}_threshold_{s}']) for s in SLUGS] for r in restored])
        require(np.array_equal(exported_thresholds,np.broadcast_to(thresholds,p.shape)), 'CSV threshold mismatch')
        require(np.array_equal(p,probabilities[name]) and np.array_equal(pred,p>=thresholds), 'CSV probability/prediction mismatch')


def fit_final_and_predict(clip,png,y,choices):
    clip_train,clip_valid=clip['train'],clip['validation']
    scalers={};train_blocks={};val_blocks={};records={}
    for dim,columns in ((10,slice(None)),(9,slice(1,None))):
        scaler=StandardScaler().fit(png['train'][:,columns])
        require(scaler.n_samples_seen_==1512,'Final scaler fit includes non-train')
        scalers[str(dim)]=scaler;records[str(dim)]=scaler_record(scaler,dim)
        train_blocks[dim]=scaler.transform(png['train'][:,columns])/np.sqrt(dim)
        val_blocks[dim]=scaler.transform(png['validation'][:,columns])/np.sqrt(dim)
    probabilities={};models={};iterations={}
    for name in SCHEMES:
        dim=9 if name.endswith('9') else 10
        fit_x=representation(name,clip_train,train_blocks[dim])
        valid_x=representation(name,clip_valid,val_blocks[dim])
        p=np.empty((328,4));models[name]=[];iterations[name]=[]
        for j,choice in enumerate(choices[name]):
            model=fit_model(fit_x,y[:,j],choice['C'],choice['class_weight'])
            p[:,j]=model.predict_proba(valid_x)[:,1]  # Exactly one call per final binary model.
            models[name].append(model);iterations[name].append(int(model.n_iter_[0]))
        require(np.isfinite(p).all() and ((p>=0)&(p<=1)).all(),'Invalid validation probabilities')
        probabilities[name]=p
        print(json.dumps({'event':'validation_predicted_once','scheme':name,'pages':328}),flush=True)
    return probabilities,models,scalers,{'scalers':records,'model_n_iter':iterations,'fit_count':20,'predict_proba_calls':20}


def conflict_diagnostics(rows,y,probabilities,manifest):
    ids={r['slide_id']:i for i,r in enumerate(rows)}; hashes={r['slide_id']:r['image_sha256'] for r in manifest}
    result=[]
    for left,right in (('slide_0383','slide_0459'),('slide_0514','slide_2390'),('slide_1312','slide_2227')):
        a,b=ids[left],ids[right]
        require(hashes[left]==hashes[right] and rows[a]['group_id']==rows[b]['group_id'],'Known duplicate identity changed')
        result.append({'slide_ids':[left,right],'group_id':rows[a]['group_id'],
                       'image_sha256':hashes[left],'truth':[y[a].tolist(),y[b].tolist()],
                       'conflicting_labels':[LABELS[j] for j in range(4) if y[a,j]!=y[b,j]],
                       'oof_probabilities':{name:[p[a].tolist(),p[b].tolist()] for name,p in probabilities.items()}})
    return result


def fmt(value):
    return 'N/A' if value is None else f'{value:.4f}'


def interpret(result):
    oof,valid=result['oof'],result['validation']
    def overall(results,name): return results[name]['selected_threshold']
    a,c,c9=(overall(oof,n) for n in ('A','C','C9'))
    va,vc,vc9=(overall(valid,n) for n in ('A','C','C9'))
    dap=c['macro']['average_precision']-a['macro']['average_precision']
    dap9=c9['macro']['average_precision']-a['macro']['average_precision']
    vf=vc['macro']['f1']-va['macro']['f1']
    vap=vc['macro']['average_precision']-va['macro']['average_precision']
    low,high=result['bootstrap']['results']['C_minus_A']['macro_ap_95_percentile_interval']
    changes=[]
    for label in LABELS:
        x,y=c['per_label'][label],a['per_label'][label]
        changes.append(f"{label}: AP {x['average_precision']-y['average_precision']:+.4f}，F1 {x['f1']-y['f1']:+.4f}")
    source_changes=[]
    for source in oof['A']['by_source']:
        x=oof['C']['by_source'][source]['metrics']['macro']['average_precision']
        y=oof['A']['by_source'][source]['metrics']['macro']['average_precision']
        source_changes.append(f'{source} ΔMacro AP={x-y:+.4f}')
    ranking=[]
    for label in LABELS:
        x=oof['C']['within_group'][label]['pooled_pair_auc'];y=oof['A']['within_group'][label]['pooled_pair_auc']
        ranking.append(f'{label} {x-y:+.4f}' if x is not None and y is not None else label+' N/A')
    dfp=c['micro']['fp']-a['micro']['fp'];dfn=c['micro']['fn']-a['micro']['fn']
    both='是' if dfp<0 and dfn<0 else '否，未同时降低两者'
    supported=dap>0 and dap9>0 and vap>0 and vf>0 and low>0
    next_step=('有理由保留小规模研究候选并做预先设计的独立复核，但仍不足以替换正式模型。'
               if supported else '本轮没有形成足够稳定的总体收益证据，应停止继续追加同类PNG/CLIP组合或反复调参；保留负结果，只有在新数据、标注审查或明确局部检测假设出现时再立项。')
    return [
        f"1. **哪种最好？** train OOF Macro AP最高为{result['best_oof_macro_ap_scheme']}；五组按同一主要指标选择，未用validation选方案。",
        f"2. **C相对A幅度？** ΔMacro AP={dap:+.4f}，ΔMacro F1={c['macro']['f1']-a['macro']['f1']:+.4f}；探索性整组bootstrap AP差区间[{low:+.4f},{high:+.4f}]。",
        '3. **各标签提升还是下降？** C−A：'+'；'.join(changes)+'。AP与F1可能方向不同，不能只选其中好看的数。',
        f'4. **同时控制误报漏检？** {both}。C−A总FP={dfp:+d}、FN={dfn:+d}，均使用各自train OOF预先选定阈值。',
        f"5. **去aspect_ratio仍改善？** C9−A ΔMacro AP={dap9:+.4f}、ΔMacro F1={c9['macro']['f1']-a['macro']['f1']:+.4f}；validation的C9−A ΔMacro AP={vc9['macro']['average_precision']-va['macro']['average_precision']:+.4f}。宽高比的作用不能单凭总体分数认定为因果，其他特征仍含来源信息。",
        '6. **来源一致？** C−A train OOF：'+'；'.join(source_changes)+'。结合来源正例率和各来源四标签表判断，不能把整体差值当所有来源共同收益。',
        '7. **组内区分是否提高？** C−A pooled组内排序AUC变化：'+'；'.join(ranking)+'。详表同时保留等组权AUC、配对数和C9，不把来源间排序当组内缺陷识别。',
        f'8. **validation支持吗？** C−A ΔMacro AP={vap:+.4f}，ΔMacro F1={vf:+.4f}；这是一次冻结后探索性诊断，若方向不一致则不能宣称稳定改善，也没有据此再调参。',
        '9. **足以替换4.3C？** 否。只有内部OOF和已多轮观察的validation，且存在选参/阈值偏差、来源关联、四页相关性和矛盾监督；本轮不做test，不能凭研究对照升级正式模型。',
        '10. **下一步继续还是停止？** '+next_step,
    ]


def generate_report(result):
    oof=result['oof'];val=result['validation'];best=result['best_oof_macro_ap_scheme']
    def m(subset,name): return subset[name]['selected_threshold']
    lines=['# 4.3F-2B：冻结CLIP + PNG布局统计融合对照', '',
           f"实际运行 {result['started_at']} 至 {result['finished_at']}。只使用train1512页/378组和validation328页/82组；test封存，excluded不参与。五组360次CV拟合及20次全train拟合均实际完成，收敛警告0。模型均为candidate，未替换4.3C、未接入智能体。", '',
           '## 1. 固定输入、对齐与尺度', '',
           'A=原中心裁剪CLIP512维；B=PNG十特征10维；C=CLIP+PNG522维；B9=去掉aspect_ratio的PNG9维；C9=CLIP+PNG9共521维。PNG统计不等于原PPTX对象结构。本轮没有新特征、图像下载、CLIP提取、微调或额外分类器。', '',
           f'CLIP openai/clip-vit-base-patch32，revision `{REVISION}`。原float32数组只读mmap，严格按slide_id映射先提取1840个白名单行，才做有限性、范数检查与float64逐行L2归一化；未加载/统计/变换test行，也没有为计算整个旧NPY摘要而读取test值。选中行与PNG CSV、recipe图像SHA及split/group逐ID吻合，无缺失重复。', '',
           'PNG列按4.3F-2A冻结顺序，每折StandardScaler只fit1008个训练折样本；变换本折训练/留出后除以sqrt(10)或sqrt(9)。B/C共享同一10维变换，B9/C9共享同一9维变换；融合直接拼接，不再归一化或搜索块权重。最后scaler只fit1512个train。零方差列按StandardScaler scale=1保留；每折/最终列名、mean/var/scale和样本数完整记录在配置JSON中。', '',
           'X只含上述视觉数值；source_type、alteration、ID、group、split、annotation bbox/agreement/缺陷名称/response不进入X。annotations仅逐目标读取真实bool response形成y。validation真值在所有预测完成后才加载。', '',
           '| 标签 | train正/负 | validation正/负 |', '| --- | --- | --- |']
    for j,label in enumerate(LABELS): lines.append(f'| {label} | {EXPECTED_POS["train"][j]}/{1512-EXPECTED_POS["train"][j]} | {EXPECTED_POS["validation"][j]}/{328-EXPECTED_POS["validation"][j]} |')
    lines += ['', '## 2. 固定分组CV、选择与不确定性', '',
              f'沿用4.3E：3折GroupKFold(shuffle=True, random_state=42)，按数值slide_id排序。每组4页不跨折，所有五方案同折；折清单SHA-256=`{FOLD_SHA}`。每折1008训练/504留出、252/126组，训练和留出各标签都有正负例。', '',
              'LR固定lbfgs/max_iter=3000/tol=1e-6/random_state=42。每方案每标签6配置：C=0.1/1/10 × class_weight=None/balanced；各配置均产生完整train OOF概率。按pooled OOF AP最大选参数，同分取较小C、再None。每个入选标签仅在train OOF上于0.05–0.95、步长0.01选F1最大阈值；同分优先距0.5近、再较大阈值。预测采用p>=threshold。', '',
              '同一OOF同时用于选参数、选阈值并汇报最佳结果，存在乐观选择偏差；不是完全无偏的泛化评估。整组配对bootstrap固定2000次、seed42，378个group有放回抽样，四页一起重复，比较方案共享同一抽样；区间只针对已选OOF概率，不包含重新拟合/选参或CV模型依赖的不确定性，属于探索性检查。没有根据区间改模型。', '',
              '所有20组参数/阈值先冻结到cache并记录规范化SHA，再进行20次最终train拟合及各一次validation概率预测。validation已在旧阶段反复观察，仍是探索性诊断；本轮A必须与本轮其他方案比较，不能与旧选参规则下的A直接归因比较。', '',
              '## 3. train OOF总体结果', '', '| 方案/维度 | Macro AP | Macro P | Macro R | Macro F1 | Micro P | Micro R | Micro F1 | FP | FN |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        q=m(oof,name);a=q['macro'];u=q['micro']
        lines.append(f'| {name}/{DIMENSIONS[name]} | '+ ' | '.join(fmt(v) for v in (a['average_precision'],a['precision'],a['recall'],a['f1'],u['precision'],u['recall'],u['f1']))+f" | {u['fp']} | {u['fn']} |")
    lines += ['', f'主要准则Macro OOF AP最高为 **{best}**，未用validation选择最佳方案。', '',
              '| 方案 | 标签 | AP | Precision | Recall | F1 | TP | FP | TN | FN | 实际正例 | 预测正例 |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        for label,q in m(oof,name)['per_label'].items():
            lines.append(f'| {name} | {label} | '+' | '.join(fmt(q[k]) for k in ('average_precision','precision','recall','f1'))+' | '+' | '.join(str(q[k]) for k in ('tp','fp','tn','fn','actual_positive','predicted_positive'))+' |')
    lines += ['', '### 参数与阈值（全部来自train OOF）', '', '| 方案 | 标签 | C | class_weight | threshold |', '| --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        for q in result['cv'][name]['selections']: lines.append(f"| {name} | {q['label']} | {q['C']} | {q['class_weight']} | {q['threshold']:.2f} |")
    lines += ['', '### 相对A与去宽高比敏感性', '', '| 对照差值 | ΔMacro AP | ΔMacro F1 | ΔFP | ΔFN |', '| --- | --- | --- | --- | --- |']
    for left,right in (('C','A'),('C9','A'),('B9','B'),('C9','C')):
        a,b=m(oof,left),m(oof,right)
        lines.append(f"| {left}−{right} | {a['macro']['average_precision']-b['macro']['average_precision']:+.4f} | {a['macro']['f1']-b['macro']['f1']:+.4f} | {a['micro']['fp']-b['micro']['fp']:+d} | {a['micro']['fn']-b['micro']['fn']:+d} |")
    lines += ['', '| 对照 | 标签 | ΔAP | ΔF1 | ΔFP | ΔFN |', '| --- | --- | --- | --- | --- | --- |']
    for name in ('C','C9'):
        for label in LABELS:
            a,b=m(oof,name)['per_label'][label],m(oof,'A')['per_label'][label]
            lines.append(f"| {name}−A | {label} | {a['average_precision']-b['average_precision']:+.4f} | {a['f1']-b['f1']:+.4f} | {a['fp']-b['fp']:+d} | {a['fn']-b['fn']:+d} |")
    lines += ['', '| 组bootstrap对照 | 目标 | AP差值 | 95% percentile区间 |', '| --- | --- | --- | --- |']
    for name,q in result['bootstrap']['results'].items():
        low,high=q['macro_ap_95_percentile_interval'];lines.append(f"| {name} | Macro | {q['macro_ap_difference']:+.4f} | [{low:+.4f}, {high:+.4f}] |")
        for label,d in q['per_label'].items():
            low,high=d['ap_95_percentile_interval'];lines.append(f"| {name} | {label} | {d['ap_difference']:+.4f} | [{low:+.4f}, {high:+.4f}] |")
    lines += ['', '## 4. 来源诊断', '',
              '宽高比不是直接标签泄漏，但在本范围内Gemini/Google均16:9、gdcdataset均4:3，存在来源捷径。去掉宽高比不能消除CLIP、色彩和纹理中全部来源信息。来源字段只用于下表诊断。P无预测正例、R无真实正例或AP缺少任一真实类别时为N/A；macro仅平均可定义标签并在JSON记录有效数，不能把不可定义值冒充0。', '',
              '| 集合 | 来源 | 页数 | 字号正例 | 遮挡正例 | 杂乱正例 | 溢出正例 |', '| --- | --- | --- | --- | --- | --- | --- |']
    for subset,results in (('train OOF',oof),('validation',val)):
        for source,d in results['A']['by_source'].items():
            q=d['metrics']['per_label'];lines.append(f"| {subset} | {source} | {d['pages']} | "+' | '.join(f"{q[l]['actual_positive']}/{d['pages']} ({q[l]['positive_rate']:.1%})" for l in LABELS)+' |')
    lines += ['', '| 集合 | 方案 | 来源 | Macro AP | Macro F1 | FP | FN |', '| --- | --- | --- | --- | --- | --- | --- |']
    for subset,results in (('train OOF',oof),('validation',val)):
        for name in SCHEMES:
            for source,d in results[name]['by_source'].items():
                q=d['metrics'];lines.append(f"| {subset} | {name} | {source} | {fmt(q['macro']['average_precision'])} | {fmt(q['macro']['f1'])} | {q['micro']['fp']} | {q['micro']['fn']} |")
    lines += ['', '<details><summary>train OOF分来源四标签完整指标</summary>', '', '| 方案 | 来源 | 标签 | AP | P | R | F1 | TP | FP | TN | FN |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        for source,d in oof[name]['by_source'].items():
            for label,q in d['metrics']['per_label'].items():
                lines.append(f'| {name} | {source} | {label} | '+' | '.join(fmt(q[k]) for k in ('average_precision','precision','recall','f1'))+' | '+' | '.join(str(q[k]) for k in ('tp','fp','tn','fn'))+' |')
    lines += ['', '</details>', '', '## 5. 同源组内增强版本排序', '',
              '对每个同时有正负例的四页组枚举全部正负页对：正例概率更高计1，相同计0.5。pooled AUC按配对数加权，另报各组等权AUC。这是页内缺陷变化诊断，不等于独立原始页面级评估。无可比较组时为N/A。', '',
              '| 集合 | 标签 | 可比较组 | 正负配对 | A pooled AUC | B | C | B9 | C9 | A组等权AUC | C组等权AUC | C9组等权AUC |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for subset,results in (('train OOF',oof),('validation',val)):
        for label in LABELS:
            a=results['A']['within_group'][label]
            lines.append(f"| {subset} | {label} | {a['comparable_groups']} | {a['positive_negative_pairs']} | "+' | '.join(fmt(results[n]['within_group'][label]['pooled_pair_auc']) for n in SCHEMES)+' | '+' | '.join(fmt(results[n]['within_group'][label]['equal_group_auc']) for n in ('A','C','C9'))+' |')
    lines += ['', '## 6. 冻结后validation一次探索性诊断', '', '| 方案 | Macro AP | Macro P | Macro R | Macro F1 | Micro F1 | FP | FN |', '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        q=m(val,name);lines.append(f'| {name} | '+' | '.join(fmt(q['macro'][k]) for k in ('average_precision','precision','recall','f1'))+f" | {fmt(q['micro']['f1'])} | {q['micro']['fp']} | {q['micro']['fn']} |")
    lines += ['', '| 方案 | 标签 | AP | Precision | Recall | F1 | TP | FP | TN | FN | 实际正例 | 预测正例 |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in SCHEMES:
        for label,q in m(val,name)['per_label'].items():
            lines.append(f'| {name} | {label} | '+' | '.join(fmt(q[k]) for k in ('average_precision','precision','recall','f1'))+' | '+' | '.join(str(q[k]) for k in ('tp','fp','tn','fn','actual_positive','predicted_positive'))+' |')
    lines += ['', '默认0.5阈值的全套对照、分来源及组内指标同样保存在配置JSON；没有据validation重新选参数、阈值或方案。', '',
              '## 7. 重复PNG、候选模型与复现记录', '',
              '保留三对已知矛盾样本，不删除、重标注或移动组。它们在同组同折，原图片SHA和PNG统计相同；OOF概率与各自标签已记录于预测CSV及配置conflicts字段。安全图像特征不能凭ID区分相同像素的相反监督。', '',
              '| 页面对 | group_id | 四标签 | 冲突标签 |', '| --- | --- | --- | --- |']
    for q in result['conflicts']:
        lines.append('| '+' / '.join(q['slide_ids'])+f" | {q['group_id']} | {q['truth']} | "+'、'.join(q['conflicting_labels'])+' |')
    lines += ['', '新模型文件保存五组研究候选各四个LR、仅train拟合的两个scaler、标签顺序、阈值、特征块尺度/顺序、CLIP revision、PNG recipe和冻结选择摘要；不是正式替代模型，不含旧模型副本或新CLIP权重。配置JSON保存完整6配置搜索结果、fold清单、scaler参数、n_iter、来源/组内/默认阈值指标和bootstrap。', '',
              '预测CSV分开保存1512行OOF和328行validation，均含slide_id/group_id/split/source_type/fold_id、4真值、五方案各4概率/预测/阈值，17位有效数字可独立重算全部表。source只作诊断列。程序已重新读取CSV核对身份、真值、概率及阈值判定；附加独立验算结果在最终验收中记录。', '',
              f"环境：{json.dumps(result['versions'],ensure_ascii=False)}。训练代码SHA-256=`{result['training_script_sha256']}`；冻结选择SHA-256=`{result['frozen_selection_sha256']}`。", '',
              '| 输入/产物 | SHA-256 |', '| --- | --- |']
    for key in ('split_sha256','png_csv_sha256','png_recipe_file_sha256','png_recipe_payload_sha256','image_manifest_sha256','feature_index_sha256','selected_clip_raw_values_sha256','selected_clip_l2_float64_sha256'):
        lines.append(f"| {key} | `{result['inputs'][key]}` |")
    for path,value in result['output_sha256'].items(): lines.append(f'| {path} | `{value}` |')
    lines += ['', '复现入口：在ml_stage4中运行 `.\\.venv\\Scripts\\python.exe -B scripts\\train_png_fusion_cv.py`。脚本拒绝覆盖现有本轮产物；`--validate-only`只检查输入，不训练、不读取validation标签。正常流程只运行了一次，不提供自动重复validation/调参入口。', '',
              '## 8. 末尾十项直接回答', '',
              *interpret(result), '',
              '本轮停止，等待验收；没有运行test或系统集成。']
    REPORT.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-only',action='store_true')
    args=parser.parse_args()
    if not args.validate_only:
        require(not any(p.exists() for p in OUTPUTS),'This experiment already has outputs; refusing overwrite/repeated validation')
    started=now()
    rows,clip,png,sources,provenance,manifest=load_features()
    masks={subset:np.array([r['split']==subset for r in rows]) for subset in ('train','validation')}
    subset_rows={s:[r for r in rows if r['split']==s] for s in masks}
    subset_sources={s:sources[mask] for s,mask in masks.items()}
    train_y,train_label_sha=load_labels(subset_rows['train'])
    folds,fold_ids,fold_record=make_folds(train_y,subset_rows['train'])
    if args.validate_only:
        print(json.dumps({'status':'passed','feature_rows':len(rows),'train_label_counts':train_y.sum(axis=0).tolist(),
                          'fold_sha256':fold_record['row_fold_manifest_sha256'],
                          'clip_selected_shape':provenance['clip_selected_shape'],
                          'selected_clip_values_sha256':provenance['selected_clip_raw_values_sha256'],
                          'png_recipe_payload_sha256':provenance['png_recipe_payload_sha256'],
                          'test_feature_values_materialized':0},ensure_ascii=True))
        return
    prepared,scaler_records=prepare_fold_features(clip[masks['train']],png[masks['train']],folds)
    cv={};oof_p={}
    with threadpool_limits(limits=1):
        for name in SCHEMES:
            oof_p[name],cv[name]=run_cv(name,prepared,train_y,folds)
        choices={name:cv[name]['selections'] for name in SCHEMES}
        frozen={'protocol':PROTOCOL,'inputs':provenance,'choices':choices,'fold_sha256':FOLD_SHA,
                'training_script_sha256':file_sha(Path(__file__))}
        frozen_sha=digest(frozen);save_json(CACHE/'frozen_selection.json',frozen)
        frozen_at=now()
        thresholds={n:[c['threshold'] for c in choices[n]] for n in SCHEMES}
        oof={n:diagnostic(train_y,oof_p[n],thresholds[n],subset_rows['train'],subset_sources['train']) for n in SCHEMES}
        uncertainty=bootstrap(train_y,oof_p,subset_rows['train'])
        write_predictions(OOF_CSV,subset_rows['train'],subset_sources['train'],train_y,oof_p,choices,fold_ids)
        validation_started=now()
        val_p,models,scalers,final_record=fit_final_and_predict(
            {s:clip[mask] for s,mask in masks.items()},{s:png[mask] for s,mask in masks.items()},train_y,choices)
    require(digest(frozen)==frozen_sha and json.loads((CACHE/'frozen_selection.json').read_text(encoding='utf-8'))==frozen,
            'Selections changed during validation')
    valid_y,valid_label_sha=load_labels(subset_rows['validation'])
    validation={n:diagnostic(valid_y,val_p[n],thresholds[n],subset_rows['validation'],subset_sources['validation']) for n in SCHEMES}
    write_predictions(VAL_CSV,subset_rows['validation'],subset_sources['validation'],valid_y,val_p,choices)
    bundle={'status':'candidate; research only; not promoted or integrated','labels':LABELS,'slugs':SLUGS,
            'schemes':DIMENSIONS,'models':models,'png_scalers':scalers,'selections':choices,
            'feature_preprocessing':PROTOCOL,'clip_revision':REVISION,'png_recipe_payload_sha256':RECIPE_PAYLOAD_SHA,
            'split_sha256':SPLIT_SHA,'frozen_selection_sha256':frozen_sha}
    temporary=MODEL.with_suffix('.joblib.part');joblib.dump(bundle,temporary,compress=3);os.replace(temporary,MODEL)
    versions={n:importlib.metadata.version(n) for n in ('numpy','scikit-learn','scipy','joblib','threadpoolctl')}
    versions['python']=platform.python_version()
    best=max(SCHEMES,key=lambda n:(oof[n]['selected_threshold']['macro']['average_precision'],-DIMENSIONS[n]))
    result={'protocol':PROTOCOL,'started_at':started,'finished_at':now(),'versions':versions,'inputs':provenance,
            'train_label_manifest_sha256':train_label_sha,'validation_label_manifest_sha256':valid_label_sha,
            'training_script_sha256':file_sha(Path(__file__)),'folds':fold_record,'fold_scalers':scaler_records,
            'cv':cv,'cv_fit_count':360,'final_fit':final_record,'convergence_warnings':[],
            'frozen_selection_sha256':frozen_sha,'selection_frozen_at':frozen_at,
            'validation_prediction_started_at':validation_started,'validation_truth_loaded_after_all_predictions':True,
            'oof':oof,'validation':validation,'bootstrap':uncertainty,'best_oof_macro_ap_scheme':best,
            'conflicts':conflict_diagnostics(subset_rows['train'],train_y,oof_p,manifest),
            'output_sha256':{str(p.relative_to(BASE)):file_sha(p) for p in (OOF_CSV,VAL_CSV,MODEL)},
            'csv_roundtrip_identity_truth_probability_prediction_checks':'passed','test_rows_accessed':0}
    save_json(CONFIG,result)
    result['output_sha256'][str(CONFIG.relative_to(BASE))]=file_sha(CONFIG)
    generate_report(result)
    print(json.dumps({'status':'passed','best_oof_macro_ap_scheme':best,'cv_fits':360,'final_fits':20,
                      'oof_macro_ap':{n:oof[n]['selected_threshold']['macro']['average_precision'] for n in SCHEMES},
                      'validation_macro_ap':{n:validation[n]['selected_threshold']['macro']['average_precision'] for n in SCHEMES},
                      'outputs':result['output_sha256']},ensure_ascii=True),flush=True)


if __name__=='__main__':
    main()
