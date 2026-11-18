"""Authorization races exercise status reads only; no robot or model calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from f1_harness.contracts import HarnessError, Task, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.remote import RemoteTaskBackend
from f1_harness.task_runner import EvidenceRemoteBackend, _wait_for_task_authority, run_task


class Clock:
    def __init__(self):self.value=0.
    def monotonic(self):return self.value
    def sleep(self,duration):self.value+=duration


class Store:
    def __init__(self):self.events=[]
    def event(self,kind,**values):self.events.append(dict(kind=kind,**values))


class Backend:
    def __init__(self):
        self.task=Task(task_id='current-task',object_id='block',hole_id='hole',calibration_id='cal')
        self.instance='owner';self.session='session';self.calls=[];self.statuses=[]
        self.prepared_authority=dict(task_hash=digest(self.task),authorized=False,epoch=4,task_generation=2)

    def status(self,epoch=4,authorized=False,**changes):
        return dict(owner_instance_id=self.instance,task_hash=digest(self.task),epoch=epoch,
            authorized=authorized,hold_requested=False,hardware_ready=True,
            perception_context=dict(owner_instance_id=self.instance,session_id=self.session,
                task_id=self.task.task_id,task_hash=digest(self.task),task_generation=2,epoch=epoch),
            **changes)

    def call(self,method,**kwargs):
        self.calls.append(method)
        assert method=='status', 'Authorization polling must never send an action'
        item=self.statuses.pop(0) if len(self.statuses)>1 else self.statuses[0]
        if isinstance(item,Exception):raise item
        return item


@pytest.fixture
def waiting(monkeypatch):
    clock=Clock();store=Store();backend=Backend()
    monkeypatch.setattr('f1_harness.task_runner.time.monotonic',clock.monotonic)
    monkeypatch.setattr('f1_harness.task_runner.time.sleep',clock.sleep)
    def wait(initial=None,heartbeat_error=None,timeout_s=1.):
        return _wait_for_task_authority(backend,initial or backend.status(),timeout_s=timeout_s,
            waiting='infra_start',heartbeat_error=heartbeat_error or [],store=store)
    return backend,store,clock,wait


def test_wait_accepts_only_the_single_authorization_after_preparation(waiting):
    backend,store,clock,wait=waiting
    backend.statuses=[backend.status(),backend.status(5,True)]
    result=wait()
    assert result['authorized'] and result['epoch']==5
    assert backend.calls==['status','status'] and clock.value==pytest.approx(.2)
    assert not store.events


@pytest.mark.parametrize('missed_before_first_read',[False,True])
def test_brief_authorization_and_revoke_fail_with_original_owner_reason(waiting,missed_before_first_read):
    backend,store,clock,wait=waiting
    reason='runtime_latched:RuntimeError: 无法读取自身发布者RMW身份'
    revoked=backend.status(6,authority_revocation=dict(reason=reason,epoch=6,
        task_hash=digest(backend.task),session_id=backend.session))
    backend.statuses=[revoked]
    with pytest.raises(HarnessError,match=reason):
        wait(revoked if missed_before_first_read else None)
    assert clock.value<=.1
    assert store.events[-1]['reason']=='owner_authority_revoked:'+reason
    assert all(method=='status' for method in backend.calls)


@pytest.mark.parametrize('session_closed',[False,True])
def test_followup_shutdown_epoch_retains_first_bound_revocation(waiting,session_closed):
    backend,store,clock,wait=waiting
    reason='runtime_latched:original_control_fault'
    status=backend.status(8,authority_revocation=dict(reason=reason,epoch=6,
        task_hash=digest(backend.task),session_id=backend.session))
    if session_closed:status['perception_context']['session_id']=None
    with pytest.raises(HarnessError,match=reason):wait(status)
    assert store.events[-1]['reason']=='owner_authority_revoked:'+reason
    assert not backend.calls


@pytest.mark.parametrize('authorized,epoch',[(False,6),(True,4),(True,7)])
def test_old_or_reauthorized_epoch_is_not_accepted(waiting,authorized,epoch):
    backend,store,clock,wait=waiting
    with pytest.raises(HarnessError,match='epoch_changed'):
        wait(backend.status(epoch,authorized))
    assert not backend.calls and clock.value==0


@pytest.mark.parametrize('field,value',[
    ('owner_instance_id','replacement-owner'),('session_id','other-session'),
    ('task_id','previous-task'),('task_hash','other-task-hash'),('task_generation',3),
])
def test_another_preparation_or_session_cannot_supply_authority(waiting,field,value):
    backend,store,clock,wait=waiting
    status=backend.status(5,True)
    status['perception_context'][field]=value
    with pytest.raises(HarnessError,match='context_changed'):wait(status)
    assert not backend.calls


def test_old_revocation_does_not_cancel_a_new_prepared_task(waiting):
    backend,store,clock,wait=waiting
    status=backend.status(5,True,authority_revocation=dict(reason='previous_stop',epoch=4,
        task_hash=digest(backend.task),session_id=backend.session))
    assert wait(status)['epoch']==5
    assert not store.events


def test_legacy_owner_epoch_and_hold_still_fail_promptly(waiting):
    backend,store,clock,wait=waiting
    backend.prepared_authority.pop('task_generation')
    status=backend.status();status['hold_requested']=True
    with pytest.raises(HarnessError,match='owner_hold_requested'):wait(status)
    assert not backend.calls


@pytest.mark.parametrize('error',[ConnectionError('disconnected'),TimeoutError('timeout')])
def test_owner_disconnection_is_terminal_without_retransmission(waiting,error):
    backend,store,clock,wait=waiting
    backend.statuses=[error]
    with pytest.raises(HarnessError,match='owner_connection_lost:'+type(error).__name__):wait()
    assert backend.calls==['status'] and clock.value==pytest.approx(.1)


def test_lost_heartbeat_is_checked_before_accepting_authority(waiting):
    backend,store,clock,wait=waiting
    with pytest.raises(HarnessError,match='owner_connection_lost'):
        wait(backend.status(5,True),heartbeat_error=['owner_connection_lost'])
    assert not backend.calls


def test_authorized_snapshot_with_lost_hardware_admission_does_not_start(waiting):
    backend,store,clock,wait=waiting
    status=backend.status(5,True);status['hardware_ready']=False
    with pytest.raises(HarnessError,match='owner_integration_and_calibration_not_admitted'):wait(status)
    assert not backend.calls


def test_unstarted_task_timeout_does_not_change_owner_state(waiting):
    backend,store,clock,wait=waiting
    backend.statuses=[backend.status()]
    with pytest.raises(HarnessError,match='infra_start_timeout'):wait(timeout_s=.2)
    assert set(backend.calls)=={'status'} and clock.value==pytest.approx(.2)


def test_atomic_prepare_receipt_is_captured_without_extra_rpc(monkeypatch):
    receipt=dict(task_hash='hash',authorized=False,epoch=0,task_generation=1)
    calls=[]
    def call(self,method,**kwargs):calls.append(method);return receipt
    monkeypatch.setattr(RemoteTaskBackend,'call',call)
    backend=EvidenceRemoteBackend.__new__(EvidenceRemoteBackend)
    assert backend.call('prepare_task',task={})==receipt
    assert backend.prepared_authority==receipt and backend.prepared_authority is not receipt
    assert calls==['prepare_task']


def test_run_task_never_starts_worker_when_start_was_already_revoked(tmp_path,monkeypatch):
    backend=Backend();backend.pending=None;calls=[]
    reason='runtime_latched:RuntimeError: publisher identity missing'
    revoked=backend.status(6,authority_revocation=dict(reason=reason,epoch=6,
        task_hash=digest(backend.task),session_id=backend.session))
    def call(method,**kwargs):
        calls.append(method)
        if method=='capabilities':return dict(mode='hardware',hardware_ready=True)
        if method=='status':return deepcopy(revoked)
        if method=='execution_events':return dict(events=[],next=kwargs['after'])
        raise AssertionError(method)
    backend.call=call
    backend.heartbeat=lambda:None
    backend.cancel=lambda:calls.append('close_session')
    backend.close_images=lambda:calls.append('close_images')
    monkeypatch.setattr('f1_harness.task_runner.EvidenceRemoteBackend',lambda *args:backend)
    def worker(*args,**kwargs):raise AssertionError('Worker must not start after revoked START')
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',worker)
    task_path=tmp_path/'task.json';task_path.write_text(backend.task.model_dump_json())
    store=EvidenceStore(tmp_path/'evidence')
    args=SimpleNamespace(task=task_path,gateway='unused',image_gateway='unused',
        authorization_source='web',start_timeout=120,planner='fixed')
    with pytest.raises(HarnessError,match=reason):run_task(args,store,None)
    assert calls==['capabilities','status','execution_events','close_session','close_images']
    assert 'remote_authority_wait_failed' in (store.root/'trace.jsonl').read_text()
    assert 'remote_authority_observed' not in (store.root/'trace.jsonl').read_text()
