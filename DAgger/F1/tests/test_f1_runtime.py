from dataclasses import replace
from types import SimpleNamespace
import json
import socket
import time
import numpy as np
import pytest
from f1dagger.runtime import ControlLoop
from f1dagger.network import IntentServer
from g2dagger.protocol import encode_packet
from g2dagger.hil_supervisor import HILState
from test_f1_policy import engine
from test_f1_output import make_owner
from test_real_observation_shadow import packet


def setup_loop():
    e,j=engine(teleop=True)
    o,t=make_owner()
    now=[time.monotonic_ns()]
    arm=SimpleNamespace(release=lambda:None,target=lambda p,f,last,dt:np.r_[last[0]+1,last[1:]])
    records=[]
    j.control=lambda **kw:records.append(kw) or True
    loop=ControlLoop(e,o,arm,j,{},clock=lambda:now[0])
    def step(seq,toggle=0,**kw):
        now[0]+=20_000_000
        p=replace(packet(seq,toggle),**kw)
        loop.step(now[0],(p,now[0]))
    return loop,e,o,t,now,records,step


def test_manual_rb_release_flushes_pending_arm_and_disconnect_does_not_resume():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);assert not t.sent
        step(1,1);assert e.started and not t.sent
        step(2,1,deadman=True);assert records[-1]['source']=='human_arm'
        previous=o.last_submitted.copy()
        step(3,1,deadman=False)
        assert e.supervisor.state is HILState.HIL_LATCHED
        np.testing.assert_equal(o.last_submitted,previous)
        now[0]+=210_000_000
        loop.step(now[0])
        assert e.supervisor.state is HILState.SAFE_HOLD
        step(4,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        np.testing.assert_equal(o.last_submitted,previous)
    finally:e.close()


def test_dispatch_watchdog_catches_slow_target_before_publication():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);step(1,1)
        def slow(*args):
            now[0]+=210_000_000
            return np.r_[20.,np.zeros(13),32.,66.]
        loop.arm.target=slow
        step(2,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert o.last_submitted[0]==0 and records[-1]['source']=='hold'
    finally:e.close()


def test_loopback_ack_replay_and_disconnect():
    server=IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(),timeout=1) as client:
            wire=encode_packet(packet(0))
            client.sendall(wire)
            ack=json.loads(client.recv(4096))
            assert ack['accepted'] and ack['reason'].startswith('powered_hil:')
            incoming,_=server.snapshot('hil_latched')
            assert incoming[0].sequence==0
            client.sendall(wire)
            ack=json.loads(client.recv(4096))
            assert not ack['accepted']
            _,reason=server.snapshot('hil_latched')
            assert reason=='invalid_operator_packet'
    finally:server.close()


@pytest.mark.parametrize('configured', [False, True], ids=['legacy', 'demo-v0'])
def test_y_home_holds_grippers_and_requires_arrival(monkeypatch, configured):
    import f1dagger.runtime as runtime
    from f1dagger.contract import ARM_NAMES
    class Home:
        def __init__(self,profile,start,goal):self.goal=goal;self.pending=None
        def propose(self,dt):self.pending=True;return self.goal.copy()
        def commit(self):self.pending=None
    monkeypatch.setattr(runtime,'HomeMotion',Home)
    loop,e,o,t,now,records,step=setup_loop()
    loop.profile=dict(max_joint_speed_deg_s=30,joint_tolerance_deg=1,
        default_presets=[dict(key='lxl-xy-reset-v1',units='degree',joints=dict.fromkeys(ARM_NAMES,0))])
    goal=np.zeros(14)
    if configured:
        from f1dagger.config import DAGGER_ROOT
        preset=json.loads((DAGGER_ROOT/'configs/f1_09_hil.json').read_text())['f710_home']
        preset.pop('clearance', None)  # Existing single-stage compatibility contract.
        loop.profile.update(robot_id='F1-09',f710_home=preset)
        goal=np.array([preset['joints'][name] for name in ARM_NAMES])
    measured=[np.zeros(14)]
    o.source.feedback=lambda:(np.zeros(22),np.r_[measured[0],32.,66.],(now[0],)*16)
    try:
        step(0);step(1,1)
        step(2,1,home_reset=True)
        assert records[-1]['source']=='home' and loop.home is not None
        np.testing.assert_equal(loop.home[:14],goal)
        np.testing.assert_equal(o.last_submitted[14:],[32,66])
        step(3,1)
        assert e.supervisor.state is HILState.HIL_LATCHED
        measured[0]=goal.copy()
        for seq in range(4,13):step(seq,1)
        assert loop.home is None
        assert any(event[0][0]=='home_completed_feedback' for event in e.recorder.events)
        step(13,2)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
    finally:e.close()


