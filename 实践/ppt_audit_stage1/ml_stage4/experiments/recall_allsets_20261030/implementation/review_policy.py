"""Pure decisions for reuse by a future caller; never changes an application."""
from __future__ import annotations
import math


def decide(detector,proposal,threshold,material=None,policy='targeted'):
    if detector not in {'D5','D6'} or policy not in {'targeted','conservative'}:
        raise ValueError('Only the registered D5/D6 policies are supported')
    if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not math.isfinite(threshold):
        raise ValueError('A finite registered threshold is required')
    score=proposal.get('score')
    available=(isinstance(score,(int,float)) and not isinstance(score,bool) and math.isfinite(score)
               and proposal.get('status')!='unsupported')
    automatic=bool(available and score>=threshold)
    f=proposal.get('features',{})
    if detector=='D5':
        unresolved=bool(f.get('unknown_area_fraction',0) or f.get('missing_content_region_object_ids'))
        request=(unresolved or proposal.get('status') in {'unknown','unsupported'}) if policy=='conservative' else False
        material_unassessed=False
    else:
        if material is None:raise ValueError('D6 requires explicit material-scope evidence')
        unresolved=any(v['status'] in {'unknown','unsupported'} for k,v in f.get('recall_components',{}).items() if k!='A05')
        material_unassessed=bool(material.get('requires_unresolved_material_review'))
        request=(unresolved or material_unassessed or proposal.get('status') in {'unknown','unsupported'}) if policy=='conservative' else material.get('status') in {'targeted_review_candidate','unknown'}
    review=bool((request or not available) and not automatic)
    return dict(detector=detector,policy=policy,automatic_risk_candidate=automatic,
        manual_review_required=review,any_action=automatic or review,
        status='risk_candidate' if automatic else 'needs_review' if review else 'no_candidate_in_available_scope',
        score=score,score_kind='ordinal_not_probability',threshold=threshold,
        partial_semantic_uncertainty=unresolved,material_semantics_unassessed=material_unassessed,
        semantic_artifact_confirmed=False,entire_page_certified_normal=False,
        zero_future_miss_guarantee=False)
