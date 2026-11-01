"""R2: use known image alpha bounds; local disorganization needs no page spread."""
from copy import deepcopy
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('precision_r1_evidence_frozen',Path(__file__).with_name('evidence_refinement.py'))
R1=importlib.util.module_from_spec(spec);spec.loader.exec_module(R1)
VERSION='evidence-refinement-r2'
refine_decorations=R1.refine_decorations
background_variant=R1.background_variant

def refine_occlusion(ctx,previous):
 local=deepcopy(ctx);bounds=[]
 for o in local['objects']:
  op=o.get('opacity') or {};limits=op.get('alpha_extrema')
  if o.get('kind')=='image' and isinstance(limits,list) and len(limits)==2 and all(R1.finite(v) and 0<=v<=255 for v in limits) and limits[0]<=limits[1]:
   o.setdefault('opacity',{})['alpha']=limits[0]/255.
   bounds.append(dict(object_id=o['id'],lower_alpha=limits[0]/255.,upper_alpha=limits[1]/255.,source='verified_embedded_image_alpha_extrema'))
 result=R1.refine_occlusion(local,previous)
 # Rectangle contact of text layout envelopes is not automatically glyph loss.
 result['evidence']=[e for e in result['evidence'] if e.get('kind')!='intersecting_text_layout_requires_glyph_review']
 if result['evidence']:
  result['status']='candidate';result['score']=max(e.get('ordinal_strength',.65) for e in result['evidence'])
 else:result['status']=previous['status'];result['score']=previous['score']
 result['features']['embedded_alpha_bounds']=bounds;result['version']=VERSION
 return result

def refine_clutter(ctx,previous):
 out=R1.refine_clutter(ctx,previous);f=previous.get('features',{});nodes=f.get('nodes',[]);ids=f.get('residual_object_ids',[])
 strongest=f.get('strongest_local_window') or {};gap=f.get('median_nearest_gap_font_units')
 # A concentrated cluster is precisely a local phenomenon; requiring it to
 # occupy four whole-page grid cells caused a deterministic coverage gap.
 trigger=(len(ids)>=12 and strongest.get('node_count',0)>=10 and R1.finite(gap) and gap<=3.0
          and len(f.get('grid',{}).get('protected_object_ids',[]))<len(nodes)*.5)
 if trigger:
  item=R1.evidence(strongest.get('object_ids',ids),'concentrated_disorganized_microtext_cluster',strongest.get('bbox'),residual_count=len(ids),local_count=strongest.get('node_count'),median_gap_font_units=gap,whole_page_spread_required=False)
  out=R1.add_candidate(out,[item])
 out['features']['localized_disorganization']=dict(trigger=trigger,residual_count=len(ids),local_count=strongest.get('node_count',0),gap_font_units=gap)
 out['version']=VERSION
 return out
