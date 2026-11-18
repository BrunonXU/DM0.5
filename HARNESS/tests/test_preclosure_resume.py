from dataclasses import replace
import pytest
from f1_harness.contracts import SceneObject
from f1_harness.trajectory import preclosure_checkpoint
from test_unpublished_plan import make_bridge, finish_plan


@pytest.mark.parametrize('closed,empty,expected',[(False,True,True),(True,True,False),(False,False,False)])
def test_only_known_empty_unclosed_grasp_can_resume(tmp_path,closed,empty,expected):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        poll();finish_plan(b);poll()
        e.running=e.running.model_copy(update={'skill':'grasp'})
        obj=SceneObject(object_id='block',shape='square',held=False if empty else None)
        b.plan_observation=obs.model_copy(update={'objects':(obj,)})
        target=list(state['held16']);target[7]=2.
        close=list(target);close[15]=100.
        b.proposal=replace(b.proposal,targets=(tuple(target),tuple(close)))
        b.current_target=close if closed else target;b.index=1 if closed else 0
        if closed:state['measured16'][15]=100.
        for dt in (0,20_000_000,2_020_000_000):
            clock[0]=10_000_000_000+dt
            b.update_feedback(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
            b.after_publication(publication_id=str(dt),measured16=state['measured16'],
                                publication_complete=True,now_ns=clock[0])
        result=e.results['one-request']
        assert bool(result.resume_request_id)==expected
        assert e.hold_requested and result.reason=='motion_stalled'
        assert e.motion_checkpoints['one-request']['preclosure']==expected
    finally:b.close()


def test_preclosure_check_rejects_changed_feedback_or_no_closure():
    from types import SimpleNamespace
    anchor=[0.]*15+[18.];closed=[0.]*15+[100.]
    plan=SimpleNamespace(targets=(anchor,closed),gripper_tolerance=.5)
    assert preclosure_checkpoint(plan,0,anchor,anchor)
    assert not preclosure_checkpoint(plan,1,anchor,anchor)
    assert not preclosure_checkpoint(plan,0,closed,anchor)
    assert not preclosure_checkpoint(SimpleNamespace(targets=(anchor,),gripper_tolerance=.5),0,anchor,anchor)
