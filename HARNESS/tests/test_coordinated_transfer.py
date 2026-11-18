"""Loaded lift coordination keeps object geometry and the original checks."""
from dataclasses import replace
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from test_full_development_skills import scene,pose
from f1_harness.skills import propose,coordinated_lift,validate_transfer_lift_fraction


def loaded_scene():
    s=scene();held=s.eef_object.copy()
    held[:3,:3]=Rotation.from_euler('x',35,degrees=True).as_matrix()
    held[:3,3]=[.04,.02,-.1]
    return replace(s,eef_object=held,base_object=s.base_eef@held)


def goals(s):return np.array(propose('transfer_above_hole',s)['stages'][0]['goals'])


def test_coordinated_lift_keeps_object_position_and_final_hole_goal():
    s=loaded_scene();old=goals(s);changed=goals(replace(s,transfer_lift_rotation_fraction=.33))
    assert np.allclose((old[0]@s.eef_object)[:3,3],(changed[0]@s.eef_object)[:3,3])
    assert not np.allclose(old[0][:3,:3],changed[0][:3,:3])
    assert not np.allclose(old[0][:3,3],changed[0][:3,3])
    assert np.allclose(old[-1],changed[-1])
    for skill in ('align','insert','release'):
        assert propose(skill,s)==propose(skill,replace(s,transfer_lift_rotation_fraction=.33))


def test_default_lift_is_exactly_unchanged():
    s=loaded_scene();lift=pose(.1,.2,.7);end=pose(.2,.3,.6)
    assert coordinated_lift(s,lift,end) is lift
    assert np.array_equal(goals(s),goals(replace(s,transfer_lift_rotation_fraction=0.)))


@pytest.mark.parametrize('bad',[True,False,None,'0.33',float('nan'),float('inf'),-.33,.67])
def test_only_explicit_registered_fraction_is_accepted(bad):
    with pytest.raises(ValueError,match='unregistered_transfer_lift'):
        validate_transfer_lift_fraction(bad)


def test_unrelated_staging_rotation_cannot_silently_override_lift():
    s=replace(loaded_scene(),transfer_staging_pose=pose(.1,.2,.7),transfer_lift_rotation_fraction=.33)
    with pytest.raises(ValueError,match='incompatible_with_staging'):
        propose('transfer_above_hole',s)


def test_coordinated_lift_remains_subject_to_tool_and_object_sweeps():
    s=replace(loaded_scene(),transfer_lift_rotation_fraction=.33)
    # The start is occupied; changing the rotation recipe must not bypass this.
    obstacle=dict(min=[-.2,-.2,.2],max=[.3,.3,.7])
    with pytest.raises(RuntimeError,match='sweep_blocked|no_clear_square'):
        propose('transfer_above_hole',replace(s,obstacles=(obstacle,)))


def test_service_and_revalidator_bind_the_coordinated_lift_recipe():
    from test_plan_revalidation import observation,service
    from f1_harness.contracts import Task,SkillIntent,digest
    clock=[1_000_000_000]
    task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    serv=service(clock,task);serv.fixture['transfer_lift_rotation_fraction']=.33
    s=loaded_scene();original=observation(clock[0])
    obj=original.objects[0].model_copy(update=dict(held=True,
        pose=tuple(s.base_object.ravel()),eef_object_pose=tuple(s.eef_object.ravel())))
    original=original.model_copy(update={'objects':(obj,original.objects[1])})
    intent=SkillIntent(request_id='r',session_id='s',owner_instance_id='owner',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=original.observation_id,
        scene_version=original.scene_version,calibration_id=task.calibration_id,skill='transfer_above_hole')
    candidate=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        full22=[0.]*22,held16=original.state16))
    clock[0]+=1_000_000_000;after=clock[0];clock[0]+=100_000_000
    latest=observation(clock[0],name='new').model_copy(update={'objects':original.objects})
    args=dict(intent=intent.model_dump(),proposal=candidate,original_observation=original.model_dump(),
        observation=latest.model_dump(),full22=[0.]*22,held16=original.state16,after_ns=after)
    assert serv.dispatch('revalidate_plan',args)['geometry_checks']['stages']==1
    serv.fixture['transfer_lift_rotation_fraction']=0.
    with pytest.raises(RuntimeError,match='stage_binding_mismatch'):
        serv.dispatch('revalidate_plan',args)
