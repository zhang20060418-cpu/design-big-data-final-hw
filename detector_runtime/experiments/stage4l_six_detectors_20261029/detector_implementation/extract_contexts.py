"""Native/OpenXML ingestion once per document; reuse verified K COM measurements.

Run in the existing backend environment (python-pptx/lxml/Pillow/COM). No numpy,
labels, template names, model scores or original G2 data enter this stage.
"""
from collections import Counter
import csv
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

EXP=Path(__file__).resolve().parents[1]
ML=EXP.parents[1]
PROJECT=ML.parent
DATA=ML/'benchmark_constructed_20261029'
K=EXP.parent/'stage4k_constructed_diagnostics_20261029'
J=EXP.parent/'stage4j_measurement_debug_20261029'
RESULTS=EXP/'results'
NS='http://schemas.openxmlformats.org/drawingml/2006/main'

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()
def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
    sys.modules[name]=m;spec.loader.exec_module(m);return m
def norm(b,w,h):return dict(x=b['x']/w,y=b['y']/h,w=b['w']/w,h=b['h']/h)
def frame(shape):return dict(x=float(shape.left)/12700,y=float(shape.top)/12700,w=float(shape.width)/12700,h=float(shape.height)/12700)

def colors_from_xml(el):
    # Explicit RGB only. Theme resolution is not fabricated from a scheme name.
    return sorted({'#'+n.get('val').upper() for n in el.iter('{'+NS+'}srgbClr') if len(n.get('val',''))==6})

def explicit_style(text_frame):
    families=set();sizes=[];unknown_family=False
    for p in text_frame.paragraphs:
        for r in p.runs:
            if not r.text.strip():continue
            name=r.font.name or p.font.name
            if name:families.add(name)
            else:unknown_family=True
            value=r.font.size or p.font.size
            if value is not None:sizes.append(float(value.pt))
    return sorted(families),sorted(set(sizes)),unknown_family

def rotated_envelope(box,angle,page_w,page_h):
    # Rotate in physical pt, then normalize: normalized x/y have unequal scales.
    x,y,w,h=[box[k] for k in ('x','y','w','h')]
    cx=(x+w/2)*page_w;cy=(y+h/2)*page_h;t=math.radians(angle)
    points=[]
    for a,b in [(x,y),(x+w,y),(x+w,y+h),(x,y+h)]:
        dx=a*page_w-cx;dy=b*page_h-cy
        points.append([(cx+dx*math.cos(t)-dy*math.sin(t))/page_w,
                       (cy+dx*math.sin(t)+dy*math.cos(t))/page_h])
    xs=[p[0] for p in points];ys=[p[1] for p in points]
    return points,dict(x=min(xs),y=min(ys),w=max(xs)-min(xs),h=max(ys)-min(ys))

