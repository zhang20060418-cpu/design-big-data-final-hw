"""State, race and HTTP tests use no models or external APIs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.services import review_gate as gate


def test_read_before_first_pass_does_not_create_any_gate_or_directory(tmp_path):
    directory=tmp_path/'task'
    before=gate.read(directory)
    assert before['state']=='not_ready' and not before['can_skip']
    assert not directory.exists()


def test_default_five_second_deadline_and_refresh_never_resets_it(tmp_path,monkeypatch):
    monkeypatch.delenv('PPT_AGENT_REVIEW_GATE_SECONDS',raising=False)
    clock=[1000.]
    monkeypatch.setattr(gate.time,'time',lambda:clock[0])
    first=gate.prepare(tmp_path,True)
    assert first['countdown_seconds']==5 and first['remaining_ms']==5000 and first['can_skip']
    original=(tmp_path/'analysis/review_gate.json').read_bytes()
    clock[0]+=1.75
    refreshed=gate.read(tmp_path)
    replay=gate.prepare(tmp_path,True,delay_seconds=55)
    assert replay['deadline_at']==first['deadline_at']==refreshed['deadline_at']
    assert replay['remaining_ms']==3250
    assert (tmp_path/'analysis/review_gate.json').read_bytes()==original
    assert 'execution_token' not in replay


@pytest.mark.parametrize('reason',['user_disabled','missing_api_configuration','review_mode_off'])
def test_disabled_gates_never_wait_or_claim(tmp_path,reason):
    start=time.monotonic()
    result=gate.wait_and_claim(tmp_path,False,reason=reason)
    assert time.monotonic()-start<.5
    assert not result['execute'] and result['gate']['state']=='disabled'
    assert result['gate']['skip_reason']==reason and result['gate']['remaining_ms']==0


def test_skip_is_explicit_persistent_idempotent_and_has_risk_warning(tmp_path):
    first=gate.prepare(tmp_path,True,delay_seconds=5)
    skipped=gate.skip(tmp_path)
    assert skipped['gate_id']==first['gate_id'] and skipped['state']=='skipped'
    assert skipped['skipped_by_user'] and not skipped['can_skip']
    assert '误诊' in skipped['warning'] and '漏检' in skipped['warning']
    assert gate.skip(tmp_path)==skipped
    decision=gate.wait_and_claim(tmp_path,True)
    assert decision['execute'] is False and decision['reason']=='skipped'
    assert gate.read(tmp_path)['state']=='skipped'


def test_expired_window_cannot_be_skipped_even_before_worker_claim(tmp_path,monkeypatch):
    clock=[100.]
    monkeypatch.setattr(gate.time,'time',lambda:clock[0])
    gate.prepare(tmp_path,True,delay_seconds=5)
    clock[0]=105.
    assert not gate.read(tmp_path)['can_skip']
    with pytest.raises(gate.GateConflict,match='等待窗口已结束'):
        gate.skip(tmp_path)
    claimed=gate.wait_and_claim(tmp_path,True)
    assert claimed['execute'] and claimed['gate']['state']=='executing'
    with pytest.raises(gate.GateConflict):gate.skip(tmp_path)


def test_wait_calls_tick_until_deadline_then_authorizes_once(tmp_path):
    ticks=[]
    start=time.monotonic()
    first=gate.wait_and_claim(tmp_path,True,delay_seconds=.05,on_tick=ticks.append,poll_s=.01)
    assert time.monotonic()-start>=.045 and ticks
    assert all(item['state']=='waiting' for item in ticks)
    assert first['execute'] and first['execution_token']
    assert gate.wait_and_claim(tmp_path,True)['execute'] is False


def test_tick_can_skip_without_deadlock_or_api_execution(tmp_path):
    result=gate.wait_and_claim(tmp_path,True,delay_seconds=5,on_tick=lambda _:gate.skip(tmp_path))
    assert not result['execute'] and result['gate']['state']=='skipped'


@pytest.mark.parametrize('outcome',['completed','failed','partial','mock_only','not_checked'])
def test_completion_requires_private_claim_token_and_never_replays(tmp_path,outcome):
    claimed=gate.wait_and_claim(tmp_path,True,delay_seconds=0)
    with pytest.raises(gate.GateConflict,match='令牌不匹配'):
        gate.complete(tmp_path,'not-owner')
    completed=gate.complete(tmp_path,claimed['execution_token'],outcome=outcome)
    assert completed['state']=='completed' and completed['outcome']==outcome
    assert completed['completed_at']
    assert not gate.wait_and_claim(tmp_path,True)['execute']
    assert gate.complete(tmp_path,claimed['execution_token'],outcome=outcome)==completed


def test_interrupted_execution_is_fail_closed_across_service_reentry(tmp_path):
    first=gate.wait_and_claim(tmp_path,True,delay_seconds=0)
    # Nothing calls complete: the API might have been sent before a crash.
    again=gate.wait_and_claim(tmp_path,True,delay_seconds=0)
    assert first['execute'] and not again['execute']
    assert again['reason']=='execution_already_started'
    assert again['gate']['state']=='executing' and not again['gate']['can_skip']


def test_many_worker_threads_have_one_execution_winner(tmp_path):
    gate.prepare(tmp_path,True,delay_seconds=0)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda _:gate.wait_and_claim(tmp_path,True),range(24)))
    assert sum(r['execute'] for r in results)==1
    assert len({r['execution_token'] for r in results if r['execute']})==1


def test_independent_processes_have_one_execution_winner(tmp_path):
    gate.prepare(tmp_path,True,delay_seconds=0)
    backend=Path(__file__).resolve().parents[1]
    code='from app.services.review_gate import wait_and_claim; import sys,json; print(json.dumps(wait_and_claim(sys.argv[1],True)))'
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    workers=[subprocess.Popen([sys.executable,'-B','-c',code,str(tmp_path)],cwd=backend,env=env,
             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8') for _ in range(2)]
    results=[]
    for process in workers:
        stdout,stderr=process.communicate(timeout=15)
        assert process.returncode==0,stderr
        results.append(json.loads(stdout))
    assert sum(r['execute'] for r in results)==1


@pytest.mark.parametrize('value',['{broken','null','{"state":"waiting"}'])
def test_corrupted_gate_does_not_reset_or_authorize(tmp_path,value):
    folder=tmp_path/'analysis';folder.mkdir()
    saved=folder/'review_gate.json';saved.write_text(value,encoding='utf8')
    with pytest.raises(gate.GateStateError):gate.read(tmp_path)
    with pytest.raises(gate.GateStateError):gate.prepare(tmp_path,True)
    assert saved.read_text(encoding='utf8')==value


def test_qa_delay_override_is_explicit_but_default_is_not_changed(tmp_path,monkeypatch):
    monkeypatch.setenv('PPT_AGENT_REVIEW_GATE_SECONDS','0')
    assert gate.wait_and_claim(tmp_path,True)['execute']
    assert gate.read(tmp_path)['countdown_seconds']==0


@pytest.mark.parametrize('value',['nan','inf','-1','61'])
def test_invalid_delay_is_rejected(tmp_path,value):
    with pytest.raises(ValueError):gate.prepare(tmp_path,True,delay_seconds=float(value))
    assert not (tmp_path/'analysis/review_gate.json').exists()


def api_task():
    from app.storage import real_store
    from app.routers.common import task_dir
    task=real_store.create(name='synthetic gate test',options={'enable_mllm':True},source_name='fixture.pptx',size_bytes=0)
    real_store.update(task['id'],status='running')
    task=real_store.get(task['id'])
    return task,task_dir(task)


def test_gate_routes_poll_read_only_skip_and_task_detail(client):
    task,directory=api_task();tid=task['id']
    first=client.get(f'/api/tasks/{tid}/review-gate')
    assert first.status_code==200 and first.json()['state']=='not_ready'
    assert first.json()['result_ready'] is False
    assert not (directory/'analysis/review_gate.json').exists()
    gate.prepare(directory,True,delay_seconds=5)
    second=client.get(f'/api/tasks/{tid}')
    assert second.json()['review_gate']['can_skip']
    skipped=client.post(f'/api/tasks/{tid}/skip-review')
    assert skipped.status_code==200 and skipped.json()['state']=='skipped'
    assert skipped.json()['task_status']=='running' and not skipped.json()['result_ready']
    assert client.post(f'/api/tasks/{tid}/skip-review').status_code==200
    assert client.get(f'/api/tasks/{tid}').json()['review_gate']['skipped_by_user']


def test_gate_api_rejects_late_skip_and_bad_state(client):
    task,directory=api_task();tid=task['id']
    gate.wait_and_claim(directory,True,delay_seconds=0)
    refused=client.post(f'/api/tasks/{tid}/skip-review')
    assert refused.status_code==409 and refused.json()['error']['code']=='review_already_started'
    assert gate.read(directory)['state']=='executing'


def test_gate_api_denies_nonlocal_skip(client):
    task,directory=api_task();gate.prepare(directory,True,delay_seconds=5)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app,client=('203.0.113.9',12345)) as remote:
        response=remote.post(f"/api/tasks/{task['id']}/skip-review")
        assert response.status_code==403
    assert gate.read(directory)['state']=='waiting'


def test_early_skip_requires_authorization_and_survives_worker_prepare(tmp_path):
    with pytest.raises(gate.GateConflict):
        gate.skip(tmp_path)
    assert not (tmp_path/'analysis/review_gate.json').exists()
    skipped=gate.skip(tmp_path,allow_before_ready=True)
    assert skipped['state']=='skipped' and skipped['skipped_before_review']
    assert skipped['skip_reason']=='user_skipped_before_review'
    assert skipped['deadline_at'] is None and skipped['execution_started_at'] is None
    assert skipped['warning']==gate.WARNING
    assert gate.prepare(tmp_path,True,delay_seconds=5)['gate_id']==skipped['gate_id']
    decision=gate.wait_and_claim(tmp_path,True,delay_seconds=5)
    assert decision['execute'] is False and decision['gate']['state']=='skipped'
    assert gate.skip(tmp_path)==skipped


def test_early_skip_cannot_cancel_already_authorized_request(tmp_path):
    claimed=gate.wait_and_claim(tmp_path,True,delay_seconds=0)
    with pytest.raises(gate.GateConflict):
        gate.skip(tmp_path,allow_before_ready=True)
    assert claimed['execute'] and gate.read(tmp_path)['state']=='executing'


def test_early_skip_and_worker_prepare_race_has_one_winner(tmp_path):
    import threading
    for index in range(10):
        directory=tmp_path/str(index)
        barrier=threading.Barrier(2)
        def worker():
            barrier.wait()
            return gate.wait_and_claim(directory,True,delay_seconds=0)
        def user():
            barrier.wait()
            try:
                return gate.skip(directory,allow_before_ready=True)
            except gate.GateConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(worker)
            skip_result=pool.submit(user).result(timeout=5)
            execution=future.result(timeout=5)
        final=gate.read(directory)
        if skip_result is not None:
            assert final['state']=='skipped' and not execution['execute']
        else:
            assert final['state']=='executing' and execution['execute']


def test_early_skip_http_is_available_before_stage_and_does_not_finish_task(client):
    from app.storage import real_store
    task,directory=api_task();tid=task['id']
    public=client.get(f'/api/tasks/{tid}/review-gate').json()
    assert public['state']=='not_ready' and public['can_skip']
    assert public['skip_available_before_stage']
    assert not (directory/'analysis/review_gate.json').exists()
    assert client.get(f'/api/tasks/{tid}').json()['review_gate']['can_skip']
    skipped=client.post(f'/api/tasks/{tid}/skip-review')
    assert skipped.status_code==200
    assert skipped.json()['skipped_before_review'] and not skipped.json()['result_ready']
    assert real_store.get(tid)['status']=='running'
    assert not (directory/'report/report.json').exists()
    assert client.post(f'/api/tasks/{tid}/skip-review').status_code==200
    assert not gate.wait_and_claim(directory,True,delay_seconds=5)['execute']


@pytest.mark.parametrize('task_status,enabled,review_mode,stage_status',[
    ('ready',True,'risk_only','pending'),
    ('completed',True,'risk_only','pending'),
    ('running',False,'risk_only','pending'),
    ('running',True,'off','pending'),
    ('running',True,'risk_only','running'),
    ('running',True,'risk_only','done'),
    ('running',True,'risk_only','failed'),
])
def test_early_skip_http_rejects_disabled_or_already_started_tasks(
        client,task_status,enabled,review_mode,stage_status):
    from app.storage import real_store
    task,directory=api_task();tid=task['id']
    stages=task['stages']
    for stage in stages:
        if stage['key']=='mllm':stage['status']=stage_status
    real_store.update(tid,status=task_status,stages_json=stages,
        options_json={'enable_mllm':enabled,'review_mode':review_mode,'mllm_user_choice':enabled})
    response=client.get(f'/api/tasks/{tid}/review-gate')
    assert not response.json()['can_skip']
    assert client.post(f'/api/tasks/{tid}/skip-review').status_code==409
    assert not (directory/'analysis/review_gate.json').exists()


def test_old_failed_review_details_are_visible_without_rewriting_history(client):
    from app.storage import real_store
    task,directory=api_task();tid=task['id']
    stages=task['stages']
    for stage in stages:
        if stage['key']=='mllm':stage.update(status='failed',message='按需复核执行完成。',detail={})
    real_store.update(tid,status='completed_with_errors',stages_json=stages)
    folder=directory/'analysis';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'reviews.json'
    saved=json.dumps([{'slide_index':2,'status':'error','error_code':'http_502','error':'接口返回HTTP 502'},
        {'slide_index':3,'status':'ok','error':None},
        {'slide_index':4,'status':'error','error_code':'timeout','error':'请求超时'}],ensure_ascii=False)
    path.write_text(saved,encoding='utf8')
    for suffix in ('','/stages'):
        public=client.get(f'/api/tasks/{tid}{suffix}').json()
        reviewed=next(stage for stage in public['stages'] if stage['key']=='mllm')
        assert len(reviewed['detail']['review_errors'])==2
        assert reviewed['detail']['review_errors'][0]['error_code']=='http_502'
        assert 'HTTP 502' in reviewed['message'] and '未形成复核结论' in reviewed['message']
    assert path.read_text(encoding='utf8')==saved
    assert real_store.get(tid)['stages']==stages


def test_old_unstarted_task_projects_configured_default_without_persisting(client,monkeypatch):
    from app.routers import tasks
    from app.storage import real_store
    from types import SimpleNamespace
    task,_=api_task();tid=task['id']
    old_options={'enable_mllm':False,'review_mode':'risk_only'}
    real_store.update(tid,status='ready',options_json=old_options)
    monkeypatch.setattr(tasks,'settings_store',SimpleNamespace(
        all=lambda:{'api_base_url':'https://example.invalid/v1','api_model':'test-only'},
        get_api_key=lambda:'test-only'))
    public=client.get(f'/api/tasks/{tid}').json()['options']
    assert public['enable_mllm'] is True and public['mllm_user_choice'] is None
    assert real_store.get(tid)['options']==old_options
    marked={**old_options,'mllm_user_choice':False}
    real_store.update(tid,options_json=marked)
    assert client.get(f'/api/tasks/{tid}').json()['options']['enable_mllm'] is False


def test_new_automatic_upload_follows_later_api_configuration_without_db_edit(client,monkeypatch):
    from app.routers import tasks
    from app.storage import real_store
    from types import SimpleNamespace
    task,_=api_task();tid=task['id']
    saved={'enable_mllm':False,'review_mode':'risk_only','mllm_user_choice':None}
    real_store.update(tid,status='ready',options_json=saved)
    ready=[False]
    monkeypatch.setattr(tasks,'settings_store',SimpleNamespace(
        all=lambda:{'api_base_url':'https://example.invalid/v1','api_model':'test-only'},
        get_api_key=lambda:'test-only' if ready[0] else ''))
    assert client.get(f'/api/tasks/{tid}').json()['options']['enable_mllm'] is False
    ready[0]=True
    options=client.get(f'/api/tasks/{tid}').json()['options']
    assert options['enable_mllm'] is True and options['mllm_user_choice'] is None
    assert real_store.get(tid)['options']==saved
