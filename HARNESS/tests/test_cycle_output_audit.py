"""Audit failure injection; motion references alone must not count as output proof."""
import sys
from pathlib import Path
from copy import deepcopy
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ordinary_output_sweeps import stationary_release,readonly_outputs


def rows():
    closed=[0.]*15+[100.]
    opened=[0.]*15+[18.]
    return [dict(request_id='release',virtual_ns=1,submitted16=closed,measured16=closed.copy(),reference16=closed.copy(),new_motion_reference=True),
            dict(request_id='release',virtual_ns=2,submitted16=opened,measured16=closed.copy(),reference16=opened.copy(),new_motion_reference=True)]


def test_release_command_only_changes_gripper_once():
    assert all(stationary_release(rows(),18,100).values())


@pytest.mark.parametrize('fault',['actual_arm','reference_arm','not_open','reclosed'])
def test_release_rejects_motion_and_unfinished_opening(fault):
    data=rows()
    if fault=='actual_arm':data[-1]['submitted16'][7]=.01
    if fault=='reference_arm':data[-1]['reference16'][7]=.01
    if fault=='not_open':data[-1]['submitted16'][15]=100.
    if fault=='reclosed':
        last=deepcopy(data[-1]);last['submitted16'][15]=100.;data.append(last)
    with pytest.raises(ValueError):stationary_release(data,18,100)


@pytest.mark.parametrize('fault',['none','actual_movement','motion_reference','missing'])
def test_readonly_cannot_cover_movement_or_missing_evidence(fault):
    q=[0.]*15+[100.]
    data=[dict(request_id='verify',submitted16=q.copy(),measured16=q.copy(),new_motion_reference=False)]
    stages=[dict(skill='verify_grasp',request=dict(request_id='verify'))]
    if fault=='actual_movement':data[0]['submitted16'][7]=.01
    if fault=='motion_reference':data[0]['new_motion_reference']=True
    if fault=='missing':
        with pytest.raises(ValueError,match='output_missing'):readonly_outputs([],stages)
    else:assert readonly_outputs(data,stages)[0]['passed'] is (fault=='none')


@pytest.mark.parametrize('fault',['none','motion_reference','actual_movement','unknown_publication'])
def test_rejected_transfer_hold_needs_terminal_zero_motion_and_unchanged_output(fault):
    q=[0.]*15+[100.]
    data=[dict(request_id='rejected',submitted16=q.copy(),measured16=q.copy(),new_motion_reference=False)]
    stages=[dict(skill='transfer_above_hole',request=dict(request_id='rejected'),
        result=dict(lifecycle='completed',outcome='pending_review',no_motion=True))]
    if fault=='motion_reference':data[0]['new_motion_reference']=True
    if fault=='actual_movement':data[0]['submitted16'][7]=.01
    if fault=='unknown_publication':
        stages[0]['result']['no_motion']=False
        assert readonly_outputs(data,stages)==[]
    else:
        result=readonly_outputs(data,stages)[0]
        assert result['declared_zero_motion_rejection']
        assert result['passed'] is (fault=='none')


@pytest.mark.parametrize('endpoint_complete',[False,True])
def test_local_output_audit_cannot_accept_a_clear_prefix_with_unrestored_endpoint(monkeypatch,endpoint_complete):
    from types import SimpleNamespace
    import numpy as np
    import ordinary_output_sweeps as audit
    from f1_harness.contracts import HarnessError,digest
    from test_local_environment_motion import partial_setup
    service,obs,intent,_=partial_setup()
    plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    request=intent.request_id;q=list(obs.state16);q[7]=.1
    outputs=[dict(request_id=request,virtual_ns=10,submitted16=q,measured16=list(obs.state16))]
    # Later camera-time feedback is not the frozen controller reference.
    camera_state=list(obs.state16);camera_state[7]=.02
    latest=obs.model_copy(update={'state16':tuple(camera_state)})
    events=[dict(kind='harness_planning_started',request_id=request,full_reference=[0.]*22,
        held_reference=list(obs.state16),intent=intent.model_dump()),
        dict(kind='harness_plan_revalidated',proof=dict(request_id=request,candidate_sha256=digest(plan),
            observation=obs.model_dump())),
        dict(kind='harness_plan_revalidated',proof=dict(request_id=request,candidate_sha256=digest(plan),
            observation=latest.model_dump())),
        dict(kind='harness_skill_first_publication',request_id=request)]
    original_scene=service._scene;references=[];observed=[]
    def scene_from_reference(observation,full,reference,**kwargs):
        references.append(list(reference))
        observed.append(observation.state16)
        return original_scene(observation,full,reference,**kwargs)
    service._scene=scene_from_reference
    checked=[]
    def endpoint(scene,pose):
        checked.append(pose.copy())
        if not endpoint_complete:raise HarnessError('local_observation_departure_incomplete')
    monkeypatch.setattr('f1_harness.local_environment.require_departure_endpoint',endpoint)
    monkeypatch.setattr(audit,'_motion_clear',lambda *a,**kw:True)
    monkeypatch.setattr(audit,'local_endpoint_padding_status',lambda *a:dict(
        diagnostic_only=True,target_plane_padding_restored=False))
    pose=np.eye(4);pose[2,3]=.412
    numeric=SimpleNamespace(dispatch=lambda *a,**kw:pose.tolist(),numerics=SimpleNamespace(
        mapping=SimpleNamespace(check_action=lambda *a:None),gate=SimpleNamespace(check_preventive=lambda *a:None)))
    result=audit.sweep_ordinary(outputs,events,[dict(skill='move_tcp',request_id=request,proposal=plan)],service,numeric)[0]
    assert len(checked)==1 and np.array_equal(checked[0],pose)
    assert references==[list(obs.state16)] and references[0]!=camera_state
    assert observed==[latest.state16]
    assert result['passed'] is endpoint_complete
    assert result['checks']['local_endpoint_contract_satisfied'] is endpoint_complete
    assert result['local_padding_diagnostic']['target_plane_padding_restored'] is False
    if not endpoint_complete:
        assert result['failures']==[dict(kind='local_departure_endpoint',reason='local_observation_departure_incomplete',virtual_ns=10)]


@pytest.mark.parametrize('height,restored',[(.01,False),(.02,True)])
def test_advisory_endpoint_padding_is_measured_independently_of_policy(height,restored):
    from types import SimpleNamespace as NS
    import numpy as np
    from ordinary_output_sweeps import local_endpoint_padding_status
    scene=NS(local_collision_evidence=NS(object_id='target',surface_anchor=NS(supporting_plane=NS(
        normal_world=(0,0,1),centre_world=(0,0,0),all_target_front_side_max_m=.001))),
        diagnostic_native_from_world=np.eye(4),eef_tip_reference=np.eye(4),attached_radius_m=.02,
        tool_local_boxes=({'min':[-.01,-.01,0.],'max':[.01,.01,.02]},),
        planning_margin_m=.015,tool_padding_policy='advisory',admission_profile='development_coarse')
    pose=np.eye(4);pose[2,3]=height
    result=local_endpoint_padding_status(scene,pose)
    assert result['target_plane_padding_restored'] is restored
    assert result['diagnostic_only'] and result['configured_tool_padding_m']==.015
    assert result['effective_tool_margin_m']==0.
