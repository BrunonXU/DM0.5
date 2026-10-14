"""Recorded failed-Y retry through bounded publication, with no robot I/O."""
import copy
import json
from pathlib import Path
import numpy as np
import pytest
from test_f1_numerical import setup
from f1dagger.clearance_home import ClearanceHomeMotion, FeedbackAlignedClearanceHome, ClearanceHomeStopped
from f1dagger.contract import home_target
from f1dagger.teleop import F1ArmTarget
from f1dagger.robot_io import make_output, CollisionGate


def fixture(setup):
    c,p,m,u,_=setup
    from f1dagger.config import pico_paths
    pico_paths(c['_infra_root'])
    r=json.loads((Path(__file__).parent/'fixtures/clearance_retry_20261013.json').read_text())[3]
    full=np.array(r['full22']);q=np.array(r['start'])
    return c,copy.deepcopy(p),m,u,full,q


def test_recorded_retry_aligns_command_without_faking_feedback_or_jumping(setup,tmp_path):
    c,p,m,u,full,q=fixture(setup);fixed=q.copy();actual=np.r_[full[:14],q[14:]]
    with pytest.raises(ClearanceHomeStopped,match='start feedback'):
        ClearanceHomeMotion(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    motion=FeedbackAlignedClearanceHome(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    assert motion.stage=='align'
    output=make_output(c);output.planner.set_limits(m.limits);output.push(q[:14],0.,enabled=(False,False))
    gate=CollisionGate(c,m,tmp_path/'collision',full)
    for tick in range(1,1501):
        now=tick*4_000_000
        if tick%5==0:
            target=motion.propose(.02,full,q,actual,(now,)*16,now)
            gate.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
            if motion.stage=='lift':break
        nxt=np.r_[output.sample(now/1e9),q[14:]]
        assert np.max(np.abs(nxt-q))<=p['max_joint_speed_deg_s']*.004+1e-7
        m.check_action(nxt);gate.check(nxt,full)
        np.testing.assert_array_equal(nxt[:7],fixed[:7]);np.testing.assert_array_equal(nxt[14:],fixed[14:])
        q=nxt
    assert motion.stage=='lift' and not motion.done
    assert np.max(np.abs(q[7:14]-actual[7:14]))<=.01
    np.testing.assert_array_equal(actual[:14],full[:14])
    # New route starts 6cm above the actual pose, not the failed waypoint.
    np.testing.assert_allclose(motion.lift-motion.pose(actual[7:14]).translation,[0,0,.06],atol=1e-9)


def test_retry_never_advances_with_stale_feedback_or_persistent_error(setup):
    c,p,m,u,full,q=fixture(setup);actual=np.r_[full[:14],q[14:]]
    motion=FeedbackAlignedClearanceHome(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    for tick in range(1,151):
        now=tick*20_000_000
        q=motion.propose(.02,full,q,actual,(0,)*16,now);motion.commit()
    assert motion.stage=='align'
    bad=actual.copy();bad[13]+=2.;badfull=full.copy();badfull[:14]=bad[:14]
    for tick in range(151,170):
        now=tick*20_000_000
        q=motion.propose(.02,badfull,q,bad,(now,)*16,now);motion.commit()
    assert motion.stage=='align'
    with pytest.raises(ClearanceHomeStopped,match='align feedback timeout'):
        motion.propose(.02,full,q,actual,(motion.deadline+1,)*16,motion.deadline+1)


def test_recorded_retry_completes_route_with_delayed_tracking(setup,tmp_path):
    c,p,m,u,full,q=fixture(setup)
    # User deferred speed work: exercise the original 4cm/s route.
    p['f710_home']['clearance'].pop('linear_speed_m_s',None)
    fixed=q.copy();actual=np.r_[full[:14],q[14:]]
    r=json.loads((Path(__file__).parent/'fixtures/clearance_retry_20261013.json').read_text())[2]
    first=ClearanceHomeMotion(p,np.array(r['start']),home_target(p,q),F1ArmTarget(u,m,c['ik']),np.array(r['full22']),0)
    checkpoint=first.retry_context();checkpoint['stage']='right'  # Recorded lift-completed event.
    motion=FeedbackAlignedClearanceHome(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0,retry=checkpoint)
    output=make_output(c);output.planner.set_limits(m.limits);output.push(q[:14],0.,enabled=(False,False))
    gate=CollisionGate(c,m,tmp_path/'collision',full)
    history=[actual.copy()]*6;stages=[]
    for tick in range(1,15001):
        now=tick*4_000_000
        measured=actual.copy() if motion.aligning else history[-6].copy()
        full[:14]=measured[:14]
        if tick%5==0:
            target=motion.propose(.02,full,q,measured,(now,)*16,now)
            if not stages or stages[-1]!=motion.stage:stages.append(motion.stage)
            gate.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
        nxt=np.r_[output.sample(now/1e9),q[14:]]
        assert np.max(np.abs(nxt-q))<=p['max_joint_speed_deg_s']*.004+1e-7
        m.check_action(nxt);gate.check(nxt,full);q=nxt;history.append(q.copy())
        np.testing.assert_array_equal(q[:7],fixed[:7]);np.testing.assert_array_equal(q[14:],fixed[14:])
        if motion.done:break
    assert stages==['align','right','return'] and motion.done, (stages,now/1e9)
    np.testing.assert_array_equal(motion.clear,checkpoint['clear'])
    np.testing.assert_allclose(measured[7:14],motion.goal[7:14],atol=p['joint_tolerance_deg'])


@pytest.mark.parametrize('speed',[0,-1,float('nan'),.081,True])
def test_home_speed_cannot_exceed_existing_cartesian_limit(setup,speed):
    _,p,_,_,_,q=fixture(setup);p['f710_home']['clearance']['linear_speed_m_s']=speed
    with pytest.raises(ValueError,match='speed'):home_target(p,q)
