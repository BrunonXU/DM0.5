"""Endpoint advice preserves frame semantics and cannot certify a route."""
from dataclasses import replace
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.collision_guidance import endpoint_clearance_hint
from f1_harness.geometry import tool_sweep_clear
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.adapters import _planning_feedback
from f1_harness.skills import check_motion_route, tool_margin
from test_full_development_skills import scene, pose


def test_rotated_multibox_tool_hint_clears_only_named_obstacle():
    tool_pose = pose(.4, .1, .5)
    tool_pose[:3,:3] = Rotation.from_euler('xyz', [19, 32, 41], degrees=True).as_matrix()
    obstacle = dict(min=[-.03]*3, max=[.03]*3, pose=tool_pose.tolist())
    object_pose = pose(.2, .3, .1)
    object_pose[:3,:3] = Rotation.from_euler('zy', [71, 18], degrees=True).as_matrix()
    boxes = (dict(min=[-.02]*3, max=[.02]*3),
             dict(min=[-.01]*3, max=[.01]*3, pose=pose(.04).tolist()))
    value = replace(scene(), base_object=object_pose, tool_local_boxes=boxes)
    hint = endpoint_clearance_hint(value, tool_pose, obstacle)
    moved = tool_pose.copy()
    moved[:3,3] += object_pose[:3,:3] @ hint['translation_m']
    assert tool_sweep_clear(moved, moved, value.eef_tip_reference, value.attached_radius_m,
        [obstacle], margin_m=tool_margin(value), local_boxes=boxes)
    assert not hint['motion_authorized'] and not hint['path_checked'] and not hint['grasp_checked']
    # The colliding starting endpoint remains rejected. Advice changes no gate.
    with pytest.raises(RuntimeError, match='scene_sweep_blocked'):
        check_motion_route(replace(value, obstacles=(obstacle,)), [moved], start=tool_pose)


def test_hint_covariant_under_world_rotation_and_translation():
    end = pose(.4, .1, .5)
    obstacle = dict(min=[-.015, -.02, -.03], max=[.015, .02, .03], pose=end.tolist())
    value = replace(scene(), base_object=pose(.2), tool_local_boxes=(dict(min=[-.01]*3,max=[.01]*3),))
    first = endpoint_clearance_hint(value, end, obstacle)
    world = pose(1, -2, .7)
    world[:3,:3] = Rotation.from_euler('xyz',[23,47,11],degrees=True).as_matrix()
    moved = replace(value, base_object=world@value.base_object)
    second = endpoint_clearance_hint(moved, world@end, obstacle|dict(pose=(world@end).tolist()))
    assert np.allclose(first['translation_m'], second['translation_m'], atol=1e-12, rtol=0)
    assert second['distance_m'] == pytest.approx(first['distance_m'])


def test_endpoint_advice_survives_owner_and_planner_without_mutating_goal():
    obstacle = dict(min=[.39,-.02,.48], max=[.41,.02,.52], role='bucket', name='wall')
    value = replace(scene(), base_eef=pose(0,0,.5), obstacles=(obstacle,),
        tool_local_boxes=(dict(min=[-.005]*3,max=[.005]*3),), object_held=False,eef_object=None)
    end = pose(.4,0,.5); before=end.copy()
    with pytest.raises(RuntimeError) as caught:
        check_motion_route(value,[end])
    metrics = _planning_feedback(numerical_failure_detail(caught.value))['metrics']
    hint = metrics['endpoint_clearance_hint']
    assert hint['frame']=='object' and hint['scope']=='single_obstacle_tool_endpoint_fixed_orientation'
    assert len(hint['translation_m'])==3 and not hint['motion_authorized']
    assert np.array_equal(end,before)
    with pytest.raises(RuntimeError) as sweep:
        check_motion_route(replace(value,obstacles=(obstacle|dict(min=[.19,-.02,.48],max=[.21,.02,.52]),)),[end])
    assert 'endpoint_clearance_hint' not in numerical_failure_detail(sweep.value)['metrics']


def test_unsupported_envelopes_do_not_invent_advice():
    assert endpoint_clearance_hint(replace(scene(),tool_local_boxes=None),pose(),{}) is None
    assert endpoint_clearance_hint(scene(),pose(),dict(type='halfspace')) is None
