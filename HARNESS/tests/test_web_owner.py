"""Browser authority is explicit, bounded, and consumed only by the control owner."""
import json

import pytest

from f1_harness.contracts import HarnessError, Mode, Task, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.gateway import OWNER_METHODS
from f1_harness.owner_bridge import OwnerBridge


@pytest.fixture
def web_owner(tmp_path):
    now = [1_000_000_000]
    engine = TaskEngine(EvidenceStore(tmp_path), mode=Mode.HARDWARE, clock=lambda: now[0])
    engine.hardware_ready = True
    engine.web_control_enabled = True
    engine.recording_identity = {'owner_run_id': 'owner-run'}
    session = engine.open_session()['session_id']
    engine.prepare_task(session, Task(task_id='browser-task', object_id='square', hole_id='hole',
                                     calibration_id='coarse').model_dump())
    bridge = OwnerBridge(engine, numerical_planner=lambda *a: None, result_verifier=lambda *a: None,
                         feedback_age_ns=500_000_000, reference_tolerance_deg=.5,
                         stop_window_ns=200_000_000, stop_gripper_delta=.1, clock=lambda: now[0])
    bridge.update_feedback(full22=(0.,)*22, measured16=(0.,)*16, stamps16=(now[0],)*16)
    yield bridge, engine, now
    bridge.close()


def binding(engine, request_id='start'):
    return dict(request_id=request_id, run_id='owner-run', owner_instance_id=engine.instance,
                session_id=engine.session, task_hash=digest(engine.task), epoch=engine.epoch)


def test_real_async_journal_survives_numpy_waypoint_then_stop(web_owner,tmp_path):
    import numpy as np
    from f1_harness.contracts import SkillIntent
    from f1_harness.evidence import AsyncOwnerJournal
    from f1_harness.owner_bridge import JointProposal
    bridge,engine,now=web_owner
    original_store=engine.store
    journal=AsyncOwnerJournal(EvidenceStore(tmp_path/'async'))
    engine.store=journal
    try:
        bridge.queue_web_control('web_start',binding(engine))
        bridge.process_web_controls(feedback_healthy=True)
        intent=SkillIntent(request_id='moving',session_id=engine.session,owner_instance_id=engine.instance,
            task_id=engine.task.task_id,task_hash=digest(engine.task),epoch=engine.epoch,
            observation_id='observation',scene_version='scene',calibration_id='coarse',skill='approach_grasp')
        engine.running=intent
        bridge.proposal=JointProposal('moving',engine.epoch,'scene','coarse',(0.,)*22,
            ((0.,)*16,(0.,)*16),.1,100_000_000,.1)
        bridge.current_target=np.zeros(16)
        bridge.plan_held_reference=(0.,)*16
        for index in range(2):
            now[0]+=10_000_000
            bridge.update_feedback(full22=(0.,)*22,measured16=np.zeros(16),stamps16=(np.int64(now[0]),)*16)
            bridge.after_publication(publication_id=str(index),measured16=np.zeros(16),
                publication_complete=True,now_ns=now[0])
        assert bridge.index==1
        journal.flush()
        bridge.revoke('runtime_latched:test',preempted=True)
        bridge.queue_web_control('web_stop',binding(engine,'stop'))
        bridge.process_web_controls(feedback_healthy=True)
        for index in range(2):
            now[0]+=200_000_000
            bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(now[0],)*16)
            bridge.after_publication(publication_id='hold-'+str(index),measured16=(0.,)*16,
                publication_complete=True,now_ns=now[0])
        assert bridge.web_control_status('stop')['stage']=='measured_stationary'
        journal.flush()
        rows=[json.loads(line) for line in (journal.root/'trace.jsonl').read_text().splitlines()]
        waypoint=next(row for row in rows if row['kind']=='harness_measured_waypoint')
        assert waypoint['settle_required'] is False
        assert any(row['kind']=='hold_acknowledged' and row['stage']=='measured_stationary' for row in rows)
    finally:
        engine.store=original_store
        journal.close()


