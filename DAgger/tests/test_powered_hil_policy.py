from dataclasses import replace
from types import SimpleNamespace
import threading
import time
import numpy as np
import pytest

from g2dagger.powered_hil_policy import PoweredHILPolicy
from g2dagger.powered_hil_io import RecordedRobot, controller_type
from g2dagger.hil_supervisor import HILState
from g2dagger.recording_sinks import NullSink
from test_real_observation_shadow import Source, packet


class Journal:
    def __init__(self):
        self.obs, self.chunks, self.events = [], [], []
    def record_timestamped_observation(self, obs, **fields):
        self.obs.append((obs, fields))
        return True
    def record_policy_chunk(self, **fields):
        self.chunks.append(fields)
        return True
    def record_event(self, event, **fields):
        self.events.append((event, fields))
        return True


def make_engine(infer=None, sink=None):
    events, journal = [], Journal()
    engine = PoweredHILPolicy(lambda e, **f: events.append((e, f)), Source(),
        infer or (lambda obs, rid: SimpleNamespace(actions=np.ones((50, 34)))),
        journal, sink or NullSink(), require_start=False, capture_hz=30, request_period=.001)
    return engine, journal, events


def pump(engine, predicate, *, toggle=0, sequence=0):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        now = time.monotonic_ns()
        engine.operator(packet(sequence, toggle), now, now)
        engine.tick(now)
        sequence += 1
        time.sleep(.005)
    assert predicate()
    return sequence


def test_sync_executes_one_chunk_then_requests_new_observation_and_start_handoff():
    engine, journal, events = make_engine()
    try:
        seq = pump(engine, lambda: engine.ready is not None)
        t = time.monotonic_ns()
        first, source = engine.sample(t, np.zeros(34))
        assert source == "policy" and np.array_equal(first, np.zeros(34))
        engine.sample(t+10_000_000, first)
        engine.tick(time.monotonic_ns())
        assert engine.request_id == 1  # No inference during active sync chunk.
        now = time.monotonic_ns()
        engine.operator(packet(seq, 1), now, now)
        assert engine.supervisor.state is HILState.HIL_LATCHED
        assert engine.chunk is None and engine.ready is None
        assert engine.sample(now, first)[1] == "hold"
        # RB alone cannot exit HIL.
        engine.operator(replace(packet(seq+1, 2), deadman=True), now+1, now+1)
        assert engine.supervisor.state is HILState.HIL_LATCHED
        # Pending progressive command blocks exit (not physical feedback).
        engine.operator(packet(seq+2, 3), now+2, now+2, hand_pending=True)
        assert engine.supervisor.state is HILState.HIL_LATCHED
        engine.operator(packet(seq+3, 4), now+3, now+3)
        assert engine.supervisor.state is HILState.HANDOFF_HOLD
        handoff = engine.supervisor.handoff_monotonic_ns
        pump(engine, lambda: engine.ready is not None, toggle=4, sequence=seq+4)
        assert engine.supervisor.state is HILState.POLICY
        assert journal.chunks[-1]["observation_monotonic_ns"] > handoff
        assert any(e == "fresh_policy_to_policy" for e, _ in events)
    finally:
        engine.close()


def test_late_real_response_cannot_reenter_after_human_intervention():
    entered, release = threading.Event(), threading.Event()
    def infer(obs, rid):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(actions=np.ones((50, 34)))
    engine, journal, events = make_engine(infer)
    try:
        seq = pump(engine, entered.is_set)
        now = time.monotonic_ns()
        engine.operator(packet(seq, 1), now, now)
        count = len(journal.obs)
        time.sleep(.06)
        assert len(journal.obs) > count
        release.set()
        pump(engine, lambda: engine.real_future is None, toggle=1, sequence=seq+1)
        assert engine.ready is None and engine.chunk is None
        assert engine.supervisor.state is HILState.HIL_LATCHED
        assert [f["accepted"] for e, f in events if e == "powered_policy_result"] == [False]
    finally:
        release.set()
        engine.close()


