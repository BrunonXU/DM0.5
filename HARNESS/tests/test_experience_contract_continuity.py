"""Cross-layer continuation contracts; synthetic journals, no robot/model API."""
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from f1_harness.contracts import Observation, SkillResult, Task, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.failure_corpus import record_failures
from f1_harness.memory import ExperienceStore
from f1_harness.planner_progress import PlannerProgress


def test_distinct_measured_segments_progress_but_duplicate_receipt_does_not():
    guard=PlannerProgress()
    for index in range(8):
        call=dict(tool='execute_skill',ok=True,kwargs=dict(name='move_tcp'),
            request_id=str(index),lifecycle='completed',outcome='success',no_motion=False,
            evidence_recorded=True)
        result=guard.update(dict(primitive_calls=[call]))
        assert result['idle_turns']==0 and not result['exhausted']
    for index in range(3):
        result=guard.update(dict(primitive_calls=[call]))
        assert result['exhausted'] is (index==2)


def test_actual_adapter_evidence_summary_keeps_repeated_completed_moves_progressing(tmp_path):
    from f1_harness.adapters import F1ArmToolkit,F1Robot
    class Backend:
        def __init__(self):self.steps=0
        def execute_tool(self,name,args):
            assert name=='execute_skill'
            self.steps+=1
            return dict(request_id=str(self.steps),lifecycle='completed',outcome='success',
                        no_motion=False,evidence_ids=['measured-'+str(self.steps)])
        def env_steps(self):return self.steps
        def solved(self):return False
    arm=F1ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    guard=PlannerProgress()
    for _ in range(8):
        reply=arm.execute_tool('python',dict(code='result = robo.skill("move_tcp", target_tcp_pose=[1,0,0,.1,0,1,0,.2,0,0,1,.3,0,0,0,1])')).result
        assert not reply.get('exception'),reply
        assert reply['primitive_calls'][0]['evidence_recorded'] is True
        assert reply['moved'] is True
        assert guard.update(reply)['idle_turns']==0


@pytest.mark.parametrize('change',[{'no_motion':True},{'evidence_recorded':False},
                                  {'lifecycle':'running'},{'outcome':'pending_review'}])
def test_new_request_id_without_measured_motion_does_not_extend_budget(change):
    guard=PlannerProgress()
    call=dict(tool='execute_skill',ok=True,kwargs=dict(name='move_tcp'),request_id='initial',
        lifecycle='completed',outcome='success',no_motion=False,evidence_recorded=True)
    guard.update(dict(primitive_calls=[call]))
    for index in range(3):
        result=guard.update(dict(primitive_calls=[dict(call,request_id=str(index),**change)]))
    assert result['exhausted']


def test_unrelated_records_do_not_evict_scoped_history(tmp_path):
    from f1_harness.program_experience import archive_exploration_cells
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    root=tmp_path/'experience';memory=ExperienceStore(root)
    for index in range(34):
        run=tmp_path/f'run-{index}'
        owner=EvidenceStore(run/'collected/owner/harness')
        owner.event('run_identity',synthetic=False)
        scoped=task if index==0 else task.model_copy(update={'operator_instruction':f'other-{index}'})
        owner.event('task_prepared',task=scoped.model_dump())
        owner.event('skill_result',skill='approach_grasp',result=dict(request_id=str(index),
            outcome='failure',reason='retained-related-failure' if index==0 else 'unrelated'))
        record=record_failures(run,root)
        os.utime(record['path'],ns=(index+1,index+1))
        planner=EvidenceStore(run/'planner')
        planner.event('planner_tool_call',name='python',call_id=str(index),request_id='synthetic',
                      arguments=dict(code=f'historical_example = {index}'))
        planner.event('planner_tool_result',call_id=str(index),result={})
        for identity in archive_exploration_cells(run,root):
            os.utime(root/'exploration'/(identity+'.json'),ns=(index+1,index+1))
    recalled=memory.advisory_context(task)
    assert len(recalled['failures'])==1
    assert recalled['failures'][0]['facts'][0]['reason']=='retained-related-failure'
    assert len(recalled['explorations'])==1
    assert recalled['explorations'][0]['historical_code']=='historical_example = 0'


