from types import SimpleNamespace
import pytest
from f1_replay.bundle import ARM_NAMES
from f1_replay.pose_cli import wait_arm_tracking
from f1_replay.runtime import ReplayError,run_preparation_schedule


def errors(value):
    return {'joints':dict.fromkeys(ARM_NAMES,value),'grippers':{}}


def test_transient_lag_holds_then_resumes_without_changing_target():
    clock=[0.];held=[];logs=[];samples=iter([5.057,5.01,4.8])
    paused=wait_arm_tracking(lambda:errors(next(samples)),lambda:held.append('same_target'),
        lambda *x:logs.append(x),5.,10.,20.,now=lambda:clock[0],
        sleep=lambda dt:clock.__setitem__(0,clock[0]+dt))
    assert paused and held==['same_target','same_target']
    assert logs[-1][0]=='arm_tracking_wait_resumed'


def test_unresponsive_arm_remains_bounded():
    clock=[0.];held=[]
    with pytest.raises(ReplayError,match='未继续累积目标'):
        wait_arm_tracking(lambda:errors(5.057),lambda:held.append('same_target'),lambda *x:None,
            5.,.2,20.,now=lambda:clock[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+dt))
    assert len(held)==4


def test_wait_does_not_swallow_feedback_fault():
    samples=iter([errors(5.1)])
    def read():
        try:return next(samples)
        except StopIteration:raise ReplayError('反馈过旧')
    with pytest.raises(ReplayError,match='反馈过旧'):
        wait_arm_tracking(read,lambda:None,lambda *x:None,5.,10.,20.,sleep=lambda _:None)


def test_explicit_pause_stretches_schedule_without_skip_or_catchup():
    clock=[0];sent=[];counter=[0]
    def pause():
        counter[0]+=1
        if counter[0]==2:
            clock[0]+=1_000_000_000
            return True
        return False
    frames=[SimpleNamespace(time_ns=i*40_000_000,source_time_ns=i*40_000_000,action=(i,)) for i in range(3)]
    run_preparation_schedule(SimpleNamespace(frames=frames,duration_ns=120_000_000),
        lambda a:sent.append((a,clock[0])),lambda:None,lambda *x:None,.2,
        now=lambda:clock[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+round(dt*1e9)),
        max_command_hz=30,before_frame=pause)
    assert [x[0] for x in sent]==[(0,),(1,),(2,)]
    assert sent[1][1]>=1_040_000_000 and sent[2][1]-sent[1][1]>=40_000_000
