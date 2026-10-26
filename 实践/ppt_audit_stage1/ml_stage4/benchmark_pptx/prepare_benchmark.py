"""4.3G-1: model-free PPTX source, rendering, review and freezing workflow.

This script never assigns defect labels or loads any classifier/CLIP feature.
PowerPoint edits only new copies. Every annotation starts as literal unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import html
import io
import math
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import quote

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
ML = ROOT.parent
PROJECT = ML.parent
WORKSPACE = ML.parents[3]
PLAN = ROOT / 'source_plan.json'
MANIFEST = ROOT / 'benchmark_manifest.csv'
REPORT = ML / 'reports/pptx_benchmark_preparation.md'
STATE = ROOT / 'preparation_state.json'
VARIANTS = ('font_small', 'occlusion', 'clutter', 'overflow')
NS = {'p':'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
REVIEW_FIELDS = ('improper_font_sizing','occluded_content','cluttered_layout','content_overflow_cutoff',
                 'annotation_status','review_notes','human_reviewed','reviewer_id','reviewed_at',
                 'render_review_status','font_review_status','provenance_status')


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def require(ok,message):
    if not ok:raise ValueError(message)


def save_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as handle:return list(csv.DictReader(handle))


def install_model_guard():
    blocked=[(ML/n).resolve() for n in ('models','features','data/slideaudit')]
    def guard(event,args):
        if event!='open' or not args or not isinstance(args[0],(str,bytes,os.PathLike)):return
        p=Path(os.fsdecode(args[0])).resolve()
        if any(p.is_relative_to(r) for r in blocked):raise PermissionError('Benchmark preparation blocks model/features/SlideAudit reads: '+str(p))
    sys.addaudithook(guard)


def load_plan():
    return json.loads(PLAN.read_text(encoding='utf-8'))


def pptx_info(path):
    """Inspect actual ordered OOXML slides, not the often-stale app.Slides field."""
    with zipfile.ZipFile(path) as z:
        require(z.testzip() is None,'PPTX ZIP integrity failed: '+str(path))
        p=ET.fromstring(z.read('ppt/presentation.xml'));size=p.find('p:sldSz',NS)
        rels=ET.fromstring(z.read('ppt/_rels/presentation.xml.rels'))
        targets={r.attrib['Id']:posixpath.normpath(r.attrib['Target'].lstrip('/') if r.attrib['Target'].startswith('/') else posixpath.join('ppt',r.attrib['Target'])) for r in rels}
        slides=[]
        for i,s in enumerate(p.findall('p:sldIdLst/p:sldId',NS),1):
            part=targets[s.attrib['{'+NS['r']+'}id']]
            tree=ET.fromstring(z.read(part))
            text=[q.text or '' for q in tree.findall('.//a:t',NS)]
            slides.append({'index':i,'native_slide_id':int(s.attrib['id']),'part':part,
                           'text_characters':sum(map(len,text)),'picture_count':len(tree.findall('.//p:pic',NS)),
                           'shape_count_xml':len(tree.findall('.//p:sp',NS)),
                           'hidden':tree.attrib.get('show','1')=='0'})
        declared=set();themes=set()
        for name in z.namelist():
            if name.endswith('.xml') and name.startswith(('ppt/slides/','ppt/slideMasters/','ppt/slideLayouts/','ppt/theme/')):
                tree=ET.fromstring(z.read(name))
                for q in tree.iter():
                    value=q.attrib.get('typeface','').strip()
                    if value and not value.startswith('+'):
                        (themes if name.startswith('ppt/theme/') else declared).add(value)
        embedded={q.attrib.get('typeface','') for q in p.findall('p:embeddedFontLst/p:embeddedFont/p:font',NS)}
        core={}
        if 'docProps/core.xml' in z.namelist():
            for q in ET.fromstring(z.read('docProps/core.xml')):
                if q.tag.split('}')[-1] in ('creator','lastModifiedBy','created','modified','title'):
                    core[q.tag.split('}')[-1]]=q.text or ''
    return {'slide_count':len(slides),'slide_width_emu':int(size.attrib['cx']),
            'slide_height_emu':int(size.attrib['cy']),'aspect_ratio':int(size.attrib['cx'])/int(size.attrib['cy']),
            'slides':slides,'declared_fonts':sorted(declared),'theme_fonts':sorted(themes),
            'embedded_fonts':sorted(embedded-{''}),'core_metadata':core,'package_status':'valid_zip_xml'}


def source_inventory(plan):
    """All project PPTX plus supplied sources; copies/versions are not independent docs."""
    paths=set()
    for directory,dirs,files in os.walk(WORKSPACE):
        dirs[:]=[d for d in dirs if d not in {'.git','.venv','.cache','__pycache__','.workbuddy','benchmark_pptx','node_modules'}]
        paths.update((Path(directory)/n).resolve() for n in files if n.lower().endswith('.pptx'))
    for d in plan['documents']+plan.get('probes',[]):paths.add(Path(d['source_path']).resolve())
    selected={Path(d['source_path']).resolve():d for d in plan['documents']}
    probes={Path(d['source_path']).resolve():d for d in plan.get('probes',[])}
    com_log=json.loads((ROOT/'operations_originals.json').read_text(encoding='utf-8-sig'))
    com_records={d['document_id']:d for d in com_log['documents']}
    records=[]
    for path in sorted(paths,key=str):
        field={'path':str(path),'pptx_sha256':sha256_file(path),'selected_for_benchmark':'yes' if path in selected else 'no',
               'document_id':'','source_class':'U','source_type':'unverified','prior_audit_use':'not_established',
               'evidence':'','com_open_status':'not_tested_this_round','render_status':'not_tested_this_round'}
        if path in selected:
            d=selected[path];field.update(document_id=d['document_id'],source_class=d['source_class'],source_type=d['source_type'],
                prior_audit_use=d['prior_usage_in_audit_project'],evidence=d['evidence_path']+'; '+d['source_notes'])
        elif path in probes or ('test_samples' in path.parts) or ('downloaded_pptx' in path.parts):
            field.update(source_class='B',source_type='prior_engineering_test_or_scripted_probe',prior_audit_use='yes',
                         evidence='Existing stages1-3 docs/data_flow.md and outputs/测试总结.md or F-2A geometry test records')
        elif 'final_hw' in path.parts:
            field.update(source_class='C',source_type='script_authored_or_ai_image_assembled_proposal_family',
                         evidence='final_hw/06_presentation README, slide_jobs.json and local build scripts; same UI proposal lineage')
        try:
            info=pptx_info(path)
            field.update(slide_count=info['slide_count'],slide_width_emu=info['slide_width_emu'],slide_height_emu=info['slide_height_emu'],
                         aspect_ratio=info['aspect_ratio'],package_status=info['package_status'],error='')
        except Exception as exc:field.update(slide_count='',slide_width_emu='',slide_height_emu='',aspect_ratio='',package_status='failed',error=str(exc))
        chosen=selected.get(path) or probes.get(path)
        if chosen and chosen['document_id'] in com_records:
            actual=com_records[chosen['document_id']]
            field['com_open_status']='opened' if actual.get('slide_count') else 'failed'
            pages=actual.get('pages',[])
            field['render_status']='rendered_pending_human_review' if pages and all((p.get('render') or {}).get('render_status')=='rendered_pending_visual_review' for p in pages) else 'failed_or_incomplete'
        records.append(field)
    counts=Counter(r['pptx_sha256'] for r in records)
    for r in records:r['same_bytes_path_count']=counts[r['pptx_sha256']]
    path=ROOT/'source_inventory.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(records[0]),lineterminator='\n');writer.writeheader();writer.writerows(records)
    return records


def verify_source_copies(plan):
    for d in plan['documents']+plan.get('probes',[]):
        require(sha256_file(Path(d['source_path']))==d['source_sha256'],'Source changed: '+d['document_id'])
        require(sha256_file(ROOT/d['original_path'])==d['source_sha256'],'Raw copy changed: '+d['document_id'])


def font_diagnostic(info,installed):
    aliases={'微软雅黑':'Microsoft YaHei','微软雅黑 Light':'Microsoft YaHei Light','宋体':'SimSun','黑体':'SimHei','等线':'DengXian'}
    norm=lambda s:re.sub(r'[\s_-]+','',aliases.get(s,s)).casefold()
    available={norm(n) for n in installed};embedded={norm(n) for n in info['embedded_fonts']}
    missing=[n for n in info['declared_fonts'] if norm(n) not in available|embedded]
    return {'declared_fonts':info['declared_fonts'],'theme_font_candidates':info['theme_fonts'],
            'embedded_fonts':info['embedded_fonts'],'explicit_fonts_not_found_by_name':missing,
            'limitation':'Installed-name/embedded checks are diagnostic. Inheritance, aliases, glyph fallback and actual substitution need human visual review.',
            'font_review_status':'pending_human_review'}


def collect_manifest(plan):
    require(not MANIFEST.exists(),'Refusing to overwrite an existing annotation manifest')
    verify_source_copies(plan)
    original=json.loads((ROOT/'operations_originals.json').read_text(encoding='utf-8-sig'))
    controlled=json.loads((ROOT/'operations_controlled.json').read_text(encoding='utf-8-sig'))
    originals={d['document_id']:d for d in original['documents']}
    controls={d['document_id']:d for d in controlled['documents']}
    rows=[];diagnostics={}
    def make(d,info,slide,variant,pptx_path,page,op=None):
        is_original=variant=='original';record=page.get('render') or {}
        png=record.get('png_path') or f"renders/{d['document_id']}/{variant}/s{slide['index']:04d}.png"
        status='ok' if record.get('render_status')=='rendered_pending_visual_review' else 'failed'
        error=record.get('error') or page.get('error') or ''
        expected_id=slide['native_slide_id'];actual_id=page.get('native_slide_id')
        if actual_id!=expected_id:status='failed';error+=' native slide ID does not match copied original.'
        if not is_original and (not op or op.get('operation_status')!='modified_pending_human_review'):
            status='failed';error+=' Controlled operation failed or not performed.'
        pp=ROOT/pptx_path;ip=ROOT/png
        pw=ph='';png_hash=''
        if ip.exists():
            with Image.open(ip) as image:image.load();pw,ph=image.size
            png_hash=sha256_file(ip)
            ratio=info['slide_width_emu']/info['slide_height_emu']
            if abs((pw/ph)/ratio-1)>.001:status='failed';error+=' aspect ratio mismatch.'
        else:status='failed';error+=' PNG missing.'
        sample=f"{d['document_id']}_s{slide['index']:04d}_{variant}"
        row={'sample_id':sample,'document_id':d['document_id'],'original_slide_id':str(expected_id),
             'original_slide_index':str(slide['index']),'variant_slide_id':str(actual_id or ''),'variant_id':variant,
             'group_id':f"{d['document_id']}_sid{expected_id}",'source_type':d['source_type'],'source_class':d['source_class'],
             'natural_or_controlled':'natural' if is_original else 'controlled',
             'modification_type':'none' if is_original else variant,'pptx_path':pptx_path,'png_path':png,
             **{n:'unknown' for n in LABEL_COLUMNS},'annotation_status':'pending_human_review',
             'review_notes':'原始页面不预设为全阴性；待人工逐标签核验。' if is_original else '操作仅代表修改意图，不代表缺陷标签成立；可多标签或unknown。',
             'human_reviewed':'no','reviewer_id':'','reviewed_at':'','render_status':status,
             'render_review_status':'pending_human_review','font_review_status':'pending_human_review',
             'provenance_status':d['provenance_status'],'generation_status':'original' if is_original else (op or {}).get('operation_status','failed'),
             'render_error':error,'pptx_sha256':sha256_file(pp) if pp.exists() else '', 'png_sha256':png_hash,
             'png_width':str(pw),'png_height':str(ph),'slide_width_emu':str(info['slide_width_emu']),
             'slide_height_emu':str(info['slide_height_emu']),'reserved_split':'independent_test_reserved'}
        if d['document_id']=='D01_96b385d0' and slide['index']==2 and variant in ('font_small','clutter'):
            row['review_notes']+=' Codex目视初查：修改集中在页面底部窄带，可能未形成目标缺陷，须人工独立判断；此备注不是人工标签。'
        return row
    for d in plan['documents']:
        info=pptx_info(ROOT/d['original_path']);doc=originals[d['document_id']]
        diagnostics[d['document_id']]=font_diagnostic(info,original['installed_font_families'])
        diagnostics[d['document_id']].update(package_info=info,com_referenced_fonts=doc.get('referenced_fonts'),com_font_query_error=doc.get('font_query_error'))
        by_page={p['slide_index']:p for p in doc.get('pages',[])}
        for slide in info['slides']:rows.append(make(d,info,slide,'original',d['original_path'],by_page.get(slide['index'],{})))
        cdoc=controls.get(d['document_id'],{});versions={v['variant_id']:v for v in cdoc.get('variants',[])}
        for variant in VARIANTS:
            v=versions.get(variant,{});vp={p['slide_index']:p for p in v.get('pages',[])}
            path=v.get('pptx_path') or f"pptx/variants/{d['document_id']}_{variant}.pptx"
            for index in d['selected_slide_indices']:
                p=vp.get(index,{});row=make(d,info,info['slides'][index-1],variant,path,p,p.get('operation'))
                if not v.get('save_reopen_verified'):row['render_status']='failed';row['render_error']+=' saved copy reopen not verified.'
                rows.append(row)
    with MANIFEST.open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)
    save_json(ROOT/'font_and_source_diagnostics.json',diagnostics)
    return rows


def validate_manifest(rows,plan):
    ids=[r['sample_id'] for r in rows];require(len(ids)==len(set(ids)),'Duplicate sample ID')
    identities={};groups=defaultdict(set);expected={d['document_id']:d for d in plan['documents']}
    infos={k:pptx_info(ROOT/d['original_path']) for k,d in expected.items()}
    planned=set()
    for key,d in expected.items():
        planned.update((key,s['index'],'original') for s in infos[key]['slides'])
        planned.update((key,int(i),v) for i in d['selected_slide_indices'] for v in VARIANTS)
    actual=[(r['document_id'],int(r['original_slide_index']),r['variant_id']) for r in rows]
    require(len(actual)==len(set(actual)) and set(actual)==planned,'Manifest differs from predeclared pages/variants')
    artifacts={};pixel_changes=[]
    for r in rows:
        require(r['document_id'] in expected,'Unregistered document')
        d=expected[r['document_id']];slide=infos[r['document_id']]['slides'][int(r['original_slide_index'])-1]
        require(str(slide['native_slide_id'])==r['original_slide_id'],'Native original slide ID/index mismatch')
        require(r['group_id']==f"{r['document_id']}_sid{slide['native_slide_id']}",'Group ID changed from original identity')
        original=r['variant_id']=='original'
        pptx=d['original_path'] if original else f"pptx/variants/{r['document_id']}_{r['variant_id']}.pptx"
        png=f"renders/{r['document_id']}/{r['variant_id']}/s{slide['index']:04d}.png"
        require(r['pptx_path']==pptx and r['png_path']==png,'Manifest artifact path changed from recorded identity')
        require(r['natural_or_controlled']==('natural' if original else 'controlled'),'Incorrect subset identity')
        require(r['reserved_split']=='independent_test_reserved','Benchmark is not reserved independently')
        identity=(r['document_id'],r['original_slide_id'])
        require(identities.setdefault(identity,r['group_id'])==r['group_id'],'Original page variants split group')
        groups[r['group_id']].add(identity)
        require(all(r[n] in ('0','1','unknown') for n in LABEL_COLUMNS),'Illegal label; unknown must not become 0')
        for path_key,hash_key in (('pptx_path','pptx_sha256'),('png_path','png_sha256')):
            path=_review_relative(ROOT,r[path_key])
            if r['render_status']=='ok':
                require(path.is_file(),'Successful sample artifact is missing')
                if str(path) not in artifacts:artifacts[str(path)]=sha256_file(path)
                digest=artifacts[str(path)]
                require(digest==r[hash_key],'Artifact SHA differs from manifest: '+r['sample_id'])
        if r['render_status']=='ok':
            with Image.open(ROOT/r['png_path']) as image:
                image.load();require(image.size==(int(r['png_width']),int(r['png_height'])),'PNG dimensions changed')
            require(abs((int(r['png_width'])/int(r['png_height']))/(int(r['slide_width_emu'])/int(r['slide_height_emu']))-1)<=.001,'Render has wrong proportion')
        if r['natural_or_controlled']=='controlled':
            original=next(q for q in rows if q['group_id']==r['group_id'] and q['variant_id']=='original')
            pixel_changes.append({'sample_id':r['sample_id'],'png_bytes_differ':r['png_sha256']!=original['png_sha256']})
    require(all(len(v)==1 for v in groups.values()),'One group contains different original pages')
    for d in plan['documents']:
        require(sum(r['document_id']==d['document_id'] and r['variant_id']=='original' for r in rows)==infos[d['document_id']]['slide_count'],'Original pages missing')
    return {'status':'passed','rows':len(rows),'unique_groups':len(groups),
            'unknown_pages':sum(any(r[n]=='unknown' for n in LABEL_COLUMNS) for r in rows),
            'unknown_label_cells':sum(r[n]=='unknown' for r in rows for n in LABEL_COLUMNS),
            'failed_render_samples':sum(r['render_status']!='ok' for r in rows),
            'controlled_raw_png_identity_checks':pixel_changes,
            'models_or_predictions_read':False}


def compatibility_check(plan):
    sys.path.insert(0,str(ML/'scripts'))
    from check_png_render_aspect_ratio import analyze_record, proportional_export_size
    records=[]
    for d in plan.get('probes',[]):
        path=ROOT/d['original_path'];info=pptx_info(path)
        image=ROOT/'renders'/d['document_id']/'original/s0001.png'
        if image.exists():
            pw,ph=proportional_export_size(info['slide_width_emu'],info['slide_height_emu'])
            q=analyze_record({'case':d['document_id'],'mode':'proportional','pptx':str(path),'png':str(image),
                              'requested_width':pw,'requested_height':ph})
            records.append(q)
        else:records.append({'case':d['document_id'],'passed':False,'error':'PNG missing'})
    save_json(ROOT/'compatibility_check.json',{'evaluation_samples':False,'records':records})
    return records


def previews(rows):
    """Review-only contact sheets; never named in the benchmark input manifest."""
    cache=ML/'.cache/benchmark_review';cache.mkdir(parents=True,exist_ok=True)
    natural=[r for r in rows if r['variant_id']=='original' and r['render_status']=='ok']
    for start in range(0,len(natural),16):
        sheet=Image.new('RGB',(1536,960),'#eeeeee');draw=ImageDraw.Draw(sheet)
        for k,r in enumerate(natural[start:start+16]):
            with Image.open(ROOT/r['png_path']) as image:
                image=image.convert('RGB');image.thumbnail((376,211))
                x=(k%4)*384;y=(k//4)*240;sheet.paste(image,(x,y+24));draw.text((x+3,y+4),r['sample_id'],fill='black')
        sheet.save(cache/f'originals_{start//16+1:02d}.jpg',quality=92)
    grouped=defaultdict(list)
    for r in rows:grouped[r['group_id']].append(r)
    for group,samples in grouped.items():
        if len(samples)<2:continue
        samples=sorted(samples,key=lambda r:(r['variant_id']!='original',r['variant_id']))
        sheet=Image.new('RGB',(1920,240),'#eeeeee');draw=ImageDraw.Draw(sheet)
        for k,r in enumerate(samples):
            if r['render_status']!='ok':continue
            with Image.open(ROOT/r['png_path']) as image:
                image=image.convert('RGB');image.thumbnail((376,211));sheet.paste(image,(k*384,24))
            draw.text((k*384+3,4),r['variant_id'],fill='black')
        sheet.save(cache/(group+'.jpg'),quality=94)


def review_state_summary(rows, plan, check, compatibility):
    """Derive review status from current human records; never invent labels."""
    reviewers=set();confirmed=[]
    for row in rows:
        try:
            stamp=str(row.get('reviewed_at',''))
            valid_time=('T' in stamp or ' ' in stamp) and bool(datetime.fromisoformat(stamp.replace('Z','+00:00')))
        except ValueError:valid_time=False
        has_human=(row.get('annotation_status')=='reviewed' and row.get('human_reviewed')=='yes'
                   and bool(row.get('reviewer_id','').strip()) and valid_time)
        if has_human:
            reviewers.add(row['reviewer_id'].strip())
            confirmed.append(row)
    complete=[r for r in confirmed if all(r.get(n) in ('0','1') for n in LABEL_COLUMNS)]
    blockers=[]
    if len(complete)!=len(rows):blockers.append(f'human four-label review incomplete: {len(rows)-len(complete)} samples')
    gates={'render_status':{'ok'},'render_review_status':{'confirmed'},
           'font_review_status':{'confirmed_no_visible_issue','confirmed_substitution_accepted'},
           'provenance_status':{'confirmed'},'source_class':{'A','C'},
           'reserved_split':{'independent_test_reserved'}}
    for key,allowed in gates.items():
        failed=sum(r.get(key) not in allowed for r in rows)
        if failed:blockers.append(f'{key} gate incomplete/invalid: {failed} samples')
    if not rows:blockers.append('empty benchmark')
    if compatibility and not all(q.get('passed') is True for q in compatibility):blockers.append('render compatibility probe failed')
    frozen=False;record={}
    if (ROOT/'freeze_record.json').exists():
        check_frozen_benchmark(ROOT)  # A stale/tampered record must never produce a ready report.
        record=json.loads((ROOT/'freeze_record.json').read_text(encoding='utf-8'))
        require(not blockers,'Frozen record conflicts with current review gates: '+str(blockers))
        frozen=True
    by_document={}
    for d in plan['documents']:
        classes=sorted({r['source_class'] for r in rows if r['document_id']==d['document_id']})
        by_document[d['document_id']]=classes[0] if len(classes)==1 else ('mixed:'+','.join(classes) if classes else d['source_class'])
    return {'workflow_state':'frozen' if frozen else ('review_complete_awaiting_freeze' if not blockers else 'pending_human_review'),
            'frozen':frozen,'ready_for_final_test':frozen,'freeze_blockers':blockers,
            'human_reviewer_ids':sorted(reviewers),'human_reviewer_count':len(reviewers),
            'human_reviewed_pages':len(complete),'human_unreviewed_pages':len(rows)-len(complete),
            'human_confirmed_positive_counts':{n:sum(r.get(n)=='1' for r in confirmed) for n in LABEL_COLUMNS},
            'human_confirmed_negative_counts':{n:sum(r.get(n)=='0' for r in confirmed) for n in LABEL_COLUMNS},
            'unverified_numeric_label_cells':sum(r.get(n) in ('0','1') for r in rows if r not in confirmed for n in LABEL_COLUMNS),
            'source_class_by_document':by_document,'source_classes':dict(Counter(by_document.values())),
            'cohort_counts':dict(Counter(r['source_class']+'/'+r['natural_or_controlled'] for r in rows)),
            'freeze_record':record}


def write_preparation_report(rows,plan,check,inventory,compatibility):
    counts=Counter(r['natural_or_controlled'] for r in rows)
    summary=review_state_summary(rows,plan,check,compatibility)
    state={'updated_at':now(),**summary,'models_or_new_predictions_read':False,
           'source_documents':len(plan['documents']),'sample_counts':dict(counts),
           'annotation_status_counts':dict(Counter(r['annotation_status'] for r in rows)),
           'checks':check,'manifest_sha256':sha256_file(MANIFEST),'plan_sha256':sha256_file(PLAN),
           'originals_unchanged':True,'compatibility_probe_passed':bool(compatibility) and all(q['passed'] for q in compatibility),
           'no_development_subset':'All documents reserved for independent test; none used to tune.'}
    save_json(STATE,state)
    frozen=summary['frozen'];reviewers=summary['human_reviewer_count']
    status_text={'frozen':'已人工核验并冻结','review_complete_awaiting_freeze':'审核门槛完成，等待执行冻结','pending_human_review':'待人工核验或门槛待补齐'}[summary['workflow_state']]
    classes='、'.join(f'{key}={value}' for key,value in sorted(summary['source_classes'].items()))
    positives=summary['human_confirmed_positive_counts'];negatives=summary['human_confirmed_negative_counts']
    infos={d['document_id']:pptx_info(ROOT/d['original_path']) for d in plan['documents']}
    proportions=Counter(f"{v['slide_width_emu']}:{v['slide_height_emu']}" for v in infos.values())
    lines=['# 4.3G-1：独立PPTX基准准备与人工核验','',
           f"**当前状态：{status_text}；frozen={str(frozen).lower()}，ready_for_final_test={str(summary['ready_for_final_test']).lower()}。本轮没有模型训练、特征提取或新基准预测。**",'',
           f"实际使用{len(plan['documents'])}份用户原稿，共{counts['natural']}页未修改原始页面与{counts['controlled']}个可控修改样本。标签生成时均为unknown；以下统计依据当前manifest及人工审核字段，不根据修改操作或原稿身份自动赋值。",'',
           '## 1. 来源与独立性','',
           '用户于2026-10-26声明：工具包括Codex/Kimi，逐份具体工具不清楚；约近一个月生成；均未导入本PPT审查项目测试。声明和本地生成链证据分别记录；导出器名称不能单独证明AI平台。','',
           '| 文稿ID | 当前源文件 | 页数/真实页面比例 | 来源机制与当前分类 | 当前原始SHA-256 |',
           '| --- | --- | --- | --- | --- |']
    for d in plan['documents']:
        info=infos[d['document_id']]
        ratio=info['aspect_ratio'];ratio_name='16:9' if abs(ratio-16/9)<.001 else ('4:3' if abs(ratio-4/3)<.001 else f'{ratio:.6f}:1')
        lines.append(f"| {d['document_id']} | {d['source_path']} | {info['slide_count']} / {ratio_name} | {d['source_type']}，{summary['source_class_by_document'][d['document_id']]} | `{d['source_sha256']}` |")
    lines += ['',f'当前文稿来源分类：{classes}。A=来源已核实的自然AI文稿；B=旧工程调试样本；C=人工或脚本构建，包括AI辅助脚本及可编辑转译；A_candidate=用户声明AI生成、生成链待核实。natural仅表示未修改原页，不把C自动变为自然AI来源。','',
              '本地旧PPTX摘要/名称调查与用户未使用声明均保留在source_inventory.csv、source_plan.json；扫描不能证明所有历史会话绝无接触。未读取SlideAudit test，未使用旧validation成功/失败页选择新稿。', '',
              'D02与D03同属Game_design课程，主题和素材可能相关；3份文件不等于3个独立生成平台或完全独立主题群体，页面数不能掩盖文稿相关性。D03包内2026-09-23时间可能继承模板，不能代替用户口述的实际生成日期。', '',
              f"source_inventory.csv调查{len(inventory)}个路径、{len(set(r['pptx_sha256'] for r in inventory))}个不同SHA；复制、历史版本及同主题重制不计独立文稿。未选文件只做ZIP/XML检查，不宣称均已COM渲染。",'']
    for d in plan['documents']:lines.append(f"- {d['document_id']}：{d.get('source_notes','未提供额外说明').rstrip('。；')}；来源状态以manifest当前provenance_status及实际证据为准。")
    lines += ['', '## 2. 子集、修改与同源关系','',
              f"未修改原页{counts['natural']}张、可控修改尝试{counts['controlled']}张。每稿固定原页2与5各制作font_small、occlusion、clutter、overflow四版，规则先于任何新模型预测确定。每版保留文稿，仅修改登记页；原文件不覆盖。",'',
              '字号干预设8pt并关闭AutoSize；遮挡添加不透明对象；拥挤复制正文对象并压缩间距；裁切将内容移到边界外。具体操作、shape ID和几何见operations_controlled.json。操作意图不等于缺陷成立，允许原页已有缺陷、多标签或无效干预。','',
              '首次controlled尝试因目标选择的≥30字符限制过严、子进程Get-FileHash不可用而失败；已按最小范围修复后重跑。首次日志及12份尝试PPTX保留在ml_stage4/.cache/benchmark_build/failed_attempt_01，不纳入有效基准或成功统计；下列最终样本状态只来自当前正式operations日志和manifest实际检查。','',
              'original_slide_id使用真实native SlideID，另列1基页序；同原页各版继承group_id，document_id保留文稿相关性。全部预留independent_test_reserved，不供调参；旧几何探针只在calibration，排除评估。','',
              '目前干预只覆盖每稿第2与第5页，且每类只有固定操作强度，不能代表全部版式与缺陷程度。4:3仅有渲染兼容探针，尚无真实独立4:3自然来源。', '',
              '| 来源 × 页面子集 | 样本数 |','| --- | ---: |']
    lines.extend(f'| {key} | {value} |' for key,value in sorted(summary['cohort_counts'].items()))
    lines += ['', '## 3. 渲染、字体与工件','',
              'PowerPoint COM按PageSetup真实比例、长边1920导出原始PNG。可控版保存关闭后再只读重开；清单记录原PNG像素尺寸与实际页尺寸，校验解码、SHA及比例。未使用旧固定1920×1080方式强制导出4:3。','',
              f"清单渲染/操作失败{check['failed_render_samples']}个。几何探针16:9→1920×1080、4:3→1920×1440，圆/矩形、四边标记与包围框映射通过={state['compatibility_probe_passed']}。探针证明入口几何，不冒充新增自然4:3稿；实际来源比例见上表。",'',
              'Codex已查看84原页的缩略总览、6组原页/修改版对照及6张选定原页大图，并抽看字号、遮挡和越界原图；这仅是渲染初查，不是人工缺陷核验。D01第2页的字号/拥挤干预集中在底部窄带，可能不足以形成目标缺陷，已在review_notes提醒，保持unknown。','',
              '字体显式/主题/嵌入名称与COM诊断见font_and_source_diagnostics.json。名称缺失、别名、继承及字形fallback不能仅凭安装列表判定；render_review_status及font_review_status由人工逐页确认。原模型输入没有诊断框或标注；.cache中的缩略图只供核验。','',
              '## 4. 人工标签与冻结','',
              f"当前有记录的人工核验者{reviewers}人：{', '.join(summary['human_reviewer_ids']) or '尚无'}；四标签及人工身份时间字段完整的页面{summary['human_reviewed_pages']}张，尚未完成{summary['human_unreviewed_pages']}张。人数不等于独立多人一致性评估；当前未记录多人一致性或kappa。",'',
              '0也需要人工确认不存在，unknown不作阴性；正负数量仅统计annotation_status=reviewed、human_reviewed=yes且reviewer_id与ISO时间有效的人工记录。未审核的数字标签不计确认数量。','',
              '| 标签 | 人工确认正例 | 人工确认负例 | unknown单元格 |','| --- | ---: | ---: | ---: |']
    lines.extend(f"| {n} | {positives[n]} | {negatives[n]} | {sum(r[n]=='unknown' for r in rows)} |" for n in LABEL_COLUMNS)
    lines += ['',f"未核验的数字标签单元格：{summary['unverified_numeric_label_cells']}；有unknown样本{check['unknown_pages']}个、unknown单元格{check['unknown_label_cells']}个。",'',
              '流程：待生成→待人工核验→核验完成→数据冻结。review_protocol.md给出判定说明，review_gallery.html并排显示原页与修改版。冻结检查所有拟冻结行，不自动筛掉unknown；A/C来源及natural/controlled必须独立报告。','']
    if summary['freeze_blockers']:
        lines.append('当前冻结阻塞项：')
        lines.extend('- '+v for v in summary['freeze_blockers'])
    elif not frozen:lines.append('当前人工门槛已填齐，仍需成功执行freeze后才可称为冻结；本状态不能代替正式冻结检查。')
    else:
        record=summary['freeze_record']
        lines += [f"冻结时间：{record['frozen_at']}；已复核freeze_record、manifest、标签、计划与工件一致性。",'',f"冻结清单SHA-256：`{record['manifest_sha256']}`；标签文件SHA-256：`{record['labels_sha256']}`。"]
    lines += ['', '## 5. 产物与复现','',
              'benchmark_manifest.csv、source_inventory.csv、source_plan.json、原/修改PPTX、原始renders、画廊、核验协议及两份operations日志位于benchmark_pptx。prepare_benchmark.py和modify_and_render.ps1不修改前三阶段、旧划分、模型、阈值或旧特征。','',
              '```powershell','.\\.venv\\Scripts\\python.exe -B benchmark_pptx\\prepare_benchmark.py --check',
              '# 人工编辑清单后刷新画廊和报告，不自动赋标签','.\\.venv\\Scripts\\python.exe -B benchmark_pptx\\prepare_benchmark.py --refresh-review',
              '# 全部门槛通过后执行一次冻结','.\\.venv\\Scripts\\python.exe -B benchmark_pptx\\prepare_benchmark.py --freeze','```','',
              f"当前manifest SHA-256：`{state['manifest_sha256']}`；source_plan原字节SHA-256：`{state['plan_sha256']}`。未生成freeze_record前，这些摘要不构成人工标签冻结。",'',
              '冻结后check拒绝清单、标签、source_plan或工件变化。不得因A/C/H/C9预测改标签、筛页或调整阈值；错误须另建有版本说明的修订记录。冻结门槛通过不等于独立文稿数或统计效力充分。','',
              '## 6. 十项直接回答','',
              f"1. 使用{len(plan['documents'])}份用户原稿；当前来源文稿数：{classes}，不同cohort不能冒充全新自然AI样本。",
              f"2. 原始页面{counts['natural']}张。",f"3. 可控修改尝试{counts['controlled']}张；操作/渲染失败{check['failed_render_samples']}个。",
              f"4. 未修改原页/可控页构成为{counts['natural']}/{counts['controlled']}；它不等于自然缺陷/可控缺陷成立比例，真实标签见人工统计。",
              f"5. 四标签人工确认正例依次为{[positives[n] for n in LABEL_COLUMNS]}，负例为{[negatives[n] for n in LABEL_COLUMNS]}（顺序同上表）。",
              f"6. 有unknown页面{check['unknown_pages']}张、unknown标签单元格{check['unknown_label_cells']}个；失败{check['failed_render_samples']}个。",
              f"7. 实际原稿比例见来源表；16:9/4:3专用探针检查通过={state['compatibility_probe_passed']}，不把探针计作独立自然来源。",
              '8. 本地未发现旧审查文稿SHA/名称重合；来源不确定项按计划与清单保留，历史未接触结论受本地证据和用户声明范围限制。',
              f"9. frozen={str(frozen).lower()}；只有验证通过的freeze_record才表示冻结。",
              f"10. ready_for_final_test={str(summary['ready_for_final_test']).lower()}；{'人工与工件冻结门槛通过，可提交4.3G-2验收，仍须按cohort报告并说明规模限制。' if frozen else '尚不具备4.3G-2评估条件：先补齐阻塞项并成功冻结。'}本轮未运行A/C/H/C9或查看新基准预测。",'',
              '完成后停止，等待人工核验与验收，不自动执行下一阶段。']
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    REPORT.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return state


LABEL_COLUMNS = (
    "improper_font_sizing", "occluded_content", "cluttered_layout",
    "content_overflow_cutoff",
)


def _review_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _review_canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _review_relative(root, value):
    root = Path(root).resolve()
    p = Path(str(value))
    if not str(value).strip() or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"Expected a benchmark-relative artifact path: {value!r}")
    result = (root / p).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"Artifact escapes benchmark directory: {value!r}")
    return result


def _review_csv_rows(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _review_string_rows(rows):
    return [{str(k): "" if v is None else str(v) for k, v in r.items()} for r in rows]


def build_gallery(rows, root):
    """Write a static, model-free gallery; this function never assigns labels."""
    root = Path(root).resolve()
    groups = defaultdict(list)
    for row in rows:
        groups[(str(row.get("document_id", "")), str(row.get("group_id", "")))].append(row)
    esc = lambda value: html.escape(str(value), quote=True)

    def link(value):
        _review_relative(root, value)
        return quote(Path(str(value)).as_posix(), safe="/")

    def sort_row(row):
        natural = str(row.get("natural_or_controlled", "")) == "natural"
        return (not natural, str(row.get("variant_id", "")), str(row.get("sample_id", "")))

    cards = []
    for (document, group), samples in sorted(groups.items()):
        pieces = []
        for row in sorted(samples, key=sort_row):
            labels = " · ".join(f"{esc(k)}: <strong>{esc(row.get(k, 'unknown'))}</strong>" for k in LABEL_COLUMNS)
            png = row.get("png_path", "")
            pptx = row.get("pptx_path", "")
            if png and _review_relative(root, png).is_file():
                image = f'<a href="{link(png)}" target="_blank"><img src="{link(png)}" loading="lazy" alt="{esc(row.get("sample_id", ""))}"></a>'
            else:
                image = '<p class="missing">无可核验 PNG；不得冻结</p>'
            ppt_link = f'<a href="{link(pptx)}">打开 PPTX</a>' if pptx else 'PPTX 路径缺失'
            pieces.append(
                f'<article><h3>{esc(row.get("sample_id", ""))}</h3>'
                f'<p>{esc(row.get("natural_or_controlled", ""))} / {esc(row.get("variant_id", ""))} / 来源 {esc(row.get("source_class", "unknown"))}</p>'
                f'{image}<p>{ppt_link} · 渲染：{esc(row.get("render_status", "unknown"))}</p>'
                f'<p>{labels}</p><p>人工状态：{esc(row.get("annotation_status", "unknown"))}；'
                f'审核者：{esc(row.get("reviewer_id", ""))}</p>'
                f'<p>修改记录：{esc(row.get("modification_type", ""))}</p>'
                f'<p>复核备注：{esc(row.get("review_notes", ""))}</p></article>'
            )
        cards.append(f'<section><h2>{esc(document)} / {esc(group)}</h2><div class="variants">{"".join(pieces)}</div></section>')
    unknown = sum(str(r.get(k, "unknown")) not in ("0", "1") for r in rows for k in LABEL_COLUMNS)
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PPTX 基准人工核验</title><style>body{font-family:system-ui,sans-serif;margin:24px;background:#f5f6f8;color:#17202a}section{margin:26px 0}h2{font-size:18px}.variants{display:flex;gap:16px;overflow-x:auto;align-items:flex-start}article{background:white;border:1px solid #ccd2da;border-radius:8px;padding:12px;min-width:360px;max-width:640px;flex:1}img{width:100%;height:auto;display:block;border:1px solid #ddd}p{font-size:13px;overflow-wrap:anywhere}.notice,.missing{background:#fff3c4;padding:12px}a{color:#1756a9}</style><h1>PPTX 基准人工核验画廊</h1>'
    page += f'<p class="notice">共 {len(rows)} 页；尚未确认的标签单元格 {unknown} 个。unknown 不等于阴性。此页面只展示原始渲染与人工清单，不提供模型预测，也不代替人的目视核验。点击图片查看原始像素；修改操作本身不能证明标签成立。原页也必须逐项检查。</p>'
    page += ''.join(cards) + '</html>\n'
    target = root / "review_gallery.html"
    target.write_text(page, encoding="utf-8")
    return target


def _review_check_rows(root, rows):
    """Validate every proposed frozen row and actual artifact; no filtering."""
    if not rows:
        raise ValueError("No samples to freeze; human-reviewed benchmark is empty.")
    problems, seen, groups, artifacts, cache = [], set(), {}, [], {}
    required = {
        "annotation_status": {"reviewed"}, "human_reviewed": {"yes"},
        "render_status": {"ok"}, "render_review_status": {"confirmed"},
        "font_review_status": {"confirmed_no_visible_issue", "confirmed_substitution_accepted"},
        "provenance_status": {"confirmed"}, "source_class": {"A", "C"},
        "natural_or_controlled": {"natural", "controlled"},
        "reserved_split": {"independent_test_reserved"},
    }
    for row in rows:
        sid = str(row.get("sample_id", ""))
        prefix = sid or "<missing sample_id>"
        if not sid or sid in seen:
            problems.append(f"{prefix}: missing/duplicate sample_id")
        seen.add(sid)
        for key, choices in required.items():
            if str(row.get(key, "")) not in choices:
                problems.append(f"{prefix}: {key}={row.get(key, '<missing>')!r}; expected {sorted(choices)}")
        for key in LABEL_COLUMNS:
            if str(row.get(key, "")) not in {"0", "1"}:
                problems.append(f"{prefix}: {key} must be human-confirmed 0/1; unknown cannot freeze")
        for key in ("document_id", "original_slide_id", "variant_id", "group_id", "source_type", "reviewer_id"):
            if not str(row.get(key, "")).strip():
                problems.append(f"{prefix}: {key} is required")
        when = str(row.get("reviewed_at", ""))
        try:
            if "T" not in when and " " not in when:
                raise ValueError("timestamp required")
            datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"{prefix}: reviewed_at must be an ISO datetime")
        group = str(row.get("group_id", ""))
        origin = (str(row.get("document_id", "")), str(row.get("original_slide_id", "")), str(row.get("original_slide_index", "")))
        if group in groups and groups[group] != origin:
            problems.append(f"{prefix}: group_id joins different original pages")
        groups[group] = origin
        try:
            pp = _review_relative(root, row.get("pptx_path", ""))
            png = _review_relative(root, row.get("png_path", ""))
            for path in (pp, png):
                if not path.is_file():
                    raise ValueError(f"artifact missing: {path}")
            if pp not in cache:
                with zipfile.ZipFile(pp) as z:
                    # Integrity of every referenced PPTX member, including media.
                    bad = z.testzip()
                    if bad:
                        raise ValueError(f"invalid PPTX ZIP member: {bad}")
                    presentation = ET.fromstring(z.read("ppt/presentation.xml"))
                    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
                    size = presentation.find("p:sldSz", ns)
                    if size is None:
                        raise ValueError("PPTX has no page dimensions")
                    slides = presentation.find("p:sldIdLst", ns)
                    cache[pp] = {"sha256": _review_sha(pp), "w": int(size.get("cx")), "h": int(size.get("cy")), "slides": len(slides) if slides is not None else 0}
            if png not in cache:
                with Image.open(png) as image:
                    if image.format != "PNG":
                        raise ValueError("render is not a PNG")
                    image.load()
                    cache[png] = {"sha256": _review_sha(png), "w": image.width, "h": image.height}
            pinfo, info = cache[pp], cache[png]
            if pinfo["sha256"] != str(row.get("pptx_sha256", "")).lower():
                raise ValueError("PPTX SHA-256 differs from manifest")
            if info["sha256"] != str(row.get("png_sha256", "")).lower():
                raise ValueError("PNG SHA-256 differs from manifest")
            if (info["w"], info["h"]) != (int(row.get("png_width", 0)), int(row.get("png_height", 0))):
                raise ValueError("PNG dimensions differ from manifest")
            if (pinfo["w"], pinfo["h"]) != (int(row.get("slide_width_emu", 0)), int(row.get("slide_height_emu", 0))):
                raise ValueError("PPTX page dimensions differ from manifest")
            if min(info["w"], info["h"], pinfo["w"], pinfo["h"]) <= 0:
                raise ValueError("nonpositive image/page size")
            # Permit nearest-integer pixel rounding, not content stretching.
            if abs(info["w"] - info["h"] * pinfo["w"] / pinfo["h"]) > 1.01 and abs(info["h"] - info["w"] * pinfo["h"] / pinfo["w"]) > 1.01:
                raise ValueError("render aspect ratio differs from PPTX page")
            if int(row.get("original_slide_index", 0)) < 1:
                raise ValueError("original_slide_index must be positive")
            artifacts.append({"sample_id": sid, "pptx_path": str(row["pptx_path"]), "png_path": str(row["png_path"]), "pptx_sha256": pinfo["sha256"], "png_sha256": info["sha256"], "png_width": info["w"], "png_height": info["h"], "slide_width_emu": pinfo["w"], "slide_height_emu": pinfo["h"]})
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            problems.append(f"{prefix}: {exc}")
    if problems:
        preview = "\n".join(problems[:60])
        extra = f"\n... {len(problems) - 60} further gate failures" if len(problems) > 60 else ""
        raise ValueError(f"Benchmark freeze blocked ({len(problems)} gate failures); nothing frozen.\n{preview}{extra}")
    return sorted(artifacts, key=lambda x: x["sample_id"])


def freeze_benchmark(root, rows, plan):
    """Freeze all supplied manifest rows only after genuine human review."""
    root = Path(root).resolve()
    record_path = root / "freeze_record.json"
    labels_path = root / "frozen_labels.csv"
    if record_path.exists():
        raise ValueError("freeze_record.json already exists; frozen benchmark cannot be overwritten.")
    if labels_path.exists():
        raise ValueError("frozen_labels.csv exists without a freeze record; investigate partial output before retrying.")
    manifest = root / "benchmark_manifest.csv"
    manifest_data = manifest.read_bytes()
    disk_rows = _review_csv_rows(manifest_data)
    rows = _review_string_rows(list(rows))
    if rows != disk_rows:
        raise ValueError("Passed rows differ from benchmark_manifest.csv; freeze requires its complete, unchanged row set.")
    plan_path = root / "source_plan.json"
    plan_bytes = plan_path.read_bytes()
    disk_plan = json.loads(plan_bytes.decode("utf-8-sig"))
    if _review_canonical(disk_plan) != _review_canonical(plan):
        raise ValueError("Passed plan differs from source_plan.json; freeze aborted.")
    artifacts = _review_check_rows(root, rows)
    plan_data = _review_canonical(plan)  # Fail before writing if plan is not serializable.
    fields = ("sample_id", "document_id", "original_slide_id", "original_slide_index", "variant_id", "group_id", "source_class", "natural_or_controlled", *LABEL_COLUMNS, "reviewer_id", "reviewed_at")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: row[k] for k in fields} for row in rows)
    labels_data = stream.getvalue().encode("utf-8-sig")
    cohorts = Counter((r["source_class"], r["natural_or_controlled"]) for r in rows)
    record = {
        "schema_version": 1, "frozen_at": datetime.now(timezone.utc).isoformat(),
        "ready_for_final_test": True, "models_run_on_benchmark": False,
        "manifest_file": manifest.name, "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
        "labels_file": labels_path.name, "labels_sha256": hashlib.sha256(labels_data).hexdigest(),
        "artifacts_canonical_sha256": hashlib.sha256(_review_canonical(artifacts)).hexdigest(),
        "sample_count": len(rows), "document_count": len({r["document_id"] for r in rows}),
        "group_count": len({r["group_id"] for r in rows}),
        "cohorts": [{"source_class": key[0], "natural_or_controlled": key[1], "samples": n} for key, n in sorted(cohorts.items())],
        "reviewers": sorted({r["reviewer_id"] for r in rows}),
        "inter_annotator_agreement_measured": False,
        "single_reviewer_limit": "No independent multi-reviewer agreement is established by this record.",
        "reserved_split": "independent_test_reserved", "plan": plan,
        "plan_file": "source_plan.json",
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "plan_canonical_sha256": hashlib.sha256(plan_data).hexdigest(),
        "cohort_interpretation": "Report A/C and natural/controlled separately; C never proves natural AI generalization. Ready means completed freeze gates, not sufficient statistical power.",
        "freeze_rule": "No label edits or sample selection based on A/C/H/C9 predictions after freeze.",
    }
    record_data = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    # The ready record is committed last. Unknown labels never enter either file.
    label_temp = labels_path.with_name(labels_path.name + ".tmp")
    record_temp = record_path.with_name(record_path.name + ".tmp")
    try:
        label_temp.write_bytes(labels_data)
        record_temp.write_bytes(record_data)
        if manifest.read_bytes() != manifest_data:
            raise ValueError("Manifest changed during verification; freeze aborted.")
        if plan_path.read_bytes() != plan_bytes:
            raise ValueError("Source plan changed during verification; freeze aborted.")
        os.replace(label_temp, labels_path)
        os.replace(record_temp, record_path)
    finally:
        for temp in (label_temp, record_temp):
            if temp.exists():
                temp.unlink()
    return record


def check_frozen_benchmark(root):
    """Verify frozen labels, manifest bytes and every artifact without inference."""
    root = Path(root).resolve()
    path = root / "freeze_record.json"
    if not path.is_file():
        raise ValueError("No freeze_record.json; benchmark is not frozen or ready.")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("ready_for_final_test") is not True:
        raise ValueError("Freeze record is not ready_for_final_test.")
    manifest_data = (root / "benchmark_manifest.csv").read_bytes()
    if hashlib.sha256(manifest_data).hexdigest() != record.get("manifest_sha256"):
        raise ValueError("Frozen manifest bytes changed; do not evaluate this benchmark.")
    if _review_sha(root / "frozen_labels.csv") != record.get("labels_sha256"):
        raise ValueError("Frozen labels changed; do not evaluate this benchmark.")
    rows = _review_csv_rows(manifest_data)
    artifacts = _review_check_rows(root, rows)
    if hashlib.sha256(_review_canonical(artifacts)).hexdigest() != record.get("artifacts_canonical_sha256"):
        raise ValueError("Frozen artifact digest changed; do not evaluate this benchmark.")
    if hashlib.sha256(_review_canonical(record.get("plan"))).hexdigest() != record.get("plan_canonical_sha256"):
        raise ValueError("Frozen plan digest differs from record.")
    plan_bytes = (root / "source_plan.json").read_bytes()
    if hashlib.sha256(plan_bytes).hexdigest() != record.get("plan_file_sha256"):
        raise ValueError("Frozen source_plan.json bytes changed; do not evaluate this benchmark.")
    disk_plan = json.loads(plan_bytes.decode("utf-8-sig"))
    if hashlib.sha256(_review_canonical(disk_plan)).hexdigest() != record.get("plan_canonical_sha256"):
        raise ValueError("Current source plan differs from frozen plan.")
    if len(rows) != record.get("sample_count"):
        raise ValueError("Frozen sample count differs from record.")
    return {"status": "passed", "ready_for_final_test": True, "samples": len(rows), "manifest_sha256": record["manifest_sha256"], "labels_sha256": record["labels_sha256"]}


def write_review_protocol(root):
    root = Path(root).resolve()
    text = """# PPTX 基准人工核验与冻结

