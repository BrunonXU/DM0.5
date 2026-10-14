"""Real local TCP, callbacks, numerics and journal; synthetic sensors/output only.

No RosTransport, ROS node, robot address, or hardware publisher is constructed.
The synthetic binding prevents this fixture being mistaken for onsite evidence.
"""
from dataclasses import replace
import json
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from f1dagger.config import infra_import
from f1dagger.contract import CONTRACT, FULL_NAMES, home_target
from f1dagger.export import select_segments
from f1dagger.network import IntentServer
from f1dagger.observation import RosObservationSource
from f1dagger.policy import F1PolicyEngine
from f1dagger.recording import F1Recorder, audit, make_sink, read_rows
from f1dagger.robot_io import CollisionGate, F1Owner, make_output
from f1dagger.runtime import ControlLoop
from f1dagger.teleop import F1ArmTarget
from g2dagger.hil_supervisor import HILState
from g2dagger.protocol import decode_ack, encode_packet
from test_f1_numerical import setup
from test_f1_output import Transport
from test_f1_policy import observation
from test_real_observation_shadow import packet


class SyntheticCallbacks:
    def __init__(self, config, full):
        self.store = infra_import(config['_infra_root'], 'robot.adapter').StateStore()
        self.store.connected = True
        self.full = full
        self.jpeg = observation().frames[0].payload
        self.stop = threading.Event()
        self.tick(True)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def tick(self, camera):
        sec, nano = divmod(time.monotonic_ns(), 1_000_000_000)
        header = SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nano))
        self.store.update_joints(SimpleNamespace(name=FULL_NAMES, position=self.full, header=header))
        for side, value in [('left', 43.), ('right', 66.)]:
            self.store.update_gripper(side, SimpleNamespace(name=[side+'_gripper_position'],
                                                           position=[value], header=header))
        if camera:
            for key in ('head', 'left', 'right'):
                self.store.update_camera(key, SimpleNamespace(data=self.jpeg, format='jpeg', header=header))

    def run(self):
        next_frame = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            camera = now >= next_frame
            if camera:
                next_frame += 1/30
                if next_frame < now:
                    next_frame = now + 1/30
            self.tick(camera)
            self.stop.wait(.002)

    def close(self):
        self.stop.set()
        self.thread.join(2)


