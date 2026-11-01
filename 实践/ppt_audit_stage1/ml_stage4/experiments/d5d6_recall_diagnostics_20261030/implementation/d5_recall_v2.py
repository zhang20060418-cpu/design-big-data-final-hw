"""Bounded D5 r2: allocated native layout space versus semantic content ink.

R1 is retained unchanged. A separate layout-blocker raster records only proven
native containers with multiple measured information children. It does not
change semantic content/unknown masks or claim container interiors are ink.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import math
from pathlib import Path
import numpy as np

_spec=importlib.util.spec_from_file_location('d5_known_space_frozen_r1',Path(__file__).with_name('d5_recall.py'))
R1=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(R1)
VERSION='d5-allocated-layout-recall-r2'
CONFIG={
    'container_min_page_area':.01,'container_max_page_area':.60,
    'minimum_measured_children':2,'child_containment_min':.95,
    'minimum_child_bbox_width_fraction':.10,'minimum_child_bbox_height_fraction':.10,
    'sparse_rows_min':3,'sparse_rows_max_occupancy_upper':.12,
    'sparse_rows_min_side_void':.20,'sparse_rows_span_x_min':.50,
    'sparse_rows_span_x_max':.80,'sparse_rows_span_y_min':.40,
    'sparse_rows_anchor_max':.12,'sparse_rows_centroid_side_max':.45,
    'sparse_rows_gap':.025,'sparse_rows_component':.50,
    'local_underfill_min_container_page_area':.15,
    'local_underfill_max_occupancy_upper':.10,
    'local_underfill_max_content_bbox_area_fraction':.20,
    'local_underfill_min_empty_rectangle_fraction':.60,
}


def _area(b):return b['w']*b['h'] if b else 0.


def _inter(a,b):
    if not a or not b:return None
    x,y=max(a['x'],b['x']),max(a['y'],b['y'])
    right,bottom=min(a['x']+a['w'],b['x']+b['w']),min(a['y']+a['h'],b['y']+b['h'])
    return dict(x=x,y=y,w=right-x,h=bottom-y) if right>x and bottom>y else None


def _bounds(boxes):
    if not boxes:return None
    x,y=min(b['x'] for b in boxes),min(b['y'] for b in boxes)
    return dict(x=x,y=y,w=max(b['x']+b['w'] for b in boxes)-x,
                h=max(b['y']+b['h'] for b in boxes)-y)


def _unrotated(o):
    v=R1._num(o.get('rotation',0))
    return v is not None and min(v%360,360-v%360)<1e-6


def _geometry(mask,unknown):
    blocked=mask|unknown;h,w=mask.shape;yy,xx=np.nonzero(mask)
    if not len(xx):return None
    b=dict(x=float(xx.min()/w),y=float(yy.min()/h),w=float((xx.max()-xx.min()+1)/w),h=float((yy.max()-yy.min()+1)/h))
    center=[float((xx.mean()+.5)/w),float((yy.mean()+.5)/h)]
    y,x=np.nonzero(blocked)
    axis=dict(left=float(x.min()/w),right=float((w-1-x.max())/w),top=float(y.min()/h),bottom=float((h-1-y.max())/h))
    return dict(bounds=b,centroid=center,offset=math.hypot(center[0]-.5,center[1]-.5)/math.sqrt(.5),
        axis=axis,largest_empty=R1._largest_empty(blocked),
        occupancy_lower=float(mask.mean()),occupancy_upper=float(blocked.mean()))


def layout_containers(ctx,body_ids):
    """Allocation evidence; no source IDs, names or generator conventions."""
    body_ids=set(body_ids);nodes=[];accepted=[]
    for o in ctx.get('objects',[]):
        if o.get('id') not in body_ids:continue
        regions=[b for v in o.get('content_regions',[]) if (b:=R1._box(v))]
        if not regions or not _unrotated(o):continue
        if o.get('kind') not in {'text','cell','table','chart','image'}:continue
        if o.get('kind') in {'text','cell'} and not o.get('text_chars',0):continue
        nodes.append((o,_bounds(regions)))
    for o in ctx.get('objects',[]):
        b=R1._box(o.get('bbox'));native=o.get('native_shape') or {}
        if (o.get('kind')!='shape' or o.get('text_chars',0) or o.get('text') or not b
            or not CONFIG['container_min_page_area']<=_area(b)<=CONFIG['container_max_page_area']
            or not _unrotated(o) or o.get('geometry_type') not in {'rect','roundRect'}
            or o.get('content_class') in {'background','information','unknown'}
            or not (native.get('has_visible_fill') is True or native.get('has_visible_line') is True)):
            continue
        children=[]
        z=R1._num(o.get('z_order'))
        if z is None:continue
        for node,nb in nodes:
            nz=R1._num(node.get('z_order'))
            if nz is None or nz<=z or node.get('id')==o.get('id'):continue
            if _area(_inter(nb,b))/_area(nb)>=CONFIG['child_containment_min']:
                children.append((node,nb))
        if len(children)<CONFIG['minimum_measured_children']:continue
        bounds=_bounds([nb for _,nb in children])
        if bounds['w']/b['w']<CONFIG['minimum_child_bbox_width_fraction'] or bounds['h']/b['h']<CONFIG['minimum_child_bbox_height_fraction']:continue
        accepted.append(dict(object_id=o.get('id'),bbox=b,child_object_ids=[n.get('id') for n,_ in children],
            child_bounds=bounds,measurement_source='native_rectangular_allocated_space_with_multiple_measured_front_children',
            occupancy_kind='layout_allocation_not_opaque_ink_or_semantic_information'))
    return accepted


def _bands(boxes,gap):
    out=[]
    for b in sorted(boxes,key=lambda b:b['y']):
        if out and b['y']<=out[-1][1]+gap:out[-1][1]=max(out[-1][1],b['y']+b['h'])
        else:out.append([b['y'],b['y']+b['h']])
    return out


def propose(ctx,previous_result=None):
    result=R1.propose(ctx,previous_result)
    result['version']=VERSION;f=result['features'];f['bounded_r2_config']=deepcopy(CONFIG)
    f['r1_score']=result.get('score');f['r1_status']=result['status']
    if result.get('score') is None or f.get('role_gate'):return result
    canvas=f.get('body_canvas')
    if not canvas:return result
    shared=ctx['spatial'];shape=shared['content_mask'].shape;h,w=shape
    x0=round(canvas['x']*w);x1=round((canvas['x']+canvas['w'])*w)
    y0=round(canvas['y']*h);y1=round((canvas['y']+canvas['h'])*h)
    body=np.zeros(shape,dtype=bool);unresolved=shared['unknown_mask'].copy();known=np.zeros(shape,dtype=bool)
    ids=set(f['body_object_ids']);body_boxes=[]
    for o in ctx.get('objects',[]):
        regions=[b for v in o.get('content_regions',[]) if (b:=R1._box(v))]
        if o.get('id') in ids:
            for b in regions:R1._paint(body,b);body_boxes.append(b)
        if o.get('content_class')=='information' or o.get('semantic_layer') in {'title','body','footer','auxiliary'}:
            for b in regions:R1._paint(known,b)
        if o.get('content_class')=='unknown' or (o.get('semantic_layer')=='unknown' and o.get('content_class') not in {'background','decoration'}):
            R1._paint(unresolved,R1._box(o.get('bbox')))
    unresolved |= shared['content_mask'] & ~known
    unresolved &= ~known
    containers=layout_containers(ctx,ids);layout=np.zeros(shape,dtype=bool)
    for c in containers:R1._paint(layout,c['bbox'])
    # The unknown mask is retained separately. A blocker union never clears it.
    b=body[y0:y1,x0:x1];u=unresolved[y0:y1,x0:x1];allocated=(body|layout)[y0:y1,x0:x1]
    geom=_geometry(allocated,u)
    if geom is None:return result
    empty=geom['largest_empty'];empty['bbox_page']=R1._to_page(empty['bbox'],canvas)
    components=dict(known_empty_rectangle_outside_allocated_modules=empty['area_ratio'],
        continuous_axis_void_outside_allocated_modules=max(geom['axis'].values()),
        compact_displaced_allocated_body=(1-_area(geom['bounds']))*geom['offset'],
        retained_previous_candidate=.55 if (previous_result or {}).get('status')=='candidate' else 0.)
    local=[]
    for c in containers:
        box=c['bbox'];a=max(0,math.floor(box['x']*w));z=min(w,math.ceil((box['x']+box['w'])*w))
        t=max(0,math.floor(box['y']*h));k=min(h,math.ceil((box['y']+box['h'])*h))
        localgeom=_geometry(body[t:k,a:z],unresolved[t:k,a:z])
        if localgeom:
            strong=(_area(box)>=CONFIG['local_underfill_min_container_page_area']
                and localgeom['occupancy_upper']<=CONFIG['local_underfill_max_occupancy_upper']
                and _area(localgeom['bounds'])<=CONFIG['local_underfill_max_content_bbox_area_fraction']
                and localgeom['largest_empty']['area_ratio']>=CONFIG['local_underfill_min_empty_rectangle_fraction'])
            local.append(dict(container_object_id=c['object_id'],bbox=box,measurements=localgeom,strong_underfill=bool(strong)))
    components['local_container_compact_underfill']=max((x['measurements']['largest_empty']['area_ratio'] for x in local if x['strong_underfill']),default=0.)
    # An explicit pre-existing sparse-row hypothesis: low content area, several
    # separated bands, anchored to one side with a continuous unused corridor.
    # A card is allocated layout space, so sparse glyph rows inside cards do
    # not qualify. This intentionally remains a weak review candidate.
    rawgeom=_geometry(b,u);bands=_bands(body_boxes,CONFIG['sparse_rows_gap'])
    side='right' if rawgeom['axis']['right']>=rawgeom['axis']['left'] else 'left'
    sidevoid=rawgeom['axis'][side]
    anchor=rawgeom['bounds']['x'] if side=='right' else 1-rawgeom['bounds']['x']-rawgeom['bounds']['w']
    sidecenter=rawgeom['centroid'][0] if side=='right' else 1-rawgeom['centroid'][0]
    row=(not containers and len(bands)>=CONFIG['sparse_rows_min']
        and rawgeom['occupancy_upper']<=CONFIG['sparse_rows_max_occupancy_upper']
        and sidevoid>=CONFIG['sparse_rows_min_side_void']
        and CONFIG['sparse_rows_span_x_min']<=rawgeom['bounds']['w']<=CONFIG['sparse_rows_span_x_max']
        and rawgeom['bounds']['h']>=CONFIG['sparse_rows_span_y_min']
        and anchor<=CONFIG['sparse_rows_anchor_max'] and sidecenter<=CONFIG['sparse_rows_centroid_side_max'])
    components['sparse_anchored_rows_with_lateral_void']=CONFIG['sparse_rows_component'] if row else 0.
    score=float(max(components.values()));unknown=float(u.mean())
    status='candidate' if score>=R1.CONFIG['default_threshold'] else 'unknown' if unknown>0 or f.get('missing_content_region_object_ids') else 'no_candidate_in_available_scope'
    result.update(score=score,status=status,score_components=components,review_required=status in {'candidate','unknown'})
    f.update(layout_allocation=dict(containers=containers,allocated_mask_area_fraction=float(layout.mean()),
        semantic_content_mask_changed=False,unknown_mask_cleared=False,occupancy_kind='native_layout_allocation_not_ink'),
        allocated_body_geometry=geom,local_container_measurements=local,
        sparse_row_measurements=dict(bands=bands,body_occupancy_upper=rawgeom['occupancy_upper'],
            side_void=sidevoid,anchor=anchor,side_centroid=sidecenter,triggered=bool(row)),score_components=components,
        scoring_contract='max(independent_known_void_or_concentration_outside_verified_layout_allocation,local_compact_underfill,sparse_rows,previous_candidate)')
    f['triggered_rules']=['allocated_layout_space_whitespace_review'] if status=='candidate' else []
    result['evidence']=[dict(kind='known_empty_region_outside_verified_native_layout_allocation',
        object_ids=f['body_object_ids'],bbox=empty['bbox_page'],content_bbox=R1._to_page(geom['bounds'],canvas),
        measurement_source=shared.get('mask_source','unknown'),metrics=dict(score_components=components))]
    if row:result['evidence'].append(dict(kind='sparse_measured_rows_with_continuous_lateral_void',
        object_ids=f['body_object_ids'],bbox=f['body_bounds_page'],measurement_source='measured_paragraph_bands_and_known_space',
        metrics=f['sparse_row_measurements']))
    result['limitations'].append('Container layout allocation protects internal breathing room, while separate local compact-underfill checks remain exploratory.')
    return result


detect=propose
