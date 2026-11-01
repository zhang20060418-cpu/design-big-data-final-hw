"""Explicit coordinate contracts. Original COM values are never overwritten.

Bounds are layout envelopes, not proof that glyphs are legible or truncated.
The live cell adapter requires Office-control evidence for both axes. A simple
cell-origin translation is deliberately not accepted merely because it fits.
"""
from __future__ import annotations
import math
import copy
import hashlib,json
from pathlib import Path


def valid_bbox(box):
    return (isinstance(box, dict) and set(('x','y','w','h')).issubset(box)
            and all(isinstance(box[k], (float,int)) and math.isfinite(box[k]) for k in ('x','y','w','h'))
            and box['w'] >= 0 and box['h'] >= 0)


def translate_bbox(box, tx, ty):
    if not valid_bbox(box) or not all(math.isfinite(v) for v in (tx,ty)):
        raise ValueError('Nonfinite or invalid coordinate input')
    return dict(x=box['x']+tx,y=box['y']+ty,w=box['w'],h=box['h'])


def normalized_bbox(box, width_pt, height_pt):
    if not valid_bbox(box) or not math.isfinite(width_pt) or not math.isfinite(height_pt) or min(width_pt,height_pt)<=0:
        raise ValueError('Invalid page or bounds')
    return dict(x=box['x']/width_pt,y=box['y']/height_pt,w=box['w']/width_pt,h=box['h']/height_pt)


def cell_layout_to_slide(measurement, control_proof):
    """Use the separately measured page-space origin only when proven.

    Preferred COM TextFrame (v1) bounds are page space only after independent
    image checks on controls. TextFrame2 raw bounds are preserved as cell-local.
    This prevents extending the two-cell L observation to all cells blindly.
    """
    out={'status':'unknown','raw_bbox_pt':measurement.get('textframe2_bbox_pt'),
         'coordinate_space':'unverified_cell_text_layout','page_bbox_pt':None,
         'transform_matrix':None,'proof':None,'scope':'layout_envelope_not_glyph_mask'}
    rotation=measurement.get('rotation')
    if rotation is None or not isinstance(rotation,(float,int)) or not math.isfinite(rotation):
        out['reason']='rotation_unavailable_without_verified_parent_evidence';return out
    if abs(float(rotation))>1e-6 or measurement.get('group_path'):
        out['reason']='rotated_or_grouped_cell_transform_not_proven';return out
    if not control_proof or control_proof.get('status')!='verified_on_controls':
        out['reason']='no_independent_control_proof';return out
    method=control_proof.get('method')
    if method=='TextFrame2_cell_anchor_relative':
        box=measurement.get('textframe2_bbox_pt');frame=measurement.get('shape_frame_bbox_pt')
        margins=measurement.get('margins_pt',{});anchor=measurement.get('vertical_anchor')
        orientation=measurement.get('orientation')
        if not valid_bbox(box) or not valid_bbox(frame) or anchor not in (1,3,4) or orientation!=1:
            out['reason']='outside_controlled_horizontal_cell_geometry';return out
        if any(not isinstance(margins.get(k),(int,float)) or not math.isfinite(margins[k]) for k in ('top','bottom')):
            out['reason']='unavailable_cell_margins';return out
        # COM BoundTop is anchored around the cell's vertical alignment point.
        # This affine origin was checked against native-PowerPoint PNG glyphs.
        f={1:0.,3:.5,4:1.}[anchor]
        tx=frame['x'];ty=frame['y']+f*frame['h']+(.5-f)*(margins['top']+margins['bottom'])
        out.update(status='measured',coordinate_space='slide_pt_from_verified_cell_anchor',
                   page_bbox_pt=translate_bbox(box,tx,ty),transform_matrix=[[1,0,tx],[0,1,ty],[0,0,1]],
                   proof=control_proof.get('proof_id'),raw_source='live_COM_TextFrame2_anchor_relative',
                   applicability='horizontal_unrotated_table_cell; same Office version; layout envelope only')
        return out
    if method=='TextFrame_v1_Bound_page_space':
        box=measurement.get('textframe1_bbox_pt')
        if valid_bbox(box):
            out.update(status='measured',coordinate_space='slide_pt_live_COM_TextFrame_v1',
                       page_bbox_pt=dict(box),transform_matrix=[[1,0,0],[0,1,0],[0,0,1]],
                       proof=control_proof.get('proof_id'),raw_source='live_COM_TextFrame_v1')
            return out
    if method=='TextFrame2_local_plus_verified_origin':
        origin=measurement.get('verified_text_origin_pt')
        box=measurement.get('textframe2_bbox_pt')
        if origin and valid_bbox(box) and all(math.isfinite(origin[k]) for k in ('x','y')):
            out.update(status='measured',coordinate_space='slide_pt_from_live_cell_local',
                       page_bbox_pt=translate_bbox(box,origin['x'],origin['y']),
                       transform_matrix=[[1,0,origin['x']],[0,1,origin['y']],[0,0,1]],
                       proof=control_proof.get('proof_id'),raw_source='live_COM_TextFrame2')
            return out
    out['reason']='required_verified_measurement_missing_or_unsupported_method'
    return out


