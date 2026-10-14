"""Real numerical clearance trajectory; never imports ROS or publishes motion."""
import copy
import json
from pathlib import Path
import numpy as np
import pytest
from test_f1_numerical import setup
from f1dagger.contract import FULL_NAMES, home_target
from f1dagger.teleop import F1ArmTarget
from f1dagger.clearance_home import ClearanceHomeMotion, ClearanceHomeStopped
from f1dagger.robot_io import CollisionGate


def motion_fixture(setup, captured=False):
    c,p,m,u,full=setup
    p=copy.deepcopy(p)
    p['f710_home']['clearance']['schema']='f1_right_clearance_v1'
    p['f710_home']['clearance'].pop('max_rotation_deg',None)
    p['f710_home']['clearance'].pop('linear_speed_m_s',None)
    preset=json.loads((Path(__file__).parent/'fixtures'/('clearance_start_20261013.json' if captured else 'demo_v0_preset.json')).read_text())
    full=np.array([preset['joints'][n] for n in FULL_NAMES])
    q=np.r_[full[:14],43.,66.]
    arm=F1ArmTarget(u,m,c['ik'])
    motion=ClearanceHomeMotion(p,q,home_target(p,q),arm,full,0)
    return motion,full,q


@pytest.mark.parametrize('captured',[False, True])
def test_cartesian_clearance_order_orientation_fixed_axes_and_final_feedback(setup,tmp_path,captured):
    c,p,m,u,_=setup
    motion,full,q=motion_fixture(setup,captured)
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    initial=q.copy();origin=motion.pose(q[7:14]);seen=[];max_drift=0.
    for tick in range(1,3001):
        now=tick*20_000_000
        target=motion.propose(.02,full,q,q,(now,)*16,now)
        assert guard.check(target,full)['safe']
        np.testing.assert_array_equal(target[:7],initial[:7])
        np.testing.assert_array_equal(target[14:],initial[14:])
        assert np.max(np.abs(target[7:14]-q[7:14])) <= p['max_joint_speed_deg_s']*.02+1e-7
        if not seen or seen[-1]!=motion.stage:
            seen.append(motion.stage)
            actual=motion.pose(q[7:14])
            if motion.stage=='right':np.testing.assert_allclose(actual.translation-origin.translation,[0,0,.06],atol=.002)
            if motion.stage=='return':np.testing.assert_allclose(actual.translation-origin.translation,[0,-.10,.06],atol=.002)
        if motion.stage!='return':
            actual=motion.pose(target[7:14]);drift=np.linalg.norm(motion.pin.log3(actual.rotation@origin.rotation.T));max_drift=max(max_drift,drift)
            assert abs(actual.translation[0]-origin.translation[0])<.002
            if motion.stage=='lift':assert abs(actual.translation[1]-origin.translation[1])<.002
        motion.commit();q=target;full[:14]=q[:14]
        if motion.done:break
    assert motion.done, (motion.stage,q.tolist())
    assert seen==['lift','right','return']
    assert max_drift < np.deg2rad(2)
    np.testing.assert_allclose(q[7:14],motion.goal[7:14],atol=.01)


def test_stale_or_absent_arrival_never_skips_clearance_and_times_out(setup):
    motion,full,q=motion_fixture(setup);fixed=q.copy()
    for tick in range(1,500):
        now=tick*20_000_000
        target=motion.propose(.02,full,q,fixed,(0,)*16,now);motion.commit();q=target
    assert motion.stage=='lift' and not motion.done
    with pytest.raises(RuntimeError,match='feedback timeout'):motion.propose(.02,full,q,fixed,(0,)*16,motion.deadline+1)
    full[:14]=q[:14]
    for tick in range(500,520):
        now=tick*20_000_000
        target=motion.propose(.02,full,q,q,(1,)*16,now);motion.commit();q=target;full[:14]=q[:14]
    assert motion.stage=='lift'


