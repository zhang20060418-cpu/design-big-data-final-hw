"""R2: physical severity for envelope uncertainty and native title hierarchy."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('precision_r1_route_frozen',Path(__file__).with_name('routing.py'))
R1=importlib.util.module_from_spec(spec);spec.loader.exec_module(R1)
VERSION='evidence-qualified-review-r2'
LABELS=R1.LABELS;THRESHOLDS=R1.THRESHOLDS

def route(ctx,detectors,material=None):
 answer=R1.route(ctx,detectors,material);height=ctx['page'].get('height_pt',540);width=ctx['page'].get('width_pt',960)
 os=ctx.get('objects',[]);byid={o['id']:o for o in os}
 texts=[o for o in os if o.get('text_chars',0) and o.get('text_role') not in {'page_number','navigation'}]
 fonts=[R1._font(o) for o in texts];fonts=[v for v in fonts if v]
 title_hierarchy=bool(fonts and max(fonts)*540/height>=28 and len(texts)<=8 and sum(o.get('text_chars',0) for o in texts)<=150 and not any(o.get('kind') in {'table','chart','cell'} for o in os))
 for k,d in answer['decisions'].items():
  reasons=list(d['critical_review_reasons']);deferred=[]
  if k=='D5' and 'low_known_content_and_small_unresolved_regions' in reasons:
   reasons.remove('low_known_content_and_small_unresolved_regions');deferred.append(dict(kind='weak_partial_occupancy_without_positive_known_void_witness'))
  if k=='D1' and title_hierarchy and 'unresolved_role_with_small_or_missing_font' in reasons:
   relevant=[byid.get(u.get('object_id'),{}) for u in detectors[k].get('unresolved',[]) if u.get('kind')=='text_role_unresolved']
   # Exempt only measured 9pt+ supporting typography under a large native
   # title. Tiny/missing font remains a critical unresolved measurement.
   if relevant and all(R1._font(o) and R1._font(o)*540/height>=9 for o in relevant):
    reasons.remove('unresolved_role_with_small_or_missing_font');deferred.append(dict(kind='measured_secondary_typography_under_large_native_title',font_hierarchy_confirmed_only=True))
  if k=='D2':
   kind='text_foreground_layout_intersection_requires_glyph_confirmation'
   if kind in reasons:
    substantial=False
    for u in detectors[k].get('unresolved',[]):
     if u.get('kind')!=kind:continue
     b=u.get('bbox') or {};fs=[R1._font(byid.get(i,{})) for i in u.get('object_ids',[])];fs=[v for v in fs if v]
     if fs and b.get('h',0)*height>=min(fs)*.35 and b.get('w',0)*width>=min(fs):substantial=True
    if not substantial:
     reasons=[v for v in reasons if v!=kind];deferred.append(dict(kind='small_text_layout_envelope_contact_without_glyph_proof'))
  d['semantic_unresolved'].extend(deferred);d['critical_review_reasons']=reasons
  d['review_required']=bool(d['automatic_risk_candidate'] or reasons)
  if d['semantic_unresolved']:answer['semantic_unresolved'][k]=d['semantic_unresolved']
 answer['review_required']=bool(answer['global_review_reasons'] or any(d['review_required'] for d in answer['decisions'].values()))
 answer['route']='llm_review' if answer['review_required'] else 'no_trigger_with_disclosed_unassessed_scope'
 answer['version']=VERSION
 return answer
