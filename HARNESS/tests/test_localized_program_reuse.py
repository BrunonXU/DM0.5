"""Deferred code resumes only when its first grasp was never submitted."""
import json
from types import SimpleNamespace

import pytest

from f1_harness.evidence import EvidenceStore
from f1_harness.program_reuse import _unsubmitted_grasp,resume_localized_grasp
from f1_harness.remote import RemoteTaskBackend


def setup_case(tmp_path):
    store=EvidenceStore(tmp_path)
    reason='observation_readiness_timeout:grasp_collision_geometry_missing'
    item=dict(id='candidate',source_sha256='source',goal='verify_grasp',candidate={'source':'frozen'})
    receipt=dict(reason=reason,lifecycle='rejected',outcome='pending_review',no_motion=True,request_id='preflight')
    def blob(data):return store.put_blob(json.dumps(data).encode())
    call=dict(name='execute_skill',input_sha256=blob(dict(name='approach_grasp')),
              request_id='preflight',result_sha256=blob(receipt))
    binding=dict(backend_calls=[dict(name='geometry_context'),call],request_ids=['preflight'])
    backend=object.__new__(RemoteTaskBackend)
    backend.pending=None
    backend.attention=dict(skill='approach_grasp',reason=reason,submitted=False)
    backend._progress=lambda *args,**kwargs:None
    return store,item,binding,backend


@pytest.mark.parametrize('change',['earlier_action','request_chain','unknown','pending','submitted','lost_receipt'])
def test_unresolved_or_submitted_action_cannot_arm_program(tmp_path,change):
    store,item,binding,backend=setup_case(tmp_path)
    if change=='earlier_action':binding['backend_calls'].insert(0,dict(name='execute_skill'))
    elif change=='request_chain':binding['backend_calls'][-1]['request_chain_sha256']='prior-owner-attempt'
    elif change=='unknown':binding['backend_calls'][-1]['exception_type']='OSError'
    elif change=='pending':backend.pending='unknown'
    elif change=='submitted':backend.attention['submitted']=True
    elif change=='lost_receipt':binding['backend_calls'][-1].pop('result_sha256')
    assert _unsubmitted_grasp(item,binding,store,backend) is None


@pytest.mark.parametrize('owner_advanced',[False,True])
def test_queued_hint_does_not_submit_another_grasp_and_host_runs_same_source_once(tmp_path,owner_advanced):
    store,item,binding,backend=setup_case(tmp_path)
    backend.deferred_grasp=dict(_unsubmitted_grasp(item,binding,store,backend),
                                kind='validation_program',relocalized=True)
    actual=backend.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert actual['no_motion'] is True and actual['reason']=='localized_grasp_program_waiting_for_host'
    backend._await_observation=lambda **kwargs:backend.deferred_grasp.update(ready=True)
    backend.call=lambda method:dict(authorized=True,attempted_motion=['grasp'] if owner_advanced else [],
                                     completed_skills=[])
    invocations=[]
    def run(candidate,store,*,call_id,request_id):
        invocations.append((candidate,call_id))
        return SimpleNamespace(result=dict(value='False')),dict(backend_calls=[],request_ids=[])
    arm=SimpleNamespace(execute_candidate=run)
    def resume():
        return resume_localized_grasp(arm,backend,SimpleNamespace(),store,
            authority_valid=lambda:True,finish_completed=lambda:None)
    resume()
    assert resume() is None
    assert invocations==([] if owner_advanced else [(item['candidate'],'validation-program-candidate-localized')])
