"""Entity-level planning diagnostics; no pose invention or motion permission."""

POSE_FORMAT = dict(layout='row_major_4x4',translation_unit='metre',
    current_tcp_pose='T_world_tcp',object_pose='T_world_object',hole_pose='T_world_hole',
    nominal_grasp_tcp_pose='T_object_tcp',current_tcp_pose_object='T_object_tcp_current')


def tool_geometry(scene):
    """Describe the same registered collision envelope in the model's TCP frame."""
    import numpy as np
    from .geometry import transform
    from .skills import tool_margin
    tcp_eef = np.linalg.inv(transform(scene.eef_tip_reference))
    boxes = [dict(min=list(box['min']), max=list(box['max']),
                  pose=(tcp_eef @ transform(box.get('pose', np.eye(4)))).ravel().tolist())
             for box in (scene.tool_local_boxes or ())]
    return dict(frame='tcp', pose_format='T_tcp_box_row_major_4x4',
                translation_unit='metre', local_boxes=boxes,
                padding_m=tool_margin(scene),
                fallback_radius_m=scene.attached_radius_m if not boxes else None,
                purpose='registered collision envelope, not measured contact or grasp success')


FIXTURE_TRANSIT_SKILLS=frozenset(('approach_grasp','grasp','regrasp','transfer_above_hole','move_tcp','recover_grasp','release_supported'))


def fixture_available(task,observation):
    return bool(task.admission_profile=='development_coarse' and observation.fixture is not None)


def aperture_missing_reason(observation):
    for warning in reversed(observation.quality_warnings):
        if warning.startswith(('aperture_unavailable:', 'fixture_unavailable:')):
            return warning.split(':', 1)[1][:200]
    return 'not_observed'


def planning_entity_ids(task,observation,skill):
    from .local_environment import local_scope
    if local_scope(observation) and skill in ('move_tcp','reobserve'):
        return ()  # Independent local collision evidence is validated by the caller.
    if skill=='withdraw_reset':return (task.hole_id,)
    if task.goal=='approach_grasp' or skill in FIXTURE_TRANSIT_SKILLS and fixture_available(task,observation):
        return (task.object_id,)
    return (task.object_id,task.hole_id)


def entity_status(task, observation):
    from .planner_geometry import observation_warnings
    objects={o.object_id:o for o in observation.objects}
    required=[('target_object',task.object_id)]
    if task.goal!='approach_grasp':required.append(('aperture',task.hole_id))
    entities=[];missing=[]
    for role,identity in required:
        obj=objects.get(identity)
        reason='not_observed' if obj is None else 'pose_unavailable' if obj.pose is None else None
        if reason=='not_observed' and role=='aperture':
            reason=aperture_missing_reason(observation)
        row=dict(role=role,object_id=identity,visible=bool(obj and obj.visible),
            has_pose=bool(obj and obj.pose is not None),reason=reason)
        if obj is not None:
            row.update(held=obj.held,frame_id=obj.frame_id,
                evidence_ids=list(obj.evidence_ids),
                in_hand_transform_available=obj.eef_object_pose is not None,
                inserted=obj.inserted,stationary=obj.stationary)
        entities.append(row)
        if reason:missing.append(dict(role=role,object_id=identity,reason=reason,
            recovery='relocalize_target' if role=='target_object' else 'relocalize_fixture_and_aperture'))
    return dict(entities=entities,missing_entities=missing,
        quality_warnings=observation_warnings(observation.quality_warnings),
        measurement_received_ns=observation.received_ns,
        perception_boundary=dict(
            scope='registered_task_entities',
            shape_label_is_independent_classification=False,
            pose_is_physical_accuracy_certificate=False,
            unknown_held_is_not_empty=True,
            fixture_reference_is_aperture_measurement=False),
        geometry_status='partial' if missing else 'complete',
        fixture_collision_geometry_available=fixture_available(task,observation) or not any(m['role']=='aperture' for m in missing),
        fixture_reference=None if observation.fixture is None else observation.fixture.model_dump(),
        aperture_required_for=['align','insert','release','verify_placed'],
        fixture_transit_available=fixture_available(task,observation))
