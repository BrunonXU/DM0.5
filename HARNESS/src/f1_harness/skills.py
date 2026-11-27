"""Geometry-only semantic proposals. No device access and no guessed robot dimensions."""
from dataclasses import dataclass
import numpy as np

from .contracts import HarnessError
from .geometry import transform, eef_goal, swept_box_clear, tool_sweep_clear, attached_object_sweep_clear


@dataclass(frozen=True)
class SkillGeometry:
    base_eef: np.ndarray
    base_object: np.ndarray
    base_hole: np.ndarray | None
    eef_object: np.ndarray | None
    object_grasp_eef: np.ndarray
    # All distances/poses come from the measured fixture/calibration profile.
    approach_clearance_m: float
    transfer_clearance_m: float
    insertion_depth_m: float
    attached_radius_m: float
    uncertainty_m: float
    effective_clearance_m: float
    obstacles: tuple[dict, ...]
    grasp_opening: float
    release_opening: float
    goal: str = "placed"
    eef_tip_reference: np.ndarray | None = None
    maximum_approach_translation_m: float | None = None
    tool_local_boxes: tuple[dict, ...] | None = None
    admission_profile: str = "precision"
    planning_margin_m: float | None = None
    object_planning_margin_m: float | None = None
    object_local_box: dict | None = None
    object_held: bool | None = None
    # Current full-development scene pose is bottom centre, so this is explicitly 0.
    # Other registered object origins may state their height above the bottom.
    object_insertion_reference_height_m: float = 0.
    target_object_id: str | None = None
    # Registered free-space staging frame, expressed in the IK world. Its Z
    # axis defines the lift direction; its orientation is not a gripper goal.
    transfer_staging_pose: np.ndarray | None = None
    transfer_rotation_path: str = 'shortest'
    # Explicit opt-in: rotate a registered fraction during the free-space lift.
    # The held object's lift endpoint is preserved, not the EEF translation.
    transfer_lift_rotation_fraction: float = 0.
    approach_direction: str = 'vertical'
    grasp_height_policy: str = 'registered_point'
    pregrasp_distances_m: tuple[float, ...] | None = None
    success_trajectory: dict | None = None
    approach_route: str = 'direct'
    pregrasp_adjustment_object_m: tuple[float, ...] | None = None
    tool_padding_policy: str = 'required'
    # Explicitly version the semantic boundary. Historical plans retain their
    # old hole-aligned endpoint for reproduction; the live transport macro is
    # admitted by the fixture and leaves precise object orientation to align.
    transfer_policy: str = 'hole_aligned'
    # Registered pitch freedom about nominal local X at the same TCP. The
    # descriptor must be reapplied to the nominal scene for every replan.
    grasp_pitch_offsets_deg: tuple[float, ...] = ()
    prefer_registered_grasp_pitch: bool = False
    parameterized_grasp: bool = False
    grasp_candidate_descriptor: dict | None = None
    grasp_candidate_variant: int | None = None
    approach_direction_object: tuple[float, float, float] | None = None
    cartesian_request: dict | None = None
    aperture_measured: bool = True
    # Reporting only: convert internal planning poses to the API's native world.
    diagnostic_native_from_world: np.ndarray | None = None
    support_measurement: dict | None = None
    # Current measured occupancy/free-space evidence, independent of object pose.
    local_collision_evidence: object | None = None
    local_registered_environment: bool = False
    local_sweep_certificate: dict | None = None


def _positive(*values):
    return all(isinstance(v,(int,float,np.number)) and not isinstance(v,bool)
               and np.isfinite(v) and v>0 for v in values)


def stage_obstacles(scene, *, allow_target_contact=False):
    # Only the registered target becomes an allowed contact / attached body.
    # Neighbours, support surfaces and bucket geometry remain forbidden.
    return tuple(o for o in scene.obstacles if not (
        (allow_target_contact or scene.object_held is True) and scene.target_object_id is not None
        and o.get('role')=='target_object' and o.get('object_id')==scene.target_object_id))


def tool_margin(scene):
    if scene.tool_padding_policy not in ('required','advisory'):
        raise HarnessError('unregistered_tool_padding_policy')
    if scene.tool_padding_policy=='advisory':
        if scene.admission_profile!='development_coarse':
            raise HarnessError('tool_padding_advisory_requires_coarse_profile')
        return 0.
    return scene.planning_margin_m if scene.admission_profile=='development_coarse' else scene.uncertainty_m


def support_axis(scene):
    """Leave the measured table along its normal, independent of object long Z."""
    table=next((o for o in scene.obstacles if o.get('type')=='halfspace' and o.get('role')=='table'),None)
    rotation=transform(scene.base_object)[:3,:3]
    if table is None:return rotation[:,2]
    components=rotation.T@np.asarray(table['normal'],float)
    axis=int(np.argmax(np.abs(components)))
    return rotation[:,axis]*np.sign(components[axis])


def check_regrasp_support(scene):
    """A visible empty-grip target above the table is not an unattended pickup."""
    from itertools import product
    table=next((o for o in scene.obstacles if o.get('type')=='halfspace' and o.get('role')=='table'),None)
    if table is None or scene.object_local_box is None:
        raise HarnessError('regrasp_support_geometry_missing')
    box=scene.object_local_box
    corners=np.array(list(product(*zip(box['min'],box['max']))))
    pose=transform(scene.base_object)
    distances=((corners@pose[:3,:3].T+pose[:3,3])-np.asarray(table['point']))@np.asarray(table['normal'])
    tolerance=(scene.uncertainty_m or scene.planning_margin_m or 0.)+(scene.object_planning_margin_m or 0.)
    if abs(float(distances.min()))>tolerance:
        raise HarnessError('regrasp_object_not_on_registered_support')


