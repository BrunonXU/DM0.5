"""Free-space path generation for existing semantic goals, never a contact strategy."""
from .contracts import HarnessError
from .geometry import transform


ROUTES = {'approach_grasp': 'joint_space_approach', 'move_tcp': 'joint_space_tcp'}
SEARCHABLE_FAILURES = frozenset((
    'scene_sweep_blocked', 'actual_IK_path_tool_sweep_blocked',
    'actual_IK_path_scene_sweep_blocked', 'actual_IK_path_attached_object_sweep_blocked',
    'actual_IK_candidate_collision_rejected', 'bounded_IK_did_not_reach_goal',
    'bounded_IK_wall_time_exceeded', 'planning_joint_envelope_exceeded',
))


def eligible(fixture, skill, scene):
    if fixture.get('joint_space_free_motion') is not True or skill not in ROUTES:
        return False
    if scene.goal != 'placed' or scene.admission_profile != 'development_coarse':
        return False
    if skill == 'approach_grasp':
        return scene.object_held is not True and scene.eef_object is None
    request = scene.cartesian_request or {}
    return len(request.get('goals', ())) == 1 and not request.get('support_contact', False)


def propose_joint_motion(scene, skill, *, variant=0, pregrasp_distance_m=None):
    """Preserve the selected goal while letting OMPL choose its connecting path.

    Approach opens in place before free-space travel. Contact/grasp closure and
    mandatory via poses keep their existing implementations and are not inferred.
    """
    from .skills import propose, check_motion_route
    if skill == 'approach_grasp':
        if scene.object_held is True or scene.eef_object is not None:
            raise HarnessError('joint_approach_requires_unattached_tool')
        original = propose(skill, scene, variant=variant,
                           pregrasp_distance_m=pregrasp_distance_m, _endpoint_only=True)
        stage = dict(original['stages'][-1])
        if len(stage['goals']) != 1 or stage.get('allow_target_contact'):
            raise HarnessError('joint_approach_requires_free_space_endpoint')
        goal = stage['goals'][0]
        check_motion_route(scene, [goal], start=goal, skill=skill,
                           route_family=ROUTES[skill], variant=variant)
        opening = dict(name='pregrasp_open', goals=[], gripper=scene.release_opening,
                       eef_object=None, allow_target_contact=False)
        if stage.get('pregrasp_distance_m') is not None:
            opening['pregrasp_distance_m'] = stage['pregrasp_distance_m']
        stage.update(name='pregrasp_joint_motion', route_family=ROUTES[skill],
                     gripper=None, eef_object=None, allow_target_contact=False)
        return dict(kind='motion', skill=skill, stages=[opening, stage])
    if skill != 'move_tcp':
        raise HarnessError('unregistered_joint_motion_skill')
    request = scene.cartesian_request or {}
    if len(request.get('goals', ())) != 1 or request.get('support_contact', False):
        raise HarnessError('joint_tcp_requires_single_free_space_goal')
    if scene.object_held is True and scene.eef_object is None:
        raise HarnessError('measured_in_hand_transform_required')
    attached = scene.eef_object if scene.object_held is True else None
    goal = request['goals'][0]
    check_motion_route(scene, [goal], start=goal, eef_object=attached,
                       skill=skill, route_family=ROUTES[skill], variant=variant)
    return dict(kind='motion', skill=skill, stages=[dict(name='free_space_tcp',
        goals=[goal], gripper=None, eef_object=None if attached is None else transform(attached).tolist(),
        allow_target_contact=False, route_family=ROUTES[skill],
        posture_reference_policy='measured_stage_start')])


def uses_joint_motion(fixture, intent, proposal):
    from .transport import uses_joint_transport
    if uses_joint_transport(fixture, intent, proposal):
        return True
    marked = [entry for entry in proposal.geometry_trace if entry.get('route_family') in ROUTES.values()]
    if not marked:
        return False
    expected = 2 if intent.skill == 'approach_grasp' else 1
    if (fixture.get('joint_space_free_motion') is not True or intent.skill not in ROUTES
            or len(marked) != 1 or marked[0].get('route_family') != ROUTES[intent.skill]
            or len(proposal.geometry_trace) != expected
            or type(proposal.candidate_variant) is not int
            or proposal.candidate_variant not in (range(4) if intent.skill == 'approach_grasp' else range(1))):
        raise HarnessError('revalidation_joint_motion_contract_changed')
    return True


def reconstruction(scene, intent, proposal, *, pregrasp_distance_m=None):
    if intent.skill == 'transfer_above_hole':
        from .transport import joint_transport_goal
        offset = proposal.geometry_trace[0].get('goal_region_offset_m', [0., 0.])
        yaw_deg = proposal.geometry_trace[0].get('goal_yaw_offset_deg', 0)
        return joint_transport_goal(scene, [offset], yaw_offset_deg=yaw_deg)
    return propose_joint_motion(scene, intent.skill, variant=proposal.candidate_variant,
                                pregrasp_distance_m=pregrasp_distance_m)


def is_search_stage(stage):
    return stage.get('route_family') in (*ROUTES.values(), 'joint_space_transport')


def search_bounds(scene, stage):
    from .skills import stage_obstacles, tool_margin
    bounds = dict(joint_space_search=True, free_space_motion=True,
        attached_radius_m=scene.attached_radius_m, obstacles=stage_obstacles(scene),
        uncertainty_m=tool_margin(scene), tool_local_boxes=scene.tool_local_boxes,
        eef_tip_reference=None if scene.eef_tip_reference is None else transform(scene.eef_tip_reference).tolist())
    if stage.get('eef_object') is not None:
        bounds.update(eef_object=stage['eef_object'], object_local_box=scene.object_local_box,
            object_planning_margin_m=scene.object_planning_margin_m
                if scene.admission_profile == 'development_coarse' else scene.uncertainty_m)
    return bounds
