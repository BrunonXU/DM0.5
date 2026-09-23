from types import SimpleNamespace as N
import pytest
from f1_replay.pose_segments import segment_end_indices,wait_pose_segment
from f1_replay.bundle import ARM_NAMES,BODY_NAMES
from f1_replay.runtime import ReplayError

def frame(x):return N(action=(x,)*14+(0.,)*7)
def errs(x):return {'joints':{**dict.fromkeys(ARM_NAMES,x),**dict.fromkeys(BODY_NAMES,0.)},'grippers':{'left':0.,'right':0.}}
def test_boundaries_keep_every_waypoint_and_final_target():
    frames=[frame(i*.01) for i in range(401)]
    ends=segment_end_indices(frames,8)
    assert ends[-1]==400 and all(i%8==0 for i in ends)
    for a,b in zip([0]+ends,ends):assert 0<frames[b].action[0]-frames[a].action[0]<=1

def test_body_and_gripper_boundaries_are_also_bounded():
    frames=[N(action=(0.,)*14+(i*.004,)*5+(i*.1,)*2) for i in range(161)]
    ends=segment_end_indices(frames,8)
    for a,b in zip([0]+ends,ends):
        assert frames[b].action[14]-frames[a].action[14]<=.25
        assert frames[b].action[19]-frames[a].action[19]<=5

@pytest.mark.parametrize('frames,stride',[([frame(0),frame(2)],1),([frame(0),frame(.1)],8)])
def test_invalid_sampling_rejected(frames,stride):
    with pytest.raises(ReplayError):segment_end_indices(frames,stride)

def run_wait(read,clock,hold):
    return wait_pose_segment(read,hold,lambda *a:None,hz=200,body_tolerance=.5,
                            gripper_tolerance=5,timeout_s=.3,
                            now=lambda:clock[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+dt))

def test_stalled_arm_only_holds_endpoint_then_stops():
    clock=[0.];holds=[]
    with pytest.raises(ReplayError,match='未发送后续段'):
        run_wait(lambda:errs(1.),clock,lambda n:holds.append(n))
    assert 59<=len(holds)<=61

def test_transient_lag_settles_before_continuation():
    clock=[0.];holds=[]
    run_wait(lambda:errs(.1 if clock[0]>=.05 else .8),clock,lambda n:holds.append(clock[0]))
    assert .2<=clock[0]<.3
    assert all(b-a>=.004999 for a,b in zip(holds,holds[1:]))

def test_freshness_failure_propagates_without_further_hold():
    clock=[0.];holds=[]
    def read():
        if clock[0]>.02:raise ReplayError('反馈过旧')
        return errs(1.)
    with pytest.raises(ReplayError,match='反馈过旧'):
        run_wait(read,clock,lambda n:holds.append(n))
    assert len(holds)<=4
