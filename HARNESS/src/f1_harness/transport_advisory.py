"""Native transfer lookahead from a predicted grasp, never an executable plan."""
from dataclasses import replace
import numpy as np

from .contracts import HarnessError, MotionParameters
from .geometry import transform
from .owner_bridge import numerical_failure_detail
from .skills import candidate_variants


def native_transport_advisory(service,intent,scene,grasp,full,deadline_ns):
    from .skill_services import CANDIDATE_FAILURES
    report=dict(status='unavailable',predicted_attachment_only=True,
        source='predicted_native_grasp_endpoint',motion_authorized=False,
        actual_transfer_requires_new_measurements=True,checked_variants=[],feasible_variant=None)
    contact=next((s for s in grasp['geometry_trace'] if s.get('semantic_stage')=='grasp_close'),None)
    if contact is None:
        report['reason']='predicted_contact_stage_missing';return report
    contact_q=grasp['targets'][contact['end_index']-1]
    held=grasp['targets'][-1]
    contact_pose=transform(service.numerics.call('forward_pose',full22=full,held16=contact_q))
    endpoint=transform(service.numerics.call('forward_pose',full22=full,held16=held))
    attachment=np.linalg.inv(contact_pose)@transform(scene.base_object)
    loaded=replace(scene,base_eef=endpoint,base_object=endpoint@attachment,
        object_held=True,eef_object=attachment)
    following=intent.model_copy(update=dict(skill='transfer_above_hole',parameters=MotionParameters()))
    incomplete=False
    attempts=[(variant,False) for variant in candidate_variants('transfer_above_hole')]
    if service.fixture.get('joint_space_transport') is True:
        attempts.append((2,True))
    for variant,joint_search in attempts:
        if service.clock()>=deadline_ns:
            incomplete=True;report['reason']='preview_budget_remaining_insufficient';break
        try:
            planned=service._solve_candidate(following,loaded,held,full,variant,
                deadline_ns=deadline_ns,
                **({'joint_search':True} if joint_search else {}))
        except HarnessError as exc:
            detail=numerical_failure_detail(exc)
            # Advisory failure never becomes a rejection of this approach.
            # Transport/timeouts are unknown, not a proof of infeasibility.
            if detail['code'] not in CANDIDATE_FAILURES or detail['code'] in (
                    'bounded_IK_wall_time_exceeded','numerical_joint_search_wall_time_exceeded'):
                incomplete=True
            report['checked_variants'].append(dict(variant=variant,planned=False,diagnostic=detail))
            if joint_search:report['checked_variants'][-1]['planner']='joint_space_search'
            if incomplete:break
        else:
            report['checked_variants'].append(dict(variant=variant,planned=True,target_count=len(planned['targets'])))
            if joint_search:report['checked_variants'][-1]['planner']='joint_space_search'
            report.update(status='predicted_feasible',feasible_variant=variant)
            return report
    report['status']='incomplete' if incomplete else 'no_registered_route'
    return report
