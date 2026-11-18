"""Audit emitted approach/alignment/insertion/release commands against their scene.

Contact grasp, uncertain observation and empty return have separate auditors.
No control permission or measured physical success is inferred here.
"""
from dataclasses import replace
import numpy as np
from f1_harness.command_forecast import sweep_anchor
from f1_harness.contracts import HarnessError, Observation, SkillIntent, digest
from f1_harness.geometry import transform, tool_margin_departure_clear, tool_sweep_clear
from f1_harness.grasp_selection import recorded_scene
from f1_harness.owner_bridge import JointProposal
from f1_harness.skills import _motion_clear, stage_obstacles, tool_margin

COVERED=frozenset(('approach_grasp','align','insert','release','move_tcp'))
READ_ONLY=frozenset(('verify_grasp','inspect_in_hand','verify_placed','reobserve'))


def local_endpoint_padding_status(scene,end):
    """Report the original padding separately from the policy's motion contract."""
    from f1_harness.local_environment import departure_plane
    evidence=getattr(scene,'local_collision_evidence',None)
    if evidence is None:return None
    plane=departure_plane(evidence)
    actual=transform(scene.diagnostic_native_from_world)@transform(end)
    return dict(diagnostic_only=True,tool_padding_policy=scene.tool_padding_policy,
        configured_tool_padding_m=scene.planning_margin_m,effective_tool_margin_m=tool_margin(scene),
        target_plane_available=plane is not None,
        target_plane_padding_restored=(None if plane is None else tool_sweep_clear(actual,actual,
            scene.eef_tip_reference,scene.attached_radius_m,(plane,),margin_m=scene.planning_margin_m,
            local_boxes=scene.tool_local_boxes)))


def stationary_release(rows,opening,closed):
    if not rows:raise ValueError('release_output_missing')
    anchor=sweep_anchor(rows)
    if anchor[15]!=closed:raise ValueError('release_must_start_closed')
    q=np.asarray([r['submitted16'] for r in rows]);ref=np.asarray([r['reference16'] for r in rows])
    if (not np.all(q[:,:15]==anchor[:15]) or not np.all(ref[:,:15]==anchor[:15])):
        raise ValueError('release_translated_or_rotated_arm')
    values=q[:,15];changes=np.flatnonzero(values!=closed)
    if not len(changes) or not np.all(values[changes[0]:]==opening):
        raise ValueError('release_opening_missing_or_reclosed')
    return dict(arm_command_held=True,arm_reference_held=True,release_once=True)


def readonly_outputs(outputs,stages):
    checks=[]
    for item in stages:
        result=item.get('result',{})
        rejected_without_motion=(result.get('lifecycle')=='completed' and result.get('no_motion') is True
            and result.get('outcome') in ('failure','pending_review'))
        if (item['skill'] not in READ_ONLY and not item['request'].get('verification_request_id')
                and not rejected_without_motion):continue
        request=item['request']['request_id'];rows=[r for r in outputs if r['request_id']==request]
        if not rows:raise ValueError('read_only_output_missing')
        anchor=sweep_anchor(rows)
        checks.append(dict(request_id=request,skill=item['skill'],rows=len(rows),
            declared_zero_motion_rejection=rejected_without_motion,
            passed=not any(r['new_motion_reference'] for r in rows)
                and all(np.array_equal(r['submitted16'],anchor) for r in rows)))
    return checks


