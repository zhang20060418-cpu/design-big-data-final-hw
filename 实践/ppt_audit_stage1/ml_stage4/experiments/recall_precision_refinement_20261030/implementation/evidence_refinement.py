"""Native/visual evidence refinements. No labels, IDs, names or notes as features."""
from collections import Counter
from copy import deepcopy
from functools import lru_cache
from io import BytesIO
from itertools import combinations
import hashlib,math
from pathlib import Path
from statistics import median
from zipfile import ZipFile
import numpy as np
from PIL import Image

VERSION='evidence-refinement-r1'
def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def box(v):
 if not isinstance(v,dict) or not all(finite(v.get(k)) for k in ['x','y','w','h']):return None
 return v if v['w']>0 and v['h']>0 else None
def area(b):return max(0,b['w'])*max(0,b['h']) if b else 0.
def inter(a,b):
 a=box(a);b=box(b)
 if not a or not b:return None
 x=max(a['x'],b['x']);y=max(a['y'],b['y']);w=min(a['x']+a['w'],b['x']+b['w'])-x;h=min(a['y']+a['h'],b['y']+b['h'])-y
 return dict(x=x,y=y,w=w,h=h) if min(w,h)>0 else None
def paragraphs(o):return [p for p in o.get('paragraphs',[]) if p.get('text_chars',0)>0 and box(p.get('bbox'))]
def font(o):
 vs=[p.get('font_pt') for p in paragraphs(o)]+list(o.get('font_sizes_pt',[]));vs=[v for v in vs if finite(v) and v>0]
 return median(vs) if vs else None
def evidence(ids,kind,bbox=None,**measure):
 return dict(object_ids=ids,kind=kind,measurement_source='native_OpenXML_and_COM_measured_paragraphs',bbox=bbox,measurements=measure,ordinal_strength=.65)
def add_candidate(result,items):
 out=deepcopy(result);out['evidence'].extend(items);out['version']=VERSION+'+'+str(result.get('version',''))
 if items:out['status']='candidate';out['score']=max(float(result.get('score') or 0),max(e.get('ordinal_strength',.65) for e in items))
 return out
def alpha(o):
 v=o.get('native_shape',{}).get('fill_alpha')
 if finite(v) and 0<=v<=1:return v
 op=o.get('opacity') or {};v=op.get('alpha')
 if finite(v) and 0<=v<=1:return v
 if op.get('state')=='fully_opaque':return 1.
 if op.get('state')=='transparent':return 0.
 return None

def refine_occlusion(ctx,previous):
 w=ctx['page']['width_pt'];h=ctx['page']['height_pt'];items=[];records=[]
 for target in ctx['objects']:
  f=font(target)
  if target.get('text_chars',0)<4 or not f:continue
  if abs(target.get('rotation',0) or 0)>1e-6:continue
  for upper in ctx['objects']:
   if upper.get('z_order',-1)<=target.get('z_order',-1):continue
   if abs(upper.get('rotation',0) or 0)>1e-6:continue
   a=alpha(upper);ub=box(upper.get('bbox'))
   if a is None or a<.60 or not ub:continue
   if upper.get('kind')!='image' and upper.get('geometry_type')!='rect':continue
   for p in paragraphs(target):
    hit=inter(p['bbox'],ub)
    if not hit:continue
    coverage=area(hit)/area(p['bbox']);lostx=hit['w']*w/f;losty=hit['h']*h/f
    sufficient=coverage>=.20 and lostx>=2. and losty>=.60
    records.append(dict(target_object_id=target['id'],upper_object_id=upper['id'],alpha=a,coverage=coverage,lost_font_x=lostx,lost_font_y=losty,trigger=sufficient))
    if sufficient:items.append(evidence([target['id'],upper['id']],'substantial_local_span_under_known_partially_opaque_object',hit,alpha=a,coverage=coverage,lost_font_x=lostx,lost_font_y=losty,glyph_visibility_confirmed=False))
 # Actual paragraph envelopes intersecting text foreground remain a review
 # witness, not a claim that the full rectangle is opaque ink.
 for u in previous.get('unresolved',[]):
  if u.get('kind')=='text_foreground_layout_intersection_requires_glyph_confirmation':
   items.append(evidence(u.get('object_ids',[]),'intersecting_text_layout_requires_glyph_review',u.get('bbox'),glyph_confirmed=False))
 out=add_candidate(previous,items);out['features']['refined_local_opacity_measurements']=records
 return out

