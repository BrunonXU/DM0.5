"""Support placement is reverse departure, never a general collision exemption."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.geometry import attached_object_sweep_clear


BOX=dict(min=[-.02,-.02,0.],max=[.02,.02,.08])
TABLE=dict(type='halfspace',role='table',point=[0,0,0],normal=[0,0,1])


def pose(z,x=0.):
    value=np.eye(4);value[:3,3]=[x,0,z];return value


def clear(start,end,*,direction='approach',obstacles=(TABLE,),box=BOX):
    return attached_object_sweep_clear(start,end,np.eye(4),box,obstacles,
        margin_m=.003,support_contact_tolerance_m=.003,support_contact_direction=direction)


def test_normal_approach_to_support_is_reverse_of_existing_lift():
    assert clear(pose(.1),pose(0))
    assert not clear(pose(.1),pose(0),direction='depart')
    assert clear(pose(0),pose(.1),direction='depart')
    assert attached_object_sweep_clear(pose(0),pose(.1),np.eye(4),BOX,(TABLE,),
        margin_m=.003,support_contact_tolerance_m=.003)


def test_contact_rule_uses_existing_model_tolerance_without_permitting_pass_through():
    assert clear(pose(.1),pose(-.002))
    assert not clear(pose(.1),pose(-.004))
    assert not clear(pose(-.1),pose(0))


def test_horizontal_sweep_in_contact_is_not_a_support_approach():
    assert not clear(pose(0),pose(0,.1))
    assert clear(pose(0),pose(0))  # A stationary endpoint check is valid.
    assert clear(pose(.1),pose(.1,.1))  # Clear free-space motion retains ordinary checks.


def test_rotation_with_clear_endpoints_but_interior_penetration_is_rejected():
    box=dict(min=[0.,0.,0.],max=[.1,.01,.1])
    start=pose(0);end=pose(.1)
    end[:3,:3]=Rotation.from_euler('y',180,degrees=True).as_matrix()
    middle=pose(.05);middle[:3,:3]=Rotation.from_euler('y',90,degrees=True).as_matrix()
    assert (middle@np.array([.1,0,0,1]))[2]<0
    assert not clear(start,end,box=box)
    assert not clear(end,start,box=box)


def test_support_allowance_does_not_exempt_other_obstacles():
    neighbour=dict(min=[-.01,-.01,.09],max=[.01,.01,.11],role='neighbour')
    assert not clear(pose(.1),pose(0),obstacles=(TABLE,neighbour))


@pytest.mark.parametrize('direction',['',None,'sideways'])
def test_unknown_direction_rejected_even_without_contact_tolerance(direction):
    with pytest.raises(ValueError,match='invalid_support_contact_direction'):
        clear(pose(.1),pose(0),direction=direction)
    with pytest.raises(ValueError,match='invalid_support_contact_direction'):
        attached_object_sweep_clear(pose(.1),pose(.2),np.eye(4),BOX,(),margin_m=.003,
            support_contact_direction=direction)


def test_skill_route_passes_direction_but_keeps_tool_collision():
    from dataclasses import replace
    from f1_harness.skills import _motion_clear,check_motion_route
    from f1_harness.contracts import HarnessError
    from test_full_development_skills import scene
    offset=pose(-.1)
    geometry=replace(scene(),obstacles=(TABLE,),object_local_box=BOX,
        object_planning_margin_m=.003,tool_local_boxes=({'min':[-.01,-.01,-.01],'max':[.01,.01,.01]},))
    assert _motion_clear(geometry,pose(.2),pose(.1),offset,support_lift=True,
                         support_contact_direction='approach')
    check_motion_route(geometry,[pose(.1)],start=pose(.2),eef_object=offset,support_lift=True,
                       support_contact_direction='approach')
    with pytest.raises(HarnessError,match='scene_sweep_blocked'):
        check_motion_route(geometry,[pose(0)],start=pose(.1),eef_object=np.eye(4),support_lift=True,
                           support_contact_direction='approach')
