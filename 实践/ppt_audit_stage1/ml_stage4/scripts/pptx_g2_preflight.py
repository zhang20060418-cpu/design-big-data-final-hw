"""Read-only, prediction-free checks for the preregistered independent G-2 run.

Public API: ``preflight(base=None, output_path=None)`` returns a JSON-safe report;
``load_ordered_samples(base=None)`` returns complete frozen manifest rows in
release-index order then sample_id order, with absolute input paths and the
registered family/scope. It performs no feature extraction or prediction.
The caller MUST require report['status']=='passed' before any inference.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import sys
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import joblib
import numpy as np
from PIL import Image
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


BASE = Path(__file__).resolve().parents[1]
LABELS = (
    'Improper Font Sizing', 'Occluded Content', 'Cluttered Layout',
    'Content Overflow/Cut-off',
)
SLUGS = (
    'improper_font_sizing', 'occluded_content', 'cluttered_layout',
    'content_overflow_cutoff',
)
FEATURES = (
    'aspect_ratio', 'edge_density', 'flat_region_ratio', 'gray_std',
    'grid_edge_cv', 'border_edge_ratio', 'horizontal_projection_spread',
    'vertical_projection_spread', 'mean_gray', 'mean_color_range',
)
MODES = ('f1', 'high_recall', 'low_fpr')
SCHEMES = ('A', 'C', 'H', 'C9')
REVISION = '3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268'
BUNDLE_SHA = '53a56f3bb1d776cca5e4a33926b6db57f74d3e982d16cf73ff3a097dc592ff63'
SELECTION_SHA = '0f95c3c794b4eef6e4a1d9c0b6a2c07968b3283993081fcd1221408255200993'
RECIPE_SHA = '6d06910804152b6fbbbc314384c6b40041a30ad17e39190bcee7c44106c13b4a'
PAYLOAD_SHA = '0d1fc0aae630960bcd2236eb11f93634ad4a1457f87195a0c5d5fc533c1f42a3'
FAMILY_SHA = '2dd05b685ce47a2f85d5b2f6156a7ca4fb9278a3c0d914e8a9e55de03d6a1e49'
THRESHOLDS = {
    'A': {'f1': [.13,.24,.17,.13], 'high_recall': [.12,.19,.12,.05], 'low_fpr': [.21,.65,.17,.17]},
    'C': {'f1': [.13,.16,.57,.14], 'high_recall': [.13,.14,.44,.08], 'low_fpr': [.20,.20,.56,.15]},
    'H': {'f1': [.13,.16,.57,.14], 'high_recall': [.12,.14,.44,.08], 'low_fpr': [.21,.20,.56,.15]},
    'C9': {'f1': [.14,.35,.61,.24], 'high_recall': [.13,.37,.42,.05], 'low_fpr': [.20,.57,.57,.17]},
}
CLIP_HASHES = {
    'pytorch_model.bin': 'a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f',
    'config.json': 'b575ef3c36f2a057fa19e221650105052d61cc9c1a972ec15019c6261ec98770',
    'preprocessor_config.json': '910e70b3956ac9879ebc90b22fb3bc8a75b6a0677814500101a4c072bd7857bd',
}
LABEL_FIELDS = (
    'sample_id', 'document_id', 'original_slide_id', 'original_slide_index',
    'variant_id', 'group_id', 'source_class', 'natural_or_controlled',
    *SLUGS, 'reviewer_id', 'reviewed_at',
)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def safe_relative(root, relative):
    path = Path(str(relative))
    resolved = (Path(root) / path).resolve()
    if path.is_absolute() or '..' in path.parts or not resolved.is_relative_to(Path(root).resolve()):
        raise ValueError(f'Nonlocal benchmark input: {relative}')
    return resolved


def load_ordered_samples(base=None):
    """Return frozen-only row order; no SlideAudit sources are accessed."""
    base = Path(base or BASE).resolve()
    registry = read_json(base / 'reports/pptx_independent_evaluation_freeze_registry.json')
    families = registry['source_family_mapping']
    result = []
    for release in registry['release_index']:
        root = safe_relative(base, release['root'])
        plan = read_json(root / 'source_plan.json')
        docs = {d['document_id']: d for d in plan['documents']}
        for original in sorted(read_csv(root / 'benchmark_manifest.csv'), key=lambda r: r['sample_id']):
            row = dict(original)
            cohort = release['cohort_id']
            doc = docs[row['document_id']]
            github = bool(doc.get('github_url') or doc.get('source_evidence', {}).get('repository'))
            row.update({
                'cohort_id': cohort, 'release_root': str(root),
                'source_family_id': families[row['document_id']],
                'source_scope': 'github_download' if github else 'local',
                'source_content_type': 'image_based' if cohort == 'v2' else 'native_editable_or_mixed',
                'png_absolute_path': str(safe_relative(root, row['png_path'])),
                'pptx_absolute_path': str(safe_relative(root, row['pptx_path'])),
            })
            result.append(row)
    return result


class Checks:
    def __init__(self):
        self.items = []
        self.blockers = []

    def require(self, name, condition, detail=None):
        condition = bool(condition)
        item = {'name': name, 'passed': condition}
        if detail is not None:
            item['detail'] = detail
        self.items.append(item)
        if not condition:
            self.blockers.append(name)
        return condition


def _load_checker(base):
    path = base / 'benchmark_pptx/prepare_benchmark.py'
    spec = importlib.util.spec_from_file_location('g2_existing_frozen_checker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inspect_bundle(base, checks, registered_files):
    path = base / 'models/fusion_routing_candidate.joblib'
    actual = sha256(path)
    registered_files[str(path)] = actual
    if not checks.require('bundle_sha256', actual == BUNDLE_SHA, actual):
        return {'guarded_model_calls': 0, 'inspection_skipped': 'SHA mismatch'}
    blocked = []
    def forbid(*args, **kwargs):
        blocked.append('blocked fit/predict/transform invocation')
        raise RuntimeError('Preflight forbids fitting, transforming or predicting')
    with ExitStack() as stack:
        for cls, names in [
            (LogisticRegression, ('fit','predict','predict_proba','decision_function','score','predict_log_proba')),
            (StandardScaler, ('fit','partial_fit','fit_transform','transform','inverse_transform')),
        ]:
            for name in names:
                if hasattr(cls, name):
                    stack.enter_context(patch.object(cls, name, forbid))
        bundle = joblib.load(path)
        checks.require('bundle_label_names_order', bundle['labels'] == list(LABELS))
        checks.require('bundle_label_slug_order', bundle['slugs'] == list(SLUGS))
        checks.require('bundle_model_keys', set(bundle['models']) == {'A','C','C9'})
        checks.require('bundle_clip_revision', bundle['clip_revision'] == REVISION)
        checks.require('bundle_recipe_payload', bundle['png_recipe_payload_sha256'] == PAYLOAD_SHA)
        checks.require('bundle_selection_sha', bundle['frozen_selection_sha256'] == SELECTION_SHA)
        checks.require('bundle_feature_order', bundle['feature_recipe']['png_feature_order'] == list(FEATURES))
        routes = [bundle['routing'][label]['selected'] for label in LABELS]
        checks.require('H_routes_A_C_C_C', routes == ['A','C','C','C'], routes)
        models = {}
        thresholds = {}
        expected_parameters = {
            'A': [(1.,None),(10.,'balanced'),(1.,None),(10.,None)],
            'C': [(.1,None),(.1,None),(1.,'balanced'),(1.,None)],
            'C9': [(.1,None),(1.,'balanced'),(1.,'balanced'),(10.,None)],
        }
        for scheme, dimension in [('A',512),('C',522),('C9',521)]:
            estimators = bundle['models'][scheme]
            checks.require(f'{scheme}_four_estimators', len(estimators) == 4)
            selected = bundle['selections'][scheme]['selections']
            thresholds[scheme] = {
                mode: [float(s['thresholds'][mode]['threshold']) for s in selected]
                for mode in MODES
            }
            model_rows = []
            for j, model in enumerate(estimators):
                params = model.get_params()
                c, weight = expected_parameters[scheme][j]
                valid = (
                    isinstance(model, LogisticRegression) and model.n_features_in_ == dimension
                    and model.coef_.shape == (1,dimension) and model.intercept_.shape == (1,)
                    and np.array_equal(model.classes_, np.array([0,1]))
                    and np.isfinite(model.coef_).all() and np.isfinite(model.intercept_).all()
                    and params['C'] == c and params['class_weight'] == weight
                    and params['solver'] == 'lbfgs' and params['max_iter'] == 3000
                    and params['tol'] == 1e-6 and params['random_state'] == 42
                    and selected[j]['label'] == LABELS[j]
                    and selected[j]['C'] == c and selected[j]['class_weight'] == weight
                )
                checks.require(f'{scheme}_{SLUGS[j]}_frozen_classifier', valid)
                model_rows.append({
                    'label': SLUGS[j], 'n_features_in': int(model.n_features_in_),
                    'classes': model.classes_.tolist(), 'positive_class_column': 1,
                    'C': c, 'class_weight': weight,
                    'coef_sha256': hashlib.sha256(model.coef_.tobytes()).hexdigest(),
                    'intercept_sha256': hashlib.sha256(model.intercept_.tobytes()).hexdigest(),
                })
            models[scheme] = model_rows
        thresholds['H'] = {mode: [thresholds[routes[j]][mode][j] for j in range(4)] for mode in MODES}
        for scheme in SCHEMES:
            for mode in MODES:
                value = thresholds[scheme][mode]
                checks.require(f'{scheme}_{mode}_thresholds',
                               np.array_equal(value,THRESHOLDS[scheme][mode]) and np.isfinite(value).all(), value)
        checks.require('H_explicit_threshold_metadata', all(
            np.array_equal(bundle['hybrid_thresholds'][mode], thresholds['H'][mode]) for mode in MODES))
        scalers = {}
        for key, dimension in [('10',10),('9',9)]:
            scaler = bundle['png_scalers'][key]
            valid = (
                isinstance(scaler, StandardScaler) and scaler.n_features_in_ == dimension
                and int(scaler.n_samples_seen_) == 1512 and scaler.with_mean and scaler.with_std
                and scaler.mean_.shape == scaler.var_.shape == scaler.scale_.shape == (dimension,)
                and np.isfinite(scaler.mean_).all() and np.isfinite(scaler.var_).all()
                and np.isfinite(scaler.scale_).all() and (scaler.scale_ > 0).all()
            )
            checks.require(f'PNG{key}_frozen_train_scaler', valid)
            scalers[key] = {'n_features_in': int(scaler.n_features_in_),
                            'n_samples_seen': int(scaler.n_samples_seen_),
                            'mean': scaler.mean_.tolist(), 'var': scaler.var_.tolist(),
                            'scale': scaler.scale_.tolist(), 'fixed_divisor': float(np.sqrt(dimension))}
        checks.require('C9_removes_aspect_only', all(np.array_equal(
            getattr(bundle['png_scalers']['10'],attr)[1:],
            getattr(bundle['png_scalers']['9'],attr)) for attr in ('mean_','var_','scale_')))
    checks.require('zero_guarded_fit_predict_transform_calls', not blocked, len(blocked))
    return {'models': models, 'scalers': scalers, 'thresholds': thresholds,
            'H_routes': routes, 'guarded_model_calls': len(blocked),
            'feature_contract': 'raw float32 CLIP -> float64 L2; frozen scaler PNG10/sqrt(10) or PNG9/sqrt(9); concatenate; no whole-vector renormalization'}


def _pptx_info(path):
    ns = {'p':'http://schemas.openxmlformats.org/presentationml/2006/main'}
    with zipfile.ZipFile(path) as z:
        presentation = ET.fromstring(z.read('ppt/presentation.xml'))
        size = presentation.find('p:sldSz',ns)
        slides = presentation.find('p:sldIdLst',ns)
        return {'width': int(size.get('cx')), 'height': int(size.get('cy')),
                'slide_ids': [s.get('id') for s in slides]}


def preflight(base=None, output_path=None):
    """Check all frozen inputs and model metadata without predicting any page."""
    base = Path(base or BASE).resolve()
    checks = Checks()
    files = {}
    report = {'schema_version':'pptx-g2-preflight-v1',
              'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'base': str(base), 'official_slideaudit_data_accessed': False,
              'feature_extraction_calls': 0, 'model_forward_calls': 0,
              'fit_predict_transform_calls': 0, 'checks': checks.items, 'blockers': checks.blockers}
    report['environment'] = {'python': platform.python_version(), 'executable': sys.executable,
                             'platform': platform.platform(),
                             'packages': {name:importlib.metadata.version(name) for name in
                                 ('numpy','Pillow','scikit-learn','joblib','torch','transformers')}}
    registry_path = base / 'reports/pptx_independent_evaluation_freeze_registry.json'
    protocol_path = base / 'reports/pptx_independent_evaluation_protocol.md'
    try:
        registry = read_json(registry_path)
        protocol_text = protocol_path.read_text('utf-8')
        for path in (registry_path,protocol_path,Path(__file__).resolve()):
            files[str(path)] = sha256(path)
        checks.require('registry_binds_protocol_sha', files[str(protocol_path)] == registry['protocol_sha256'])
        checks.require('protocol_has_frozen_identities', all(s in protocol_text for s in
                       (BUNDLE_SHA,SELECTION_SHA,REVISION,RECIPE_SHA,PAYLOAD_SHA,FAMILY_SHA,*CLIP_HASHES.values())))
        checks.require('three_registered_releases', [r['cohort_id'] for r in registry['release_index']] == ['v1','v2','v3'])
        family_map = registry['source_family_mapping']
        checks.require('family_map_canonical_sha', digest(family_map) == registry['source_family_mapping_sha256'] == FAMILY_SHA)
        checks.require('family_map_23_documents_8_units', len(family_map) == 23 and len(set(family_map.values())) == 8)
        checker = _load_checker(base)
        report['releases'] = []
        all_rows = []
        pptx_cache = {}
        groups = defaultdict(list)
        image_modes = Counter()
        for release in registry['release_index']:
            root = safe_relative(base,release['root'])
            cohort = release['cohort_id']
            result = {'cohort_id':cohort,'root':str(root)}
            try:
                result['existing_full_frozen_check'] = checker.check_frozen_benchmark(root)
                checks.require(f'{cohort}_existing_full_frozen_check', result['existing_full_frozen_check']['status']=='passed')
            except Exception as exc:
                result['existing_full_frozen_check'] = {'status':'failed','error':str(exc)}
                checks.require(f'{cohort}_existing_full_frozen_check',False,str(exc))
            mapping = {
                'benchmark_manifest.csv':'manifest_sha256','frozen_labels.csv':'labels_sha256',
                'source_plan.json':'source_plan_sha256','freeze_record.json':'freeze_record_sha256',
                'source_confirmation_decisions.json':'source_confirmation_decisions_sha256',
                'user_final_confirmation.json':'attestation_sha256',
            }
            expected = {name:release[key] for name,key in mapping.items()}
            expected.update(release.get('supporting_evidence_sha256',{}))
            for name, expected_sha in expected.items():
                path = safe_relative(root,name)
                actual = sha256(path) if path.is_file() else None
                if actual is not None:
                    files[str(path)] = actual
                checks.require(f'{cohort}_registered_file_{name}',actual == expected_sha,actual)
            record = read_json(root/'freeze_record.json')
            rows = read_csv(root/'benchmark_manifest.csv')
            labels = read_csv(root/'frozen_labels.csv')
            expected_labels = [{k:r[k] for k in LABEL_FIELDS} for r in rows]
            checks.require(f'{cohort}_frozen_labels_exact_manifest_projection',labels == expected_labels)
            checks.require(f'{cohort}_record_registry_integrity',
                           record['artifacts_canonical_sha256'] == release['artifacts_canonical_sha256']
                           and record['sample_count'] == release['sample_count']
                           and record['document_count'] == release['document_count']
                           and record['group_count'] == release['group_count'])
            plan = read_json(root/'source_plan.json')
            docs = {doc['document_id']:doc for doc in plan['documents']}
            checks.require(f'{cohort}_document_ids_unique',len(docs)==len(plan['documents'])==release['document_count'])
            checks.require(f'{cohort}_source_family_confirmation_matches',all(
                doc.get('provenance_confirmation',{}).get('related_source_family') == family_map.get(doc['document_id'])
                for doc in docs.values()))
            natural_count = Counter()
            for row in rows:
                sid = row['sample_id']
                doc = docs[row['document_id']]
                pptx = safe_relative(root,row['pptx_path'])
                png = safe_relative(root,row['png_path'])
                if pptx not in pptx_cache:
                    pptx_cache[pptx] = _pptx_info(pptx)
                info = pptx_cache[pptx]
                original_index = int(row['original_slide_index'])
                checks.require(f'{sid}_slide_identity',
                    0 < original_index <= len(info['slide_ids'])
                    and info['slide_ids'][original_index-1] == row.get('variant_slide_id',row['original_slide_id']))
                with Image.open(png) as image:
                    image.load()
                    opaque = image.convert('RGBA').getchannel('A').getextrema() == (255,255)
                    expected_size = (int(row['png_width']),int(row['png_height']))
                    image_modes[image.mode] += 1
                    checks.require(f'{sid}_opaque_complete_png',image.format=='PNG' and opaque and image.size==expected_size and max(image.size)==1920)
                if row['natural_or_controlled']=='natural':
                    natural_count[row['document_id']] += 1
                    checks.require(f'{sid}_natural_source_identity',
                        row['variant_id']=='original' and row['source_class']==doc['source_class']
                        and row['pptx_sha256']==doc['source_sha256'])
                else:
                    checks.require(f'{sid}_controlled_not_natural_A',row['source_class']=='C' and row['variant_id']!='original')
                groups[row['group_id']].append(row)
            for doc_id,doc in docs.items():
                original_path = safe_relative(root,doc['original_path'])
                info = pptx_cache.get(original_path) or _pptx_info(original_path)
                checks.require(f'{cohort}_{doc_id}_all_original_pages_retained',natural_count[doc_id]==len(info['slide_ids']))
            result['sample_count'] = len(rows)
            result['natural_pages'] = sum(r['natural_or_controlled']=='natural' for r in rows)
            result['controlled_pages'] = sum(r['natural_or_controlled']=='controlled' for r in rows)
            result['document_count'] = len({r['document_id'] for r in rows})
            result['group_count'] = len({r['group_id'] for r in rows})
            report['releases'].append(result)
            all_rows.extend(rows)
        samples = load_ordered_samples(base)
        checks.require('352_unique_samples',len(samples)==len({r['sample_id'] for r in samples})==352)
        checks.require('expected_release_counts',dict(Counter(r['cohort_id'] for r in samples))=={'v1':108,'v2':100,'v3':144})
        checks.require('23_documents_328_groups',len({r['document_id'] for r in samples})==23 and len(groups)==328)
        checks.require('328_natural_24_controlled',dict(Counter(r['natural_or_controlled'] for r in samples))=={'natural':328,'controlled':24})
        checks.require('four_binary_truths_all_rows',all(r[k] in ('0','1') for r in samples for k in SLUGS))
        primary = [r for r in samples if r['source_class']=='A' and r['natural_or_controlled']=='natural']
        checks.require('primary_E_A_282_pages_21_documents',len(primary)==registry['primary_E_A']['pages']==282 and len({r['document_id'] for r in primary})==21)
        checks.require('sample_source_class_counts',dict(Counter(r['source_class'] for r in samples))==registry['sample_source_class_counts'])
        checks.require('registered_families_cover_exact_document_set',set(family_map)=={r['document_id'] for r in samples})
        for group, rows in groups.items():
            naturals = [r for r in rows if r['natural_or_controlled']=='natural']
            checks.require(f'{group}_source_group_identity', len(naturals)==1
                           and len({(r['document_id'],r['original_slide_id'],r['original_slide_index']) for r in rows})==1)
        v3gh = [r for r in samples if r['cohort_id']=='v3' and r['source_scope']=='github_download']
        checks.require('V3_github85_local59',len(v3gh)==85 and sum(r['cohort_id']=='v3' and r['source_scope']=='local' for r in samples)==59)
        report['ordered_input_index'] = [{k:r[k] for k in (
            'sample_id','cohort_id','document_id','group_id','original_slide_id','original_slide_index',
            'variant_id','modification_type','natural_or_controlled','source_class','source_family_id',
            'source_scope','source_content_type','pptx_absolute_path','png_absolute_path',
            'pptx_sha256','png_sha256','png_width','png_height',*SLUGS)} for r in samples]
        report['ordered_input_index_sha256'] = digest(report['ordered_input_index'])
        report['sample_count'] = len(samples)
        report['primary_E_A_count'] = len(primary)
        report['image_modes'] = dict(image_modes)
        report['label_support'] = {k:{'positive':sum(r[k]=='1' for r in samples),
                                      'negative':sum(r[k]=='0' for r in samples)} for k in SLUGS}
        snapshot = base/f'models/huggingface/models--openai--clip-vit-base-patch32/snapshots/{REVISION}'
        for name, expected_sha in CLIP_HASHES.items():
            path = snapshot/name
            actual = sha256(path)
            files[str(path)] = actual
            checks.require(f'CLIP_{name}_sha256',actual==expected_sha,actual)
        processor = read_json(snapshot/'preprocessor_config.json')
        checks.require('CLIP_processor_config', processor['do_center_crop'] is True
                       and processor['do_resize'] is True and processor['do_normalize'] is True
                       and processor['size']==224 and processor['crop_size']==224 and processor['resample']==3
                       and processor['image_mean']==[.48145466,.4578275,.40821073]
                       and processor['image_std']==[.26862954,.26130258,.27577711])
        config = read_json(snapshot/'config.json')
        checks.require('CLIP_vision_projection_dimension_512',config['projection_dim']==512 and config['vision_config']['image_size']==224)
        recipe_path = base/'features/png_layout_recipe.json'
        files[str(recipe_path)] = sha256(recipe_path)
        recipe = read_json(recipe_path)
        checks.require('PNG_recipe_file_sha256',files[str(recipe_path)]==RECIPE_SHA)
        checks.require('PNG_recipe_canonical_payload',digest({k:v for k,v in recipe.items() if k!='payload_sha256'})==recipe['payload_sha256']==PAYLOAD_SHA)
        checks.require('PNG_recipe_version_order',recipe['algorithm']['version']=='png-layout-v1.0.0'
                       and recipe['feature_names']==recipe['algorithm']['feature_names']==list(FEATURES))
        extractor_path = base/'scripts/extract_png_layout_features.py'
        files[str(extractor_path)] = sha256(extractor_path)
        checks.require('PNG_feature_extractor_frozen_sha',files[str(extractor_path)]==recipe['source_sha256'])
        report['model_inspection'] = _inspect_bundle(base,checks,files)
        report['registry_interpretation'] = (
            'The unchanged v1.0 protocol describes the earlier candidate gate state; '
            'the SHA-bound release registry is its append-only actual-data freeze appendix, '
            'not a model/metric/threshold amendment.')
    except Exception as exc:
        checks.require('preflight_completed_without_exception',False,f'{type(exc).__name__}: {exc}')
    report['registered_input_sha256'] = files
    report['status'] = 'passed' if not checks.blockers else 'blocked'
    report['checks_passed'] = sum(item['passed'] for item in checks.items)
    report['checks_total'] = len(checks.items)
    report['completed_at'] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=BASE/'outputs/pptx_g2_preflight.json')
    args = parser.parse_args()
    result = preflight(output_path=args.output)
    print(json.dumps({k:result.get(k) for k in ('status','sample_count','primary_E_A_count','checks_passed','checks_total','blockers')},ensure_ascii=False))
    raise SystemExit(0 if result['status']=='passed' else 2)


if __name__ == '__main__':
    main()
