from dataclasses import replace

import numpy as np
import pytest

from f1_harness.grasp_suggestions import square_seeds,suggest_grasps
from f1_harness.geometry import transform
from test_full_development_skills import scene,pose


def prepared():
    return replace(scene(),parameterized_grasp=True,object_held=False)


def test_world_relocation_preserves_object_relative_seeds():
    value=prepared();world=pose(.3,-.2,.1)
    world[:3,:3]=[[0,-1,0],[1,0,0],[0,0,1]]
    shifted=replace(value,base_object=world@value.base_object,base_eef=world@value.base_eef,
                    base_hole=world@value.base_hole)
    before,after=square_seeds(value),square_seeds(shifted)
    assert before==after
    for a,b in zip(before,after):
        assert np.allclose(shifted.base_object@transform(b['grasp_tcp_pose']),
                           world@value.base_object@transform(a['grasp_tcp_pose']))


def test_object_size_changes_offsets_not_nominal_calibration():
    value=prepared();wide=replace(value,object_local_box={'min':[-.03,-.03,0],'max':[.03,.03,.06]})
    small,big=square_seeds(value),square_seeds(wide)
    center=transform(small[0]['grasp_tcp_pose'])[:3,3]
    assert small[0]==big[0]
    for a,b in zip(small[1:],big[1:]):
        assert np.allclose(transform(b['grasp_tcp_pose'])[:3,3]-center,
                           2*(transform(a['grasp_tcp_pose'])[:3,3]-center))
        assert b['approach_distance_m']==pytest.approx(2*a['approach_distance_m'])


def test_blocked_sample_is_advice_not_global_unreachability():
    value=replace(prepared(),obstacles=({'min':[-10,-10,-10],'max':[10,10,10]},))
    advice=suggest_grasps(value)
    assert advice['candidates'] and advice['geometrically_clear_count']==0
    assert all(c['geometry_status']=='predicted_blocked' for c in advice['candidates'])
    assert not advice['exhaustive'] and advice['continuous_proposals_allowed']
    assert not advice['motion_authorized'] and not advice['numerical_feasibility_checked']


def test_held_object_does_not_suggest_another_grasp():
    assert suggest_grasps(replace(prepared(),object_held=True))['status']=='not_applicable'


def test_suggestions_survive_structured_geometry_packing():
    from f1_harness.planner_geometry import pack_geometry,unpack_geometry
    advice=suggest_grasps(prepared())
    geometry={'grasp_suggestions':advice,'obstacles':[]}
    assert unpack_geometry(pack_geometry(geometry))==geometry
    assert 0<len(advice['candidates'])<=6
