"""Partial-information D5 geometry, with independently measurable whitespace.

One fixed candidate, developed on native train geometry. The caller may choose
an operating threshold on train. The score is ordinal and never a probability.
Unknown pixels remain blockers; an unresolved card does not erase separately
measurable, known empty space. This does not identify semantic intended blanks.
"""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np

VERSION = "d5-known-space-recall-v1"
CONFIG = {
    "canvas_margin": .04, "canvas_bottom": .92, "header_gap": .04,
    "header_bottom_max": .28, "raster_policy": "use_supplied_shared_boolean_raster",
    "protected_roles": ("cover", "section", "ending"),
    "default_threshold": .50,
}


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _box(value):
    if not isinstance(value, dict):
        return None
    b = {k: _num(value.get(k)) for k in ("x", "y", "w", "h")}
    if any(v is None for v in b.values()) or min(b["w"], b["h"]) <= 0:
        return None
    x, y = max(0., b["x"]), max(0., b["y"])
    right, bottom = min(1., b["x"] + b["w"]), min(1., b["y"] + b["h"])
    return dict(x=x, y=y, w=right-x, h=bottom-y) if right>x and bottom>y else None


def _mask(value):
    return isinstance(value, np.ndarray) and value.ndim == 2 and value.dtype == np.bool_ and value.size > 0


def _paint(mask, box):
    if box:
        h,w=mask.shape
        mask[max(0,math.floor(box['y']*h)):min(h,math.ceil((box['y']+box['h'])*h)),
             max(0,math.floor(box['x']*w)):min(w,math.ceil((box['x']+box['w'])*w))]=True


def _largest_empty(blocked):
    """Exact largest rectangle on the raster, with unknown pixels blocked."""
    h,w=blocked.shape; heights=np.zeros(w,dtype=int); best_area=0; best=None
    for row in range(h):
        heights=np.where(blocked[row],0,heights+1); stack=[]
        for col in range(w+1):
            bar=int(heights[col]) if col<w else 0; left=col
            while stack and stack[-1][1]>bar:
                start,old=stack.pop(); area=old*(col-start)
                candidate=(row-old+1,start,row+1,col)
                if area>best_area or (area==best_area and area and (best is None or candidate<best)):
                    best_area,best=area,candidate
                left=start
            if bar and (not stack or stack[-1][1]<bar): stack.append((left,bar))
    if best is None:return dict(area_ratio=0.,bbox=None)
    t,l,b,r=best
    return dict(area_ratio=float(best_area/blocked.size),bbox=dict(x=l/w,y=t/h,w=(r-l)/w,h=(b-t)/h))


def _to_page(box, canvas):
    if box is None:return None
    return dict(x=canvas['x']+box['x']*canvas['w'],y=canvas['y']+box['y']*canvas['h'],
                w=box['w']*canvas['w'],h=box['h']*canvas['h'])


def _navigation(ctx):
    if ctx.get('page',{}).get('role')!='contents':return False
    for e in ctx.get('page',{}).get('role_evidence',[]):
        if (e.get('kind')=='contents_navigation_structure' and e.get('accepted') is True
            and not e.get('substantial_paragraph_ids') and not e.get('substantial_media_ids')
            and not e.get('drawn_table_like')
            and (len(e.get('numbered_label_pairs',[]))>=3 or len(e.get('inline_numbered_ids',[]))>=3
                 or e.get('aligned_short_rows',0)>=4)):
            return True
    return False


