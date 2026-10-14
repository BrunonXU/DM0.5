import sys,math
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from robot.motion_plan import FixedPlan,lift_targets
from robot.adapter import BODY_NAMES,ActionRejected
from test_ros_adapter import adapter,Event,joints
from test_data_auth import http_app

def configured():
    a,c,t=adapter();a.profile.update(fixed_speed=True,max_joint_delta_deg=None,max_joint_speed_deg_s=30.,max_joint_acceleration_deg_s2=75.,body_speed_deg_s=15.,body_acceleration_deg_s2=40.,max_action_s=6.)
    a.profile['joint_limits_deg'].update(dict(zip(BODY_NAMES,[[-130,130],[-10,10],[-70,0],[-95,0],[-40,0]])))
    a.store.update_joints(joints(list(BODY_NAMES),[0,0,-10,-20,-10]))
    return a,c,t

def test_large_change_succeeds_without_five_degree_limit():
    a,c,t=configured();r=a.execute({'kind':'joints','targets':{'arm_l_j1':90},'duration_s':.001},Event(c))
    assert r['status']=='succeeded' and r['elapsed_s']<6
    samples=[v['arm_l_j1'] for key,v in t.sent if key=='joint_command']
    assert samples[-1]==90 and max(y-x for x,y in zip(samples,samples[1:]))<=30/30+1e-6

def test_duration_input_cannot_override_fixed_profile():
    a,c,t=configured()
    short=a.plan({'kind':'joints','targets':{'arm_l_j1':60},'duration_s':.1})
    long=a.plan({'kind':'joints','targets':{'arm_l_j1':60},'duration_s':100})
    assert short==long and not t.sent

def test_six_second_limit_rejects_before_publisher():
    a,c,t=configured();a.store.update_joints(joints(['arm_l_j1'],[-160]))
    with pytest.raises(ActionRejected,match='拆分'):a.execute({'kind':'joints','targets':{'arm_l_j1':160}},Event(c))
    assert not t.prepared and not t.sent

def test_body_lift_uses_hall_targets_and_holds_other_axes():
    a,c,t=configured();a.store.update_joints(joints(['body_j1','body_j2'],[2,1]))
    r=a.execute({'kind':'lift','level':4},Event(c));assert r['status']=='succeeded'
    target=t.sent[-1][1];assert target=={'body_j3':-20.,'body_j4':-40.,'body_j5':-20.,'body_j1':2.,'body_j2':1.}

def test_bad_lift_does_not_leak_execution_lock():
    a,c,t=configured()
    with pytest.raises(ValueError):a.execute({'kind':'lift','level':0},Event(c))
    assert a.execute({'kind':'lift','level':2},Event(c))['status']=='succeeded'

def test_hardware_joint_bounds_remain():
    a,c,t=configured()
    with pytest.raises(ActionRejected,match='限位'):a.execute({'kind':'joints','targets':{'body_j3':20}},Event(c))
    assert not t.sent

def test_fixed_gripper_speed_and_timeout_budget():
    a,c,t=configured();r=a.execute({'kind':'gripper','side':'left','position':100},Event(c))
    assert r['status']=='succeeded' and r['elapsed_s']<6
    samples=[v for k,v in t.sent if k=='left_gripper_command']
    assert max(y-x for x,y in zip(samples,samples[1:]))<=25/30+1e-6

def test_acceleration_and_velocity_are_bounded_for_short_and_long_paths():
    for distance in (.01,1,10,120):
        p=FixedPlan({'j':0},{'j':distance},{'j':30},{'j':75});dt=.0005
        samples=[p.sample(i*dt)['j'] for i in range(math.ceil(p.duration/dt)+1)]
        velocities=[(b-a)/dt for a,b in zip(samples,samples[1:])]
        assert max(velocities)<=30+1e-5
        assert max(abs(b-a)/dt for a,b in zip(velocities,velocities[1:]))<=75+.001
        assert abs(samples[-1]-distance)<1e-6

def test_long_base_hold_stops_at_six_seconds_and_sends_zero():
    a,c,t=configured()
    class RenewEvent(Event):
        def wait(self,seconds):
            self.clock.now+=seconds
            a.renew_base({'vx':.05,'vy':0.,'wz':0.,'ttl_s':.25})
            return False
    start=c();r=a.execute({'kind':'base','vx':.05,'vy':0.,'wz':0.,'ttl_s':.25},RenewEvent(c))
    assert r['status']=='completed' and 6<=c()-start<6.1
    assert [v for key,v in t.sent][-3:]==[(0.,0.,0.)]*3

def test_body_planner_abort_stops_new_targets_when_cancelled():
    a,c,t=configured();r=a.execute({'kind':'lift','level':4},Event(c,after=c()+.3))
    assert r['status']=='cancelled' and r['hold_sent']
    assert t.sent[-1][1]['body_j4']>-40

def test_joint_failure_after_dispatch_sends_fresh_hold_when_still_owned():
    a,c,t=configured();t.mirror=False
    with pytest.raises(ActionRejected,match='时限'):a.execute({'kind':'joints','targets':{'arm_l_j1':30}},Event(c))
    assert t.sent[-1][1]['arm_l_j1']==0

def test_http_plan_requires_lease_and_has_no_execution(http_app,monkeypatch):
    from robot.adapter import ActionRejected
    app,request=http_app;_,_,cookie=request('/api/session')
    assert request('/api/actions/plan',{'kind':'lift','level':1},cookie)[0]==401
    _,auth,_=request('/api/control/acquire',{'control_code':'fixture-control-code'},cookie)
    monkeypatch.setattr(app.adapter,'plan',lambda spec:{'motion_s':2,'targets':{}},raising=False)
    body={'kind':'lift','level':1,'lease_id':auth['lease_id']}
    assert request('/api/actions/plan',body,cookie)[0]==200
    assert app.actions.latest() is None
    def reject(spec):raise ActionRejected('超过6秒，请拆分目标')
    monkeypatch.setattr(app.adapter,'plan',reject)
    assert request('/api/actions/plan',body,cookie)[0]==409
    assert app.actions.latest() is None