状态顺序：待生成 → 待人工核验 → 核验完成 → 数据冻结。程序和 Codex 均不能替代人的目视确认。本阶段不运行 A/C/H/C9，不显示新基准预测。

1. 打开 `review_gallery.html`，按组并排查看原始页面和所有修改版本；点击 PNG 查看原始像素，必要时在 PowerPoint 打开对应 PPTX。修改操作只说明意图，不能代替缺陷判断；原始页面也不得预设四标签全阴性。
2. 逐页检查导出比例、边缘内容、字体、遮挡与渲染完整性。`render_review_status=confirmed` 只在确认原页与导出对应、无额外拉伸/裁剪/错误补边后填写。字体正常填 `confirmed_no_visible_issue`；发现替代但核验可接受才填 `confirmed_substitution_accepted` 并写明备注；未核验保留 unknown。
3. 四标签只填 `1`（确认存在）、`0`（确认不存在）或 `unknown`（无法确定）。字号：关键内容或正文的可读字号确有不当；遮挡：应当可见的有意义内容被其他对象实质遮住；拥挤：可见布局密度、间距或组织造成明显阅读困难；溢出/裁切：关键文字或对象有实际不可见/截断内容。装饰越界、正常叠放、设计留白、图像细节均不得自动当缺陷；允许多标签共存，边界案例写说明。
4. 核实实际来源和生成方式。A=已核实的真实 AI 生成文稿，C=已核实的脚本/人工构造或可控修改；B 历史工程样本、U/unknown/A_candidate 来源待核实者不能进入正式冻结。保留 document_id 与 group_id；同源修改不成为新的独立来源。来源清楚后填 `provenance_status=confirmed`。A/C、natural/controlled 必须分别统计，不把 C 宣称为自然 AI 数据。
5. 人工核验四标签及渲染、字体、来源后，填写 reviewer_id、ISO reviewed_at、human_reviewed=yes、annotation_status=reviewed，并保存 `benchmark_manifest.csv`。只填写自己实际检查的项目。unknown 不得转成阴性，审核未完成不得标 ready。
6. 所有拟冻结行均通过门槛后，使用本目录生成脚本的冻结入口；它生成 `frozen_labels.csv` 和 `freeze_record.json`，记录清单原始字节、标签和工件 SHA-256。冻结入口不会自动删除 unknown 或失败页来凑数。冻结前如果要另建研究 cohort，必须明确记录范围及原因，保留未审核样本，且不能基于模型预测筛选。
7. 冻结后先运行冻结一致性检查。不能因 A/C/H/C9 的预测而改标签、重筛页面或调整阈值。若发现实质标注错误，隔离记录并另建有版本说明的修订基准，不能静默覆盖本次冻结；模型首次独立对照留待下一阶段。

