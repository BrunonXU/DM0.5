"""Actual low-wrist Y starting poses; numerical plant, never robot I/O."""
import json
from pathlib import Path
import numpy as np
import pytest
from legacy_home_setup import setup, numerical_setup
from f1dagger.clearance_home import FeedbackAlignedClearanceHome, ClearanceHomeStopped
from f1dagger.contract import home_target
from f1dagger.teleop import F1ArmTarget
from f1dagger.robot_io import make_output, CollisionGate


@pytest.mark.parametrize('case',[0,1])
@pytest.mark.parametrize('delay_ticks',[0,10])
def test_recorded_low_wrist_full_route_with_original_guards(setup,tmp_path,case,delay_ticks):
    c,p,m,u,_=setup
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_low_wrist_20261014.json').read_text())[case]
    q=np.array(sample['start_submitted']);full=np.array(sample['full22']);fixed=q.copy()
    measured=np.r_[full[:14],q[14:]]
    motion=FeedbackAlignedClearanceHome(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    output=make_output(c);output.planner.set_limits(m.limits);output.push(q[:14],0.,enabled=(False,False))
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    history=[measured.copy()]*(delay_ticks+1);stages=[motion.stage];max_angle=0.;wrist_excursion=0.
    for tick in range(1,15001):
        now=tick*4_000_000;measured=history[-delay_ticks-1].copy();full[:14]=measured[:14]
        if tick%5==0:
            previous=motion.stage
            target=motion.propose(.02,full,q,measured,(now,)*16,now)
            if previous!=motion.stage:
                waypoint=motion.lift if previous=='lift' else motion.clear
                assert np.linalg.norm(motion.pose(measured[7:14]).translation-waypoint)<=.002
                stages.append(motion.stage)
            guard.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
        nxt=np.r_[output.sample(now/1e9),q[14:]]
        assert output.fault is None
        m.check_action(nxt);guard.check(nxt,full)
        assert np.max(np.abs(nxt-q))<=p['max_joint_speed_deg_s']*.004+1e-7
        np.testing.assert_array_equal(nxt[:7],fixed[:7]);np.testing.assert_array_equal(nxt[14:],fixed[14:])
        if motion.stage!='return':
            angle=np.linalg.norm(motion.pin.log3(motion.pose(nxt[7:14]).rotation@motion.rotation.T))
            max_angle=max(max_angle,angle)
            assert angle<=np.deg2rad(p['f710_home']['clearance']['max_rotation_deg'])
            wrist_excursion=max(wrist_excursion,abs(nxt[12])-abs(fixed[12]))
        q=nxt;history.append(q.copy());history=history[-delay_ticks-2:]
        if motion.done:break
    assert motion.done and stages==['lift','right','return']
    assert wrist_excursion>1., 'real correction must permit coordinated wrist motion'
    np.testing.assert_allclose(q[7:14],motion.goal[7:14],atol=.01)
    np.testing.assert_allclose(measured[7:14],motion.goal[7:14],atol=p['joint_tolerance_deg'])


def test_unresponsive_wrist_is_not_reported_as_success(setup,tmp_path):
    c,p,m,u,_=setup
    sample=json.loads((Path(__file__).parent/'fixtures/clearance_low_wrist_20261014.json').read_text())[0]
    q=np.array(sample['start_submitted']);full=np.array(sample['full22']);initial=q.copy()
    motion=FeedbackAlignedClearanceHome(p,q,home_target(p,q),F1ArmTarget(u,m,c['ik']),full,0)
    output=make_output(c);output.planner.set_limits(m.limits);output.push(q[:14],0.,enabled=(False,False))
    guard=CollisionGate(c,m,tmp_path/'collision',full)
    with pytest.raises(ClearanceHomeStopped,match='tracking error|feedback timeout|orientation budget'):
        for tick in range(1,15001):
            now=tick*4_000_000;measured=q.copy();measured[12]=initial[12];full[:14]=measured[:14]
            if tick%5==0:
                target=motion.propose(.02,full,q,measured,(now,)*16,now)
                guard.check(target,full);output.push(target[:14],now/1e9,enabled=motion.enabled);motion.commit()
            q=np.r_[output.sample(now/1e9),q[14:]];m.check_action(q);guard.check(q,full)
            assert not motion.done
    assert motion.stage=='lift' and not motion.done
