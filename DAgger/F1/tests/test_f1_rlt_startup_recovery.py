"""Regressions for the onsite START / neutral-ACK failures."""
from dataclasses import replace
from types import SimpleNamespace
import json
import sys
import time

import pytest
from dagger_core.hil_supervisor import HILState
from f1dagger.input_recovery import RecoveryChallenges
from f1dagger.policy import F1PolicyEngine
from test_real_observation_shadow import packet
from test_f1_rlt_hil import make_engine, episode_packet, send_operator


def paused():
    proof = RecoveryChallenges()
    events = []
    pause = dict(awaiting_session=False, session='s', retired_sessions=set(),
                 sequence=0, toggle=0, received=900_000_000, neutral=None,
                 previous_rb=False, proof_after=900_000_000)
    e = SimpleNamespace(input_pause=pause, supervisor=SimpleNamespace(), fault_lifecycle=False,
        watchdog_ns=200_000_000, emit=lambda name, **kw: events.append(name),
        unified_episodes=True, recovery_proof=proof.age,
        session=SimpleNamespace(process=lambda p: None))
    return e, proof, events


def test_onsite_delayed_neutral_ack_does_not_advertise_rb_too_early():
    e, proof, events = paused()
    token = proof.issue('s', 1_000_000_000)
    p = replace(packet(1), controls_neutral=True, recovery_token=token)
    F1PolicyEngine._paused_operator(e, p, 1_188_848_733, 1_188_848_733)
    assert e.supervisor.hold_reason == 'input_wait_fresh_packet'
    assert 'input_recovery_ready' not in events
    F1PolicyEngine._paused_operator(e, replace(p, sequence=2), 1_197_076_000, 1_197_076_000)
    assert e.input_pause['neutral'] == 1_188_848_733
    assert 'input_recovery_ready' not in events
    fresh = proof.issue('s', 1_199_000_000)
    F1PolicyEngine._paused_operator(e, replace(p, sequence=3, recovery_token=fresh), 1_250_000_000, 1_250_000_000)
    assert e.supervisor.hold_reason == 'input_wait_press_rb'
    assert events.count('input_recovery_ready') == 1


def test_early_rb_and_expired_neutral_never_queue_permission():
    for rb, now in [(True, 1_197_076_000), (False, 1_210_000_000)]:
        e, proof, events = paused()
        token = proof.issue('s', 1_000_000_000)
        p = replace(packet(1), controls_neutral=True, recovery_token=token)
        F1PolicyEngine._paused_operator(e, p, 1_188_848_733, 1_188_848_733)
        F1PolicyEngine._paused_operator(e, replace(p, sequence=2, deadman=rb, controls_neutral=not rb), now, now)
        assert e.input_pause['neutral'] is None
        assert 'input_recovery_ready' not in events


def test_reconnect_handshake_keeps_manual_pause_without_hard_fault():
    e, j, _, _ = make_engine()
    try:
        send_operator(e, 0)
        e.supervisor.state = HILState.HIL_LATCHED
        now = time.monotonic_ns()
        p = replace(episode_packet(0, 0), session_id='reconnected',
                    policy_switch_id=None, policy_switch_pressed=None, operator_protocol=None, home_end_id=None, local_result=None)
        assert not e.operator(p, now, now)
        assert e.input_pause and e.hard_hold_reason is None
        assert e.supervisor.state is HILState.SAFE_HOLD
        now = time.monotonic_ns()
        assert not e.operator(replace(p, sequence=1, policy_switch_id=0, policy_switch_pressed=False, operator_protocol='f1_rlt_local_v4', local_result='success', home_end_id=0), now, now)
        assert e.input_pause and e.hard_hold_reason is None
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:
        e.close()


def test_network_preparation_connects_without_sending_model_request(monkeypatch):
    from f1dagger import websocket_policy as mod
    sent, replies, connections = [], [], []
    connection = SimpleNamespace(send=sent.append, recv=lambda **kw: b'{"success":true}', close=lambda: None)
    def connect(*a, **kw):
        connections.append(a)
        return connection
    monkeypatch.setitem(sys.modules, 'websockets.sync.client', SimpleNamespace(connect=connect))
    monkeypatch.setattr(mod.signal, 'signal', lambda *a: None)
    packets = iter([b'', b'real-request'])
    def recv(*a):
        try: return next(packets)
        except StopIteration: raise EOFError
    pipe = SimpleNamespace(recv_bytes=recv, send_bytes=lambda b: replies.append(json.loads(b)), close=lambda: None)
    mod.network_worker(pipe, 'ws://local/ws', 1.)
    assert len(connections) == 1 and sent == [b'real-request']
    assert replies[0]['transport_ready'] is True and replies[1]['success'] is True


