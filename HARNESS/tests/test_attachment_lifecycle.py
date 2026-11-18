"""Loaded motion consumes fresh loss evidence before another waypoint."""
from itertools import product
import numpy as np
import pytest

from f1_harness.owner_bridge import JointProposal
from f1_harness.scene import box_frame_from_face
from test_grasp_execution import observed
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('skill',['transfer_above_hole','align','insert','move_tcp'])
def test_loaded_loss_cancels_remaining_motion_and_requires_hold(tmp_path,skill):
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        engine.running=engine.pending.model_copy(update={'skill':skill});engine.pending=None
        engine.completed_skills.update(('approach_grasp','grasp','verify_grasp','inspect_in_hand'))
        state['measured16'][15]=state['held16'][15]=100.
        target=tuple(state['held16'])
        plan=JointProposal(engine.running.request_id,engine.epoch,'scene','real',tuple(state['full22']),
                           (target,target),.1,100_000_000,.5)
        bridge.proposal=plan;bridge.current_target=target
        bridge.plan_observation=observed(obs,clock[0],True)
        bridge.update_feedback(full22=state['full22'],measured16=target,stamps16=(clock[0],)*16)
        bridge.after_publication(publication_id='loaded',measured16=target,publication_complete=True,now_ns=clock[0])
        clock[0]+=20_000_000
        engine.live_observation=observed(obs,clock[0],False)
        assert poll() is None
        result=engine.results[plan.request_id]
        assert result.reason=='attachment_lost' and result.cancel_stage=='cancel_requested'
        assert result.resume_request_id is None and not result.no_motion
        assert bridge.proposal is None and engine.hold_requested and engine.authorized
        assert 'verify_grasp' not in engine.completed_skills
        engine.acknowledge_hold(publication_id='hold',measured_stationary=True)
        assert engine.results[plan.request_id].cancel_stage=='measured_stationary'
    finally:bridge.close()


@pytest.mark.parametrize('spans,expected_height',[(.03,.06),(.06,.03)])
def test_rectangular_face_maps_long_axis_into_plane(spans,expected_height):
    box=dict(min=[-.015,-.015,0],max=[.015,.015,.06])
    face=np.eye(4);face[2,3]=.03
    obj,diagnostic=box_frame_from_face(face,{'boundary_fit':{'robust_planar_spans_m':[spans,expected_height]}},box)
    corners=np.array(list(product(*zip(box['min'],box['max']))))@obj[:3,:3].T+obj[:3,3]
    assert corners[:,2].min()==pytest.approx(0)
    assert corners[:,2].max()==pytest.approx(.03)
    assert abs(obj[2,2])<1e-9 and np.linalg.det(obj[:3,:3])==pytest.approx(1)
    assert diagnostic['normal_is_long_axis'] is False


def test_square_top_preserves_registered_bottom_origin():
    face=np.eye(4);face[2,3]=.06
    obj,_=box_frame_from_face(face,{'boundary_fit':{'robust_planar_spans_m':[.03,.03]}},
                              dict(min=[-.015,-.015,0],max=[.015,.015,.06]))
    assert np.allclose(obj,np.eye(4))


@pytest.mark.parametrize('failure,structured',[
    ('scene_sweep_blocked:table',True),('registered_candidates_exhausted:{}',True),
    ('numerical_worker_lost',False),('unrelated runtime error',False)])
def test_rpc_preview_candidate_rejection_is_a_result_but_unknown_failure_propagates(tmp_path,failure,structured):
    engine,bridge,clock,state,_,_=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None;engine.attempted_motion.clear()
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    def reject(*args):raise RuntimeError(failure)
    bridge.preview_planner=reject
    try:
        if structured:
            result=bridge.preview_skill(intent.model_dump())
            assert result['planned'] is False and result['no_motion'] and result['current_scope']
        else:
            with pytest.raises(RuntimeError):bridge.preview_skill(intent.model_dump())
        assert not engine.running and not engine.pending and not engine.request_publications
    finally:bridge.close()


def test_tool_waits_for_measured_loss_hold_by_querying_same_request(tmp_path,monkeypatch):
    import time
    from f1_harness.remote import RemoteTaskBackend
    from f1_harness.contracts import SkillResult
    engine,bridge,_,_,_,_=make_bridge(tmp_path)
    intent=engine.pending.model_copy(update={'skill':'transfer_above_hole'})
    backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
    backend.session=engine.session;backend.steps=0;backend.task=engine.task;backend._progress=lambda *a,**kw:None
    calls=[];stages=iter(['cancel_requested','owner_hold_committed','measured_stationary'])
    def read(method,**kwargs):
        calls.append((method,kwargs))
        if method=='heartbeat':return {}
        return SkillResult(request_id=intent.request_id,lifecycle='completed',outcome='failure',
                           reason='attachment_lost',cancel_stage=next(stages)).model_dump()
    backend.call=read
    monkeypatch.setattr('f1_harness.remote.time.sleep',lambda _:None)
    try:
        result=backend._submit_and_wait(intent,deadline=time.monotonic()+1)
        assert result['cancel_stage']=='measured_stationary' and backend.pending is None
        assert sum(name=='submit_skill' for name,_ in calls)==1
        assert [kw['request_id'] for name,kw in calls if name=='skill_result']==[intent.request_id]*2
    finally:bridge.close()
