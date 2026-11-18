"""Offline command-path audit for grasp contact/closure/support lift.

The attachment is hypothetical at commanded closure, never grasp evidence.
Uses the registered contact allowances, not free-space collision semantics.
"""
import numpy as np
from itertools import product
from f1_harness.contracts import Observation,SkillIntent,digest
from f1_harness.owner_bridge import JointProposal
from f1_harness.grasp_selection import recorded_scene
from f1_harness.geometry import transform
from f1_harness.command_forecast import sweep_anchor
from f1_harness.skills import _motion_clear
from f1_harness.contact_departure import target_departure_clear
from f1_harness.revalidation import pose_error,tolerances
from support_plane_diagnostic import plane_clearance


def support_diagnostics(scene,start,end,attachment):
    """Explain a rejected sweep without changing its original outcome."""
    corners=np.asarray(list(product(*zip(scene.object_local_box['min'],scene.object_local_box['max']))))
    local=corners@attachment[:3,:3].T+attachment[:3,3]
    return [dict(plane_clearance(start,end,local,obstacle['normal'],obstacle['point']),
                 registered_object_margin_m=scene.object_planning_margin_m)
            for obstacle in scene.obstacles
            if obstacle.get('type')=='halfspace' and obstacle.get('role')=='table']


def grasp_phases(rows,plan,events,request_id,opening,closed,*,skill='grasp',complete=True):
    if not rows:raise ValueError('grasp_outputs_missing')
    if any(r.get('simulation') is not True for r in rows):raise ValueError('offline_command_evidence_required')
    stamps=[r['virtual_ns'] for r in rows]
    if any(type(s) is not int for s in stamps) or any(b<=a for a,b in zip(stamps,stamps[1:])):
        raise ValueError('grasp_output_clock_invalid')
    stages=plan['geometry_trace'];names=[stage.get('semantic_stage') for stage in stages]
    allowed=([['grasp_close','grasp_lift']] if skill=='grasp' else
        [['regrasp_open','regrasp_clearance','grasp_close','grasp_lift'],
         ['regrasp_open','regrasp_exit','regrasp_clearance','grasp_close','grasp_lift']])
    if (skill not in ('grasp','regrasp') or names not in allowed or stages[0]['start_index']!=0
            or any(type(s.get('start_index')) is not int or type(s.get('end_index')) is not int
                   or s['end_index']<=s['start_index'] for s in stages)
            or any(a['end_index']!=b['start_index'] for a,b in zip(stages,stages[1:]))
            or stages[-1]['end_index']!=len(plan['targets'])):
        raise ValueError('unregistered_grasp_stage_topology')
    initial=np.asarray(plan['trajectory']['anchor'],float)
    if initial.shape!=(16,) or not np.isfinite(initial).all():raise ValueError('invalid_grasp_anchor')
    if skill=='grasp' and initial[15]!=opening:raise ValueError('grasp_must_begin_open')
    if initial[15] not in (opening,closed):raise ValueError('unregistered_initial_gripper')
    q=np.asarray([r['submitted16'] for r in rows],float)
    if q.shape!=(len(rows),16) or not np.isfinite(q).all():raise ValueError('invalid_grasp_commands')
    if not np.isin(q[:,15],[opening,closed]).all():raise ValueError('unregistered_gripper_command')
    arrivals=[e for e in events if e.get('kind')=='harness_measured_waypoint' and e.get('request_id')==request_id]
    if (len({e['index'] for e in arrivals})!=len(arrivals)
            or any(type(e.get('monotonic_ns')) is not int for e in arrivals)):
        raise ValueError('ambiguous_grasp_arrival_evidence')
    ends=[next((e['monotonic_ns'] for e in arrivals if e['index']==stage['end_index']-1),None)
          for stage in stages]
    if complete and any(stamp is None for stamp in ends):
        missing=names[next(i for i,stamp in enumerate(ends) if stamp is None)]
        raise ValueError('closure_arrival_missing' if missing=='grasp_close' else
                         'lift_arrival_missing' if missing=='grasp_lift' else 'regrasp_stage_arrival_missing')
    for index,stamp in enumerate(ends):
        if stamp is not None and any(previous is None or previous>=stamp for previous in ends[:index]):
            raise ValueError('grasp_stage_arrival_order_invalid')
    semantic=[]
    for stamp in stamps:
        index=0
        while index<len(stages)-1 and ends[index] is not None and stamp>ends[index]:index+=1
        semantic.append(names[index])
    open_rows=np.flatnonzero(q[:,15]==opening)
    opening_row=-1 if initial[15]==opening else int(open_rows[0]) if len(open_rows) else None
    if opening_row is None:
        if complete:raise ValueError('regrasp_never_opened')
        closure=None
    else:
        changes=np.flatnonzero(q[max(0,opening_row):,15]!=opening)
        closure=int(changes[0])+max(0,opening_row) if len(changes) else None
    if closure is None and complete:raise ValueError('grasp_never_closed')
    if closure is not None and not np.all(q[closure:,15]==closed):
        raise ValueError('gripper_reopened_or_unregistered_closure')
    closure_ns=stamps[closure] if closure is not None else None
    close_stage=stages[names.index('grasp_close')]
    closure_index=close_stage['end_index']-1
    before=next((e for e in arrivals if e['index']==closure_index-1),None)
    closed_arrival=next((e for e in arrivals if e['index']==closure_index),None)
    lifted=next((e for e in arrivals if e['index']==len(plan['targets'])-1),None)
    drift=None
    if closure is not None:
        if before is None or before['monotonic_ns']>=closure_ns or semantic[closure]!='grasp_close':
            raise ValueError('closure_before_precontact_arrival')
        if closed_arrival is not None and closed_arrival['monotonic_ns']<closure_ns:
            raise ValueError('closure_arrival_missing')
        hold_end=closed_arrival['monotonic_ns'] if closed_arrival else stamps[-1]
        hold=[i for i,stamp in enumerate(stamps) if closure_ns<=stamp<=hold_end]
        references=np.asarray([rows[i]['reference16'] for i in hold],float)
        if (references.shape!=(len(hold),16) or not np.isfinite(references).all()
                or not np.all(references[:,7:14]==references[0,7:14])):
            raise ValueError('arm_reference_changed_during_closure')
        drift=float(np.max(np.ptp(q[hold,7:14],axis=0)))
    elif closed_arrival is not None or lifted is not None:
        raise ValueError('grasp_arrival_without_closure_command')
    for index,name in enumerate(semantic):
        if name in ('regrasp_exit','regrasp_clearance') and q[index,15]!=opening:
            raise ValueError('regrasp_travel_requires_open_gripper')
        if name=='grasp_lift' and (closure is None or closed_arrival is None or q[index,15]!=closed):
            raise ValueError('lift_before_confirmed_closure')
    if skill=='regrasp':
        hold=[i for i,name in enumerate(semantic) if name=='regrasp_open']
        reference=np.asarray([rows[i]['reference16'] for i in hold],float)
        if (hold and (reference.shape!=(len(hold),16) or not np.isfinite(reference).all()
                or not np.all(reference[:,:15]==initial[:15]))):
            raise ValueError('arm_reference_changed_during_reopen')
        if stages[0].get('target_contact_hold') and hold and not np.allclose(q[hold,:15],initial[:15],atol=1e-9,rtol=0):
            raise ValueError('arm_command_moved_during_contact_reopen')
    phases=[name if name.startswith('regrasp_') else
            'preclosure' if closure_ns is None or stamp<closure_ns else
            'closure_hold' if closed_arrival is None or stamp<=closed_arrival['monotonic_ns'] else 'support_lift'
            for stamp,name in zip(stamps,semantic)]
    return dict(closure_row=closure,closure_ns=closure_ns,
        closure_arrival_ns=closed_arrival['monotonic_ns'] if closed_arrival else None,
        lift_arrival_ns=lifted['monotonic_ns'] if lifted else None,
        precontact_arrival_ns=before['monotonic_ns'] if before else None,
        closure_command_arm_drift_deg=drift,phases=phases,semantic_stages=semantic,
        plan_completed=all(stamp is not None for stamp in ends),
        stage_completion=[dict(semantic_stage=name,arrival_ns=stamp,completed=stamp is not None)
                          for name,stamp in zip(names,ends)])