def test_failed_prepare_closes_transport_and_releases_inflight():
    from test_f1_websocket_policy import client
    p, _, _ = client()
    calls, closed = [], []
    p._exchange = lambda data: calls.append(data) or {'success': False, 'error': 'connect failed'}
    p.close = lambda: closed.append(True)
    with pytest.raises(ValueError, match='preparation failed'):
        p.prepare()
    assert calls == [b''] and closed == [True] and not p.inflight.locked()


def authorized_start_wait():
    e, journal, base_calls, _ = make_engine()
    e.fault_lifecycle = True
    e.episode_buttons.reset_start(time.monotonic_ns())
    e.episodes.phase = 'idle'
    e.episodes.start = lambda now: setattr(e.episodes, 'phase', 'recording') or True
    send_operator(e, 0, 0)
    send_operator(e, 1, 1, hil_button_pressed=True)
    assert e.started and e.startup_policy_authorized
    e.episode_buttons.poll(time.monotonic_ns())
    now = time.monotonic_ns()
    e.last_received_ns = now-220_000_000
    e.disconnect(now, 'operator_watchdog')
    assert e.input_pause['startup_policy_wait'] and not e.policy_enabled
    proof = RecoveryChallenges()
    e.recovery_proof = proof.age
    token = proof.issue(e.packet.session_id, now+10_000_000)
    fresh = replace(episode_packet(2, 1), controls_neutral=True, recovery_token=token)
    return e, journal, base_calls, fresh, now+30_000_000


def test_accepted_first_start_resumes_without_second_start_or_rb():
    e, journal, calls, fresh, now = authorized_start_wait()
    try:
        old_epoch = e.supervisor.policy_epoch
        old_intervention = e.supervisor.intervention_id
        assert e.operator(fresh, now, now)
        assert e.input_pause is None and e.policy_enabled
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.minimum_observation_ns == now
        assert e.supervisor.policy_epoch > old_epoch
        assert e.supervisor.intervention_id == old_intervention
        assert not calls and not e.episode_buttons.opening
        assert any(n == ('startup_input_restored',) for n, _ in journal.events)
    finally:
        e.close()


@pytest.mark.parametrize('reason', ['operator_disconnected', 'operator_session_changed',
    'controller_disconnected', 'target_rejected:collision'])
def test_real_disconnect_or_hardware_fault_revokes_startup_resume(reason):
    e, journal, _, fresh, now = authorized_start_wait()
    try:
        e.disconnect(now, reason)
        assert not e.startup_policy_authorized
        assert not (e.input_pause or {}).get('startup_policy_wait')
        e.operator(fresh, now+1, now+1)
        assert not any(n == ('startup_input_restored',) for n, _ in journal.events)
        assert not e.policy_enabled
    finally:
        e.close()


def test_delayed_challenge_cannot_resume_and_recording_ready_cannot_bypass_wait():
    e, _, _, fresh, now = authorized_start_wait()
    try:
        e.episode_buttons.opening = True
        e.episode_buttons.poll(now)
        assert not e.policy_enabled and e.input_pause
        e.operator(replace(fresh, recovery_token=None), now, now)
        assert e.supervisor.hold_reason == 'policy_wait_operator'
        assert not e.policy_enabled and e.input_pause
    finally:
        e.close()


def test_published_policy_gets_bounded_same_session_resume_lease():
    e, _, _, fresh, now = authorized_start_wait()
    try:
        e.operator(fresh, now, now)
        e.supervisor.state = HILState.POLICY
        e.after_publication(dict(publication_complete=True, execution_mode='powered'), powered=True)
        assert not e.startup_policy_authorized
        e.disconnect(now+220_000_000, 'operator_watchdog')
        assert e.input_pause['takeover_required']
        assert e.input_pause.get('policy_input_wait')
        assert e._input_resume_lease['deadline'] == now+1_000_000_000
    finally:
        e.close()


def test_second_start_during_wait_takes_over_instead_of_auto_starting():
    e, _, _, fresh, now = authorized_start_wait()
    try:
        assert e.operator(replace(fresh, hil_toggle_id=2, hil_button_pressed=True,
                                  controls_neutral=False), now, now)
        assert e.input_pause and not e.input_pause['takeover_required']
        assert not e.startup_policy_authorized and not e.policy_enabled
    finally:
        e.close()


def test_invalid_packets_do_not_extend_policy_resume_deadline():
    e, _, _, fresh, now = authorized_start_wait()
    try:
        deadline = e._input_resume_lease['deadline']
        for index in range(3):
            t = now+index*200_000_000
            e.operator(replace(fresh, sequence=3+index, recovery_token=None), t, t)
            assert e._input_resume_lease['deadline'] == deadline
        e.operator(replace(fresh, sequence=6), deadline+1, deadline+1)
        assert e.input_pause is not None and not e.policy_enabled
    finally:
        e.close()
