"""An approximate IK fit is a numeric explanation, never a replacement action."""
from dataclasses import replace
import json

import numpy as np
import pytest

from f1_harness.adapters import _planning_feedback
from f1_harness.contracts import HarnessError
from f1_harness.frames import FrameChain
from f1_harness.owner_bridge import numerical_failure_detail
from test_plan_revalidation import scenario


def failed_search(target, fitted):
    return dict(search_phase='endpoint_ik', frame='ik_world', goal=target.tolist(),
        best_fit_eef_pose=fitted.ravel().tolist(), position_error_reference='eef',
        position_error_m=.0511076, rotation_error_rad=.00002,
        position_tolerance_m=.0005, rotation_tolerance_rad=.005,
        ik_seed_count=9, ik_pose_fit_count=0, native_endpoint_checks=0,
        native_valid_goal_count=0, ompl_started=False, best_fit_native_checked=False,
        best_fit_native_valid=None, best_fit_executable=False, motion_authorized=False,
        path_checked=False, grasp_checked=False, search_exhaustive=False,
        recovery_hint='change_goal_or_approach_and_preview', arbitrary_secret='must not pass')


def test_stage_failure_converts_ik_eef_to_native_tcp_before_model_feedback(scenario):
    service, args, _, original, _, intent = scenario
    world_native=np.array([[0.,-1.,0.,.4],[1.,0.,0.,-.2],[0.,0.,1.,.1],[0.,0.,0.,1.]])
    tip=np.eye(4);tip[:3,3]=[.02,.03,.15]
    service.frames=FrameChain(world_native,np.eye(4),'robot_base','ik_world')
    geometry=service._scene(original,args['full22'],args['held16'])
    geometry=replace(geometry,eef_tip_reference=tip,diagnostic_native_from_world=np.linalg.inv(world_native))
    target=np.eye(4);target[:3,3]=[.7,-.1,.9]
    fitted=target.copy();fitted[0,3]-=.05
    angle=.001;fitted[:3,:3]=[[np.cos(angle),-np.sin(angle),0.],[np.sin(angle),np.cos(angle),0.],[0.,0.,1.]]
    diagnostic=failed_search(target,fitted)
    def fail(method,**kw):
        raise ValueError('numerical_joint_search_no_valid_goal:'+json.dumps(diagnostic))
    service.numerics.call=fail
    # Reuse the normal service exception path without requiring a connection
    # to the fake failure's target. This test exercises only coordinate/report contracts.
    from unittest.mock import patch
    with patch('f1_harness.skill_services.propose',return_value=dict(kind='motion',stages=[dict(
            goals=[target.tolist()],gripper=None,route_family='joint_space_approach')])):
        with pytest.raises(HarnessError) as error:
            service._solve_candidate(intent,geometry,args['held16'],args['full22'],0)
    parsed=numerical_failure_detail(error.value)
    feedback=_planning_feedback(parsed)
    for item in (parsed,feedback):
        metrics=item['metrics']
        assert item['code']=='numerical_joint_search_no_valid_goal'
        assert metrics['search_phase']=='endpoint_ik' and metrics['native_endpoint_checks']==0
        assert metrics['ompl_started'] is False and metrics['best_fit_executable'] is False
        assert metrics['motion_authorized'] is False and metrics['path_checked'] is False
        assert metrics['frame']=='robot_base' and metrics['position_error_reference']=='eef'
        expected_target=np.linalg.inv(world_native)@target@tip
        expected_fit=np.linalg.inv(world_native)@fitted@tip
        np.testing.assert_allclose(np.asarray(metrics['target_tcp_pose']).reshape(4,4),expected_target)
        np.testing.assert_allclose(np.asarray(metrics['best_fit_tcp_pose']).reshape(4,4),expected_fit)
        np.testing.assert_allclose(metrics['target_minus_fit_tcp_m'],expected_target[:3,3]-expected_fit[:3,3])
        assert not {'goal','best_fit_eef_pose','arbitrary_secret'}&metrics.keys()


def test_large_candidate_history_retains_failure_stage_and_non_executable_fit():
    pose=np.eye(4);pose[:3,3]=[.7123456789,-.123456789,1.023456789]
    metrics=failed_search(pose,pose)
    metrics.pop('goal');metrics.pop('best_fit_eef_pose')
    metrics.update(frame='robot_base',target_tcp_pose=pose.ravel().tolist(),
                   best_fit_tcp_pose=pose.ravel().tolist(),target_minus_fit_tcp_m=[.05,0.,0.],
                   route_family='joint_space_approach')
    last=dict(code='numerical_joint_search_no_valid_goal',metrics=metrics)
    candidates=[dict(variant=i%4,diagnostic=dict(code='bounded_IK_did_not_reach_goal',
        metrics=dict(goal=pose.tolist(),final_arm_deg=[10.]*7,position_error_m=.1))) for i in range(28)]
    candidates.append(dict(variant=2,diagnostic=last))
    error=HarnessError('registered_candidates_exhausted:'+json.dumps(dict(
        diagnostic=dict(code='registered_candidates_exhausted',metrics=metrics),candidates=candidates)))
    parsed=numerical_failure_detail(error)
    for value in (parsed,_planning_feedback(parsed)):
        assert len(json.dumps(value,ensure_ascii=False))<=4095
        assert value['candidate_count']==29
        final=value['last_candidate_failure']
        assert final['code']=='numerical_joint_search_no_valid_goal'
        assert final['metrics']['search_phase']=='endpoint_ik'
        assert final['metrics']['best_fit_executable'] is False
        assert value['metrics']['position_error_m']==pytest.approx(.0511076)
        assert len(value['metrics']['target_tcp_pose'])==16
        assert len(value['metrics']['best_fit_tcp_pose'])==16
        assert value['metrics']['frame']=='robot_base'