def test_start_cancels_pending_home_flushes_output_and_does_not_resume():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);step(1,1)
        previous=o.last_submitted.copy()
        loop.home=previous.copy();loop.home_motion=SimpleNamespace(stage='lift')
        # The new START must clear Home before trying to call propose().
        step(2,2)
        assert e.supervisor.state is HILState.HANDOFF_HOLD and loop.home is None
        np.testing.assert_array_equal(o.last_submitted,previous)
        step(3,2);step(4,3)
        assert e.supervisor.state is HILState.HIL_LATCHED and loop.home is None
        np.testing.assert_array_equal(o.last_submitted,previous)
    finally:e.close()


@pytest.mark.parametrize('reason',['tracking error','feedback timeout'])
def test_clearance_stop_retains_hil_and_requires_new_y(monkeypatch,reason):
    import f1dagger.runtime as runtime
    from f1dagger.clearance_home import ClearanceHomeStopped
    class Failed:
        stage='lift'
        def retry_context(self):return None
        def propose(self,*args):raise ClearanceHomeStopped(reason)
    monkeypatch.setattr(runtime,'ClearanceHomeMotion',Failed)
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);step(1,1)
        previous=o.last_submitted.copy()
        loop.home=previous.copy();loop.home_motion=Failed()
        # A held Y is consumed on the exception path too.
        loop.y_down=True
        step(2,1,home_reset=True)
        assert e.supervisor.state is HILState.HIL_LATCHED and loop.home is None
        assert loop.y_down and records[-1]['source']=='hold'
        assert any(v[0][0]=='home_stopped' for v in e.recorder.events)
        np.testing.assert_array_equal(o.last_submitted,previous)
        step(3,1,home_reset=True)
        assert loop.home is None
        step(4,2)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
    finally:e.close()


def test_runtime_reports_waiting_input_edges_and_mode_without_per_packet_spam():
    from f1dagger.runtime import RuntimeReporter
    loop, e, o, t, now, records, step = setup_loop()
    messages = []
    sink = SimpleNamespace(diagnostics=lambda: dict(
        pending_items=8, count=123, writer_error=None, last_rejection_reason=None))
    reporter = RuntimeReporter('shadow', sink, emit=lambda message, **kw: messages.append(message))
    def report(seq, toggle=0, **fields):
        p = replace(packet(seq, toggle), **fields)
        reporter.report(now[0], e, (p, now[0]))
    try:
        report(0)
        assert 'waiting_for_teleop_start' in messages[-1] and '"execution": "SHADOW"' in messages[-1]
        assert '"pending_items": 8' in messages[-1]
        report(1)
        assert len(messages) == 1
        report(2, 1, hil_button_pressed=True)
        assert len(messages) == 2 and '"start_pressed": true' in messages[-1]
        step(0); step(1, 1)
        report(3, 1)
        assert '"state": "hil_latched"' in messages[-1]
        count = len(messages)
        now[0] += 5_000_000_000
        report(4, 1)
        assert len(messages) == count+1
        e.disconnect(now[0], 'operator_disconnected')
        report(5, 1)
        assert '"state": "safe_hold"' in messages[-1]
        assert '"hold_reason": "operator_disconnected"' in messages[-1]
    finally:
        e.close()


def test_first_rt_publishes_but_pause_and_retakeover_do_not_replay_latches():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0, active_side=None, grip_target=None)
        step(1, 1, active_side=None, grip_target=None)
        step(2, 1, active_side='right', grip_target=None)
        step(3, 1, active_side='right', grip_target=1.)
        assert records[-1]['source'] == 'human_hand'
        assert list(o.last_submitted[14:]) == [32., 100.]
        step(4, 2, active_side='right', grip_target=1.)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        step(5, 2, active_side='right', grip_target=0.)
        assert records[-1]['source'] == 'hold'
        assert list(o.last_submitted[14:]) == [32., 100.]
        step(6, 3, active_side='right', grip_target=0.)
        assert e.supervisor.state is HILState.HIL_LATCHED
        step(7, 3, active_side='right', grip_target=0.)
        assert list(o.last_submitted[14:]) == [32., 100.]
        step(8, 3, active_side='right', grip_target=1.)
        step(9, 3, active_side='right', grip_target=0.)
        assert records[-1]['source'] == 'human_hand'
        assert list(o.last_submitted[14:]) == [32., 0.]
    finally:
        e.close()


