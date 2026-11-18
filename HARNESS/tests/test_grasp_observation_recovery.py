from rejection_assertions import assert_rejected
"""Unknown attachment gets one measured observation, never assumed success."""
import ast
from dataclasses import replace

import numpy as np
import pytest

from f1_harness.contracts import HarnessError, SkillIntent, Task, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.astra_programs import install_recovery_helpers
from f1_harness.skills import propose
from f1_harness.grasp_observation import check_actual_path
from test_full_development_skills import pose, scene
from test_astra_programs import setup_cells, cell
from test_pending_grasp_evidence import verifier, arriving


def unknown_scene():
    # Fingers enclose the target sides; lifting exits along its upper surface.
    target=dict(role='target_object',object_id='block',min=[-.015,-.015,0],max=[.015,.015,.06],pose=pose(.2,0,.2).tolist())
    fingers=({'min':[-.035,-.03,-.1],'max':[-.017,.03,-.03]},
             {'min':[.017,-.03,-.1],'max':[.035,.03,-.03]})
    return replace(scene(),base_eef=pose(.2,0,.3),object_held=None,eef_object=None,
                   tool_local_boxes=fingers,obstacles=(target,))


def test_observation_preserves_orientation_and_uses_two_collision_hypotheses():
    value=unknown_scene();result=propose('recover_grasp',value);stage=result['stages'][0]
    end=np.array(stage['goals'][0]);attached=np.array(stage['eef_object'])
    assert np.allclose(end[:3,:3],value.base_eef[:3,:3])
    assert np.allclose(end[:3,3]-value.base_eef[:3,3],[0,0,value.approach_clearance_m])
    assert np.allclose(value.base_eef@attached,value.base_object)
    assert value.object_held is None and value.eef_object is None
    assert stage['gripper'] is None and stage['uncertain_grasp_observation']
    q=[0.]*15+[100.]
    check_actual_path(value,[value.base_eef,end],[q],q,dict(grasp_opening=100,gripper_tolerance=.5))
    with pytest.raises(HarnessError,match='actual_IK'):
        check_actual_path(value,[value.base_eef,end],[[0.]*15+[18.]],q,dict(grasp_opening=100,gripper_tolerance=.5))


def test_uncertain_observation_cannot_cross_real_obstacles_or_known_attachment():
    value=unknown_scene()
    ceiling=dict(role='bucket',min=[.16,-.05,.34],max=[.24,.05,.4])
    with pytest.raises(HarnessError,match='scene_sweep'):
        propose('recover_grasp',replace(value,obstacles=(*value.obstacles,ceiling)))
    for held in (True,False):
        with pytest.raises(HarnessError,match='unknown_attachment'):
            propose('recover_grasp',replace(value,object_held=held))


def test_gripper_change_is_not_a_retryable_departure_geometry_failure():
    from f1_harness.skill_services import CANDIDATE_FAILURES
    value=unknown_scene();q=[0.]*15+[100.]
    with pytest.raises(HarnessError) as caught:
        check_actual_path(value,[value.base_eef,value.base_eef],[[0.]*15+[18.]],q,
                          dict(grasp_opening=100,gripper_tolerance=.5))
    assert str(caught.value)=='actual_IK_grasp_observation_gripper_changed'
    assert str(caught.value) not in CANDIDATE_FAILURES


@pytest.mark.parametrize('held,outcome',[(True,'success'),(False,'failure'),(None,'pending_review')])
def test_completed_observation_motion_is_not_grasp_success(monkeypatch,held,outcome):
    service,intent,obs,clock=verifier(monkeypatch,skill='recover_grasp')
    service.task=service.task.model_copy(update=dict(allowed_skills=(*service.task.allowed_skills,'recover_grasp')))
    intent=intent.model_copy(update=dict(task_hash=digest(service.task)))
    service.latest=obs(200,held);arriving(service,clock,[])
    result=service.dispatch('verify_skill',dict(intent=intent.model_dump(),after_ns=100))
    assert result['outcome']==outcome
    assert result['reason']==('grasp_held_evidence_missing' if held is None else 'grasp_observation')


@pytest.mark.parametrize('prefix',['','post_motion_verification_unavailable:'])
@pytest.mark.parametrize('recovery,transport,regrasp',[
    (dict(outcome='success'),True,False),
    (dict(outcome='pending_review',reason='grasp_held_evidence_missing'),False,False),
    (dict(outcome='failure',reason='grasp_observation'),True,True),
    (dict(outcome='failure',reason='grasp_not_attached',cancel_stage='measured_stationary'),True,True),
    (dict(outcome='failure',reason='grasp_not_attached',cancel_stage='cancel_requested'),False,False),
])
def test_one_pyrua_cell_recovers_then_transports_only_with_proof(tmp_path,recovery,transport,regrasp,prefix):
    replies=[dict(outcome='success')]*2+[dict(outcome='pending_review',reason=prefix+'grasp_held_evidence_missing',verification_request_id='verify-checkpoint'),recovery]+[dict(outcome='success')]*10
    backend,arm,_=setup_cells(tmp_path,replies)
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'recovery'),regrasp_limit=1,grasp_observation_enabled=True)
    result=ast.literal_eval(cell(arm,'pick_and_place()')['value'])
    names=[v['name'] for v in backend.motions]
    assert names[:4]==['approach_grasp','grasp','verify_grasp','recover_grasp']
    assert names.count('recover_grasp')==1
    assert ('transfer_above_hole' in names)==transport
    assert ('regrasp' in names)==regrasp
    if transport:
        assert names[names.index('transfer_above_hole')-1]=='verify_grasp'
        assert result['outcome']=='success'
    else:assert result['program_stage']=='recover_grasp'