def test_writer_failure_revokes_and_stop_preserves_only_actual_hold(web_owner,tmp_path):
    from f1_harness.contracts import SkillIntent
    from f1_harness.evidence import AsyncOwnerJournal
    bridge,engine,now=web_owner
    bridge.queue_web_control('web_start',binding(engine))
    bridge.process_web_controls(feedback_healthy=True)
    engine.running=SkillIntent(request_id='moving',session_id=engine.session,owner_instance_id=engine.instance,
        task_id=engine.task.task_id,task_hash=digest(engine.task),epoch=engine.epoch,
        observation_id='observation',scene_version='scene',calibration_id='coarse',skill='approach_grasp')
    original_store=engine.store
    journal=AsyncOwnerJournal(EvidenceStore(tmp_path/'failed'))
    engine.store=journal
    try:
        journal.event('unsupported_test_event',value=object())
        journal.thread.join(timeout=1)
        assert journal.error=='TypeError'
        assert json.loads((journal.root/'writer-error.json').read_text())['kind']=='unsupported_test_event'
        with pytest.raises(RuntimeError,match='owner_journal_unavailable'):engine.tick()
        assert not engine.authorized and engine.running is None and engine.hold_requested
        bridge.current_target=object()
        bridge.queue_web_control('web_stop',binding(engine,'stop'))
        bridge.process_web_controls(feedback_healthy=False)
        status=bridge.web_control_status('stop')
        assert status['stage']=='cancel_requested' and status['journal_error']
        assert bridge.current_target is None and engine.hold_confirmation is None
        for index in range(2):
            now[0]+=200_000_000
            bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(now[0],)*16)
            with pytest.raises(RuntimeError,match='owner_journal_unavailable'):
                bridge.after_publication(publication_id='actual-hold-'+str(index),measured16=(0.,)*16,
                    publication_complete=True,now_ns=now[0])
        status=bridge.web_control_status('stop')
        assert status['stage']=='measured_stationary' and status['journal_error']
        assert status['owner']['hold_confirmation']['publication_id']=='actual-hold-1'
    finally:
        engine.store=original_store
        with pytest.raises(RuntimeError,match='owner_journal_not_flushed'):journal.close()


@pytest.mark.parametrize('failure_at',['queue','consume'])
def test_failed_journal_cannot_authorize_queued_start(web_owner,failure_at):
    bridge,engine,_=web_owner
    original=engine.store
    class BrokenJournal:
        error='TypeError'
        def event(self,*args,**kwargs):raise RuntimeError('owner_journal_unavailable')
    try:
        if failure_at=='consume':bridge.queue_web_control('web_start',binding(engine))
        engine.store=BrokenJournal()
        if failure_at=='queue':
            with pytest.raises(RuntimeError,match='owner_journal_unavailable'):
                bridge.queue_web_control('web_start',binding(engine))
            assert not bridge.web_queue
        bridge.process_web_controls(feedback_healthy=True)
        assert not engine.authorized
    finally:engine.store=original


@pytest.mark.parametrize('condition',['stationary','drift','stale','repeated','invalid_gripper','busy','journal'])
def test_restart_readiness_uses_fresh_distinct_actual_feedback(web_owner,condition):
    bridge,engine,now=web_owner
    for index in range(3):
        now[0]+=100_000_000
        measured=[0.]*16
        if condition=='drift':measured[7]=index*bridge.stop_delta_deg*.6
        stamps=[now[0]]*16
        if condition=='repeated':stamps=[1_000_000_000]*16
        if condition=='invalid_gripper':stamps[15]=None
        bridge.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=stamps)
    if condition=='stale':now[0]+=bridge.feedback_age_ns+1
    if condition=='busy':engine.authorized=True
    if condition=='journal':engine.store.error='TypeError'
    result=bridge.status()['restart_readiness']
    assert result['state']==('ready' if condition=='stationary' else 'waiting')
    assert result['owner_instance_id']==engine.instance
    assert engine.hold_confirmation is None
    if condition=='journal':del engine.store.error


def test_web_start_requires_owner_tick_and_is_idempotent(web_owner):
    bridge, engine, _ = web_owner
    payload = binding(engine)
    assert 'web_start' not in OWNER_METHODS and 'web_stop' not in OWNER_METHODS
    queued = bridge.queue_web_control('web_start', payload)
    assert queued['lifecycle'] == 'queued' and not engine.authorized
    assert bridge.queue_web_control('web_start', payload)['duplicate']
    bridge.process_web_controls(feedback_healthy=True)
    assert engine.authorized and engine.epoch == 1
    assert bridge.web_control_status('start')['stage'] == 'owner_authorized'
    assert bridge.queue_web_control('web_start', payload)['duplicate']
    bridge.process_web_controls(feedback_healthy=True)
    assert engine.epoch == 1
    events = [json.loads(line) for line in (engine.store.root/'trace.jsonl').read_text().splitlines()]
    assert sum(row['kind'] == 'web_authorization' for row in events) == 1
    assert not any(row['kind'] == 'local_authorization' for row in events)
    assert not any('physical_event' in row for row in events)
    with pytest.raises(HarnessError, match='one_shot'):
        bridge.queue_web_control('web_start', binding(engine, 'again'))


