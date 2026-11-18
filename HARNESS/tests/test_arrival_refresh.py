import pytest
from f1_harness.owner_bridge import JointProposal
from f1_harness.trajectory import schedule
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('proof_valid,context_valid',[(True,True),(False,True),(True,False)])
def test_refresh_only_valid_geometry_in_same_request(tmp_path,proof_valid,context_valid):
    e,b,clock,state,_,obs=make_bridge(tmp_path)
    try:
        e.running=e.pending;e.pending=None
        target=tuple(state['held16']);closing=list(target);closing[15]+=10
        timing=schedule([target,closing],target,[dict(end_index=1),dict(end_index=2)],20.)
        b.proposal=JointProposal(e.running.request_id,e.epoch,'scene','real',(0.,)*22,
            (target,tuple(closing)),None,300_000_000,.5,trajectory=timing)
        b.current_target=target;b.plan_observation=obs;b.plan_held_reference=target
        b.trajectory_at_boundary=True;b.waypoint_published_ns=clock[0]-1
        b.waypoint_verifier=lambda *a:dict(accepted=proof_valid)
        measured=list(target)
        def tick():
            b.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(clock[0],)*16)
            b.after_publication(publication_id=str(clock[0]),measured16=measured,
                                publication_complete=True,now_ns=clock[0])
        tick()
        future,fn,args=b.executor.jobs[-1]
        if not context_valid:b.arrival_check['key']=('unrelated',e.epoch,0)
        future.set_result(fn(*args))
        clock[0]+=20_000_000;measured[7]+=2*b.stop_delta_deg
        tick()
        assert b.index==0 and b.settled_since is None
        assert len(b.executor.jobs)==1
        if proof_valid and context_valid:
            # Moving samples do not trigger eager geometry refresh.
            clock[0]+=20_000_000;measured[7]+=2*b.stop_delta_deg;tick()
            assert len(b.executor.jobs)==1
            # A new equivalent sample advances computation, not arrival.
            clock[0]+=20_000_000;tick()
            assert len(b.executor.jobs)==2 and b.index==0 and b.settled_since is None
            pending=b.arrival_future
            for _ in range(5):
                clock[0]+=20_000_000;tick()
            assert b.arrival_future is pending and len(b.executor.jobs)==2
            pending.set_result(dict(accepted=True))
            clock[0]+=20_000_000;tick()
            assert b.index==0
            for _ in range(14):
                clock[0]+=20_000_000;tick();assert b.index==0
            clock[0]+=20_000_000;tick()
            assert b.index==1
        else:
            clock[0]+=80_000_000;tick();assert len(b.executor.jobs)==1
            clock[0]+=20_000_000;tick();assert len(b.executor.jobs)==2
    finally:b.close()
