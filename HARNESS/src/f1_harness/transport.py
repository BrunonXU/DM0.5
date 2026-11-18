"""Bounded loaded transport routes, independent of the subsequent hole alignment.

The object reference point, not the wrist, is carried over the measured hole.
Historical motion supplies a shape and soft postures; every resulting pose and
the subsequently solved FK path still pass the ordinary geometry/IK checks.
"""
from dataclasses import replace
from itertools import product

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .contracts import HarnessError
from .geometry import transform


ROUTE_FAMILIES = (
    'demonstrated_transport',
    'orientation_preserving_transport',
    'outside_bucket_reorientation_transport',
    'orientation_preserving_other_side_transport',
)


def route_family(variant):
    if type(variant) is not int or variant not in range(len(ROUTE_FAMILIES)):
        raise HarnessError('unregistered_candidate_variant')
    return ROUTE_FAMILIES[variant]


def _corners(box):
    points = np.array(list(product(*zip(box['min'], box['max']))), dtype=float)
    offset = transform(box.get('pose', np.eye(4)))
    return points @ offset[:3, :3].T + offset[:3, 3]


def _minimum_object_height(scene, rotation, normal):
    """Lowest tool and held-object points clear the lid during coarse transit."""
    from .skills import tool_margin
    attachment = transform(scene.eef_object)
    height = scene.transfer_clearance_m + scene.object_insertion_reference_height_m
    if scene.object_local_box:
        object_rotation = rotation @ attachment[:3, :3]
        lowest = float(np.min((_corners(scene.object_local_box) @ object_rotation.T) @ normal))
        margin = scene.object_planning_margin_m if scene.admission_profile == 'development_coarse' else scene.uncertainty_m
        height = max(height, scene.transfer_clearance_m + margin - lowest)
    offset = float(normal @ rotation @ attachment[:3, 3])
    if scene.tool_local_boxes:
        lowest = min(float(np.min((_corners(box) @ rotation.T) @ normal)) for box in scene.tool_local_boxes)
        height = max(height, scene.transfer_clearance_m + tool_margin(scene) + offset - lowest)
    else:
        tip = np.zeros(3) if scene.eef_tip_reference is None else transform(scene.eef_tip_reference)[:3, 3]
        height = max(height, scene.transfer_clearance_m + scene.attached_radius_m + tool_margin(scene)
                     + offset - float(normal @ rotation @ tip))
    return height


def _at_object_point(rotation, object_point, attachment):
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = object_point - rotation @ attachment[:3, 3]
    return result


def _check_route(scene, goals, variant, *, family=None, start=None, stage_index=0):
    from .skills import check_motion_route
    check_motion_route(scene, goals, start=start, eef_object=scene.eef_object,
                       route_family=family or route_family(variant), variant=variant,
                       stage_index=stage_index)


def joint_transport_goal(scene):
    """An endpoint for general joint search, without requiring a straight route.

    Reuse the measured transport orientation prior and loaded-object clearance.
    This first implementation keeps an exact endpoint; goal regions are future
    work. No insertion, support contact or object reorientation is inferred.
    """
    if scene.object_held is not True or scene.eef_object is None:
        raise HarnessError('measured_in_hand_transform_required')
    current, hole, attachment = map(transform, (scene.base_eef, scene.base_hole, scene.eef_object))
    segment = (scene.success_trajectory or {}).get('segments', {}).get('transfer')
    rotation = transform(segment['eef_poses'][-1])[:3, :3] if segment else current[:3, :3]
    normal = hole[:3, 2]
    height = max(float(normal @ ((current @ attachment)[:3, 3]-hole[:3, 3])),
                 _minimum_object_height(scene, rotation, normal))
    goal = _at_object_point(rotation, hole[:3, 3]+normal*height, attachment)
    _check_route(scene, [goal], 2, family='joint_space_transport', start=goal)
    stage = dict(name='transport_above_bucket', route_family='joint_space_transport',
                 goals=[goal.tolist()], gripper=None, eef_object=attachment.tolist(),
                 allow_target_contact=False)
    return dict(kind='motion', skill='transfer_above_hole', stages=[stage])


def uses_joint_transport(fixture, intent, proposal):
    """Bind reconstruction to the implementation recorded in a fixed proposal."""
    marked = any(e.get('route_family')=='joint_space_transport' for e in proposal.geometry_trace)
    if marked and (fixture.get('joint_space_transport') is not True
            or intent.skill!='transfer_above_hole' or len(proposal.geometry_trace)!=1
            or proposal.candidate_variant!=2):
        raise HarnessError('revalidation_joint_transport_contract_changed')
    return marked