def test_first_revocation_cause_survives_cleanup_and_resets_for_new_task(web_owner):
    _, engine, _ = web_owner
    task = engine.task.model_dump()
    session = engine.session
    engine.authorize_web(request_id='start', run_id='owner-run', feedback_healthy=True)
    engine.revoke('runtime_latched:publisher_identity')
    cause = engine.status()['authority_revocation']
    engine.revoke('session_closed')
    assert engine.status()['authority_revocation'] == cause
    assert cause['epoch'] < engine.epoch
    assert cause['session_id'] == session and cause['task_hash'] == digest(engine.task)
    cause['reason'] = 'caller_mutation'
    assert engine.status()['authority_revocation']['reason'] == 'runtime_latched:publisher_identity'
    engine.acknowledge_hold(publication_id='hold-evidence', measured_stationary=True)
    prepared = engine.prepare_task(session, task)
    assert prepared['task_generation'] == 2
    assert engine.status()['authority_revocation'] is None


@pytest.mark.parametrize('field,value', [('run_id', 'other'), ('owner_instance_id', 'other'),
    ('session_id', 'other'), ('task_hash', 'other'), ('epoch', 1), ('epoch', True)])
def test_web_request_binding_cannot_authorize_another_task(web_owner, field, value):
    bridge, engine, _ = web_owner
    payload = binding(engine); payload[field] = value
    with pytest.raises(HarnessError):
        bridge.queue_web_control('web_start', payload)
    bridge.process_web_controls(feedback_healthy=True)
    assert not engine.authorized


def test_web_request_rechecks_identity_when_consumed(web_owner):
    bridge, engine, _ = web_owner
    bridge.queue_web_control('web_start', binding(engine))
    engine.epoch += 1
    bridge.process_web_controls(feedback_healthy=True)
    assert not engine.authorized
    assert bridge.web_control_status('start')['reason'] == 'web_control_scope_mismatch'


@pytest.mark.parametrize('fault', ['stale_gripper', 'unhealthy', 'lease', 'not_ready', 'disabled'])
def test_web_authorization_preserves_feedback_and_capability_gates(web_owner, fault):
    bridge, engine, now = web_owner
    payload = binding(engine)
    if fault == 'disabled':
        engine.web_control_enabled = False
        with pytest.raises(HarnessError, match='not_enabled'):
            bridge.queue_web_control('web_start', payload)
        return
    bridge.queue_web_control('web_start', payload)
    if fault == 'stale_gripper':
        stamps = [now[0]]*16; stamps[15] = 0
        bridge.update_feedback(full22=(0.,)*22, measured16=(0.,)*16, stamps16=stamps)
    elif fault == 'lease':
        now[0] += engine.lease_ns + 1
    elif fault == 'not_ready':
        engine.hardware_ready = False
    bridge.process_web_controls(feedback_healthy=fault != 'unhealthy')
    assert not engine.authorized and bridge.web_control_status('start')['lifecycle'] == 'rejected'


def test_stop_before_consumed_start_never_authorizes(web_owner):
    bridge, engine, _ = web_owner
    bridge.queue_web_control('web_start', binding(engine))
    bridge.queue_web_control('web_stop', binding(engine, 'stop'))
    bridge.process_web_controls(feedback_healthy=True)
    assert not engine.authorized
    assert bridge.web_control_status('start')['stage'] == 'cancelled_before_authorization'
    assert bridge.web_control_status('stop')['stage'] == 'no_motion_authorized'


def test_stop_records_request_hold_and_actual_stationarity_separately(web_owner):
    bridge, engine, now = web_owner
    bridge.queue_web_control('web_start', binding(engine))
    bridge.process_web_controls(feedback_healthy=True)
    bridge.queue_web_control('web_stop', binding(engine, 'stop'))
    assert engine.authorized and bridge.web_control_status('stop')['stage'] == 'cancel_requested'
    bridge.process_web_controls(feedback_healthy=True)
    assert not engine.authorized and engine.hold_requested
    bridge.after_publication(publication_id='hold-1', measured16=(0.,)*16,
                             publication_complete=True, now_ns=now[0])
    assert bridge.web_control_status('stop')['stage'] == 'owner_hold_committed'
    now[0] += 200_000_000
    bridge.update_feedback(full22=(0.,)*22, measured16=(0.,)*16, stamps16=(now[0],)*16)
    bridge.after_publication(publication_id='hold-2', measured16=(0.,)*16,
                             publication_complete=True, now_ns=now[0])
    assert bridge.web_control_status('stop')['stage'] == 'measured_stationary'
    assert not engine.hold_requested


