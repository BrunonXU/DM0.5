"""Offline forecast from current smoother state and a planned reference segment.

No recorded future commands, observations or arrival times are input. Optional
post-boundary samples predict continued publication of that same reference,
not measured arrival or advancement into the next segment. Collision auditing
is separate. This module cannot authorize or publish robot commands.
"""
import math
import time
import numpy as np

from output_branch import fork_output
from f1_harness.trajectory import sample,validate_schedule
from f1_harness.contracts import digest
from f1_harness.command_forecast import forecast_commands


def preview_segment(output, *, publication=None, feedback=None, **plan):
    if publication is not None or feedback is not None:
        if publication is None or feedback is None:
            raise ValueError('preview_requires_command_and_feedback_evidence')
        return forecast_commands(output, publication=publication, feedback=feedback,
                                 predictor=_preview_segment_legacy, **plan)
    # Historical ideal-following fixtures keep their explicitly synthetic schema.
    return _preview_segment_legacy(output, **plan)


def _preview_segment_legacy(output, *, skill, request_id, trajectory, targets,
                    start_index, measured16, first_publication_ns,
                    tick_ns=20_000_000, max_samples=1000, max_wall_s=3.,
                    post_boundary_samples=0,
                    clock=time.monotonic):
    """Fork between committed samples, on the source simulation's own thread.

    The declared 50Hz timeline follows OwnerBridge: first publication anchors
    the segment; the next fresh feedback tick starts its reference clock.
    Ideal following is used only to label this forecast's own previous output.
    There is no transport or dependency on the rest of the replay.
    """
    if skill not in ('transfer_above_hole','withdraw_reset'):
        raise ValueError('segment_preview_free_space_only')
    if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
        raise ValueError('segment_preview_request_required')
    if not validate_schedule(trajectory,targets):raise ValueError('invalid_preview_schedule')
    q=np.array(targets,float,copy=True);measured=np.array(measured16,float,copy=True)
    if measured.shape!=(16,) or not np.isfinite(measured).all():
        raise ValueError('invalid_preview_state')
    if (type(start_index) is not int or not 0<=start_index<len(q)
            or start_index and start_index-1 not in trajectory['stops']):
        raise ValueError('preview_requires_registered_segment_start')
    if (type(first_publication_ns) is not int or first_publication_ns<0
            or type(tick_ns) is not int or tick_ns!=20_000_000):
        raise ValueError('preview_requires_declared_50hz_clock')
    if (type(max_samples) is not int or not 1<=max_samples<=1000
            or type(max_wall_s) not in (int,float) or not math.isfinite(max_wall_s) or max_wall_s<=0):
        raise ValueError('invalid_segment_preview_budget')
    if type(post_boundary_samples) is not int or not 0<=post_boundary_samples<=250:
        raise ValueError('invalid_post_boundary_sample_count')
    end=next(i for i in trajectory['stops'] if i>=start_index)
    anchor=np.array(trajectory['anchor'] if start_index==0 else q[start_index-1],float)
    segment=np.vstack([anchor,q[start_index:end+1]])
    if (not np.all(segment[:,:7]==measured[:7])
            or not np.all(segment[:,14:]==measured[14:])):
        raise ValueError('segment_preview_cannot_change_inactive_arm_or_gripper')
    branch,state=fork_output(output)
    previous_ns=round(state['sample_time_s']*1e9)
    if not 0<first_publication_ns-previous_ns<=50_000_000:
        raise ValueError('preview_start_not_after_current_sample')
    if not np.array_equal(measured[:14],np.asarray(state['position_deg'])):
        raise ValueError('preview_state_does_not_match_current_output')
    started=clock();rows=[];complete=False;boundary_step=None
    for step in range(max_samples):
        if clock()-started>max_wall_s:raise RuntimeError('segment_preview_wall_budget_exceeded')
        now=first_publication_ns+step*tick_ns
        reference,index,boundary,actual_end=sample(trajectory,q,start_index=start_index,
            elapsed_s=max(0,(step-1)*tick_ns/1e9))
        assert actual_end==end
        branch.push(reference[:14],now/1e9,enabled=(False,True))
        command=np.r_[branch.sample(now/1e9),reference[14:]]
        if branch.fault or not np.isfinite(command).all():
            raise RuntimeError('segment_preview_output_fault:'+str(branch.fault))
        rows.append(dict(virtual_ns=now,request_id=request_id,reference16=reference.tolist(),
            submitted16=command.tolist(),measured16=measured.tolist(),reference_index=index,
            at_reference_boundary=bool(boundary),new_motion_reference=True,simulation=True))
        measured=command.copy()
        if boundary and boundary_step is None:boundary_step=step
        if boundary_step is not None and step-boundary_step>=post_boundary_samples:
            complete=True;break
    if not complete:raise RuntimeError('segment_preview_sample_budget_exceeded')
    if clock()-started>max_wall_s:raise RuntimeError('segment_preview_wall_budget_exceeded')
    # A second independent fork observes state only; it must match the input.
    _,after=fork_output(output)
    if state!=after:raise RuntimeError('segment_preview_mutated_source')
    result=dict(schema='f1-planned-segment-preview-v1',skill=skill,request_id=request_id,
        plan_sha256=digest(dict(trajectory=trajectory,targets=q.tolist())),
        start_index=start_index,end_index=end,first_publication_ns=first_publication_ns,
        tick_ns=tick_ns,input_state=state,source_unchanged=True,rows=rows,
        compute_s=clock()-started,physical_motion=False,execution_authorized=False,
        forecast_scope='reference_prefix_to_first_boundary_only',
        measured_arrival_predicted=False,whole_skill_collision_checked=False,
        limitations=['In-process offline branch, not live owner IPC or serialization.',
            'Declared 50Hz scheduling and ideal following; no future observations/timing input.',
            'No forecast of endpoint settling, next segment or task completion.',
            'Original output smoother only; joint/body/task-scene auditing is separate.'])
    if post_boundary_samples:
        result.update(schema='f1-planned-segment-preview-v2',
            forecast_scope='reference_prefix_and_conditional_boundary_hold',
            reference_boundary_ns=rows[boundary_step]['virtual_ns'],
            post_boundary_samples=post_boundary_samples,
            conditional_hold=dict(reference16=q[end].tolist(),enabled=[False,True],
                scheduling_tick_ns=tick_ns,requires_same_request=True,
                requires_no_hold_reset_or_fault=True,requires_no_new_segment=True),
            boundary_completion_predicted=False)
        result['limitations'][2]=('Post-boundary output is conditional on repeated endpoint references; '
            'no prediction of measured arrival, STOP/hold, next segment or task completion.')
    return result
