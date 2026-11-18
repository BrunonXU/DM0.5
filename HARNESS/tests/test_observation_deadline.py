"""Keep local observation budgets separate from real network failures."""

import pytest
from contextlib import contextmanager
import threading
import time

from rpent.utils.rpc.http_rpc import HttpRpcClient, HttpRpcServer
from rpent.utils.rpc.rpc_client import RpcError

from f1_harness.contracts import HarnessError,Task,digest
from f1_harness.remote import ObservationWaitEnded,RemoteTaskBackend
from test_remote_readiness import backend,combined_backend,Store


MISSING = 'object_not_visible_in_either_calibrated_view'


def timeout_event(b):
    return next(event for event in reversed(b.evidence_store.events)
                if event['kind'] == 'observation_readiness_timeout')


def test_local_deadline_before_status_preserves_last_missing_object_reason(monkeypatch):
    """b312: the last status was never sent, so it cannot be a network timeout."""
    b = combined_backend(monkeypatch, [RpcError('observe_with_status', MISSING)])
    original = b.call
    reads = 0

    def call(method, **kwargs):
        nonlocal reads
        if method == 'observe_with_status':
            reads += 1
            if reads == 2:
                b.clock.value = .15
        return original(method, **kwargs)

    b.call = call
    with pytest.raises(ObservationWaitEnded, match=MISSING) as raised:
        b._await_observation(timeout_s=.15, skill='reobserve')

    assert type(raised.value) is ObservationWaitEnded
    assert b.wire_calls == ['observe_with_status', 'status', 'observe_with_status']
    event = timeout_event(b)
    assert event['transport_error'] is None
    assert event['reason'] == event['first_reason']
    assert event['reason'].endswith(MISSING)
    assert event['waited_ms'] == pytest.approx(150.)
    assert event['submitted'] is False
    assert not b.submissions and b.pending is None


@pytest.mark.parametrize('failure', [
    TimeoutError('socket timed out'),
    ConnectionError('connection reset by peer'),
    RpcError('status', 'HTTP request failed: timed out'),
])
def test_real_status_failure_preserves_perception_cause_but_stays_fatal(monkeypatch, failure):
    b = combined_backend(monkeypatch, [RpcError('observe_with_status', MISSING)])
    original = b.call
    statuses = 0

    def call(method, **kwargs):
        nonlocal statuses
        if method == 'status':
            statuses += 1
            if statuses == 2:
                raise failure
        return original(method, **kwargs)

    b.call = call
    with pytest.raises(HarnessError, match=MISSING) as raised:
        b._await_observation(timeout_s=.3, skill='reobserve')

    assert type(raised.value) is HarnessError
    event = timeout_event(b)
    assert event['reason'].endswith(MISSING)
    assert event['first_reason'].endswith(MISSING)
    assert event['transport_error'] == type(failure).__name__ + ':' + str(failure)
    assert event['waited_ms'] < 300.
    assert not b.submissions and b.pending is None


@pytest.mark.parametrize('combined', [True, False])
@pytest.mark.parametrize('failure', [
    TimeoutError('socket timed out'),
    ConnectionError('connection reset by peer'),
    RpcError('observe_with_status', 'HTTP request failed: timed out'),
])
def test_real_observation_transport_error_cannot_become_perception_wait(monkeypatch, combined, failure):
    make_backend = combined_backend if combined else backend
    b = make_backend(monkeypatch, [failure])
    with pytest.raises(HarnessError) as raised:
        b._await_observation(timeout_s=.3, skill='reobserve')

    assert type(raised.value) is HarnessError
    event = timeout_event(b)
    assert event['transport_error'] == type(failure).__name__ + ':' + str(failure)
    assert not b.submissions and b.pending is None


def test_local_deadline_after_read_does_not_accept_unverified_observation(monkeypatch):
    b = backend(monkeypatch, [True])
    original = b.call

    def call(method, **kwargs):
        value = original(method, **kwargs)
        if method == 'observe':
            b.clock.value = .15
        return value

    b.call = call
    with pytest.raises(ObservationWaitEnded) as raised:
        b._await_observation(timeout_s=.15, skill='reobserve')

    assert type(raised.value) is ObservationWaitEnded
    assert b.calls == ['status', 'observe']
    assert timeout_event(b)['transport_error'] is None
    assert not b.submissions and b.pending is None


@contextmanager
def http_backend(observe):
    """The actual RPent socket/HTTP/error envelope, with no robot services."""
    b=RemoteTaskBackend.__new__(RemoteTaskBackend)
    b.task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    b.session='session';b.instance='owner';b.pending=None;b.steps=0;b._solved=False
    b.observation_snapshot_supported=True;b.evidence_store=Store();b.wire_calls=[]
    def dispatch(method,args,kwargs):
        b.wire_calls.append(method)
        if method=='observe_with_status':return observe()
        assert method=='status','Read-only regression must never submit motion'
        return dict(owner_instance_id='owner',task_hash=digest(b.task),authorized=True,
            hold_requested=False,hardware_ready=True,epoch=1,task_generation=1,
            perception_context=dict(session_id=b.session,owner_instance_id=b.instance,
                task_hash=digest(b.task),task_id=b.task.task_id,epoch=1,task_generation=1))
    server=HttpRpcServer(('127.0.0.1',0),dispatch)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True)
    thread.start()
    b.rpc=HttpRpcClient(f'http://127.0.0.1:{server.server_address[1]}',enable_sessions=False)
    try:yield b
    finally:server.shutdown();server.server_close();thread.join(timeout=1)


