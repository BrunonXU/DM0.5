"""Faults revoke motion without destroying supervision or local task identity."""
import numpy as np
import pytest
from dagger_core.hil_supervisor import HILState
from f1dagger.faults import FeedbackUnavailable
from f1dagger.runtime import SupervisedControlLoop
from test_f1_preventive_boundary import system


@pytest.fixture
def supervised(system):
    m = system
    m.loop.__class__ = SupervisedControlLoop
    m.loop.fault = None
    m.e.fault_lifecycle = True
    return m


def test_feedback_outage_retains_local_task_and_requires_fresh_reauthorization(supervised):
    m = supervised
    m.e.rlt_phase = 'actor'
    m.e.local_task.begin('episode', m.now[0], 'actor')
    feedback = m.o.source.feedback
    m.o.source.feedback = lambda: (_ for _ in ()).throw(FeedbackUnavailable('temporary outage'))
    count = m.o.sequence
    m.send()
    assert m.loop.fault['retryable'] and m.e.local_task.status == 'active'
    assert m.e.input_pause and m.o.sequence == count
    m.send(start=True)
    assert m.o.sequence == count and m.e.local_task.status == 'active'
    m.o.source.feedback = feedback
    m.send(controls_neutral=True)
    assert m.loop.fault is None and m.e.input_pause
    assert m.o.sequence == count
    m.send(controls_neutral=True)
    assert m.e.supervisor.state is HILState.SAFE_HOLD


def test_unknown_publication_failure_latches_but_keeps_supervisor_alive(supervised):
    m = supervised
    m.e.local_task.begin('episode', m.now[0], 'actor')
    m.o.publish = lambda now: (_ for _ in ()).throw(RuntimeError('partial publication'))
    m.send()
    assert m.loop.fault and not m.loop.fault['retryable']
    count = m.o.sequence
    m.send(start=True, deadman=True)
    assert m.o.sequence == count and m.e.supervisor.state is HILState.SAFE_HOLD


def test_manual_long_dt_reanchors_without_ending_episode(supervised):
    m = supervised
    m.e.supervisor.state = HILState.HIL_LATCHED
    m.e.local_task.begin('episode', m.now[0], 'actor')
    m.loop.previous = (m.e.supervisor.state, m.e.supervisor.policy_epoch)
    m.loop.last_target = m.now[0] - 100_000_000
    m.loop.next_target = 0
    m.send(deadman=True)
    assert m.loop.fault is None and m.e.input_pause is None
    assert m.e.local_task.status == 'active' and m.e.episodes.phase == 'recording'
    assert m.e.chunk is None and m.e.supervisor.state is HILState.HIL_LATCHED


def test_recording_fault_stops_collection_but_allows_explicit_manual_exit(supervised):
    m = supervised
    m.e.local_task.begin('episode', m.now[0], 'actor')
    m.journal.control = lambda **kw: False
    m.send()
    assert m.e.recording_failed and m.e.episodes is None
    assert m.e.input_pause['takeover_required'] and not m.e.policy_enabled
    assert m.e.local_task.status == 'interrupted'
    assert m.loop.fault is None
    m.send(start=True)
    m.send(controls_neutral=True)
    m.send(controls_neutral=True)
    m.send(deadman=True)
    assert m.e.supervisor.state is HILState.HIL_LATCHED
    m.send(controls_neutral=True)
    m.send(start=True)
    assert m.e.supervisor.state is HILState.HIL_LATCHED and not m.e.policy_enabled
    assert m.e.home_request is False


@pytest.mark.parametrize('reason', ['operator_watchdog', 'operator_disconnected',
                                  'policy_failed:timeout', 'camera_unavailable:gap'])
def test_first_transient_fault_preserves_local_segment(system, reason):
    m = system
    m.e.fault_lifecycle = True
    m.e.local_task.begin('episode', m.now[0], 'actor')
    m.e.supervisor.state = HILState.HIL_LATCHED
    m.e.disconnect(m.now[0], reason)
    assert m.e.local_task.status == 'active'
    assert m.e.episodes.phase == 'recording'