def rebase_support_lift(stage,contact_pose,nominal_contact,object_pose):
    """Anchor a planned lift to its solved contact FK, never reset to an ideal pose.

    The attachment remains a collision prediction until real grasp verification.
    Revalidation reconstructs this same transformation independently from FK.
    """
    if stage.get('attachment_basis')!='predicted_for_collision_only':return stage
    actual=transform(contact_pose);offset=actual@np.linalg.inv(transform(nominal_contact))
    return dict(stage,goals=[(offset@transform(g)).tolist() for g in stage['goals']],
                eef_object=(np.linalg.inv(actual)@transform(object_pose)).tolist())


def _motion_clear(scene,start,end,eef_object=None,*,allow_target_contact=False,support_lift=False,
                  support_contact_direction='depart',measurement_envelope=False):
    coarse=scene.admission_profile=='development_coarse'
    margin=tool_margin(scene)
    obstacles=stage_obstacles(scene,allow_target_contact=allow_target_contact)
    if scene.tool_local_boxes:
        if not tool_sweep_clear(start,end,scene.eef_tip_reference,scene.attached_radius_m,
                               obstacles,margin_m=margin,local_boxes=scene.tool_local_boxes):return False
    elif not swept_box_clear(start[:3,3],end[:3,3],[scene.attached_radius_m]*3,
                             obstacles,uncertainty_m=margin):return False
    if eef_object is not None and scene.object_local_box is not None:
        object_margin=scene.object_planning_margin_m if coarse else margin
        if not attached_object_sweep_clear(start,end,eef_object,scene.object_local_box,
                                            tuple(o for o in obstacles if o.get('collision_scope')!='tool_only'),
                                            margin_m=object_margin,
                                            support_contact_tolerance_m=object_margin if support_lift else None,
                                            support_contact_direction=support_contact_direction):return False
    if getattr(scene,'local_collision_evidence',None) is not None:
        from .local_environment import local_sweep_evidence
        if not local_sweep_evidence(scene,start,end,measurement_envelope=measurement_envelope)['clear']:return False
    return True


def check_motion_route(scene, goals, *, start=None, eef_object=None,
                       allow_target_contact=False, support_lift=False,
                       support_contact_direction='depart',
                       skill=None, route_family=None, variant=0, stage_index=0,
                       code='scene_sweep_blocked'):
    """Use the ordinary sweep once per segment; explain a rejected segment."""
    from dataclasses import replace
    import json
    before=transform(scene.base_eef if start is None else start)
    options=dict(allow_target_contact=allow_target_contact,support_lift=support_lift,
                 support_contact_direction=support_contact_direction)
    for index,raw in enumerate(goals):
        after=transform(raw)
        obstacle_scene=scene
        if getattr(scene,'local_collision_evidence',None) is not None:
            from .local_environment import local_sweep_evidence
            local=local_sweep_evidence(scene,before,after)
            if not local['clear']:
                raise HarnessError('local_observation_space_unknown:'+json.dumps(
                    dict(diagnostic=dict(code='local_observation_space_unknown',metrics=local)),separators=(',',':')))
            obstacle_scene=replace(scene,local_collision_evidence=None,local_sweep_certificate=None)
        if not _motion_clear(obstacle_scene,before,after,eef_object,**options):
            metrics=dict(stage_index=stage_index,segment_index=index,goal_index=index,variant=variant)
            if skill is not None:metrics['skill']=skill
            if route_family is not None:metrics['route_family']=route_family
            for ordinal,obstacle in enumerate(stage_obstacles(scene,allow_target_contact=allow_target_contact)):
                single=replace(obstacle_scene,obstacles=(obstacle,))
                if _motion_clear(single,before,after,eef_object,**options):continue
                metrics.update(obstacle_index=ordinal,obstacle_role=obstacle.get('role'),
                    obstacle_name=obstacle.get('name',obstacle.get('fixture_part',obstacle.get('id'))),
                    collision_body='attached_object' if _motion_clear(single,before,after,None,**options) else 'tool')
                # Endpoint failures need a different pose; clear endpoints with
                # a blocked sweep need a different connecting route. Give the
                # planner this distinction without changing the sweep decision.
                start_clear=_motion_clear(single,before,before,eef_object,**options)
                end_clear=_motion_clear(single,after,after,eef_object,**options)
                metrics.update(start_pose_clear=start_clear,end_pose_clear=end_clear,
                    collision_location=('start_pose' if not start_clear else
                                        'end_pose' if not end_clear else 'sweep_envelope'))
                if metrics['collision_body']=='tool' and not end_clear:
                    from .collision_guidance import endpoint_clearance_hint
                    hint=endpoint_clearance_hint(scene,after,obstacle)
                    if hint is not None:metrics['endpoint_clearance_hint']=hint
                if scene.eef_tip_reference is not None and scene.diagnostic_native_from_world is not None:
                    tip=transform(scene.eef_tip_reference)
                    native=transform(scene.diagnostic_native_from_world)
                    metrics.update(segment_start_tcp_pose=(native@before@tip).ravel().tolist(),
                                   segment_end_tcp_pose=(native@after@tip).ravel().tolist())
                break
            raise HarnessError(code+':'+json.dumps(
                dict(diagnostic=dict(code=code,metrics=metrics)),separators=(',',':')))
        before=after

    if getattr(scene,'local_collision_evidence',None) is not None and goals:
        from .local_environment import require_departure_endpoint
        require_departure_endpoint(scene,before)


