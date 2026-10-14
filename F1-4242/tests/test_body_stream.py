import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import math
import pytest
from robot.motion_plan import SmoothBodyPlan,BodyCadence
from robot.adapter import ActionRejected,StateStore,RosTransport
from test_motion_plan import configured
from test_ros_adapter import Event,Clock

def stream():
    a,c,t=configured();a.profile.update(body_command_hz=200.,body_max_command_gap_s=.025)
    t.assert_exclusive_cached=t.assert_exclusive
    return a,c,t

def test_body_stream_respects_velocity_acceleration_and_zero_end_acceleration():
    p=SmoothBodyPlan({'q':0.},{'q':30.},{'q':15.},{'q':40.});dt=.0001
    samples=[p.sample(i*dt)['q'] for i in range(math.ceil(p.duration/dt)+1)]
    speeds=[(b-a)/dt for a,b in zip(samples,samples[1:])]
    accelerations=[(b-a)/dt for a,b in zip(speeds,speeds[1:])]
    assert max(speeds)<=15+1e-5 and max(map(abs,accelerations))<=40+1e-3
    assert abs(accelerations[0])<.01 and abs(accelerations[-1])<.01
    assert samples[-1]==30.

def test_body_200hz_stream_and_arrival_with_timing_evidence():
    a,c,t=stream();result=a.execute({'kind':'lift','level':4},Event(c))
    assert result['status']=='succeeded' and result['elapsed_s']<6
    timing=result['command_timing'];assert timing['actual_hz']==pytest.approx(200) and timing['max_gap_ms']==pytest.approx(5)
    positions=[v['body_j4'] for k,v in t.sent]
    assert max(abs(b-x) for x,b in zip(positions,positions[1:]))<=15/200+1e-6

def test_cached_check_does_not_query_dds_and_still_rejects_stale_graph():
    c=Clock();s=StateStore(c);s.graph_at=c();s.graph={'joint_command':{'subscribers':[{}],'publishers':[]}}
    t=RosTransport({'graph_max_age_s':.5},s)
    t.refresh_graph=lambda:pytest.fail('per-tick DDS graph call')
    t.assert_exclusive_cached('joint_command')
    c.now+=.6
    with pytest.raises(ActionRejected,match='过期'):t.assert_exclusive_cached('joint_command')

def test_deadlines_skip_late_slots_without_catchup_burst():
    p=BodyCadence(10,200,.025);p.dispatch(10);p.dispatch(10.012)
    assert p.wait_s(10.013)==pytest.approx(.002)
    with pytest.raises(ValueError,match='不会跳帧'):p.dispatch(10.038)

def test_delayed_stream_fails_and_does_not_jump_to_later_target():
    a,c,t=stream()
    class Delayed(Event):
        def wait(self,seconds):self.clock.now+=.04;return False
    with pytest.raises(ValueError,match='节拍预算') as info:a.execute({'kind':'lift','level':4},Delayed(c))
    assert info.value.commands_sent and info.value.hold_sent
    assert len(t.sent)==2  # initial output + current measured hold only
    assert info.value.command_timing['samples']==1

def test_external_command_stops_high_rate_body_stream():
    a,c,t=stream()
    class Conflict(Event):
        def wait(self,seconds):self.clock.now+=seconds;t.conflict=True;return False
    with pytest.raises(ActionRejected,match='external'):a.execute({'kind':'lift','level':4},Conflict(c))
    assert len(t.sent)==1

def test_arm_and_body_groups_must_not_share_high_rate_driver_path():
    a,c,t=stream()
    with pytest.raises(ActionRejected,match='分组'):a.plan({'kind':'joints','targets':{'arm_l_j1':1,'body_j4':-30}})
    assert not t.prepared and not t.sent

def test_body_stream_cancel_keeps_six_second_budget():
    a,c,t=stream();result=a.execute({'kind':'lift','level':4},Event(c,after=c()+.15))
    assert result['status']=='cancelled' and result['hold_sent'] and result['command_timing']['actual_hz']==pytest.approx(200)
