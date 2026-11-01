"""Evidence-qualified review. Unknown semantics remain unknown, never label 0."""
import math

VERSION='evidence-qualified-review-r1'
LABELS=('D1','D2','D3','D4','D5','D6')
THRESHOLDS={'D5':.3308457711442786,'D6':.75}
def finite(v):return isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v)
def _font(o):
 v=[p.get('font_pt') for p in o.get('paragraphs',[])]+list(o.get('font_sizes_pt',[]));v=[x for x in v if finite(x) and x>0]
 return min(v) if v else None
def route(ctx,detectors,material=None):
 page=ctx.get('page',{});objects=ctx.get('objects',[]);byid={o['id']:o for o in objects};height=page.get('height_pt',540)
 texts=[o for o in objects if o.get('text_chars',0)>0];images=[o for o in objects if o.get('kind')=='image']
 measured=[o for o in texts if any(p.get('text_chars',0)>0 and p.get('bbox') for p in o.get('paragraphs',[]))]
 global_reasons=[]
 if ctx.get('availability',{}).get('native')!='available':global_reasons.append('native_structure_unavailable')
 if not all(finite(page.get(k)) and page[k]>0 for k in ['width_pt','height_pt']):global_reasons.append('page_dimensions_unavailable')
 if not page.get('png_path') or not page.get('png_sha256'):global_reasons.append('original_image_identity_unavailable')
 if images and sum(o.get('text_chars',0) for o in measured)<30:global_reasons.append('raster_semantic_content_requires_full_image_review')
 for o in texts:
  if o.get('text_chars',0)>=8 and o.get('text_role') not in {'page_number','navigation'} and not o.get('paragraphs'):
   global_reasons.append('substantial_text_actual_layout_missing');break
 decisions={};deferred={}
 for k in LABELS:
  r=detectors.get(k,{})
  candidate=(finite(r.get('score')) and r.get('status')!='unsupported' and r['score']>=THRESHOLDS[k]) if k in THRESHOLDS else r.get('status')=='candidate'
  critical=[];semantic=[]
  if not r:critical.append('detector_missing')
  for u in r.get('unresolved',[]):
   kind=u.get('kind','untyped_unresolved');obj=byid.get(u.get('object_id'),{})
   if k=='D4' and kind=='image_crop_subject_and_intent_unknown':
    crop=u.get('crop') or {};horizontal=sum(max(0,crop.get(x,0)) for x in ['left','right']);vertical=sum(max(0,crop.get(x,0)) for x in ['top','bottom'])
    retained=max(0,1-horizontal)*max(0,1-vertical)
    semantic.append(dict(kind=kind,object_id=u.get('object_id'),retained_fraction=retained))
    if retained<=.25:critical.append('extreme_native_image_crop_requires_subject_review')
   elif k=='D1' and kind=='text_role_unresolved':
    f=_font(obj)
    semantic.append(dict(kind=kind,object_id=u.get('object_id'),font=f))
    if obj.get('text_chars',0)>=8 and (f is None or f*540/height<12):critical.append('unresolved_role_with_small_or_missing_font')
   else:critical.append(kind)
  if k=='D5':
   f=r.get('features',{});unknown=f.get('unknown_area_fraction');lower=f.get('content_occupancy_lower')
   if finite(unknown) and unknown>0:semantic.append(dict(kind='unassessed_content_regions',area_fraction=unknown))
   if r.get('status')=='unsupported':
    if page.get('content_type')=='image_only':critical.append('D5_native_content_unavailable')
    else:semantic.append(dict(kind='D5_semantics_unsupported'))
   elif not finite(r.get('score')):
    if page.get('role') not in {'cover','section','ending','contents'}:critical.append('D5_no_measurable_body_score')
   if finite(unknown) and finite(lower) and lower<.10 and 0<unknown<.30:critical.append('low_known_content_and_small_unresolved_regions')
  if k=='D6':
   if material is None or material.get('status')=='unknown':critical.append('material_pixels_unavailable')
   elif material.get('status')=='targeted_review_candidate':critical.append('targeted_bitmap_readability_evidence')
   if material and material.get('requires_unresolved_material_review'):semantic.append(dict(kind='A05_semantics_unsupported'))
  # Score absence is a capability gap. Do not turn it into a negative or a
  # confirmed defect; severe native loss is routed at the page level above.
  if not finite(r.get('score')) and not critical:semantic.append(dict(kind='no_finite_score_in_available_scope'))
  decisions[k]=dict(automatic_risk_candidate=bool(candidate),review_required=bool(candidate or critical),
    critical_review_reasons=list(dict.fromkeys(critical)),semantic_unresolved=semantic,source_status=r.get('status','missing'),
    ordinal_score=r.get('score'),threshold=THRESHOLDS.get(k),coverage=r.get('coverage','unknown'))
  if semantic:deferred[k]=semantic
 required=bool(global_reasons or any(v['review_required'] for v in decisions.values()))
 return dict(version=VERSION,policy='evidence_qualified',review_required=required,
    route='llm_review' if required else 'no_trigger_with_disclosed_unassessed_scope',decisions=decisions,
    candidate_labels=[k for k,v in decisions.items() if v['automatic_risk_candidate']],
    uncertain_labels=[k for k,v in decisions.items() if v['critical_review_reasons'] or v['semantic_unresolved']],
    global_review_reasons=list(dict.fromkeys(global_reasons)),semantic_unresolved=deferred,
    entire_page_certified_normal=False,zero_future_miss_guarantee=False,llm_review_executed=False)