class RetryingJournalRemote:
    """Use the real Remote implementation with a synthetic terminal owner RPC."""
    def __new__(cls,run):
        from f1_harness.remote import RemoteTaskBackend
        backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
        backend.task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal',
            allowed_skills=('approach_grasp','grasp','verify_grasp'))
        backend.session=run.name+'-session';backend.instance=run.name+'-owner';backend.pending=None
        backend.steps=0;backend._solved=False;backend._closed=False
        owner=EvidenceStore(run/'collected/owner/harness')
        owner.event('run_identity',synthetic=True,scenario_id=run.name,
                    scene_manifest_sha256=run.name+'-scene',heldout=True)
        owner.event('task_prepared',task_hash=digest(backend.task),task=backend.task.model_dump())
        backend.evidence_store=EvidenceStore(run/'remote')
        attempts={}
        status=dict(owner_instance_id=backend.instance,task_hash=digest(backend.task),epoch=1,
            mode='fake',authorized=True,hold_requested=False,perception_context={'session_id':backend.session})
        def observe(**kwargs):
            now=time.monotonic_ns()
            return Observation(observation_id=str(now),acquisition_instance='synthetic',
                received_ns=now,clock_domain='synthetic',scene_version=str(now),calibration_id='cal',
                synthetic=True,stationary=True),dict(status)
        backend._await_observation=observe
        def call(method,**kwargs):
            if method=='geometry_context':return dict(available=True)
            if method=='status':return dict(status)
            if method in ('close_session','finish_task'):return {}
            assert method=='submit_skill',method
            intent=kwargs['intent'];skill=intent['skill'];request=intent['request_id']
            attempts[skill]=attempts.get(skill,0)+1
            failed=skill=='grasp' and attempts[skill]==1
            owner.event('skill_accepted',intent=intent)
            published=not failed and skill!='verify_grasp'
            if published:owner.event('harness_skill_first_publication',request_id=request)
            result=SkillResult(request_id=request,lifecycle='completed',
                outcome='failure' if failed else 'success',
                reason='planning_observation_expired_before_publication' if failed else 'synthetic_measured',
                no_motion=not published,retryable=failed,synthetic=True,
                evidence_ids=() if failed else (request+'-evidence',)).model_dump()
            owner.event('skill_result',skill=skill,result=result)
            return result
        backend.call=call
        return backend


def test_remote_request_chain_cannot_leak_into_next_call_or_failed_admission(tmp_path):
    from f1_harness.contracts import HarnessError
    backend=RetryingJournalRemote(tmp_path/'run')
    result=backend.execute_tool('execute_skill',{'name':'grasp'})
    assert len(backend.last_request_chain)==2
    assert backend.last_request_chain[-1]['result']==result
    backend.execute_tool('geometry_context',{})
    assert backend.last_request_chain==[]
    backend.execute_tool('execute_skill',{'name':'verify_grasp'})
    assert len(backend.last_request_chain)==1
    backend.pending='unresolved'
    with pytest.raises(HarnessError,match='unresolved_submission'):
        backend.execute_tool('execute_skill',{'name':'grasp'})
    assert backend.last_request_chain==[]


def test_actual_remote_retry_sandbox_binding_and_verifier_keep_one_lineage(tmp_path):
    from f1_harness.program_experience import (record_candidate,register_run_agent_programs,
        run_program_evidence,bound_program_invocations,agent_program_goal_match)
    from f1_harness.sandbox import SandboxedArm
    run=tmp_path/'discovery';backend=RetryingJournalRemote(run)
    candidate=dict(title='Retry provenance test',claim='Synthetic protocol only',source='''def applicable(scene):
    return True

def run_skill(robo):
    first = robo.skill("approach_grasp")
    if first.get("outcome") != "success":
        return first
    grasp = robo.skill("grasp")
    if grasp.get("outcome") != "success":
        return grasp
    return robo.skill("verify_grasp")
''')
    store=EvidenceStore(run/'planner');arm=SandboxedArm(backend,run/'worker').start()
    try:
        result,binding=arm.execute_candidate(candidate,store,call_id='program',request_id='no-model-api')
        assert not result.result.get('exception'),result.result
    finally:arm.close()
    record_candidate(store,candidate,call_id='program',request_id='no-model-api')
    actions=[c for c in binding['backend_calls'] if c['name']=='execute_skill']
    assert len(actions)==3
    chain=json.loads(store.blob(actions[1]['request_chain_sha256']))
    assert len(chain)==2 and chain[0]['result']['no_motion'] and chain[-1]['result']['outcome']=='success'
    # The large internal chain never enters the public result/model ledger.
    assert 'request_chain' not in json.dumps(result.result)
    registered=register_run_agent_programs(run,tmp_path/'memory')
    assert len(registered['candidate_ids'])==1,registered
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    proposed=memory._candidate(registered['candidate_ids'][0])
    evidence=run_program_evidence(run)
    invocation=bound_program_invocations(proposed['agent_program'],evidence)[0]
    match=agent_program_goal_match(proposed,evidence,invocation)
    assert len(match['request_ids'])==4
    from copy import deepcopy
    for mutation in ('missing_chain','published_retry','changed_identity'):
        changed=deepcopy(invocation)
        grasp_call=next(c for c in changed['calls'] if c['name']=='execute_skill'
                        and c['arguments']['name']=='grasp')
        if mutation=='missing_chain':grasp_call.pop('request_chain')
        elif mutation=='published_retry':grasp_call['request_chain'][0]['result']['no_motion']=False
        else:grasp_call['request_chain'][0]['intent']['epoch']+=1
        assert agent_program_goal_match(proposed,evidence,changed) is None
    for field,value in (('no_motion',True),('retryable',True),('cancel_stage','cancel_requested'),
                        ('resume_request_id','other'),('verification_request_id','other'),
                        ('rejected_candidate_variant',2),('candidate_variant',2)):
        changed=deepcopy(invocation)
        public=next(c for c in changed['calls'] if c['name']=='execute_skill'
                    and c['arguments']['name']=='grasp')
        public['result'][field]=value
        assert agent_program_goal_match(proposed,evidence,changed) is None
    changed=deepcopy(invocation)
    next(c for c in changed['calls'] if c['name']=='execute_skill')['result']['recovery_context']={'state':'available'}
    assert agent_program_goal_match(proposed,evidence,changed) is not None
    # A real unrelated request must not be excused as an internal retry.
    rows=evidence['owner_rows']
    last=next(i for i,r in enumerate(rows) if r.get('kind')=='skill_accepted'
              and r['intent']['skill']=='verify_grasp')
    foreign=dict(rows[last],intent=dict(rows[last]['intent'],request_id='unrelated'))
    rows.insert(last,foreign)
    assert agent_program_goal_match(proposed,evidence,invocation) is None
    validation=tmp_path/'validation'
    validation_store=EvidenceStore(validation/'planner')
    arm=SandboxedArm(RetryingJournalRemote(validation),validation/'worker').start()
    try:
        result,_=arm.execute_candidate(candidate,validation_store,call_id='validation',request_id='no-model-api')
        assert not result.result.get('exception'),result.result
    finally:arm.close()
    record_candidate(validation_store,candidate,call_id='validation',request_id='no-model-api')
    receipt=memory.verify(registered['candidate_ids'][0],validation_run=validation,scenario_id='validation')
    assert receipt['accepted'],receipt
    memory.promote(registered['candidate_ids'][0])
    assert memory.snapshot()['count']==1


