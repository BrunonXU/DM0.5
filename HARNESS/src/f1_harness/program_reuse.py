"""Run frozen, independently verified programs before asking for a new strategy."""
import ast
import json
import time

from .contracts import HarnessError, SkillResult
from .program_experience import validated_agent_programs


class ProgramOwnerFailure(HarnessError):
    """A submitted skill has a known terminal receipt and execution authority is gone."""

    def __init__(self, skill, receipt):
        super().__init__(receipt.reason)
        self.skill = skill
        self.receipt = receipt


def _program_authority_error(binding, store, backend):
    if backend.pending is None:
        # Use the last motion call's host-captured owner chain, never the
        # candidate's Python return value or an earlier request's failure.
        call = next((c for c in reversed(binding.get('backend_calls', ()))
                     if c.get('name') == 'execute_skill'), {})
        try:
            chain = json.loads(store.blob(call['request_chain_sha256']))
            intent = chain[-1]['intent']
            receipt = SkillResult.model_validate(chain[-1]['result'])
            if (receipt.request_id == intent['request_id'] == call['request_id']
                    and receipt.lifecycle == 'completed' and receipt.outcome == 'failure'
                    and receipt.reason and 'no_motion' in receipt.model_fields_set):
                return ProgramOwnerFailure(intent['skill'], receipt)
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            pass
    return HarnessError('program_reuse_authority_changed')


def _ends_with_verified_grasp(binding,store):
    actions=[c for c in binding.get('backend_calls',()) if c.get('name')=='execute_skill']
    if not actions:return False
    last=actions[-1]
    try:
        request=json.loads(store.blob(last['input_sha256']))
        result=json.loads(store.blob(last['result_sha256']))
    except (KeyError,ValueError,OSError,TypeError):return False
    return (request.get('name')=='verify_grasp' and result.get('request_id')==last.get('request_id')
            and result.get('lifecycle')=='completed' and result.get('outcome')=='success'
            and bool(result.get('evidence_ids')) and not result.get('assisted'))


def _unsubmitted_grasp(item,binding,store,backend):
    """Only retry a first approach which the trusted host never sent to the owner."""
    if item['goal']!='verify_grasp' or backend.pending is not None:return None
    calls=binding.get('backend_calls',())
    if (len(calls)!=2 or [c.get('name') for c in calls]!=['geometry_context','execute_skill']
            or calls[1].get('request_chain_sha256') or calls[1].get('exception_type')):return None
    try:
        arguments=json.loads(store.blob(calls[1]['input_sha256']))
        receipt=json.loads(store.blob(calls[1]['result_sha256']))
    except (KeyError,TypeError,ValueError,OSError):return None
    reason=receipt.get('reason')
    if (arguments.get('name')!='approach_grasp' or set(arguments)-{'name','parameters'}
            or not calls[1].get('request_id') or receipt.get('request_id')!=calls[1]['request_id']
            or binding.get('request_ids')!=[calls[1]['request_id']]
            or receipt.get('lifecycle')!='rejected' or receipt.get('outcome')!='pending_review'
            or receipt.get('no_motion') is not True or receipt.get('resume_request_id')
            or receipt.get('verification_request_id') or reason not in (
                'observation_readiness_timeout:coarse_geometry_cannot_authorize_precision_task',
                'observation_readiness_timeout:grasp_collision_geometry_missing')
            or getattr(backend,'attention',None)!=dict(skill='approach_grasp',reason=reason,submitted=False)):
        return None
    return dict(item=item,arguments=arguments,ready=False)


def resume_localized_grasp(arm,backend,task,store,*,authority_valid,finish_completed):
    deferred=getattr(backend,'deferred_grasp',None)
    if not deferred:return None
    if deferred and deferred.pop('relocalized',False):
        from .remote import ObservationWaitEnded
        if not authority_valid():raise HarnessError('program_reuse_authority_changed')
        budget=min(4.,getattr(backend,'task_deadline',None)-time.monotonic()
                   if getattr(backend,'task_deadline',None) is not None else 4.)
        if budget<=0:return None
        try:
            backend._await_observation(skill='approach_grasp',timeout_s=budget)
        except ObservationWaitEnded as exc:
            store.event('program_localization_still_unresolved',candidate_id=deferred['item']['id'],
                        reason=str(exc),motion_requested=False)
            return None
    if not deferred['ready']:return None
    backend.deferred_grasp=None  # Consume before any owner I/O, including an unknown reply.
    if not authority_valid():raise HarnessError('program_reuse_authority_changed')
    status=backend.call('status')
    if (backend.pending is not None or status.get('attempted_motion')!=[]
            or status.get('completed_skills')!=[] or status.get('authorized') is not True
            or any(status.get(k) for k in ('external_execution','hold_requested','pending','running','motion_wait'))):
        store.event('program_localization_resume_cancelled',candidate_id=deferred['item']['id'],
                    reason='owner_state_changed')
        return None
    return _run_program_choices([deferred['item']],arm,backend,task,store,
        authority_valid=authority_valid,finish_completed=finish_completed,
        kind=deferred['kind'],after_localization=True)


