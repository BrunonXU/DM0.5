"""Offline, same-state segment recommendations; never mutate a running proposal.

The binding is intentionally process-local. Certificate object identity is not
an IPC snapshot, and a clear conditional segment is not a complete skill proof.
Numerical callbacks are trusted local code; cancellation is observed between
calls, not by forcibly interrupting native computation.
"""
from copy import deepcopy
import math
import time

import numpy as np

from output_branch import fork_output
from segment_preview import preview_segment
from f1_harness.contracts import digest
from f1_harness.trajectory import schedule, validate_schedule
from f1_harness.contracts import Observation
from f1_harness.command_forecast import sweep_anchor
from sweep_blocks import make_blocks, check_block
from segment_preferences import preference_scope, preferred_order


class RecommendationInvalidated(RuntimeError):
    pass


def audit_candidate(forecast, event, service, numeric, *, pool=None, context_binding=None,
                    cancelled=lambda:False, current=lambda:True, timeout_s=10.):
    """A rejecting counterexample suffices; a clear result needs every point.

    Joint/native exceptions propagate; they are not relabeled as clear or as
    environment collisions. This helper has no owner or publication handle.
    """
    full=event['full_reference']
    scene=service._scene(Observation.model_validate(event['observation']),full,event['held_reference'])
    jobs=make_blocks(forecast['rows'],full,scene)
    count=0;failures=[];computed=0
    if pool is None:
        for job in jobs:
            value=check_block(numeric,job,stop_on_failure=True)
            count+=value['samples'];failures.extend(value['failures'])
            if failures:break
        computed=count
    else:
        if not context_binding:raise ValueError('pooled_candidate_requires_state_binding')
        deadline=time.monotonic()+timeout_s
        # Bounded waves preserve ordered first-counterexample semantics. A
        # rejected wave never queues the remaining candidate suffix.
        for start in range(0,len(jobs),pool.worker_count):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('candidate_audit_deadline')
            values=pool.map(jobs[start:start+pool.worker_count],context_binding=context_binding,
                cancelled=cancelled,current=current,timeout_s=remaining,stop_on_failure=True)
            computed+=sum(v['samples'] for v in values)
            for value in values:
                count+=value['samples'];failures.extend(value['failures'])
                if failures:break
            if failures:break
    return [dict(skill=forecast['skill'],request_id=forecast['request_id'],samples=count,
        evidence_scope=forecast.get('audit_scope','legacy_simulated_output'),
        max_joint_step_deg=.1,failures=failures,passed=not failures,
        complete=not failures,stopped_on_first_failure=bool(failures),
        computed_samples=computed,discarded_after_first_failure=computed-count,
        backend='warm_processes' if pool is not None else 'serial')]


def state_binding(output, context):
    _, state = fork_output(output)
    planner = output.planner
    names = ('size', 'dt', 'max_velocity', 'max_acceleration', 'max_jerk',
             'target_cutoff_hz', 'deadband')
    certificate = planner._certificate
    return digest(dict(context=deepcopy(context), state=state,
        process_local_identity=[id(output), id(planner),
            None if certificate is None else id(certificate[0])],
        profiles={name: {field: getattr(obj, field) for field in names}
                  for name, obj in (('adapter', planner), ('native', planner._radians))},
        output_limits=dict(max_velocity=output.max_velocity, timeout=output.timeout),
        bounds=None if planner.bounds is None else planner.bounds.tolist(),
        enabled_axes=planner._enabled.tolist(), native_initialized=planner._radians.initialized))