def object_from_shape(shape,idx,order,w,h,measured,region,j,group_path=()):
    sid=f's{idx:04d}-shapeid{shape.shape_id}'
    is_table=bool(getattr(shape,'has_table',False));is_chart=bool(getattr(shape,'has_chart',False))
    kind='table' if is_table else ('chart' if is_chart else ('image' if int(shape.shape_type)==13 else
          ('group' if int(shape.shape_type)==6 else ('line' if int(shape.shape_type)==9 else 'shape'))))
    has_text=bool(getattr(shape,'has_text_frame',False))
    text=shape.text_frame.text if has_text else ''
    if text.strip():kind='text'
    actual=measured.get(sid)
    b=actual['frame_bbox_pt'] if actual else (region.get(sid,{}).get('bbox_pt') or frame(shape))
    b=norm(b,w,h)
    fonts,sizes,unknown_family=explicit_style(shape.text_frame) if has_text else ([],[],False)
    paragraphs=[]
    if actual:
        sizes=sorted(set(sizes+actual['effective_font_sizes_pt']))
        for p in actual['paragraphs']:
            paragraphs.append(dict(id=sid+f'-p{p["index"]}',bbox=norm(p['bbox_pt'],w,h),
                font_pt=p['effective_font_pt'],text_chars=p['text_chars'],line_boxes=None,
                measurement_source='verified_K_PowerPoint_nonempty_paragraph_layout'))
        raw_wrap=actual['effective_wrap_raw'];wrap=True if raw_wrap==-1 else False if raw_wrap==0 else None
    else:wrap=shape.text_frame.word_wrap if has_text else None
    opacity=region.get(sid,{}).get('opacity')
    if opacity is None:opacity=j.opacity_from_sppr(getattr(shape._element,'spPr',None))
    opacity={**opacity,'state':opacity['state'] if opacity['state'] in {'fully_opaque','transparent'} else 'partial_or_unknown',
             'source':'verified_K_rect_region_alpha_or_J_local_fill'}
    sppr=getattr(shape._element,'spPr',None)
    geom=sppr.find('{'+NS+'}prstGeom') if sppr is not None else None
    rotation=float(shape.rotation)
    polygon,envelope=rotated_envelope(b,rotation,w,h)
    o={'id':sid,'parent_id':None,'kind':kind,'bbox':b,'rotation':rotation,'z_order':order,
       'text':text,'text_chars':len(''.join(text.split())), 'font_sizes_pt':sizes,'font_families':fonts,
       'font_source':'verified_COM_and_explicit_runs' if actual else 'explicit_runs_or_unresolved',
       'word_wrap':wrap,'auto_size':str(shape.text_frame.auto_size) if has_text else None,
       'text_role':'unknown','role_source':'not_inferred_yet','paragraphs':paragraphs,
       'opacity':opacity,'fill_rgb':None,'explicit_colors':colors_from_xml(shape._element),
       'image_sha256':None,'image_format':None,'image_crop':None,
       'content_class':'unknown','classification_source':'not_inferred_yet','decoration_role':None,
       'shape_type':str(shape.shape_type),'geometry_type':geom.get('prst') if geom is not None else None,
       'group_path':list(group_path),'cell':None,'is_placeholder':bool(shape.is_placeholder),
       'visible_envelope':envelope,'polygon':polygon,
       'available':{'native':'available','paragraph_layout':'available' if actual else 'not_measured',
                    'font_family':'partial' if unknown_family else ('available' if fonts else 'unknown'),
                    'line_boxes':'not_cached','pixel_glyphs':'not_implemented'},
       'measurement_source':'native_OpenXML_once_plus_verified_COM_cache' if actual else 'native_OpenXML_once',
       'rendered_text_height_px':None,'module_id':None}
    solid=sppr.find('{'+NS+'}solidFill') if sppr is not None else None
    if solid is not None:
        rgb=solid.find('{'+NS+'}srgbClr')
        if rgb is not None:o['fill_rgb']='#'+rgb.get('val','').upper()
    if kind=='image':
        import io
        from PIL import Image
        blob=shape.image.blob;o['image_sha256']=hashlib.sha256(blob).hexdigest()
        try:
            with Image.open(io.BytesIO(blob)) as im:o['image_format']=im.format
        except Exception:o['image_format']='unsupported_embedded_format'
        o['image_crop']={name:float(getattr(shape,'crop_'+name)) for name in ['left','right','top','bottom']}
    if has_text:
        body=shape.text_frame._txBody.bodyPr
        normfit=body.find('{'+NS+'}normAutofit')
        o['font_scale_attr']=None if normfit is None else normfit.get('fontScale')
    return o

