"""Current geometric support evidence, without motion or a task-specific posture.

A registered box near a registered plane is a contact candidate, not measured
load or force. Independent current RGB-D plus an unheld stationary object may
support the observed static-support relation. Every scalar retains its source;
uncalibrated geometry errors do not become a fabricated accuracy certificate.
"""
from itertools import product

import numpy as np

from .geometry import transform


def measure_support(observation, *, object_id, object_local_box, support_surface,
                    contact_tolerance_m):
    """Describe a measured object's relation to one registered/observed plane.

    The caller supplies its existing geometric contact tolerance. It labels the
    relation only; this function neither grants motion nor verifies release.
    All three object-axis angles are returned so the task chooses the desired
    orientation rather than this module assuming which axis must be upright.
    """
    if (not isinstance(contact_tolerance_m, (int, float))
            or isinstance(contact_tolerance_m, bool)
            or not np.isfinite(contact_tolerance_m) or contact_tolerance_m < 0):
        raise ValueError('finite_nonnegative_support_contact_tolerance_required')
    result=dict(object_id=object_id, relation='unknown', reason='object_missing',
        support_observed=False, contact_measured=False, motion_authorized=False,
        precision_certified=False, geometry_error_bound_m=None,
        tolerance_source='caller_registered_contact_tolerance',
        contact_tolerance_m=float(contact_tolerance_m),
        gap_m=None, penetration_m=None, axis_normal_angles_deg=None,
        normal_in_object=None, support_normal=None, held=None, stationary=None,
        object_measurement=None, support_measurement=None,
        observation_id=observation.observation_id, calibration_id=observation.calibration_id,
        synthetic=observation.synthetic,quality_warnings=list(observation.quality_warnings),
        pairing_quality=None,source_time_pairing_verified=False)
    obj=next((item for item in observation.objects if item.object_id==object_id),None)
    if obj is None:return result
    result.update(held=obj.held,stationary=obj.stationary)
    frames=[frame for frame in observation.frames if frame.blob_sha256 in obj.evidence_ids]
    object_time=min((frame.received_ns for frame in frames),default=None)
    measurement=dict(source='current_rgbd_object_pose',evidence_ids=list(obj.evidence_ids),
        measured_at_ns=object_time,observation_received_ns=observation.received_ns,
        frame_id=obj.frame_id,uncertainty_m=obj.uncertainty_m,
        dimensions_source='registered_object_box',
        frames=[dict(camera=f.camera,stream=f.stream,source_ns=f.source_ns,
                     received_ns=f.received_ns,source_epoch=f.source_epoch,
                     publisher_id=f.publisher_id) for f in frames])
    result['object_measurement']=measurement
    if obj.pose is None or not obj.visible:
        result['reason']='current_object_pose_unavailable';return result
    if not isinstance(object_local_box,dict):
        result['reason']='registered_object_box_unavailable';return result
    if not isinstance(support_surface,dict):
        result['reason']='support_surface_unavailable';return result
    plane={key:support_surface.get(key) for key in (
        'support_id','frame_id','point','normal','source','calibration_id','measured_at_ns')}
    plane['evidence_ids']=list(support_surface.get('evidence_ids') or ())
    result['support_measurement']=plane
    if (not plane['support_id'] or plane['frame_id']!=obj.frame_id
            or not observation.calibration_id or plane['calibration_id']!=observation.calibration_id):
        result['reason']='support_frame_or_calibration_mismatch';return result
    if plane['source'] not in ('registered_calibration','rgbd_plane'):
        result['reason']='support_source_unavailable';return result
    if (plane['source']=='rgbd_plane' and
            (type(plane['measured_at_ns']) is not int or plane['measured_at_ns']<0
             or plane['measured_at_ns']>observation.received_ns or not plane['evidence_ids'])):
        result['reason']='support_measurement_identity_unavailable';return result
    if plane['source']=='rgbd_plane':
        plane_frames=[frame for frame in observation.frames if frame.blob_sha256 in plane['evidence_ids']]
        if (len(plane_frames)!=2 or len({frame.camera for frame in plane_frames})!=1
                or {frame.stream for frame in plane_frames}!={'rgb','depth'}
                or plane['measured_at_ns']!=min(frame.received_ns for frame in plane_frames)):
            result['reason']='support_source_evidence_not_current_rgbd';return result
    try:
        pose=transform(obj.pose)
        low,high=np.asarray(object_local_box['min'],float),np.asarray(object_local_box['max'],float)
        point,normal=np.asarray(plane['point'],float),np.asarray(plane['normal'],float)
        if (any(value.shape!=(3,) for value in (low,high,point,normal))
                or not all(np.isfinite(value).all() for value in (low,high,point,normal))
                or not np.all(high>low) or np.linalg.norm(normal)==0):
            raise ValueError('invalid_support_geometry')
        normal=normal/np.linalg.norm(normal)
    except (ValueError,TypeError,KeyError):
        result['reason']='invalid_support_geometry';return result
    corners=np.asarray(list(product(*zip(low,high))))
    world_corners=corners@pose[:3,:3].T+pose[:3,3]
    gap=float(((world_corners-point)@normal).min())
    axis_cosines=np.clip(pose[:3,:3].T@normal,-1.,1.)
    relation=('separated' if gap>contact_tolerance_m else
              'model_conflict' if gap < -contact_tolerance_m else 'geometric_contact_candidate')
    result.update(relation=relation,reason='measured_pose_against_support_model',
        gap_m=gap,penetration_m=max(0.,-gap),support_normal=normal.tolist(),
        normal_in_object=axis_cosines.tolist(),
        axis_normal_angles_deg=np.rad2deg(np.arccos(axis_cosines)).tolist())
    # The actual object source must match both current streams. Blob/evidence
    # references from another snapshot cannot establish fresh static support.
    cameras={f.camera for f in frames}
    pairing=next((p for p in observation.pairings if p.camera in cameras),None)
    by_stream={f.stream:f for f in frames}
    coarse=observation.geometry_scope in ('coarse_static_task','coarse_object_verification')
    permitted=('source_metadata_matched','source_time_bounded','receipt_only') if coarse else (
        'source_metadata_matched','source_time_bounded')
    paired=(pairing is not None and pairing.quality in permitted
        and by_stream.get('rgb') is not None and by_stream.get('depth') is not None
        and pairing.rgb_sequence==by_stream['rgb'].sequence and pairing.depth_sequence==by_stream['depth'].sequence)
    result['pairing_quality']=None if pairing is None else pairing.quality
    result['source_time_pairing_verified']=bool(pairing and pairing.quality in (
        'source_metadata_matched','source_time_bounded'))
    if pairing and pairing.quality=='receipt_only':
        result['quality_warnings'].append('support_rgbd_receipt_pairing_source_time_unverified')
    current_rgbd=(len(cameras)==1 and {f.stream for f in frames}=={'rgb','depth'}
        and len(frames)==2 and len(set(obj.evidence_ids))==2
        and all(f.source_ns is not None and 0<=f.received_ns<=observation.received_ns for f in frames)
        and not observation.faults and paired)
    if not current_rgbd:
        result['reason']='object_source_evidence_not_current_rgbd'
    elif relation=='geometric_contact_candidate' and obj.held is False and obj.stationary is True:
        result.update(support_observed=True,reason='current_unheld_stationary_object_at_support')
    elif relation=='geometric_contact_candidate':
        result['reason']='support_load_unresolved_while_held_or_moving'
    return result
