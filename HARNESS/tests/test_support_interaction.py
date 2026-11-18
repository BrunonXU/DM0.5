from dataclasses import asdict,replace
from types import SimpleNamespace
import numpy as np
import pytest

from f1_harness.contracts import Task,SKILLS,SkillResult,MotionParameters,digest,HarnessError
from f1_harness.support_motion import scene_support,verify_support_release
from f1_harness.skills import propose
from f1_harness.motion_goals import cartesian_proposal
from test_support_evidence import observed,BOX,PLANE
from test_full_development_skills import scene,pose


def service():
    return SimpleNamespace(task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal',
        admission_profile='development_coarse',allowed_skills=SKILLS),
        fixture=dict(native_frame='base',object_local_box=BOX,object_planning_margin_m=.002,
            obstacles=[dict(type='halfspace',role='table',point=PLANE['point'],normal=PLANE['normal'])]))


def test_release_on_support_does_not_need_a_hole_or_claim_placement():
    source=service();obs=observed(held=True)
    measurement=scene_support(source,obs)
    value=replace(scene(),base_hole=None,aperture_measured=False,support_measurement=measurement)
    planned=propose('release_supported',value)
    assert planned['stages'][0]['goals']==[]
    assert planned['stages'][0]['gripper']==value.release_opening
    assert not measurement['support_observed']
    before=verify_support_release(source.task,obs,measurement,'request')
    assert before.outcome=='pending_review'
    after=observed(held=False)
    result=verify_support_release(source.task,after,scene_support(source,after),'request')
    assert result.outcome=='success' and result.reason=='supported_release_observed'
    assert after.objects[0].inserted is None


@pytest.mark.parametrize('change',[dict(held=None),dict(stationary=False),dict(height=.05),dict(stale=True)])
def test_release_cannot_be_planned_from_unknown_airborne_or_old_evidence(change):
    source=service();obs=observed(held=change.get('held',True),stationary=change.get('stationary',True),
        pose=pose(z=change.get('height',0)))
    if change.get('stale'):
        obs=obs.model_copy(update=dict(frames=()))
    measured=scene_support(source,obs)
    with pytest.raises(HarnessError,match='support_release_'):
        propose('release_supported',replace(scene(),support_measurement=measured))


def test_explicit_supported_move_uses_same_collision_contract():
    value=replace(scene(),base_eef=pose(.2,0,.4),base_object=pose(.2,0,.3),
        obstacles=(dict(type='halfspace',role='table',point=[0,0,.2],normal=[0,0,1]),),
        cartesian_request=dict(goals=[pose(.2,0,.3).tolist()],support_contact=True))
    plan=cartesian_proposal(value)
    assert plan['stages'][0]['support_contact_direction']=='approach'
    with pytest.raises(HarnessError,match='scene_sweep_blocked'):
        cartesian_proposal(replace(value,cartesian_request=dict(goals=[pose(.2,0,.3).tolist()])))
    with pytest.raises(HarnessError,match='support_motion_requires_attachment'):
        cartesian_proposal(replace(value,object_held=False,eef_object=None))
    with pytest.raises(ValueError,match='support_contact_requires_move_tcp'):
        MotionParameters(support_contact=True).for_skill('grasp')


def test_supported_release_resets_manipulation_phase_without_budget_or_placement_reset(tmp_path):
    from test_unpublished_plan import make_bridge
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        intent=engine.pending
        engine.pending=None;engine.attempted_motion.clear()
        engine.task=engine.task.model_copy(update=dict(allowed_skills=SKILLS))
        engine.completed_skills={'approach_grasp','grasp','verify_grasp'}
        intent=intent.model_copy(update=dict(skill='release_supported',task_hash=digest(engine.task)))
        engine._check_intent(intent)
        from f1_harness.contracts import Mode
        engine.mode=Mode.FAKE
        engine.running=intent;engine.attempted_motion={'approach_grasp','grasp','release_supported'}
        engine.motion_checkpoints={'old_transport':dict(skill='move_tcp')}
        engine.corrections=1;engine.regrasps=1
        engine.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
            outcome='success',reason='supported_release_observed',evidence_ids=('a'*64,),synthetic=True))
        assert engine.completed_skills=={'release_supported'}
        assert not engine.attempted_motion
        assert engine.corrections==engine.regrasps==1
        assert engine.active_grasp_selection is None
        assert engine.motion_checkpoints=={}
    finally:bridge.close()


def test_verify_service_uses_registered_geometry_and_independent_current_observation():
    from f1_harness.skill_services import VerifyService
    svc=service();verifier=VerifyService(svc.task,fixture=svc.fixture)
    intent=SimpleNamespace(skill='release_supported',request_id='r')
    assert verifier._verify_observation(intent,observed(held=False)).outcome=='success'
    assert verifier._verify_observation(intent,observed(held=True)).outcome=='pending_review'
    unknown=VerifyService(svc.task)
    assert unknown._verify_observation(intent,observed()).outcome=='pending_review'


@pytest.mark.parametrize('held,history,allowed',[(None,False,True),(None,True,False),(True,False,False),(False,True,True)])
def test_new_approach_never_opens_a_known_or_unresolved_previous_grasp(tmp_path,held,history,allowed):
    from test_unpublished_plan import make_bridge
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        intent=engine.pending;engine.pending=None
        engine.attempted_motion={'grasp'} if history else set()
        target=observed(held=held).objects[0].model_copy(update=dict(
            object_id=engine.task.object_id,eef_object_pose=None))
        engine.observation=obs.model_copy(update=dict(objects=(target,)))
        if allowed:engine._check_intent(intent)
        else:
            with pytest.raises(HarnessError,match='approach_requires_current_unheld_target'):
                engine._check_intent(intent)
    finally:bridge.close()
