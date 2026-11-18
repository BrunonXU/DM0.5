"""Support interaction contracts, independent of an object's desired posture."""
from dataclasses import replace
from itertools import product
import numpy as np
from .contracts import HarnessError, SkillResult
from .geometry import transform
from .support_evidence import measure_support


def scene_support(service, observation):
    fixture=service.fixture
    table=next((item for item in fixture.get('obstacles',())
                if item.get('type')=='halfspace' and item.get('role')=='table'),None)
    if table is None or fixture.get('object_local_box') is None:
        return dict(relation='unknown',reason='registered_support_geometry_missing',
                    support_observed=False,motion_authorized=False)
    return measure_support(observation,object_id=service.task.object_id,
        object_local_box=fixture['object_local_box'],
        support_surface=dict(support_id=table.get('name','table'),frame_id=fixture['native_frame'],
            point=table['point'],normal=table['normal'],source='registered_calibration',
            calibration_id=service.task.calibration_id,measured_at_ns=None),
        contact_tolerance_m=fixture['object_planning_margin_m'])


def retain_support_contact(scene,reference_pose,*,maximum_change_m,source_observation_id):
    """Retain a previously admissible coarse contact hypothesis across views.

    This does not move either measured pose or the table. Only the departing
    object's collision model records its fixed, currently observed contact
    baseline. Tool/table, other obstacles and ordinary attached motion retain
    their original geometry. No claim of measured support force is made.
    """
    if (scene.admission_profile!='development_coarse' or scene.object_held is True
            or scene.object_local_box is None or not source_observation_id):return scene
    prior=transform(reference_pose);current=transform(scene.base_object)
    box=scene.object_local_box;corners=np.array(list(product(*zip(box['min'],box['max']))))
    old=corners@prior[:3,:3].T+prior[:3,3];new=corners@current[:3,:3].T+current[:3,3]
    if np.max(np.linalg.norm(new-old,axis=1))>maximum_change_m:return scene
    obstacles=[]
    for item in scene.obstacles:
        if item.get('type')=='halfspace' and item.get('role')=='table':
            normal=np.asarray(item['normal'],float);normal/=np.linalg.norm(normal)
            previous_gap=float(np.min((old-np.asarray(item['point']))@normal))
            current_gap=float(np.min((new-np.asarray(item['point']))@normal))
            margin=scene.object_planning_margin_m
            if -margin<=previous_gap<=margin and current_gap < -margin:
                item=dict(item,support_departure_reference=dict(schema='f1-coarse-support-reference-v1',
                    object_id=scene.target_object_id,source_observation_id=source_observation_id,
                    previous_object_pose=prior.tolist(),measured_object_pose=current.tolist(),
                    maximum_change_m=maximum_change_m,model_gap_m=current_gap,
                    physical_contact_verified=False))
        obstacles.append(item)
    return replace(scene,obstacles=tuple(obstacles))


def current_support_departure(scene,*,observed_open_gripper=False):
    """Keep measured model overlap as a fixed departure hypothesis, not support proof.

    A visible object's registered box can straddle a measured
    table because of fit/calibration error. Its existing overlap is not a new
    commanded penetration. Only the subsequent monotonically departing object
    sweep may use this baseline; tool/table and every other obstacle stay intact.
    """
    evidence=scene.support_measurement or {}
    measurement=evidence.get('object_measurement') or {}
    if (scene.admission_profile!='development_coarse' or scene.object_held is True
            or scene.eef_object is not None or scene.object_local_box is None
            or not observed_open_gripper or evidence.get('held') is True
            or evidence.get('relation')!='model_conflict'
            or evidence.get('reason')!='measured_pose_against_support_model'
            or not evidence.get('observation_id') or len(measurement.get('evidence_ids',()))!=2):
        return scene
    pose=transform(scene.base_object)
    corners=np.array(list(product(*zip(scene.object_local_box['min'],scene.object_local_box['max']))))
    points=corners@pose[:3,:3].T+pose[:3,3]
    obstacles=[]
    for item in scene.obstacles:
        if item.get('type')=='halfspace' and item.get('role')=='table':
            normal=np.asarray(item['normal'],float);normal/=np.linalg.norm(normal)
            distances=(points-np.asarray(item['point'],float))@normal
            if float(distances.min())<0<float(distances.max()):
                item=dict(item,support_departure_reference=dict(
                    schema='f1-observed-model-departure-v1',object_id=scene.target_object_id,
                    source_observation_id=evidence['observation_id'],
                    source_evidence_ids=measurement['evidence_ids'],measured_object_pose=pose.tolist(),
                    model_gap_m=float(distances.min()),stationary=evidence.get('stationary'),
                    held=evidence.get('held'),observed_open_gripper=True,
                    quality_warnings=evidence.get('quality_warnings',[]),
                    physical_contact_verified=False,precision_certified=False))
        obstacles.append(item)
    return replace(scene,obstacles=tuple(obstacles))


def require_support_release(scene):
    """Permit a release attempt at measured contact, never claim load/force proof."""
    measured=scene.support_measurement or {}
    if (scene.object_held is not True or scene.eef_object is None
            or measured.get('held') is not True):
        raise HarnessError('support_release_attachment_unresolved')
    if (measured.get('relation')!='geometric_contact_candidate'
            or measured.get('reason')!='support_load_unresolved_while_held_or_moving'):
        raise HarnessError('support_release_contact_unavailable')
    if measured.get('stationary') is not True:
        raise HarnessError('support_release_object_not_stationary')


def verify_support_release(task,observation,measurement,request_id):
    obj=next((item for item in observation.objects if item.object_id==task.object_id),None)
    success=bool(obj and measurement.get('support_observed') is True)
    reason='supported_release_observed' if success else 'supported_release_evidence_missing'
    return SkillResult(request_id=request_id,lifecycle='completed',
        outcome='success' if success else 'pending_review',reason=reason,
        evidence_ids=() if obj is None else obj.evidence_ids,synthetic=observation.synthetic)
