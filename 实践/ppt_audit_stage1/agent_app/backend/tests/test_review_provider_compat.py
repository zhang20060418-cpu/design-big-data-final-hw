"""Provider response failures remain explicit, with no extra API retries."""
import json

import pytest

from app.services import mllm, review_agent
from tests.test_review_agent import claim, pages, options


def test_official_deepseek_structured_review_disables_implicit_thinking():
    original={'model':'deepseek-flash','max_tokens':4096}
    prepared=mllm.chat_payload('https://api.deepseek.com',original)
    assert prepared['thinking']=={'type':'disabled'}
    assert 'thinking' not in original


@pytest.mark.parametrize('base,extra',[
    ('https://other-provider.invalid/v1',{}),
    ('https://api.deepseek.com',{'thinking':{'type':'enabled'}}),
    ('https://api.deepseek.com',{'reasoning_effort':'low'}),
])
def test_other_providers_and_explicit_preferences_are_unchanged(base,extra):
    payload={'model':'deepseek-flash',**extra}
    assert mllm.chat_payload(base,payload)==payload


@pytest.mark.parametrize('finish,content,reasoning,code',[
    ('length','','private reasoning text','output_truncated'),
    ('length','{"verdicts":[','', 'output_truncated'),
    ('stop','     ','','empty_final_response'),
    ('stop','','private reasoning text','empty_final_response'),
])
def test_no_final_json_is_never_treated_as_success(finish,content,reasoning,code):
    body={'choices':[{'finish_reason':finish,'message':{'content':content,'reasoning_content':reasoning}}]}
    with pytest.raises(mllm.ModelOutputError) as caught:
        mllm.completion_text(body)
    assert caught.value.code==code
    assert 'private reasoning text' not in json.dumps(caught.value.metadata)
    assert 'private reasoning text' not in str(caught.value)


def test_final_json_uses_content_and_never_reasoning():
    text,meta=mllm.completion_text({'choices':[{'finish_reason':'stop','message':{
        'content':'{"verdicts":[]}','reasoning_content':'private reasoning text'}}]})
    assert json.loads(text)=={'verdicts':[]}
    assert meta['reasoning_present'] and 'private reasoning text' not in json.dumps(meta)


def test_gateway_error_keeps_safe_provider_reason_and_redacts_key():
    message=mllm.http_error_message(502,{'error':{'message':'upstream timeout key=secret-test'}},'secret-test')
    assert '502' in message and 'upstream timeout' in message and 'secret-test' not in message


def test_review_records_truncation_without_automatically_spending_more_calls(tmp_path,monkeypatch):
    opts=options(llm_backend='api')
    settings={'api_base_url':'https://api.deepseek.com','api_model':'deepseek-flash'}
    plan=review_agent.plan_review([], [claim()], pages(tmp_path), opts,settings)
    calls=[]
    def post(*args):
        calls.append(True)
        return 200,{'choices':[{'finish_reason':'length','message':{'content':'','reasoning_content':'never persist me'}}]}
    monkeypatch.setattr(mllm,'_post_chat',post)
    monkeypatch.setattr(mllm,'validate_base_url',lambda *a,**k:a[0])
    result=review_agent.review_plan(plan,tmp_path,opts,settings,'test-key')[0]
    assert result['error_code']=='output_truncated' and result['verdicts']==[]
    assert result['response_metadata']['finish_reason']=='length'
    assert len(calls)==result['actual_http_calls']==1
    assert 'never persist me' not in json.dumps(result)