@pytest.mark.parametrize('corruption',['missing','json','identity'])
def test_bad_optional_snapshot_keeps_real_task_runner_zero_experience_path(tmp_path,monkeypatch,corruption):
    from test_real_bootstrap_contract import owner_rpc_fixture
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.task_runner import run_task
    task=Task(task_id='task',object_id='square-1',hole_id='square-hole-1',calibration_id='cal')
    snapshot=Path(ExperienceStore(tmp_path/'memory').snapshot()['path'])
    if corruption=='missing':snapshot.unlink()
    elif corruption=='json':snapshot.write_text('{broken')
    else:
        value=json.loads(snapshot.read_text());value['synthetic']=True;snapshot.write_text(json.dumps(value))
    requests=[]
    def make_arm(backend,workspace,**kwargs):
        arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=workspace,images='on-demand')
        arm.start=lambda:arm;arm.close=lambda:None
        return arm
    class Planner(ResponsesPlanner):
        def __init__(self,credentials,store):self.store=store
        def prepare_connection(self):pass
        def close(self):pass
        def request(self,**kwargs):
            requests.append(kwargs)
            return dict(output=[dict(type='function_call',name='finish',call_id='no-motion',
                arguments=json.dumps(dict(status='stuck',summary='Synthetic read-only test')))])
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',make_arm)
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner',Planner)
    with owner_rpc_fixture(tmp_path/'rpc') as (engine,observer,gateway,images,calls):
        path=tmp_path/'task.json';path.write_text(task.model_dump_json())
        store=EvidenceStore(tmp_path/'planner')
        run_task(SimpleNamespace(task=path,gateway=gateway,image_gateway=images,
            authorization_source='web',start_timeout=2,planner='astra',experience_snapshot=snapshot),store,None)
        assert len(requests)==1 and 'submit_skill' not in calls and not engine.request_publications
    rows=[json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]
    assert any(row['kind']=='experience_snapshot_unavailable' for row in rows)
    assert not any(row['kind']=='experience_snapshot_read' for row in rows)
    assert 'input_image' not in json.dumps(requests)


def test_web_snapshot_creation_failure_still_starts_zero_experience_planner(tmp_path,monkeypatch):
    from f1_harness.web_process import WorkspaceProcesses
    processes=WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.root=Path(__file__).resolve().parents[1]
    processes.run=tmp_path;processes.config={}
    spawned=[];processes.spawn=lambda row:spawned.append(row)
    def unavailable(*args,**kwargs):raise OSError('synthetic historical storage failure')
    monkeypatch.setattr(ExperienceStore,'snapshot',unavailable)
    processes.start_planner(dict(config=dict(mode='astra')))
    assert len(spawned)==1 and '--experience-snapshot' not in spawned[0]['argv']
    rows=[json.loads(line) for line in (tmp_path/'trace.jsonl').read_text().splitlines()]
    assert rows[-1]['kind']=='experience_snapshot_unavailable' and not rows[-1]['executable']


def test_malformed_optional_note_is_not_an_execution_preference():
    from f1_harness.programs import baseline_experience
    assert baseline_experience(dict(files={'broken.md':'---\nconfidence: [\n---\n'}))==[]
