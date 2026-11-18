"""Bounded read-only evidence collection between model turns, never motion recovery."""
import json

# Only observation/planning failures benefit from automatic image collection.
# Authority, transport and unknown publication failures are not converted here.
VISUAL_REASONS=('mask_uncertain_requires_new_prompt','missing_geometry_entities','insufficient valid depth','object_not_visible','object_mask','grasp_held_evidence_missing',
    'stale_observation_measurements','scene_expired','numerical_plan_rejected','wrist_view')


def visual_failure(result):
    calls=result.get('primitive_calls',[]) if isinstance(result,dict) else []
    failed=[{k:c.get(k) for k in ('tool','kwargs','request_id','outcome','reason','error','verification_request_id','missing_entities','planning_diagnostic') if c.get(k) is not None}
        for c in calls if c.get('ok') is False or c.get('planned') is False or c.get('outcome') in ('pending_review','failure')]
    text=json.dumps(failed,ensure_ascii=False)+' '+str(result.get('exception',''))
    if any(x in text for x in ('authority_','feedback_expired','unknown_publication','connection_lost')):
        return None
    reason=next((r for r in VISUAL_REASONS if r in text),None)
    if reason is None:return None
    return dict(reason=reason,failed_calls=failed)


def visual_failure_key(diagnostic, completed_skills=()):
    """Same cause/strategy gets one automatic image pair per completed phase.

    New request IDs and observation IDs are bookkeeping, not a new visual
    failure. Explicit camera requests remain available to the model.
    """
    from .adapters import _planning_feedback
    def stable(value):
        if isinstance(value, dict):
            return {key:stable(item) for key,item in value.items() if key not in (
                'request_id','observation_id','scene_version','verification_request_id',
                'resume_request_id','position_error_m','rotation_error_rad','iterations')}
        if isinstance(value,list):return [stable(item) for item in value]
        return value
    calls=[]
    for row in diagnostic.get('failed_calls',[]):
        calls.append(dict(tool=row.get('tool'),kwargs=stable(row.get('kwargs',{})),
            outcome=row.get('outcome'),
            missing_entities=stable(row.get('missing_entities',[])),
            planning_diagnostic=stable(_planning_feedback(row.get('planning_diagnostic')))))
    return json.dumps(dict(reason=diagnostic['reason'],calls=calls,
                           completed_skills=sorted(completed_skills)),sort_keys=True)


def initial_plan_context(arm):
    """Keep geometry in the structured ledger, never in truncated stdout.

    F1Robot includes losslessly packed geometry for failed inspect_plan calls.
    The original full result remains available inside the persistent worker.
    """
    return arm.execute_tool('python',dict(code='''initial_plan = robo.inspect_plan('approach_grasp')
g = initial_plan.get('geometry', {})
''')).result


def gather_images(arm, store, *, diagnostic):
    # Goes through the existing tool/image path so pixel hints bind to the
    # actually shown frame, crop and task. No direct generic file/image access.
    code='''for camera in ("right", "head"):
    try:
        robo.show(camera)
        print("evidence_camera", camera)
    except Exception as error:
        print("evidence_camera_unavailable", camera, type(error).__name__)
'''
    if diagnostic.get('reason')=='numerical_plan_rejected':
        # A new route needs measured geometry, not another model turn just to
        # discover/read the existing interface. Keep it in the structured ledger
        # and in the persistent worker for the next conditional program.
        code+='''try:
    g = robo.geometry()
    print("route_geometry", "g contains current measured geometry; use inspect_plan with new route parameters")
except Exception as error:
    print("route_geometry_unavailable", type(error).__name__)
'''
    result=arm.execute_tool('python',dict(code=code))
    receipt=dict(diagnostic=diagnostic,tool_result=result.result,motion_requested=False)
    store.event('planner_diagnostic_bundle',**receipt)
    return result,receipt