def test_watchdog_invalidates_active_chunk_and_never_resumes_automatically():
    engine, journal, events = make_engine()
    try:
        seq = pump(engine, lambda: engine.ready is not None)
        now = time.monotonic_ns()
        engine.sample(now, np.zeros(34))
        engine.tick(now+201_000_000)
        assert engine.supervisor.state is HILState.SAFE_HOLD
        assert engine.chunk is None
        engine.operator(packet(seq), now+202_000_000, now+202_000_000)
        assert engine.supervisor.state is HILState.SAFE_HOLD
    finally:
        engine.close()


def test_trajectory_anchor_and_30hz_contract_end_holds_without_extrapolation():
    engine, _, _ = make_engine()
    try:
        engine.ready = np.arange(50)[:, None] * np.ones((1, 34))
        engine.sample(1_000_000_000, -np.ones(34))
        np.testing.assert_allclose(engine.sample(1_050_000_000, np.zeros(34))[0], .5)
        np.testing.assert_allclose(engine.sample(3_000_000_000, np.zeros(34))[0], 49)
        assert engine.chunk is None
        np.testing.assert_allclose(engine.sample(4_000_000_000, np.full(34, 49))[0], 49)
    finally:
        engine.close()


def test_recording_overflow_stops_powered_candidate():
    class Full(NullSink):
        def submit(self, observation):
            return False
    engine, _, _ = make_engine(sink=Full())
    try:
        engine.capture_thread.join(timeout=1)
        with pytest.raises(RuntimeError, match="rejected record"):
            engine.tick(time.monotonic_ns())
    finally:
        engine.close()


def test_async_disk_error_stops_candidate_before_next_dispatch():
    sink = NullSink()
    engine, _, _ = make_engine(sink=sink)
    try:
        sink.writer_error = "disk full"
        with pytest.raises(RuntimeError, match="recording writer failed"):
            engine.tick(time.monotonic_ns())
    finally:
        engine.close()


def test_sdk_proxy_retains_actual_partial_failure_and_reuses_single_controller():
    journal = Journal()
    class Robot:
        def joint_servo_control(self, req, **kwargs):
            return 7
        def move_ee_pos(self, req):
            raise RuntimeError("hardware failure")
    proxy = RecordedRobot(Robot(), journal)
    req = SimpleNamespace(joint_names=["left"], joint_positions=[.4])
    assert proxy.joint_servo_control(req) == 7
    with pytest.raises(RuntimeError):
        proxy.move_ee_pos(req)
    calls = proxy.drain_calls()
    assert calls[0]["sdk_return"] == 7 and calls[1]["sdk_return"] is None
    assert "hardware failure" in calls[1]["error"]
    class Base:
        _servo_output_mode = "arms-only"
        _max_send_seconds = .03
        _hand_thread = None
        _robot = proxy
        def _read_dual_side_servo_layouts(self):
            return ((tuple(f'j{i}' for i in range(17)), tuple(range(17))),
                    (tuple(f'j{i}' for i in range(17,34)), tuple(range(17,34))))
    controller = controller_type(Base)()
    controller.prepare_hil_layouts()
    controller.set_hil_execution(False)
    assert controller._servo_output_mode == "unified-34"
    assert controller._max_send_seconds == .2
    controller._timing_warning_callback("servo_send_overrun", .24, .2)
    assert journal.events[-1][0] == "servo_send_overrun"
    controller.set_hil_execution(True)
    assert controller._servo_output_mode == "arms-only"
    assert controller._max_send_seconds == .03
    assert controller._timing_warning_callback is None


