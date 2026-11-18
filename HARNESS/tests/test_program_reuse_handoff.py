"""Host-receipt handoff tests; scripted state is not physical task evidence."""
import json
from types import SimpleNamespace

import pytest

from f1_harness.evidence import EvidenceStore
from f1_harness.contracts import HarnessError
from f1_harness.program_reuse import try_validation_program


@pytest.mark.parametrize('last_skill,outcome,prior_transport,expect_tail',[
    ('transfer_above_hole','failure',False,False),
    ('verify_grasp','pending_review',False,False),
    ('verify_grasp','success',True,False),
    ('verify_grasp','success',False,True),
])
def test_only_terminal_measured_grasp_can_start_placement_tail(
        tmp_path,last_skill,outcome,prior_transport,expect_tail):
    from test_program_trial_owner import owner
    store=EvidenceStore(tmp_path/'host')
    calls=[];executions=[];tail_calls=[]
    # A failed later action does not change engine.phase. Deliberately expose
    # the old successful phase so the handoff must inspect actual host receipts.
    actions=[('grasp','success'),('verify_grasp','success')]
    if prior_transport:actions.append(('transfer_above_hole','failure'))
    actions.append((last_skill,outcome))
    for index,(name,result_outcome) in enumerate(actions):
        request_id='request-'+str(index)
        arguments={'name':name}
        result=dict(request_id=request_id,lifecycle='completed',outcome=result_outcome,
            evidence_ids=['measured'] if result_outcome=='success' else [],assisted=False)
        calls.append(dict(name='execute_skill',request_id=request_id,
            input_sha256=store.put_blob(json.dumps(arguments).encode()),
            result_sha256=store.put_blob(json.dumps(result).encode())))
    binding=dict(request_ids=[call['request_id'] for call in calls],backend_calls=calls)

    def execute_candidate(candidate,*args,**kwargs):
        executions.append(candidate)
        # A program may catch an action's failure and return normally, or even
        # print success. Neither can replace the host's last measured result.
        return SimpleNamespace(result={'value':"{'applicable': True, 'program_invoked': True, 'result': 'success'}"}),binding

    def execute_tool(name,params):
        tail_calls.append((name,params))
        return SimpleNamespace(result={'value':'None'})

    arm=SimpleNamespace(execute_candidate=execute_candidate,execute_tool=execute_tool)
    # Consume the production TaskEngine.status shape. The synthetic protocol
    # fixture seeds measured state, but cannot invent fields absent from RPC.
    engine,_=owner(tmp_path/'owner')
    engine.phase='verify_grasp'
    engine.completed_skills.update(('grasp','verify_grasp'))
    engine.attempted_motion.update(name for name,_ in actions)
    engine.last_failure='' if outcome=='success' and not prior_transport else 'later_action_failed'
    backend=SimpleNamespace(pending=None,call=lambda method:engine.status())
    item=dict(id='candidate',goal='verify_grasp',source_sha256='a'*64,candidate={})
    result=try_validation_program(item,arm,backend,
        SimpleNamespace(allowed_skills=('withdraw_reset',)),store,
        authority_valid=lambda:True,finish_completed=lambda:None)

    assert len(executions)==1
    assert len(result['attempts'])==1
    assert result['completed'] is None
    assert bool(tail_calls) is expect_tail
    if expect_tail:
        assert tail_calls==[('python',{'code':'place_held_block(withdraw=True)'})]
    else:
        assert 'local_tail' not in result['attempts'][0]
    assert result['attempts'][0]['binding']==binding
    events=[json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]
    assert events[-1]['kind']=='validation_program_handoff'
    assert events[-1]['restart_allowed'] is False


def test_trial_registration_cannot_accept_motion_after_heartbeat_expires(tmp_path,monkeypatch):
    from test_program_trial_owner import owner,rows
    engine,intent=owner(tmp_path)
    register=engine.register_program_trial

    def expire_then_register(*args,**kwargs):
        # First _check_intent passed. The evidence registration's own tick now
        # discovers an expired lease before submit could enqueue the action.
        engine.clock=lambda:engine.heartbeat_ns+engine.lease_ns+1
        return register(*args,**kwargs)

    monkeypatch.setattr(engine,'register_program_trial',expire_then_register)
    try:
        result=engine.submit_skill(intent.model_dump())
    except HarnessError as exc:
        # Revocation changes epoch, so an obsolete-scope protocol error is also
        # valid; it must never appear as an accepted or unknown physical action.
        assert str(exc) in ('not_authorized','owner_or_epoch_mismatch')
    else:
        assert result['lifecycle']=='rejected' and result['no_motion'] is True
    assert not engine.authorized and engine.pending is None and engine.running is None
    assert engine.take_pending() is None
    assert not any(row['kind'] in ('skill_accepted','harness_skill_first_publication')
                   for row in rows(engine))
    assert any(row['kind']=='authority_revoked' and row['reason']=='heartbeat_expired'
               for row in rows(engine))