def test_plan_revalidation_retains_uncertain_motion_and_closed_gripper():
    from test_plan_revalidation import service, observation
    clock=[1_000_000_000];task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',
        admission_profile='development_coarse',allowed_skills=('recover_grasp',))
    serv=service(clock,task);value=unknown_scene()
    from dataclasses import asdict
    serv.fixture.update(asdict(value));serv.fixture["effective_clearance_m"]=.003
    # Numerics below is a test FK/IK; native numeric replay is separate.
    old_call=serv.numerics.call
    def call(method,**kw):
        if method=='forward_pose':return value.base_eef.tolist()
        return old_call(method,**kw)
    serv.numerics.call=call
    obs=observation(clock[0]);obj=obs.objects[0].model_copy(update=dict(held=None,pose=tuple(value.base_object.ravel())))
    obs=obs.model_copy(update=dict(objects=(obj,obs.objects[1]),state16=(0.,)*15+(100.,)))
    intent=SkillIntent(request_id='recover',session_id='session',owner_instance_id='owner',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id=task.calibration_id,skill='recover_grasp')
    args=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16))
    candidate=serv.dispatch('plan_skill',args)
    assert candidate['geometry_trace'][0]['uncertain_grasp_observation']
    clock[0]+=100_000_000;after=clock[0];clock[0]+=100_000_000
    latest=observation(clock[0],name='new').model_copy(update=dict(objects=obs.objects,state16=obs.state16))
    proof_args=dict(args,proposal=candidate,original_observation=obs.model_dump(),observation=latest.model_dump(),after_ns=after)
    proof=serv.dispatch('revalidate_plan',proof_args)
    assert proof['geometry_checks']['swept_segments']>0
    candidate['geometry_trace'][0]['uncertain_grasp_observation']=False
    with pytest.raises(HarnessError,match='stage_contract_changed'):
        serv.dispatch('revalidate_plan',proof_args)


def test_owner_allows_one_recovery_after_unknown_verification_not_generic_motion(tmp_path):
    from f1_harness.contracts import SceneObject,SkillResult
    from test_unpublished_plan import make_bridge
    engine,bridge,clock,_,_,obs=make_bridge(tmp_path)
    try:
        engine.pending=None;engine.results.clear()
        engine.task=engine.task.model_copy(update={'allowed_skills':(*engine.task.allowed_skills,'recover_grasp')})
        target=SceneObject(object_id='block',shape='square',held=None,pose=tuple(pose().ravel()),
            frame_id='native',visible=True,evidence_ids=('measured-image',))
        engine.update_observation(obs.model_copy(update=dict(objects=(target,),state16=(0.,)*15+(100.,))))
        engine.completed_skills.update(('approach_grasp','grasp'))
        engine.attempted_motion.update(('approach_grasp','grasp'))
        intent=SkillIntent(request_id='probe',session_id=engine.session,owner_instance_id=engine.instance,
            task_id=engine.task.task_id,task_hash=digest(engine.task),epoch=engine.epoch,
            observation_id=obs.observation_id,scene_version=obs.scene_version,
            calibration_id=engine.task.calibration_id,skill='recover_grasp')
        assert_rejected(engine.submit_skill(intent.model_dump()), 'unresolved_grasp_verification')
        engine.attempted_motion.add('verify_grasp')
        intent=intent.model_copy(update={'request_id':'probe-after-verification'})
        engine.submit_skill(intent.model_dump());assert engine.take_pending().skill=='recover_grasp'
        engine.record_skill_publication(intent.request_id,publication_id='probe-output',now_ns=clock[0])
        engine.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
            outcome='pending_review',reason='grasp_held_evidence_missing',evidence_ids=('actual',)))
        assert 'verify_grasp' not in engine.completed_skills
        assert_rejected(engine.submit_skill(intent.model_copy(update={'request_id':'repeat'}).model_dump()), 'motion_already_attempted')
        assert_rejected(engine.submit_skill(intent.model_copy(update={'request_id':'transport','skill':'transfer_above_hole'}).model_dump()), 'skill_precondition:verify_grasp')
    finally:bridge.close()


def test_observation_after_recovery_can_verify_object_only_without_authorizing_motion():
    from f1_harness.observation import require_geometry_identity
    from test_plan_revalidation import observation
    obs=observation(1_000_000_000)
    obs=obs.model_copy(update=dict(geometry_scope='coarse_object_verification',objects=obs.objects[:1]))
    require_geometry_identity(obs,goal='placed',admission_profile='development_coarse',
        skill='recover_grasp',verification=True)
    with pytest.raises(ValueError,match='coarse_geometry'):
        require_geometry_identity(obs,goal='placed',admission_profile='development_coarse',skill='recover_grasp')


def test_empty_tool_clearance_cannot_hide_collision_of_potentially_held_block():
    from f1_harness.contact_departure import contact_exit_geometry_clear
    value=unknown_scene()
    obstacle=dict(role='bucket',min=[.19,-.01,.30],max=[.21,.01,.32])
    value=replace(value,obstacles=(*value.obstacles,obstacle),tool_padding_policy='advisory')
    goal=value.base_eef.copy();goal[2,3]+=value.approach_clearance_m
    assert contact_exit_geometry_clear(value,[value.base_eef,goal])
    with pytest.raises(HarnessError,match='scene_sweep'):
        propose('recover_grasp',value)