def test_real_producer_journal_passes_causality_and_detects_tampering(tmp_path):
    import json
    from g2dagger.hil_episode import HILEpisodeRecorder, audit_hil_episode
    from g2dagger.powered_hil_io import record_dispatch
    path = tmp_path / 'episode'
    journal = HILEpisodeRecorder(path, episode_id='offline', task_prompt='fake hardware test')
    engine = PoweredHILPolicy(journal.record_event, Source(),
        lambda obs, rid: SimpleNamespace(actions=np.ones((50, 34))),
        journal, NullSink(), require_start=False, capture_hz=30, request_period=.001)
    class Robot:
        def joint_servo_control(self, request):
            return 0
    proxy = RecordedRobot(Robot(), journal)
    proxy.joint_indices = {f'j{i}': i for i in range(34)}
    previous = np.zeros(34)
    sequence = 0
    def dispatch(source=None):
        nonlocal previous, sequence
        now = time.monotonic_ns()
        selected, actual_source = engine.sample(now, previous)
        source = source or actual_source
        proxy.joint_servo_control(SimpleNamespace(
            joint_names=list(proxy.joint_indices), joint_positions=selected.tolist()))
        record_dispatch(engine, proxy, now=now, sequence=sequence, hold=previous,
                        selected=selected, submitted=selected, source=source,
                        human_arm=source == 'human_arm', human_hand=source == 'human_arm')
        previous = selected.copy()
        sequence += 1
    try:
        dispatch()  # POLICY waiting for its first result.
        seq = pump(engine, lambda: engine.ready is not None)
        dispatch()
        time.sleep(.01)
        dispatch()
        now = time.monotonic_ns()
        engine.operator(packet(seq, 1), now, now)
        dispatch('human_arm')  # Both masks normalize to human_arm_and_hand.
        dispatch('home')
        dispatch('human_hold')
        now = time.monotonic_ns()
        engine.operator(packet(seq+1, 2), now, now)
        dispatch()
        pump(engine, lambda: engine.ready is not None, toggle=2, sequence=seq+2)
        dispatch()
    finally:
        engine.close()
        journal.close()
    report = audit_hil_episode(path)
    assert report['valid'], report['errors']
    assert not report['training_ready']
    file = path / 'control.jsonl'
    original = file.read_text()
    for field in ('sampling', 'sdk'):
        records = [json.loads(line) for line in original.splitlines()]
        if field == 'sampling':
            records[2]['policy_sampling']['alpha'] += .1
        else:
            records[2]['sdk_calls'][0]['submitted_positions'][0] += .1
        file.write_text(''.join(json.dumps(row)+'\n' for row in records))
        assert not audit_hil_episode(path)['valid']
    file.write_text(original)


def test_first_start_gates_inference_then_later_start_enters_hil():
    journal = Journal()
    engine = PoweredHILPolicy(lambda *a, **k: None, Source(),
        lambda obs, rid: SimpleNamespace(actions=np.ones((50, 34))),
        journal, NullSink(), capture_hz=30)
    try:
        now = time.monotonic_ns()
        # Connecting with START already held must not start anything.
        engine.operator(replace(packet(0, 4), hil_button_pressed=True), now, now)
        engine.tick(now)
        assert not engine.started and engine.real_future is None
        assert engine.sample(now, np.ones(34))[1] == 'hold'
        engine.operator(packet(1, 4), now+1, now+1)  # Release.
        time.sleep(.04)
        assert journal.obs  # Recording remains alive while waiting.
        now = time.monotonic_ns()
        engine.operator(replace(packet(2, 5), hil_button_pressed=True), now, now)
        assert engine.started and engine.supervisor.state is HILState.POLICY
        assert engine.supervisor.intervention_id == 0
        pump(engine, lambda: engine.ready is not None, toggle=5, sequence=3)
        assert journal.chunks[-1]['observation_monotonic_ns'] > engine.started_monotonic_ns
        now = time.monotonic_ns()
        engine.operator(replace(packet(100, 6), hil_button_pressed=True), now, now)
        assert engine.supervisor.state is HILState.HIL_LATCHED
    finally:
        engine.close()


def test_start_gate_rejects_counter_gap_and_stale_packets():
    journal = Journal()
    engine = PoweredHILPolicy(lambda *a, **k: None, Source(), lambda *a: None,
                             journal, NullSink())
    try:
        now = time.monotonic_ns()
        engine.operator(packet(0), now, now)
        engine.operator(packet(1, 2), now+1, now+1)
        assert not engine.started
        engine.operator(packet(2, 3), now+2, now+300_000_000)
        assert not engine.started
    finally:
        engine.close()


