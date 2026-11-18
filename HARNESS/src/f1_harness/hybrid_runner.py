"""End the VLA skill at placement preparation; reuse the ordinary Harness tail.

The coarse region schedules HOLD, never proves grasp, alignment or placement.
All remaining movement consumes the existing measured skills.
"""
import time
from .hybrid import PlacementOpportunity


def run_hybrid(backend, planner, store, *, cancelled, clock=time.monotonic):
    deadline=getattr(backend,'task_deadline',clock()+300)
    claimed=None
    acquiring=False
    recovery=False
    reason='placement_preparation'
    started=clock()
    while not cancelled() and clock()<deadline:
        status=backend.call('status')
        if not status.get('authorized') or status.get('hold_requested'):
            return dict(outcome='interrupted',reason='owner_authority_ended')
        hybrid=status.get('hybrid')
        if not isinstance(hybrid,dict):raise RuntimeError('hybrid_owner_not_connected')
        current=hybrid.get('opportunity',{})
        identity=tuple(current.get('identity',()))
        if acquiring:
            if hybrid['phase']=='local':
                store.event('hybrid_stage_handoff_completed',opportunity=list(claimed),
                    base_continues=False,placement_verified=False,requires_current_geometry=True)
                return dict(outcome='handoff',reason=reason,status=status,
                    execution=dict(implementation='vla_rtc',placement_verified=False,
                                   old_actions_discarded=True,recovery_requested=recovery))
            if not recovery and hybrid['phase']=='base' and not PlacementOpportunity.applicable(claimed,current):
                reason='handoff_window_changed'
                backend.call('hybrid_control',session_id=backend.session,epoch=status['epoch'],
                             operation='recover',opportunity=list(identity))
                recovery=True
        elif hybrid['phase']=='base' and (hybrid.get('recovery_reason') or hybrid.get('return_observed')
                or clock()-started>=hybrid.get('segment_budget_s',30.)):
            # A bounded executor segment yields to the shared task program;
            # elapsed time is not proof of a physical failure or task failure.
            reason=hybrid.get('recovery_reason') or ('vla_return_without_placement_verification'
                if hybrid.get('return_observed') else 'vla_segment_budget_yield')
            backend.call('hybrid_control',session_id=backend.session,epoch=status['epoch'],
                         operation='recover',opportunity=list(identity))
            claimed=identity;acquiring=True;recovery=True
            store.event('hybrid_recovery_handoff_requested',reason=reason,
                        opportunity=list(identity),model_request_required=False)
        elif hybrid['phase']=='base' and current.get('eligible'):
            # One command per operation. Unknown RPC results are never resent.
            for operation in ('claim','acquire'):
                backend.call('hybrid_control',session_id=backend.session,epoch=status['epoch'],
                             operation=operation,opportunity=list(identity))
            claimed=identity;acquiring=True
            store.event('hybrid_stage_handoff_requested',opportunity=list(identity),
                reason='placement_approach_region',model_request_required=False,
                precise_arrival_verified=False)
        time.sleep(.05)
    return dict(outcome='interrupted' if cancelled() else 'pending_review',
                reason='cancelled' if cancelled() else 'task_budget_exhausted')


def placement_tail_context(arm,store):
    """Try the existing local program once; retain its exact continuation."""
    result=arm.execute_tool('python',{'code':
        'hybrid_tail_result = continue_external_task()\nhybrid_tail_result'}).result
    store.event('hybrid_local_tail_result',result=result,vla_resumed=False)
    return result
