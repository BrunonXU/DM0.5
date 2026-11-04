"""Transport recovery with real episode writers and zero hardware output."""
from contextlib import contextmanager
from dataclasses import replace
import json
import socket
import time
from types import SimpleNamespace

import numpy as np
import pytest

from dagger_core.hil_supervisor import HILState
from dagger_core.protocol import encode_packet
from f1dagger.input_recovery import RecoveryChallenges
from f1dagger.network import IntentServer
from test_f1_demonstration import recorder, wait_for
from test_f1_recording_audio import Cues
from test_f1_runtime import setup_loop
from test_real_observation_shadow import packet


@contextmanager
def recording_loop(tmp_path):
    loop, engine, owner, transport, now, _, step = setup_loop()
    cues = Cues()
    writer = recorder(tmp_path, audio=cues)
    engine.episodes = engine.recorder = engine.sink = loop.recorder = writer
    events = []
    def emit(name, **fields):
        events.append((name, fields))
        assert writer.record_event(name, **fields)
    engine.emit = emit
    proofs = RecoveryChallenges()
    engine.recovery_proof = proofs.age
    engine.source.feedback = lambda: (np.zeros(22), np.zeros(16), (now[0],) * 16)
    owner.source.feedback = lambda: (np.zeros(22), owner.last_submitted.copy(), (now[0],) * 16)
    def send(seq, toggle=1, *, session='s', token=True, **fields):
        proof = proofs.issue(session, now[0] + 1) if token is True else token
        step(seq, toggle, session_id=session, recovery_token=proof, **fields)
    def disconnect(reason):
        now[0] += 4_000_000
        loop.step(now[0], disconnected=reason)
    try:
        step(0)
        step(1, 1)
        wait_for(lambda: writer.phase == 'recording')
        send(2)
        yield SimpleNamespace(loop=loop, engine=engine, owner=owner, transport=transport,
                              now=now, writer=writer, cues=cues, proofs=proofs,
                              send=send, disconnect=disconnect, events=events)
    finally:
        engine.close()
        writer.close()


@pytest.mark.parametrize('reason', ['operator_disconnected', 'operator_transport_error:timed out'])
def test_transport_loss_keeps_episode_and_new_session_requires_neutral_then_new_rb(tmp_path, reason):
    with recording_loop(tmp_path) as t:
        t.send(3, deadman=True)
        held = t.owner.last_submitted.copy()
        run = t.writer.run_id
        t.disconnect(reason)
        assert t.writer.phase == 'recording' and t.writer.run_id == run
        assert t.engine.supervisor.state is HILState.SAFE_HOLD
        assert t.engine.input_pause['awaiting_session']
        assert t.cues.events == ['started']
        # Even a fresh connection's pressed START/RB is only an anchor.
        t.send(0, 25, session='new', deadman=True, hil_button_pressed=True)
        assert t.writer.phase == 'recording' and t.writer.run_id == run
        for seq in range(1, 4):
            t.send(seq, 25, session='new', deadman=True, grip_target=1.)
            np.testing.assert_array_equal(t.owner.last_submitted, held)
        t.send(4, 25, session='new', controls_neutral=True)
        assert t.engine.supervisor.hold_reason == 'input_wait_fresh_packet'
        np.testing.assert_array_equal(t.owner.last_submitted, held)
        # The sender must echo a challenge issued after the neutral sample
        # before the UI can promise that a new RB press is ready.
        t.send(5, 25, session='new', controls_neutral=True)
        assert t.engine.supervisor.hold_reason == 'input_wait_press_rb'
        t.send(6, 25, session='new', deadman=True)
        assert t.engine.input_pause is None
        assert t.engine.supervisor.state is HILState.HIL_LATCHED
        assert t.writer.phase == 'recording' and t.writer.run_id == run
        np.testing.assert_array_equal(t.owner.last_submitted, held)
        t.send(7, 25, session='new', deadman=True)
        assert t.owner.last_submitted[0] > held[0]
        assert t.cues.events == ['started']
        # START ends the current episode; only another press opens the next.
        t.send(8, 25, session='new')
        t.send(9, 26, session='new', hil_button_pressed=True)
        wait_for(lambda: t.writer.phase == 'idle')
        assert t.writer.run_id == run and t.cues.events == ['started', 'saved']
        manifest = json.loads((tmp_path / 'data' / run / 'journal/manifest.json').read_text())
        assert manifest['complete'] and not manifest['binding'].get('termination')
        t.send(10, 26, session='new')
        t.send(11, 27, session='new', hil_button_pressed=True)
        wait_for(lambda: t.writer.phase == 'recording')
        assert t.writer.run_id != run and t.cues.events == ['started', 'saved', 'started']


