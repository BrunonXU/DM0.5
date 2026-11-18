"""Continuous geometric requests, reconstructed by planning AND revalidation.

The model supplies TCP goals, never joint commands or a collision exemption.
World means the observed native world; object/hole are measured task frames.
"""
from dataclasses import replace
import numpy as np

from .contracts import HarnessError, MotionParameters
from .geometry import transform


def grasp_descriptor(parameters):
    p=parameters
    if p.grasp_tcp_pose is None and p.approach_direction_object is None and p.approach_distance_m is None:
        return None
    return dict(strategy='object_tcp',grasp_tcp_pose=p.grasp_tcp_pose,
                approach_direction_object=p.approach_direction_object,
                approach_distance_m=p.approach_distance_m)


def apply_request(service,scene,intent):
    if intent.skill!='move_tcp':return scene
    if not service.fixture.get('cartesian_motion'):
        raise HarnessError('cartesian_motion_not_enabled')
    p=intent.parameters.for_skill('move_tcp')
    if getattr(scene,'local_collision_evidence',None) is not None and (
            p.target_frame!='world' or p.support_contact):
        raise HarnessError('local_environment_requires_world_noncontact_motion')
    if p.target_frame=='hole' and not scene.aperture_measured:
        raise HarnessError('measured_aperture_required_for_hole_relative_motion')
    frame=(service.frames.world_native if p.target_frame=='world' else
           scene.base_object if p.target_frame=='object' else scene.base_hole)
    poses=[frame@transform(v)@np.linalg.inv(transform(scene.eef_tip_reference))
           for v in (*p.via_tcp_poses,p.target_tcp_pose)]
    return replace(scene,cartesian_request=dict(goals=[p.tolist() for p in poses],
        parameters=p.supplied(),support_contact=p.support_contact),success_trajectory=None)


def cartesian_proposal(scene):
    from .skills import check_motion_route
    if scene.cartesian_request is None:raise HarnessError('cartesian_target_missing')
    if scene.object_held is True and scene.eef_object is None:
        raise HarnessError('measured_in_hand_transform_required')
    goals=scene.cartesian_request['goals']
    attached=scene.eef_object if scene.object_held is True else None
    contact=scene.cartesian_request.get('support_contact',False)
    if contact and (attached is None or scene.object_local_box is None
                    or not any(o.get('type')=='halfspace' and o.get('role')=='table' for o in scene.obstacles)):
        raise HarnessError('support_motion_requires_attachment_and_support')
    check_motion_route(scene,goals,eef_object=attached,skill='move_tcp',route_family='continuous_tcp',
        support_lift=contact,support_contact_direction='approach' if contact else 'depart')
    support_fields=(dict(support_contact_tolerance_m=scene.object_planning_margin_m,
        support_contact_direction='approach') if contact else {})
    return dict(kind='motion',skill='move_tcp',stages=[dict(name='free_space_tcp',goals=goals,
        gripper=None,eef_object=None if attached is None else transform(attached).tolist(),
        allow_target_contact=False,posture_reference_policy='measured_stage_start',
        route_family='continuous_tcp',**support_fields)])
