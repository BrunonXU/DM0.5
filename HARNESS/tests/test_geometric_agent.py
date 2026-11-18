"""Geometric tool contracts and actual owner/planner paths, without hardware."""
from dataclasses import replace
import numpy as np
import pytest
from pydantic import ValidationError

from f1_harness.contracts import MotionParameters, SkillIntent, HarnessError
from f1_harness.motion_goals import apply_request, cartesian_proposal
from f1_harness.grasp_candidates import apply_descriptor
from f1_harness.grasp_selection import recorded_scene
from f1_harness.owner_bridge import JointProposal
from test_grasp_selection_planning import setup
from test_unpublished_plan import make_bridge


def test_continuous_full_grasp_reaches_numerics_and_revalidation(monkeypatch):
    plan, obs, intent, _ = setup()
    plan.fixture['parameterized_grasp'] = True
    pose = np.eye(4); pose[:3, 3] = [.002, -.003, .02]
    intent = SkillIntent.model_validate(dict(intent.model_dump(), parameters=dict(
        grasp_tcp_pose=pose.ravel().tolist(), approach_direction_object=[1., 0., 1.], approach_distance_m=.11)))
    seen=[]
    def solve(intent, scene, held, full, variant,*,deadline_ns=None): 
        seen.append((scene,variant)); return {'geometry_trace':[{}]}
    monkeypatch.setattr(plan, '_solve_candidate', solve)
    plan.dispatch('plan_skill', dict(intent=intent.model_dump(), observation=obs.model_dump(),
        full22=[0.]*22, held16=list(obs.state16)))
    scene, variant = seen[0]
    assert variant == 0 and len(seen) == 1
    assert np.allclose(scene.object_grasp_eef @ scene.eef_tip_reference, pose)
    assert np.allclose(scene.approach_direction_object,[1,0,1])
    assert scene.grasp_candidate_descriptor['strategy']=='object_tcp'


@pytest.mark.parametrize('frame',['world','object','hole'])
def test_tcp_request_uses_declared_frame_and_tcp_offset(frame):
    plan, obs, intent, _ = setup(); plan.fixture['cartesian_motion']=True
    scene = plan._scene(obs,[0.]*22,obs.state16)
    target=np.eye(4); target[:3,3]=[.15,.1,.5]
    intent=SkillIntent.model_validate(dict(intent.model_dump(),skill='move_tcp',parameters=dict(
        target_tcp_pose=target.ravel().tolist(),target_frame=frame)))
    applied=apply_request(plan,scene,intent)
    basis={'world':plan.frames.world_native,'object':scene.base_object,'hole':scene.base_hole}[frame]
    goal=np.array(applied.cartesian_request['goals'][-1])
    assert np.allclose(goal @ scene.eef_tip_reference,basis @ target)
    proposal=cartesian_proposal(applied)
    assert proposal['stages'][0]['allow_target_contact'] is False
    assert proposal['stages'][0]['gripper'] is None


def test_generic_motion_retains_attached_object_requirement():
    plan,obs,intent,_=setup();plan.fixture['cartesian_motion']=True
    scene=replace(plan._scene(obs,[0.]*22,obs.state16),object_held=True,eef_object=None)
    intent=SkillIntent.model_validate(dict(intent.model_dump(),skill='move_tcp',parameters=dict(
        target_tcp_pose=np.eye(4).ravel().tolist())))
    with pytest.raises(HarnessError,match='measured_in_hand'):
        cartesian_proposal(apply_request(plan,scene,intent))


def test_bad_rotation_or_ambiguous_grasp_is_rejected():
    bad=np.eye(4);bad[0,0]=2
    with pytest.raises(ValueError):MotionParameters(grasp_tcp_pose=tuple(bad.ravel()))
    with pytest.raises(ValueError):MotionParameters(grasp_tcp_pose=tuple(np.eye(4).ravel()),variant=1)


def test_preview_has_no_submission_or_task_progress(tmp_path):
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    intent=engine.pending
    engine.pending=None
    engine.attempted_motion.clear()
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    bridge.preview_planner=lambda *args:JointProposal(intent.request_id,intent.epoch,intent.scene_version,
        intent.calibration_id,tuple(state['full22']),(tuple(state['held16']),),.1,100000000,.5)
    try:
        before=(set(engine.completed_skills),set(engine.attempted_motion),dict(engine.results))
        result=bridge.preview_skill(intent.model_dump())
        assert result['planned'] and result['no_motion'] and not result['executable']
        assert (set(engine.completed_skills),set(engine.attempted_motion),dict(engine.results))==before
        assert engine.pending is None and not engine.running and not bridge.executor.jobs
    finally:bridge.close()