def _square_goals(object_goal,current,eef_object):
    candidates=[]
    for angle in (0,np.pi/2,np.pi,3*np.pi/2):
        c,s=np.cos(angle),np.sin(angle);symmetric=object_goal.copy()
        symmetric[:3,:3]=object_goal[:3,:3]@np.array([[c,-s,0],[s,c,0],[0,0,1.]])
        target=eef_goal(symmetric,eef_object)
        change=np.arccos(np.clip((np.trace(current[:3,:3].T@target[:3,:3])-1)/2,-1,1))
        candidates.append((change,target))
    return [target for _,target in sorted(candidates,key=lambda row:row[0])]


def _nearest_square_goal(object_goal,current,eef_object):
    return _square_goals(object_goal,current,eef_object)[0]


def validate_transfer_lift_fraction(value, *, staged=False):
    if type(value) not in (int,float) or value not in (0.,.33):
        raise ValueError('unregistered_transfer_lift_rotation_fraction')
    if value and staged:
        raise ValueError('coordinated_lift_incompatible_with_staging')
    return float(value)


def coordinated_lift(scene, lift, goal):
    """Retain the planned held-object position while turning toward its goal."""
    fraction=validate_transfer_lift_fraction(scene.transfer_lift_rotation_fraction,
        staged=scene.transfer_staging_pose is not None)
    if not fraction:return lift
    from scipy.spatial.transform import Rotation,Slerp
    result=lift.copy()
    result[:3,:3]=Slerp([0,1],Rotation.from_matrix(np.stack([
        scene.base_eef[:3,:3],goal[:3,:3]])))(fraction).as_matrix()
    # Rotating about the wrist without compensation could lower the block or
    # sweep it into the rim. Preserve its original planned lift endpoint.
    result[:3,3]=(lift@scene.eef_object)[:3,3]-result[:3,:3]@scene.eef_object[:3,3]
    return result


def _staged_transfer(scene, current, goal):
    """Keep the requested object pose while choosing an explicit wrist route.

    The shortest SO(3) interpolation can reach a joint limit even when the
    endpoint has a feasible wrist branch. A registered opposite rotation is
    split into sub-pi segments so the existing interpolator cannot silently
    collapse it back onto the short, infeasible branch. Every segment still
    passes geometry and the original constrained numerical solver.
    """
    from scipy.spatial.transform import Rotation
    staging=transform(scene.transfer_staging_pose)
    axis=staging[:3,2]
    retract=current.copy()
    retract[:3,3]=staging[:3,3]+axis*np.dot(current[:3,3]-staging[:3,3],axis)
    above=retract.copy();above[:3,3]=staging[:3,3]
    vector=Rotation.from_matrix(goal[:3,:3]@current[:3,:3].T).as_rotvec()
    angle=float(np.linalg.norm(vector))
    if scene.transfer_rotation_path=='opposite_shortest':
        if angle<1e-6:raise HarnessError('registered_long_rotation_axis_unavailable')
        vector*=1-2*np.pi/angle
    elif scene.transfer_rotation_path!='shortest':
        raise HarnessError('invalid_registered_transfer_rotation')
    count=max(1,int(np.ceil(np.linalg.norm(vector)/(np.pi*.75))))
    goals=[retract,above]
    for fraction in np.linspace(1/count,1,count):
        rotated=above.copy()
        rotated[:3,:3]=Rotation.from_rotvec(vector*fraction).as_matrix()@current[:3,:3]
        goals.append(rotated)
    lifted=goals[-1].copy()
    lifted[:3,3]+=axis*max(0.,float(np.dot(goal[:3,3]-lifted[:3,3],axis)))
    return goals+[lifted,goal]


def candidate_variants(skill):
    # Square symmetries or registered empty-tool return bends; same limits.
    if skill=='recover_grasp':return range(3)
    return range(4) if skill in ('approach_grasp','transfer_above_hole','align','withdraw_reset') else range(1)


def _clear_grasp_height(scene, goal):
    """Choose a grasp height inside the object instead of grazing table padding."""
    if scene.grasp_height_policy=='registered_point':return goal
    if scene.grasp_height_policy!='table_clearance_within_object' or scene.admission_profile!='development_coarse':
        raise HarnessError('unregistered_grasp_height_policy')
    from itertools import product
    obj=transform(scene.base_object);axis=support_axis(scene);tip=transform(scene.eef_tip_reference)
    reserve=scene.object_planning_margin_m
    shift=0.
    for obstacle in scene.obstacles:
        if obstacle.get('type')!='halfspace' or obstacle.get('role')!='table':continue
        normal=np.asarray(obstacle['normal'],float);point=np.asarray(obstacle['point'],float)
        ascent=float(axis@normal)
        if ascent<=0:raise HarnessError('grasp_height_axis_not_above_table')
        for box in scene.tool_local_boxes:
            corners=np.array(list(product(*zip(box['min'],box['max']))))
            offset=transform(box.get('pose',np.eye(4)))
            corners=corners@offset[:3,:3].T+offset[:3,3]
            world=corners@goal[:3,:3].T+goal[:3,3]
            distance=float(np.min((world-point)@normal))
            shift=max(shift,(tool_margin(scene)+reserve-distance)/ascent)
    result=goal.copy();result[:3,3]+=axis*shift
    contact=np.linalg.inv(obj)@result@tip
    contact_axis=int(np.argmax(np.abs(obj[:3,:3].T@axis)))
    if not scene.object_local_box['min'][contact_axis]+reserve-1e-9<=contact[contact_axis,3]<=scene.object_local_box['max'][contact_axis]-reserve+1e-9:
        raise HarnessError('grasp_height_outside_registered_object')
    return result