@pytest.mark.parametrize('powered', [False, True], ids=['shadow', 'synthetic-owner'])
def test_tcp_start_teleop_disconnect_recording_pipeline(setup, tmp_path, powered):
    config, profile, mapping, urdf, full = setup
    # This static-sensor pipeline exercises the original Home compatibility path.
    # Moving feedback / new clearance geometry are covered by test_f1_clearance_home.
    import copy
    profile = copy.deepcopy(profile)
    profile['f710_home'].pop('clearance', None)
    full = full.copy()
    full[:14] = home_target(profile, np.zeros(16))[:14]
    rig = SyntheticCallbacks(config, full)
    source = RosObservationSource(rig.store, profile, config)
    transport = Transport()  # Records calls in memory, never constructs ROS publishers.
    run = tmp_path/'raw'
    run.mkdir()
    engine = server = owner = sink = journal = client = None
    try:
        owner = F1Owner(transport, source, mapping, make_output(config),
                        CollisionGate(config, mapping, tmp_path/'model', full), powered=powered)
        owner.initialize(time.monotonic())
        arm = F1ArmTarget(urdf, mapping, config['ik'])
        arm.warmup(full, owner.last_submitted)
        journal = F1Recorder(run/'journal', episode_id='synthetic-pipeline', task_prompt='synthetic-only',
            binding=dict(execution_mode='powered' if powered else 'shadow',
                         synthetic=True, capture_hz=30., recording_mode='hil-record'))
        sink = make_sink(run/'payload', 'hil-record')
        def emit(event, **fields):
            assert journal.record_event(event, **fields)
        engine = F1PolicyEngine(emit, source, None, journal, sink, config, teleop_only=True)
        loop = ControlLoop(engine, owner, arm, journal, profile)
        server = IntentServer(0)
        client = socket.create_connection(server.sock.getsockname(), timeout=1)
        sequence = 0
        acks = []
        def drive(seconds, toggle=1, session='first', **fields):
            nonlocal sequence
            deadline = time.monotonic()+seconds
            next_packet = 0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if client is not None and now >= next_packet:
                    p = replace(packet(sequence, toggle), session_id=session,
                                **dict({'grip_target': None}, **fields))
                    client.sendall(encode_packet(p))
                    wire = b''
                    while not wire.endswith(b'\n'):
                        chunk = client.recv(4096)
                        assert chunk
                        wire += chunk
                    ack = decode_ack(wire)
                    assert ack.accepted
                    acks.append(ack)
                    sequence += 1
                    next_packet = now+.02
                incoming, reason = server.snapshot(engine.supervisor.state.value)
                loop.step(time.monotonic_ns(), incoming, reason)
                time.sleep(.004)

        drive(.08, toggle=0)
        assert not engine.started and not transport.prepared and not transport.sent
        drive(.10, hil_button_pressed=True)
        assert engine.started and engine.supervisor.state is HILState.HIL_LATCHED
        start = owner.last_submitted.copy()
        drive(.75, deadman=True, axes=(.15, 0., 0.))
        assert engine.supervisor.state is HILState.HIL_LATCHED
        assert np.max(np.abs(owner.last_submitted[:7]-start[:7])) > .001
        assert not np.any(owner.last_submitted[7:] != start[7:])
        drive(.2)
        held = owner.last_submitted.copy()
        drive(.2)
        np.testing.assert_allclose(owner.last_submitted, held, atol=1e-10)
        drive(.22, home_reset=True)
        drive(.25)
        assert loop.home is None  # Fresh synthetic home feedback, never a physical-arrival claim.
        np.testing.assert_array_equal(owner.last_submitted[14:], [43., 66.])
        drive(.16, toggle=2, hil_button_pressed=True)
        assert engine.supervisor.state is HILState.HANDOFF_HOLD
        drive(.06, toggle=2)
        drive(.16, toggle=3, hil_button_pressed=True)
        assert engine.supervisor.state is HILState.HIL_LATCHED
        client.close()
        client = None
        drive(.25)
        assert engine.supervisor.state is HILState.SAFE_HOLD
        held = owner.last_submitted.copy()
        client = socket.create_connection(server.sock.getsockname(), timeout=1)
        drive(.12, toggle=0, session='reconnected')
        assert engine.supervisor.state is HILState.SAFE_HOLD
        np.testing.assert_allclose(owner.last_submitted, held, atol=1e-10)
        drive(.15, session='reconnected', hil_button_pressed=True)
        drive(.35, session='reconnected')
        assert engine.supervisor.state is HILState.HIL_LATCHED
        assert engine.request_id == 0 and engine.acquisition_error is None
        assert any(a.reason == 'powered_hil:hil_latched' for a in acks)
        if powered:
            assert transport.prepared and transport.sent
        else:
            assert not transport.prepared and not transport.sent
    finally:
        if client: client.close()
        if server: server.close()
        if engine: engine.close()
        if owner: owner.close()
        rig.close()
        if sink: assert sink.close()
        if journal: assert journal.close()

    report = audit(run)
    assert report['valid'], report
    obs = read_rows(run/'journal/observations.jsonl')
    control = read_rows(run/'journal/control.jsonl')
    events = read_rows(run/'journal/events.jsonl')
    assert len(obs) > 60 and len(control) > len(obs)
    rate = (len(obs)-1)*1e9/(obs[-1]['available_monotonic_ns']-obs[0]['available_monotonic_ns'])
    assert 27 < rate <= 31, rate
    assert any(e['event'] == 'home_completed_feedback' for e in events)
    assert not any(e['event'] in ('target_rejected', 'publication_failed') for e in events)
    review = dict(contract=CONTRACT, manifest_sha256=report['manifest_sha256'],
                  reviewer='synthetic-test-only', accepted_interventions=[1, 2, 3])
    if not powered:
        assert all(row['submitted'] is None for row in control)
        with pytest.raises(ValueError, match='shadow'):
            select_segments(run, review)
        return
    segments, selection, _ = select_segments(run, review)
    assert selection['fps'] == 30 and len(segments) >= 3
    assert any(row['state'] != row['action'] for s in segments for row in s)
    for segment in segments:
        assert len({(row['intervention_id'], row['policy_epoch']) for row in segment}) == 1
        first, last = segment[0]['control_stream_sequence'], segment[-1]['control_stream_sequence']
        assert all(row['hil_state'] == 'hil_latched' and row['source'].startswith('human_')
                   for row in control[first:last+1])
    (tmp_path/'pipeline_report.json').write_text(json.dumps(dict(report, synthetic=True,
        measured_capture_hz=rate, selection=selection, acks=len(acks)), indent=2))