@pytest.mark.parametrize('case',[0,1],ids=['large-wrist-error-stops','small-bias-converges'])
def test_recorded_feedback_bias_with_output_and_delay(setup,tmp_path,case):
    from f1dagger.robot_io import make_output
    c,p,m,u,_=setup
    p=copy.deepcopy(p)
    p['f710_home']['clearance']['schema']='f1_right_clearance_v1'
    p['f710_home']['clearance'].pop('max_rotation_deg',None)
    p['f710_home']['clearance'].pop('linear_speed_m_s',None)
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_failures_20261013.json').read_text())[case]
    full=np.array(sample['full22']);q=np.array(sample['start']);initial=q.copy()
    motion=ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    origin=motion.pose(full[7:14]);bias=np.array(sample['steady_error_deg'])
    output=make_output(c);output.push(q[:14],0.,enabled=(False,False))
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    history=[q.copy()]*10;stages=[];stopped=None
    for tick in range(1,15001):
        now=tick*4_000_000;measured=history[-6].copy()
        # Inject recorded steady error progressively; do not fabricate an
        # initial discontinuity. This is a plant model, not hardware proof.
        measured[7:14]+=bias*min(tick*.004,1.)
        full[:14]=measured[:14]
        if tick%5==0:
            previous=motion.stage
            try:target=motion.propose(.02,full,q,measured,(now,)*16,now)
            except ClearanceHomeStopped as exc:stopped=str(exc);break
            if motion.stage!=previous:
                pose=motion.pose(measured[7:14]);stages.append(motion.stage)
                desired=motion.lift if previous=='lift' else motion.clear
                assert np.linalg.norm(pose.translation-desired)<=.002
                assert np.linalg.norm(motion.pin.log3(pose.rotation@origin.rotation.T))<=np.deg2rad(2)
            guard.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
        nxt=np.r_[output.sample(now/1e9),q[14:]];m.check_action(nxt);guard.check(nxt,full)
        assert np.max(np.abs(nxt[7:14]-q[7:14]))<=p['max_joint_speed_deg_s']*.004+1e-7
        q=nxt;history.append(q.copy())
        np.testing.assert_array_equal(q[:7],initial[:7]);np.testing.assert_array_equal(q[14:],initial[14:])
        if motion.done:break
    if case==0:
        assert stopped and 'tracking error' in stopped
        assert motion.stage=='lift' and not stages and not motion.done
        assert now<5_000_000_000  # Reject persistent error, not a 19 s stall.
    else:
        assert stopped is None and motion.done,(stopped,motion.stage)
        assert stages==['right','return']
        assert now<20_000_000_000
        np.testing.assert_allclose(q[7:14],motion.goal[7:14],atol=.01)


def test_body_motion_invalid_interval_and_start_mismatch_rejected(setup):
    motion,full,q=motion_fixture(setup)
    with pytest.raises(RuntimeError,match='interval'):motion.propose(.2,full,q,q,(1,)*16,1)
    full[18]+=1
    with pytest.raises(RuntimeError,match='reference changed'):motion.propose(.02,full,q,q,(1,)*16,1)
    c,p,m,u,_=setup;full[7]+=p['joint_tolerance_deg']+1
    with pytest.raises(RuntimeError,match='start feedback'):ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)


@pytest.mark.parametrize('value',[0,-.1,.201,float('nan'),True])
def test_invalid_clearance_distance_rejected(setup,value):
    _,p,*_=setup;p=copy.deepcopy(p);p['f710_home']['clearance']['lift_m']=value
    with pytest.raises(ValueError,match='clearance'):home_target(p,np.zeros(16))


def test_clearance_with_real_output_planner_and_delayed_feedback(setup,tmp_path):
    from f1dagger.robot_io import make_output
    c,p,m,u,_=setup
    motion,full,q=motion_fixture(setup,True)
    initial=q.copy();output=make_output(c);output.push(q[:14],0.,enabled=(False,False))
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    history=[q.copy()]*10;maximum_step=0.
    for tick in range(1,15001):
        now=tick*4_000_000;measured=history[-6].copy();full[:14]=measured[:14]
        if tick%5==0:
            target=motion.propose(.02,full,q,measured,(now,)*16,now)
            guard.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
        nxt=np.r_[output.sample(now/1e9),q[14:]];m.check_action(nxt);guard.check(nxt,full)
        maximum_step=max(maximum_step,float(np.max(np.abs(nxt[7:14]-q[7:14]))))
        q=nxt;history.append(q.copy())
        np.testing.assert_array_equal(q[:7],initial[:7]);np.testing.assert_array_equal(q[14:],initial[14:])
        if motion.done:break
    assert motion.done, motion.stage
    assert maximum_step<=p['max_joint_speed_deg_s']*.004+1e-7