def _sweep_request(rows,events,planned,service,numeric):
    request=planned['request_id'];skill=planned['skill'];plan=planned['proposal']
    terminal=[e['result'] for e in events if e.get('kind')=='skill_result'
              and e.get('result',{}).get('request_id')==request]
    if len(terminal)!=1:raise ValueError('grasp_terminal_result_missing_or_ambiguous')
    outcome=terminal[0]['outcome']
    start=next(e for e in events if e.get('kind')=='harness_planning_started' and e.get('request_id')==request)
    proofs=[e['proof'] for e in events if e.get('kind')=='harness_plan_revalidated'
            and e.get('proof',{}).get('request_id')==request]
    if len(proofs)!=1 or proofs[0]['candidate_sha256']!=digest(plan):raise ValueError('grasp_plan_proof_mismatch')
    proof=proofs[0]
    phases=grasp_phases(rows,plan,events,request,service.fixture['release_opening'],
                        service.fixture['grasp_opening'],skill=skill,complete=outcome=='success')
    full=start['full_reference'];before=sweep_anchor(rows)
    observation=Observation.model_validate(proof['observation'])
    scene=service._scene(observation,full,before.tolist(),skill=skill)
    scene=recorded_scene(service,scene,observation,JointProposal(**plan),SkillIntent.model_validate(start['intent']))
    stages={s['semantic_stage']:s for s in plan['geometry_trace']}
    fk=lambda q:transform(numeric.dispatch('forward_pose',dict(full22=full,held16=q.tolist())))
    previous=fk(before);attachment=None;distance=angle=None
    checks=dict(registered_stage_and_gripper_sequence=True)
    if phases['closure_row'] is not None:
        closure_pose=fk(np.asarray(rows[phases['closure_row']]['submitted16'],float))
        attachment=np.linalg.inv(closure_pose)@transform(scene.base_object)
        # Contact goals are already bound to the revalidation proof. Regenerating
        # regrasp from a later command anchor could choose a different exit route.
        target=stages['grasp_close']['goals'][-1]
        tip=transform(scene.eef_tip_reference);distance,angle=pose_error(closure_pose@tip,transform(target)@tip)
        tolerance=tolerances(service.fixture,skill)
        checks['closure_fingertip_within_registered_goal']=(distance<=tolerance['goal_position_m']
                                                         and angle<=tolerance['rotation_rad'])
    counts={name:0 for name in dict.fromkeys(phases['phases'])};failures=[];native_failures=[];departure=[]
    for row,phase,name in zip(rows,phases['phases'],phases['semantic_stages']):
        target=np.asarray(row['submitted16'],float);stage=stages[name]
        if name=='regrasp_exit' and not stage.get('target_contact_departure'):
            raise ValueError('regrasp_exit_departure_proof_missing')
        allow_contact=(name in ('grasp_close','grasp_lift') or name=='regrasp_exit'
                       or name=='regrasp_open' and stage.get('target_contact_hold') is True)
        if name=='regrasp_exit' and not departure:departure.append(previous.copy())
        steps=max(1,int(np.ceil(np.max(abs(target[:14]-before[:14]))/.1)))
        for fraction in np.linspace(0,1,steps+1)[1:]:
            q=before+fraction*(target-before)
            try:
                numeric.numerics.mapping.check_action(q);numeric.numerics.gate.check_preventive(q,full)
            except Exception as exc:
                native_failures.append(dict(phase=phase,virtual_ns=row['virtual_ns'],reason=str(exc)))
            current=fk(q)
            loaded=attachment is not None and row['virtual_ns']>phases['closure_ns']
            if not _motion_clear(scene,previous,current,attachment if loaded else None,
                                 allow_target_contact=allow_contact,support_lift=loaded):
                failures.append(dict(phase=phase,virtual_ns=row['virtual_ns'],fraction=float(fraction),
                    support_plane_diagnostics=support_diagnostics(scene,previous,current,attachment) if loaded else [],
                    unheld_tool_sweep_clear=_motion_clear(scene,previous,current,allow_target_contact=allow_contact)))
            if attachment is not None and row['virtual_ns']==phases['closure_ns'] and fraction==1.:
                if not _motion_clear(scene,current,current,attachment,allow_target_contact=True,support_lift=True):
                    failures.append(dict(phase='attachment_at_closure',virtual_ns=row['virtual_ns'],fraction=1.))
            if name=='regrasp_exit':departure.append(current.copy())
            counts[phase]+=1;previous=current
        before=target
    departure_witness=None
    if departure:
        exit_completed=next(s['completed'] for s in phases['stage_completion'] if s['semantic_stage']=='regrasp_exit')
        if not exit_completed:
            # Only a mathematical extension witness, not counted as emitted
            # output or a completed exit. Strict proof of the whole joined path
            # also covers the actually emitted prefix.
            departure_witness=stages['regrasp_exit']['goals'][-1]
            departure.append(transform(departure_witness))
        checks['registered_target_departure_clear']=bool(target_departure_clear(scene,departure))
    checks.update(native_path_clear=not native_failures,contact_and_support_sweeps_clear=not failures)
    return dict(request_id=request,skill=skill,request_outcome=outcome,
        plan_completed=phases['plan_completed'],prefix_only=not phases['plan_completed'],
        audited_scope='all_recorded_emitted_commands',passed=all(checks.values()),checks=checks,
        output_rows=len(rows),covered_output_rows=len(rows),samples=sum(counts.values()),phase_samples=counts,
        closure_position_error_m=distance,closure_rotation_error_rad=angle,closure=phases,
        hypothetical_eef_object=attachment.tolist() if attachment is not None else None,
        departure_prefix_extension_witness=departure_witness,
        failures=failures,native_failures=native_failures,evidence_scope='simulated_command_path',
        physical_grasp_proven=False,
        attachment_source='FK_at_first_closed_command_and_observed_object' if attachment is not None else None)


