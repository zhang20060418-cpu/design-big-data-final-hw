"""Synthetic integration contracts; no formal benchmark or model tuning."""
from copy import deepcopy
import pytest
from pydantic import ValidationError
from app.config import DEFECT_TYPES
from app.schemas import ReviewOptions
from app.services.merge import attach_clip_reviews,merge
from app.services.report import verification_pipeline
from app.services.review_agent import plan_review
from app.services.visual_classifier import VisualClassifierService

D1,D2,D3,D4,D5,D6=DEFECT_TYPES

def native(defect=D2):
    return dict(issue_id='native-1',candidate_id='native-1',slide_index=1,defect_type=defect,
        raw_status='candidate',title='Native evidence',evidence_source='six_detector',evidence_summary='Measured region',
        bbox=dict(x=1.,y=1.,w=2.,h=1.),normalized_bbox=dict(x=.1,y=.1,w=.2,h=.1),
        element_id='s1-object2',related_slides=[1],sources=[dict(source='six_detector',summary='Native evidence')],
        review_status='unreviewed',recommendation='Check measured region')

def visual(alert=False):
    return dict(status='ok',slide_index=1,document_id='doc',
        probabilities={d:.1 for d in DEFECT_TYPES[:4]},alerts={d:int(alert) for d in DEFECT_TYPES[:4]},
        thresholds={d:.2 for d in DEFECT_TYPES[:4]},model_version='frozen-synthetic',work_point='f1',model_revision='fixed-revision')

def test_clip_negative_does_not_veto_native_and_reaches_planner():
    original=[native()];snapshot=deepcopy(original);v=[visual()]
    enriched=attach_clip_reviews(original,v,1)
    assert original==snapshot and enriched[0]['review_status']=='unreviewed'
    assert enriched[0]['raw_status']=='candidate' and enriched[0]['evidence_conflict']
    assert enriched[0]['clip_review']['alert']==0 and enriched[0]['bbox']==snapshot[0]['bbox']
    plan=plan_review(enriched,[],[dict(index=1,status='rendered',png='s0001.png')],
        dict(enable_mllm=True,review_mode='all',max_api_calls=1,max_review_pages=1),{},visual_results=v)
    item=next(i for i in plan['items'] if i['candidate_id']=='native-1')
    assert any(e.get('source')=='visual_model' or e.get('kind')=='clip_four_label_auxiliary' for e in item['evidence'])

def test_positive_clip_only_creates_page_candidate_before_llm():
    findings=attach_clip_reviews([], [visual(True)],1)
    assert len(findings)==4
    assert {f['defect_type'] for f in findings}==set(DEFECT_TYPES[:4])
    assert all(f['bbox'] is None and f.get('normalized_bbox') is None for f in findings)
    assert all(f['review_status']=='unreviewed' for f in findings)

def test_clip_attachment_is_idempotent_and_d5_d6_are_not_fabricated():
    original=[native(),native(D5),native(D6)]
    once=attach_clip_reviews(original,[visual()],1)
    twice=attach_clip_reviews(once,[visual()],1)
    assert once==twice
    assert all('clip_review' not in i for i in twice if i['defect_type'] in (D5,D6))
    assert sum(s['source']=='visual_model' for s in twice[0]['sources'])==1

def test_final_fusion_preserves_native_status_after_negative_clip():
    result,summary=merge([native()],[visual()],[],1,claims=[])
    assert len(result)==1 and result[0]['review_status']=='unreviewed'
    assert result[0]['clip_review']['alert']==0 and summary['visual_pages_scored']==1

def test_manual_clip_requires_r2_and_defaults_stay_off():
    assert ReviewOptions().enable_six_detectors and not ReviewOptions().enable_visual
    with pytest.raises(ValidationError,match='R2'):
        ReviewOptions(enable_visual=True,enable_six_detectors=False)

@pytest.mark.parametrize('mode,enabled,reviews,expected',[
    ('api',False,[],'not_requested'),('api',True,[],'not_checked'),
    ('mock',True,[dict(status='ok',mock=True,verdicts=[dict(judgement='uncertain')])],'mock_only'),
    ('api',True,[dict(status='error',mock=False,verdicts=[])],'partial')])
def test_mock_off_and_failures_are_never_final_confirmation(mode,enabled,reviews,expected):
    p=verification_pipeline(dict(enable_visual=True,enable_mllm=enabled,review_mode='all',llm_backend=mode),[],[],reviews,{})
    assert p['final_review_status']==expected

def test_batch_plugin_cannot_swap_page_identity_or_round_probability_boundary():
    service=VisualClassifierService();service._loaded=True
    service._manifest=dict(labels=DEFECT_TYPES[:4],thresholds={d:.13 for d in DEFECT_TYPES[:4]})
    service._batch_impl=lambda pages,cfg:[{**visual(),'slide_index':99}]
    r=service.predict_batch([dict(png='p.png',document_id='doc',slide_index=1)])
    assert r[0]['status']=='error' and not r[0]['probabilities']
    probs,alerts=service._normalise({d:.12999999 for d in DEFECT_TYPES[:4]})
    assert probs[D1]==.12999999 and alerts[D1]==0