@pytest.mark.parametrize('case',range(4))
def test_relaxed_route_from_failed_poses_with_limited_wrist_response(setup,tmp_path,case):
    from f1dagger.robot_io import make_output
    c,p,m,u,_=setup
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_relaxed_20261013.json').read_text())[case]
    full=np.array(sample['full22']);q=np.array(sample['start']);initial=q.copy()
    motion=ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    assert motion.relaxed
    bias=np.array(sample['steady_error_deg']);output=make_output(c)
    output.push(q[:14],0.,enabled=(False,False));guard=CollisionGate(c,m,tmp_path/'collision',full)
    history=[q.copy()]*10;stages=[];max_angle=0.;stage_times=[]
    for tick in range(1,15001):
        now=tick*4_000_000;measured=history[-6].copy()
        measured[7:14]+=bias*min(tick*.004,1.)
        # Test plant only; not a claimed hardware limit.
        measured[12]=min(measured[12],55.)
        full[:14]=measured[:14]
        if tick%5==0:
            previous=motion.stage
            target=motion.propose(.02,full,q,measured,(now,)*16,now)
            if motion.stage!=previous:
                desired=motion.lift if previous=='lift' else motion.clear
                assert np.linalg.norm(motion.pose(measured[7:14]).translation-desired)<=.002
                stages.append(motion.stage);stage_times.append(now/1e9)
            guard.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
        nxt=np.r_[output.sample(now/1e9),q[14:]];m.check_action(nxt);guard.check(nxt,full)
        assert np.max(np.abs(nxt[7:14]-q[7:14]))<=p['max_joint_speed_deg_s']*.004+1e-7
        if motion.stage!='return':
            assert abs(nxt[12])<=abs(initial[12])+1e-6
            angle=np.linalg.norm(motion.pin.log3(motion.pose(nxt[7:14]).rotation@motion.rotation.T))
            max_angle=max(max_angle,angle)
            assert angle<=np.deg2rad(10.)
        q=nxt;history.append(q.copy())
        np.testing.assert_array_equal(q[:7],initial[:7]);np.testing.assert_array_equal(q[14:],initial[14:])
        if motion.done:break
    assert motion.done and stages==['right','return']
    assert stage_times[0]<5 and stage_times[1]<10
    np.testing.assert_allclose(q[7:14],motion.goal[7:14],atol=.01)
    print('relaxed',sample['source_run'],'stage_s',stage_times,'max_rotation_deg',np.rad2deg(max_angle))


def test_relaxed_home_already_at_preset_does_not_depart(setup):
    c,p,m,u,_=setup
    preset=json.loads((Path(__file__).parent/'fixtures/demo_v0_preset.json').read_text())
    full=np.array([preset['joints'][n] for n in FULL_NAMES]);q=np.r_[full[:14],0.,0.]
    motion=ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    assert motion.stage=='return'
    for tick in range(1,12):
        now=tick*20_000_000;target=motion.propose(.02,full,q,q,(now,)*16,now)
        np.testing.assert_allclose(target,q,atol=1e-9);motion.commit()
    assert motion.done


def test_relaxed_rotation_budget_remains_a_stop_condition(setup):
    c,p,m,u,_=setup
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_relaxed_20261013.json').read_text())[2]
    full=np.array(sample['full22']);q=np.array(sample['start'])
    motion=ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    measured=q.copy();measured[13]+=15.;full[:14]=measured[:14]
    with pytest.raises(ClearanceHomeStopped,match='orientation budget'):
        motion.propose(.02,full,q,measured,(1,)*16,1)


@pytest.mark.parametrize('offset,stage',[(.999,'return'),(1.001,'lift')])
def test_home_ready_uses_existing_feedback_tolerance_without_expanding_it(setup,offset,stage):
    c,p,m,u,_=setup
    preset=json.loads((Path(__file__).parent/'fixtures/demo_v0_preset.json').read_text())
    full=np.array([preset['joints'][n] for n in FULL_NAMES]);full[7]+=offset
    q=np.r_[full[:14],43.,66.]
    motion=ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    assert motion.stage==stage
