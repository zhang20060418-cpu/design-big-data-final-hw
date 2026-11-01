"""Frozen auxiliary CLIP bridge: synthetic contracts, never benchmark labels."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

BACKEND=Path(__file__).resolve().parents[1]

def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

@pytest.fixture
def bridge():
    return module(BACKEND/'models/frozen_clip_impl.py','test_frozen_bridge')

@pytest.fixture
def page(tmp_path):
    path=tmp_path/'renders/s0001.png';path.parent.mkdir();path.write_bytes(b'synthetic-request-path-only')
    return {'png_path':path,'document_id':'fixture','slide_index':1}

def test_import_and_availability_do_not_import_heavy_models(bridge):
    before=set(sys.modules);status=bridge.check_available()
    assert not ({'torch','transformers','numpy','sklearn'} & (set(sys.modules)-before))
    assert status['model_loaded'] is False and status['CLIP_forwards']==0

def test_missing_runtime_is_truthful(bridge,monkeypatch,tmp_path):
    monkeypatch.setattr(bridge,'ML_PYTHON',tmp_path/'missing.exe')
    state=bridge.check_available()
    assert state['available'] is False and state['state']=='error'
    assert 'missing.exe' in state['reason']

@pytest.mark.parametrize('scenario',['duplicate','outside','workpoint'])
def test_invalid_inputs_never_launch_worker(bridge,monkeypatch,page,tmp_path,scenario):
    monkeypatch.setattr(bridge,'check_available',lambda:{'available':True})
    monkeypatch.setattr(bridge.subprocess,'run',lambda *a,**k:pytest.fail('Invalid input launched worker'))
    pages=[page];config={'task_dir':tmp_path}
    if scenario=='duplicate':pages=[page,deepcopy(page)]
    elif scenario=='outside':config['task_dir']=tmp_path/'different-task'
    else:config['work_point']='new_threshold'
    with pytest.raises(ValueError):bridge.predict_batch(pages,config)

def test_batch_once_keeps_raw_metadata_and_offline_environment(bridge,monkeypatch,page,tmp_path):
    monkeypatch.setattr(bridge,'check_available',lambda:{'available':True})
    calls=[]
    def fake(command,**kwargs):
        calls.append((command,kwargs))
        request=Path(command[command.index('--request')+1]);out=Path(command[command.index('--output')+1])
        data=json.loads(request.read_text(encoding='utf8'))
        results=[dict(document_id=r['document_id'],slide_index=r['slide_index'],status='ok',
            probabilities={'synthetic':.2},alerts={'synthetic':1},thresholds={'synthetic':.13},model_version='synthetic') for r in data['pages']]
        out.write_text(json.dumps({'status':'complete','results':results}),encoding='utf8')
        return SimpleNamespace(returncode=0,stdout='synthetic worker',stderr='')
    monkeypatch.setattr(bridge.subprocess,'run',fake)
    result=bridge.predict_batch([page],{'task_dir':tmp_path})
    assert len(calls)==1 and result[0]['probabilities']=={'synthetic':.2}
    assert result[0]['thresholds']=={'synthetic':.13}
    assert calls[0][1]['env']['HF_HUB_OFFLINE']=='1'
    assert calls[0][1]['env']['TRANSFORMERS_OFFLINE']=='1'
    assert 'features' not in result[0]['artifacts']
    assert Path(result[0]['artifacts']['results']).is_file()

def test_wrong_page_identity_is_error_not_a_reordered_success(bridge,monkeypatch,page,tmp_path):
    monkeypatch.setattr(bridge,'check_available',lambda:{'available':True})
    def fake(command,**kwargs):
        out=Path(command[command.index('--output')+1])
        out.write_text(json.dumps({'status':'complete','results':[{'document_id':'another','slide_index':1}]}),encoding='utf8')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(bridge.subprocess,'run',fake)
    result=bridge.predict_batch([page],{'task_dir':tmp_path})
    assert result[0]['status']=='error' and not result[0]['probabilities']
    assert result[0]['error_code']=='clip_invalid_result'

@pytest.mark.parametrize('failure',['timeout','worker_failure'])
def test_worker_failure_never_creates_placeholder_probabilities(bridge,monkeypatch,page,tmp_path,failure):
    monkeypatch.setattr(bridge,'check_available',lambda:{'available':True})
    folder=tmp_path/'analysis/clip';folder.mkdir(parents=True)
    (folder/'results.json').write_text('{"status":"complete","results":[]}',encoding='utf8')
    def fake(command,**kwargs):
        assert not (folder/'results.json').exists(),'Stale result was not removed'
        if failure=='timeout':raise subprocess.TimeoutExpired(command,30)
        return SimpleNamespace(returncode=1,stdout='',stderr='real error')
    monkeypatch.setattr(bridge.subprocess,'run',fake)
    result=bridge.predict_batch([page],{'task_dir':tmp_path})
    assert result[0]['status']=='error'
    assert result[0]['probabilities']==result[0]['alerts']=={}
    assert result[0]['error_code']==('clip_timeout' if failure=='timeout' else 'clip_inference_failed')

def test_manifest_declares_frozen_four_labels_and_disabled_default():
    manifest=json.loads((BACKEND/'models/visual_model.json').read_text(encoding='utf8'))
    pins=json.loads((BACKEND/'models/frozen_clip_manifest.json').read_text(encoding='utf8'))
    assert manifest['default_enabled'] is False
    assert manifest['labels']==pins['labels'] and len(manifest['labels'])==4
    assert list(manifest['thresholds'].values())==pins['thresholds']['f1']==[.13,.16,.57,.14]
    assert pins['clip_revision']=='3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268'

def test_sha_guard_rejects_unfrozen_content_before_deserialization(tmp_path):
    worker=module(BACKEND/'app/services/clip_inference_worker.py','test_frozen_worker')
    (tmp_path/'weights.bin').write_bytes(b'not-original')
    pins={'assets_sha256':{'weights.bin':'0'*64},'labels':list(worker.LABELS),'thresholds':{m:[.5]*4 for m in worker.MODES}}
    with pytest.raises(ValueError,match='SHA'):worker.verify_assets(pins,tmp_path)

def test_synthetic_classification_reuses_frozen_522_recipe_and_positive_class():
    interpreter=BACKEND.parent/'detector_runtime/.venv/Scripts/python.exe'
    script='''import importlib.util,sys,numpy as np
from pathlib import Path
p=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location("fixture_worker",p);w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
sys.path.insert(0,str(w.ML/'scripts'));h=w.load_module('fixture_g2',w.ML/'scripts/evaluate_pptx_g2.py')
class S:
 def transform(self,x):return np.asarray(x)*2
class C:
 classes_=np.array([1,0])
 def predict_proba(self,x):
  assert x.shape==(2,522)
  return np.column_stack([np.full(2,.7),np.full(2,.3)])
b={'png_scalers':{'10':S(),'9':S()},'models':{'C':[C() for _ in range(4)]}}
x=np.ones((2,512),np.float32);z=np.ones((2,10),np.float64)
f,p=w.classify_c(b,x,z,h)
np.testing.assert_allclose(f[:,:512],1/np.sqrt(512));np.testing.assert_allclose(f[:,512:],2/np.sqrt(10));np.testing.assert_allclose(p,.7)
assert p.shape==(2,4) and f.shape==(2,522)
print('synthetic-frozen-recipe-passed')
'''
    result=subprocess.run([str(interpreter),'-B','-c',script,str(BACKEND/'app/services/clip_inference_worker.py')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert 'synthetic-frozen-recipe-passed' in result.stdout