def try_verified_programs(arm,backend,task,snapshot,store,*,authority_valid,finish_completed,synthetic=False):
    """Return current receipts on handoff; never restart a failed invocation.

    Only a pure, false applicability predicate permits trying another entry.
    Once a program runs, an unresolved result goes to Astra with its progress.
    The independently verified grasp case may use the installed placement tail;
    this is a known suffix, not a generated manipulation strategy.
    """
    choices=validated_agent_programs(snapshot,task,synthetic=synthetic)
    if task.goal=='placed':
        choices+=validated_agent_programs(snapshot,task,goal='verify_grasp',synthetic=synthetic)
    return _run_program_choices(choices,arm,backend,task,store,
        authority_valid=authority_valid,finish_completed=finish_completed,kind='verified_program')


def try_validation_program(item,arm,backend,task,store,*,authority_valid,finish_completed):
    """Execute one host-frozen trial. Its result is still awaiting sealed review."""
    return _run_program_choices([item],arm,backend,task,store,
        authority_valid=authority_valid,finish_completed=finish_completed,kind='validation_program')


def _run_program_choices(choices,arm,backend,task,store,*,authority_valid,finish_completed,kind,after_localization=False):
    attempted=[]
    for item in choices:
        if not authority_valid():raise HarnessError('program_reuse_authority_changed')
        if backend.pending is not None:raise HarnessError('unresolved_submission_requires_query')
        store.event(kind+'_selected',candidate_id=item['id'],goal=item['goal'],
            source_sha256=item['source_sha256'],review_sha256=item.get('review_sha256'),model_requests=0,
            after_localization=after_localization)
        reply,binding=arm.execute_candidate(item['candidate'],store,
            call_id=kind.replace('_','-')+'-'+item['id']+('-localized' if after_localization else ''),request_id=None)
        record=dict(candidate_id=item['id'],goal=item['goal'],binding=binding,result=reply.result)
        attempted.append(record)
        if not authority_valid():
            raise _program_authority_error(binding, store, backend)
        completed=finish_completed()
        if completed is not None:
            return dict(completed=completed,attempts=attempted,model_requests=0)
        if reply.result.get('exception') or reply.result.get('error') or backend.pending is not None:
            break
        if not after_localization:
            deferred=_unsubmitted_grasp(item,binding,store,backend)
            if deferred:
                backend.deferred_grasp=dict(deferred,kind=kind)
                store.event('program_waiting_for_grasp_geometry',candidate_id=item['id'],
                            source_sha256=item['source_sha256'],rejected_request_id=binding['request_ids'][0])
                break
        try:value=ast.literal_eval(reply.result.get('value',''))
        except (ValueError,SyntaxError,TypeError):value=None
        # The host-generated wrapper owns these two fields. Owner receipts own
        # every success decision; arbitrary run_skill return values are ignored.
        if (value==dict(applicable=False,program_invoked=False)
                and not binding['request_ids']):
            continue
        if item['goal']=='verify_grasp' and _ends_with_verified_grasp(binding,store):
            status=backend.call('status')
            if (status.get('phase')=='verify_grasp' and 'verify_grasp' in status.get('completed_skills',())
                    and not status.get('hold_requested') and status.get('authorized') is True
                    and not any(status.get(key) for key in ('pending','running','motion_wait'))
                    and not set(status.get('attempted_motion',())).intersection(
                        ('transfer_above_hole','align','insert','release','withdraw_reset'))):
                # No partially executed placement follows this grasp receipt.
                tail_code='place_held_block(withdraw='+repr('withdraw_reset' in task.allowed_skills)+')'
                if after_localization:tail_code='localized_program_tail = '+tail_code+'\nlocalized_program_tail'
                tail=arm.execute_tool('python',dict(code=tail_code)).result
                record['local_tail']=tail
                completed=finish_completed()
                if completed is not None:
                    return dict(completed=completed,attempts=attempted,model_requests=0)
        break
    if attempted:
        store.event(kind+'_handoff',attempts=attempted,model_requests=0,
                    restart_allowed=False)
    return dict(completed=None,attempts=attempted,model_requests=0)
