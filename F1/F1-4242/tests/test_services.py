import sys,time,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import ControlLease,ActionManager,EventLog,PresetStore
import pytest

def test_lease_shared_process_lock_and_expiration(tmp_path):
    now=[1.]; a=ControlLease(tmp_path/'lock',clock=lambda:now[0]); b=ControlLease(tmp_path/'lock')
    token=a.acquire('a')
    with pytest.raises(ValueError):b.acquire('b')
    with pytest.raises(ValueError):a.require('other',token)
    now[0]=5
    with pytest.raises(ValueError):a.require('a',token)
    # Expiry does not unlock until manager has actually stopped command output.
    with pytest.raises(ValueError):b.acquire('b')
    a.release();b.acquire('b');b.release()

def test_base_completion_does_not_claim_feedback_arrival(tmp_path):
    class Fake:
        def execute(self,*args):return {'status':'completed','commands_sent':True,'feedback_verified':False}
    manager=ActionManager(Fake(),EventLog(tmp_path/'events'))
    row=manager.start({'kind':'base'},'session');manager.thread.join(1)
    assert manager.get(row['id'])['status']=='completed'
    assert not manager.get(row['id'])['result']['feedback_verified']

def test_preset_import_is_atomic_and_finite(tmp_path):
    store=PresetStore(tmp_path/'presets',{'id':'F1-09','limits':{'arm_l_j1':[-178,178]}})
    valid={'name':'pick','joints':{'arm_l_j1':1}}
    with pytest.raises(ValueError):store.import_many([valid,{'name':'bad','joints':{'arm_l_j1':float('nan')}}])
    assert store.all()==[]
    with pytest.raises(ValueError):store.save(dict(valid,robot_id='other'))
    assert store.save(valid)['joints']==valid['joints']
