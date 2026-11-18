"""Geometric support measurements do not assume a task or command a release."""
from copy import deepcopy

import numpy as np
import pytest

from f1_harness.contracts import Frame,Observation,Pairing,SceneObject
from f1_harness.support_evidence import measure_support


BOX=dict(min=[-.02,-.02,0.],max=[.02,.02,.08])
PLANE=dict(support_id='table',frame_id='base',point=[0.,0.,0.],normal=[0.,0.,1.],
    source='registered_calibration',calibration_id='cal',measured_at_ns=None)


def observed(*,pose=None,held=False,stationary=True):
    pose=np.eye(4) if pose is None else pose
    frames=tuple(Frame(camera='right',stream=stream,frame_id='optical',sequence=3,
        source_ns=123,received_ns=100,clock_domain='robot',width=2,height=2,
        encoding=encoding,blob_sha256=sha) for stream,encoding,sha in (
            ('rgb','rgb8','a'*64),('depth','16UC1','b'*64)))
    return Observation(observation_id='obs',acquisition_instance='capture',received_ns=110,
        clock_domain='robot',calibration_id='cal',frames=frames,
        pairings=(Pairing(camera='right',quality='source_time_bounded',rgb_sequence=3,depth_sequence=3,skew_ns=0),),
        objects=(SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),frame_id='base',
            held=held,stationary=stationary,evidence_ids=tuple(f.blob_sha256 for f in frames)),))


def measure(observation,**kwargs):
    return measure_support(observation,object_id='block',object_local_box=BOX,
        support_surface=kwargs.get('support_surface',PLANE),contact_tolerance_m=.003)


def test_current_unheld_stationary_object_has_observed_geometric_support():
    result=measure(observed())
    assert result['support_observed'] and result['relation']=='geometric_contact_candidate'
    assert result['gap_m']==0. and result['axis_normal_angles_deg']==[90.,90.,0.]
    assert result['object_measurement']['measured_at_ns']==100
    assert result['object_measurement']['observation_received_ns']==110
    assert result['support_measurement']['measured_at_ns'] is None
    assert result['geometry_error_bound_m'] is None
    assert not result['precision_certified'] and not result['contact_measured'] and not result['motion_authorized']


def test_lying_rotated_object_uses_box_and_plane_not_long_axis_assumption():
    pose=np.eye(4);pose[:3,:3]=[[1,0,0],[0,0,-1],[0,1,0]];pose[2,3]=.02
    result=measure(observed(pose=pose))
    assert result['support_observed'] and result['gap_m']==pytest.approx(0)
    assert result['axis_normal_angles_deg']==[90.,0.,90.]


def test_tilted_support_reports_angles_in_same_declared_frame():
    from scipy.spatial.transform import Rotation
    pose=np.eye(4);pose[:3,:3]=Rotation.from_euler('y',30,degrees=True).as_matrix()
    plane=dict(PLANE,normal=pose[:3,2].tolist())
    result=measure(observed(pose=pose),support_surface=plane)
    assert result['support_observed'] and result['gap_m']==pytest.approx(0,abs=1e-12)
    assert result['axis_normal_angles_deg']==pytest.approx([90.,90.,0.])


@pytest.mark.parametrize('held,stationary',[(True,True),(None,True),(False,False),(False,None)])
def test_geometric_contact_while_held_or_unstable_does_not_prove_support(held,stationary):
    result=measure(observed(held=held,stationary=stationary))
    assert result['relation']=='geometric_contact_candidate' and not result['support_observed']
    assert result['held'] is held and result['stationary'] is stationary


@pytest.mark.parametrize('height,relation',[(.1,'separated'),(-.02,'model_conflict')])
def test_separation_or_model_penetration_is_not_a_supported_object(height,relation):
    pose=np.eye(4);pose[2,3]=height
    result=measure(observed(pose=pose))
    assert result['relation']==relation and not result['support_observed']
    assert result['gap_m']==pytest.approx(height) and result['penetration_m']==max(0,-height)


@pytest.mark.parametrize('field,value',[('frame_id','other'),('calibration_id','other'),('source',None)])
def test_support_identity_is_required(field,value):
    result=measure(observed(),support_surface=dict(PLANE,**{field:value}))
    assert result['relation']=='unknown' and not result['support_observed']
    assert result['gap_m'] is None


@pytest.mark.parametrize('change',['old_blob','future_frame','missing_source_time','fault','unpaired','missing_pose','invisible'])
def test_missing_or_mismatched_current_evidence_preserves_unknown(change):
    observation=observed()
    if change=='old_blob':
        observation=observation.model_copy(update=dict(objects=(observation.objects[0].model_copy(update=dict(evidence_ids=('c'*64,'d'*64))),)))
    elif change=='future_frame':
        observation=observation.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(received_ns=200)) for f in observation.frames)))
    elif change=='missing_source_time':
        observation=observation.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(source_ns=None)) for f in observation.frames)))
    elif change=='fault':observation=observation.model_copy(update=dict(faults=('source_time_reversed:right',)))
    elif change=='unpaired':observation=observation.model_copy(update=dict(pairings=()))
    else:
        observation=observation.model_copy(update=dict(objects=(observation.objects[0].model_copy(update=(
            dict(pose=None) if change=='missing_pose' else dict(visible=False))),)))
    result=measure(observation)
    assert not result['support_observed'] and not result['motion_authorized']
    assert result['reason']!='current_unheld_stationary_object_at_support'


def test_measured_plane_requires_its_actual_current_source_time_and_blobs():
    observation=observed()
    plane=dict(PLANE,source='rgbd_plane',measured_at_ns=100,evidence_ids=['a'*64,'b'*64])
    assert measure(observation,support_surface=plane)['support_observed']
    for changed in (dict(plane,measured_at_ns=99),dict(plane,evidence_ids=['c'*64,'d'*64])):
        result=measure(observation,support_surface=changed)
        assert not result['support_observed'] and result['relation']=='unknown'


def test_module_preserves_inputs_and_missing_object_is_unknown():
    observation=observed();before=observation.model_dump();plane=deepcopy(PLANE)
    measure(observation,support_surface=plane)
    assert observation.model_dump()==before and plane==PLANE
    assert measure(observation.model_copy(update=dict(objects=())))['reason']=='object_missing'


@pytest.mark.parametrize('scope',['coarse_static_task','coarse_object_verification'])
def test_coarse_receipt_pairing_keeps_quality_without_new_precision_gate(scope):
    observation=observed()
    observation=observation.model_copy(update=dict(geometry_scope=scope,
        pairings=tuple(p.model_copy(update=dict(quality='receipt_only')) for p in observation.pairings)))
    result=measure(observation)
    assert result['support_observed'] and result['relation']=='geometric_contact_candidate'
    assert result['pairing_quality']=='receipt_only' and not result['source_time_pairing_verified']
    assert 'support_rgbd_receipt_pairing_source_time_unverified' in result['quality_warnings']
    assert all(f['source_ns']==123 for f in result['object_measurement']['frames'])
    assert not result['precision_certified']


def test_receipt_pairing_does_not_claim_precise_support():
    observation=observed()
    observation=observation.model_copy(update=dict(geometry_scope='precision',
        pairings=tuple(p.model_copy(update=dict(quality='receipt_only')) for p in observation.pairings)))
    result=measure(observation)
    assert not result['support_observed'] and not result['source_time_pairing_verified']
