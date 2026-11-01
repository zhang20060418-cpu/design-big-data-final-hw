"""API-aware defaults preserve explicit choices; never contact a provider."""
import pytest
from app.schemas import ReviewOptions
from app.services.review_defaults import resolve_review_defaults
from app.services.report import verification_pipeline

READY={'api_base_url':'https://example.invalid/v1','api_model':'configured-model'}

def test_configured_api_defaults_review_on_with_candidate_strategy():
    original=ReviewOptions();snapshot=original.model_dump()
    result=resolve_review_defaults(original,READY,'test-key')
    assert result.enable_mllm and result.llm_backend=='api'
    assert result.review_strategy=='selective_review'
    assert result.max_api_calls==6 and result.max_review_pages==6
    assert original.model_dump()==snapshot

@pytest.mark.parametrize('settings,key',[({},''),({},'k'),({'api_base_url':'https://example.invalid'},'k'),
    ({'api_model':'m'},'k'),(READY,''),(READY,'   ')])
def test_incomplete_configuration_defaults_off(settings,key):
    assert not resolve_review_defaults(ReviewOptions(),settings,key).enable_mllm

def test_manual_off_survives_settings_refresh_and_start_resolution():
    options=ReviewOptions(enable_mllm=False)
    resolved=resolve_review_defaults(options,READY,'test-key')
    assert resolved.mllm_user_choice is False and not resolved.enable_mllm
    assert not options.enable_mllm

def test_off_mode_is_respected_and_auto_default_does_not_choose_mock():
    result=resolve_review_defaults(ReviewOptions(review_mode='off'),READY,'key')
    assert result.enable_mllm is False and result.llm_backend=='api'

def test_explicit_legacy_enable_does_not_rewrite_legacy_response_contract():
    result=resolve_review_defaults(ReviewOptions(enable_mllm=True),READY,'key')
    assert result.enable_mllm and result.review_strategy is None

def test_gate_skip_is_distinct_from_success_or_configuration_off():
    result=verification_pipeline({'enable_mllm':True,'review_mode':'risk_only'},[],[],[],{},
        gate={'skipped_by_user':True,'warning':'跳过可能误诊或漏检'})
    assert result['final_review_status']=='user_skipped'
    assert result['llm_actual_http_calls']==0 and result['user_skipped_review']
    assert '误诊' in result['skip_warning']


def test_auto_marker_replaces_old_default_false_when_api_ready():
    result=resolve_review_defaults(ReviewOptions(enable_mllm=False,mllm_user_choice=None),READY,'key')
    assert result.enable_mllm is True and result.mllm_user_choice is None
    assert result.review_strategy=='selective_review'


def test_user_off_marker_survives_serialization_and_later_start():
    original=resolve_review_defaults(ReviewOptions(enable_mllm=True,mllm_user_choice=False),READY,'key')
    restored=ReviewOptions.model_validate(original.model_dump())
    assert not resolve_review_defaults(restored,READY,'key').enable_mllm
    assert restored.mllm_user_choice is False


def test_auto_marker_tracks_later_configuration_without_becoming_explicit():
    first=resolve_review_defaults(ReviewOptions(mllm_user_choice=None),{},'')
    assert not first.enable_mllm
    later=resolve_review_defaults(ReviewOptions.model_validate(first.model_dump()),READY,'key')
    assert later.enable_mllm and later.mllm_user_choice is None