def test_new_session_without_observed_eof_and_first_start_only_anchor(tmp_path):
    with recording_loop(tmp_path) as t:
        run = t.writer.run_id
        held = t.owner.last_submitted.copy()
        t.send(0, 90, session='new', hil_button_pressed=True)
        assert t.engine.input_pause and t.writer.run_id == run
        assert t.writer.phase == 'recording' and t.cues.events == ['started']
        np.testing.assert_array_equal(t.owner.last_submitted, held)
        t.send(1, 90, session='new')
        t.send(2, 91, session='new', hil_button_pressed=True)
        wait_for(lambda: t.writer.phase == 'idle')
        assert t.writer.run_id == run and t.cues.events == ['started', 'saved']


def test_old_session_token_held_rb_and_stale_feedback_cannot_recover(tmp_path):
    with recording_loop(tmp_path) as t:
        held = t.owner.last_submitted.copy()
        t.disconnect('operator_disconnected')
        t.send(3, controls_neutral=True)  # The closed session cannot be reused.
        assert t.engine.input_pause['awaiting_session']
        t.send(0, session='new')
        old = t.proofs.issue('s', t.now[0] + 1)
        t.send(1, session='new', token=old, controls_neutral=True)
        assert t.engine.input_pause['neutral'] is None
        t.send(2, session='new', controls_neutral=True)
        t.engine.source.feedback = lambda: (np.zeros(22), np.zeros(16), (1,) * 16)
        t.send(3, session='new', deadman=True)
        assert t.engine.supervisor.hold_reason == 'input_wait_feedback'
        t.engine.source.feedback = lambda: (np.zeros(22), np.zeros(16), (t.now[0],) * 16)
        t.send(4, session='new', deadman=True)
        assert t.engine.input_pause
        np.testing.assert_array_equal(t.owner.last_submitted, held)
        t.send(5, session='new', controls_neutral=True)
        t.send(6, session='new', deadman=True)
        assert t.engine.input_pause is None


@pytest.mark.parametrize('fault', ['target_rejected:collision', 'target_rejected:stale F1 joint feedback',
                                  'publication_failed:test', 'invalid_operator_packet:decode',
                                  'camera_unavailable:timeout'])
def test_hard_fault_cannot_be_downgraded_by_eof_new_session_or_fresh_rb(tmp_path, fault):
    with recording_loop(tmp_path) as t:
        t.disconnect('operator_disconnected')
        t.disconnect(fault)
        wait_for(lambda: t.writer.phase == 'idle')
        held = t.owner.last_submitted.copy()
        t.disconnect('operator_disconnected')
        t.disconnect('operator_transport_error:timed out')
        assert t.engine.input_pause is None and t.engine.hard_hold_reason == fault
        assert t.engine.supervisor.hold_reason == fault
        t.send(0, session='new', controls_neutral=True)
        t.send(1, session='new', controls_neutral=True)
        t.send(2, session='new', deadman=True)
        assert t.engine.supervisor.state is HILState.SAFE_HOLD
        assert t.writer.phase == 'idle' and t.cues.events == ['started']
        assert t.engine.hard_hold_reason == fault
        np.testing.assert_array_equal(t.owner.last_submitted, held)


def test_three_episodes_transport_pause_saves_once_per_explicit_stop(tmp_path):
    with recording_loop(tmp_path) as t:
        ids, seq, toggle, session = [], 3, 1, 's'
        for index in range(3):
            run = t.writer.run_id
            ids.append(run)
            t.disconnect('operator_disconnected')
            session, seq = 'reconnected-' + str(index), 0
            t.send(seq, toggle, session=session)
            t.send(seq + 1, toggle, session=session, controls_neutral=True)
            t.send(seq + 2, toggle, session=session, deadman=True)
            assert t.writer.run_id == run and t.writer.phase == 'recording'
            t.send(seq + 3, toggle, session=session)
            toggle += 1
            t.send(seq + 4, toggle, session=session, hil_button_pressed=True)
            wait_for(lambda: t.writer.phase == 'idle')
            assert t.cues.events == ['started', 'saved'] * (index + 1)
            manifest = json.loads((tmp_path / 'data' / run / 'journal/manifest.json').read_text())
            assert manifest['complete'] and not manifest['writer_error']
            if index < 2:
                t.send(seq + 5, toggle, session=session)
                toggle += 1
                t.send(seq + 6, toggle, session=session, hil_button_pressed=True)
                wait_for(lambda: t.writer.phase == 'recording')
        assert len(set(ids)) == 3
        assert len(list((tmp_path / 'data').glob('*/journal/manifest.json'))) == 3


