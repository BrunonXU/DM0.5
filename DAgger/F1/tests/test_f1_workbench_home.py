"""No robot I/O: exact workbench curve and bounded settling semantics."""
import copy
import numpy as np
import pytest
from legacy_home_setup import setup, numerical_setup
from test_f1_clearance_home import motion_fixture
from f1dagger.clearance_home import WorkbenchHomeMotion, ClearanceHomeStopped
from f1dagger.config import infra_import


def test_home_samples_are_identical_to_workbench_and_only_commit_advances(setup):
    c,p,_,_,full=setup
    kind=infra_import(c['_infra_root'],'robot.motion_plan').SmoothBodyPlan
    start=np.r_[full[:14],43.,66.];goal=start.copy();goal[7:14]+=[1.,2.,3.,4.,5.,6.,7.]
    motion=WorkbenchHomeMotion(p,start,goal,kind)
    reference=kind(dict(enumerate(start[:14])),dict(enumerate(goal[:14])),
        dict.fromkeys(range(14),p['max_joint_speed_deg_s']),
        dict.fromkeys(range(14),p['max_joint_acceleration_deg_s2']),59.7)
    t=0.
    for _ in range(300):
        t=min(t+.02,reference.duration)
        candidate=motion.propose(.02)
        np.testing.assert_array_equal(candidate,motion.propose(.02))
        np.testing.assert_allclose(candidate[:14],list(reference.sample(t).values()),atol=1e-12)
        np.testing.assert_array_equal(candidate[:7],start[:7])
        np.testing.assert_array_equal(candidate[14:],start[14:])
        motion.commit()
    np.testing.assert_array_equal(candidate,goal)


@pytest.mark.parametrize('persistent',[False,True])
def test_tracking_error_waits_three_seconds_without_chasing_feedback(setup,persistent):
    motion,full,q=motion_fixture(setup)
    # Fixed valid waypoint. The feedback is initially 1.52 degrees behind;
    # transient recovery at one second must not be treated as a failed Home.
    motion.command_translation=motion.pose(q[7:14]).translation.copy()
    motion.lift=motion.command_translation.copy()
    initial_target=motion.command_translation.copy()
    for tick in range(1,152):
        now=tick*20_000_000;actual=q.copy()
        if persistent or tick<50:actual[7]-=1.52
        if persistent and tick==151:
            with pytest.raises(ClearanceHomeStopped,match='after settling wait'):
                motion._correct_settled_feedback(q,actual,(now,)*16,now)
        else:
            waiting=motion._correct_settled_feedback(q,actual,(now,)*16,now)
            if 9<=tick<50:assert waiting is True
            np.testing.assert_array_equal(motion.command_translation,initial_target)
    assert not motion.done


def test_candidate_wrist_limit_is_local_stop_without_clipping(setup):
    motion,full,q=motion_fixture(setup)
    candidate=q.copy();candidate[12:14]=[60.,50.]
    original=candidate.copy()
    with pytest.raises(ClearanceHomeStopped,match='candidate outside joint/wrist limits'):
        motion._check_clearance_candidate(candidate)
    np.testing.assert_array_equal(candidate,original)
    # The same invalid values in the actual submitted state remain a global
    # contract violation; never absorb them as ordinary local candidate failure.
    with pytest.raises(ValueError,match='coupled wrist'):
        motion.propose(.02,full,candidate,q,(1,)*16,1)


def test_intermediate_arrival_requires_joint_following_even_if_fk_is_close(setup):
    motion,full,q=motion_fixture(setup)
    motion._pose_close=lambda *args,**kwargs:True
    motion.relaxed=False
    actual=q.copy();actual[7]+=1.01
    assert not motion._arrived(actual,q)


@pytest.mark.parametrize('case',[0,1])
def test_deployed_v1_route_through_real_owner_output_with_delay(setup,tmp_path,case):
    import os,json
    from pathlib import Path
    from test_f1_home_low_wrist import test_recorded_low_wrist_full_route_with_original_guards
    c,p,m,u,full=setup
    path=os.environ.get('F1_HOME_TEST_CONFIG')
    if not path:pytest.skip('requires frozen candidate session config')
    cfg=json.loads(Path(path).read_text());p=copy.deepcopy(p);p['f710_home']=cfg['f710_home']
    assert p['f710_home']['name']=='积木demo-V1'
    # This regression freezes the previous clearance route, even when the
    # deployed candidate now explicitly selects the direct workbench route.
    p['f710_home'].pop('route',None)
    p['f710_home']['clearance']=dict(schema='f1_right_clearance_v2',frame='base_link',
        lift_m=.06,right_m=.10,max_rotation_deg=10.)
    test_recorded_low_wrist_full_route_with_original_guards((c,p,m,u,full),tmp_path,case,10)
