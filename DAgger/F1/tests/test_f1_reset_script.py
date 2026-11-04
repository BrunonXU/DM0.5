"""Standalone reset uses real IK/output with an in-memory transport only."""
import importlib.util
import json
from pathlib import Path
import numpy as np
import pytest
from legacy_home_setup import setup, numerical_setup
from f1dagger.contract import ARM_NAMES, home_target
from f1dagger.robot_io import CollisionGate

@pytest.fixture
def script():
    spec=importlib.util.spec_from_file_location('reset_one_shot',Path(__file__).parents[1]/'scripts/61_right_clearance_reset.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@pytest.mark.parametrize('interrupt',[False,True])
def test_only_right_arm_and_interrupt_ends_publication(setup,tmp_path,monkeypatch,script,interrupt):
    c,p,m,u,_=setup
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_relaxed_20261013.json').read_text())[0]
    full=np.array(sample['full22']);state=np.array(sample['start']);original=full.copy()
    clock=[1.];sent=[];released=[];events=[]
    class Source:
        def feedback(self):return full.copy(),state.copy(),(int(clock[0]*1e9),)*16
    class Transport:
        def assert_exclusive(self,key):assert key=='joint_command'
        def assert_exclusive_cached(self,key):assert key=='joint_command'
        def prepare(self,key):assert key=='joint_command'
        def release(self,key):released.append(key)
        def publish(self,key,values):
            assert key=='joint_command' and set(values)==set(ARM_NAMES[7:14])
            sent.append(values);state[7:14]=[values[k] for k in ARM_NAMES[7:14]];full[7:14]=state[7:14]
    monkeypatch.setattr(script.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(script.time,'monotonic_ns',lambda:int(clock[0]*1e9))
    def sleep(seconds):clock[0]+=max(seconds,.000001)
    monkeypatch.setattr(script.time,'sleep',sleep)
    gate=CollisionGate(c,m,tmp_path/'collision',full)
    script.run_motion(c,p,m,u,Source(),Transport(),gate,lambda:interrupt and len(sent)>=30,lambda event,**fields:events.append(event))
    assert sent and released==['joint_command']
    np.testing.assert_array_equal(full[:7],original[:7]);np.testing.assert_array_equal(full[14:],original[14:])
    np.testing.assert_array_equal(state[14:],sample['start'][14:])
    if interrupt:assert len(sent)==30 and events[-1]=='cancelled'
    else:
        assert events[-1]=='completed'
        np.testing.assert_allclose(state[7:14],home_target(p,state)[7:14],atol=.01)


@pytest.mark.parametrize('fixture_name,stages', [('clearance_preview_20261014.json',['lift','right','return']),('clearance_home_ready_20261014.json',['return'])])
def test_recorded_preview_wrist_boundary_completes_without_relaxing_guards(setup,tmp_path,script,fixture_name,stages):
    c,p,m,u,_=setup
    from f1dagger.config import pico_paths
    pico_paths(c['_infra_root'])
    sample=json.loads((Path(__file__).parent/'fixtures'/fixture_name).read_text())
    full=np.array(sample['full_measured_deg']);state=np.r_[full[:14],43.,66.]
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    result=script.preview(c,p,m,u,full,state,guard)
    assert result['stages']==stages
    assert result['max_rotation_deg']<=p['f710_home']['clearance']['max_rotation_deg']
    assert result['physical_arrival_verified'] is False
    np.testing.assert_array_equal(full,sample['full_measured_deg'])