def refine_decorations(ctx,previous):
 texts=[o for o in ctx['objects'] if o.get('kind') in {'text','cell'} and o.get('text_chars',0)>=8 and o.get('text_role') not in {'navigation','page_number'}]
 shapes=[];hitpairs=[]
 for o in ctx['objects']:
  b=box(o.get('bbox'));a=alpha(o);geom=o.get('native_shape',{}).get('preset_geometry') or o.get('geometry_type')
  if o.get('kind')!='shape' or o.get('text_chars',0) or not b or not finite(a) or not .10<=a<=.95 or not .0008<=area(b)<=.18:continue
  if geom in {'rect','roundRect','round1Rect','round2SameRect'}:continue
  shapes.append(o)
  for t in texts:
   if o.get('z_order',-1)<=t.get('z_order',-1):continue
   for p in paragraphs(t):
    hit=inter(b,p['bbox'])
    if area(hit)/max(area(p['bbox']),1e-9)>=.15:hitpairs.append((o['id'],t['id'],hit))
 distinct={a for a,b,h in hitpairs}
 trigger=len(shapes)>=4 and len(distinct)>=2
 items=[]
 if trigger:
  items=[evidence(sorted({a for a,b,h in hitpairs}|{b for a,b,h in hitpairs}),'multiple_translucent_noncontainer_ornaments_overlap_measured_text',None,shape_count=len(shapes),interfering_shapes=len(distinct),overlap_pairs=len(hitpairs),semantic_intent='unverified')]
  items[0]['ordinal_strength']=.80
 out=add_candidate(previous,items);out['features']['decoration_interference']=dict(shape_count=len(shapes),interfering_shapes=len(distinct),pairs=[dict(upper_object_id=a,target_object_id=b,bbox=h) for a,b,h in hitpairs],trigger=trigger)
 # Preserve old A05 unsupported status; this is A03 review evidence only.
 return out

@lru_cache(maxsize=40)
def _assets(path):
 result={}
 try:
  with ZipFile(path) as z:
   for name in z.namelist():
    if not name.startswith('ppt/media/'):continue
    data=z.read(name);digest=hashlib.sha256(data).hexdigest()
    try:
     with Image.open(BytesIO(data)) as im:
      im=im.convert('RGB');im.thumbnail((320,320),Image.Resampling.BICUBIC);rgb=np.asarray(im,dtype=np.float32)/255.
     dx=np.max(abs(rgb[:,1:]-rgb[:,:-1]),axis=2);dy=np.max(abs(rgb[1:]-rgb[:-1]),axis=2)
     result[digest]=dict(edge_fraction=float((np.mean(dx>.04)+np.mean(dy>.04))/2),rgb_std=float(np.std(rgb,axis=(0,1)).mean()))
    except (OSError,ValueError):result[digest]=dict(status='unavailable')
 except (OSError,ValueError):pass
 return result

def background_variant(ctx,peers):
 """Return a separate, explicitly hypothetical D5 context; original untouched.

 Full-page photos/infographics without substantial native overlays are never
 removed. Repeated low-frequency bottom-layer media plus native overlaid text
 supports a background hypothesis, not proof of human design intent.
 """
 out=deepcopy(ctx);out.pop('spatial',None);candidates=[]
 counts=Counter(o.get('image_sha256') for p in peers for o in p['objects'] if o.get('kind')=='image')
 assets=None
 for o in out['objects']:
  b=box(o.get('bbox'))
  if o.get('kind')!='image' or not b or area(inter(b,dict(x=0.,y=0.,w=1.,h=1.)))<.85:continue
  overlays=[v for v in out['objects'] if v.get('z_order',-1)>o.get('z_order',-1) and v.get('kind') in {'text','cell'} and paragraphs(v) and v.get('text_role') not in {'page_number','navigation'}]
  if len(overlays)<3 or sum(v.get('text_chars',0) for v in overlays)<60:continue
  if assets is None:assets=_assets(str(ctx.get('source',{}).get('pptx_path','')))
  pixel=assets.get(o.get('image_sha256'),{})
  supported=counts[o.get('image_sha256')]>=2 and pixel.get('edge_fraction',1) <=.035
  candidates.append(dict(object_id=o['id'],repeated_asset_count=counts[o.get('image_sha256')],overlaid_native_text_objects=len(overlays),pixels=pixel,background_hypothesis=supported))
  if supported:
   o['content_class']='background';o['content_regions']=[];o['semantic_layer']='background';o['semantic_role']='background_candidate';o['classification_source']='repeated_low_frequency_bottom_image_plus_substantial_native_text_overlay'
 return out,dict(candidates=candidates,background_ids=[r['object_id'] for r in candidates if r['background_hypothesis']],original_masks_unchanged=True,semantic_intent_confirmed=False)

def refine_clutter(ctx,previous):
 """Add local paragraph interference without treating table cells as pages."""
 nodes=[];items=[]
 for o in ctx['objects']:
  if o.get('kind')!='text' or o.get('text_role') in {'title','navigation','page_number'}:continue
  f=font(o)
  if not f:continue
  for p in paragraphs(o):
   if p['bbox']['y']<.18 or p['bbox']['y']>.9:continue
   nodes.append(dict(id=o['id'],bbox=p['bbox'],font=f,chars=p.get('text_chars',0)))
 pairs=[]
 for a,b in combinations(nodes,2):
  if a['id']==b['id']:continue
  hit=inter(a['bbox'],b['bbox']);fraction=area(hit)/max(1e-9,min(area(a['bbox']),area(b['bbox'])))
  if fraction>=.20:pairs.append((a,b,hit,fraction))
 # Independent pairs across >=6 text objects, not a single duplicated label.
 ids={n['id'] for a,b,h,f in pairs for n in [a,b]}
 if len(pairs)>=3 and len(ids)>=6:
  items=[evidence(sorted(ids),'multiple_independent_body_paragraph_envelopes_interfere',None,pairs=len(pairs),object_count=len(ids),glyph_confirmation=False)]
 out=add_candidate(previous,items);out['features']['body_paragraph_interference']=dict(pairs=len(pairs),object_count=len(ids),trigger=bool(items))
 return out
