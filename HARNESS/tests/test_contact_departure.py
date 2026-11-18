from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from f1_harness.contact_departure import target_departure_clear
from f1_harness.geometry import tool_sweep_clear


def scene():
    target=dict(role='target_object',object_id='block',min=[0,-.1,-.1],max=[.03,.1,.1])
    return SimpleNamespace(object_held=False,eef_object=None,target_object_id='block',
        tool_local_boxes=(dict(min=[-.01,-.01,-.01],max=[.01,.01,.01]),),
        obstacles=(target,),eef_tip_reference=np.eye(4),attached_radius_m=.01,
        tool_padding_policy='advisory',admission_profile='development_coarse',planning_margin_m=.015)


def poses():
    start=np.eye(4);start[0,3]=-.005
    end=start.copy();end[0,3]=-.04
    return [start,end]


def test_expected_contact_exit_is_not_ordinary_clear_sweep():
    value=scene();path=poses()
    assert not tool_sweep_clear(*path,value.eef_tip_reference,value.attached_radius_m,value.obstacles,
                               margin_m=0,local_boxes=value.tool_local_boxes)
    assert target_departure_clear(value,path)


def test_outward_proposal_uses_nearest_face_and_transforms_with_scene():
    from f1_harness.contact_departure import outward_contact_direction
    value=scene();value.base_eef=poses()[0];value.base_object=np.eye(4)
    before=value.base_eef.copy();direction=outward_contact_direction(value)
    assert np.allclose(direction,[-1,0,0])
    assert np.array_equal(value.base_eef,before) and value.object_held is False
    frame=np.eye(4);frame[:3,:3]=Rotation.from_euler('xyz',[.1,-.4,.3]).as_matrix()
    frame[:3,3]=[.2,.4,-.1]
    value.base_eef=frame@value.base_eef;value.base_object=frame@value.base_object
    value.obstacles[0]['pose']=frame
    assert np.allclose(outward_contact_direction(value),frame[:3,:3]@direction)


def test_conflicting_contact_faces_do_not_invent_an_exit_direction():
    from f1_harness.contact_departure import outward_contact_direction
    value=scene();value.base_eef=poses()[0];value.base_object=np.eye(4)
    value.tool_local_boxes+=(dict(min=[.03,-.01,-.01],max=[.05,.01,.01]),)
    assert outward_contact_direction(value) is None


@pytest.mark.parametrize('fault',['held','unknown','attached','through_target','deeper_then_exit',
    'rotation_into_target','neighbor','table','no_final_clearance','wrong_id','missing_target','extra_target'])
def test_departure_cannot_be_used_as_general_contact_or_obstacle_bypass(fault):
    value=scene();path=poses()
    if fault=='held':value.object_held=True
    if fault=='unknown':value.object_held=None
    if fault=='attached':value.eef_object=np.eye(4)
    if fault=='through_target':path[-1][0,3]=.06
    if fault=='deeper_then_exit':
        mid=path[0].copy();mid[0,3]=0.;path.insert(1,mid)
    if fault=='rotation_into_target':
        path[-1][:3,:3]=Rotation.from_euler('z',np.pi/2).as_matrix();path[-1][0,3]=-.02
    if fault=='neighbor':value.obstacles+=(dict(role='neighbor',min=[-.05,-.1,-.1],max=[-.02,.1,.1]),)
    if fault=='table':value.obstacles+=(dict(type='halfspace',role='table',normal=[0,0,1],point=[0,0,0]),)
    if fault=='no_final_clearance':path[-1][0,3]=-.009
    if fault=='wrong_id':value.target_object_id='other'
    if fault=='missing_target':value.obstacles=()
    if fault=='extra_target':value.obstacles=value.obstacles*2
    assert not target_departure_clear(value,path)