def test_web_session_lease_expiry_does_not_restart(web_owner):
    bridge, engine, now = web_owner
    payload = binding(engine)
    bridge.queue_web_control('web_start', payload)
    bridge.process_web_controls(feedback_healthy=True)
    now[0] += engine.lease_ns + 1
    engine.tick()
    assert not engine.authorized and engine.hold_requested
    bridge.queue_web_control('web_start', payload)  # Receipt replay cannot regain authority.
    bridge.process_web_controls(feedback_healthy=True)
    assert not engine.authorized
    with pytest.raises(HarnessError, match='one_shot'):
        bridge.queue_web_control('web_start', binding(engine, 'new-start'))


def test_request_id_reuse_cannot_change_request(web_owner):
    bridge, engine, _ = web_owner
    payload = binding(engine)
    bridge.queue_web_control('web_start', payload)
    with pytest.raises(HarnessError, match='content_conflict'):
        bridge.queue_web_control('web_stop', payload)


@pytest.mark.parametrize('stop_at', ['before_finish','after_close','already_stationary'])
def test_stop_survives_own_finish_and_close_without_resurrecting_motion(web_owner,stop_at):
    bridge,engine,now=web_owner
    bridge.queue_web_control('web_start',binding(engine));bridge.process_web_controls(feedback_healthy=True)
    payload=binding(engine,'stop');session=engine.session
    engine.revoke('candidate_failed')
    if stop_at=='before_finish':bridge.queue_web_control('web_stop',payload)
    engine.finish_task(session)
    if stop_at=='already_stationary':
        for index in range(2):
            now[0]+=200_000_000
            bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(now[0],)*16)
            bridge.after_publication(publication_id=str(index),measured16=(0.,)*16,
                                     publication_complete=True,now_ns=now[0])
        assert engine.hold_confirmation['stage']=='measured_stationary'
    engine.close_session(session)
    if stop_at!='before_finish':bridge.queue_web_control('web_stop',payload)
    bridge.process_web_controls(feedback_healthy=True)
    result=bridge.web_control_status('stop')
    assert result['lifecycle']=='completed' and not engine.authorized
    if stop_at=='already_stationary':assert result['stage']=='measured_stationary'
    else:
        assert result['stage']=='cancel_requested'
        with pytest.raises(HarnessError,match='before_hold'):bridge.web_close_session(payload)
    assert bridge.queue_web_control('web_stop',payload)['duplicate']


def test_old_stop_cannot_target_new_task_or_future_epoch(web_owner):
    bridge,engine,_=web_owner
    old=binding(engine,'old-stop')
    engine.revoke('finished');engine.prepare_task(engine.session,engine.task.model_dump())
    with pytest.raises(HarnessError,match='scope_mismatch'):bridge.queue_web_control('web_stop',old)
    future=binding(engine,'future');future['epoch']+=1
    with pytest.raises(HarnessError,match='scope_mismatch'):bridge.queue_web_control('web_stop',future)


@pytest.mark.parametrize('cleanup_failure',[False,True])
def test_supervisor_waits_for_real_hold_before_closing_session(web_owner,cleanup_failure):
    from f1_harness.web_execution import WorkspaceSupervisor
    bridge,engine,now=web_owner
    engine.required_task_hash=digest(engine.task)
    bridge.queue_web_control('web_start',binding(engine));bridge.process_web_controls(feedback_healthy=True)
    class Processes:
        expected_task_hash=digest(engine.task)
        expected_owner_instance=engine.instance
        calls=[]
        def stop_planner(self):self.calls.append('stop_planner')
        def owner_call(self,method,**kw):
            self.calls.append(method)
            if method=='status':return engine.status()
            if method=='web_stop':
                # The session can close after status was read, before STOP arrives.
                engine.finish_task(engine.session);engine.close_session(engine.session)
                result=bridge.queue_web_control(method,kw)
                bridge.process_web_controls(feedback_healthy=True)
                return result
            if method=='web_control_status':
                now[0]+=200_000_000
                bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(now[0],)*16)
                bridge.after_publication(publication_id=str(now[0]),measured16=(0.,)*16,
                                         publication_complete=True,now_ns=now[0])
                return bridge.web_control_status(**kw)
            if method=='web_close_session':
                if cleanup_failure:raise ConnectionError('cleanup response lost')
                return bridge.web_close_session(kw)
            raise AssertionError(method)
    processes=Processes();supervisor=object.__new__(WorkspaceSupervisor)
    stage=supervisor._stop_owner(processes,stop_request=True,store=engine.store)
    assert stage=='measured_stationary'
    assert processes.calls.index('stop_planner')<processes.calls.index('web_close_session')
    assert engine.session is None and not engine.authorized
    if cleanup_failure:
        assert 'web_cleanup_incomplete' in (engine.store.root/'trace.jsonl').read_text()