def propose(ctx, previous_result=None):
    """Return one D5 score/evidence record, with no labels or file reads.

    Partial uncertainty can coexist with positive known-space evidence. If no
    such support is available, the result is unknown rather than a score zero.
    Existing candidate evidence is retained as a component, not used as truth.
    """
    p=ctx.get('page',{}); previous=previous_result or {}
    result=dict(detector='D5',label='excessive_whitespace',version=VERSION,
        status='unknown',coverage='partial',score=None,score_kind='ordinal_heuristic_not_probability',
        features=dict(config=deepcopy(CONFIG),page_role=p.get('role','unknown'),
                      previous_status=previous.get('status'),triggered_rules=[]),evidence=[],
        review_required=True,review_reasons=[],limitations=[
            'Low native occupancy and a large empty region do not establish intended whitespace.',
            'Unknown geometry is blocked in all empty-space measurements and never relabelled as background.',
            'The ordinal score is not a probability; a train-calibrated threshold may fail on other documents.'])
    native=ctx.get('availability',{}).get('native','unknown')
    if p.get('content_type')=='image_only' or p.get('role')=='image' or native in {'unavailable','unsupported','missing','failed','not_available'}:
        result.update(status='unsupported',coverage='unsupported');result['review_reasons']=['semantic_native_content_unavailable'];return result
    if p.get('role') in CONFIG['protected_roles'] or _navigation(ctx):
        result.update(status='no_candidate_in_available_scope',score=0.,review_required=False)
        result['features']['role_gate']='protected_minimal_or_measured_navigation_role';return result
    if p.get('role') not in {'body','chart','contents'}:
        result['review_reasons']=['unsupported_page_role'];return result
    spatial=ctx.get('spatial',{}); shared_content=spatial.get('content_mask'); shared_unknown=spatial.get('unknown_mask')
    if not _mask(shared_content) or not _mask(shared_unknown) or shared_content.shape!=shared_unknown.shape:
        result['review_reasons']=['shared_masks_missing_or_misaligned'];return result
    shape=shared_content.shape
    body=np.zeros(shape,dtype=bool); title=np.zeros(shape,dtype=bool); auxiliary=np.zeros(shape,dtype=bool)
    unresolved=shared_unknown.copy(); ids=[]; headers=[]; missing=[]
    for obj in ctx.get('objects',[]):
        cls=obj.get('content_class','unknown'); sem=obj.get('semantic_layer'); role=obj.get('text_role','unknown')
        regions=[b for v in obj.get('content_regions',[]) if (b:=_box(v))]
        if cls in {'background','decoration'}:continue
        if sem in {'footer','auxiliary'} or cls=='auxiliary' or role in {'page_number','navigation'} or (role=='footnote' and obj.get('role_subtype')!='local_annotation'):
            target=auxiliary
            if not regions:regions=[b for q in obj.get('paragraphs',[]) if q.get('text_chars',0) and (b:=_box(q.get('bbox')))]
        elif cls=='unknown' or sem=='unknown':
            _paint(unresolved,_box(obj.get('bbox')));continue
        elif sem=='title' or (sem!='body' and role=='title'):
            target=title
        elif cls=='information':
            target=body
        else:
            _paint(unresolved,_box(obj.get('bbox')));continue
        if not regions and target is not auxiliary:
            _paint(unresolved,_box(obj.get('bbox')));missing.append(obj.get('id'));continue
        # A short measured kicker above a top title does not establish body span.
        if target is body and obj.get('kind') in {'text','cell'} and regions and max(b['y']+b['h'] for b in regions)<=.18:
            target=title
        for b in regions:
            _paint(target,b)
            if target is title and b['y']+b['h']<=CONFIG['header_bottom_max']:headers.append(b)
        if target is body and regions:ids.append(obj.get('id'))
    known=body|title|auxiliary
    unresolved |= shared_content & ~known
    # Known content overlapping an unresolved rectangle remains known content;
    # unresolved remainder remains blocked. Nothing is inferred to be empty.
    unresolved &= ~known
    top=max(CONFIG['canvas_margin'],max((b['y']+b['h'] for b in headers),default=0.)+CONFIG['header_gap'])
    h,w=shape;x0=math.floor(CONFIG['canvas_margin']*w);x1=math.ceil((1-CONFIG['canvas_margin'])*w)
    y0=math.floor(top*h);y1=math.ceil(CONFIG['canvas_bottom']*h)
    if y1<=y0 or x1<=x0:
        result['review_reasons']=['no_valid_body_canvas'];return result
    b=body[y0:y1,x0:x1]; u=unresolved[y0:y1,x0:x1]; block=b|u|title[y0:y1,x0:x1]
    canvas=dict(x=x0/w,y=y0/h,w=(x1-x0)/w,h=(y1-y0)/h)
    if not b.any():
        result['review_reasons']=['no_measured_body_content'];return result
    yy,xx=np.nonzero(b); bh,bw=b.shape
    bounds=dict(x=float(xx.min()/bw),y=float(yy.min()/bh),w=float((xx.max()-xx.min()+1)/bw),h=float((yy.max()-yy.min()+1)/bh))
    centroid=[float((xx.mean()+.5)/bw),float((yy.mean()+.5)/bh)]
    offset=math.hypot(centroid[0]-.5,centroid[1]-.5)/math.sqrt(.5)
    empty=_largest_empty(block);empty['bbox_page']=_to_page(empty['bbox'],canvas)
    occupied_y,occupied_x=np.nonzero(block)
    axis=dict(left=float(occupied_x.min()/bw),right=float((bw-1-occupied_x.max())/bw),
              top=float(occupied_y.min()/bh),bottom=float((bh-1-occupied_y.max())/bh))
    side=max(axis.values()); e=empty['area_ratio']; bounds_area=bounds['w']*bounds['h']
    # Three mechanisms are independent of glyph count: a large provably empty
    # rectangle, a full-axis vacant strip, and displaced compact body content.
    # A zero-weight content-ink score avoids confusing large fonts with filling
    # a page, or penalising white chart interiors as absent information.
    components=dict(known_empty_rectangle=e,continuous_axis_void=side,
        compact_displaced_body=float((1-bounds_area)*offset),
        retained_previous_candidate=.55 if previous.get('status')=='candidate' else 0.)
    score=max(e,side,components['compact_displaced_body'],components['retained_previous_candidate'])
    score=float(min(1.,max(0.,score)))
    unknown_fraction=float(u.mean())
    coverage='full' if unknown_fraction==0 and not missing and native=='available' and p.get('content_type')=='native_editable' else 'partial'
    # Known empty geometry is still evidence when another part is unresolved.
    # Below the default operating point, residual uncertainty stays explicit.
    status='candidate' if score>=CONFIG['default_threshold'] else 'unknown' if unknown_fraction>0 or missing else 'no_candidate_in_available_scope'
    result.update(status=status,score=score,coverage=coverage,score_components=components,
                  review_required=status in {'candidate','unknown'})
    f=result['features'];f.update(raster_shape=list(shape),body_canvas=canvas,body_object_ids=ids,body_bounds_page=_to_page(bounds,canvas),
        body_centroid_page=[canvas['x']+centroid[0]*canvas['w'],canvas['y']+centroid[1]*canvas['h']],
        body_centroid_offset_half_diagonal=offset,body_bounds_area_ratio=bounds_area,
        content_occupancy_lower=float(b.mean()),content_occupancy_upper=float((b|u).mean()),
        unknown_area_fraction=unknown_fraction,missing_content_region_object_ids=missing,
        axis_known_empty_fractions=axis,largest_known_empty_rectangle=empty,score_components=components,
        scoring_contract='max(known_empty_rectangle,continuous_axis_void,compact_displaced_body,retained_previous_candidate)')
    if unknown_fraction>0 or missing:result['review_reasons'].append('partial_unresolved_content_elsewhere')
    if status=='candidate':f['triggered_rules']=['large_known_empty_space_or_displaced_compact_body']
    result['evidence'].append(dict(kind='known_empty_region_with_unknown_pixels_blocked',object_ids=ids,
        bbox=empty['bbox_page'],content_bbox=f['body_bounds_page'],
        measurement_source=spatial.get('mask_source','unknown'),
        metrics=dict(score_components=components,unknown_fraction=unknown_fraction,body_bounds_area_ratio=bounds_area)))
    return result


detect = propose