def test_clearance_y_only_enables_right_arm_and_abort_requires_new_y(monkeypatch):
    import f1dagger.runtime as runtime
    from f1dagger.config import DAGGER_ROOT
    class Clearance:
        enabled=(False,True)
        stage='lift'
        done=False
        pending=None
        def __init__(self,profile,start,goal,arm,full,now):self.goal=goal
        def propose(self,dt,full,submitted,measured,stamps,now):
            self.pending=True
            result=submitted.copy();result[7]+=.01;return result
        def commit(self):self.pending=None
    monkeypatch.setattr(runtime,'ClearanceHomeMotion',Clearance)
    loop,e,o,t,now,records,step=setup_loop()
    preset=json.loads((DAGGER_ROOT/'configs/f1_09_hil.json').read_text())['f710_home']
    loop.profile=dict(robot_id='F1-09',f710_home=preset,max_joint_speed_deg_s=30,joint_tolerance_deg=1)
    enabled=[];approve=o.approve
    def capture(target,now,**kw):enabled.append(kw['enabled']);return approve(target,now,**kw)
    o.approve=capture
    try:
        step(0);step(1,1)
        from dagger_core.protocol import ProtocolError
        with pytest.raises(ProtocolError):step(2,1,home_reset=True,deadman=True)
        assert loop.home is None
        step(3,1,home_reset=False,deadman=False)
        step(4,1,home_reset=True,deadman=False)
        assert isinstance(loop.home_motion,Clearance) and enabled[-1]==(False,True)
        np.testing.assert_array_equal(loop.home[:7],o.last_submitted[:7])
        previous=o.last_submitted.copy()
        now[0]+=210_000_000;loop.step(now[0])
        assert e.supervisor.state is HILState.SAFE_HOLD and loop.home is None
        step(5,1,home_reset=True)
        np.testing.assert_array_equal(o.last_submitted,previous)
        assert loop.home is None
    finally:e.close()


def test_valid_rb_takeover_cancels_home_before_next_publication():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);step(1,1)
        loop.home=o.last_submitted.copy()
        loop.home_motion=SimpleNamespace(stage='lift')  # propose must not run
        step(2,1,deadman=True,active_side='right')
        assert loop.home is None and loop.home_motion is None
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert records[-1]['source']=='human_arm'
        assert any(x[0][0]=='home_cancelled' for x in e.recorder.events)
    finally:e.close()


def test_new_y_reuses_failed_geometry_but_rb_and_start_discard_it(monkeypatch):
    import f1dagger.runtime as runtime
    from f1dagger.config import DAGGER_ROOT
    calls=[]
    class FailedHome:
        stage='right'
        enabled=(False,True)
        pending=None
        done=False
        def __init__(self,*args,retry=None):calls.append(retry)
        def propose(self,*args):raise runtime.ClearanceHomeStopped('recorded wrist tracking error')
        def retry_context(self):return {'checkpoint':'same-route'}
    monkeypatch.setattr(runtime,'ClearanceHomeMotion',FailedHome)
    loop,e,o,t,now,records,step=setup_loop()
    preset=json.loads((DAGGER_ROOT/'configs/f1_09_hil.json').read_text())['f710_home']
    loop.profile=dict(robot_id='F1-09',f710_home=preset,max_joint_speed_deg_s=30,joint_tolerance_deg=1)
    try:
        step(0);step(1,1);step(2,1,home_reset=True)
        assert loop.home is None and loop.home_retry_context=={'checkpoint':'same-route'}
        step(3,1,home_reset=False);step(4,1,home_reset=True)
        assert calls==[None,{'checkpoint':'same-route'}]
        step(5,1,deadman=True,active_side='right')
        assert loop.home_retry_context is None
        step(6,1,home_reset=False);step(7,1,home_reset=True)
        assert calls[-1] is None
        step(8,2,home_reset=False)
        assert loop.home_retry_context is None
    finally:e.close()