def extract():
    from pptx import Presentation
    start=time.perf_counter();RESULTS.mkdir(exist_ok=True)
    guard=json.loads((RESULTS/'preflight_integrity.json').read_text(encoding='utf-8'))
    assert guard['status']=='passed'
    # Validate K cached measurements against K's immutable delivery ledger.
    with (K/'outputs/artifact_sha256.csv').open(encoding='utf-8-sig',newline='') as f:
        ledger={r['path'].replace('\\','/'):r['sha256'] for r in csv.DictReader(f)}
    for name in ['native_measurements.json','native_run_summary.json','frozen_visual_index.csv']:
        assert sha(K/'outputs'/name)==ledger['outputs/'+name]
    cached=json.loads((K/'outputs/native_measurements.json').read_text(encoding='utf-8'))
    cache={r['sample_id']:r for r in cached}
    with (K/'outputs/frozen_visual_index.csv').open(encoding='utf-8-sig',newline='') as f:kindex={r['sample_id']:r for r in csv.DictReader(f)}
    keys=['sample_id','document_id','group_id','original_slide_index','pptx_path','png_path','pptx_sha256','png_sha256']
    with (DATA/'manifest_all.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=sorted([{k:r[k] for k in keys} for r in csv.DictReader(f)],key=lambda r:r['sample_id'])
    assert len(rows)==130 and {r['document_id'] for r in rows}=={f'C{i:02d}' for i in range(1,12)}
    for row in rows:
        assert row['pptx_sha256']==kindex[row['sample_id']]['pptx_sha256'] and row['png_sha256']==kindex[row['sample_id']]['png_sha256']
        for key in ['pptx','png']:
            path=(DATA/row[key+'_path']).resolve()
            assert path.is_relative_to(DATA.resolve()) and sha(path)==row[key+'_sha256']
    j=load_module('l_reuse_J',J/'scripts/measurement_adapter.py')
    contexts=[];parse_calls=0
    for doc in sorted({r['document_id'] for r in rows}):
        selected=[r for r in rows if r['document_id']==doc]
        source=DATA/selected[0]['pptx_path'];prs=Presentation(str(source));parse_calls+=1
        for row in selected:
            idx=int(row['original_slide_index']);c=cache[row['sample_id']];w,h=c['page_width_pt'],c['page_height_pt']
            measured={o['object_id']:o for o in c['text_objects']};regions={o['object_id']:o for o in c['opaque_regions']}
            slide=prs.slides[idx-1];objects=[]
            for order,shape in enumerate(slide.shapes):
                o=object_from_shape(shape,idx,order,w,h,measured,regions,j);objects.append(o)
                if getattr(shape,'has_table',False):
                    for r,tablerow in enumerate(shape.table.rows,1):
                        for col,cell in enumerate(tablerow.cells,1):
                            sid=o['id']+f'-r{r}c{col}';actual=measured.get(sid)
                            if not cell.text.strip():continue
                            fonts,sizes,uf=explicit_style(cell.text_frame)
                            paras=[] if actual is None else [dict(id=sid+f'-p{p["index"]}',bbox=norm(p['bbox_pt'],w,h),
                                font_pt=p['effective_font_pt'],text_chars=p['text_chars'],line_boxes=None,
                                measurement_source='verified_K_COM_table_cell') for p in actual['paragraphs']]
                            sizes=sorted(set(sizes+(actual['effective_font_sizes_pt'] if actual else [])))
                            child={**o,'id':sid,'parent_id':o['id'],'kind':'cell','cell':{'row':r,'column':col},
                                'text':cell.text,'text_chars':len(''.join(cell.text.split())),
                                'bbox':norm(actual['frame_bbox_pt'],w,h) if actual else o['bbox'],
                                'paragraphs':paras,'font_families':fonts,'font_sizes_pt':sizes,
                                'text_role':'table_text','role_source':'native_table_cell','opacity':{'state':'partial_or_unknown','source':'cell_fill_not_resolved'},
                                'fill_rgb':None,'explicit_colors':colors_from_xml(cell._tc),'image_sha256':None,
                                'available':{**o['available'],'paragraph_layout':'available' if actual else 'not_measured'}}
                            objects.append(child)
            from PIL import Image
            with Image.open(DATA/row['png_path']) as im:pw,ph=im.size
            for o in objects:
                if o['paragraphs']:o['rendered_text_height_px']=min(p['bbox']['h']*ph for p in o['paragraphs'])
            ctx={'schema_version':'slide-feature-context-v1','sample_id':row['sample_id'],'document_id':doc,'page_index':idx,
                'page':{'width_pt':w,'height_pt':h,'aspect':w/h,'role':'unknown','role_source':'not_inferred_yet',
                    'role_confidence':0.,'content_type':'mixed' if any(o['kind']=='image' for o in objects) else 'native_editable',
                    'png_path':str((DATA/row['png_path']).resolve()),'png_sha256':row['png_sha256'],'png_width':pw,'png_height':ph},
                'source':{'pptx_path':str(source.resolve()),'pptx_sha256':row['pptx_sha256'],'COM_cache_sha256':sha(K/'outputs/native_measurements.json')},
                'objects':objects,'spatial':{},'relations':{},'document':{},
                'availability':{'native':'available','COM':'cached_verified','pixel_glyph_masks':'not_implemented',
                    'line_boxes':'not_cached','ocr':'not_installed'},'warnings':[]}
            contexts.append(ctx)
        assert sha(source)==selected[0]['pptx_sha256']
    contexts.sort(key=lambda r:r['sample_id'])
    target=RESULTS/'raw_contexts.json'
    target.write_text(json.dumps(contexts,ensure_ascii=False,indent=1,allow_nan=False),encoding='utf-8')
    metadata={'status':'complete','created_at':datetime.now().astimezone().isoformat(),'pages':130,'documents':11,
        'PPTX_parse_calls':parse_calls,'COM_cache_pages_reused':130,'new_COM_calls':0,'new_CLIP_forwards':0,
        'objects':sum(len(c['objects']) for c in contexts),'table_cells':sum(o['kind']=='cell' for c in contexts for o in c['objects']),
        'input_label_columns_projected_out':True,'raw_context_sha256':sha(target),'elapsed_seconds':time.perf_counter()-start,
        'J_source_sha256':sha(J/'scripts/measurement_adapter.py'),'K_cache_sha256':sha(K/'outputs/native_measurements.json')}
    (RESULTS/'context_extraction.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(metadata,ensure_ascii=False))

if __name__=='__main__':extract()