def test_started_episode_audit_rejects_pre_start_dispatch(tmp_path):
    import json
    from g2dagger.hil_episode import HILEpisodeRecorder, audit_hil_episode
    from g2dagger.powered_hil_io import record_dispatch
    path = tmp_path / 'started'
    journal = HILEpisodeRecorder(path, episode_id='start-test', task_prompt='offline startup')
    engine = PoweredHILPolicy(journal.record_event, Source(),
        lambda *a: SimpleNamespace(actions=np.ones((50, 34))), journal, NullSink())
    try:
        now = time.monotonic_ns()
        engine.operator(packet(0), now, now)
        now = time.monotonic_ns()
        engine.operator(replace(packet(1, 1), hil_button_pressed=True), now, now)
        pump(engine, lambda: engine.ready is not None, toggle=1, sequence=2)
        now = time.monotonic_ns()
        action, source = engine.sample(now, np.zeros(34))
        proxy = RecordedRobot(object(), journal)
        record_dispatch(engine, proxy, now=now, sequence=0, hold=np.zeros(34),
                        selected=action, submitted=None, source=source)
    finally:
        engine.close()
        journal.close()
    assert audit_hil_episode(path)['valid']
    events = path / 'events.jsonl'
    rows = [json.loads(l) for l in events.read_text().splitlines()]
    for row in rows:
        if row['event'] == 'operator_start_authorized':
            row['monotonic_ns'] = now + 1
    events.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert not audit_hil_episode(path)['valid']


def test_actual_controller_policy_single34_human14_and_resume_recording():
    from g2_sort.deployment.g2_client.robot_control import G2RobotController
    from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES, POLICY_JOINT_LIMITS
    journal = Journal()
    class Fake:
        def get_joint_states(self):
            return {"states": [{"name": n, "motor_position": 0.0} for n in POLICY_JOINT_NAMES[:14]]}
        def get_whole_body_status(self):
            return dict(left_end_model='o10_t2',right_end_model='o10_t2',left_end_error=0,right_end_error=0)
        def get_motion_control_status(self):
            return SimpleNamespace(mode=5,collision_pairs_1=[],collision_pairs_2=[])
        def get_end_state(self):
            result={}
            for side, offset in [('left',14),('right',24)]:
                names=list(POLICY_JOINT_NAMES[offset:offset+10])[::-1]
                names=[n+' ' if n=='idx77_hand_r_index_pip_joint' else n for n in names]
                result[side+'_end_state']=dict(names=names,end_states=[dict(enable=True,status=0,err_code=0,position=.1) for n in names])
            return result
        def joint_servo_control(self,req,enable_low_latency=False):
            assert enable_low_latency is False
            return 0
    proxy=RecordedRobot(Fake(),journal)
    gdk=SimpleNamespace(JointServoControlReq=SimpleNamespace)
    controller=controller_type(G2RobotController)(powered=True,robot=proxy,gdk_module=gdk,
        servo_output_mode='arms-only',servo_channel='normal',stabilization_seconds=0)
    controller.prepare_hil_layouts()
    target=POLICY_JOINT_LIMITS[:,0]+np.linspace(.3,.5,34)*np.ptp(POLICY_JOINT_LIMITS,axis=1)
    for human,count in [(False,34),(True,14),(False,34)]:
        controller.set_hil_execution(human)
        controller.send_action(target)
        calls=proxy.drain_calls()
        assert len(calls)==1
        assert calls[0]['joint_names']==list(POLICY_JOINT_NAMES[:count])
        assert calls[0]['joint_indices']==list(range(count))
        np.testing.assert_allclose(calls[0]['submitted_positions'],target[:count])
        assert calls[0]['sdk_return']==0
        assert controller._hand_thread is None
    controller.close()
