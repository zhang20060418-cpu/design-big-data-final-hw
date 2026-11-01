"""Evidence references are actual, candidate-scoped IDs, never prompt examples."""
from copy import deepcopy
import json

import pytest

from app.services import review_agent as agent
from tests.test_review_agent import claim, options, pages, selected, valid_data


def page_six_plan(tmp_path):
    rendered = pages(tmp_path, 6)
    opts = options(review_mode='manual', manual_pages=[6], max_api_calls=1)
    plan = agent.plan_review([], [claim(slide=6)], rendered, opts, {})
    items = [item for item in plan['items'] if item['candidate_id'] in plan['calls'][0]['candidate_ids']]
    return plan, items, opts


def reference_contract(system):
    raw = system.split('本次请求逐候选引用合同（由服务端实际发送的证据生成）：\n', 1)[1]
    return json.loads(raw.split('\n可信版本规则：\n', 1)[0])


def test_page_six_example_uses_real_page_instead_of_hardcoded_page_one(tmp_path):
    plan, items, opts = page_six_plan(tmp_path)
    images = agent._images(plan['calls'][0], items, tmp_path)
    messages = agent.PromptBuilder().build(items, images, opts)
    system = messages[0]['content']
    example = json.loads(system.split('\n严格输出结构：\n', 1)[1])['verdicts'][0]
    assert example['candidate_id'] == items[0]['candidate_id']
    assert example['evidence_refs'] == ['page:s0006']
    assert 'page:s0001' not in system
    assert 'page:s0001 或实际evidence_id' not in json.dumps(messages, ensure_ascii=False)
    contract = reference_contract(system)
    by_id = {entry['candidate_id']: entry for entry in contract}
    image_refs = {image['evidence_id'] for image in images}
    for item in items:
        allowed = image_refs | {e['evidence_id'] for e in item.get('evidence', [])}
        assert set(by_id[item['candidate_id']]['allowed_evidence_refs']) == allowed
        assert by_id[item['candidate_id']]['allowed_object_ids'] == item['object_ids']


def test_no_image_prompt_example_uses_only_available_native_evidence():
    item = claim(slide=6, evidence=[{'evidence_id': 'actual:measurement-6',
        'measurement_source': 'fixture', 'measurements': {'overlap': .8}}])
    messages = agent.PromptBuilder().build([item], [], options())
    example = json.loads(messages[0]['content'].split('\n严格输出结构：\n', 1)[1])['verdicts'][0]
    assert example['evidence_refs'] == ['actual:measurement-6']
    assert 'page:s0001' not in messages[0]['content']


@pytest.mark.parametrize('invalid_ref', ['page:s0001', 'guessed:region-6', 'page:s0006 或实际evidence_id'])
def test_unprovided_refs_are_rejected_with_inspectable_details(tmp_path, invalid_ref):
    _, items, _ = page_six_plan(tmp_path)
    payload = valid_data(items)
    payload['verdicts'][0]['evidence_refs'] = [invalid_ref]
    with pytest.raises(agent.EvidenceValidationError) as caught:
        agent.validate_review(payload, items, {'page:s0006'})
    details = caught.value.details
    assert caught.value.code == 'unknown_evidence_ref'
    assert details['candidate_id'] == items[0]['candidate_id']
    assert details['invalid_refs'] == [invalid_ref]
    assert 'page:s0006' in details['allowed_refs']
    assert items[0]['candidate_id'] in str(caught.value)
    assert invalid_ref in str(caught.value)


def test_other_candidates_native_measurement_is_not_silently_accepted(tmp_path):
    _, items, opts = page_six_plan(tmp_path)
    items = deepcopy(items)
    items[0]['evidence'] = [{'evidence_id': 'own:native'}]
    items[1]['evidence'] = [{'evidence_id': 'other:native'}]
    payload = valid_data(items)
    payload['verdicts'][0]['evidence_refs'] = ['other:native']
    with pytest.raises(agent.EvidenceValidationError) as caught:
        agent.validate_review(payload, items, {'page:s0006'})
    assert 'other:native' not in caught.value.details['allowed_refs']
    images = [{'evidence_id': 'page:s0006', 'slide_index': 6, 'kind': 'full_page', 'uri': 'fixture-image'}]
    contract = reference_contract(agent.PromptBuilder().build(items, images, opts)[0]['content'])
    assert 'other:native' not in contract[0]['allowed_evidence_refs']
    assert 'own:native' in contract[0]['allowed_evidence_refs']


def test_supplied_image_and_own_native_evidence_remain_valid(tmp_path):
    _, items, _ = page_six_plan(tmp_path)
    payload = valid_data(items)
    item = items[0]
    payload['verdicts'][0]['evidence_refs'] = ['page:s0006', item['evidence'][0]['evidence_id']]
    clean = agent.validate_review(payload, items, {'page:s0006'})
    assert len(clean['verdicts']) == len(items)


def test_api_failure_keeps_bad_ids_without_raw_completion_or_extra_requests(tmp_path, monkeypatch):
    plan, items, opts = page_six_plan(tmp_path)
    opts = {**opts, 'llm_backend': 'api'}
    settings = {'api_base_url': 'https://fixture.invalid/v1', 'api_model': 'fixture-model'}
    payload = valid_data(items)
    payload['verdicts'][0]['evidence_refs'] = ['page:s0001']
    calls = []
    def post(*args):
        calls.append(True)
        return 200, {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(payload), 'reasoning_content': 'private never persist'}}]}
    monkeypatch.setattr(agent.mllm, '_post_chat', post)
    monkeypatch.setattr(agent.mllm, 'validate_base_url', lambda *a, **k: a[0])
    result = agent.review_plan(plan, tmp_path, opts, settings, 'test-secret')[0]
    assert result['status'] == 'error' and result['error_code'] == 'unknown_evidence_ref'
    assert result['verdicts'] == []
    assert result['validation_errors'][0]['invalid_refs'] == ['page:s0001']
    assert result['validation_errors'][0]['candidate_id'] == items[0]['candidate_id']
    assert 'page:s0006' in result['validation_errors'][0]['allowed_refs']
    assert result['actual_http_calls'] == len(calls) == 1
    assert all(item['outcome'] == 'not_checked' for item in selected(plan))
    assert 'private never persist' not in json.dumps(result)


def test_parent_identity_still_belongs_to_server_not_model(tmp_path):
    _, items, _ = page_six_plan(tmp_path)
    items[0]['parent_candidate_id'] = 'actual-parent'
    payload = valid_data(items)
    clean = agent.validate_review(payload, items, {'page:s0006'})
    assert clean['verdicts'][0]['parent_candidate_id'] == 'actual-parent'
    payload['verdicts'][0]['parent_candidate_id'] = 'invented-parent'
    with pytest.raises(ValueError):
        agent.validate_review(payload, items, {'page:s0006'})