def joint_transport_bounds(scene):
    from .skills import stage_obstacles,tool_margin
    return dict(joint_space_search=True,attached_radius_m=scene.attached_radius_m,
        obstacles=stage_obstacles(scene),uncertainty_m=tool_margin(scene),
        eef_tip_reference=None if scene.eef_tip_reference is None else transform(scene.eef_tip_reference).tolist(),
        tool_local_boxes=scene.tool_local_boxes,
        eef_object=transform(scene.eef_object).tolist(),object_local_box=scene.object_local_box,
        object_planning_margin_m=scene.object_planning_margin_m
            if scene.admission_profile=='development_coarse' else scene.uncertainty_m)


def propose_transport(scene, variant):
    """Return one of four registered routes; do not solve or assume insertion."""
    family = route_family(variant)
    if scene.object_held is not True or scene.eef_object is None:
        raise HarnessError('measured_in_hand_transform_required')
    current, hole, attachment = map(transform, (scene.base_eef, scene.base_hole, scene.eef_object))
    normal = hole[:3, 2]
    segment = (scene.success_trajectory or {}).get('segments', {}).get('transfer')
    rotation = current[:3, :3]
    if variant in (0, 2) and segment:
        # Measured successful transit orientation. The actual object may remain
        # tilted relative to the aperture; align is responsible for that later.
        rotation = transform(segment['eef_poses'][-1])[:3, :3]
    current_point = (current @ attachment)[:3, 3]
    rotations = Slerp([0, 1], Rotation.from_matrix(np.stack([current[:3, :3], rotation])))(
        np.linspace(0, 1, 17)).as_matrix()
    # Variant 2 changes posture at the current object location, before entering
    # the bucket corridor. Only its later, already oriented transit needs the
    # lid-wide clearance envelope. Table, bucket and neighbours are nevertheless
    # checked over every reorientation segment below, without exemptions.
    clearance_rotations = [rotation] if variant == 2 else rotations
    height = max(float(normal @ (current_point - hole[:3, 3])),
                 *(_minimum_object_height(scene, r, normal) for r in clearance_rotations))
    object_goal = hole[:3, 3] + normal * height
    lift_point = current_point + normal * (height - float(normal @ (current_point - hole[:3, 3])))
    lift = _at_object_point(current[:3, :3], lift_point, attachment)
    end = _at_object_point(rotation, object_goal, attachment)
    references = None
    if variant == 0 and segment:
        from .demonstration import retarget
        # Retain the demonstrated spatial curve, but do not import a local
        # wrist twist residual into a different grasp/attachment.
        route, refs = retarget(segment, lift, end, preserve_rotation=False)
        for goal in route:
            point = (goal @ attachment)[:3, 3]
            goal[:3, 3] += normal * max(0., height - float(normal @ (point - hole[:3, 3])))
        goals = [lift, *route]
        references = [segment['arm_deg'][0], *refs]
    elif variant == 2:
        # Explicit short angular segments preserve the held reference point
        # during reorientation. A single EEF Slerp would sweep the payload on an
        # unintended wrist-centred arc. Numerical/output sweeps remain required.
        oriented = [_at_object_point(r, current_point, attachment) for r in rotations[1:]]
        raised = _at_object_point(rotation, lift_point, attachment)
        goals = [*oriented, raised, end]
    elif variant == 3:
        direction = object_goal - lift_point
        direction -= normal * float(normal @ direction)
        side = np.cross(normal, direction)
        if np.linalg.norm(side) < 1e-9:
            side = hole[:3, 0].copy()
        side *= -1. / np.linalg.norm(side)
        offset = side * max(scene.approach_clearance_m, scene.transfer_clearance_m)
        # Translation first, rotation in the overhead corridor. Distinct
        # topology from the direct route; all dimensions use existing geometry.
        bend1 = _at_object_point(current[:3, :3], lift_point + offset, attachment)
        bend2 = _at_object_point(rotation, object_goal + offset, attachment)
        goals = [lift, bend1, bend2, end]
        if segment:
            references = [segment['arm_deg'][0], segment['arm_deg'][0],
                          segment['arm_deg'][-1], segment['arm_deg'][-1]]
    else:
        goals = [lift, end]
    _check_route(scene, goals, variant)
    stage = dict(name='transport_above_bucket', route_family=family,
                 goals=[goal.tolist() for goal in goals], gripper=None,
                 eef_object=attachment.tolist(), allow_target_contact=False)
    if variant == 2:
        # The current arm shape already realizes the low grasp. Pulling the
        # first small rotation toward an old high transfer posture can hit a
        # coupled shoulder limit. PlanService resolves this from measured q at
        # this stage's start; it is a soft objective, never a joint command.
        stage['posture_reference_policy'] = 'measured_stage_start'
        if segment:
            stage['orientation_prior_source'] = scene.success_trajectory['source_run']
    if scene.tool_padding_policy == 'advisory':
        from .skills import _motion_clear
        padded = replace(scene, tool_padding_policy='required')
        misses = [i for i, (before, after) in enumerate(zip([current, *goals], goals))
                  if not _motion_clear(padded, before, after, attachment)]
        if misses:
            stage['padding_advisory'] = dict(nominal_tool_margin_m=scene.planning_margin_m,
                goal_indices=misses, actual_geometry_checked=True, scope='semantic_segments')
    if references is not None:
        stage.update(posture_references_deg=references, trajectory_source=scene.success_trajectory['source_run'])
    return dict(kind='motion', skill='transfer_above_hole', stages=[stage])


