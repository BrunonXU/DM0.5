"""An observation motion checks empty and loaded hypotheses; neither is evidence."""
import numpy as np
from .contracts import HarnessError
from .geometry import transform
from .contact_departure import contact_exit_geometry_clear


def proposal(scene,variant=0):
    from .skills import check_motion_route
    if scene.object_held is not None or scene.eef_object is not None:
        raise HarnessError('grasp_observation_requires_unknown_attachment')
    current=transform(scene.base_eef);obj=transform(scene.base_object)
    distance=scene.approach_clearance_m
    if not isinstance(distance,(int,float)) or not np.isfinite(distance) or distance<=0:
        raise HarnessError('grasp_observation_distance_missing')
    # Reuse the registered normal lift distance. Preserve orientation and
    # closed gripper; this is an observation action, not repeated closure.
    direction=obj[:3,2]
    if variant:
        from .contact_departure import outward_contact_direction
        outward=outward_contact_direction(scene)
        if outward is None:raise HarnessError('scene_sweep_blocked:no_distinct_contact_exit')
        direction=direction+(1. if variant==1 else 2.)*outward
        norm=np.linalg.norm(direction)
        if norm<1e-10:raise HarnessError('scene_sweep_blocked:opposed_contact_exit')
        direction=direction/norm
    goal=current.copy();goal[:3,3]+=direction*distance
    hypothesis=np.linalg.inv(current)@obj
    if not contact_exit_geometry_clear(scene,[current,goal]):
        raise HarnessError('scene_sweep_blocked:uncertain_grasp_empty_path')
    check_motion_route(scene,[goal],eef_object=hypothesis,allow_target_contact=True,
        support_lift=True,skill='recover_grasp',route_family='dual_hypothesis_observation')
    return dict(kind='motion',skill='recover_grasp',stages=[dict(name='grasp_observation',
        goals=[goal.tolist()],gripper=None,eef_object=hypothesis.tolist(),allow_target_contact=True,
        support_contact_tolerance_m=scene.object_planning_margin_m,
        uncertain_grasp_observation=True,posture_reference_policy='measured_stage_start',
        route_family='dual_hypothesis_observation' if not variant else 'dual_hypothesis_outward_observation')])


def require_closed(fixture, held):
    target=fixture.get('grasp_opening');tolerance=fixture.get('gripper_tolerance')
    if (type(target) not in (int,float) or type(tolerance) not in (int,float)
            or not np.isfinite([target,tolerance]).all() or tolerance<=0
            or len(held)!=16 or not np.isfinite(held[15])
            or abs(held[15]-target)>tolerance):
        raise HarnessError('grasp_observation_requires_closed_gripper')


def check_actual_path(scene, poses, targets, held, fixture):
    """Bind the empty hypothesis and unchanged gripper to the actual IK path.

    Loaded geometry is checked separately by native IK and revalidation.
    The inferred attachment only describes a collision hypothesis.
    """
    require_closed(fixture,held)
    if any(q[15]!=held[15] for q in targets):
        raise HarnessError('actual_IK_grasp_observation_gripper_changed')
    if not contact_exit_geometry_clear(scene,poses):
        raise HarnessError('actual_IK_uncertain_grasp_departure_not_proven')
