"""Physical sweep failures retain the obstructed phase through the owner parser."""
from dataclasses import replace

import pytest

from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.skills import check_motion_route, propose
from test_full_development_skills import pose, scene


def test_approach_failure_identifies_current_phase_and_real_obstacle():
    obstacle=dict(min=[.18,-.04,.27],max=[.22,.04,.4],role='target_object',
                  object_id='block',name='square')
    value=replace(scene(),object_held=False,eef_object=None,obstacles=(obstacle,))
    with pytest.raises(RuntimeError,match='scene_sweep_blocked:') as caught:
        propose('approach_grasp',value)
    diagnostic=numerical_failure_detail(caught.value)
    assert diagnostic['code']=='scene_sweep_blocked'
    assert diagnostic['metrics']['skill']=='approach_grasp'
    assert diagnostic['metrics']['stage_index']==0
    assert diagnostic['metrics']['obstacle_name']=='square'
    assert diagnostic['metrics']['collision_body']=='tool'


def test_lift_failure_is_not_misreported_as_approach_or_future_insert():
    obstruction=dict(min=[.19,-.01,.4],max=[.21,.01,.43],role='neighbor',name='overhang')
    value=replace(scene(),base_eef=pose(.2,0,.3),object_held=False,eef_object=None,
                  obstacles=(obstruction,))
    with pytest.raises(RuntimeError,match='grasp_lift_sweep_blocked:') as caught:
        propose('grasp',value)
    diagnostic=numerical_failure_detail(caught.value)
    assert diagnostic['code']=='grasp_lift_sweep_blocked'
    assert diagnostic['metrics']['stage_index']==1
    assert diagnostic['metrics']['route_family']=='grasp_lift'
    assert diagnostic['metrics']['obstacle_name']=='overhang'


def test_attached_object_collision_diagnostic_is_distinct_from_tool():
    obstacle=dict(min=[.195,-.005,.195],max=[.205,.005,.205],role='neighbor',name='payload_only')
    value=replace(scene(),obstacles=(obstacle,))
    with pytest.raises(RuntimeError,match='scene_sweep_blocked:') as caught:
        check_motion_route(value,[pose(0,0,.5)],eef_object=pose(.2,0,-.3),
                           skill='transfer_above_hole')
    diagnostic=numerical_failure_detail(caught.value)
    assert diagnostic['metrics']['collision_body']=='attached_object'
    assert diagnostic['metrics']['obstacle_name']=='payload_only'