def grasp_route_lookahead(scene, proposal, *, placement_scene=None):
    """Check the predicted grasp/lift and a registered loaded transport route.

    Attachment here is a prediction for candidate selection only. Real transfer
    still requires independently verified in-hand evidence and a new plan.
    No IK result, motion authorization or success evidence is produced here.
    Registered transport separates a future insertion preview from the current
    grasp/lift/transport checks. Its warning cannot authorize later insertion.
    The historical hole-aligned policy retains its old full-chain requirement.
    An optional measured placement scene applies only after transport, matching
    the execution stage's geometry. It must not retrospectively relocate bucket
    obstacles for the already predicted coarse grasp and loaded transit.
    """
    from dataclasses import replace
    if (scene.goal!='placed' or scene.object_held is True or not proposal.get('stages')
            or proposal.get('kind')!='motion'
            or proposal.get('skill') not in ('approach_grasp','grasp','regrasp')):
        raise HarnessError('grasp_lookahead_scope_mismatch')
    if proposal['skill']=='approach_grasp':
        pregrasp=transform(proposal['stages'][-1]['goals'][-1])
        grasp=propose('grasp',replace(scene,base_eef=pregrasp))
    else:grasp=proposal
    contact_stage=next(stage for stage in grasp['stages'] if stage.get('allow_target_contact')
                       and stage['goals'] and stage.get('gripper')==scene.grasp_opening)
    contact=transform(contact_stage['goals'][-1])
    lifted=transform(grasp['stages'][-1]['goals'][-1])
    attachment=np.linalg.inv(contact)@transform(scene.base_object)
    loaded=replace(scene,base_eef=lifted,base_object=lifted@attachment,
                   eef_object=attachment,object_held=True)
    variants=[];placement_previews=[];transport_previews=[]
    separated=scene.transfer_policy=='registered_transport'
    for variant in candidate_variants('transfer_above_hole'):
        preview_skill='transfer_above_hole';alignment_attempts=[]
        try:
            transfer=propose('transfer_above_hole',loaded,variant=variant)
            above=transform(transfer['stages'][-1]['goals'][-1])
            current=replace(loaded,base_eef=above,base_object=above@attachment)
            if placement_scene is not None:
                current=replace(current,base_hole=placement_scene.base_hole,
                    obstacles=placement_scene.obstacles,
                    aperture_measured=placement_scene.aperture_measured)
            if separated:variants.append(variant)
            # Keep the prediction visible, without treating a predicted
            # attachment as the actual future insertion pose.
            for skill in ('align','insert','release'):
                preview_skill=skill
                if skill=='align' and separated:
                    # The transport family no longer selects the insertion
                    # symmetry. Search the independently registered align
                    # alternatives rather than silently requiring variant 0.
                    for alignment_variant in candidate_variants('align'):
                        try:
                            step=propose(skill,current,variant=alignment_variant)
                            from .transport import check_predicted_feed
                            check_predicted_feed(current,transform(step['stages'][-1]['goals'][-1]),
                                                 alignment_variant)
                            break
                        except HarnessError as exc:
                            if (str(exc) not in ('scene_sweep_blocked','no_clear_square_symmetry_route')
                                    and not str(exc).startswith('scene_sweep_blocked:')):
                                raise
                            from .owner_bridge import numerical_failure_detail
                            alignment_attempts.append(dict(variant=alignment_variant,
                                diagnostic=numerical_failure_detail(exc)))
                    else:
                        if any(a['diagnostic']['metrics'].get('route_family')=='aligned_insertion_preview'
                               for a in alignment_attempts):
                            preview_skill='insert'
                        raise HarnessError('no_clear_square_symmetry_route')
                else:step=propose(skill,current)
                goals=[g for stage in step['stages'] for g in stage['goals']]
                if goals:
                    endpoint=transform(goals[-1])
                    current=replace(current,base_eef=endpoint,base_object=endpoint@attachment)
            if not separated:variants.append(variant)
            else:placement_previews.append(dict(transfer_variant=variant,
                outcome='geometrically_clear',alignment_attempts=alignment_attempts))
        except HarnessError as exc:
            if (str(exc) not in ('scene_sweep_blocked','no_clear_square_symmetry_route',
                    'finite_scene_motion_parameters_required','registered_development_tool_object_geometry_required',
                    'insertion_feed_exceeds_registered_object_height','measured_aperture_required_for_engagement',
                    'destination_geometry_unavailable')
                    and not str(exc).startswith('scene_sweep_blocked:')):
                raise
            if separated and preview_skill=='transfer_above_hole':
                from .owner_bridge import numerical_failure_detail
                transport_previews.append(dict(transfer_variant=variant,outcome='advisory',
                    blocked_skill=preview_skill,reason=str(exc),diagnostic=numerical_failure_detail(exc)))
            if separated and preview_skill!='transfer_above_hole':
                from .owner_bridge import numerical_failure_detail
                placement_previews.append(dict(transfer_variant=variant,outcome='advisory',
                    blocked_skill=preview_skill,diagnostic=numerical_failure_detail(exc),
                    alignment_attempts=alignment_attempts))
    if not variants and not separated:raise HarnessError('grasp_has_no_placement_route')
    result=dict(predicted_attachment_only=True,numerical_feasibility_checked=False,
                checked_skills=['grasp','transfer_above_hole','align','insert','release'],
                transfer_candidate_variants=variants,predicted_eef_object=attachment.tolist())
    if separated:
        result.update(required_path_skills=['grasp'],
            transport_preview=dict(advisory_only=True,motion_authorized=False,
                status='predicted_clear' if variants else 'unresolved',candidates=transport_previews),
            precision_placement_preview=dict(advisory_only=True,motion_authorized=False,
                requires_fresh_in_hand_evidence=True,candidates=placement_previews))
    return result