def test_real_http_clipped_tail_timeout_preserves_perception_reason():
    """0339: healthy missing-object replies, then HTTP's last short budget expires."""
    reads=0
    def observe():
        nonlocal reads
        reads+=1
        if reads>1:time.sleep(.25)
        raise ValueError(MISSING)
    with http_backend(observe) as b:
        with pytest.raises(ObservationWaitEnded,match=MISSING) as raised:
            b._await_observation(timeout_s=.2,skill='reobserve')
        assert type(raised.value) is ObservationWaitEnded and reads>=2
        event=timeout_event(b)
        assert event['transport_error'] is None and event['reason'].endswith(MISSING)
        assert event['reason']==event['first_reason']
        assert event['waited_ms']>=200 and not event['submitted'] and b.pending is None


def test_real_http_full_transport_timeout_remains_fatal_before_acquisition_deadline():
    def observe():
        time.sleep(2.2)
        raise ValueError(MISSING)
    with http_backend(observe) as b:
        with pytest.raises(HarnessError,match='observation_transport_unavailable') as raised:
            b._await_observation(timeout_s=4.,skill='reobserve')
        assert type(raised.value) is HarnessError
        event=timeout_event(b)
        assert event['transport_error'] and event['waited_ms']<3500
        assert b.wire_calls==['observe_with_status'] and not event['submitted']


def test_real_http_unseeded_sam_reaches_planner_bootstrap_without_readiness_timeout():
    from f1_harness.task_runner import _initial_planner_observation
    def observe():
        raise ValueError('mask_view=right:mask_uncertain_requires_new_prompt:object=square-1')
    with http_backend(observe) as b:
        status=b.call('status');started=time.monotonic()
        result=_initial_planner_observation(b,status,b.evidence_store)
        assert not result['geometry_available'] and not result['motion_evidence']
        assert result['recovery']=='localize_current_entities_then_reacquire'
        assert b.wire_calls==['status','observe_with_status','status','status']
        assert time.monotonic()-started<1.
        assert b.evidence_store.events[-1]['kind']=='planner_initial_observation_unavailable'
        assert not any(e['kind']=='observation_readiness_timeout' for e in b.evidence_store.events)
        assert b.pending is None and not hasattr(b,'attention')


@pytest.mark.parametrize('cause',[
    TimeoutError('actual socket timeout'),ConnectionError('connection reset')])
def test_transport_failure_before_local_deadline_is_never_reclassified(monkeypatch,cause):
    failure=RpcError('observe_with_status','HTTP request failed: '+str(cause))
    failure.__cause__=cause
    b=combined_backend(monkeypatch,[failure])
    with pytest.raises(HarnessError) as raised:b._await_observation(timeout_s=.3,skill='reobserve')
    assert type(raised.value) is HarnessError and timeout_event(b)['transport_error']


@pytest.mark.parametrize('reason',[
    'mask_view=right:mask_uncertain_requires_new_prompt:object=square-1',MISSING])
def test_bootstrap_prompt_requirement_hands_off_after_checked_status_without_polling(monkeypatch,reason):
    b=combined_backend(monkeypatch,[RpcError('observe_with_status',reason)])
    with pytest.raises(ObservationWaitEnded,match='observation_prompt_required'):
        b.execute_tool('observe',{},prompt_handoff=True)
    assert b.wire_calls==['observe_with_status','status'] and b.clock.value==0.
    assert not b.submissions and b.pending is None and not hasattr(b,'attention')
    event=b.evidence_store.events[-1]
    assert event['kind']=='observation_prompt_required' and not event['submitted']
    assert not any(e['kind']=='observation_readiness_timeout' for e in b.evidence_store.events)


@pytest.mark.parametrize('fault',['authorized','hold_requested','epoch','attention'])
def test_prompt_handoff_never_precedes_authority_and_identity_validation(monkeypatch,fault):
    reason='mask_view=right:mask_uncertain_requires_new_prompt:object=square-1'
    b=combined_backend(monkeypatch,[RpcError('observe_with_status',reason)]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':
            if fault=='authorized':value['authorized']=False
            elif fault=='hold_requested':value['hold_requested']=True
            elif fault=='epoch':value['epoch']=2
            else:value['observation_readiness']=dict(state='needs_attention',code='camera_identity_changed')
        return value
    b.call=call
    with pytest.raises(HarnessError):
        b._await_observation(skill='reobserve',prompt_handoff=True,
            expected_scope=('owner','session',digest(b.task),1))
    assert not any(e['kind']=='observation_prompt_required' for e in b.evidence_store.events)
    assert not b.submissions and b.pending is None