def test_start_consumed_during_hard_fault_save_cannot_clear_primary_fault():
    loop, engine, owner, transport, now, _, step = setup_loop()
    try:
        step(0)
        step(1, 1)
        fault = 'target_rejected:collision'
        engine.disconnect(now[0], fault)
        engine.episodes = SimpleNamespace(phase='saving', start=lambda _: False)
        step(2, 2)
        assert engine.supervisor.state is HILState.SAFE_HOLD
        assert engine.hard_hold_reason == fault
        engine.disconnect(now[0], 'operator_disconnected')
        assert engine.supervisor.hold_reason == fault
        assert engine.input_pause is None
        # Camera wait cannot replace the original hard-fault reason either.
        engine.camera_issue = 'missing camera'
        step(3, 3)
        assert engine.hard_hold_reason == fault and engine.supervisor.hold_reason == fault
    finally:
        engine.close()


@pytest.mark.parametrize('toggle,reason', [(3, 'hil_toggle_id_gap'), (0, 'hil_toggle_id_regressed')])
def test_supervisor_toggle_fault_is_interrupted_save_not_success(tmp_path, toggle, reason):
    with recording_loop(tmp_path) as t:
        run = t.writer.run_id
        t.send(3, toggle)
        wait_for(lambda: t.writer.phase == 'idle')
        assert t.engine.hard_hold_reason == reason
        assert t.engine.supervisor.state is HILState.SAFE_HOLD
        assert t.cues.events == ['started']
        manifest = json.loads((tmp_path / 'data' / run / 'journal/manifest.json').read_text())
        assert manifest['complete']
        assert manifest['binding']['termination']['reason'] == reason
        t.disconnect('operator_disconnected')
        assert t.engine.supervisor.hold_reason == reason and t.engine.input_pause is None


def test_transport_pause_invalid_interval_survives_session_anchor_until_explicit_recovery(tmp_path):
    from f1dagger.export import input_invalid_intervals
    with recording_loop(tmp_path) as t:
        t.disconnect('operator_disconnected')
        start = t.engine.input_pause['invalid_from']
        t.send(0, session='new')
        t.send(1, session='new', controls_neutral=True)
        events = lambda: [dict(event=name, **fields) for name, fields in t.events]
        assert input_invalid_intervals(events()) == [(start, None)]
        t.send(2, session='new', deadman=True)
        assert input_invalid_intervals(events()) == [(start, t.now[0])]


@pytest.mark.parametrize('bad', ['malformed', 'same_connection_session_change', 'replay'])
def test_receiver_reports_protocol_errors_separately_from_transport(bad):
    server = IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(), timeout=1) as client:
            client.sendall(encode_packet(packet(0)))
            assert json.loads(client.recv(4096))['accepted']
            server.snapshot('hil_latched')
            wire = (b'{}\n' if bad == 'malformed' else
                    encode_packet(replace(packet(1), session_id='new')) if bad == 'same_connection_session_change' else
                    encode_packet(packet(0)))
            client.sendall(wire)
            result = []
            def received():
                _, reason = server.snapshot('hil_latched')
                if reason:
                    result.append(reason)
                return bool(result)
            wait_for(received)
            assert result[0].startswith('invalid_operator_packet')
            assert not result[0].startswith('operator_transport_error:')
    finally:
        server.close()


def test_receiver_eof_is_a_recoverable_transport_event():
    server = IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(), timeout=1) as client:
            client.sendall(encode_packet(packet(0)))
            assert json.loads(client.recv(4096))['accepted']
            server.snapshot('hil_latched')
        result = []
        def received():
            _, reason = server.snapshot('hil_latched')
            if reason:
                result.append(reason)
            return bool(result)
        wait_for(received)
        assert result == ['operator_disconnected']
    finally:
        server.close()


@pytest.mark.parametrize('bad', ['malformed', 'same_connection_session_change'])
def test_pending_eof_cannot_swallow_later_protocol_fault(bad):
    server = IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(), timeout=1) as client:
            client.sendall(encode_packet(packet(0)))
            assert json.loads(client.recv(4096))['accepted']
        wait_for(lambda: server.disconnected == 'operator_disconnected')
        # Deliberately do not snapshot: the main loop has not consumed the EOF.
        with socket.create_connection(server.sock.getsockname(), timeout=1) as client:
            if bad == 'same_connection_session_change':
                client.sendall(encode_packet(packet(0)))
                assert json.loads(client.recv(4096))['accepted']
            client.sendall(b'{}\n' if bad == 'malformed' else
                           encode_packet(replace(packet(1), session_id='new')))
            assert client.recv(4096) == b''
        wait_for(lambda: (server.disconnected or '').startswith('invalid_operator_packet'))
        # Another connection's EOF must not downgrade that hard error either.
        with socket.create_connection(server.sock.getsockname(), timeout=1) as client:
            client.sendall(encode_packet(packet(0)))
            assert json.loads(client.recv(4096))['accepted']
        wait_for(lambda: server.latest is None)
        _, reason = server.snapshot('hil_latched')
        assert reason.startswith('invalid_operator_packet')
    finally:
        server.close()
