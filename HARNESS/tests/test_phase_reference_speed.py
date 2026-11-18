"""Phase reference pace is explicit, while contact and controller limits stay separate."""
from copy import deepcopy
import numpy as np
import pytest
from f1_harness.trajectory import reference_speed,schedule,validate_schedule


def config():
    return dict(execution_mode='timed_reference',reference_velocity_deg_s=20.,
        reference_velocity_by_skill_deg_s={'transfer_above_hole':30.,'withdraw_reset':25.})


def test_phase_selection_does_not_speed_contact_or_approach():
    f=config();before=deepcopy(f)
    assert reference_speed(f,'transfer_above_hole')==30
    assert reference_speed(f,'withdraw_reset')==25
    for s in ('approach_grasp','grasp','align','insert','release','regrasp','retreat',None):
        assert reference_speed(f,s)==20
    assert f==before
    f.pop('reference_velocity_by_skill_deg_s')
    assert reference_speed(f,'transfer_above_hole')==20


@pytest.mark.parametrize('bad',[True,None,float('nan'),float('inf'),0,-1,'30'])
def test_invalid_override_cannot_be_accepted(bad):
    f=config();f['reference_velocity_by_skill_deg_s']['withdraw_reset']=bad
    with pytest.raises(ValueError,match='invalid_reference_velocity'):reference_speed(f,'grasp')


@pytest.mark.parametrize('skill',['grasp','unknown'])
def test_only_registered_skills_can_override(skill):
    f=config();f['reference_velocity_by_skill_deg_s'][skill]=30
    with pytest.raises(ValueError,match='unregistered_phase_reference_speed'):reference_speed(f,skill)


@pytest.mark.parametrize('skill',['approach_grasp','insert','recover_grasp'])
def test_precision_or_observation_can_slow_without_changing_other_skills_or_joint_targets(skill):
    f=config();f['reference_velocity_by_skill_deg_s'][skill]=10.
    q=np.zeros((3,16));q[:,7]=[1.,2.,3.];anchor=np.zeros(16)
    targets=q.copy();stages=[dict(end_index=3)]
    base=schedule(q,anchor,stages,reference_speed(f,'align'))
    fine=schedule(q,anchor,stages,reference_speed(f,skill))
    assert reference_speed(f,'grasp')==reference_speed(f,'align')==20.
    assert reference_speed(f,'transfer_above_hole')==30.
    assert np.array_equal(q,targets)
    assert fine['stops']==base['stops'] and fine['times_s'][-1]==2*base['times_s'][-1]


def test_override_is_not_silently_ignored_in_legacy_mode():
    f=config();f.pop('execution_mode')
    with pytest.raises(ValueError,match='requires_timed_reference'):reference_speed(f,'withdraw_reset')


def test_faster_reference_preserves_finger_and_semantic_barriers():
    q=np.zeros((5,16));q[:,7]=[1,3,3,4,5];q[2:,15]=100;anchor=np.zeros(16)
    trace=[dict(end_index=4),dict(end_index=5)]
    slow=schedule(q,anchor,trace,20.);fast=schedule(q,anchor,trace,30.)
    assert slow['stops']==fast['stops']==[1,2,3,4]
    assert fast['times_s'][-1]<slow['times_s'][-1] and validate_schedule(fast,q)


def test_revalidation_binds_phase_speed_and_reference_anchor():
    from test_plan_revalidation import observation,service
    from f1_harness.contracts import Task,SkillIntent,digest
    clock=[1_000_000_000]
    task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    serv=service(clock,task);serv.fixture.update(config())
    original=observation(clock[0]);intent=SkillIntent(request_id='r',session_id='s',owner_instance_id='owner',
        task_id=task.task_id,task_hash=digest(task),epoch=1,observation_id=original.observation_id,
        scene_version=original.scene_version,calibration_id=task.calibration_id,skill='approach_grasp')
    candidate=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        full22=[0.]*22,held16=original.state16))
    assert candidate['trajectory']['reference_velocity_deg_s']==20
    clock[0]+=1_000_000_000;after=clock[0];clock[0]+=100_000_000
    latest=observation(clock[0],name='new')
    args=dict(intent=intent.model_dump(),proposal=candidate,original_observation=original.model_dump(),
        observation=latest.model_dump(),full22=[0.]*22,held16=original.state16,after_ns=after)
    assert serv.dispatch('revalidate_plan',args)['geometry_checks']
    serv.fixture['reference_velocity_deg_s']=25
    with pytest.raises(RuntimeError,match='reference_schedule_mismatch'):serv.dispatch('revalidate_plan',args)
    serv.fixture['reference_velocity_deg_s']=20
    candidate['trajectory']['anchor'][0]=1
    with pytest.raises(RuntimeError,match='reference_schedule_mismatch'):serv.dispatch('revalidate_plan',args)