def test_owner_preview_returns_real_placement_warning_without_rejecting_approach(tmp_path):
    from f1_harness.skills import propose,grasp_route_lookahead
    from test_full_development_skills import scene
    geometry=replace(scene(),object_held=False,eef_object=None,transfer_policy='registered_transport',
        obstacles=({'min':[.42,-.2,.25],'max':[.65,.2,.34],
                    'role':'bucket','name':'closed_aperture'},))
    approach=propose('approach_grasp',geometry)
    trace=dict(approach['stages'][0],grasp_route_lookahead=grasp_route_lookahead(geometry,approach))
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None;engine.attempted_motion.clear()
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    bridge.preview_planner=lambda *args:JointProposal(intent.request_id,intent.epoch,intent.scene_version,
        intent.calibration_id,tuple(state['full22']),(tuple(state['held16']),),.1,100000000,.5,
        geometry_trace=(trace,))
    try:
        result=bridge.preview_skill(intent.model_dump())
        assert result['planned'] and result['current_scope'] and result['no_motion']
        advisory=result['placement_advisory']
        assert advisory['status']=='predicted_blocked'
        assert advisory['predicted_attachment_only'] and not advisory['motion_authorized']
        assert not advisory['numerical_feasibility_checked']
        assert advisory['obstruction']['metrics']['obstacle_name']=='closed_aperture'
        assert engine.pending is None and not engine.running and not bridge.executor.jobs
        # Exercise the actual PyRUA ledger projection of this production reply.
        from pyrualean.arms import ArmToolkit
        from f1_harness.adapters import F1Robot
        from test_planner_skill_feedback import Backend
        arm=ArmToolkit(F1Robot(Backend(result)),arm='cells',workspace=tmp_path/'worker',images=False)
        feedback=arm.execute_tool('python',{'code':"p = robo.preview('approach_grasp')"}).result
        call=next(c for c in feedback['primitive_calls'] if c['tool']=='preview_skill')
        assert call['placement_advisory']==advisory
        assert not feedback.get('exception')
    finally:bridge.close()


def test_image_only_gateway_does_not_update_owner():
    from types import SimpleNamespace
    from f1_harness.gateway import Gateway
    def fail(*args):raise AssertionError('must not ingest image-only data')
    gateway=Gateway(SimpleNamespace(update_observation=fail),SimpleNamespace(
        camera_observation=lambda:dict(image_only=True,motion_evidence=False,frames=[])),None)
    assert gateway.dispatch('camera_observation')['image_only']


def test_failed_observation_does_not_lose_owner_verification_checkpoint():
    from f1_harness.remote import RemoteTaskBackend
    backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
    original=dict(request_id='original',verification_request_id='original',outcome='pending_review',
        reason='post_motion_verification_unavailable:fresh_post_motion_scene_missing',lifecycle='completed')
    backend.session='session';backend.call=lambda *a,**kw:original.copy()
    result=backend._needs_attention('approach_grasp','surface_points_missing_or_invalid','original')
    assert result['request_id']=='original' and result['verification_request_id']=='original'
    assert result['continuation_submitted'] is False and result['outcome']=='pending_review'


def test_tcp_plan_reconstructs_requested_goals_during_fresh_revalidation():
    from f1_harness.contracts import SKILLS,digest
    from test_plan_revalidation import observation
    plan,obs,intent,clock=setup();plan.fixture['cartesian_motion']=True
    plan.task=plan.task.model_copy(update=dict(allowed_skills=SKILLS))
    target=np.eye(4);target[:3,3]=[.05,.05,.5]
    intent=SkillIntent.model_validate(dict(intent.model_dump(),skill='move_tcp',task_hash=digest(plan.task),
        parameters=dict(target_tcp_pose=target.ravel().tolist())))
    candidate=plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    clock[0]+=100000000;fresh=observation(clock[0],name='fresh')
    proof=plan.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=candidate,
        original_observation=obs.model_dump(),observation=fresh.model_dump(),
        full22=[0.]*22,held16=list(obs.state16),after_ns=clock[0]-1))
    assert proof['observation_sha256']==digest(fresh)


def test_tcp_continuation_can_omit_goal_but_fresh_motion_cannot():
    _,_,intent,_=setup()
    data=dict(intent.model_dump(),skill='move_tcp')
    with pytest.raises(ValueError):SkillIntent.model_validate(data)
    continued=SkillIntent.model_validate(dict(data,resume_request_id='owner-issued'))
    assert not continued.parameters.supplied()


def test_zero_publication_move_restores_grasp_progress(tmp_path):
    from f1_harness.contracts import SKILLS,SkillResult
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        engine.pending=engine.pending.model_copy(update=dict(skill='move_tcp'))
        engine.completed_skills={'approach_grasp'}
        engine.active_grasp_selection={'saved':'selection'}
        intent=engine.take_pending()
        assert 'approach_grasp' not in engine.completed_skills and engine.active_grasp_selection is None
        engine.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
            outcome='pending_review',no_motion=True,reason='numerical_plan_rejected:registered_candidates_exhausted'))
        assert engine.completed_skills=={'approach_grasp'}
        assert engine.active_grasp_selection=={'saved':'selection'}
    finally:bridge.close()


def test_pyrua_can_generate_preview_then_submit_continuous_tcp(tmp_path):
    from f1_harness.adapters import F1Robot
    from pyrualean.arms import ArmToolkit
    from test_astra_programs import cell
    class Backend:
        def __init__(self):self.calls=[]
        def execute_tool(self,name,args):
            self.calls.append((name,args))
            if name=='geometry_context':return dict(current_tcp_pose=np.eye(4).ravel().tolist())
            if name=='preview_skill':return dict(planned=True,no_motion=True,current_scope=True,executable=False)
            if name=='execute_skill':return dict(outcome='success',reason='synthetic')
            if name=='observe':return dict(observation=dict(observation_id='synthetic',objects=[]),status={})
            raise AssertionError(name)
        def env_steps(self):return 0
        def solved(self):return False
    backend=Backend();arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images=False)
    cell(arm,'pose = robo.geometry()["current_tcp_pose"]\npose[3] += 0.073\ncheck = robo.preview("move_tcp", target_tcp_pose=pose)\nif check["planned"]:\n    result = robo.move_to(pose)')
    relevant=[(name,args) for name,args in backend.calls if name!='observe']
    assert [name for name,_ in relevant]==['geometry_context','preview_skill','execute_skill']
    assert relevant[-1][1]['parameters']['target_tcp_pose'][3]==.073
