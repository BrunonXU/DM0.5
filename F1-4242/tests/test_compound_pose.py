import json,math,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from robot.geometry import frames,ground,check_ground_path,model
from robot.waist_kinematics import CartesianBodyPlan,forward
from robot.adapter import BODY_NAMES,HEAD_NAMES,ActionRejected
from test_body_stream import stream
from test_ros_adapter import joints,Event
LIMITS=dict(zip(('body_j3','body_j4','body_j5'),[[-70,0],[-95,0],[-40,0]]))

def test_screenshot_target_crosses_ground_but_native_home_path_does_not():
    pose={n:0. for n in model()['joint_map']};pose.update(body_j3=-66.316,body_j4=-79.393,body_j5=-40.365)
    with pytest.raises(ValueError,match='穿过地面'):check_ground_path(pose,{'body_j4':0})
    assert check_ground_path(pose,{'body_j3':-5,'body_j4':-10,'body_j5':-5})['ground_clearance_m']>0
    assert ground()==pytest.approx(-.2172060094)

def test_planar_waist_fk_matches_full_urdf_frame():
    for q3,q4,q5 in [(-5,-10,-5),(-66.316,-79.393,-40),(-20,-35,-12),(-40,-70,-30)]:
        pose=dict(zip(('body_j3','body_j4','body_j5'),(q3,q4,q5)));x,z,pitch=forward(pose);f=frames(pose)['body_Link3']
        assert x==pytest.approx(f[0][3],abs=1e-9) and z==pytest.approx(f[2][3],abs=1e-9)

def test_cartesian_stream_preserves_pitch_x_and_dynamic_limits():
    q={'body_j1':12,'body_j2':0,'body_j3':-30,'body_j4':-50,'body_j5':-20}
    p=CartesianBodyPlan(q,.02,LIMITS,15,40);x,z,pitch=forward(q);dt=.001
    rows=[p.sample(i*dt) for i in range(math.ceil(p.duration/dt)+1)]
    for row in rows:
        xx,zz,pp=forward(row);assert xx==pytest.approx(x,abs=1e-9) and pp==pytest.approx(pitch,abs=1e-9)
        assert row['body_j1']==12
        for n in LIMITS:assert LIMITS[n][0]<=row[n]<=LIMITS[n][1]
    assert forward(rows[-1])[1]==pytest.approx(z+.02)
    for n in LIMITS:
        velocities=[(b[n]-a[n])/dt for a,b in zip(rows,rows[1:])]
        assert max(map(abs,velocities))<=15.01
        assert max(abs(b-a)/dt for a,b in zip(velocities,velocities[1:]))<=40.1

def test_bent_at_lower_joint_limit_cannot_keep_pitch_and_rise():
    q={'body_j3':-66.316,'body_j4':-79.393,'body_j5':-40}
    with pytest.raises(ValueError,match='关节行程'):CartesianBodyPlan(q,.02,LIMITS,15,40)
    assert CartesianBodyPlan(q,-.02,LIMITS,15,40).duration<6

def test_head_body_share_stream_arms_still_separate():
    a,c,t=stream();a.profile['joint_limits_deg'].update(dict(zip(HEAD_NAMES,[[-40,40],[-25,25],[-15,15]])));a.store.update_joints(joints(list(HEAD_NAMES),[0,0,0]))
    r=a.execute({'kind':'joints','targets':{'head_j2':5,'body_j1':5}},Event(c));assert r['status']=='succeeded'
    assert r['command_timing']['actual_hz']==pytest.approx(200)
    with pytest.raises(ActionRejected,match='分组'):a.plan({'kind':'joints','targets':{'head_j2':2,'arm_l_j1':3}})

def test_small_feedback_overshoot_only_returns_within_hardware_range():
    a,c,t=stream();a.profile['feedback_limit_recovery_deg']=1.;a.store.update_joints(joints(['body_j5'],[-40.36]))
    r=a.execute({'kind':'joints','targets':{'body_j5':-39}},Event(c));assert r['status']=='succeeded'
    assert all(v['body_j5']>=-40 for k,v in t.sent)
    a.store.update_joints(joints(['body_j5'],[-42]))
    with pytest.raises(ActionRejected,match='当前反馈'):a.plan({'kind':'joints','targets':{'body_j5':-39}})

def test_full_home_exact_native_pose_and_xy_arms():
    cfg=json.loads((Path(__file__).resolve().parents[1]/'configs/f1-09.json').read_text());p=next(x for x in cfg['default_presets'] if x['source']=='native_pose_2')['joints']
    assert len(p)==22 and [p[n] for n in BODY_NAMES]==[0,0,-5,-10,-5] and [p[n] for n in HEAD_NAMES]==[0,0,0]
    xy=next(x for x in cfg['default_presets'] if x['source']=='lxl_xy_reset')['joints'];assert all(p[n]==v for n,v in xy.items())

def test_relative_waist_real_executor_uses_cancellable_200hz_stream():
    a,c,t=stream();a.profile['waist_cartesian_enabled']=True
    a.store.update_joints(joints(list(BODY_NAMES),[0,0,-30,-50,-20]))
    r=a.execute({'kind':'waist_height','delta_m':.02},Event(c));assert r['status']=='succeeded'
    assert r['command_timing']['actual_hz']==pytest.approx(200)
    assert forward(t.sent[-1][1])[1]==pytest.approx(forward({'body_j3':-30,'body_j4':-50,'body_j5':-20})[1]+.02)
    r=a.execute({'kind':'waist_height','delta_m':-.02},Event(c,after=c()+.15));assert r['status']=='cancelled' and r['hold_sent']

def test_ground_rejection_creates_no_publishers_and_sends_no_commands():
    a,c,t=stream();a.profile['ground_guard']=True
    a.store.update_joints(joints(list(HEAD_NAMES),[0,0,0]))
    a.store.update_joints(joints(list(BODY_NAMES),[0,0,-66.3,-30,-40]))
    with pytest.raises(ValueError,match='穿过地面'):a.execute({'kind':'joints','targets':{'body_j4':0}},Event(c))
    assert not t.prepared and not t.sent
