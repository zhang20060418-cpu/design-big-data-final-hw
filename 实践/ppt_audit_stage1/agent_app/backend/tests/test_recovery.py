"""New-attempt recovery preserves history, validates reuse and debounces cost."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import shutil

from PIL import Image
import pytest

from app.config import DEFECT_TYPES
from app.schemas import ReviewOptions
from app.services import recovery
from app.services.pptx_parser import parse_document, sha256_file
from app.storage import real_store, default_stages


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')


@pytest.fixture
def parent_task(fixture_pptx):
    options=ReviewOptions(enable_mllm=True,mllm_user_choice=True,review_strategy='full_review',llm_backend='mock').model_dump()
    parent=real_store.create(name='recovery parent', options=options,source_name=fixture_pptx.name,size_bytes=fixture_pptx.stat().st_size)
    root=recovery.directory(parent)
    (root/'source').mkdir(parents=True)
    shutil.copy2(fixture_pptx,root/'source/source.pptx')
    digest=sha256_file(fixture_pptx)
    document,detail=parse_document(fixture_pptx)
    document['sha256']=digest
    count=document['slide_count']
    write(root/'parse/slides_detail.json',detail)
    slides=[]
    for i in range(1,count+1):
        png=root/f'renders/s{i:04d}.png';png.parent.mkdir(exist_ok=True)
        Image.new('RGB',(160,90),'white').save(png)
        slides.append({'index':i,'status':'rendered','png':png.name,'thumb':None,'width':160,'height':90,'sha256':sha256_file(png)})
    write(root/'renders/slides.json',{'result':{'ok':True,'slides':slides,'meta':{
        'source_sha256_before':digest,'source_sha256_after':digest,
        'six_measurement':{'native_contexts_path':str(root/'renders/native_contexts.json')}}}})
    write(root/'renders/native_contexts.json',{'source':str(root/'source/source.pptx')})
    claims=[{'candidate_id':f'c-{i}-{d}','document_id':parent['id'],'slide_index':i,'defect_type':d} for i in range(1,count+1) for d in DEFECT_TYPES]
    write(root/'analysis/claims.json',{'claims':claims,'capability':{'version':'fixture'}})
    write(root/'analysis/observations.json',[])
    write(root/'analysis/visual_results.json',{'enabled':False,'results':[],'stage_status':'skipped'})
    calls=[{'call_id':f'call-{i}','candidate_ids':[f'c-{i}-{DEFECT_TYPES[0]}'],'images':[{'slide_index':i,'png_path':str(root/f'renders/s{i:04d}.png')}]} for i in [1,2]]
    write(root/'analysis/review_plan.json',{'calls':calls,'items':[],'rule_version':'fixture'})
    write(root/'analysis/reviews.json',[
        {'call_id':'call-1','status':'ok','actual_http_calls':1,'verdicts':[{'candidate_id':f'c-1-{DEFECT_TYPES[0]}','judgement':'uncertain'}]},
        {'call_id':'call-2','status':'error','actual_http_calls':1,'verdicts':[],'error':'invalid reference'}])
    write(root/'analysis/review_gate.json',{'state':'completed','execution_token':'private-parent-token'})
    write(root/'report/report.json',{'reviews':[],'risks':[{'text':'original risk'}]})
    stages=default_stages()
    for stage in stages:
        stage['status']='failed' if stage['key']=='mllm' else 'skipped' if stage['key']=='visual' else 'done'
    real_store.update(parent['id'],status='completed_with_errors',document_json=document,
        source_sha256=digest,stages_json=stages,slide_count=count)
    return real_store.get(parent['id'])


def test_continue_preserves_parent_and_copies_only_verified_artifacts(parent_task):
    original=recovery.directory(parent_task)
    preserved={str(p.relative_to(original)):sha256_file(p) for p in original.rglob('*') if p.is_file()}
    response,created=recovery.create_attempt(parent_task,'continue',idempotency_key='one')
    assert created and response['task_id']!=parent_task['id']
    child=real_store.get(response['task_id']);root=recovery.directory(child)
    context=json.loads((root/'analysis/recovery_context.json').read_text(encoding='utf8'))
    assert context['screening_reusable'] and set(['validate','parse','render','structure'])<=set(context['reused_stages'])
    assert len(context['cached_reviews'])==1 and response['pending_review_calls']==1
    assert context['parent_usage']['actual_http_calls']==2
    assert not (root/'analysis/review_gate.json').exists()
    assert not (root/'analysis/reviews.json').exists() and not (root/'report').exists()
    assert child['document']['sha256']==parent_task['source_sha256']
    assert sha256_file(root/'renders/s0001.png')==sha256_file(original/'renders/s0001.png')
    assert context['cached_review_plan']['calls'][0]['images'][0]['png_path']==str(__import__('pathlib').Path('renders/s0001.png'))
    native=json.loads((root/'renders/slides.json').read_text(encoding='utf8'))['result']['meta']['six_measurement']['native_contexts_path']
    assert native==str(root/'renders/native_contexts.json')
    assert all(sha256_file(original/name)==digest for name,digest in preserved.items())


def test_restart_does_not_copy_screening_or_reports(parent_task):
    response,_=recovery.create_attempt(parent_task,'restart')
    child=real_store.get(response['id']);root=recovery.directory(child)
    assert child['document'] is None
    assert not (root/'renders').exists() and not (root/'report').exists()
    context=json.loads((root/'analysis/recovery_context.json').read_text(encoding='utf8'))
    assert context['reused_stages']==[] and context['cached_reviews']==[]
    assert context['action']=='restart'


@pytest.mark.parametrize('corruption',['png','size','hash','path','incomplete'])
def test_invalid_render_never_reuses_corrupt_or_incomplete_evidence(parent_task,corruption):
    root=recovery.directory(parent_task)
    path=root/'renders/slides.json';payload=json.loads(path.read_text(encoding='utf8'))
    if corruption=='png':(root/'renders/s0001.png').write_bytes(b'broken image')
    elif corruption=='size':payload['result']['slides'][0]['width']=200
    elif corruption=='hash':payload['result']['slides'][0]['sha256']='0'*64
    elif corruption=='path':payload['result']['slides'][0]['png']='../source/source.pptx'
    else:payload['result']['slides'][0]['status']='failed'
    write(path,payload)
    response,_=recovery.create_attempt(parent_task,'continue')
    child=real_store.get(response['id']);context=json.loads((recovery.directory(child)/'analysis/recovery_context.json').read_text(encoding='utf8'))
    assert child['document'] is None and not context['screening_reusable']
    assert context['cached_reviews']==[] and context['warnings']


def test_source_hash_change_stops_before_new_task_creation(parent_task):
    (recovery.directory(parent_task)/'source/source.pptx').write_bytes(b'changed')
    before=real_store.count()
    with pytest.raises(recovery.RecoveryError,match='摘要'):
        recovery.create_attempt(parent_task,'restart')
    assert real_store.count()==before


def test_same_idempotency_key_in_threads_creates_one_child(parent_task):
    before=real_store.count()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:recovery.create_attempt(parent_task,'continue',idempotency_key='same-key'),range(6)))
    assert real_store.count()==before+1
    assert len({r[0]['task_id'] for r in results})==1
    assert sum(created for _,created in results)==1


def test_new_request_cannot_create_parallel_child(parent_task):
    recovery.create_attempt(parent_task,'continue',idempotency_key='first')
    with pytest.raises(recovery.RecoveryError,match='已有恢复任务'):
        recovery.create_attempt(parent_task,'restart',idempotency_key='second')


def test_corrupted_registry_is_not_silently_replaced(parent_task):
    root=recovery.directory(parent_task)
    path=root/'recovery/attempts.json';path.parent.mkdir()
    path.write_text('{broken',encoding='utf8')
    with pytest.raises(recovery.RecoveryError,match='恢复记录损坏'):
        recovery.create_attempt(parent_task,'continue')


def test_same_key_cannot_change_recovery_action(parent_task):
    recovery.create_attempt(parent_task,'continue',idempotency_key='fixed')
    with pytest.raises(recovery.RecoveryError,match='不同恢复操作'):
        recovery.create_attempt(parent_task,'restart',idempotency_key='fixed')


def test_skip_resume_enables_review_but_keeps_clip_choice(parent_task):
    options={**parent_task['options'],'enable_mllm':False,'mllm_user_choice':False,'review_mode':'off','enable_visual':False}
    real_store.update(parent_task['id'],options_json=options)
    parent_task=real_store.get(parent_task['id'])
    response,_=recovery.create_attempt(parent_task,'continue')
    child=real_store.get(response['id'])
    assert child['options']['enable_mllm'] and child['options']['mllm_user_choice'] is True
    assert child['options']['review_mode']=='risk_only' and not child['options']['enable_visual']


@pytest.mark.parametrize('status',['queued','running','preparing','ready','draft'])
def test_active_or_unstarted_parent_cannot_spawn_recovery(parent_task,status):
    real_store.update(parent_task['id'],status=status)
    parent_task=real_store.get(parent_task['id'])
    assert not recovery.availability(parent_task)['can_restart']
    with pytest.raises(recovery.RecoveryError):
        recovery.create_attempt(parent_task,'restart')


def test_api_idempotency_submits_once_and_get_is_read_only(client,parent_task,monkeypatch):
    from app.routers import tasks
    submits=[]
    monkeypatch.setattr(tasks.orchestrator,'submit',lambda *args:submits.append(args))
    root=recovery.directory(parent_task)
    # The existing gate reader may open its coordination lock; result JSON is
    # immutable and the recovery GET itself must never create an attempt.
    before=set(str(p) for p in root.rglob('*') if p.suffix!='.lock')
    details=client.get('/api/tasks/'+parent_task['id']).json()
    assert details['recovery']['can_continue']
    assert client.get(f"/api/tasks/{parent_task['id']}/recovery").json()['pending_review_calls']==1
    assert before==set(str(p) for p in root.rglob('*') if p.suffix!='.lock')
    request={'action':'continue','idempotency_key':'api-repeat'}
    first=client.post(f"/api/tasks/{parent_task['id']}/recover",json=request)
    second=client.post(f"/api/tasks/{parent_task['id']}/recover",json=request)
    assert first.status_code==201 and second.status_code==200
    assert first.json()['task_id']==second.json()['task_id'] and len(submits)==1


def test_all_planned_reviews_successful_allows_restart_not_unplanned_expansion(parent_task):
    root=recovery.directory(parent_task)
    write(root/'analysis/reviews.json',[{'call_id':f'call-{i}','status':'ok','verdicts':[{'candidate_id':f'c-{i}-{DEFECT_TYPES[0]}'}]} for i in [1,2]])
    stages=deepcopy(parent_task['stages'])
    for stage in stages:
        if stage['key']=='mllm':stage['status']='done'
    real_store.update(parent_task['id'],status='completed',stages_json=stages)
    status=recovery.availability(real_store.get(parent_task['id']))
    assert status['can_restart'] and not status['can_continue']