def sweep_ordinary(outputs,events,plans,service,numeric):
    results=[]
    for planned in plans:
        skill=planned['skill']
        if skill not in COVERED:continue
        request=planned['request_id'];plan=planned['proposal']
        rows=[r for r in outputs if r['request_id']==request]
        if not rows:continue
        start=next(e for e in events if e['kind']=='harness_planning_started' and e['request_id']==request)
        publication_index=next((i for i,e in enumerate(events)
            if e['kind']=='harness_skill_first_publication' and e.get('request_id')==request),None)
        proofs=[e['proof'] for i,e in enumerate(events) if e['kind']=='harness_plan_revalidated'
            and e['proof']['request_id']==request and (publication_index is None or i<publication_index)]
        if not proofs:raise ValueError('ordinary_output_revalidation_proof_missing')
        proof=proofs[-1]
        if proof['candidate_sha256']!=digest(plan):raise ValueError('ordinary_plan_proof_mismatch')
        obs=Observation.model_validate(proof['observation']);full=start['full_reference']
        if obs.local_collision_evidence is not None and publication_index is None:
            raise ValueError('local_output_first_publication_missing')
        before=sweep_anchor(rows)
        reference=start.get('held_reference')
        if obs.local_collision_evidence is not None and reference is None:
            raise ValueError('local_output_planning_reference_missing')
        if reference is None:reference=obs.state16
        if plan.get('trajectory') is not None and not np.array_equal(reference,plan['trajectory']['anchor']):
            raise ValueError('ordinary_output_planning_reference_mismatch')
        scene=service._scene(obs,full,reference,skill=skill)
        scene=recorded_scene(service,scene,obs,JointProposal(**plan),SkillIntent.model_validate(start['intent']))
        checks={}
        if skill=='release':checks.update(stationary_release(rows,scene.release_opening,scene.grasp_opening))
        elif skill=='approach_grasp':
            # Approach may set the registered open value at its last stage.
            # Both initial and commanded openings remain recorded; it cannot
            # close to the grasp value or toggle back after this adjustment.
            openings=np.asarray([r['submitted16'][15] for r in rows])
            changes=np.flatnonzero(openings!=before[15])
            if len(changes) and not np.all(openings[changes[0]:]==scene.release_opening):
                raise ValueError('unexpected_approach_gripper_change')
            checks['only_registered_open_adjustment']=True
        elif any(r['submitted16'][15]!=before[15] for r in rows):
            raise ValueError('unexpected_gripper_change_during_'+skill)
        attachment=scene.eef_object if scene.object_held is True else None
        if skill in ('align','insert','release') and attachment is None:
            raise ValueError('engagement_attachment_evidence_missing')
        fk=lambda q:transform(numeric.dispatch('forward_pose',dict(full22=full,held16=q.tolist())))
        previous=fk(before);failures=[];samples=0
        for row in rows:
            target=np.asarray(row['submitted16'],float)
            steps=max(1,int(np.ceil(np.max(abs(target[:14]-before[:14]))/.1)))
            for fraction in np.linspace(0,1,steps+1)[1:]:
                q=before+fraction*(target-before)
                try:
                    numeric.numerics.mapping.check_action(q);numeric.numerics.gate.check_preventive(q,full)
                except Exception as exc:failures.append(dict(kind='native',reason=str(exc),virtual_ns=row['virtual_ns']))
                current=fk(q)
                clear=_motion_clear(scene,previous,current,attachment)
                # Same pre-contact allowance as revalidation, only until the
                # first emitted interval has restored full target clearance.
                departure=(not clear and samples==0 and skill=='approach_grasp'
                    and attachment is None and scene.object_held is not True
                    and tool_margin_departure_clear(previous,current,scene.eef_tip_reference,
                        scene.attached_radius_m,stage_obstacles(scene),margin_m=scene.planning_margin_m,
                        local_boxes=scene.tool_local_boxes,target_object_id=scene.target_object_id))
                if not clear and not departure:
                    failures.append(dict(kind='environment',virtual_ns=row['virtual_ns'],fraction=float(fraction)))
                if skill=='release':
                    # Check both sides of the transition. Tool must hold its
                    # arm command; expected finger contact is allowed only with
                    # this registered target, never another obstacle.
                    empty=replace(scene,object_held=False,eef_object=None)
                    if not _motion_clear(empty,previous,current,allow_target_contact=True):
                        failures.append(dict(kind='release_empty_tool',virtual_ns=row['virtual_ns']))
                previous=current;samples+=1
            before=target
        padding_diagnostic=None
        if skill=='move_tcp' and getattr(scene,'local_collision_evidence',None) is not None:
            from f1_harness.local_environment import require_departure_endpoint
            try:
                require_departure_endpoint(scene,previous)
                checks['local_endpoint_contract_satisfied']=True
            except HarnessError as exc:
                checks['local_endpoint_contract_satisfied']=False
                failures.append(dict(kind='local_departure_endpoint',reason=str(exc),
                    virtual_ns=rows[-1]['virtual_ns']))
            padding_diagnostic=local_endpoint_padding_status(scene,previous)
        checks['native_and_environment_clear']=not failures
        results.append(dict(request_id=request,skill=skill,samples=samples,checks=checks,
            passed=all(checks.values()),failures=failures,evidence_scope='simulated_command_path',
            local_padding_diagnostic=padding_diagnostic,
            release_geometry_limit='Static registered tool envelope; no jaw deformation or force simulation.' if skill=='release' else None))
    return results