def sweep_grasp(outputs,events,plans,service,numeric):
    """Audit every recorded grasp/regrasp command, including failed prefixes.

    ``passed`` concerns emitted path checks, never the skill or physical result.
    Requests without output are outside this command audit, not successful paths.
    """
    by_request={}
    for plan in plans:
        if plan.get('skill') in ('grasp','regrasp'):
            by_request.setdefault(plan['request_id'],[]).append(plan)
    # Missing plans must not silently remove emitted requests from coverage.
    for event in events:
        if event.get('kind')=='harness_planning_started' and event.get('intent',{}).get('skill') in ('grasp','regrasp'):
            by_request.setdefault(event['request_id'],[])
    results=[]
    for request,candidates in by_request.items():
        rows=[r for r in outputs if r.get('request_id')==request]
        published=any(e.get('kind')=='harness_skill_first_publication'
                      and e.get('request_id')==request for e in events)
        if not rows and not published:continue
        try:
            if not rows:raise ValueError('published_grasp_outputs_missing')
            if len(candidates)!=1:raise ValueError('grasp_plan_missing_or_ambiguous')
            result=_sweep_request(rows,events,candidates[0],service,numeric)
        except (ValueError,TypeError,KeyError,IndexError,StopIteration) as exc:
            result=dict(request_id=request,skill=candidates[0]['skill'] if candidates else None,
                passed=False,checks={'complete_command_audit':False},samples=0,output_rows=len(rows),
                covered_output_rows=0,failures=[],native_failures=[],audit_error=str(exc) or type(exc).__name__,
                evidence_scope='simulated_command_path',physical_grasp_proven=False)
        results.append(result)
    if not results:raise ValueError('no_emitted_grasp_to_audit')
    return results