def ordinary_layout_contract(measurement):
    """Rotation/group transforms remain explicit unknown until validated."""
    box=measurement.get('textframe2_bbox_pt')
    out={'status':'unknown','raw_bbox_pt':box,'page_bbox_pt':None,'transform_matrix':None,
         'coordinate_space':'unverified_layout','scope':'layout_envelope_not_visible_pixel_boundary'}
    if measurement.get('rotation') is None or measurement.get('group_path') or abs(float(measurement.get('rotation',0)))>1e-6:
        out['reason']='rotation_or_group_needs_per_object_verified_transform';return out
    if valid_bbox(box):
        out.update(status='measured',page_bbox_pt=dict(box),coordinate_space='slide_pt_live_COM_TextFrame2',
                   transform_matrix=[[1,0,0],[0,1,0],[0,0,1]])
    else:out['reason']='invalid_measurement'
    return out


def apply_fresh_coordinates(contexts,payload):
    """Return new contexts; never mutate L or silently retain its two repairs.

    A caller must rebuild masks and object relations after this operation.
    Source SHA, sample ID and stable native cell ID are all mandatory joins.
    The source PNG remains the L PNG; new control/audit PNGs are not inputs.
    """
    proof_path=Path(__file__).resolve().parents[1]/'results/coordinate_transform_proof.json'
    if not proof_path.exists() or not payload.get('coordinate_proof_sha256'):
        raise ValueError('Live cell transform proof file/SHA is missing')
    proof_digest=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    proof_record=json.loads(proof_path.read_text(encoding='utf8'))
    if (proof_digest!=payload['coordinate_proof_sha256'] or proof_record.get('status')!='verified_on_controls'
            or proof_record.get('method')!='TextFrame2_cell_anchor_relative'):
        raise ValueError('Live cell transform proof failed SHA/status/method checks')
    copied=copy.deepcopy(contexts);lookup={};docs={}
    stats=dict(planned_cells=0,measured_cells=0,unknown_cells=0,paragraphs=0,
               replaced_old_origin_only_cells=0,sha_mismatch=0,unmatched_records=0,
               spatial_rebuild_required=True,coordinate_method='TextFrame2_cell_anchor_relative')
    for document in payload.get('documents',[]):
        if (not document.get('source_unchanged') or not document.get('source_sha256_before')
                or document.get('source_sha256_before')!=document.get('source_sha256_after')):
            raise ValueError('Fresh source changed or SHA unavailable')
        for record in (document.get('data') or {}).get('cells',[]):
            key=(record['sample_id'],record['object_id'])
            if key in lookup:raise ValueError('Duplicate fresh cell identity')
            lookup[key]=record;docs[key]=document
    stats['planned_cells']=len(lookup)
    found=set()
    for context in copied:
        width=context['page']['width_pt'];height=context['page']['height_pt']
        page_touched=False
        for obj in context['objects']:
            if obj.get('kind')!='cell':continue
            key=(context['sample_id'],obj['id']);record=lookup.get(key)
            oldparas=obj.get('paragraphs',[])
            if any('cell_local_translation' in p.get('coordinate_space','') for p in oldparas):
                stats['replaced_old_origin_only_cells']+=1
            obj['pre_M_coordinate_record']={'paragraphs':copy.deepcopy(oldparas),
                                           'unresolved_paragraphs':copy.deepcopy(obj.get('unresolved_paragraphs',[])),
                                           'availability':copy.deepcopy(obj.get('available',{})),
                                           'original_object_bbox':copy.deepcopy(obj.get('bbox'))}
            obj['paragraphs']=[];obj['content_regions']=[];obj['unresolved_paragraphs']=[];page_touched=True
            available=obj.setdefault('available',{})
            if record is None:
                stats['unknown_cells']+=1;available.update(paragraph_layout='coordinate_unverified',page_text_geometry='unknown')
                obj['coordinate_status']='unknown_no_fresh_record';continue
            found.add(key)
            source_sha=context.get('source',{}).get('pptx_sha256')
            if not source_sha or source_sha!=record.get('pptx_sha256') or source_sha!=docs[key].get('source_sha256_before'):
                stats['sha_mismatch']+=1;stats['unknown_cells']+=1
                available.update(paragraph_layout='coordinate_unverified',page_text_geometry='unknown')
                obj['coordinate_status']='unknown_source_sha_mismatch';continue
            frame=record.get('shape_frame_bbox_pt');oldbox=obj.get('bbox')
            if not valid_bbox(frame) or not valid_bbox(oldbox):
                raise ValueError('Missing actual frame for native identity verification')
            old_pt=dict(x=oldbox['x']*width,y=oldbox['y']*height,w=oldbox['w']*width,h=oldbox['h']*height)
            if max(abs(frame[k]-old_pt[k]) for k in ('x','y','w','h'))>.05:
                raise ValueError('Fresh cell frame disagrees with native stable object')
            if record.get('parent_object_id')!=obj.get('parent_id') or any(record.get(k)!=obj.get('cell',{}).get(k) for k in ('row','column')):
                raise ValueError('Fresh table row/column/parent identity mismatch')
            obj['fresh_coordinate_measurement']=copy.deepcopy(record)
            successful=[]
            for para in record.get('paragraphs',[]):
                contract=para.get('coordinate_contract',{})
                entry=dict(id=f"{obj['id']}-p{para['index']}",font_pt=para.get('effective_font_pt',para.get('font_pt')),
                           text_chars=para.get('text_chars',0),line_boxes=None,
                           raw_cell_anchor_bbox_pt=copy.deepcopy(para.get('textframe2_bbox_pt')),
                           coordinate_status=contract.get('status','unknown'),
                           coordinate_space=contract.get('coordinate_space','unverified_cell_text_layout'),
                           coordinate_proof=contract.get('proof'),transform_matrix=contract.get('transform_matrix'),
                           measurement_source='M_fresh_live_COM_cell_anchor+mechanical_PNG_proof')
                if (contract.get('status')=='measured' and valid_bbox(contract.get('page_bbox_pt'))
                        and contract.get('proof')=='results/coordinate_transform_proof.json'
                        and contract.get('coordinate_space')=='slide_pt_from_verified_cell_anchor'):
                    entry['bbox']=normalized_bbox(contract['page_bbox_pt'],width,height)
                    entry['page_bbox_pt']=dict(contract['page_bbox_pt']);successful.append(entry)
                else:
                    entry['unavailable_reason']=contract.get('reason','unverified_transform')
                    obj['unresolved_paragraphs'].append(entry)
            obj['paragraphs']=successful;stats['paragraphs']+=len(successful)
            if successful and len(successful)==len(record.get('paragraphs',[])):
                stats['measured_cells']+=1;obj['coordinate_status']='measured_control_verified_scope'
                available.update(paragraph_layout='fresh_control_verified_coordinate_space',page_text_geometry='available')
                obj['content_regions']=[dict(p['bbox']) for p in successful]
                obj['content_class']='information';obj['classification_source']='nonempty_fresh_measured_table_text'
                sizes=[p['font_pt'] for p in successful if isinstance(p.get('font_pt'),(float,int)) and p['font_pt']>0]
                if sizes:obj['font_sizes_pt']=sorted(set(sizes));obj['font_source']='M_fresh_live_COM_effective_font'
                rawwrap=record.get('wrap_raw')
                obj['word_wrap']=True if rawwrap==-1 else False if rawwrap==0 else None
            else:
                stats['unknown_cells']+=1;obj['coordinate_status']='unknown_or_partial_fresh_transform'
                available.update(paragraph_layout='coordinate_unverified',page_text_geometry='unknown')
                obj['content_class']='unknown';obj['classification_source']='fresh_cell_transform_not_fully_verified'
        if page_touched:
            context.setdefault('availability',{})['derived_spatial_features']='invalidated_requires_M_rebuild'
            context.setdefault('warnings',[]).append('M fresh cell coordinate update requires recomputing masks, content bounds and object relations; old cache values must not be used.')
    stats['unmatched_records']=len(set(lookup)-found)
    if stats['sha_mismatch'] or stats['unmatched_records']:
        raise ValueError('Fresh coordinate join failed identity/SHA checks: '+str(stats))
    return copied,stats