def recommend_segment(output, *, context, current_context, cancelled, forecast_kwargs,
                      geometry_trace, reference_speeds, audit, preferences=(),
                      max_wall_s=30., clock=time.monotonic):
    """Compare registered reference paces from one exact source/scene snapshot.

    The result is a recommendation only for the declared conditional segment.
    Caller must retain original planning/revalidation/owner authority. No speed
    or proposal is changed here, and no candidate is inferred on timeout/error.
    """
    required = {'session_id', 'owner_instance_id', 'task_hash', 'epoch', 'calibration_id', 'request_id',
                'source_plan_sha256',
                'observation', 'full_reference', 'held_reference', 'control_profile_sha256'}
    if not isinstance(context, dict) or required - context.keys():
        raise ValueError('candidate_context_incomplete')
    if context['request_id']!=forecast_kwargs.get('request_id'):
        raise ValueError('candidate_request_mismatch')
    if (not reference_speeds or len(reference_speeds)>4
            or any(type(v) not in (int,float) or v not in (30.,40.,50.,60.) for v in reference_speeds)
            or len(set(reference_speeds))!=len(reference_speeds)):
        raise ValueError('unregistered_or_duplicate_reference_candidate')
    if type(max_wall_s) not in (int,float) or not math.isfinite(max_wall_s) or max_wall_s<=0:
        raise ValueError('invalid_candidate_compute_budget')
    if forecast_kwargs.get('post_boundary_samples',0)<=0:
        raise ValueError('conditional_boundary_hold_required')
    inputs=deepcopy(forecast_kwargs);stages=deepcopy(geometry_trace)
    inputs['targets']=np.asarray(inputs['targets']).tolist()
    if 'measured16' in inputs:inputs['measured16']=np.asarray(inputs['measured16']).tolist()
    if not validate_schedule(inputs['trajectory'],inputs['targets']):
        raise ValueError('invalid_candidate_source_schedule')
    baseline=schedule(inputs['targets'],inputs['trajectory']['anchor'],stages,
                      inputs['trajectory']['reference_velocity_deg_s'])
    if baseline!=inputs['trajectory']:
        raise ValueError('candidate_stage_schedule_mismatch')
    started=clock();binding=state_binding(output,context);attempts=[]
    scope=preference_scope(context,inputs,stages,reference_speeds)
    order,usage=preferred_order(preferences,scope,reference_speeds)
    hint_metadata=dict(preference_scope=scope,preference_usage=usage,
        registered_order=list(reference_speeds),evaluated_order=order)

    def check():
        if cancelled():raise RecommendationInvalidated('candidate_cancelled')
        if clock()-started>max_wall_s:raise RecommendationInvalidated('candidate_compute_budget_exceeded')
        try:
            matches=state_binding(output,current_context())==binding
        except (ValueError,TypeError,RuntimeError) as exc:
            raise RecommendationInvalidated('candidate_context_or_output_changed') from exc
        if not matches:
            raise RecommendationInvalidated('candidate_context_or_output_changed')

    check()
    for speed in order:
        check()
        trajectory=schedule(inputs['targets'],inputs['trajectory']['anchor'],stages,speed)
        forecast=preview_segment(output,**dict(inputs,trajectory=trajectory))
        check()
        checked=audit(forecast)
        check()
        # One segment, one request. Never accept missing/truncated/foreign audit.
        expected=0;before=sweep_anchor(forecast['rows'])
        for row in forecast['rows']:
            target=np.asarray(row['submitted16'])
            expected+=max(1,int(np.ceil(np.max(abs(target[:14]-before[:14]))/.1)))
            before=target
        if (not isinstance(checked,list) or len(checked)!=1
                or checked[0].get('request_id')!=inputs['request_id']
                or checked[0].get('skill')!=inputs['skill']
                or checked[0].get('max_joint_step_deg')!=.1
                or not isinstance(checked[0].get('failures'),list)
                or type(checked[0].get('passed')) is not bool
                or checked[0]['passed']!= (not checked[0]['failures'])):
            raise RecommendationInvalidated('candidate_audit_incomplete_or_mismatched')
        passed=checked[0]['passed']
        row=checked[0];count=row.get('samples')
        complete_count=type(count) is int and count==expected
        rejected_prefix=(not passed and type(count) is int and 1<=count<=expected
            and row.get('complete') is False and row.get('stopped_on_first_failure') is True
            and len(row['failures'])==1)
        if (not complete_count and not rejected_prefix) or (passed and row.get('complete',True) is not True):
            raise RecommendationInvalidated('candidate_audit_incomplete_or_mismatched')
        attempts.append(dict(reference_speed=speed,plan_sha256=forecast['plan_sha256'],
            outcome='conditional_segment_clear' if passed else 'output_sweep_rejected',
            audit=checked,forecast_rows=len(forecast['rows']),
            first_publication_ns=inputs['first_publication_ns'],
            reference_boundary_ns=forecast['reference_boundary_ns']))
        if passed:
            check()
            return dict(schema='f1-offline-segment-recommendation-v1',binding=binding,
                **hint_metadata,
                candidate_set_sha256=digest(dict(inputs=inputs,geometry_trace=stages,speeds=list(reference_speeds))),
                attempts=attempts,recommended_reference_speed=speed,scope=forecast['forecast_scope'],
                elapsed_s=clock()-started,execution_authorized=False,whole_skill_verified=False,
                motion_success_inferred=False,process_local_binding=True)
    check()
    return dict(schema='f1-offline-segment-recommendation-v1',binding=binding,attempts=attempts,
        **hint_metadata,
        recommended_reference_speed=None,scope='no_clear_conditional_segment',elapsed_s=clock()-started,
        execution_authorized=False,whole_skill_verified=False,motion_success_inferred=False,
        process_local_binding=True)
