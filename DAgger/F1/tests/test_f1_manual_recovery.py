"""Manual protection pauses retain one archive, without replay or reauthorization."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import time

import numpy as np
import pytest

from dagger_core.hil_supervisor import HILState
from f1dagger.input_recovery import RecoveryChallenges
from f1dagger.robot_io import CandidateSafetyRejected, CollisionGate
from f1dagger.runtime import ControlLoop, receiver_status
from test_f1_demonstration import wait_for
from test_f1_episode_controls import make_episode_engine
from test_f1_output import make_owner
from test_f1_contract import mapping
from test_real_observation_shadow import packet


@pytest.fixture(params=[False, True], ids=['demonstration', 'rtc_human'])
def manual(request, tmp_path):
    e, recorder, calls, _ = make_episode_engine(tmp_path, request.param)
    owner, transport = make_owner()
    now = [time.monotonic_ns()]
    measured = owner.last_submitted.copy()
    feedback = lambda: (np.r_[measured[:14], np.zeros(8)], measured.copy(), (now[0],) * 16)
    owner.source.feedback = e.source.feedback = feedback
    e.source.state_valid_mask = (True,) * 16  # Real legacy manual source contract.
    proof = RecoveryChallenges()
    e.recovery_proof = proof.age
    controls = []
    original_control = recorder.control
    recorder.control = lambda **fields: controls.append(fields) is None and original_control(**fields)
    def target(p, full, held, dt):
        value = held.copy()
        value[7] += p.axes[0] * .1
        return value
    arm = SimpleNamespace(release=lambda: None, target=target)
    loop = ControlLoop(e, owner, arm, recorder, {'joint_tolerance_deg': 1.}, clock=lambda: now[0])
    seq = [0]
    toggle = [0]
    end = [0]
    session = ['manual-test']
    def send(*, start=False, save=False, proven=True, **fields):
        if start: toggle[0] += 1
        if save: end[0] += 1
        now[0] = max(now[0] + 20_000_000, time.monotonic_ns())
        p = replace(packet(seq[0], toggle[0]), session_id=session[0], active_side='right',
                    episode_end_id=end[0], episode_end_pressed=save,
                    recovery_token=proof.issue(session[0], now[0]) if proven else None, **fields)
        seq[0] += 1
        loop.step(now[0], (p, now[0]))
        return p
    send(); send(start=True)
    wait_for(lambda: recorder.phase == 'recording')
    send(start=request.param)
    assert e.supervisor.state is HILState.HIL_LATCHED
    # The archive is ready and any measured takeover anchor has committed.
    send()
    value = SimpleNamespace(e=e, r=recorder, o=owner, t=transport, loop=loop, now=now,
        measured=measured, proof=proof, send=send, session=session, records=controls, calls=calls)
    yield value
    e.close(); recorder.close()


def veto_new_candidate(m):
    def gate(target, full):
        if target[7] > .05:
            raise CandidateSafetyRejected('F1 collision/CoM rejected target: arm_R_Link5/body_Link5')
    m.o.gate.check = gate
    m.send(deadman=True, axes=(1., 0., 0.))
    assert m.e.input_pause and m.e.input_pause['reason'] == 'manual_candidate_collision'
    assert m.e.supervisor.state is HILState.SAFE_HOLD


def test_actual_collision_gate_uses_typed_prepublication_veto():
    gate = object.__new__(CollisionGate)
    gate.mapping = SimpleNamespace(to_model=lambda x: np.array(x))
    gate.protector = SimpleNamespace(set_feedback=lambda x: None,
        run=lambda x: (False, [], None, {}), last_reason='arm_R_Link5/body_Link5')
    with pytest.raises(CandidateSafetyRejected, match='arm_R_Link5'):
        gate.check(np.zeros(16), np.zeros(22))


@pytest.mark.parametrize('row', json.loads((Path(__file__).parent/'fixtures/manual_candidate_stops_20261023.json').read_text()),
                         ids=lambda row: row['run_id'])
def test_recorded_stops_with_real_mapping_can_pause_and_reanchor_without_home_tolerance(manual, mapping, row):
    m = manual
    submitted, measured = np.array(row['submitted']), np.array(row['measured'])
    assert np.max(np.abs(submitted[:14]-measured[:14])) > m.loop.profile['joint_tolerance_deg']
    mapping.check_action(submitted)
    mapping.check_action(measured)
    m.o.mapping = mapping
    m.o.last_submitted = submitted.copy()
    m.loop.reset_targets(m.now[0], m.loop.last_packet)
    m.measured[:] = measured
    rejected = submitted.copy(); rejected[7] += .1
    checked = []
    def gate(value, full):
        checked.append(np.array(value))
        if np.array_equal(value, rejected):
            raise CandidateSafetyRejected('F1 collision/CoM rejected target: arm_R_Link5/body_Link5')
    m.o.gate.check = gate
    m.send(deadman=True, axes=(1., 0., 0.))
    run = m.r.run_id
    assert m.e.input_pause is not None and m.r.phase == 'recording'
    assert any(np.array_equal(value[:14], measured[:14]) for value in checked)
    m.send(controls_neutral=True); m.send(deadman=True)
    assert m.e.input_pause is None and m.r.run_id == run
    np.testing.assert_array_equal(m.o.last_submitted[7:14], measured[7:14])
    np.testing.assert_array_equal(m.o.last_submitted[:7], submitted[:7])
    np.testing.assert_array_equal(m.o.last_submitted[14:], submitted[14:])
    assert m.records[-1]['source'] == 'input_recovery_anchor'


def test_collision_keeps_same_recording_and_release_repress_recovers_without_start(manual):
    m = manual
    run = m.r.run_id
    old_epoch = m.e.supervisor.policy_epoch
    held = m.o.last_submitted.copy()
    veto_new_candidate(m)
    assert m.r.run_id == run and m.r.phase == 'recording'
    assert m.e.supervisor.policy_epoch > old_epoch
    assert m.records[-1]['source'] == 'input_pause_hold'
    assert m.records[-1]['candidate'] is None and m.records[-1]['policy'] is None
    for _ in range(3):
        m.send(deadman=True, axes=(-1., 0., 0.), grip_target=0.)
        assert m.e.input_pause is not None
        np.testing.assert_array_equal(m.o.last_submitted, held)
    m.send(start=True, hil_button_pressed=True)
    assert m.r.run_id == run and m.r.phase == 'recording' and m.e.input_pause
    m.send(controls_neutral=True)
    assert receiver_status(m.e) == 'input_wait_fresh_packet'
    m.send(controls_neutral=True)
    assert receiver_status(m.e) == 'input_wait_press_rb'
    m.send(deadman=True, axes=(-1., 0., 0.), grip_target=0.)
    assert m.e.input_pause is None and m.r.run_id == run
    assert m.records[-1]['source'] == 'input_recovery_anchor'
    np.testing.assert_array_equal(m.o.last_submitted[14:], held[14:])
    m.send(deadman=True, axes=(-1., 0., 0.), grip_target=0.)
    assert m.o.last_submitted[7] == pytest.approx(-.1)
    assert m.records[-1]['source'] == 'human_arm'
    assert not any(row['source'] == 'policy' for row in m.records)
    m.send(save=True)
    wait_for(lambda: m.r.phase == 'idle')
    assert len(list(m.r.root.iterdir())) == 1
    events = [json.loads(line) for line in (m.r.root/run/'journal/events.jsonl').read_text().splitlines()]
    assert any(x['event'] == 'input_pause_started' for x in events)
    assert any(x['event'] == 'input_pause_ended' and x['outcome'] == 'rb_rearmed' for x in events)


def test_save_during_pause_closes_same_episode_even_without_completed_terminal_hold(manual):
    m = manual
    veto_new_candidate(m)
    m.e.episode_buttons.home_goal = m.o.last_submitted.copy()
    m.e.episode_buttons.hold_since = None
    m.send(save=True)
    wait_for(lambda: m.r.phase == 'idle')
    manifest = json.loads((m.r.root/m.r.run_id/'journal/manifest.json').read_text())
    assert manifest['complete']
    events = [json.loads(line) for line in (m.r.root/m.r.run_id/'journal/events.jsonl').read_text().splitlines()]
    saved = [x for x in events if x['event'] == 'episode_end_accepted'][-1]
    assert saved['paused'] and not saved['training_ready']
    assert saved['pause_reason'] == 'manual_candidate_collision'
    assert any(x['event'] == 'input_pause_ended' and x['outcome'] == 'operator_finished' for x in events)


@pytest.mark.parametrize('bad_anchor', ['limits', 'current_collision'])
def test_unsafe_measured_anchor_cannot_resume_and_held_rb_cannot_retry(manual, bad_anchor):
    m = manual
    veto_new_candidate(m)
    check = m.o.mapping.check_action
    gate = m.o.gate.check
    if bad_anchor == 'limits':
        m.measured[7] = -.2
        def reject_illegal(value):
            if value[7] < 0: raise ValueError('right J2 outside legal command range')
            return check(value)
        m.o.mapping.check_action = reject_illegal
    elif bad_anchor == 'current_collision':
        m.o.gate.check = lambda *args: (_ for _ in ()).throw(CandidateSafetyRejected('current measured collision'))
    m.send(controls_neutral=True)
    m.send(deadman=True)
    assert m.e.input_pause and receiver_status(m.e) == 'input_wait_safe_anchor'
    assert m.e.input_pause['recovery_error']
    m.measured[7] = 0.
    m.o.mapping.check_action, m.o.gate.check = check, gate
    m.send(deadman=True)
    assert m.e.input_pause  # A previous rejected press is consumed.
    m.send(controls_neutral=True); m.send(deadman=True)
    assert m.e.input_pause is None


@pytest.mark.parametrize('fault', ['operator_disconnected', 'controller_disconnected', 'operator_watchdog'])
def test_transport_and_controller_gaps_keep_episode_and_never_restart_from_held_rb(manual, fault):
    m = manual
    run = m.r.run_id
    if fault == 'controller_disconnected':
        m.send(warning=fault)
    else:
        m.now[0] += 20_000_000
        m.loop.step(m.now[0], disconnected=fault)
    assert m.e.input_pause and m.r.phase == 'recording'
    if fault == 'operator_disconnected':
        m.session[0] = 'replacement'
        m.send(deadman=True)  # Replacement-session counters are only anchored.
    m.send(deadman=True)
    assert m.e.input_pause
    m.send(controls_neutral=True, proven=False)
    m.send(deadman=True)
    assert m.e.input_pause
    m.send(controls_neutral=True)
    m.send(deadman=True)
    assert m.e.input_pause is None and m.r.run_id == run
    assert m.records[-1]['source'] == 'input_recovery_anchor'


def test_second_controller_loss_revokes_previously_proven_neutral(manual):
    m = manual
    m.send(warning='controller_disconnected')
    m.send(controls_neutral=True)
    m.send(warning='controller_disconnected')
    m.send(deadman=True)
    assert m.e.input_pause
    m.send(controls_neutral=True); m.send(deadman=True)
    assert m.e.input_pause is None


@pytest.mark.parametrize('fault', ['current_pose', 'unknown_owner', 'protocol', 'publish'])
def test_hard_failures_never_acquire_rb_recovery(manual, fault):
    m = manual
    if fault == 'current_pose':
        m.o.gate.check = lambda *args: (_ for _ in ()).throw(CandidateSafetyRejected('held pose unsafe'))
        m.send(deadman=True, axes=(1., 0., 0.))
    elif fault == 'unknown_owner':
        m.o.approve = lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('unknown output failure'))
        m.send(deadman=True, axes=(1., 0., 0.))
    elif fault == 'publish':
        m.t.conflict = True
        before = len(m.t.sent)
        with pytest.raises(RuntimeError, match='external command'):
            m.send(deadman=True, axes=(1., 0., 0.))
        assert len(m.t.sent) == before
        return  # The top-level runtime owns fatal publication teardown.
    else:
        m.e.disconnect(m.now[0], 'invalid_operator_packet:decode')
    assert m.e.input_pause is None and m.e.hard_hold_reason
    m.e.disconnect(m.now[0], 'operator_disconnected')
    assert m.e.input_pause is None
    wait_for(lambda: m.r.phase == 'idle')
    manifest = json.loads((m.r.root/m.r.run_id/'journal/manifest.json').read_text())
    assert manifest['binding']['termination']['outcome'] == 'interrupted'
