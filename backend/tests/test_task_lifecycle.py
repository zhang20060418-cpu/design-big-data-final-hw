import json
import os

from app.storage import TaskStore
from app.services import task_lifecycle as lifecycle


def task(tmp_path,status='running'):
    store=TaskStore(tmp_path/'test.db','real')
    record=store.create(name='interruption fixture',options={},source_name='fixture.pptx',size_bytes=1)
    store.update(record['id'],status=status)
    directory=tmp_path/'tasks'/record['id']
    directory.mkdir(parents=True)
    return store,record,directory


def test_startup_recovers_orphan_and_keeps_unknown_api_gate(tmp_path):
    store,record,directory=task(tmp_path)
    gate=directory/'analysis/review_gate.json';gate.parent.mkdir()
    gate.write_text(json.dumps({'state':'executing','private':'unchanged'}),encoding='utf8')
    original=gate.read_bytes()
    result=lifecycle.reconcile_active_tasks([store],data_dir=tmp_path)
    assert result['interrupted_task_ids']==[record['id']]
    updated=store.get(record['id'])
    assert updated['status']=='failed' and updated['error'].startswith('interrupted:')
    assert gate.read_bytes()==original
    assert 'API' in updated['error']


def test_live_worker_is_not_interrupted_and_owner_records_creation_identity(tmp_path):
    store,record,directory=task(tmp_path)
    lifecycle.write_owner(directory,phase='audit')
    owner=json.loads((directory/'owner.json').read_text(encoding='utf8'))
    assert owner['pid']==os.getpid()
    result=lifecycle.reconcile_active_tasks([store],data_dir=tmp_path)
    assert result['live_owners_preserved']==1
    assert store.get(record['id'])['status']=='running'


def test_pid_reuse_does_not_keep_abandoned_task_running(tmp_path,monkeypatch):
    store,record,directory=task(tmp_path,'queued')
    (directory/'owner.json').write_text(json.dumps({'pid':123,'process_start_token':'old'}),encoding='utf8')
    monkeypatch.setattr(lifecycle,'process_identity',lambda pid:('alive','new'))
    lifecycle.reconcile_active_tasks([store],data_dir=tmp_path)
    assert store.get(record['id'])['status']=='failed'


def test_unknown_process_permission_does_not_interrupt_possible_live_owner(tmp_path,monkeypatch):
    store,record,directory=task(tmp_path)
    monkeypatch.setattr(lifecycle,'process_identity',lambda pid:('unknown',None))
    result=lifecycle.reconcile_active_tasks([store],data_dir=tmp_path)
    assert result['unknown_owners_preserved']==1
    assert store.get(record['id'])['status']=='running'


def test_completed_history_is_never_changed_by_startup(tmp_path):
    store,record,directory=task(tmp_path,'completed')
    before=store.get(record['id'])
    lifecycle.reconcile_active_tasks([store],data_dir=tmp_path)
    assert store.get(record['id'])==before
