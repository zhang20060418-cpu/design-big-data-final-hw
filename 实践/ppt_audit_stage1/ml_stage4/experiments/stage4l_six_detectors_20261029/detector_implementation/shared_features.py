"""One shared feature pass, with explicit semantic uncertainty.

Native paragraph envelopes are measured text layout, not foreground glyphs.
Heuristics infer roles, never annotations or author intent. No supervised fields
or historical model predictions are accepted by this module.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import math
import re
from statistics import median
import numpy as np
from PIL import Image

VERSION = 'shared-spatial-v1.1-coordinate-and-title-guard'


def area(b):
    return max(0., b['w']) * max(0., b['h'])


def intersection(a, b):
    x, y = max(a['x'], b['x']), max(a['y'], b['y'])
    return dict(x=x, y=y, w=max(0., min(a['x']+a['w'], b['x']+b['w'])-x),
                h=max(0., min(a['y']+a['h'], b['y']+b['h'])-y))


def bounds(boxes):
    boxes = [b for b in boxes if area(b) > 0]
    if not boxes:
        return None
    x, y = min(b['x'] for b in boxes), min(b['y'] for b in boxes)
    return dict(x=x, y=y, w=max(b['x']+b['w'] for b in boxes)-x,
                h=max(b['y']+b['h'] for b in boxes)-y)


def rect_pixels(b, shape):
    if any(not math.isfinite(float(b[k])) for k in ('x','y','w','h')) or b['w'] < 0 or b['h'] < 0:
        raise ValueError('Invalid rectangle for spatial rasterization')
    h, w = shape
    return (max(0, min(w, math.floor(b['x']*w))),
            max(0, min(h, math.floor(b['y']*h))),
            max(0, min(w, math.ceil((b['x']+b['w'])*w))),
            max(0, min(h, math.ceil((b['y']+b['h'])*h))))


def add_rect(mask, b):
    if any(not math.isfinite(float(b[k])) for k in ('x','y','w','h')) or b['w'] < 0 or b['h'] < 0:
        raise ValueError('Invalid content rectangle')
    if area(b) <= 0:
        return
    x0, y0, x1, y1 = rect_pixels(b, mask.shape)
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = True


def text_boxes(o):
    return [p['bbox'] for p in o.get('paragraphs', []) if p.get('text_chars', 0) and area(p['bbox']) > 0]


def measured_fonts(o):
    values = [p['font_pt'] for p in o.get('paragraphs', []) if p.get('font_pt') is not None]
    return values or o.get('font_sizes_pt', [])


def infer_page_role(ctx):
    """Conservative role cues. Sparse geometry alone never proves a section."""
    os = ctx['objects']; ts = [o for o in os if o['text_chars'] > 0 and o['kind'] != 'cell']
    total = sum(o['text_chars'] for o in ts)
    heading = ' '.join(o['text'] for o in ts if o['bbox']['y'] < .30)[:200].strip().lower()
    if ctx['page']['content_type'] == 'image_only':
        return 'image', 'full_page_raster_without_native_text', .9
    if any(k in heading for k in ['contents', '目录', '议程', 'agenda']):
        return 'contents', 'native_heading_keyword', .8
    if any(k in heading for k in ['thank you', '谢谢', '感谢聆听', '结束语', 'questions?']):
        return 'ending', 'native_heading_keyword', .8
    if total <= 180 and any(re.match(r'^\s*(chapter\s+\d+|第[一二三四五六七八九十\d]+[章节]|part\s+[\divx]+)', o['text'], re.I) for o in ts):
        return 'section', 'explicit_chapter_heading_and_sparse_content', .75
    if ctx['page_index'] == 1 and len(ts) <= 7 and total <= 240:
        return 'cover', 'first_page_sparse_native_title_hypothesis', .65
    if any(o['kind'] in {'chart', 'table'} for o in os):
        return 'chart', 'native_chart_or_table_present', .8
    images = [o for o in os if o['kind'] == 'image']
    if sum(area(o['bbox']) for o in images) > .55 and total < 100:
        return 'image', 'major_raster_showcase_hypothesis', .65
    if ts:
        return 'body', 'native_content_without_special_role_cue', .6
    return 'unknown', 'no_sufficient_role_evidence', 0.


def classify_document(contexts, config):
    """Geometry/hash repetition marks only small header/footer auxiliaries."""
    repeats = Counter()
    q = config['repeated_auxiliary_position_quantum']
    for ctx in contexts:
        page_keys = set()
        for o in ctx['objects']:
            b = o['bbox']
            if o.get('image_sha256') and area(b) < .04 and (b['y'] < .12 or b['y'] > .85):
                page_keys.add((o['image_sha256'], *(round(b[k]/q) for k in ('x','y','w','h'))))
        repeats.update(page_keys)
    for ctx in contexts:
        os = ctx['objects']; ts = [o for o in os if o['text_chars'] > 0]
        primary_texts = [o for o in ts if o['kind'] != 'cell']
        total_text = sum(o['text_chars'] for o in ts)
        huge_images = [o for o in os if o['kind'] == 'image' and area(o['bbox']) >= .8]
        if huge_images and total_text == 0:
            ctx['page']['content_type'] = 'image_only'
        role, source, confidence = infer_page_role(ctx)
        ctx['page'].update(role=role, role_source=source, role_confidence=confidence)
        top_fonts = [median(measured_fonts(o)) for o in primary_texts if measured_fonts(o) and o['bbox']['y'] < .3]
        largest_top = max(top_fonts, default=None)
        # A cover title may be vertically centered. A small top kicker must not
        # become the title merely because the real title is below y=.30.
        cover_fonts = [median(measured_fonts(o)) for o in primary_texts
                       if measured_fonts(o) and o['bbox']['y'] < .85]
        cover_max = max(cover_fonts, default=None)
        for o in os:
            b = o['bbox']; kind = o['kind']; text = o['text'].strip()
            o['content_class'] = 'unknown'; o['classification_source'] = 'native_geometry_heuristic_unconfirmed'
            empty_native_textbox = not text and str(o.get('shape_type','')).startswith('TEXT_BOX')
            if empty_native_textbox:
                o['content_class'] = 'decoration'; o['decoration_role'] = 'empty_text'
                o['classification_source'] = 'native_TEXT_BOX_with_no_nonempty_text'
                o['content_regions'] = []
            elif text:
                fs = measured_fonts(o); size = median(fs) if fs else None
                if kind == 'cell': tr, why = 'table_text', 'native_table_cell'
                elif b['y'] >= config['auxiliary_bottom_y'] and re.fullmatch(r'\s*[-—]?\s*\d{1,3}\s*[-—]?\s*', text):
                    tr, why = 'page_number', 'numeric_bottom_margin'
                elif b['y'] >= .87 and o['text_chars'] <= 130 and size is not None and size/ctx['page']['height_pt']*540 < 11:
                    tr, why = 'footnote', 'small_bottom_margin_text_hypothesis'
                elif role == 'cover' and b['y'] < .85 and size is not None and cover_max is not None and size >= cover_max*.92 and o['text_chars'] <= 120:
                    tr, why = 'title', 'cover_dominant_font_including_centered_title_hypothesis'
                elif role != 'cover' and b['y'] < .30 and size is not None and largest_top is not None and size >= largest_top*.92 and o['text_chars'] <= 120:
                    tr, why = 'title', 'top_dominant_font_hypothesis'
                elif kind == 'text' and role == 'chart' and o['text_chars'] <= 18 and b['y'] > .30:
                    tr, why = 'chart_label', 'short_text_near_chart_or_table_hypothesis'
                else: tr, why = 'body', 'non_auxiliary_native_text_hypothesis'
                o['text_role'], o['role_source'] = tr, why
                o['content_class'] = 'auxiliary' if tr in {'page_number','navigation','footnote'} else 'information'
                o['classification_source'] = 'nonempty_measured_text+'+why
                o['content_regions'] = text_boxes(o)
                if not o['content_regions']:
                    o['content_class'] = 'unknown'
                    o['classification_source'] = 'nonempty_text_but_actual_layout_unavailable'
            elif kind in {'table','chart'}:
                cells = [c for c in os if c.get('parent_id') == o['id'] and c['text_chars']]
                o['content_class'] = 'information' if kind == 'chart' or cells else 'unknown'
                o['classification_source'] = 'native_chart_or_nonempty_table_grid_envelope'
                o['content_regions'] = [b] if o['content_class'] == 'information' else []
            elif kind == 'image':
                key = (o.get('image_sha256'), *(round(b[k]/q) for k in ('x','y','w','h')))
                if repeats[key] >= max(3, math.ceil(len(contexts)/2)):
                    o['content_class'] = 'auxiliary'; o['decoration_role'] = 'logo'
                    o['classification_source'] = 'small_repeated_header_footer_asset_hypothesis'
                elif area(b) >= config['main_image_area_lower'] and ctx['page']['content_type'] != 'image_only':
                    o['content_class'] = 'information'
                    o['classification_source'] = 'primary_image_envelope_internal_semantics_unknown'
                o['content_regions'] = [b] if o['content_class'] == 'information' else []
            elif kind in {'line','shape'}:
                contains_text = any(area(intersection(b, p))/max(area(p),1e-12) > .95
                                    for t in ts for p in text_boxes(t) if t['id'] != o['id'])
                if area(b) >= config['background_area_lower']:
                    o['content_class'] = 'background'; o['decoration_role'] = 'background'
                    o['classification_source'] = 'large_blank_native_shape_background_hypothesis'
                elif contains_text:
                    o['content_class'] = 'decoration'; o['decoration_role'] = 'container'
                    o['classification_source'] = 'blank_shape_containing_measured_text_panel_hypothesis'
                elif min(b['w'], b['h']) < .008:
                    o['content_class'] = 'decoration'; o['decoration_role'] = 'divider'
                    o['classification_source'] = 'thin_native_line_semantics_unverified'
                # Ambiguous bars, symbols and diagrams remain unknown, not decoration truth.
                o['content_regions'] = []
            else:
                o['content_regions'] = []
            if o['kind'] in {'text','cell'} and not text:
                o['content_class'] = 'decoration'; o['decoration_role'] = 'empty_text'
                o['content_regions'] = []; o['classification_source'] = 'empty_text_no_information'
            # Physical transformed envelope for each cell, not a copied table envelope.
            if kind == 'cell':
                o['visible_envelope'] = dict(b)
                o['polygon'] = [[b['x'],b['y']],[b['x']+b['w'],b['y']],
                                [b['x']+b['w'],b['y']+b['h']],[b['x'],b['y']+b['h']]]
        ctx['document'] = {
            'page_count':len(contexts),
            'role_font_reference_pt':{},
            'role_font_reference_source':'same_document_unlabelled_measured_font_median',
            'style_reference':{},
        }
    # Role references must be formed after every page has been classified.
    by_role = defaultdict(list)
    for ctx in contexts:
        for o in ctx['objects']:
            if o['text_chars']:
                by_role[o['text_role']].extend(measured_fonts(o))
    fonts = Counter(f for ctx in contexts for o in ctx['objects'] for f in o['font_families'])
    colors = Counter(c for ctx in contexts for o in ctx['objects'] for c in o.get('explicit_colors', []))
    for ctx in contexts:
        ctx['document']['role_font_reference_pt'] = {r:median(v) for r,v in sorted(by_role.items()) if v}
        ctx['document']['style_reference'] = {'explicit_font_counts':dict(fonts),'explicit_RGB_counts':dict(colors),
                                            'source':'same_document_native_explicit_properties_not_theme_resolution'}


def png_proxy(path, shape):
    """Diagnostic color/edge mask only; cannot identify informative content."""
    h, w = shape
    with Image.open(path) as image:
        rgb = np.asarray(image.convert('RGB').resize((w,h), Image.Resampling.BOX), dtype=np.float64)
    corners = np.concatenate([rgb[:4,:4].reshape(-1,3), rgb[:4,-4:].reshape(-1,3),
                              rgb[-4:,:4].reshape(-1,3), rgb[-4:,-4:].reshape(-1,3)])
    bg = np.median(corners,axis=0)
    color_distance = np.linalg.norm(rgb-bg,axis=2)
    gray = rgb.mean(axis=2)
    edge = np.zeros(shape,dtype=float)
    edge[:,1:] = np.maximum(edge[:,1:],np.abs(np.diff(gray,axis=1)))
    edge[1:,:] = np.maximum(edge[1:,:],np.abs(np.diff(gray,axis=0)))
    return (color_distance > 30) | (edge > 18), {
        'source':'full_page_PNG_BOX_downsample_corner_color_and_edge_proxy',
        'corner_RGB_median':bg.tolist(), 'corner_color_spread':float(np.std(corners,axis=0).mean()),
        'semantic_validity':'not_information_segmentation; never_substituted_for_native_mask',
    }


def object_relations(ctx):
    # Parent table grids would double-count their own cells. Cells are one table module.
    entries = []
    for o in ctx['objects']:
        if o['content_class'] != 'information' or o['kind'] == 'cell': continue
        b = bounds(o.get('content_regions', []))
        if b: entries.append((o['id'], b))
    nearest, overlaps = [], []
    for i,(aid,a) in enumerate(entries):
        local=[]
        for j,(bid,b) in enumerate(entries):
            if i==j: continue
            dx=max(0.,a['x']-b['x']-b['w'],b['x']-a['x']-a['w'])
            dy=max(0.,a['y']-b['y']-b['h'],b['y']-a['y']-a['h'])
            local.append(dict(a=aid,b=bid,gap=math.hypot(dx,dy),dx=dx,dy=dy))
            if j>i:
                inter=area(intersection(a,b))
                if inter>0: overlaps.append(dict(a=aid,b=bid,area=inter,cover_a=inter/area(a),cover_b=inter/area(b)))
        if local: nearest.append(min(local,key=lambda d:(d['gap'],d['b'])))
    ctx['relations']={'nearest':nearest,'overlaps':overlaps,
                      'source':'actual_nonempty_paragraph_envelopes_or_primary_object_frames',
                      'coordinate_metric':'unit_square_normalized_rect_edge_euclidean_not_physical_distance'}
    gaps=[n['gap'] for n in nearest]
    return dict(count=len(gaps),minimum=min(gaps) if gaps else None,
                median=median(gaps) if gaps else None,
                p10=float(np.quantile(gaps,.1)) if gaps else None)


def build_spatial(ctx, config):
    shape=(config['mask_height'],config['mask_width'])
    content=np.zeros(shape,dtype=bool);unknown=np.zeros(shape,dtype=bool)
    items=[]
    for o in ctx['objects']:
        regions=o.get('content_regions',[])
        if o['content_class']=='information':
            for b in regions: add_rect(content,b)
            if o['kind']!='cell' and regions:
                items.append((o,bounds(regions)))
        elif o['content_class']=='unknown':
            add_rect(unknown,o['visible_envelope'])
    unknown &= ~content
    png,proxy_meta=png_proxy(ctx['page']['png_path'],shape)
    ys,xs=np.nonzero(content)
    content_bounds=dict(x=float(xs.min()/shape[1]),y=float(ys.min()/shape[0]),
                        w=float((xs.max()-xs.min()+1)/shape[1]),h=float((ys.max()-ys.min()+1)/shape[0])) if len(xs) else None
    centroid=[float((xs.mean()+.5)/shape[1]),float((ys.mean()+.5)/shape[0])] if len(xs) else None
    multi=[]
    for scale in [1]+config['spatial_scales']:
        size=1/scale;steps=2*(scale-1)
        for j in range(steps+1):
            for i in range(steps+1):
                window=dict(x=i*size*.5,y=j*size*.5,w=size,h=size)
                selected=[o for o,b in items if area(intersection(b,window))/max(area(b),1e-12)>=.5]
                x0,y0,x1,y1=rect_pixels(window,shape)
                multi.append(dict(bbox=window,scale=scale,occupancy=float(content[y0:y1,x0:x1].mean()),
                    information_count=len(selected),text_count=sum(o['kind']=='text' for o in selected),
                    short_text_count=sum(o['kind']=='text' and o['text_chars']<=32 for o in selected),
                    object_ids=[o['id'] for o in selected]))
    ctx['spatial']={
        'content_mask':content,'unknown_mask':unknown,'png_foreground_mask':png,
        'content_occupancy':float(content.mean()),'content_bounds':content_bounds,'content_centroid':centroid,
        'unknown_fraction':float(unknown.mean()),'multiscale':multi,
        'nearest_gap_stats':object_relations(ctx),
        'mask_source':'actual_COM_nonempty_paragraph_envelopes+native_primary_object_envelopes; heuristic_classification',
        'mask_grid_error_bound_normalized':dict(x=1/shape[1],y=1/shape[0]),
        'foreground_proxy':proxy_meta,
    }
    ctx['warnings'] += [
        'Page/text role and information/decorative class are engineering hypotheses, not human intent.',
        'Paragraph rectangles include intra-line whitespace; no foreground glyph mask was measured.',
        'Primary raster object envelopes do not segment semantics inside the image.',
        'Unclassified native shape envelopes remain unknown and can block D5.',
    ]
    ctx['availability']['semantic_content_mask']='partial_heuristic'


def merge_line_measurements(contexts, overlay):
    if not overlay: return dict(objects=0,lines=0)
    by_id={c['sample_id']:c for c in contexts};used=lines=0
    for row in overlay.get('objects',[]):
        sid=row['sample_id'];ctx=by_id[sid]
        source=row.get('source_sha256') or row.get('pptx_sha256')
        if source is not None and source!=ctx['source']['pptx_sha256']:
            raise ValueError('COM line source SHA mismatch '+sid)
        obj=next(o for o in ctx['objects'] if o['id']==row['object_id'])
        index=int(row['paragraph_index'])
        para=next(p for p in obj['paragraphs'] if p['id'].endswith('-p'+str(index)))
        measured=row.get('measurement',{});actual=measured.get('lines',[])
        if actual:
            para['line_boxes']=[v['bbox'] for v in actual]
            para['line_measurement_source']='fresh_readonly_PowerPoint_TextRange2_Lines'
            obj['available']['line_boxes']=measured.get('status','partial')
            ctx['availability']['line_boxes']='targeted_partial'
            used+=1;lines+=len(actual)
    return dict(objects=used,lines=lines)


def apply_coordinate_contract(contexts, audit):
    """Translate only explicitly live-audited cells; defer the rest safely.

    The raw K cache is immutable. Two live cells establish that this host can
    return cell-local paragraph coordinates. That does not prove every cell or
    rotated/grouped layout has the same convention, so no global guess is made.
    """
    if audit.get('status') != 'completed' or audit.get('source_sha_unchanged') is not True:
        raise ValueError('Live cell coordinate audit is incomplete')
    if audit.get('source_sha256_before') != audit.get('source_sha256_after'):
        raise ValueError('Live cell source changed')
    verified={r['object_id']:r for r in audit['cells'] if r.get('status')=='measured'
              and r.get('raw_matches_cached_paragraph') and r.get('frame_matches_cached')
              and r.get('translated_within_global_cell')}
    repaired=deferred=0
    for ctx in contexts:
        for obj in ctx['objects']:
            if obj['kind'] != 'cell':
                for p in obj.get('paragraphs',[]):p['coordinate_space']='slide_COM_native_text'
                continue
            proof=verified.get(obj['id']) if ctx['sample_id']==audit['sample_id'] else None
            known=[];unresolved=[]
            for p in obj.get('paragraphs',[]):
                live=proof if proof and p['id'].endswith('-p'+str(proof['paragraph_index'])) else None
                if live:
                    if ctx['source']['pptx_sha256'] != audit['source_sha256_before']:
                        raise ValueError('Cell coordinate source SHA mismatch')
                    w,h=ctx['page']['width_pt'],ctx['page']['height_pt']
                    for k,scale in [('x',w),('y',h),('w',w),('h',h)]:
                        if abs(obj['bbox'][k]*scale-live['cell_frame_bbox_pt'][k]) > 1e-4:
                            raise ValueError('Cell global frame/proof mismatch')
                        if abs(p['bbox'][k]*scale-live['cached_paragraph_bbox_pt'][k]) > 1e-4:
                            raise ValueError('Cell coordinate cache/proof mismatch')
                    p['local_bbox_before_translation']=dict(p['bbox'])
                    p['bbox']={**p['bbox'],'x':p['bbox']['x']+obj['bbox']['x'],
                               'y':p['bbox']['y']+obj['bbox']['y']}
                    p['coordinate_space']='slide_from_live_verified_cell_local_translation'
                    p['coordinate_proof']='cell_coordinate_audit.json:'+obj['id']
                    p['measurement_source']='verified_K_COM_cell_local+fresh_live_COM_frame_origin'
                    known.append(p);repaired+=1
                else:
                    p['coordinate_space']='cell_local_or_unverified_not_slide_geometry'
                    p['unavailable_reason']='native_table_TextRange2_coordinates_not_verified_for_this_cell'
                    unresolved.append(p);deferred+=1
            obj['paragraphs']=known;obj['unresolved_paragraphs']=unresolved
            obj['available']['paragraph_layout']='partially_verified_coordinate_space' if known else 'coordinate_unverified'
            obj['available']['page_text_geometry']='available' if known and not unresolved else 'unknown'
            if unresolved:
                ctx['warnings'].append('Unverified cell-local paragraph geometry deferred: '+obj['id'])
    return {'live_verified_translated_paragraphs':repaired,'deferred_cell_paragraphs':deferred,
            'unknown_coordinates_not_used_as_slide_geometry':True}


def build_contexts(contexts, config, overlay=None, coordinate_audit=None):
    coordinate_stats=apply_coordinate_contract(contexts,coordinate_audit) if coordinate_audit else None
    documents=defaultdict(list)
    for ctx in contexts: documents[ctx['document_id']].append(ctx)
    for pages in documents.values():
        classify_document(pages,config)
    overlay_stats=merge_line_measurements(contexts,overlay)
    if coordinate_stats:overlay_stats['coordinate_guard']=coordinate_stats
    for ctx in contexts: build_spatial(ctx,config)
    return contexts,overlay_stats
