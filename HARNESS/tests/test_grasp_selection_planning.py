"""Registered grasp choice crosses planning, feedback and scene revalidation."""
from dataclasses import replace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.contracts import HarnessError, SkillIntent, Task, digest
from f1_harness.grasp_selection import selected_scene, recorded_scene
from f1_harness.owner_bridge import JointProposal
from f1_harness.skills import propose
from test_plan_revalidation import observation, service


def setup():
    clock=[1_000_000_000]
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',
              admission_profile='development_coarse')
    obs=observation(clock[0])
    plan=service(clock,task)
    plan.fixture['grasp_pitch_offsets_deg']=[30.]
    intent=SkillIntent(request_id='request',session_id='session',owner_instance_id='owner',
        task_id=task.task_id,task_hash=digest(task),epoch=1,observation_id=obs.observation_id,
        scene_version=obs.scene_version,calibration_id=task.calibration_id,skill='approach_grasp')
    return plan,obs,intent,clock


def selection_for(plan,obs,intent,variant=3):
    return dict(schema='f1-owner-grasp-selection-v1',owner_instance_id=intent.owner_instance_id,
        session_id=intent.session_id,task_id=intent.task_id,task_hash=intent.task_hash,
        epoch=intent.epoch,calibration_id=intent.calibration_id,object_id=plan.task.object_id,
        candidate_variant=variant,descriptor=dict(strategy='tcp_pitch',pitch_offset_deg=30.),
        reference_object_frame_id=obs.objects[0].frame_id,
        reference_object_pose=list(obs.objects[0].pose))


def test_nominal_candidates_are_tried_before_pitch_alternatives(monkeypatch):
    plan,obs,intent,_=setup()
    plan.fixture['candidate_order']={'approach_grasp':[3,0,1,2]}
    tried=[]
    def solve(intent,scene,held,full,variant,*,deadline_ns=None): 
        pitch=scene.grasp_candidate_descriptor['pitch_offset_deg']
        tried.append((pitch,variant))
        if pitch==0:raise HarnessError('scene_sweep_blocked')
        assert scene.grasp_candidate_variant==variant
        return {'geometry_trace':[{}]}
    monkeypatch.setattr(plan,'_solve_candidate',solve)
    result=plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    assert tried==[(0.,3),(0.,0),(0.,1),(0.,2),(30.,3)]


def test_grasp_keeps_successful_approach_choice_across_square_coordinate_change():
    plan,obs,intent,_=setup()
    intent=intent.model_copy(update={'skill':'grasp'})
    context=selection_for(plan,obs,intent)
    original=plan._scene(obs,[0.]*22,obs.state16)
    bound=selected_scene(plan,original,obs,context,intent)
    changed=np.array(obs.objects[0].pose).reshape(4,4).copy()
    changed[:3,:3]=changed[:3,:3]@Rotation.from_euler('z',90,degrees=True).as_matrix()
    changed[0,3]+=.001
    newer=obs.model_copy(update={'objects':(obs.objects[0].model_copy(update={
        'pose':tuple(changed.ravel())}),obs.objects[1])})
    current=selected_scene(plan,plan._scene(newer,[0.]*22,newer.state16),newer,context,intent)
    assert current.grasp_candidate_variant==3
    assert current.grasp_candidate_descriptor==bound.grasp_candidate_descriptor
    before=np.array(propose('grasp',bound)['stages'][0]['goals'][-1])
    after=np.array(propose('grasp',current)['stages'][0]['goals'][-1])
    assert np.allclose(before[:3,:3],after[:3,:3])
    assert np.allclose(after[:3,3]-before[:3,3],[.001,0,0])


@pytest.mark.parametrize('field,value', [('epoch',2),('task_hash','other'),
    ('object_id','other'),('reference_object_frame_id','other'),('candidate_variant',4)])
def test_selection_cannot_cross_owner_task_or_frame(field,value):
    plan,obs,intent,_=setup()
    intent=intent.model_copy(update={'skill':'grasp'})
    context=selection_for(plan,obs,intent);context[field]=value
    with pytest.raises(HarnessError,match='grasp_selection'):
        selected_scene(plan,plan._scene(obs,[0.]*22,obs.state16),obs,context,intent)


def test_grasp_without_completed_approach_does_not_silently_return_to_nominal_pitch():
    plan,obs,intent,_=setup();intent=intent.model_copy(update={'skill':'grasp'})
    with pytest.raises(HarnessError,match='completed_approach_grasp_selection_required'):
        plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            full22=[0.]*22,held16=list(obs.state16)))


def test_selected_pitch_is_bound_to_the_actual_plan_and_fresh_revalidation(monkeypatch):
    plan,obs,intent,clock=setup()
    solve=plan._solve_candidate
    def fail_nominal(intent,scene,held,full,variant,*,deadline_ns=None): 
        if scene.grasp_candidate_descriptor['pitch_offset_deg']==0:
            raise HarnessError('scene_sweep_blocked')
        return solve(intent,scene,held,full,variant)
    monkeypatch.setattr(plan,'_solve_candidate',fail_nominal)
    candidate=plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    entry=candidate['geometry_trace'][0]
    assert entry['grasp_candidate_descriptor']['pitch_offset_deg']==30.
    clock[0]+=100_000_000
    latest=observation(clock[0],name='fresh')
    proof=plan.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=candidate,
        original_observation=obs.model_dump(),observation=latest.model_dump(),
        full22=[0.]*22,held16=list(obs.state16),after_ns=clock[0]-1))
    assert proof['observation_sha256']==digest(latest)
    restored=recorded_scene(plan,plan._scene(latest,[0.]*22,latest.state16),latest,
                           JointProposal(**candidate),intent)
    assert restored.grasp_candidate_descriptor==entry['grasp_candidate_descriptor']


def test_measured_transport_posture_uses_a_per_goal_nullspace_reference(monkeypatch):
    # The single posture target takes a different native solver branch. The
    # saved-scene output proof used per-goal soft references, so keep that ABI.
    plan,obs,intent,_=setup();intent=intent.model_copy(update={'skill':'transfer_above_hole'})
    scene=plan._scene(obs,[0.]*22,obs.state16)
    goals=[scene.base_eef.tolist(),scene.base_eef.tolist()]
    monkeypatch.setattr('f1_harness.skill_services.propose',lambda *a,**kw:dict(kind='motion',stages=[
        dict(goals=goals,gripper=None,posture_reference_policy='measured_stage_start',
             orientation_prior_source='example-transit')]))
    previous=plan.numerics.call;solves=[]
    def record(method,**kwargs):
        if method=='solve_path':solves.append(kwargs)
        return previous(method,**kwargs)
    plan.numerics.call=record
    held=list(obs.state16);held[7:14]=[1.,2.,3.,4.,5.,6.,7.]
    result=plan._solve_candidate(intent,scene,held,[0.]*22,2)
    assert solves[0]['scene']['posture_references_deg']==[held[7:14],held[7:14]]
    assert 'posture_reference_deg' not in solves[0]['scene']
    assert result['geometry_trace'][0]['posture_reference_policy']=='measured_stage_start'
    assert result['geometry_trace'][0]['posture_prior_source'] is None

