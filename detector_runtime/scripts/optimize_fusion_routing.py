"""4.3F-2C: nested grouped LR selection, fixed routing and three alert policies.

Only frozen train/validation feature values are accessed. The protected F-2B
loader copies whitelisted rows from a read-only mmap before any computation.
No old validation predictions/model are used. Existing outputs are refused.
The public predict_hybrid function accepts one raw CLIP vector and PNG vector.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import importlib.metadata
import json
import os
from pathlib import Path
import platform

import joblib
import numpy as np
from sklearn.metrics import average_precision_score
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

import train_png_fusion_cv as old

BASE = old.BASE
LABELS, SLUGS = old.LABELS, old.SLUGS
SCHEMES = ('A', 'C', 'H', 'C9')
FITTED = ('A', 'C', 'C9')
MODES = ('f1', 'high_recall', 'low_fpr')
REPORT = BASE / 'reports/fusion_routing_cv_report.md'
OUTER_CSV = BASE / 'reports/fusion_routing_outer_oof.csv'
VALID_CSV = BASE / 'reports/fusion_routing_validation.csv'
CONFIG = BASE / 'reports/fusion_routing_config.json'
MODEL = BASE / 'models/fusion_routing_candidate.joblib'
CACHE = BASE / '.cache/fusion_routing'
OUTPUTS = (REPORT, OUTER_CSV, VALID_CSV, CONFIG, MODEL)
PROTOCOL = {
    'version': 'fusion-routing-nested-v1', 'seed': 42,
    'schemes': {'A':512, 'C':522, 'H':'label-wise A or C', 'C9':521},
    'label_order':list(LABELS), 'png_feature_order':list(old.FEATURES),
    'outer_cv':'3-fold GroupKFold(shuffle=True,random_state=42), original numeric slide_id order',
    'inner_cv':'3-fold GroupKFold(shuffle=True,random_state=42), only current development groups',
    'C':[0.1,1.0,10.0], 'class_weight':[None,'balanced'],
    'lr':{'solver':'lbfgs','max_iter':3000,'tol':1e-6,'random_state':42,'convergence_warning':'error; stop'},
    'hyperparameter_selection':'max pooled inner OOF AP per label; ties smaller C then None',
    'routing':'C iff unrounded C inner OOF AP - A inner OOF AP >= 0.0200; otherwise A; never C9',
    'threshold_grid':[i/100 for i in range(5,96)], 'predict_rule':'probability >= threshold',
    'f1_policy':'max inner OOF F1; ties nearest .5 using round(distance,12), then larger threshold',
    'high_recall_policy':'target Recall>=.70; highest eligible threshold; unmet -> .05, explicitly unmet',
    'low_fpr_policy':'target FPR<=.15; lowest eligible threshold; unmet -> .95, explicitly unmet',
    'policy_scope':'inner OOF only; fold-specific thresholds travel unchanged to outer holdout; no tuning on held-out truth',
    'clip':'float64 per-sample L2 of original center-crop features; no fit',
    'png':'fold-only StandardScaler then divide sqrt(10), C9 sqrt(9); zero-variance scale=1; concat without new weighting',
    'undefined_metrics':old.PROTOCOL['undefined_metrics'],
    'page_metrics':'all-negative false-alarm denominator=all-negative pages; true-defect page hit=any(y & alert); review=any(alert)/all pages',
    'bootstrap':{'replicates':2000,'seed':42,'unit':'whole group_id, four pages together','paired':True,
                 'ci':'2.5/97.5 percentile','missing_ap':'if any label lacks either truth class, invalidate whole paired macro-AP replicate; count, no redraw',
                 'missing_f1':'invalidate paired macro-F1 replicate if any of four label F1 is undefined; no redraw',
                 'fixed_decisions':'use recorded outer-fold predictions, never reselect thresholds, routes, parameters or refit'},
    'loso':{'enabled':True,'sources':['Gemini','Google','gdcdataset'],
            'rule':'once per source; train and select only on other train sources; same inner grouped CV and budgets; no feedback',
            'label':'source-transfer stress test, not independent new-data generalization'},
    'validation':'final full-train inner selection frozen before LOSO and final refit; predict once; load validation truth after all predictions',
    'no_test':True, 'not_for_automatic_defect_confirmation':True,
}
FIT_COUNTS = Counter()


def require(ok, message):
    old.require(ok, message)


def fmt(value):
    return 'N/A' if value is None else f'{value:.4f}'


def make_folds(y, rows, scope):
    groups=np.asarray([r['group_id'] for r in rows],dtype=object)
    require(all(v==4 for v in Counter(groups).values()), 'Incomplete group in '+scope)
    folds=list(GroupKFold(n_splits=3,shuffle=True,random_state=42).split(np.zeros((len(rows),1)),y,groups))
    assignment=np.full(len(rows),-1,dtype=np.int8); summaries=[]
    for k,(fit,hold) in enumerate(folds):
        require(not(set(groups[fit])&set(groups[hold])), 'Group leaks in '+scope)
        require((assignment[hold]==-1).all(), 'Duplicate held-out assignment')
        require(all(set(y[fit,j])=={0,1} and set(y[hold,j])=={0,1} for j in range(4)), 'Class missing in '+scope)
        assignment[hold]=k
        summaries.append({'fold_id':k,'fit_pages':len(fit),'holdout_pages':len(hold),
                          'fit_groups':len(set(groups[fit])),'holdout_groups':len(set(groups[hold])),
                          'fit_positive_counts':y[fit].sum(axis=0).tolist(),
                          'holdout_positive_counts':y[hold].sum(axis=0).tolist()})
    require((assignment>=0).all(), 'Incomplete folds')
    manifest=[{'slide_id':r['slide_id'],'group_id':r['group_id'],'fold':int(assignment[i])} for i,r in enumerate(rows)]
    return folds,assignment,{'scope':scope,'folds':summaries,'fold_manifest':manifest,
                             'fold_manifest_sha256':old.digest(manifest)}


def prepare(clip,png,folds):
    x={n:[] for n in FITTED}; records=[]
    for k,(fit,hold) in enumerate(folds):
        blocks={};rec={'fold_id':k,'scalers':{}}
        for dim,cols in ((10,slice(None)),(9,slice(1,None))):
            scaler=StandardScaler().fit(png[fit,cols])
            require(scaler.n_samples_seen_==len(fit), 'Scaler fit scope mismatch')
            blocks[dim]=(scaler.transform(png[fit,cols])/np.sqrt(dim),scaler.transform(png[hold,cols])/np.sqrt(dim))
            rec['scalers'][str(dim)]=old.scaler_record(scaler,dim)
        for name in FITTED:
            d=9 if name=='C9' else 10
            x[name].append((old.representation(name,clip[fit],blocks[d][0]),
                            old.representation(name,clip[hold],blocks[d][1])))
        records.append(rec)
    return x,records


def thresholds_for(y,p):
    values=[old.binary(y,p,t) for t in old.THRESHOLDS]
    best_f1=max(values,key=lambda m:(m['f1'],-round(abs(m['threshold']-.5),12),m['threshold']))
    recalls=[m for m in values if m['recall'] is not None and m['recall']>=.70]
    fprs=[m for m in values if m['actual_negative'] and m['fp']/m['actual_negative']<=.15]
    high=max(recalls,key=lambda m:m['threshold']) if recalls else values[0]
    low=min(fprs,key=lambda m:m['threshold']) if fprs else values[-1]
    def policy(metric,met,status,count):
        q=dict(metric);q['fpr']=q['fp']/q['actual_negative'] if q['actual_negative'] else None
        return {'threshold':q['threshold'],'constraint_met':met,'status':status,'eligible_count':count,'inner_metrics':q}
    return {'f1':policy(best_f1,True,'f1_maximum',len(values)),
            'high_recall':policy(high,bool(recalls),'target_met' if recalls else 'unmet_grid_min',len(recalls))
                          | {'maximum_grid_recall':max(m['recall'] for m in values)},
            'low_fpr':policy(low,bool(fprs),'target_met' if fprs else 'unmet_grid_max',len(fprs))
                      | {'minimum_grid_fpr':min(m['fp']/m['actual_negative'] for m in values)}}


def select_inner(clip,png,y,rows,scope,count_key):
    folds,_,record=make_folds(y,rows,scope)
    x,scaler_records=prepare(clip,png,folds)
    record['scalers']=scaler_records; record['pages']=len(rows);record['groups']=len(rows)//4
    cv={}
    for name in FITTED:
        selections=[];trials=[]
        for j,label in enumerate(LABELS):
            best=None
            for c in PROTOCOL['C']:
                for weight in PROTOCOL['class_weight']:
                    p=np.full(len(rows),np.nan);iterations=[]
                    for k,(fit,hold) in enumerate(folds):
                        model=old.fit_model(x[name][k][0],y[fit,j],c,weight)
                        FIT_COUNTS[count_key]+=1
                        p[hold]=model.predict_proba(x[name][k][1])[:,1]
                        iterations.append(int(model.n_iter_[0]))
                    require(np.isfinite(p).all() and ((p>=0)&(p<=1)).all(), 'Invalid inner OOF')
                    ap=float(average_precision_score(y[:,j],p))
                    trial={'label':label,'C':c,'class_weight':weight,'inner_oof_ap':ap,
                           'fold_n_iter':iterations,'probability_sha256':old.array_sha(p),'selected':False}
                    trials.append(trial);key=(ap,-c,0 if weight is None else -1)
                    if best is None or key>best[0]:best=(key,p,trial)
            _,p,trial=best;trial['selected']=True
            selections.append({k:trial[k] for k in ('label','C','class_weight','inner_oof_ap','fold_n_iter')}
                              | {'thresholds':thresholds_for(y[:,j],p)})
        cv[name]={'selections':selections,'trials':trials}
    routes={}
    for j,label in enumerate(LABELS):
        a=cv['A']['selections'][j]['inner_oof_ap'];c=cv['C']['selections'][j]['inner_oof_ap']
        selected='C' if c-a>=.0200 else 'A'
        routes[label]={'a_ap':a,'c_ap':c,'ap_delta':c-a,'selected':selected,
                       'reason':'unrounded delta >= 0.0200' if selected=='C' else 'unrounded delta < 0.0200'}
    record['cv']=cv;record['routing']=routes
    record['selection_sha256']=old.digest({'choices':{n:cv[n]['selections'] for n in FITTED},'routing':routes})
    record['frozen_at']=old.now()
    print(json.dumps({'event':'inner_selection_frozen','scope':scope,
                      'routes':{label:q['selected'] for label,q in routes.items()},
                      'ap_delta':{label:q['ap_delta'] for label,q in routes.items()}},ensure_ascii=True),flush=True)
    return record


def selected_thresholds(selection,name):
    choices=[]
    for j,label in enumerate(LABELS):
        routed=selection['routing'][label]['selected'] if name=='H' else name
        choices.append(selection['cv'][routed]['selections'][j])
    return {mode:np.asarray([q['thresholds'][mode]['threshold'] for q in choices],dtype=np.float64) for mode in MODES}


def refit_predict(clip,png,y,hold_clip,hold_png,selection,count_key):
    n=len(y);scalers={};fit_blocks={};hold_blocks={};rec={'fit_pages':n,'scalers':{},'n_iter':{}}
    for d,cols in ((10,slice(None)),(9,slice(1,None))):
        scaler=StandardScaler().fit(png[:,cols])
        require(scaler.n_samples_seen_==n, 'Refit scaler includes held-out data')
        scalers[str(d)]=scaler;rec['scalers'][str(d)]=old.scaler_record(scaler,d)
        fit_blocks[d]=scaler.transform(png[:,cols])/np.sqrt(d)
        hold_blocks[d]=scaler.transform(hold_png[:,cols])/np.sqrt(d)
    p={};models={}
    for name in FITTED:
        d=9 if name=='C9' else 10
        fit_x=old.representation(name,clip,fit_blocks[d]);hold_x=old.representation(name,hold_clip,hold_blocks[d])
        p[name]=np.empty((len(hold_clip),4));models[name]=[];rec['n_iter'][name]=[]
        for j,choice in enumerate(selection['cv'][name]['selections']):
            model=old.fit_model(fit_x,y[:,j],choice['C'],choice['class_weight'])
            FIT_COUNTS[count_key]+=1
            p[name][:,j]=model.predict_proba(hold_x)[:,1]
            models[name].append(model);rec['n_iter'][name].append(int(model.n_iter_[0]))
        require(np.isfinite(p[name]).all() and ((p[name]>=0)&(p[name]<=1)).all(),'Invalid held-out probability')
    p['H']=np.column_stack([p[selection['routing'][label]['selected']][:,j] for j,label in enumerate(LABELS)])
    t={name:{mode:np.broadcast_to(v,(len(hold_clip),4)).copy() for mode,v in selected_thresholds(selection,name).items()} for name in SCHEMES}
    routes=np.asarray([[selection['routing'][label]['selected'] for label in LABELS]]*len(hold_clip),dtype=object)
    rec['predict_proba_calls']=12
    return p,t,routes,models,scalers,rec


def summarize(y,p,t):
    pred=(p>=t)
    per={}
    for j,label in enumerate(LABELS):
        truth=y[:,j];alert=pred[:,j]
        tp,fp,tn,fn=(int(np.count_nonzero(m)) for m in ((truth==1)&alert,(truth==0)&alert,(truth==0)&~alert,(truth==1)&~alert))
        pos,neg=int(truth.sum()),len(y)-int(truth.sum())
        per[label]={'precision':old.ratio(tp,tp+fp),'recall':old.ratio(tp,tp+fn),
                    'f1':old.ratio(2*tp,2*tp+fp+fn),'average_precision':float(average_precision_score(truth,p[:,j])) if pos and neg else None,
                    'tp':tp,'fp':fp,'tn':tn,'fn':fn,'fpr':old.ratio(fp,fp+tn),
                    'actual_positive':pos,'actual_negative':neg,'predicted_positive':tp+fp,
                    'positive_rate':pos/len(y),'predicted_positive_rate':(tp+fp)/len(y)}
    macro={};counts={}
    for name in ('precision','recall','f1','average_precision','fpr'):
        valid=[q[name] for q in per.values() if q[name] is not None]
        macro[name]=float(np.mean(valid)) if valid else None;counts[name]=len(valid)
    tp,fp,tn,fn=(sum(q[k] for q in per.values()) for k in ('tp','fp','tn','fn'))
    micro={'precision':old.ratio(tp,tp+fp),'recall':old.ratio(tp,tp+fn),'f1':old.ratio(2*tp,2*tp+fp+fn),
           'tp':tp,'fp':fp,'tn':tn,'fn':fn,'fpr':old.ratio(fp,fp+tn)}
    all_negative=~y.any(axis=1);any_positive=~all_negative;alerted=pred.any(axis=1)
    true_hit=((y==1)&pred).any(axis=1)
    page={'pages':len(y),'all_negative_pages':int(all_negative.sum()),
          'all_negative_alerted':int((all_negative&alerted).sum()),
          'page_false_positive_rate':old.ratio(int((all_negative&alerted).sum()),int(all_negative.sum())),
          'any_positive_pages':int(any_positive.sum()),'positive_pages_with_true_hit':int((any_positive&true_hit).sum()),
          'positive_page_hit_rate':old.ratio(int((any_positive&true_hit).sum()),int(any_positive.sum())),
          'alerted_pages':int(alerted.sum()),'review_trigger_rate':float(alerted.mean()),
          'average_alert_labels':float(pred.sum()/len(y))}
    return {'per_label':per,'macro':macro,'macro_defined_label_counts':counts,'micro':micro,'page':page}


def evaluate_one(y,p,t,rows):
    # AP uses probabilities; no policy-dependent redefinition.
    ap={label:float(average_precision_score(y[:,j],p[:,j])) if len(set(y[:,j]))==2 else None for j,label in enumerate(LABELS)}
    defined=[q for q in ap.values() if q is not None]
    return {'ap':{'per_label':ap,'macro':float(np.mean(defined)) if defined else None},
            'ranking':old.grouped_ranking(y,p,rows),
            'modes':{mode:summarize(y,p,t[mode]) for mode in MODES}}


def evaluate(y,p,t,rows,sources):
    result=evaluate_one(y,p,t,rows);result['by_source']={}
    for source in sorted(set(sources)):
        mask=sources==source;positions=np.flatnonzero(mask)
        selected_rows=[rows[i] for i in positions]
        require(all(v==4 for v in Counter(r['group_id'] for r in selected_rows).values()), 'Source splits original group')
        result['by_source'][str(source)]={'pages':len(positions),**evaluate_one(y[mask],p[mask],{m:t[m][mask] for m in MODES},selected_rows)}
    return result


def grouped_bootstrap(y,probabilities,thresholds,rows):
    groups=np.asarray([r['group_id'] for r in rows]);names,inverse=np.unique(groups,return_inverse=True)
    require(len(names)==378 and np.all(np.bincount(inverse)==4),'Bootstrap groups invalid')
    cache={};confusions={}
    for name in ('A','C','H'):
        cache[name]=[];confusions[name]={}
        for j in range(4):
            scores=probabilities[name][:,j];order=np.argsort(-scores,kind='stable')
            ends=np.r_[np.flatnonzero(np.diff(scores[order])!=0),len(y)-1]
            cache[name].append((order,ends,y[order,j]))
        for mode in MODES:
            pred=probabilities[name]>=thresholds[name][mode]
            confusions[name][mode]=[((y==1)&pred).astype(float),((y==0)&pred).astype(float),((y==1)&~pred).astype(float)]
    def ap_vector(name,w):
        values=[]
        for order,ends,truth in cache[name]:
            sw=w[order];positive=np.cumsum(sw*truth)[ends];total=np.cumsum(sw)[ends]
            if positive[-1]==0 or positive[-1]==total[-1]:return None
            precision=np.divide(positive,total,out=np.zeros_like(positive,dtype=float),where=total>0)
            values.append(float(np.sum(np.diff(np.r_[0.,positive])*precision)/positive[-1]))
        return np.asarray(values)
    # Check the optimized AP arithmetic against an independent public implementation.
    oracle_weights=np.random.default_rng(17).integers(0,4,len(y))
    for name in cache:
        actual=ap_vector(name,oracle_weights)
        expected=np.array([average_precision_score(y[:,j],probabilities[name][:,j],sample_weight=oracle_weights) for j in range(4)])
        require(np.allclose(actual,expected,rtol=0,atol=1e-12),'Bootstrap AP oracle mismatch')
    rng=np.random.default_rng(42);ap_deltas={'A':[],'C':[]};f1_deltas={n:{m:[] for m in MODES} for n in ('A','C')}
    for _ in range(2000):
        count=np.bincount(rng.integers(0,len(names),len(names)),minlength=len(names));w=count[inverse].astype(float)
        aps={n:ap_vector(n,w) for n in cache}
        if all(v is not None for v in aps.values()):
            for n in ap_deltas:ap_deltas[n].append(float((aps['H']-aps[n]).mean()))
        for mode in MODES:
            f1={}
            for n in cache:
                tp,fp,fn=(w@events for events in confusions[n][mode]);den=2*tp+fp+fn
                f1[n]=float(np.mean(2*tp/den)) if (den>0).all() else None
            for n in f1_deltas:
                if f1['H'] is not None and f1[n] is not None:f1_deltas[n][mode].append(f1['H']-f1[n])
    point_ap={n:np.mean([average_precision_score(y[:,j],probabilities[n][:,j]) for j in range(4)]) for n in cache}
    result={}
    def pack(values,point):
        return {'point':float(point),'ci':np.percentile(values,[2.5,97.5]).tolist() if values else None,
                'valid_replicates':len(values),'invalid_replicates':2000-len(values)}
    for n in ('A','C'):
        f1={}
        for mode in MODES:
            h=summarize(y,probabilities['H'],thresholds['H'][mode])['macro']['f1']
            b=summarize(y,probabilities[n],thresholds[n][mode])['macro']['f1']
            f1[mode]=pack(f1_deltas[n][mode],h-b)
        result['H_minus_'+n]={'ap':pack(ap_deltas[n],point_ap['H']-point_ap[n]),'f1':f1}
    return {'protocol':PROTOCOL['bootstrap'],'results':result,'weighted_ap_oracle_passed':True,
            'limitations':'Fixed outer OOF decisions only; no full retraining/reselection uncertainty; internal interval is not independent PPTX evidence'}


def add_prediction_fields(prefix=''):
    fields=[prefix+'fold_id'] if prefix else []
    fields += [f'{prefix}{n}_prob_{s}' for n in SCHEMES for s in SLUGS]
    fields += [f'{prefix}{n}_{m}_{kind}_{s}' for n in SCHEMES for m in MODES for kind in ('pred','threshold') for s in SLUGS]
    fields += [f'{prefix}H_route_{s}' for s in SLUGS]
    return fields


def save_predictions(path,rows,sources,y,p,t,routes,fold_ids,loso=None):
    fields=['slide_id','group_id','split','source_type','fold_id']+[f'true_{s}' for s in SLUGS]+add_prediction_fields()
    if loso is not None:fields+=add_prediction_fields('loso_')
    temporary=path.with_suffix('.csv.part')
    def fill(record,i,prob,threshold,route,prefix=''):
        for n in SCHEMES:
            for j,s in enumerate(SLUGS):
                record[f'{prefix}{n}_prob_{s}']=format(float(prob[n][i,j]),'.17g')
                for mode in MODES:
                    value=float(threshold[n][mode][i,j])
                    record[f'{prefix}{n}_{mode}_threshold_{s}']=format(value,'.17g')
                    record[f'{prefix}{n}_{mode}_pred_{s}']=int(prob[n][i,j]>=value)
        record.update({f'{prefix}H_route_{s}':route[i,j] for j,s in enumerate(SLUGS)})
    with temporary.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,lineterminator='\n');writer.writeheader()
        for i,row in enumerate(rows):
            rec={k:row[k] for k in ('slide_id','group_id','split')}
            rec.update(source_type=str(sources[i]),fold_id='' if fold_ids is None else int(fold_ids[i]))
            rec.update({f'true_{s}':int(y[i,j]) for j,s in enumerate(SLUGS)})
            fill(rec,i,p,t,routes)
            if loso is not None:
                fill(rec,i,loso['probabilities'],loso['thresholds'],loso['routes'],'loso_')
                rec['loso_fold_id']=int(loso['fold_ids'][i])
            writer.writerow(rec)
    os.replace(temporary,path)
    restored=old.read_csv(path)
    require(len(restored)==len(rows) and len({r['slide_id'] for r in restored})==len(rows),'CSV count/uniqueness failed')
    for i,(a,b) in enumerate(zip(restored,rows)):
        require(all(a[k]==b[k] for k in ('slide_id','group_id','split')) and a['source_type']==str(sources[i]),'CSV identity/source changed')
        require(a['fold_id']==('' if fold_ids is None else str(int(fold_ids[i]))),'CSV fold changed')
        require([int(a[f'true_{s}']) for s in SLUGS]==y[i].tolist(),'CSV truth changed')
    for prefix,prob,threshold,route in [('',p,t,routes)]+([] if loso is None else [('loso_',loso['probabilities'],loso['thresholds'],loso['routes'])]):
        for name in SCHEMES:
            pp=np.array([[float(r[f'{prefix}{name}_prob_{s}']) for s in SLUGS] for r in restored])
            require(np.array_equal(pp,prob[name]),'CSV probabilities changed')
            for mode in MODES:
                tt=np.array([[float(r[f'{prefix}{name}_{mode}_threshold_{s}']) for s in SLUGS] for r in restored])
                pred=np.array([[int(r[f'{prefix}{name}_{mode}_pred_{s}']) for s in SLUGS] for r in restored])
                require(np.array_equal(tt,threshold[name][mode]) and np.array_equal(pred,pp>=tt),'CSV fold threshold/prediction mismatch')
        require(np.array_equal(np.array([[r[f'{prefix}H_route_{s}'] for s in SLUGS] for r in restored],dtype=object),route),'CSV routes changed')


def predict_hybrid(clip_features,png_features,model_or_path=MODEL):
    """One-page unified research inference; probabilities are not defect confirmation.

    Accept an unnormalized center-crop CLIP vector (512,) and same-page raw-PNG
    statistics (10,) in frozen order. Metadata never becomes an input feature.
    """
    bundle=joblib.load(model_or_path) if isinstance(model_or_path,(str,os.PathLike)) else model_or_path
    clip=np.asarray(clip_features,dtype=np.float64);png=np.asarray(png_features,dtype=np.float64)
    require(clip.shape==(512,) and png.shape==(10,) and np.isfinite(clip).all() and np.isfinite(png).all(),'One-page feature shape/finite mismatch')
    norm=float(np.linalg.norm(clip));require(np.isfinite(norm) and norm>1e-12,'Invalid or zero CLIP feature norm')
    clip=clip[None,:]/norm
    block=bundle['png_scalers']['10'].transform(png[None,:])/np.sqrt(10)
    fusion=np.concatenate((clip,block),axis=1);prob=[];routes=[]
    for j,label in enumerate(LABELS):
        route=bundle['routing'][label]['selected'];routes.append(route)
        model=bundle['models'][route][j]
        prob.append(float(model.predict_proba(clip if route=='A' else fusion)[0,1]))
    require(np.isfinite(prob).all(),'Invalid candidate inference')
    thresholds={mode:[float(q) for q in bundle['hybrid_thresholds'][mode]] for mode in MODES}
    return {'labels':list(LABELS),'probabilities':prob,'model_schemes':routes,'thresholds':thresholds,
            'alerts':{m:[bool(p>=t) for p,t in zip(prob,thresholds[m])] for m in MODES},
            'status':'research risk alerts; not automatic defect confirmation'}


def write_report(r):
    """Write the sole new research report from frozen run results, without fitting."""
    import json

    outer = r['outer_results']
    validation = r['validation_results']
    final = r['final_selection']
    source_names = ('Gemini', 'Google', 'gdcdataset')
    names = {'A': 'CLIP', 'C': 'CLIP+PNG10', 'H': '按标签A/C路由', 'C9': 'CLIP+PNG9（无宽高比）'}
    modes = {'f1': 'F1', 'high_recall': '高召回', 'low_fpr': '低误报'}

    def f(v):
        return fmt(v)

    def cell(v):
        if v is None:
            return 'N/A'
        return str(v).replace('|', '\\|').replace('\n', ' ')

    def diff(a, b):
        return None if a is None or b is None else float(a) - float(b)

    def sf(a, b):
        v = diff(a, b)
        return 'N/A' if v is None else f'{v:+.4f}'

    def mdtable(header, rows):
        lines.append('| ' + ' | '.join(header) + ' |')
        lines.append('| ' + ' | '.join('---' for _ in header) + ' |')
        for row in rows:
            lines.append('| ' + ' | '.join(cell(x) for x in row) + ' |')
        lines.append('')

    def details(title):
        lines.extend(['<details>', f'<summary>{title}</summary>', ''])

    def enddetails():
        lines.extend(['</details>', ''])

    def summary_table(results):
        values = []
        for scheme in SCHEMES:
            for mode in MODES:
                ss = results[scheme]['modes'][mode]
                ma, mi = ss['macro'], ss['micro']
                values.append([scheme, modes[mode], f(results[scheme]['ap']['macro']),
                    f(ma['precision']), f(ma['recall']), f(ma['f1']), f(mi['f1']),
                    mi['tp'], mi['fp'], mi['tn'], mi['fn'], f(mi['fpr'])])
        mdtable(['方案', '模式', 'Macro AP', 'Macro P', 'Macro R', 'Macro F1',
                 'Micro F1', 'TP', 'FP', 'TN', 'FN', 'Micro FPR'], values)

    def label_table(results):
        values = []
        for scheme in SCHEMES:
            for mode in MODES:
                for label in LABELS:
                    a = results[scheme]['modes'][mode]['per_label'][label]
                    values.append([scheme, modes[mode], label, f(a['precision']), f(a['recall']),
                        f(a['f1']), f(results[scheme]['ap']['per_label'][label]),
                        a['tp'], a['fp'], a['tn'], a['fn'], f(a['fpr']),
                        a['actual_positive'], a['predicted_positive'], f(a['predicted_positive_rate'])])
        mdtable(['方案', '模式', '标签', 'P', 'R', 'F1', 'AP', 'TP', 'FP', 'TN', 'FN',
                 'FPR', '真实正例', '预测正例', '预测正例比'], values)

    def page_table(results):
        values = []
        for scheme in SCHEMES:
            for mode in MODES:
                p = results[scheme]['modes'][mode]['page']
                values.append([scheme, modes[mode], p['all_negative_pages'], p['all_negative_alerted'],
                    f(p['page_false_positive_rate']), p['any_positive_pages'],
                    p['positive_pages_with_true_hit'], f(p['positive_page_hit_rate']), p['alerted_pages'],
                    f(p['review_trigger_rate']), f(p['average_alert_labels'])])
        mdtable(['方案', '模式', '全阴页', '全阴报警页', '页面FPR', '有缺陷页',
                 '检中真实缺陷页', '缺陷页命中率', '复核触发页', '复核触发率', '每页报警标签数'], values)

    def ranking_table(results):
        values = []
        for label in LABELS:
            for scheme in SCHEMES:
                a = results[scheme]['ranking'][label]
                values.append([label, scheme, a['comparable_groups'], a['positive_negative_pairs'],
                    f(a['pooled_pair_auc']), f(a['equal_group_auc'])])
        mdtable(['标签', '方案', '可比较组', '正负配对', '配对加权AUC', '组等权AUC'], values)

    def source_tables(results):
        values = []
        for source in source_names:
            a = results['A']['by_source'][source]
            row = [source, a['pages']]
            for label in LABELS:
                b = a['modes']['f1']['per_label'][label]
                row.append(f"{b['actual_positive']}/{a['pages']} ({f(b['positive_rate'])})")
            values.append(row)
        mdtable(['来源', '页数', *LABELS], values)
        values = []
        for source in source_names:
            for scheme in SCHEMES:
                a = results[scheme]['by_source'][source]
                for mode in MODES:
                    s = a['modes'][mode]
                    values.append([source, scheme, modes[mode], f(a['ap']['macro']),
                        *[f(a['ap']['per_label'][label]) for label in LABELS],
                        f(s['macro']['f1']), s['micro']['fp'], s['micro']['fn'],
                        f(s['page']['page_false_positive_rate']), f(s['page']['review_trigger_rate'])])
        mdtable(['来源', '方案', '模式', 'Macro AP', '字号AP', '遮挡AP', '拥挤AP', '裁切AP',
                 'Macro F1', 'FP', 'FN', '页面FPR', '复核触发率'], values)

    def selection_tables(block, title):
        lines.extend([f'### {title}', ''])
        rows = []
        for label in LABELS:
            a = block['routing'][label]
            rows.append([label, f(a['a_ap']), f(a['c_ap']), f(a['ap_delta']),
                         a['selected'], a['reason']])
        mdtable(['标签', 'A inner OOF AP', 'C inner OOF AP', 'C−A', 'H路由', '原因'], rows)
        rows = []
        for scheme in ('A', 'C', 'C9'):
            for a in block['cv'][scheme]['selections']:
                th = []
                for mode in MODES:
                    q = a['thresholds'][mode]
                    status = '；约束未达标' if q.get('constraint_met') is False else ''
                    th.append(f"{f(q['threshold'])}{status}（{q.get('status', '')}）")
                rows.append([scheme, a['label'], a['C'], a['class_weight'], f(a['inner_oof_ap']), *th])
        mdtable(['方案', '标签', 'C', 'class_weight', 'inner AP', 'F1阈值', '高召回阈值', '低误报阈值'], rows)

    def main_comparison(results, mode='f1'):
        return {scheme: results[scheme]['modes'][mode] for scheme in ('A', 'C', 'H')}

    lines = ['# 4.3F-2C：融合模型按标签路由与复核阈值实验', '',
        '本报告记录一次预先固定的嵌套分组交叉验证。H只在相应训练数据内部按AP增益规则选择A或C；'
        'C9仅作去宽高比敏感性参考。所有结果均为真实运行，未使用test开发、选参、选阈值或评价。', '',
        f"运行时间：{r.get('started_at', 'N/A')} 至 {r.get('finished_at', 'N/A')}。", '',
        '## 1. 冻结输入与实验边界', '',
        'train=1512页/378个四页同源组；validation=328页/82组。按slide_id白名单对齐；'
        'test=324页与excluded=236页不进入数值运算或标签读取。旧CLIP NPY只以只读mmap索引选取允许行，'
        '不重新读取整个混合NPY计算摘要。官方annotations仅提供严格bool真实标签。', '',
        'A为512维中心裁剪CLIP；C为512维CLIP+10维冻结PNG统计；H按标签复用A/C；C9去除宽高比后为521维。'
        'CLIP每行以float64固定L2归一化；PNG StandardScaler仅fit当前训练折，除以√10或√9后与CLIP拼接，'
        '不再拟合块权重或对拼接向量归一化。source_type、group_id、split、slide_id和标注信息均不进入X。', '',
        'CLIP：openai/clip-vit-base-patch32，revision '
        '`3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268`；未重新提取或微调CLIP，未修改PNG recipe。', '',
        '## 2. 嵌套分组、选参与工作模式', '',
        '外层3折GroupKFold固定seed=42，每折1008页开发/504页留出；外层训练数据内部再做3折分组OOF，'
        '每内层672页拟合/336页留出。A/C/H/C9共享全部划分，同源四页始终在同一折。'
        '每标签每表示仅搜索C∈{0.1,1,10}×class_weight∈{None,balanced}，'
        '以pooled inner OOF AP选参；并列优先较小C，再None。标准化仅fit相应训练折。', '',
        'H路由固定：每标签C inner OOF AP−A inner OOF AP≥0.0200时选C，否则选A；'
        '恰好0.0200选C。外层留出真值不决定该折任何选择。最终拟合另在完整train上执行相同3折OOF选择，'
        '然后fit1512页；冻结选择后才预测validation。', '',
        '三个模式共用4.3F-2B阈值网格0.05–0.95（含边界，步长0.01），预测为p≥threshold。'
        'F1模式最大化inner OOF F1，并列取最接近0.5、再较大阈值。高召回模式在Recall≥0.70的候选中取最高阈值；'
        '低误报模式在FPR≤0.15的候选中取最低阈值。未达标时如实标记，采用预先固定边界回退；'
        '具体回退、缺失指标和数值并列规则见配置protocol及逐折selection，未根据外层或validation放宽约束。', '',
        'AP只依赖概率排序，三种模式的AP相同；满足inner约束不保证外层或validation也达标。'
        '高召回输出只能视为风险提示。', '']

    details('实际冻结协议与版本')
    lines.extend(['```json', json.dumps({'protocol': r['protocol'], 'versions': r['versions']},
                   ensure_ascii=False, indent=2, allow_nan=False), '```', ''])
    enddetails()
    lines.extend(['## 3. 外层OOF：1512页未参与相应外层训练的预测', ''])
    summary_table(outer)
    details('外层OOF逐标签、三模式完整指标')
    label_table(outer)
    enddetails()
    lines.extend(['### 页面级误报与复核工作量', '',
        '标签FP是一项错误缺陷报警；页面FP仅统计真实四标签全阴页中有任意报警的页面；'
        '复核触发页包括真阳性和全阴性页面，不能互相替代。缺陷页命中要求至少一个报警与真实正标签重合。', ''])
    page_table(outer)
    lines.extend(['### 组内增强版本排序', '',
        '只比较同一四页组内同时存在该标签正负版本的组；并列概率计0.5。配对加权AUC按全部正负配对汇总，'
        '组等权AUC先算各组AUC再平均；它们衡量局部缺陷变化，不能用整体AP代替。', ''])
    ranking_table(outer)
    details('外层OOF来源正例率与A/C/H/C9分层诊断')
    source_tables(outer)
    enddetails()

    lines.extend(['## 4. 逐折路由、超参数和实际阈值', '',
        '外层OOF每页保存所属折的实际路由与阈值，不能用最终全train阈值代替。'
        'H复用所选方案同标签的超参数和三模式阈值；完整36次内层搜索的逐配置AP、折清单及Scaler参数见配置JSON。', ''])
    for fold in r['outer_cv']:
        details(f"外层折 {fold['fold_id']}：仅内层选择")
        selection_tables(fold['inner'], f"外层折 {fold['fold_id']}")
        enddetails()
    selection_tables(final, '完整train OOF决定的最终候选')
    lines.extend(['## 5. validation：328页探索性额外检查', '',
        'validation此前已在4.3C至4.3F-2B多轮研究中被观察，本轮结果属于探索性比较，'
        '不是全新独立测试。下表使用全部train内冻结的模型、路由、Scaler和阈值，'
        '未用validation重新选择工作模式或追加实验。', ''])
    summary_table(validation)
    details('validation逐标签、三模式完整指标')
    label_table(validation)
    enddetails()
    page_table(validation)
    ranking_table(validation)
    details('validation来源正例率与A/C/H/C9分层诊断')
    source_tables(validation)
    enddetails()

    lines.extend(['## 6. 来源迁移压力测试', ''])
    loso = r.get('loso')
    if loso and loso.get('pooled'):
        lines.extend(['依次留出Gemini、Google、gdcdataset，仅在其他来源的train组内做3折OOF完成参数、'
            '路由和阈值选择，再拟合其余来源并预测留出来源；不使用validation。'
            '这是来源迁移压力测试，不是严格独立新数据泛化测试，不作为本轮模型选择或再调参依据。', ''])
        summary_table(loso['pooled'])
        details('压力测试来源分层结果')
        source_tables(loso['pooled'])
        enddetails()
    else:
        lines.extend([f"未执行；原因：{cell(loso.get('reason') if isinstance(loso, dict) else '资源或实现条件限制')}。", ''])

    lines.extend(['## 7. 组级配对bootstrap', '',
        '整组重采样保留同源四页，H与A/C使用相同重采样组；固定随机种子和次数见protocol。'
        '区间基于已经冻结的外层OOF概率和逐页阈值，不包含完整重训练、超参数选择、路由或阈值选择的不确定性，'
        '也未消除交叉验证折之间的依赖；不能替代独立PPTX测试。AP无定义的重采样按冻结缺失规则处理，'
        '有效/无效次数如下，不因区间不理想改变实验。', ''])
    br = []
    for contrast in ('H_minus_A', 'H_minus_C'):
        block = r['bootstrap']['results'][contrast]
        a = block['ap']
        ci = a.get('ci')
        br.append([contrast, 'Macro AP', f(a['point']),
            'N/A' if ci is None else f'[{f(ci[0])}, {f(ci[1])}]',
            a.get('valid_replicates'), a.get('invalid_replicates')])
        for mode in MODES:
            a = block['f1'][mode]
            ci = a.get('ci')
            br.append([contrast, f"Macro F1/{modes[mode]}", f(a['point']),
                'N/A' if ci is None else f'[{f(ci[0])}, {f(ci[1])}]',
                a.get('valid_replicates'), a.get('invalid_replicates', 0)])
    mdtable(['对比', '指标', '点差值', '95% percentile区间', '有效次数', '无效次数'], br)

    lines.extend(['## 8. 可复现产物与检查', '',
        '逐页CSV包含真实四标签、A/C/H/C9概率、三模式二值预测与实际阈值、fold_id、H逐标签路由、'
        '诊断source_type。外层OOF及validation分开保存；第三方可仅凭这些CSV重新计算上表概率、阈值、'
        '来源、页面级与组内排序指标。搜索细节、折清单和bootstrap协议在配置JSON中。', '',
        '独立研究候选不覆盖4.3C正式模型或4.3F-2B候选。统一接口接受一页512维CLIP和同页10维冻结PNG数值，'
        '返回四概率、逐标签路由、三模式报警与阈值；输入需保持特征顺序和模型revision一致，未接入智能体。', ''])
    mdtable(['训练阶段', '实际fit数'], [[k, v] for k, v in r['fit_counts'].items()])
    hashrows = [['新训练脚本', r.get('source_sha256')],
                ['冻结最终选择', r.get('frozen_selection_sha256')]]
    for key, value in r['inputs'].items():
        if 'sha256' in key and not isinstance(value, (dict, list)):
            hashrows.append([key, value])
    for key, value in r.get('output_sha256', {}).items():
        hashrows.append([key, value])
    mdtable(['对象', 'SHA-256'], hashrows)
    lines.extend(['含test的旧CLIP全文件摘要仅引用冻结index声明，不重新读取所有数值计算；'
        '本轮白名单选中行有独立数值摘要。配置JSON不包含自身摘要，报告记录配置文件SHA以避免自引用。', '',
        '## 9. 仍未解决的问题与下一步', '',
        'PNG统计是视觉密度和分布，不是真实PPTX字号、文本框、遮挡或裁切结构。'
        '宽高比与来源关联，C9提供有限敏感性参考；任何总体AP提高仍可能依赖来源或组间差异。'
        '既知相同PNG对应冲突标签使纯图像分类存在不可识别部分，本轮保留全部官方样本与标签。'
        '页面级样本含同源增强相关性，指标不等同于独立原始PPTX泛化能力。', '',
        '4.3F-2B用同一train OOF同时选参和报告；本轮将参数、路由与阈值全部封装在外层内部的内层OOF，'
        '外层提供更严格的选择后评估。数值不能直接视作同一估计方式下的升降。', '',
        '## 10. 十一项直接回答', ''])

    route = '；'.join(f"{label}→{final['routing'][label]['selected']}" for label in LABELS)
    lines.extend([f'1. **H最终选了什么：**{route}。完全由完整train OOF的固定0.0200规则决定。', ''])
    hs = outer['H']; aa = outer['A']; cc = outer['C']
    h = hs['modes']['f1']; a = aa['modes']['f1']; c = cc['modes']['f1']
    lines.extend([f"2. **嵌套CV是否更好：**H Macro AP={f(hs['ap']['macro'])}，"
        f"相对A {sf(hs['ap']['macro'], aa['ap']['macro'])}、相对C {sf(hs['ap']['macro'], cc['ap']['macro'])}；"
        f"F1模式Macro F1={f(h['macro']['f1'])}，相对A {sf(h['macro']['f1'], a['macro']['f1'])}、"
        f"相对C {sf(h['macro']['f1'], c['macro']['f1'])}。应结合上述区间判断，不能仅据点估计宣称可靠优越。", ''])
    label = LABELS[0]
    vh = validation['H']['modes']['f1']; va = validation['A']['modes']['f1']; vc = validation['C']['modes']['f1']
    lines.extend([f"3. **字号退步是否改善：**外层H相对A/C的字号AP差为"
        f"{sf(hs['ap']['per_label'][label], aa['ap']['per_label'][label])}/"
        f"{sf(hs['ap']['per_label'][label], cc['ap']['per_label'][label])}，F1差为"
        f"{sf(h['per_label'][label]['f1'], a['per_label'][label]['f1'])}/"
        f"{sf(h['per_label'][label]['f1'], c['per_label'][label]['f1'])}；validation相应F1差为"
        f"{sf(vh['per_label'][label]['f1'], va['per_label'][label]['f1'])}/"
        f"{sf(vh['per_label'][label]['f1'], vc['per_label'][label]['f1'])}。路由选择本身不保证字号识别恢复。", ''])
    high = hs['modes']['high_recall']; low = hs['modes']['low_fpr']
    lines.extend([f"4. **高召回代价（H外层，对比F1模式）：**FN从{h['micro']['fn']}变为{high['micro']['fn']}，"
        f"减少{h['micro']['fn']-high['micro']['fn']}；FP从{h['micro']['fp']}变为{high['micro']['fp']}，"
        f"增加{high['micro']['fp']-h['micro']['fp']}；Micro Recall从{f(h['micro']['recall'])}变为"
        f"{f(high['micro']['recall'])}。这是风险提示模式，外层未必逐标签达到0.70。", '',
        f"5. **低误报代价（H外层，对比F1模式）：**FP从{h['micro']['fp']}变为{low['micro']['fp']}，"
        f"减少{h['micro']['fp']-low['micro']['fp']}；FN增加{low['micro']['fn']-h['micro']['fn']}；"
        f"Micro Recall从{f(h['micro']['recall'])}降至{f(low['micro']['recall'])}，"
        f"变化{sf(low['micro']['recall'], h['micro']['recall'])}。", ''])
    lines.extend([f"6. **页面误报与复核触发量：**H外层F1模式页面FPR={f(h['page']['page_false_positive_rate'])}，"
        f"A/C为{f(a['page']['page_false_positive_rate'])}/{f(c['page']['page_false_positive_rate'])}；"
        f"H触发{h['page']['alerted_pages']}页，A/C触发{a['page']['alerted_pages']}/{c['page']['alerted_pages']}页。"
        '三模式完整工作量见页面表，标签FP减少不能自动解释为页面触发减少。', ''])
    sdiff = []
    for source in source_names:
        sq = outer['H']['by_source'][source]['ap']['macro']
        sa = outer['A']['by_source'][source]['ap']['macro']
        sc = outer['C']['by_source'][source]['ap']['macro']
        sdiff.append(f"{source}：H−A {sf(sq, sa)}、H−C {sf(sq, sc)}")
    lines.extend(['7. **来源是否一致：**外层Macro AP差为' + '；'.join(sdiff) +
        '。各来源标签基率不同，Gemini拥挤正例尤其少，来源表和迁移压力测试共同限制结论。', ''])
    gdiff = []
    for label in LABELS:
        q = hs['ranking'][label]
        aq = aa['ranking'][label]
        cq = cc['ranking'][label]
        gdiff.append(f"{label}：配对AUC H={f(q['pooled_pair_auc'])}，H−A/H−C="
            f"{sf(q['pooled_pair_auc'], aq['pooled_pair_auc'])}/{sf(q['pooled_pair_auc'], cq['pooled_pair_auc'])}")
    lines.extend(['8. **组内区分是否改善：**' + '；'.join(gdiff) +
        '。组等权结果见表；整体AP和组内AUC可能相反，不能据AP认定已解决细粒度问题。', ''])
    lines.extend([f"9. **validation是否支持：**H Macro AP={f(validation['H']['ap']['macro'])}，"
        f"相对A/C {sf(validation['H']['ap']['macro'], validation['A']['ap']['macro'])}/"
        f"{sf(validation['H']['ap']['macro'], validation['C']['ap']['macro'])}；"
        f"F1模式Macro F1={f(vh['macro']['f1'])}，相对A/C "
        f"{sf(vh['macro']['f1'], va['macro']['f1'])}/{sf(vh['macro']['f1'], vc['macro']['f1'])}。"
        '这只能作为已多次观察validation上的探索性一致性检查，不能据此改路由或工作模式。', '',
        '10. **是否可作为独立PPTX研究候选：**工程上已形成冻结、可复现、可逐页验算的H候选，'
        '可以带着上述失败情况进入独立PPTX测试；现有证据不足以宣布替换原正式模型，'
        '也不足以由validation决定部署工作模式。独立测试应同时保留A/C对照并预先固定评价规则。', '',
        '11. **继续调分类器还是转向独立测试：**优先冻结本轮结果并开展独立PPTX测试与部署一致性验证，'
        '在独立证据到来前不扩大当前数据上的搜索空间。系统集成需另行验收并保留按需人工复核；'
        '本轮未接入智能体或自行开展下一阶段。', ''])
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text('\n'.join(lines), encoding='utf-8')



def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--validate-only',action='store_true');args=parser.parse_args()
    if not args.validate_only:require(not any(p.exists() for p in OUTPUTS),'Existing routing artifacts; refusing overwrite/repeated validation')
    started=old.now()
    rows,clip,png,sources,provenance,manifest=old.load_features()
    provenance['f2b_training_script_sha256']=old.file_sha(Path(old.__file__))
    previous=json.loads(old.CONFIG.read_text(encoding='utf-8'))
    for k in ('split_sha256','png_csv_sha256','png_recipe_payload_sha256','selected_clip_raw_values_sha256','selected_clip_l2_float64_sha256'):
        require(provenance[k]==previous['inputs'][k],'F-2B input identity changed: '+k)
    provenance['f2b_config_sha256']=old.file_sha(old.CONFIG)
    masks={s:np.asarray([r['split']==s for r in rows]) for s in ('train','validation')}
    tr=[r for r in rows if r['split']=='train'];vr=[r for r in rows if r['split']=='validation']
    tc,tg,ts=clip[masks['train']],png[masks['train']],sources[masks['train']]
    vc,vg,vs=clip[masks['validation']],png[masks['validation']],sources[masks['validation']]
    y,label_sha=old.load_labels(tr)
    require(label_sha==previous['train_label_manifest_sha256'],'Train label manifest changed')
    outer,outer_ids,outer_record=make_folds(y,tr,'outer_train')
    require(outer_record['fold_manifest_sha256']==old.FOLD_SHA,'Outer folds differ from frozen 4.3E/F-2B')
    for gid in set(r['group_id'] for r in tr):
        require(len(set(ts[[r['group_id']==gid for r in tr]]))==1,'A group mixes sources; cannot use clean LOSO')
    if args.validate_only:
        print(json.dumps({'status':'passed','train_pages':1512,'validation_pages':328,'outer_fold_sha':old.FOLD_SHA,'test_feature_values_materialized':0}));return
    CACHE.mkdir(parents=True,exist_ok=True);old.save_json(CACHE/'preregistered_protocol.json',PROTOCOL)
    p={n:np.full((1512,4),np.nan) for n in SCHEMES}
    t={n:{m:np.full((1512,4),np.nan) for m in MODES} for n in SCHEMES}
    routes=np.full((1512,4),'',dtype=object);outer_cv=[]
    with threadpool_limits(limits=1):
        for fold,(dev,hold) in enumerate(outer):
            dev_rows=[tr[i] for i in dev]
            selection=select_inner(tc[dev],tg[dev],y[dev],dev_rows,f'outer_{fold}_inner','nested_inner')
            pp,tt,rr,_,_,refit=refit_predict(tc[dev],tg[dev],y[dev],tc[hold],tg[hold],selection,'outer_refit')
            for n in SCHEMES:
                p[n][hold]=pp[n]
                for m in MODES:t[n][m][hold]=tt[n][m]
            routes[hold]=rr
            outer_cv.append({'fold_id':fold,'development_ids':[tr[i]['slide_id'] for i in dev],
                             'holdout_ids':[tr[i]['slide_id'] for i in hold],'inner':selection,'refit':refit})
        require(all(np.isfinite(p[n]).all() and all(np.isfinite(t[n][m]).all() for m in MODES) for n in SCHEMES),'Incomplete outer OOF')
        outer_results={n:evaluate(y,p[n],t[n],tr,ts) for n in SCHEMES}
        # Full-train choice is an independent, required inner CV run, not tuned from outer outcomes.
        final=select_inner(tc,tg,y,tr,'final_full_train_inner','final_inner')
        frozen={'protocol':PROTOCOL,'final_selection':final,'inputs':provenance,'source_sha256':old.file_sha(Path(__file__))}
        frozen_sha=old.digest(frozen);old.save_json(CACHE/'frozen_final_selection.json',frozen)
        frozen_at=old.now()
        uncertainty=grouped_bootstrap(y,p,t,tr)
        # One prespecified source-transfer stress test, after every final choice is frozen.
        lp={n:np.full((1512,4),np.nan) for n in SCHEMES}
        lt={n:{m:np.full((1512,4),np.nan) for m in MODES} for n in SCHEMES};lr=np.full((1512,4),'',dtype=object)
        lf=np.full(1512,-1,dtype=np.int8);loso_folds=[]
        for fold,source in enumerate(PROTOCOL['loso']['sources']):
            dev=np.flatnonzero(ts!=source);hold=np.flatnonzero(ts==source)
            require(not(set(tr[i]['group_id'] for i in dev)&set(tr[i]['group_id'] for i in hold)),'LOSO group leakage')
            sel=select_inner(tc[dev],tg[dev],y[dev],[tr[i] for i in dev],f'loso_without_{source}','loso_inner')
            pp,tt,rr,_,_,rec=refit_predict(tc[dev],tg[dev],y[dev],tc[hold],tg[hold],sel,'loso_refit')
            for n in SCHEMES:
                lp[n][hold]=pp[n]
                for m in MODES:lt[n][m][hold]=tt[n][m]
            lr[hold]=rr;lf[hold]=fold
            loso_folds.append({'held_source':source,'development_ids':[tr[i]['slide_id'] for i in dev],
                               'holdout_ids':[tr[i]['slide_id'] for i in hold],'inner':sel,'refit':rec})
        require((lf>=0).all() and all(np.isfinite(lp[n]).all() for n in SCHEMES),'Incomplete LOSO')
        loso_results={n:evaluate(y,lp[n],lt[n],tr,ts) for n in SCHEMES}
        require(old.digest(frozen)==frozen_sha,'Final choice changed after stress test')
        save_predictions(OUTER_CSV,tr,ts,y,p,t,routes,outer_ids,
                         {'probabilities':lp,'thresholds':lt,'routes':lr,'fold_ids':lf})
        validation_prediction_at=old.now()
        vp,vt,vroute,models,scalers,final_refit=refit_predict(tc,tg,y,vc,vg,final,'final_refit')
    require(old.digest(frozen)==frozen_sha,'Final choice changed during validation')
    vy,valid_label_sha=old.load_labels(vr)
    require(valid_label_sha==previous['validation_label_manifest_sha256'],'Validation label manifest changed')
    valid_results={n:evaluate(vy,vp[n],vt[n],vr,vs) for n in SCHEMES}
    save_predictions(VALID_CSV,vr,vs,vy,vp,vt,vroute,None)
    bundle={'status':'candidate; risk alerts only; not promoted or integrated','labels':list(LABELS),'slugs':list(SLUGS),
            'routing':final['routing'],'models':models,'png_scalers':scalers,
            'hybrid_thresholds':{m:selected_thresholds(final,'H')[m].tolist() for m in MODES},
            'selections':final['cv'],'feature_recipe':PROTOCOL,'clip_revision':old.REVISION,
            'png_recipe_payload_sha256':old.RECIPE_PAYLOAD_SHA,'split_sha256':old.SPLIT_SHA,
            'frozen_selection_sha256':frozen_sha,'inference_entrypoint':'scripts/optimize_fusion_routing.py:predict_hybrid',
            'inference_contract':'raw CLIP(512,), same-page PNG(10,) -> four probabilities, routes, thresholds and three alert modes'}
    temporary=MODEL.with_suffix('.joblib.part');joblib.dump(bundle,temporary,compress=3);os.replace(temporary,MODEL)
    # Exercise the serialized unified API on TRAIN features only, never a second validation prediction.
    smoke=predict_hybrid(tc[0],tg[0],MODEL)
    require(len(smoke['probabilities'])==4 and smoke['model_schemes']==[final['routing'][l]['selected'] for l in LABELS], 'Candidate interface smoke failed')
    expected={'nested_inner':648,'outer_refit':36,'final_inner':216,'final_refit':12,'loso_inner':648,'loso_refit':36}
    require(dict(FIT_COUNTS)==expected,'Unexpected fit budget '+str(FIT_COUNTS))
    versions={n:importlib.metadata.version(n) for n in ('numpy','scikit-learn','scipy','joblib','threadpoolctl')};versions['python']=platform.python_version()
    result={'protocol':PROTOCOL,'started_at':started,'finished_at':old.now(),'inputs':provenance,'versions':versions,
            'outer_folds':outer_record,'outer_cv':outer_cv,'outer_results':outer_results,
            'final_selection':final,'final_refit':final_refit,'frozen_selection_sha256':frozen_sha,
            'selection_frozen_at':frozen_at,'validation_prediction_started_at':validation_prediction_at,
            'validation_truth_loaded_after_predictions':True,'validation_results':valid_results,
            'loso':{'protocol':PROTOCOL['loso'],'folds':loso_folds,'pooled':loso_results},
            'bootstrap':uncertainty,'fit_counts':{**dict(FIT_COUNTS),'total':sum(FIT_COUNTS.values())},
            'convergence_warnings':[],'source_sha256':old.file_sha(Path(__file__)),
            'train_label_manifest_sha256':label_sha,'validation_label_manifest_sha256':valid_label_sha,
            'candidate_api_smoke':{'status':'passed','slide_id':tr[0]['slide_id'],'split':'train','result':smoke},
            'output_sha256':{str(q.relative_to(BASE)):old.file_sha(q) for q in (OUTER_CSV,VALID_CSV,MODEL)},
            'csv_roundtrip_checks':'passed; identity, truth, source, outer fold, probabilities, actual per-row thresholds/routes/alerts',
            'test_rows_accessed':0}
    old.save_json(CONFIG,result)
    result['output_sha256'][str(CONFIG.relative_to(BASE))]=old.file_sha(CONFIG)
    write_report(result)
    print(json.dumps({'status':'passed','fits':result['fit_counts'],
                      'final_routes':{l:q['selected'] for l,q in final['routing'].items()},
                      'outer_macro_ap':{n:outer_results[n]['ap']['macro'] for n in SCHEMES},
                      'validation_macro_ap':{n:valid_results[n]['ap']['macro'] for n in SCHEMES}},ensure_ascii=True),flush=True)


if __name__=='__main__':
    main()
