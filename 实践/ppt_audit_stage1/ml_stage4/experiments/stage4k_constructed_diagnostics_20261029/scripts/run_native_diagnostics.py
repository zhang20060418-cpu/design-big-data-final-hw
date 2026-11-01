"""Fixed R0/RJ/RM native candidate diagnostics; no labels/scores in algorithms.

Input projections retain only identity, path and artifact SHA columns. COM is
read-only and no PPTX or PNG is rendered/saved. Results are candidates, not truth.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import sys
import time
import traceback
from types import SimpleNamespace

EXP=Path(__file__).resolve().parents[1]
ML=EXP.parents[1]
PROJECT=ML.parent
DATA=ML/'benchmark_constructed_20261029'
OUT=EXP/'outputs'
LABELS=['improper_font_sizing','occluded_content','cluttered_layout','content_overflow_cutoff']
J=ML/'experiments/stage4j_measurement_debug_20261029/scripts/measurement_adapter.py'
NS='http://schemas.openxmlformats.org/drawingml/2006/main'


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(name,value):
    OUT.mkdir(exist_ok=True)
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def csv_save(name,rows):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def load_j():
    spec=importlib.util.spec_from_file_location('k_readonly_j_measurement_adapter',J)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module
def rect(x,y,w,h):return dict(x=float(x),y=float(y),w=float(w),h=float(h))
def area(b):return max(0.,b['w'])*max(0.,b['h'])
def intersection(a,b):return max(0.,min(a['x']+a['w'],b['x']+b['w'])-max(a['x'],b['x']))*max(0.,min(a['y']+a['h'],b['y']+b['h'])-max(a['y'],b['y']))
def overflow(b,width,height):return max(0.,-b['x'],-b['y'],b['x']+b['w']-width,b['y']+b['h']-height)
def characters(text):return len(re.sub(r'\s+','',text))


def picture_opacity(shape):
    """A cropped opaque source remains opaque only for a validated rect crop.

    Do not decide from filename, shape name or constructed modification type.
    Unsupported transforms/effects retain unknown rather than transparent.
    """
    from PIL import Image
    result=dict(state='unknown',kind='embedded_picture',reasons=[],image_sha256=None)
    try:
        if abs(float(shape.rotation)%360)>1e-6:
            result['reasons'].append('picture_rotation_unresolved');return result
        sppr=shape._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}spPr')
        geom=sppr.find('{'+NS+'}prstGeom') if sppr is not None else None
        if geom is None or geom.get('prst')!='rect':
            result['reasons'].append('picture_geometry_not_proven_rectangle');return result
        nodes=list(shape._element.iter())
        alpha=[n.tag.rsplit('}',1)[-1] for n in nodes if 'alpha' in n.tag.rsplit('}',1)[-1].lower()]
        if alpha:
            result['reasons'].append('picture_alpha_effect_not_resolved');result['alpha_nodes']=alpha;return result
        if any(n.tag.rsplit('}',1)[-1] in {'effectDag','effectLst','softEdge','blur','scene3d','sp3d'} and len(n) for n in nodes):
            result['reasons'].append('picture_effect_unresolved');return result
        crops=[float(getattr(shape,'crop_'+side)) for side in ('left','top','right','bottom')]
        if not all(0<=v<1 for v in crops) or crops[0]+crops[2]>=1 or crops[1]+crops[3]>=1:
            result['reasons'].append('picture_crop_extends_outside_source');return result
        fill=shape._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}blipFill')
        stretch=fill.find('{'+NS+'}stretch') if fill is not None else None
        fillrect=stretch.find('{'+NS+'}fillRect') if stretch is not None else None
        if fillrect is None or any(float(v)!=0 for v in fillrect.attrib.values()):
            result['reasons'].append('picture_fill_geometry_unresolved');return result
        raw=shape.image.blob;result['image_sha256']=hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw)) as image:
            image.load();result['format']=image.format;result['mode']=image.mode;result['crop_fractions']=crops
            if 'A' in image.getbands():
                extreme=image.getchannel('A').getextrema();result['alpha_extrema']=list(extreme)
                if extreme==(255,255):result['state']='fully_opaque'
                elif extreme==(0,0):result['state']='transparent'
                else:result['reasons'].append('picture_nonuniform_or_partial_alpha')
            elif 'transparency' in image.info:
                result['reasons'].append('picture_transparency_metadata_unresolved')
            else:result['state']='fully_opaque'
        return result
    except Exception as exc:
        result['reasons'].append('picture_measurement_error:'+type(exc).__name__);return result


def native_opaque_region(shape,j):
    if int(shape.shape_type)==13:return picture_opacity(shape)
    result=j.opacity_from_sppr(getattr(shape._element,'spPr',None))
    if result['state']!='fully_opaque':return dict(**result,kind='native_fill')
    sppr=getattr(shape._element,'spPr',None)
    geom=sppr.find('{'+NS+'}prstGeom') if sppr is not None else None
    if geom is None or geom.get('prst')!='rect' or abs(float(shape.rotation)%360)>1e-6:
        return dict(state='unknown',kind='native_fill',reasons=['nonrectangular_or_rotated_fill_not_proven_bbox_opaque'],local_fill=result)
    return dict(**result,kind='native_fill')


def font_candidate(text,known_sizes,frame,height,placeholder=False):
    if characters(text)<15 or not known_sizes or placeholder:return False
    if frame['y']+frame['h']>height*.93:return False
    if re.fullmatch(r'[\d\s/.\-–—]+',text.strip()):return False
    return min(known_sizes)<9.


def visible_layout_cutoff(paragraphs,width,height):
    return [dict(paragraph_index=p['index'],overflow_pt=overflow(p['bbox_pt'],width,height),bbox_pt=p['bbox_pt'])
            for p in paragraphs if overflow(p['bbox_pt'],width,height)>.04*72]


def overlap_hits(paragraphs,cover,page_area):
    if cover['opacity']['state']!='fully_opaque' or area(cover['bbox_pt'])<page_area*.03:return []
    hits=[]
    for p in paragraphs:
        ratio=intersection(p['bbox_pt'],cover['bbox_pt'])/max(1e-6,area(p['bbox_pt']))
        if ratio>=.45:hits.append(dict(paragraph_index=p['index'],coverage_ratio=ratio,paragraph_bbox_pt=p['bbox_pt']))
    return hits


def measure_text(com_shape,scope_id,order,frame,placeholder=False,cell=None):
    tr=com_shape.TextFrame2.TextRange
    text=str(tr.Text)
    if not text.strip():return None
    para=[];unknown=[]
    for k in range(1,int(tr.Paragraphs.Count)+1):
        p=tr.Paragraphs.Item(k);ptext=str(p.Text)
        if not ptext.strip():continue
        try:
            box=rect(*[getattr(p,name) for name in ('BoundLeft','BoundTop','BoundWidth','BoundHeight')])
            if not all(math.isfinite(v) for v in box.values()) or box['w']<0 or box['h']<0:raise ValueError('invalid bounds')
            size=float(p.Font.Size)
            if size<=0 or not math.isfinite(size):
                unknown.append(dict(kind='effective_font',paragraph_index=k,reason='mixed_or_unresolved_COM_font_size',raw_size=size));size=None
            para.append(dict(index=k,text_chars=characters(ptext),bbox_pt=box,effective_font_pt=size))
        except Exception as exc:unknown.append(dict(kind='paragraph_layout',paragraph_index=k,error=repr(exc)))
    sizes=[p['effective_font_pt'] for p in para if p['effective_font_pt'] is not None]
    if not para:unknown.append(dict(kind='paragraph_layout',reason='no_measurable_nonempty_paragraphs'))
    return dict(object_id=scope_id,z_order=order,cell=cell,frame_bbox_pt=frame,text_chars=characters(text),
                text=text,placeholder=placeholder,paragraphs=para,effective_font_sizes_pt=sorted(set(sizes)),
                effective_wrap_raw=int(com_shape.TextFrame2.WordWrap),unresolved=unknown)


def get_com_measurements(app,path,python_prs,j,previous_security=None):
    presentation=None;slides=[];failures=[]
    try:
        presentation=app.Presentations.Open(str(path.resolve()),-1,0,0)
        width,height=float(presentation.PageSetup.SlideWidth),float(presentation.PageSetup.SlideHeight)
        if int(presentation.Slides.Count)!=len(python_prs.slides):raise ValueError('COM/PPTX page mismatch')
        for index,py_slide in enumerate(python_prs.slides,1):
            cs=presentation.Slides.Item(index);texts=[];regions=[];pending=[]
            byid={int(cs.Shapes.Item(i).Id):cs.Shapes.Item(i) for i in range(1,int(cs.Shapes.Count)+1)}
            for order,shape in enumerate(py_slide.shapes):
                sid=f's{index:04d}-shapeid{shape.shape_id}'
                if int(shape.shape_type)==6 or abs(float(shape.rotation)%360)>1e-6:
                    pending.append(dict(object_id=sid,label_scope=LABELS,kind='unsupported_geometry',reason='group_or_rotation_not_resolved'))
                    continue
                com=byid.get(shape.shape_id)
                if com is None:
                    pending.append(dict(object_id=sid,label_scope=LABELS,kind='unmatched_native_object',reason='COM_shape_id_not_found'));continue
                frame=rect(com.Left,com.Top,com.Width,com.Height)
                opacity=native_opaque_region(shape,j)
                regions.append(dict(object_id=sid,z_order=order,bbox_pt=frame,opacity=opacity))
                try:
                    if getattr(shape,'has_table',False):
                        table=com.Table
                        for row in range(1,int(table.Rows.Count)+1):
                            for col in range(1,int(table.Columns.Count)+1):
                                cshape=table.Cell(row,col).Shape
                                cframe=rect(cshape.Left,cshape.Top,cshape.Width,cshape.Height)
                                measured=measure_text(cshape,sid+f'-r{row}c{col}',order,cframe,cell=dict(row=row,column=col,parent_object_id=sid))
                                if measured:texts.append(measured)
                    elif getattr(shape,'has_text_frame',False) and shape.text_frame.text.strip():
                        measured=measure_text(com,sid,order,frame,bool(shape.is_placeholder))
                        if measured:texts.append(measured)
                except Exception as exc:
                    pending.append(dict(object_id=sid,label_scope=[LABELS[0],LABELS[1],LABELS[3]],kind='COM_object_measurement_failed',reason=repr(exc)))
            slides.append(dict(slide_index=index,page_width_pt=width,page_height_pt=height,text_objects=texts,opaque_regions=regions,pending=pending))
    except Exception as exc:
        failures.append(dict(pptx=str(path),error=repr(exc),traceback=traceback.format_exc()))
    finally:
        # PowerPoint may terminate its automation server when the last deck is
        # closed. Restore application state while this deck is still open.
        if previous_security is not None:
            try:app.AutomationSecurity=previous_security
            except Exception as exc:failures.append(dict(stage='restore_security',error=repr(exc)))
        if presentation is not None:
            try:presentation.Close()
            except Exception as exc:failures.append(dict(stage='close_readonly_deck',error=repr(exc)))
    return slides,failures


def rm_candidates(measured,rj_issues):
    result=[];pending=list(measured['pending'])
    w,h=measured['page_width_pt'],measured['page_height_pt'];page=w*h
    def candidate(label,rule,ids,kind,details):
        result.append(dict(label=label,rule_id=rule,object_ids=ids,measurement_kind=kind,status='candidate',details=details))
    for obj in measured['text_objects']:
        for u in obj['unresolved']:
            pending.append(dict(object_id=obj['object_id'],label_scope=[LABELS[0]] if u['kind']=='effective_font' else [LABELS[1],LABELS[3]],kind=u['kind'],reason=u))
        frame_in={k:v/72 for k,v in obj['frame_bbox_pt'].items()}
        if font_candidate(obj['text'],obj['effective_font_sizes_pt'],frame_in,h/72,obj['placeholder']):
            candidate(LABELS[0],'RM03',[obj['object_id']],'effective_font',dict(min_effective_pt=min(obj['effective_font_sizes_pt']),characters=obj['text_chars'],cell=obj['cell']))
        cut=visible_layout_cutoff(obj['paragraphs'],w,h)
        if cut:candidate(LABELS[3],'RM02',[obj['object_id']],'COM_nonempty_paragraph_page_overflow',dict(paragraphs=cut,pixel_clipping_confirmed=False))
        for cover in measured['opaque_regions']:
            if cover['z_order']<=obj['z_order']:continue
            hits=overlap_hits(obj['paragraphs'],cover,page)
            if hits:
                candidate(LABELS[1],'RM05',[obj['object_id'],cover['object_id']],'opaque_region_over_paragraph_layout',dict(hits=hits,opacity=cover['opacity'],pixel_unreadability_confirmed=False))
            elif cover['opacity']['state'] not in ('fully_opaque','transparent') and area(cover['bbox_pt'])>=page*.03:
                hypothetical={**cover,'opacity':{'state':'fully_opaque'}}
                if overlap_hits(obj['paragraphs'],hypothetical,page):
                    pending.append(dict(object_id=obj['object_id'],cover_object_id=cover['object_id'],label_scope=[LABELS[1]],kind='occluding_region_opacity_or_geometry_unknown',reason=cover['opacity']))
    for issue in rj_issues:
        if issue['rule_id']=='R04':candidate(LABELS[2],'R04',[],'unchanged_RJ_count_density',issue)
    # A text foreground is not measured by the opacity of its frame fill.
    # Preserve intersecting layout envelopes as pending, never as a clean page.
    for i,a in enumerate(measured['text_objects']):
        for b in measured['text_objects'][i+1:]:
            if a['z_order']==b['z_order']:
                continue  # Different cells of one table are not layer occluders.
            if any(intersection(p['bbox_pt'],q['bbox_pt'])>0 for p in a['paragraphs'] for q in b['paragraphs']):
                pending.append(dict(object_id=a['object_id'],cover_object_id=b['object_id'],
                                    label_scope=[LABELS[1]],kind='text_foreground_layout_intersection',
                                    reason='Paragraph rectangles intersect; exact ink overlap/readability not measured'))
    return result,pending


def fixture():
    from lxml import etree
    from PIL import Image
    j=load_j();checks=[]
    def check(name,value):checks.append(dict(name=name,passed=bool(value)))
    check('short table cell does not force font candidate',not font_candidate('short',[6.],rect(1,1,2,1),7.5))
    check('long local low font preserves9pt rule',font_candidate('fifteen or more characters',[8.],rect(1,1,2,1),7.5))
    check('9pt is not less than9',not font_candidate('fifteen or more characters',[9.],rect(1,1,2,1),7.5))
    check('footer remains excluded',not font_candidate('fifteen or more characters',[6.],rect(1,7,2,.4),7.5))
    check('placeholder remains excluded',not font_candidate('fifteen or more characters',[6.],rect(1,1,2,1),7.5,True))
    p=[dict(index=1,bbox_pt=rect(-2.88,1,100,10))]
    check('cutoff0.04in equality no alert',not visible_layout_cutoff(p,720,540))
    p[0]['bbox_pt']['x']=-2.881
    check('cutoff exceeds unchanged tolerance',len(visible_layout_cutoff(p,720,540))==1)
    pp=[dict(index=1,bbox_pt=rect(0,0,100,100))]
    cover=dict(object_id='cover',bbox_pt=rect(0,0,45,100),opacity={'state':'fully_opaque'})
    check('45percent overlap boundary retained',len(overlap_hits(pp,cover,100000))==1)
    check('3percent page boundary retained',len(overlap_hits(pp,cover,150000))==1)
    check('below3percent not forced candidate',not overlap_hits(pp,cover,150001))
    check('unknown opacity never supported cover',not overlap_hits(pp,{**cover,'opacity':{'state':'unknown'}},100000))
    xml=etree.fromstring(f'<p:pic xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{NS}"><p:blipFill><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:prstGeom prst="rect"/></p:spPr></p:pic>'.encode())
    def pic(mode,color,crop=0.):
        b=io.BytesIO();Image.new(mode,(4,4),color).save(b,format='PNG')
        return SimpleNamespace(rotation=0.,_element=xml,image=SimpleNamespace(blob=b.getvalue()),crop_left=crop,crop_top=0.,crop_right=0.,crop_bottom=0.)
    check('RGB pixels can prove rect picture opaque',picture_opacity(pic('RGB','white'))['state']=='fully_opaque')
    check('RGBA255 can prove rect picture opaque',picture_opacity(pic('RGBA',(255,255,255,255)))['state']=='fully_opaque')
    check('valid crop of opaque pixels remains opaque',picture_opacity(pic('RGBA',(255,255,255,255),.2))['state']=='fully_opaque')
    check('out-of-source crop unresolved',picture_opacity(pic('RGB','white',-.1))['state']=='unknown')
    check('partial alpha not claimed opaque',picture_opacity(pic('RGBA',(255,255,255,127)))['state']=='unknown')
    check('alpha0 picture transparent',picture_opacity(pic('RGBA',(255,255,255,0)))['state']=='transparent')
    value=dict(status='passed' if all(x['passed'] for x in checks) else 'failed',checks=checks,count=len(checks),
               no_human_truth=True,new_pptx_files=0,new_model_calls=0,new_COM_calls=0)
    save('native_fixture.json',value)
    if value['status']!='passed':raise RuntimeError('Native fixture failed')
    print(json.dumps(dict(status=value['status'],fixtures=len(checks))))


def inputs():
    # Deliberately drop reference labels, modification type, notes and templates.
    allowed=['sample_id','document_id','original_slide_index','pptx_path','png_path','pptx_sha256','png_sha256']
    with (DATA/'manifest_all.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=[{key:r[key] for key in allowed} for r in csv.DictReader(f)]
    if len(rows)!=130 or len({r['sample_id'] for r in rows})!=130 or len({r['document_id'] for r in rows})!=11:raise ValueError('Scope mismatch')
    for row in rows:
        for key in ['pptx_path','png_path']:
            path=(DATA/row[key]).resolve()
            if not path.is_relative_to(DATA.resolve()):raise ValueError('Path outside constructed dataset')
            if digest(path)!=row[key.replace('_path','_sha256')]:raise ValueError('Input SHA mismatch '+row['sample_id'])
    return rows


def run():
    if (OUT/'native_run_summary.json').exists():raise RuntimeError('Native run already complete; no repeated tuning')
    preflight=OUT/'preflight_integrity.json'
    if not preflight.exists() or json.loads(preflight.read_text(encoding='utf-8')).get('status')!='passed':raise RuntimeError('Root protection preflight not passed')
    prior=OUT/'native_failure.json'
    if prior.exists() and not (OUT/'native_attempt_01_failure.json').exists():
        save('native_attempt_01_failure.json',dict(
            failure=json.loads(prior.read_text(encoding='utf-8')),
            progress=json.loads((OUT/'native_measurement_progress.json').read_text(encoding='utf-8')),
            recovery='Fresh COM connection per document; restore AutomationSecurity before closing the last deck.',
            policy_or_threshold_changed=False))
    fixture()
    from pptx import Presentation
    import pythoncom
    import win32com.client
    rows=inputs();j=load_j();parser,rules=j.baseline_modules()
    before=dict(parser=digest(j.H/'structural/source_snapshot/pptx_parser.py'),rules=digest(j.H/'structural/source_snapshot/structure_rules.py'),adapter=digest(J))
    docs={}
    for r in rows:docs.setdefault(r['document_id'],r)
    measurements=[];candidates=[];pending_records=[];page_rows=[];nontext=[];errors=[]
    started=time.perf_counter();app=None;security=None
    pythoncom.CoInitialize()
    try:
        for doc,identity in sorted(docs.items()):
            path=DATA/identity['pptx_path'];prs=Presentation(str(path))
            document,base,adapted,props=j.adapted_parse(path,parser)
            r0,r0risk=rules.analyze(base,document);rj,rjrisk,rju=j.evaluate_adapter(adapted,document,rules)
            app=win32com.client.Dispatch('PowerPoint.Application');security=app.AutomationSecurity;app.AutomationSecurity=3
            com_slides,fail=get_com_measurements(app,path,prs,j,security);errors.extend(fail)
            app=None;security=None
            byslide={s['slide_index']:s for s in com_slides}
            for row in [r for r in rows if r['document_id']==doc]:
                idx=int(row['original_slide_index']);sid=row['sample_id'];cs=byslide.get(idx)
                issue0=[i for i in r0 if i['slide_index']==idx];issuej=[i for i in rj if i['slide_index']==idx]
                unknownj=[dict(**u,method='RJ',sample_id=sid,document_id=doc) for u in rju if u['slide_index']==idx]
                issue_m,unknown_m=rm_candidates(cs,issuej) if cs is not None else ([],[dict(label_scope=LABELS,kind='COM_page_unavailable',reason='measurement_failure')])
                py=prs.slides[idx-1]
                for issue in issuej:
                    if issue['rule_id']!='R01':continue
                    targets=[shape for shape in py.shapes if shape.name==issue.get('details',{}).get('element')]
                    if len(targets)==1 and (targets[0].has_table or targets[0].has_chart):
                        issue_m.append(dict(label=LABELS[3],rule_id='R01',status='candidate',
                                            object_ids=[f's{idx:04d}-shapeid{targets[0].shape_id}'],
                                            measurement_kind='unchanged_nontext_native_frame',details=issue))
                table_ids=[f's{idx:04d}-shapeid{shape.shape_id}' for shape in py.shapes if shape.has_table]
                unknown0=[]
                for tid in table_ids:
                    entry=dict(object_id=tid,label_scope=[LABELS[0],LABELS[1],LABELS[3]],kind='native_table_cell_text_not_parsed')
                    unknown0.append(entry);unknownj.append(dict(**entry,method='RJ',sample_id=sid,document_id=doc))
                # R0 has no uncertainty channel; annotate its known parser scope
                # limitations without changing any of its candidate alerts.
                unknown0.extend(dict(object_id=p['object_id'],label_scope=[LABELS[0],LABELS[3]],kind='baseline_font_or_wrap_missing')
                    for p in props if p['slide_index']==idx and p['text_chars'] and (p['fonts']['state']!='known_local' or p['wrap']['state']=='unknown'))
                converted0=[dict(label=i['defect_type'],rule_id=i['rule_id'],status='candidate',details=i) for i in issue0]
                convertedj=[dict(label=i['defect_type'],rule_id=i['rule_id'],status='candidate',details=i) for i in issuej]
                methods={'R0':converted0,'RJ':convertedj,'RM':issue_m}
                unresolved={'R0':unknown0,'RJ':unknownj,'RM':unknown_m}
                flat=dict(sample_id=sid,document_id=doc,slide_index=idx,pptx_sha256=row['pptx_sha256'],png_sha256=row['png_sha256'])
                for method,events in methods.items():
                    scope_pending=unresolved[method]
                    for label in LABELS:
                        flat[f'{method}_alert_{label}']=int(any(i['label']==label for i in events))
                        flat[f'{method}_pending_{label}']=int(any(label in u.get('label_scope',[u.get('label')]) for u in scope_pending))
                    flat[f'{method}_candidate_count']=len(events);flat[f'{method}_pending_count']=len(scope_pending)
                    candidates.extend(dict(**event,method=method,sample_id=sid,document_id=doc) for event in events)
                    pending_records.extend(dict(**{k:v for k,v in u.items() if k not in ['method','sample_id','document_id']},method=method,sample_id=sid,document_id=doc) for u in scope_pending)
                if cs is not None:measurements.append(dict(document_id=doc,sample_id=sid,**cs))
                page_rows.append(flat)
                for issue in issuej:
                    if issue['rule_id']=='R01' and issue.get('element_id') is None:
                        nontext.append(dict(sample_id=sid,note='object_frame_evidence_separate_from_RM_visible_text_cutoff',issue=issue))
            if digest(path)!=identity['pptx_sha256']:raise RuntimeError('Source PPTX changed')
            save('native_measurement_progress.json',dict(documents_completed=len({r['document_id'] for r in page_rows}),pages=len(page_rows),errors=errors,elapsed_seconds=time.perf_counter()-started))
            print(json.dumps(dict(stage='native',document=doc,pages=len(page_rows),elapsed_seconds=time.perf_counter()-started,errors=len(errors))),flush=True)
    finally:
        if app is not None and security is not None:
            try:app.AutomationSecurity=security
            except Exception as exc:errors.append(dict(stage='restore_security',error=repr(exc)))
        pythoncom.CoUninitialize()
    if len(page_rows)!=130:raise RuntimeError('Incomplete native page coverage')
    page_rows.sort(key=lambda r:(r['document_id'],r['slide_index']))
    csv_save('native_alerts.csv',page_rows);save('native_measurements.json',measurements)
    save('native_candidates.json',candidates);save('native_pending.json',pending_records);save('native_nontext_frame_evidence.json',nontext)
    after=dict(parser=digest(j.H/'structural/source_snapshot/pptx_parser.py'),rules=digest(j.H/'structural/source_snapshot/structure_rules.py'),adapter=digest(J))
    if before!=after:raise RuntimeError('Original source changed')
    inputs() # Recheck original PPTX/PNG hashes after read-only COM.
    summary=dict(status='completed' if not errors else 'completed_with_unresolved',pages=130,documents=11,
        elapsed_seconds=time.perf_counter()-started,source_hashes=before,source_unchanged=before==after,
        native_candidates_by_method=dict(Counter(i['method'] for i in candidates)),
        native_label_alerts={m:{l:sum(r[f'{m}_alert_{l}'] for r in page_rows) for l in LABELS} for m in ['R0','RJ','RM']},
        native_label_pending_pages={m:{l:sum(r[f'{m}_pending_{l}'] for r in page_rows) for l in LABELS} for m in ['R0','RJ','RM']},
        measured_text_objects=sum(len(m['text_objects']) for m in measurements),
        measured_table_cells=sum(bool(o['cell']) for m in measurements for o in m['text_objects']),
        measured_nonempty_paragraphs=sum(len(o['paragraphs']) for m in measurements for o in m['text_objects']),
        picture_opacity_counts=dict(Counter(r['opacity']['state'] for m in measurements for r in m['opaque_regions'] if r['opacity'].get('kind')=='embedded_picture')),
        errors=errors,new_renders=0,new_models_or_forwards=0,new_human_labels=0,
        generator_labels_read_by_native_algorithms=False,visual_scores_read=False,
        cutoff_scope='actual_COM_nonempty_paragraph_envelope_not_pixel_visibility',
        occlusion_scope='later_proven_opaque_rect_region_over_paragraph_envelope_not_readability_truth')
    save('native_run_summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.dont_write_bytecode=True
    args=argparse.ArgumentParser();group=args.add_mutually_exclusive_group(required=True)
    group.add_argument('--fixture',action='store_true');group.add_argument('--run',action='store_true');opt=args.parse_args()
    try:
        if opt.fixture:fixture()
        else:run()
    except Exception as exc:
        save('native_failure.json',dict(error=repr(exc),traceback=traceback.format_exc(),new_model_calls=0,new_renders=0));raise