def propose(skill: str, scene: SkillGeometry, *, variant=0, pregrasp_distance_m=None,
            _endpoint_only=False):
    # Revalidation compares an updated semantic endpoint with a frozen FK path.
    # It must not reject that path because a different, unused route is blocked.
    # This private mode is not an execution proposal or a collision proof.
    if _endpoint_only and (skill!='approach_grasp' or scene.goal!='placed'):
        raise HarnessError('endpoint_only_scope_mismatch')
    if type(variant) is not int or variant not in candidate_variants(skill):
        raise HarnessError('unregistered_candidate_variant')
    if pregrasp_distance_m is not None and skill!='approach_grasp':
        raise HarnessError('pregrasp_distance_scope_mismatch')
    if skill=='withdraw_reset':
        from .return_scene import ReturnGeometry
        if not isinstance(scene,ReturnGeometry):raise HarnessError('fixture_return_scope_required')
        return scene.proposal(variant=variant)
    if skill in ('align','insert','release') and not scene.aperture_measured:
        raise HarnessError('measured_aperture_required_for_engagement')
    if skill=='move_tcp':
        from .motion_goals import cartesian_proposal
        return cartesian_proposal(scene)
    if skill=='reobserve' and getattr(scene,'local_collision_evidence',None) is not None:
        return dict(kind='verify',skill=skill)
    if skill=='release_supported':
        from .support_motion import require_support_release
        require_support_release(scene)
        return dict(kind='motion',skill=skill,stages=[dict(name='release_on_support',
            goals=[],gripper=scene.release_opening,eef_object=None,
            support_measurement=scene.support_measurement)])
    if scene.goal == "approach_grasp":
        if skill == "reobserve":
            return dict(kind="verify", skill=skill)
        if skill != "approach_grasp":
            raise HarnessError("development_noncontact_scope_only")
        # Keep the measured EEF orientation and current gripper opening. The tip
        # reference is the coarse empty-tool reference, never a loaded block TCP.
        current, obj, tip = map(transform, (scene.base_eef, scene.base_object, scene.eef_tip_reference))
        maximum = scene.maximum_approach_translation_m
        if (maximum is None or not 0 < maximum <= .03 or not .08 <= scene.approach_clearance_m <= .3
                or not 0 < scene.uncertainty_m <= .05 or not 0 < scene.attached_radius_m <= .5):
            raise HarnessError("bounded_noncontact_development_geometry_required")
        target = current.copy()
        target[:3, 3] = obj[:3, 3] + [0, 0, scene.approach_clearance_m] - current[:3, :3] @ tip[:3, 3]
        delta = target[:3, 3] - current[:3, 3]
        distance = float(np.linalg.norm(delta))
        if distance > maximum:
            target[:3, 3] = current[:3, 3] + delta * maximum / distance
        # This first task is one bounded approach increment, not a grasp pose claim.
        if not tool_sweep_clear(current,target,tip,scene.attached_radius_m,
                                scene.obstacles,margin_m=scene.uncertainty_m,local_boxes=scene.tool_local_boxes):
            raise HarnessError("scene_sweep_blocked")
        return dict(kind="motion", skill=skill, partial_approach=True,
                    remaining_tip_distance_m=max(0.,distance-maximum),
                    stages=[dict(goals=[target.tolist()],gripper=None)])
    current, obj = map(transform, (scene.base_eef, scene.base_object))
    hole = None if scene.base_hole is None else transform(scene.base_hole)
    if skill in ('transfer_above_hole','align','insert','retreat') and hole is None:
        raise HarnessError('destination_geometry_unavailable')
    coarse=scene.admission_profile=='development_coarse'
    grasp_stage=skill in ('approach_grasp','grasp','regrasp','recover_grasp')
    parameters=(scene.approach_clearance_m,scene.attached_radius_m) if grasp_stage else (
        scene.approach_clearance_m,scene.transfer_clearance_m,scene.insertion_depth_m,scene.attached_radius_m)
    if not _positive(*parameters):
        raise HarnessError("finite_scene_motion_parameters_required")
    if coarse:
        if (not _positive(scene.planning_margin_m,scene.object_planning_margin_m)
                or scene.object_local_box is None or not scene.tool_local_boxes or scene.eef_tip_reference is None
                or (not grasp_stage and not np.isfinite(scene.object_insertion_reference_height_m))):
            raise HarnessError('registered_development_tool_object_geometry_required')
        box=scene.object_local_box
        height=float(box['max'][2]-box['min'][2])
        if not _positive(height) or (not grasp_stage and scene.insertion_depth_m>height):
            raise HarnessError('insertion_feed_exceeds_registered_object_height')
    elif not (_positive(scene.uncertainty_m,scene.effective_clearance_m)
              and scene.uncertainty_m<scene.effective_clearance_m):
        raise HarnessError("measured_scene_error_budget_required")
    if scene.transfer_policy not in ('hole_aligned','registered_transport'):
        raise HarnessError('unregistered_transfer_policy')
    if skill=='transfer_above_hole' and scene.transfer_policy=='registered_transport':
        from .transport import propose_transport
        return propose_transport(scene,variant)
    if skill=='align' and scene.transfer_policy=='registered_transport':
        from .transport import propose_alignment
        return propose_alignment(scene,variant)
    from .grasp_candidates import pitched
    pitch_candidate=pitched(scene)
    goals, gripper = [], None
    approach_options=[];selected_distance=None;pregrasp_adjustment=None
    if skill in ("verify_grasp", "verify_placed", "inspect_in_hand", "reobserve"):
        return dict(kind="verify", skill=skill)
    if skill=='recover_grasp':
        from .grasp_observation import proposal
        return proposal(scene,variant)
    if skill in ("approach_grasp", "grasp", "regrasp"):
        if skill=='regrasp' and scene.object_held is not False:
            raise HarnessError('regrasp_requires_confirmed_empty_gripper_or_registered_station')
        grasp=transform(scene.object_grasp_eef)
        grasp_goals=[]
        for angle in (0,np.pi/2,np.pi,3*np.pi/2):
            c,s=np.cos(angle),np.sin(angle);symmetry=np.eye(4)
            symmetry[:3,:3]=[[c,-s,0],[s,c,0],[0,0,1]]
            grasp_goals.append(obj@symmetry@grasp)
        if skill=='approach_grasp':goal=grasp_goals[variant]
        elif skill=='grasp' and scene.grasp_candidate_variant is not None:
            selected=scene.grasp_candidate_variant
            if type(selected) is not int or not 0<=selected<=3:
                raise HarnessError('invalid_bound_grasp_candidate_variant')
            goal=grasp_goals[selected]
        else:
            # Continue the approached square orientation instead of undoing a
            # successfully chosen equivalent grasp immediately above the block.
            goal=min(grasp_goals,key=lambda g:np.linalg.norm(g[:3,:3]-current[:3,:3]))
        goal=_clear_grasp_height(scene,goal)
        if skill == "approach_grasp":
            direction=support_axis(scene)
            if scene.approach_direction_object is not None:
                direction=obj[:3,:3]@np.asarray(scene.approach_direction_object,float)
                direction/=np.linalg.norm(direction)
            elif scene.approach_direction in ('from_current_horizontal','tool_axis_horizontal'):
                # Approach from the current side of the block at grasp height.
                # Both this pregrasp and the later grasp are fully swept below.
                tip=transform(scene.eef_tip_reference) if scene.eef_tip_reference is not None else np.eye(4)
                if scene.approach_direction=='tool_axis_horizontal':
                    if not pitch_candidate:raise HarnessError('tool_axis_approach_requires_registered_pitch')
                    direction=goal[:3,3]-(goal@tip)[:3,3]
                else:direction=(current@tip)[:3,3]-(goal@tip)[:3,3]
                direction-=obj[:3,2]*np.dot(direction,obj[:3,2])
                if np.linalg.norm(direction)<1e-6:raise HarnessError('approach_direction_unavailable')
                direction/=np.linalg.norm(direction)
            elif scene.approach_direction!='vertical':raise HarnessError('unregistered_approach_direction')
            distances=(scene.approach_clearance_m,) if scene.pregrasp_distances_m is None else scene.pregrasp_distances_m
            if (not isinstance(distances,(list,tuple)) or not 1<=len(distances)<=4
                    or any(not _positive(d) or (d>scene.approach_clearance_m and
                        (scene.grasp_candidate_descriptor or {}).get('strategy')!='object_tcp') for d in distances)
                    or len(set(distances))!=len(distances)):
                raise HarnessError('invalid_registered_pregrasp_distances')
            if pregrasp_distance_m is not None:
                if not _positive(pregrasp_distance_m) or pregrasp_distance_m not in distances:
                    raise HarnessError('unregistered_pregrasp_distance')
                distances=(pregrasp_distance_m,)
            for distance in distances:
                candidate=goal.copy();candidate[:3,3]+=direction*distance
                if scene.pregrasp_adjustment_object_m is not None:
                    shift=np.asarray(scene.pregrasp_adjustment_object_m,float)
                    if (shift.shape!=(3,) or not np.isfinite(shift).all()
                            or np.linalg.norm(shift)>scene.approach_clearance_m):
                        raise HarnessError('unregistered_pregrasp_adjustment')
                    candidate[:3,3]+=obj[:3,:3]@shift
                    pregrasp_adjustment=shift.tolist()
                approach_options.append((distance,candidate))
            selected_distance,goal=approach_options[0]
            gripper = scene.release_opening
        else:
            gripper = scene.grasp_opening
        goals.append(goal)
    elif skill in ("transfer_above_hole", "align", "insert"):
        if scene.eef_object is None or scene.object_held is not True:
            raise HarnessError("measured_in_hand_transform_required")
        object_goal = hole.copy()
        height = scene.object_insertion_reference_height_m + (
            -scene.insertion_depth_m if skill == "insert" else scene.transfer_clearance_m)
        object_goal[:3,3] += hole[:3,2] * height
        route_start=current
        if skill == "transfer_above_hole":
            validate_transfer_lift_fraction(scene.transfer_lift_rotation_fraction,
                staged=scene.transfer_staging_pose is not None)
            lifted = current.copy()
            current_object=current@transform(scene.eef_object)
            rise=max(scene.transfer_clearance_m,
                     float(np.dot(object_goal[:3,3]-current_object[:3,3],hole[:3,2])))
            lifted[:3,3] += hole[:3,2] * rise
            goals.append(lifted)
            route_start=lifted
        goal=None
        # Four geometrically equivalent square yaws can have different tool /
        # camera clearance. Check the future insertion while still above the lid.
        for candidate in _square_goals(object_goal,current,scene.eef_object)[variant:]:
            staged=(skill=='transfer_above_hole' and scene.transfer_staging_pose is not None)
            candidate_lift=(coordinated_lift(scene,lifted,candidate)
                if skill=='transfer_above_hole' and not staged else None)
            route=_staged_transfer(scene,current,candidate) if staged else [candidate]
            before=current if staged else candidate_lift if candidate_lift is not None else route_start
            clear=True
            for after in route:
                if not _motion_clear(scene,before,after,scene.eef_object):
                    clear=False;break
                before=after
            if not clear:continue
            if skill!='insert':
                future=candidate.copy()
                future[:3,3]-=hole[:3,2]*(scene.transfer_clearance_m+scene.insertion_depth_m)
                if not _motion_clear(scene,candidate,future,scene.eef_object):continue
            goal=candidate
            if staged:goals=route[:-1]
            elif candidate_lift is not None:goals=[candidate_lift]
            break
        if goal is None:raise HarnessError('no_clear_square_symmetry_route')
        goals.append(goal)
    elif skill == "retreat":
        goal=current.copy(); goal[:3,3] += hole[:3,2] * scene.transfer_clearance_m
        goals.append(goal)
    elif skill == "release":
        gripper=scene.release_opening
    else:
        raise HarnessError("unregistered_geometric_skill")
    attached=scene.eef_object if scene.object_held is True else None
    if skill=='approach_grasp' and scene.approach_route=='direct' and not _endpoint_only:
        direct=next(((d,g) for d,g in approach_options if _motion_clear(scene,current,g,attached)),None)
        if direct is None and scene.pregrasp_adjustment_object_m is None:
            from .collision_guidance import endpoint_clearance_hint
            target=next((o for o in scene.obstacles if o.get('role')=='target_object'
                and o.get('object_id')==scene.target_object_id),None)
            if target is not None:
                for distance,endpoint in approach_options:
                    if tool_sweep_clear(endpoint,endpoint,scene.eef_tip_reference,scene.attached_radius_m,
                            (target,),margin_m=tool_margin(scene),local_boxes=scene.tool_local_boxes):continue
                    hint=endpoint_clearance_hint(scene,endpoint,target)
                    if hint is None or hint['distance_m']>scene.approach_clearance_m:continue
                    adjusted=endpoint.copy();shift=np.asarray(hint['translation_m'])
                    adjusted[:3,3]+=obj[:3,:3]@shift
                    if _motion_clear(scene,current,adjusted,attached):
                        direct=(distance,adjusted);pregrasp_adjustment=shift.tolist();break
        if direct is not None:
            selected_distance,target=direct;goals=[target]
    if (skill=='approach_grasp' and coarse and scene.approach_route=='historical' and not _endpoint_only
            and scene.success_trajectory is None and not pitch_candidate):
        # Prefer any registered direct pregrasp over a large detour. Changing
        # this distance does not change the final grasp, lift or tool margins.
        direct=next(((d,g) for d,g in approach_options if _motion_clear(scene,current,g,attached)),None)
        if direct is not None:
            selected_distance,target=direct;goals=[target]
        else:
            # A pregrasp detour is relative to measured support/target geometry.
            # Do not require a future insertion frame merely to approach.
            normal=support_axis(scene) if hole is None else hole[:3,2]
            away=current[:3,3]-(obj if hole is None else hole)[:3,3]
            away-=normal*np.dot(away,normal)
            if np.linalg.norm(away)<1e-6:raise HarnessError('approach_detour_direction_unavailable')
            for distance,target in approach_options:
                back=current.copy();back[:3,3]+=away/np.linalg.norm(away)*scene.approach_clearance_m
                high=back.copy();high[:3,3]+=normal*max(0.,np.dot(target[:3,3]-back[:3,3],normal))
                oriented=high.copy();oriented[:3,:3]=target[:3,:3]
                route=[back,high,oriented,target]
                if all(_motion_clear(scene,a,b,attached) for a,b in zip([current]+route,route)):
                    selected_distance=distance;goals=route;break
            else:
                # The final registered detour was already rejected; retain its
                # actual blocked segment instead of reducing it to a bare code.
                check_motion_route(scene,route,eef_object=attached,skill=skill,
                    route_family='pregrasp_detour',variant=variant)
                raise HarnessError('scene_sweep_blocked')
    demo_references=None
    if (scene.success_trajectory and not _endpoint_only
            and (skill!='approach_grasp' or scene.approach_route=='historical')
            and not (pitch_candidate and skill in ('approach_grasp','grasp','regrasp'))):
        from .demonstration import retarget
        name={'approach_grasp':'approach','grasp':'grasp','transfer_above_hole':'transfer'}.get(skill)
        segment=scene.success_trajectory['segments'].get(name)
        if segment and goals:
            # Clear the bucket rim before the lateral transfer curve. Preserve
            # the required lift endpoint even when a shorter demo did not need it.
            if skill=='transfer_above_hole' and len(goals)>1:
                lift=goals[0]
                route,refs=retarget(segment,lift,goals[-1])
                goals=[lift]+route;demo_references=[segment['arm_deg'][0]]+refs
            else:goals,demo_references=retarget(segment,current,goals[-1])
    if skill!='regrasp' and not _endpoint_only:
        check_motion_route(scene,goals,eef_object=attached,allow_target_contact=skill=='grasp',
            skill=skill,route_family='demonstrated_'+skill if demo_references is not None else skill,
            variant=variant)
    if gripper is not None and not 0 <= gripper <= 100:
        raise HarnessError("invalid_f1_gripper_opening")
    stages=[]
    if skill=='regrasp':
        # Confirmed empty recovery: reopen before moving down to the new grasp.
        above=goals[-1].copy();above[:3,3]+=support_axis(scene)*scene.approach_clearance_m
        route_clear=_motion_clear(scene,current,above)
        if not _motion_clear(scene,above,goals[-1],allow_target_contact=True):
            raise HarnessError('regrasp_approach_sweep_blocked')
        if not route_clear:
            from .contact_departure import target_departure_clear
            # Open first, leave contact without rotating, then orient in clear
            # space. The actual IK path must independently prove this exit.
            exit_pose=current.copy();exit_pose[:3,3]+=support_axis(scene)*scene.approach_clearance_m
            if (not coarse or not target_departure_clear(scene,[current,exit_pose])
                    or not _motion_clear(scene,exit_pose,above)):
                raise HarnessError('regrasp_approach_sweep_blocked')
            stages.extend([dict(name='regrasp_open',goals=[],gripper=scene.release_opening,eef_object=None,
                                allow_target_contact=True,target_contact_hold=True),
                           dict(name='regrasp_exit',goals=[exit_pose.tolist()],gripper=None,eef_object=None,
                                allow_target_contact=True,target_contact_departure=True)])
        else:
            stages.append(dict(name='regrasp_open',goals=[],gripper=scene.release_opening,eef_object=None))
        stages.append(dict(name='regrasp_clearance',goals=[above.tolist()],gripper=None,eef_object=None))
    stages.append(dict(goals=[g.tolist() for g in goals],gripper=gripper,
                       eef_object=None if attached is None else transform(attached).tolist(),
                       allow_target_contact=skill in ('grasp','regrasp')))
    if skill=='approach_grasp':
        if scene.approach_route not in ('direct','historical'):
            raise HarnessError('unregistered_approach_route')
        stages[-1]['approach_route']=scene.approach_route
        if pregrasp_adjustment is not None:
            stages[-1]['pregrasp_adjustment_object_m']=pregrasp_adjustment
        if scene.approach_route=='direct':
            stages[-1]['posture_reference_policy']='measured_stage_start'
    if demo_references is not None:
        stages[-1]['posture_references_deg']=demo_references
        stages[-1]['trajectory_source']=scene.success_trajectory['source_run']
    if skill=='approach_grasp' and scene.pregrasp_distances_m:
        stages[-1]['pregrasp_distance_m']=selected_distance
    if skill in ('grasp','regrasp'):
        stages[-1]['name']='grasp_close'
        # Close first, then make a bounded lift. Visual grasp verification needs actual relative motion.
        lifted=goals[-1].copy();lifted[:3,3]+=support_axis(scene)*scene.approach_clearance_m
        expected_attached=np.linalg.inv(goals[-1])@obj
        lift_goals=[lifted];lift_references=None
        if scene.success_trajectory and scene.success_trajectory['segments'].get('lift'):
            from .demonstration import retarget
            # Keep the newly attached object upright until clear of its support.
            # Historical wrist pitch changes are not contact-safe in a new scene.
            lift_goals,lift_references=retarget(scene.success_trajectory['segments']['lift'],goals[-1],lifted,
                                               preserve_rotation=False)
            # The table is a contact constraint: retain the demo's lift progress,
            # but project away lateral/downward residuals until clear of support.
            origin=goals[-1][:3,3];axis=support_axis(scene);progress=0.
            for point in lift_goals:
                progress=max(progress,float(np.dot(point[:3,3]-origin,axis)))
                point[:3,3]=origin+axis*min(scene.approach_clearance_m,progress)
        check_motion_route(scene,lift_goals,start=goals[-1],eef_object=expected_attached,
            allow_target_contact=True,support_lift=True,skill=skill,route_family='grasp_lift',
            variant=variant,stage_index=len(stages),code='grasp_lift_sweep_blocked')
        stages.append(dict(name='grasp_lift',goals=[g.tolist() for g in lift_goals],gripper=None,eef_object=expected_attached.tolist(),
                           attachment_basis='predicted_for_collision_only',allow_target_contact=True,
                           support_contact_tolerance_m=scene.object_planning_margin_m if coarse else scene.uncertainty_m))
        if lift_references is not None:
            stages[-1].update(posture_references_deg=lift_references,trajectory_source=scene.success_trajectory['source_run'])
    if scene.tool_padding_policy=='advisory' and not _endpoint_only:
        from dataclasses import replace
        padded=replace(scene,tool_padding_policy='required');before=current
        for stage in stages:
            misses=[]
            for i,raw in enumerate(stage['goals']):
                after=transform(raw)
                if not _motion_clear(padded,before,after,stage.get('eef_object'),
                    allow_target_contact=stage.get('allow_target_contact',False),
                    support_lift=stage.get('support_contact_tolerance_m') is not None):misses.append(i)
                before=after
            if misses:stage['padding_advisory']=dict(nominal_tool_margin_m=scene.planning_margin_m,
                goal_indices=misses,actual_geometry_checked=True,scope='semantic_segments')
    return dict(kind="endpoint" if _endpoint_only else "motion", skill=skill, stages=stages)