def _alignment_endpoints(scene):
    from .skills import _square_goals
    current, hole, attachment = map(transform, (scene.base_eef, scene.base_hole, scene.eef_object))
    object_goal = hole.copy()
    object_goal[:3, 3] += hole[:3, 2] * (scene.object_insertion_reference_height_m + scene.transfer_clearance_m)
    return _square_goals(object_goal, current, attachment)


def check_predicted_feed(scene, end, variant):
    future = end.copy()
    future[:3, 3] -= transform(scene.base_hole)[:3, 2] * (scene.transfer_clearance_m + scene.insertion_depth_m)
    _check_route(scene, [future], variant, family='aligned_insertion_preview', start=end, stage_index=1)


def rank_alignment_feeds(scene, variants, ranking):
    """Prefer a clear next feed while keeping every current-stage candidate."""
    from .owner_bridge import numerical_failure_detail
    if (not variants or scene.object_held is not True or scene.eef_object is None
            or scene.base_hole is None or not scene.aperture_measured):
        return variants, ranking
    endpoints = _alignment_endpoints(scene)
    rows = {row['variant']: row for row in ranking}
    scored = []
    for variant in variants:
        feed = dict(status='predicted_clear', motion_authorized=False, requires_fresh_observation=True)
        try:
            check_predicted_feed(scene, endpoints[variant], variant)
        except HarnessError as exc:
            if not str(exc).startswith('scene_sweep_blocked:'):
                raise
            feed.update(status='predicted_blocked', diagnostic=numerical_failure_detail(exc))
        scored.append(dict(rows.get(variant, {}), variant=variant, insertion_preview=feed))
    scored.sort(key=lambda row: row['insertion_preview']['status'] == 'predicted_blocked')
    return [row['variant'] for row in scored], scored


def propose_alignment(scene, variant):
    """Orient the measured held object in clear overhead space, then descend.

    The transfer endpoint is deliberately not an insertion pose. Joining that
    endpoint directly to the old alignment pose mixes a large wrist rotation
    with descent toward the rim. Keep those operations separate, using the same
    four square symmetries and the same physical path checks.
    """
    if scene.object_held is not True or scene.eef_object is None:
        raise HarnessError('measured_in_hand_transform_required')
    current, hole, attachment = map(transform, (scene.base_eef, scene.base_hole, scene.eef_object))
    normal = hole[:3, 2]
    end = _alignment_endpoints(scene)[variant]
    rotations = Slerp([0, 1], Rotation.from_matrix(np.stack([current[:3, :3], end[:3, :3]])))(
        np.linspace(0, 1, 17)).as_matrix()
    object_point = (current @ attachment)[:3, 3]
    height = max(float(normal @ (object_point - hole[:3, 3])),
                 *(_minimum_object_height(scene, r, normal) for r in rotations))
    raised_point = object_point + normal * (height - float(normal @ (object_point - hole[:3, 3])))
    lift = _at_object_point(current[:3, :3], raised_point, attachment)
    oriented = _at_object_point(end[:3, :3], raised_point, attachment)
    above = _at_object_point(end[:3, :3], hole[:3, 3] + normal * height, attachment)
    goals = [lift, oriented, above, end]
    family = 'overhead_square_alignment'
    _check_route(scene, goals, variant, family=family)
    return dict(kind='motion', skill='align', stages=[dict(name='overhead_square_alignment',
        route_family=family, goals=[goal.tolist() for goal in goals], gripper=None,
        eef_object=attachment.tolist(), allow_target_contact=False)])
