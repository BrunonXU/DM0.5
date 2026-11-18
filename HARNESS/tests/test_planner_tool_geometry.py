"""Collision explanations preserve TCP frames and distinguish pose from path."""
from dataclasses import replace
from itertools import product
import numpy as np
import pytest
from f1_harness.adapters import _planning_feedback
from f1_harness.geometry_context import tool_geometry
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.skills import check_motion_route
from test_full_development_skills import pose, scene


def test_tool_boxes_in_tcp_reproduce_same_world_corners():
    tip=pose(.023,-.156,.092)
    tip[:3,:3]=[[0,-1,0],[1,0,0],[0,0,1]]
    box=dict(min=[-.04,-.19,-.03],max=[.06,-.05,.13],pose=pose(.01,.02,.03).tolist())
    value=replace(scene(),eef_tip_reference=tip,tool_local_boxes=(box,))
    context=tool_geometry(value)
    converted=context['local_boxes'][0]
    points=np.array([(*x,1.) for x in product(*zip(box['min'],box['max']))]).T
    expected=value.base_eef@np.array(box['pose'])@points
    actual=value.base_eef@tip@np.array(converted['pose']).reshape(4,4)@points
    assert np.allclose(expected,actual,rtol=0,atol=1e-12)
    assert context['frame']=='tcp' and context['fallback_radius_m'] is None
    assert box['pose']==pose(.01,.02,.03).tolist()


@pytest.mark.parametrize('x,location,start_clear,end_clear',[
    (0.,'start_pose',False,True),(.4,'end_pose',True,False),(.2,'sweep_envelope',True,True)])
def test_failure_survives_owner_and_model_projection(x,location,start_clear,end_clear):
    obstacle=dict(min=[x-.01,-.02,.48],max=[x+.01,.02,.52],role='bucket',name='wall')
    tip=pose(.01,.02,.03)
    native=pose(.2,-.1,.3)
    native[:3,:3]=[[0,-1,0],[1,0,0],[0,0,1]]
    value=replace(scene(),base_eef=pose(0,0,.5),eef_tip_reference=tip,
        diagnostic_native_from_world=native,
        tool_local_boxes=(dict(min=[-.005]*3,max=[.005]*3),),
        obstacles=(obstacle,),object_held=False,eef_object=None)
    with pytest.raises(RuntimeError) as caught:
        check_motion_route(value,[pose(.4,0,.5)],skill='approach_grasp')
    metrics=_planning_feedback(numerical_failure_detail(caught.value))['metrics']
    assert metrics['collision_location']==location
    assert metrics['start_pose_clear'] is start_clear
    assert metrics['end_pose_clear'] is end_clear
    assert len(metrics['segment_start_tcp_pose'])==len(metrics['segment_end_tcp_pose'])==16
    assert np.allclose(np.array(metrics['segment_start_tcp_pose']).reshape(4,4),native@value.base_eef@tip)
    assert np.allclose(np.array(metrics['segment_end_tcp_pose']).reshape(4,4),native@pose(.4,0,.5)@tip)


def test_multiple_verbose_rejections_keep_obstacle_identity_and_endpoint_pose():
    import json
    from f1_harness.contracts import HarnessError
    metrics=dict(skill='align',collision_location='end_pose',obstacle_role='bucket',
        obstacle_name='wall_x_low',start_pose_clear=True,end_pose_clear=False,
        segment_start_tcp_pose=pose(.1,.2,.3).ravel().tolist(),
        segment_end_tcp_pose=pose(.4,.5,.6).ravel().tolist(),
        limits=[[float(i)/1234567 for i in range(22)]]*22,route_family='alignment')
    raw=dict(diagnostic=dict(code='registered_candidates_exhausted',metrics=metrics),
        candidates=[dict(variant=i,diagnostic=dict(code='scene_sweep_blocked',metrics=metrics)) for i in range(4)])
    value=numerical_failure_detail(HarnessError('registered_candidates_exhausted:'+json.dumps(raw)))
    feedback=_planning_feedback(value)
    for result in (value,feedback):
        assert len(json.dumps(result,ensure_ascii=False))<4096
        assert result['detail_truncated']
        assert result['metrics']['obstacle_role']=='bucket'
        assert result['metrics']['obstacle_name']=='wall_x_low'
        assert result['metrics']['collision_location']=='end_pose'
        assert result['metrics']['end_pose_clear'] is False
        assert result['metrics']['segment_end_tcp_pose']==metrics['segment_end_tcp_pose']