def test_restarted_guard_requires_new_epoch(tmp_path):
    from f1dagger.policy_safety import RestartablePlanGuard
    from test_f1_plan_guard_process import observation
    spec = {'crash': True}
    guard = RestartablePlanGuard(spec, lambda *a, **k: None,
        factory='test_f1_plan_guard_process:fake_gate', startup_timeout_s=5., request_timeout_s=.1)
    try:
        with pytest.raises(RuntimeError): guard(observation(), np.zeros((16, 16)), 1, 1)
        spec.clear()
        with pytest.raises(RuntimeError, match='new control epoch'):
            guard(observation(), np.zeros((16, 16)), 1, 2)
        guard(observation(), np.zeros((16, 16)), 2, 3)
        assert guard.worker._process.is_alive()
    finally:
        guard.close()
    with pytest.raises(RuntimeError, match='closed'):
        guard(observation(), np.zeros((16, 16)), 3, 4)


def test_derived_receipt_failure_does_not_poison_raw_and_retries_on_close(tmp_path, monkeypatch):
    import json
    from f1dagger.recording import F1Recorder
    r = F1Recorder(tmp_path/'journal', episode_id='e', task_prompt='place')
    write = r._write_local_receipt
    attempts = []
    def flaky(handles):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('derived directory temporarily unavailable')
        return write(handles)
    monkeypatch.setattr(r, '_write_local_receipt', flaky)
    assert r.record_event('rlt_local_closed', monotonic_ns=10, reward=1)
    assert r.close()
    assert len(attempts) == 2 and r.writer_error is None and r.receipt_error is None
    receipt = json.loads((tmp_path/'local_segments/placement-1.json').read_text())
    assert receipt['local_result']['reward'] == 1
    assert json.loads((tmp_path/'journal/manifest.json').read_text())['complete']


def test_derived_index_and_broken_console_do_not_skip_raw_commit(tmp_path, monkeypatch):
    import json, time
    from test_f1_demonstration import recorder, wait_for
    from f1dagger import lifecycle
    r = recorder(tmp_path)
    try:
        assert r.start(time.monotonic_ns())
        wait_for(lambda: r.phase == 'recording')
        def fail(*a, **k): raise OSError('index/display unavailable')
        monkeypatch.setattr(lifecycle, 'atomic', fail)
        monkeypatch.setattr('builtins.print', fail)
        r.finish(time.monotonic_ns())
        wait_for(lambda: r.phase == 'idle')
        assert r.last_saved['phase'] == 'saved'
        assert 'notification_error' in r.last_saved and r.report_error
        raw = tmp_path/'data'/r.run_id
        assert json.loads((raw/'journal/manifest.json').read_text())['complete']
        assert json.loads((raw/'payload/manifest.json').read_text())['complete']
    finally:
        r.close()


def test_new_standalone_network_fault_waits_for_local_consent():
    from test_f1_standalone import setup
    e, loop, transport, clock, stop = setup(duration=None)
    e.fault_lifecycle = True
    try:
        e.authorize_local(clock[0])
        e.policy_failed(TimeoutError('model delayed'), clock[0]+1)
        assert e.ik_state == 'paused' and not e.policy_enabled
        assert e.ready is None and e.chunk is None and not transport.sent
        e.recovery_hold_committed(clock[0]+2)
        assert e.ik_state == 'paused' and not e.policy_enabled
        assert e.resume_local(clock[0]+3)
        assert e.ik_state == 'retrying' and not e.policy_enabled
    finally:
        e.close()


def test_missing_camera_blocks_rearm_until_new_observations(supervised):
    m = supervised
    m.e.local_task.begin('episode', m.now[0], 'actor')
    m.e.camera_issue = 'right_wrist missing'
    m.e.disconnect(m.now[0], 'camera_unavailable:gap')
    assert m.e.local_task.status == 'active' and m.e.input_pause
    for _ in range(3): m.send(controls_neutral=True, deadman=True)
    assert m.e.input_pause and m.e.supervisor.state is HILState.SAFE_HOLD
    assert m.e.local_task.status == 'active'
