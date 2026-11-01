"""D6 recall-oriented review hypotheses; never an AI authorship classifier.

One bounded design pass uses H150 train pages and prior G150 development.
The caller owns split isolation. This module does not read data files, labels,
names, notes, template IDs, file names, or any label-associated asset hash.
A05 remains unsupported. Bitmap presence can request human material review;
that request is not an automatic A05 prediction and has no invented score.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import math
from pathlib import Path


_PATH = (Path(__file__).resolve().parents[2] /
         'stage4m_rule_optimization_20261029/implementation/generative_v3.py')
_SPEC = importlib.util.spec_from_file_location('_immutable_v3_for_d6_recall', _PATH)
V3 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(V3)
V2, LEGACY = V3.v2, V3.v2._L

DEFAULT_CONFIG = {
    'version': 'd6-recall-review-v1-20261030',
    'layout_similarity_min': .92,
    'layout_min_objects': 3,
    'layout_review_min_pages': 3,
    'icon_review_min_count': 8,
    'shape_review_min_count': 4,
    'shape_review_min_union_area': .025,
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _component(status, score, features, evidence=None):
    return {'status': status, 'score': score,
            'score_kind': 'ordinal_review_priority_not_probability',
            'features': features, 'evidence': evidence or []}


def _previous_sub(previous, code):
    return previous.get('features', {}).get('subdetectors', {}).get(code, {})


def _layout_component(ctx, contexts, cfg):
    """Similar layout is a review candidate, not proof of mechanical abuse.

    Matching peers need not be consecutive. Same-document native geometry is
    sufficient to *request review*, so a legitimate repeated template can be a
    false alarm. Complete-link to this page avoids graph-chain amplification.
    """
    native = V2._native_available(ctx)
    objects = LEGACY._layout_objects(ctx) if native else []
    role = ctx.get('page', {}).get('role', 'unknown')
    if not native or ctx.get('document_id') is None or len(objects) < cfg['layout_min_objects'] or role in {'cover', 'section', 'ending'}:
        return _component('unknown', None, {'layout_object_count': len(objects),
                          'reason': 'native_layout_or_role_not_eligible'})
    peers = []
    for other in contexts:
        if (other is ctx or other.get('sample_id') == ctx.get('sample_id')
                or other.get('document_id') != ctx.get('document_id')
                or not V2._native_available(other)
                or other.get('page', {}).get('role') in {'cover', 'section', 'ending'}):
            continue
        other_objects = LEGACY._layout_objects(other)
        if len(other_objects) < cfg['layout_min_objects']:
            continue
        similarity = LEGACY.layout_similarity(objects, other_objects)
        if _finite(similarity) and similarity >= cfg['layout_similarity_min']:
            peers.append({'sample_id': other.get('sample_id'),
                          'page_index': other.get('page_index'),
                          'layout_similarity': float(similarity)})
    enough = len(peers) + 1 >= cfg['layout_review_min_pages']
    score = min(.9, .65 + .05 * len(peers)) if enough else 0.
    features = {'layout_object_count': len(objects), 'similar_peer_count': len(peers),
                'matching_peers': peers, 'minimum_similarity': cfg['layout_similarity_min'],
                'minimum_review_cluster_pages': cfg['layout_review_min_pages'],
                'consecutive_run_required': False, 'independent_redundancy_required': False,
                'claim_boundary': 'Repeated layout requires review; uniform templates are not automatically defects.'}
    evidence = [{'mechanism': 'repeated_native_layout_review', 'object_ids': [o.get('id') for o in objects],
                 'peer_ids': [p['sample_id'] for p in peers], 'measurements': deepcopy(features)}] if enough else []
    return _component('review_candidate' if enough else 'no_candidate_in_available_scope', score, features, evidence)


def _icon_component(previous, cfg):
    old = _previous_sub(previous, 'A02')
    f = deepcopy(old.get('features', {}))
    count = f.get('candidate_icon_count')
    available = _finite(count) and old.get('status') not in {'unknown', 'unsupported'}
    trigger = available and count >= cfg['icon_review_min_count']
    f.update(one_icon_per_module_protection='retained_as_evidence_but_not_review_veto',
             review_min_icon_count=cfg['icon_review_min_count'],
             claim_boundary='Many native small images request review even when each has a caption; useful icon grids can be false alarms.')
    score = min(.95, .45 + .025 * count) if trigger else 0. if available else None
    evidence = [{'mechanism': 'dense_small_native_image_review',
                 'object_ids': f.get('candidate_icon_ids', []), 'measurements': deepcopy(f)}] if trigger else []
    return _component('review_candidate' if trigger else 'no_candidate_in_available_scope' if available else 'unknown', score, f, evidence)


def _shape_component(previous, cfg):
    old = _previous_sub(previous, 'A03')
    f = deepcopy(old.get('features', {}))
    pile = f.get('unattached_translucent_shape_pile_hypothesis', {})
    count, area = pile.get('eligible_object_count'), pile.get('union_area_ratio')
    available = _finite(count) and _finite(area)
    trigger = (available and count >= cfg['shape_review_min_count']
               and area >= cfg['shape_review_min_union_area'])
    f.update(review_min_shape_count=cfg['shape_review_min_count'],
             review_min_union_area=cfg['shape_review_min_union_area'],
             claim_boundary='Unattached translucent shape pile requests review; author intent and decoration semantics are not proven.')
    score = min(.95, .5 + .05 * count) if trigger else 0. if available else None
    evidence = [{'mechanism': 'unattached_translucent_shape_pile_review',
                 'object_ids': pile.get('object_ids', []), 'measurements': deepcopy(pile)}] if trigger else []
    return _component('review_candidate' if trigger else 'no_candidate_in_available_scope' if available else 'unknown', score, f, evidence)


def _style_component(previous):
    old = deepcopy(_previous_sub(previous, 'A04'))
    score = old.get('score')
    if old.get('status') == 'candidate' and _finite(score):
        return _component('review_candidate', float(score), old.get('features', {}), old.get('evidence', []))
    return _component(old.get('status', 'unknown'), float(score) if _finite(score) else None,
                      old.get('features', {}), old.get('evidence', []))


def _material_scope(ctx):
    """A lossless review queue, not a learned or semantic material detector."""
    # A logo/background role is only a structural hypothesis. It cannot prove
    # that embedded pixels lack an artifact; do not silently exempt those ROIs.
    images = [o for o in ctx.get('objects', []) if o.get('kind') == 'image']
    return {'status': 'unsupported', 'score': None, 'automatic_prediction': None,
            'requires_material_review': bool(images),
            'bitmap_object_ids': [o.get('id') for o in images],
            'bitmap_count': len(images), 'claim_boundary':
            'Bitmap presence only requests human review. It does not detect pseudo-text, semantic mismatch, or generated artifacts.'}


def propose(ctx, previous_result=None, document_contexts=None, config=None):
    """Return D6 automatic-scope priority plus a separate material review queue.

    No threshold is selected here. The caller must preselect a train-only
    operating point and report both automatic alarms and review routing.
    ``document_contexts`` must obey the caller's declared split/context policy.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    contexts = list(document_contexts) if document_contexts is not None else [ctx]
    if previous_result is None:
        previous_result = V3.GenerativeDesignAnomalyDetector().detect_document(contexts)['page_results'][ctx['sample_id']]
    # A supplied cached result may have used a larger peer pool. Recompute the
    # cross-page style branch from only the caller-declared current context.
    previous_result = deepcopy(previous_result)
    previous_result.setdefault('features', {}).setdefault('subdetectors', {})['A04'] = (
        V3.GenerativeDesignAnomalyDetector()._style(ctx, contexts))
    components = {'A01': _layout_component(ctx, contexts, cfg),
                  'A02': _icon_component(previous_result, cfg),
                  'A03': _shape_component(previous_result, cfg),
                  'A04': _style_component(previous_result),
                  'A05': _material_scope(ctx)}
    if not V2._native_available(ctx):
        for code in ('A01', 'A02', 'A03', 'A04'):
            components[code] = _component('unknown', None, {'reason': 'native_objects_unavailable'})
    valid = [v['score'] for k, v in components.items() if k != 'A05' and _finite(v.get('score'))]
    triggers = [k for k, v in components.items() if k != 'A05' and v.get('status') == 'review_candidate']
    evidence = [e for k in triggers for e in components[k].get('evidence', [])]
    return {'detector': 'D6', 'label': 'generative_design_anomaly', 'version': cfg['version'],
            'status': 'candidate' if triggers else 'no_candidate_in_available_scope' if valid else 'unknown',
            'score': max(valid) if valid else None,
            'score_kind': 'ordinal_heuristic_not_probability', 'coverage': 'partial',
            'features': {'recall_components': components, 'composite_triggers': triggers,
                         'automatic_scope': 'A01_A04_review_hypotheses_only',
                         'score_interpretation': 'ordinal_review_priority_not_confirmed_defect_probability',
                         'A05_status': 'unsupported', 'frozen_config': deepcopy(cfg)},
            'evidence': evidence,
            'triage_sources': triggers,
            'material_review_required': components['A05']['requires_material_review'],
            'material_review_reason': 'Uninspected embedded bitmap content; no A05 semantic detector is implemented.' if components['A05']['requires_material_review'] else None,
            'material_review': components['A05'],
            'limitations': ['This is a review priority, not confirmed defect probability.',
                            'Full D6 automatic sensitivity and AP are unsupported because A05 is unavailable.',
                            'Sending bitmaps to human review must be counted separately in false alarms and review burden.',
                            'Zero observed misses cannot guarantee zero misses on future PPTX.']}


def propose_document(contexts, previous_results=None, config=None):
    contexts = list(contexts)
    identifiers = [c.get('sample_id') for c in contexts]
    if any(not i for i in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('D6 requires unique nonempty sample_id values')
    previous_results = previous_results or V3.GenerativeDesignAnomalyDetector().detect_document(contexts)['page_results']
    return {'page_results': {c['sample_id']: propose(c, previous_results[c['sample_id']], contexts, config)
                             for c in contexts},
            'document_result': {'version': (config or {}).get('version', DEFAULT_CONFIG['version']),
                                'pages': len(contexts), 'A05_status': 'unsupported',
                                'automatic_scope': 'A01_A04_review_hypotheses_only'}}