若只有一名人工核验者，记录该人的实际身份与方法；这不构成多人一致性评估。冻结仅表示来源、工件和人工标签门槛通过，不代表样本规模、独立文稿数或目标部署覆盖已充分。当前所有待核验标签保留 unknown，未核验不得宣称 ready_for_final_test。
"""
    target = root / "review_protocol.md"
    target.write_text(text, encoding="utf-8")
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect',action='store_true');parser.add_argument('--check',action='store_true')
    parser.add_argument('--refresh-review',action='store_true');parser.add_argument('--freeze',action='store_true')
    parser.add_argument('--render-originals',action='store_true');parser.add_argument('--generate-controls',action='store_true')
    args=parser.parse_args();install_model_guard();plan=load_plan()
    if args.render_originals or args.generate_controls:
        phase='originals' if args.render_originals else 'controlled'
        require(not (ROOT/f'operations_{phase}.json').exists(),'Phase log already exists; do not overwrite recorded work')
        verify_source_copies(plan)
        result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-STA','-ExecutionPolicy','Bypass','-File',str(ROOT/'modify_and_render.ps1'),'-BenchmarkRoot',str(ROOT),'-PlanPath',str(PLAN),'-Phase',phase],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        print(result.stdout);require(result.returncode==0,result.stderr);verify_source_copies(plan);return
    if args.collect:
        rows=collect_manifest(plan);inventory=source_inventory(plan);compatibility=compatibility_check(plan)
        check=validate_manifest(rows,plan);build_gallery(rows,ROOT);write_review_protocol(ROOT);previews(rows)
        state=write_preparation_report(rows,plan,check,inventory,compatibility)
        print(json.dumps({'status':'passed','rows':len(rows),'counts':state['sample_counts'],'unknown_cells':check['unknown_label_cells'],
                          'failed_samples':check['failed_render_samples'],'frozen':False,'ready_for_final_test':False},ensure_ascii=True));return
    rows=read_csv(MANIFEST)
    already_frozen=(ROOT/'freeze_record.json').exists()
    if already_frozen:check_frozen_benchmark(ROOT)
    if args.freeze:
        try:
            # Run the same coverage, group and unchanged-source checks before freezing.
            check=validate_manifest(rows,plan);verify_source_copies(plan)
            inventory=read_csv(ROOT/'source_inventory.csv')
            compat=json.loads((ROOT/'compatibility_check.json').read_text(encoding='utf-8'))['records']
            require(bool(compat) and all(q.get('passed') is True for q in compat),'Rendering compatibility probes must pass before freeze')
            record=freeze_benchmark(ROOT,rows,plan)
        except ValueError as exc:
            print(json.dumps({'status':'blocked','frozen':already_frozen,'ready_for_final_test':already_frozen,'reason':str(exc)},ensure_ascii=True));sys.exit(2)
        build_gallery(rows,ROOT)
        write_preparation_report(rows,plan,check,inventory,compat)
        print(json.dumps(record,ensure_ascii=True));return
    check=validate_manifest(rows,plan);verify_source_copies(plan)
    if args.refresh_review:
        build_gallery(rows,ROOT);inventory=read_csv(ROOT/'source_inventory.csv')
        compat=json.loads((ROOT/'compatibility_check.json').read_text(encoding='utf-8'))['records']
        write_preparation_report(rows,plan,check,inventory,compat)
    print(json.dumps({**check,'frozen':already_frozen,'ready_for_final_test':already_frozen},ensure_ascii=True))


if __name__=='__main__':main()
