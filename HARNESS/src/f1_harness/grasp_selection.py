"""Reconstruct the owner's completed approach choice against new measurements."""
from dataclasses import replace

from .contracts import HarnessError, digest
from .geometry import align_square_pose, transform
from .grasp_candidates import apply_descriptor


def selected_scene(service, scene, observation, selection, intent):
    """This context selects geometry only; the owner still authorizes each skill."""
    keys=('owner_instance_id','session_id','task_id','task_hash','epoch','calibration_id')
    if (not isinstance(selection,dict) or selection.get('schema')!='f1-owner-grasp-selection-v1'
            or any(selection.get(key)!=getattr(intent,key) for key in keys)
            or selection.get('task_hash')!=digest(service.task)
            or selection.get('object_id')!=service.task.object_id
            or type(selection.get('candidate_variant')) is not int
            or selection['candidate_variant'] not in range(4)):
        raise HarnessError('grasp_selection_scope_mismatch')
    obj=next((item for item in observation.objects if item.object_id==service.task.object_id),None)
    if obj is None or obj.pose is None or obj.frame_id!=selection.get('reference_object_frame_id'):
        raise HarnessError('grasp_selection_object_frame_mismatch')
    # A square's 90-degree coordinate convention can change between frames.
    # Preserve the executed convention, while retaining the newly measured
    # centre, normal and orientation within that symmetry.
    aligned,_=align_square_pose(obj.pose,transform(selection['reference_object_pose']))
    scene=replace(scene,base_object=service.frames.world_pose(aligned),
                  grasp_candidate_variant=selection['candidate_variant'])
    from .support_motion import retain_support_contact
    from .revalidation import tolerances
    scene=retain_support_contact(scene,service.frames.world_pose(selection['reference_object_pose']),
        maximum_change_m=tolerances(service.fixture,'approach_grasp')['position_m'],
        source_observation_id=selection.get('source_observation_id'))
    return apply_descriptor(scene,selection['descriptor'])


def recorded_scene(service, scene, observation, plan, intent):
    """Use the same selected geometry in tracking and independent revalidation."""
    from .motion_goals import apply_request
    scene=apply_request(service,scene,intent)
    if not plan.geometry_trace:return scene
    entry=plan.geometry_trace[0]
    if intent.skill=='approach_grasp':
        route=entry.get('approach_route','historical')
        if route not in ('direct','historical'):
            raise HarnessError('revalidation_approach_route_changed')
        scene=replace(scene,approach_route=route,
            pregrasp_adjustment_object_m=entry.get('pregrasp_adjustment_object_m'))
    selection=entry.get('grasp_selection')
    if selection is not None:
        return selected_scene(service,scene,observation,selection,intent)
    descriptor=entry.get('grasp_candidate_descriptor')
    if descriptor is None:return scene
    scene=apply_descriptor(scene,descriptor)
    return replace(scene,grasp_candidate_variant=entry.get('grasp_candidate_variant'))
