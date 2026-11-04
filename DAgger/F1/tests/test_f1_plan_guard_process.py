"""Spawned numeric plan checks, with no ROS or command endpoints."""
from pathlib import Path
from types import SimpleNamespace
import json
import multiprocessing
import os
import threading
import time
import numpy as np
import pytest
from f1dagger.policy_safety import ProcessPolicyPlanGuard
from f1dagger.robot_io import CandidateSafetyRejected
from f1dagger.contract import ArmLimitRejected

class FakeGate:
    def __init__(self,spec):
        self.spec=spec
        def mapped(value):
            if spec.get('arm_limit') and value[7]>.5:raise ArmLimitRejected('joint workspace')
            return value
        self.mapping=SimpleNamespace(check_action=mapped)
    def check_preventive(self,value,full):
        if self.spec.get('crash'):os._exit(71)
        if self.spec.get('sleep'):time.sleep(self.spec['sleep'])
        if self.spec.get('log'):
            with open(self.spec['log'],'a') as f:f.write(json.dumps(value.tolist())+'\n')
        if self.spec.get('reject') and value[7]>.5:raise CandidateSafetyRejected('future collision')
        if self.spec.get('failure'):raise RuntimeError('native backend failed')

def fake_gate(spec):
    if spec.get('initialize_fail'):raise ValueError('invalid frozen model')
    if spec.get('initialize_sleep'):time.sleep(spec['initialize_sleep'])
    return FakeGate(spec)

def create(spec=None,**kw):
    events=[]
    guard=ProcessPolicyPlanGuard(spec or {},lambda event,**fields:events.append(dict(event=event,**fields)),
        factory=__name__+':fake_gate',startup_timeout_s=kw.pop('startup_timeout_s',5.),**kw)
    return guard,events

def observation():return SimpleNamespace(state16=np.zeros(16),full22=np.zeros(22))

def test_complete_c16_midpoints_and_receipt_identity(tmp_path):
    log=tmp_path/'checked.jsonl';guard,events=create(dict(log=str(log)))
    actions=np.zeros((16,16));actions[:,7]=np.arange(16)/20.
    try:
        guard(observation(),actions,3,8)
        checked=[json.loads(s) for s in log.read_text().splitlines()]
        assert len(checked)==32 and [x[7] for x in checked][1::2]==actions[:,7].tolist()
        assert checked[2][7]==pytest.approx(.025)
        e=events[-1];assert e['event']=='policy_path_checked' and e['request_id']==8 and e['policy_epoch']==3
        assert e['checked_samples']==32 and e['screened_new_waypoints']==16 and e['deferred_new_waypoints']==0
    finally:guard.close()
    assert not guard._process.is_alive()
    with pytest.raises(RuntimeError,match='closed'):guard(observation(),actions,3,9)

def test_rtc_prefix_and_bounded_join_unchanged(tmp_path):
    log=tmp_path/'checked.jsonl';guard,events=create(dict(log=str(log)))
    actions=np.zeros((32,16));actions[:,7]=np.arange(32)/20.
    try:
        guard(observation(),actions,4,9,committed_prefix=14,max_new_waypoints=1)
        checked=[json.loads(s) for s in log.read_text().splitlines()]
        assert len(checked)==2 and checked[0][7]==pytest.approx(.675) and checked[1][7]==.7
        assert events[-1]['deferred_new_waypoints']==17
    finally:guard.close()

@pytest.mark.parametrize('spec,error',[({'reject':True},CandidateSafetyRejected),({'arm_limit':True},ArmLimitRejected)])
def test_rejection_preserves_type_and_worker_remains_usable(spec,error):
    guard,events=create(spec);actions=np.zeros((16,16));actions[1,7]=1.
    try:
        with pytest.raises(error):guard(observation(),actions,1,2)
        assert events[-1]['event']=='policy_path_rejected' and events[-1]['checked_samples']==3
        assert guard._process.is_alive()
        guard(observation(),np.zeros((16,16)),2,3);assert events[-1]['event']=='policy_path_checked'
    finally:guard.close()

@pytest.mark.parametrize('spec,message',[({'crash':True},'disconnected|exited'),({'sleep':.2},'timeout'),({'failure':True},'native backend failed')])
def test_worker_failure_never_approves_and_is_reaped(spec,message):
    guard,events=create(spec,request_timeout_s=.1)
    try:
        with pytest.raises(RuntimeError,match=message):guard(observation(),np.zeros((16,16)),1,2)
        assert not any(x['event']=='policy_path_checked' for x in events)
        assert not guard._process.is_alive() and guard._closed.is_set()
    finally:guard.close()

@pytest.mark.parametrize('spec,timeout',[({'initialize_fail':True},5.),({'initialize_sleep':5.},.1)])
def test_init_failure_has_no_live_child(spec,timeout):
    before={p.pid for p in multiprocessing.active_children()}
    with pytest.raises(RuntimeError):create(spec,startup_timeout_s=timeout)
    assert {p.pid for p in multiprocessing.active_children()}==before

def test_close_interrupts_call_without_waiting_for_call_lock():
    guard,_=create(dict(sleep=3.),request_timeout_s=10.);errors=[]
    def work():
        try:guard(observation(),np.zeros((16,16)),2,3)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=work);thread.start();deadline=time.monotonic()+1
    while not guard._lock.locked() and time.monotonic()<deadline:time.sleep(.001)
    started=time.monotonic();guard.close();thread.join(2.)
    assert time.monotonic()-started<2. and not thread.is_alive()
    assert errors and isinstance(errors[0],RuntimeError)
    assert not guard._process.is_alive();guard.close()

def test_mismatched_receipt_cannot_approve_or_emit_other_events():
    guard,events=create()
    guard._receive=lambda timeout:dict(kind='checked',sequence=99,epoch=2,request_id=3,ok=True,events=[])
    try:
        with pytest.raises(RuntimeError,match='identity mismatch'):guard(observation(),np.zeros((16,16)),2,3)
        assert events==[] and not guard._process.is_alive()
    finally:guard.close()


def test_worker_ignores_terminal_sigint_until_parent_closes():
    import signal
    guard,events=create()
    try:
        os.kill(guard._process.pid,signal.SIGINT)
        guard(observation(),np.zeros((16,16)),4,7)
        assert events[-1]['event']=='policy_path_checked' and guard._process.is_alive()
    finally:guard.close()
    assert not guard._process.is_alive()


def test_close_wins_over_a_simultaneously_arriving_complete_receipt():
    guard,events=create()
    def completed_after_close(timeout):
        guard.close()
        return dict(kind='checked',sequence=1,epoch=2,request_id=3,ok=True,events=[dict(
            event='policy_path_checked',policy_epoch=2,request_id=3,checked_samples=32,screened_new_waypoints=16)])
    guard._receive=completed_after_close
    try:
        with pytest.raises(RuntimeError,match='closed'):guard(observation(),np.zeros((16,16)),2,3)
        assert not events and not guard._process.is_alive()
    finally:guard.close()