def test_exit_decision_is_frame_invariant_and_checks_each_tool_box():
    value=scene();path=poses()
    frame=np.eye(4);frame[:3,:3]=Rotation.from_euler('xyz',[.1,-.4,.3]).as_matrix();frame[:3,3]=[.2,.4,-.1]
    value.obstacles[0]['pose']=frame
    assert target_departure_clear(value,[frame@p for p in path])
    value=scene();value.tool_local_boxes+=(dict(min=[.02,-.01,-.01],max=[.04,.01,.01]),)
    # The second box is on the other side: exiting the first box's nearest
    # face would push the second deeper through the target.
    assert not target_departure_clear(value,path)


def test_regrasp_transit_tolerance_does_not_change_final_grasp_or_insert():
    from test_plan_revalidation import service
    from f1_harness.contracts import Task
    from f1_harness.revalidation import stage_tolerances,tolerances
    value=service([0],Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal')).fixture
    value['revalidation']['free_space_goal_tolerance_m']=.015
    for name in ('regrasp_open','regrasp_exit','regrasp_clearance'):
        assert stage_tolerances(value,'regrasp',name)['goal_position_m']==.015
    assert stage_tolerances(value,'regrasp',None)==tolerances(value,'regrasp')
    assert stage_tolerances(value,'insert','regrasp_exit')==tolerances(value,'insert')


def test_regrasp_opens_and_exits_before_changing_orientation_or_closing():
    from test_full_development_skills import scene as full_scene,pose
    from f1_harness.skills import propose
    value=replace(full_scene(),base_eef=pose(.2,0,.338),object_held=False,eef_object=None,
        tool_padding_policy='advisory',obstacles=(dict(role='target_object',object_id='block',
            min=[.185,-.015,.2],max=[.215,.015,.26]),))
    stages=propose('regrasp',value)['stages']
    assert [s.get('name') for s in stages[:3]]==['regrasp_open','regrasp_exit','regrasp_clearance']
    assert stages[0]['target_contact_hold'] and not stages[0]['goals']
    assert stages[0]['gripper']==value.release_opening
    assert stages[1]['target_contact_departure'] and stages[1]['gripper'] is None
    assert np.array_equal(np.array(stages[1]['goals'][0])[:3,:3],value.base_eef[:3,:3])
    assert not stages[2].get('allow_target_contact')
    assert stages[3]['gripper']==value.grasp_opening


def test_changed_departure_contract_is_rejected_even_with_clear_numerical_path(monkeypatch):
    # The semantic flag is reconstructed from the registered proposal; callers
    # cannot add a target exemption by editing serialized geometry_trace.
    from test_plan_revalidation import service,observation
    from f1_harness.contracts import Task,SkillIntent,digest,HarnessError
    from f1_harness.revalidation import revalidate_candidate
    clock=[1_000_000_000]
    task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    serv=service(clock,task);obs=observation(clock[0])
    intent=SkillIntent(request_id='r',session_id='s',owner_instance_id='o',task_id='t',task_hash=digest(task),
        epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,calibration_id='coarse',skill='grasp')
    held=list(obs.state16);full=[0.]*22
    plan=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=full,held16=held))
    plan['geometry_trace'][0]['target_contact_departure']=True
    clock[0]+=100_000_000;new=observation(clock[0],name='new')
    with pytest.raises(HarnessError,match='stage_contract_changed'):
        revalidate_candidate(serv,intent=intent.model_dump(),proposal=plan,original_observation=obs.model_dump(),
            observation=new.model_dump(),full22=full,held16=held,after_ns=obs.received_ns)


def test_grasp_lift_uses_transit_budget_while_closure_keeps_capture_budget():
    from test_plan_revalidation import service
    from f1_harness.contracts import Task
    from f1_harness.revalidation import stage_tolerances
    value=service([0],Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal')).fixture
    value['object_dimensions_m']=[.03,.03,.06]
    value['revalidation'].update(free_space_goal_tolerance_m=.015,grasp_position_tolerance_m=.008)
    for skill in ('grasp','regrasp'):
        assert stage_tolerances(value,skill,'grasp_lift')['goal_position_m']==.015
        assert stage_tolerances(value,skill,'grasp_close')['goal_position_m']==.008
    assert stage_tolerances(value,'insert','grasp_lift')['goal_position_m']<=.002
