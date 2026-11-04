"""Y must publish opening and observe arrival before its two-second hold."""
from types import SimpleNamespace
import numpy as np
from dagger_core.hil_supervisor import HILState
from f1dagger.contract import ARM_NAMES
from test_f1_runtime import setup_loop


def prepare(monkeypatch):
    import f1dagger.runtime as runtime
    class Home:
        def __init__(self, profile, start, goal): self.goal=goal.copy(); self.pending=None
        def propose(self, dt): self.pending=True; return self.goal.copy()
        def commit(self): self.pending=None
    monkeypatch.setattr(runtime, 'HomeMotion', Home)
    loop,e,o,t,now,records,step=setup_loop()
    loop.profile=dict(max_joint_speed_deg_s=30, joint_tolerance_deg=1,
        gripper_tolerance=5, settle_timeout_s=3,
        default_presets=[dict(key='lxl-xy-reset-v1',units='degree',joints=dict.fromkeys(ARM_NAMES,0))])
    feedback=np.r_[np.ones(14)*10,32.,66.]
    stamp=[None]
    o.source.feedback=lambda:(np.r_[feedback[:14],np.zeros(8)],feedback.copy(),
                              (now[0],)*15+(now[0] if stamp[0] is None else stamp[0],))
    step(0); step(1,1)
    e.home_request=True; e.ending_episode=True
    e.episode_buttons=SimpleNamespace(poll=lambda n:None,home_active=False,home_goal=None,hold_since=None)
    e.episode_buttons.operator=lambda p,r,n,h:e._operator(p,r,n,hand_pending=h)
    return loop,e,o,t,now,records,step,feedback,stamp


def test_y_opens_after_arm_arrival_requires_real_gripper_and_stable_hold(monkeypatch):
    loop,e,o,t,now,records,step,feedback,stamp=prepare(monkeypatch)
    try:
        step(2,1)
        assert o.last_submitted[15] == 66 and loop.home is not None
        feedback[:14]=0
        for i in range(3,14): step(i,1)
        assert o.last_submitted[15] == 0 and o.last_submitted[14] == 32
        assert loop.home is not None and e.episode_buttons.hold_since is None
        feedback[15]=0
        stamp[0]=loop.home_gripper_started
        step(14,1)
        assert loop.home is not None
        stamp[0]=None
        step(15,1)
        assert loop.home is None and e.episode_buttons.home_goal[15] == 0
        assert e.episode_buttons.hold_since is not None
        feedback[15]=66
        step(16,1)
        assert e.episode_buttons.hold_since is None
        feedback[15]=0
        step(17,1)
        start=e.episode_buttons.hold_since
        for i in range(18,118):step(i,1)
        assert now[0]-start == 2_000_000_000
        assert all(r['source'] in ('home','episode_reset_hold') for r in records[1:])
        assert sum(ev[0][0]=='home_gripper_open_requested' for ev in e.recorder.events)==1
    finally:e.close()


def test_y_gripper_timeout_holds_instead_of_claiming_home_complete(monkeypatch):
    loop,e,o,t,now,records,step,feedback,stamp=prepare(monkeypatch)
    try:
        feedback[:14]=0
        for i in range(2,165):step(i,1)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert 'home right gripper feedback timeout' in e.hard_hold_reason
        assert not any(ev[0][0]=='home_completed_feedback' for ev in e.recorder.events)
        assert e.episode_buttons.home_goal is None
    finally:e.close()
