"""Registered TCP pitch stays one coherent approach/contact/lift contract."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.grasp_candidates import apply_descriptor, descriptors, pitched
from f1_harness.skills import propose
from test_full_development_skills import pose, scene


def selected(angle):
    return dict(strategy='tcp_pitch',pitch_offset_deg=angle)


def nominal():
    return replace(scene(),object_held=False,eef_object=None,grasp_pitch_offsets_deg=(30.,),
                   pregrasp_distances_m=(.06,.08))


def demo():
    samples=[pose(0,0,.5),pose(.12,.04,.55),pose(.2,0,.38)]
    samples[1][:3,:3]=Rotation.from_euler('z',35,degrees=True).as_matrix()
    segment=dict(eef_poses=[p.tolist() for p in samples],arm_deg=[[0.]*7,[1.]*7,[2.]*7],times_s=[0.,1.,2.])
    return dict(schema='f1-success-trajectory-v1',source_run='measured-demo',source_sha256='a'*64,
                measurement_kind='measured_feedback',segments={name:deepcopy(segment)
                    for name in ('approach','grasp','lift','transfer')})


def test_defaults_have_only_original_four_symmetries_and_zero_keeps_original_route():
    value=replace(scene(),success_trajectory=demo())
    assert descriptors(value)==(selected(0.),)
    selected_scene=apply_descriptor(value,selected(0.))
    assert not pitched(selected_scene)
    for variant in range(4):
        assert propose('approach_grasp',selected_scene,variant=variant)==propose('approach_grasp',value,variant=variant)
    with pytest.raises(RuntimeError,match='unregistered_grasp_pitch_offset'):
        apply_descriptor(value,selected(30.))


def test_pitch_keeps_capture_point_and_nominal_closing_axis_in_arbitrary_frames():
    value=nominal();grasp=value.object_grasp_eef.copy();tip=value.eef_tip_reference.copy()
    grasp[:3,:3]=Rotation.from_euler('zyx',[19,13,-25],degrees=True).as_matrix()
    tip[:3,:3]=Rotation.from_euler('xyz',[8,12,22],degrees=True).as_matrix()
    tip[:3,3]=[.006,-.249,.009]
    value=replace(value,object_grasp_eef=grasp,eef_tip_reference=tip)
    changed=apply_descriptor(value,selected(30.))
    assert np.allclose((changed.object_grasp_eef@tip)[:3,3],(grasp@tip)[:3,3])
    assert np.allclose(changed.object_grasp_eef[:3,0],grasp[:3,0])
    assert np.allclose(changed.object_grasp_eef[:3,:3],
                       grasp[:3,:3]@Rotation.from_euler('x',30,degrees=True).as_matrix())
    assert np.array_equal(value.object_grasp_eef,grasp)
    assert apply_descriptor(changed,selected(30.)) is changed
    with pytest.raises(RuntimeError,match='grasp_candidate_already_applied'):
        apply_descriptor(changed,selected(0.))


@pytest.mark.parametrize('variant',range(4))
def test_selected_pitch_survives_approach_grasp_and_lift_without_demo_twist(variant):
    value=apply_descriptor(replace(nominal(),success_trajectory=demo()),selected(30.))
    approach=propose('approach_grasp',value,variant=variant,pregrasp_distance_m=.06)
    assert len(approach['stages'][0]['goals'])==1
    assert 'trajectory_source' not in approach['stages'][0]
    endpoint=np.array(approach['stages'][0]['goals'][0])
    symmetry=pose();symmetry[:3,:3]=Rotation.from_euler('z',variant*90,degrees=True).as_matrix()
    contact=value.base_object@symmetry@value.object_grasp_eef
    normal=value.base_object[:3,2]
    axis=contact[:3,3]-(contact@value.eef_tip_reference)[:3,3]
    axis-=normal*np.dot(axis,normal);axis/=np.linalg.norm(axis)
    assert np.allclose(endpoint[:3,3],contact[:3,3]+axis*.06)
    assert np.allclose(endpoint[:3,:3],contact[:3,:3])
    grasp=propose('grasp',replace(value,base_eef=endpoint))
    assert len(grasp['stages'][0]['goals'])==1
    assert np.allclose(grasp['stages'][0]['goals'][0],contact)
    assert 'trajectory_source' not in grasp['stages'][0]
    for raw in grasp['stages'][-1]['goals']:
        lifted=np.array(raw)
        assert np.allclose(lifted[:3,:3],contact[:3,:3])
        assert np.allclose(lifted[:2,3],contact[:2,3])
    assert value.success_trajectory['segments']['transfer']==demo()['segments']['transfer']


def test_selected_grasp_does_not_remove_or_change_transport_demonstration():
    value=replace(scene(),transfer_policy='registered_transport',success_trajectory=demo(),
                  grasp_pitch_offsets_deg=(30.,))
    changed=apply_descriptor(value,selected(30.))
    assert changed.success_trajectory is value.success_trajectory
    assert propose('transfer_above_hole',changed)==propose('transfer_above_hole',value)
    assert propose('transfer_above_hole',changed)['stages'][0]['trajectory_source']=='measured-demo'


@pytest.mark.parametrize('offsets',[[-46],[46],[0],[True],[float('nan')],[30,30],list(range(1,8))])
def test_registration_is_finite_explicit_and_bounded(offsets):
    with pytest.raises(RuntimeError,match='invalid_registered_grasp_pitch_offsets'):
        descriptors(replace(scene(),grasp_pitch_offsets_deg=offsets))


@pytest.mark.parametrize('descriptor',[
    {'strategy':'world_pitch','pitch_offset_deg':30},
    {'strategy':'tcp_pitch','pitch_offset_deg':30,'translation':[0,0,.1]},
    selected(-30),selected(True),selected(float('nan')),
])
def test_descriptor_cannot_add_unregistered_angle_or_other_motion_parameters(descriptor):
    with pytest.raises(RuntimeError,match='invalid_grasp_candidate_descriptor|unregistered_grasp_pitch_offset'):
        apply_descriptor(nominal(),descriptor)


def test_pitch_still_rejects_actual_current_route_obstruction():
    value=apply_descriptor(nominal(),selected(30.))
    obstruction=dict(min=[-.1,-.1,.2],max=[.3,.3,.7],role='neighbor',name='blocking_box')
    with pytest.raises(RuntimeError,match='scene_sweep_blocked:'):
        propose('approach_grasp',replace(value,obstacles=(obstruction,)),variant=3)


def test_owner_bound_square_variant_is_retained_even_if_another_is_nearer():
    value=apply_descriptor(nominal(),selected(30.))
    nearest=value.base_object@value.object_grasp_eef
    value=replace(value,base_eef=nearest,grasp_candidate_variant=3)
    symmetry=pose();symmetry[:3,:3]=Rotation.from_euler('z',270,degrees=True).as_matrix()
    expected=value.base_object@symmetry@value.object_grasp_eef
    assert np.allclose(propose('grasp',value)['stages'][0]['goals'][-1],expected)
    for invalid in (-1,4,True,1.5):
        with pytest.raises(RuntimeError,match='invalid_bound_grasp_candidate_variant'):
            propose('grasp',replace(value,grasp_candidate_variant=invalid))
