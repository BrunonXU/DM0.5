"""Frozen learning protocol through real bwrap, with explicitly simulated owner."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from f1_harness.contracts import Task,SKILLS,HarnessError
from f1_harness.evidence import EvidenceStore
from f1_harness.memory import ExperienceStore,read_snapshot
from f1_harness.program_reuse import try_verified_programs
from f1_harness.sandbox import SandboxedArm
from test_agent_program_memory import create_run,JournalBackend


def published(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    discovery=create_run(tmp_path/'discovery',goal='placed')
    key=memory.propose_agent_program(discovery,goal='placed')
    validation=tmp_path/'validation';create_run(validation,goal='placed')
    assert memory.verify(key,validation_run=validation,scenario_id='validation')['accepted']
    return read_snapshot(Path(memory.promote(key)['path']),synthetic=True)


class ReuseBackend(JournalBackend):
    def __init__(self,path,*,available=True,confirmed=True,unknown=False):
        super().__init__(path);self.pending=None;self.done=False
        self.available,self.confirmed,self.unknown=available,confirmed,unknown
    def execute_tool(self,name,input_dict):
        if name=='geometry_context':return dict(available=self.available)
        if self.unknown:
            self.pending='unknown-owner-request'
            raise RuntimeError('submission_unknown_stop_and_query')
        value=super().execute_tool(name,input_dict)
        if input_dict['name']=='verify_placed':self.done=self.confirmed
        return value


@pytest.mark.parametrize('available,confirmed,unknown,complete,steps',[
    (True,True,False,True,2),(False,True,False,False,0),
    (True,False,False,False,2),(True,False,True,False,0),
])
def test_published_program_runs_before_model_and_owner_alone_confirms_completion(
        tmp_path,available,confirmed,unknown,complete,steps):
    snapshot=published(tmp_path)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal',allowed_skills=SKILLS)
    backend=ReuseBackend(tmp_path/'reuse',available=available,confirmed=confirmed,unknown=unknown)
    arm=SandboxedArm(backend,tmp_path/'worker').start();store=EvidenceStore(tmp_path/'reuse/planner')
    try:
        result=try_verified_programs(arm,backend,task,snapshot,store,synthetic=True,
            authority_valid=lambda:True,finish_completed=lambda:dict(outcome='success') if backend.done else None)
        model_requests=0
        if result['completed'] is None:model_requests+=1
        assert (result['completed'] is not None)==complete
        assert model_requests==(0 if complete else 1)
        assert backend.steps==steps
        assert len(result['attempts'])==1
        if unknown:assert backend.pending=='unknown-owner-request'
    finally:arm.close()


def test_synthetic_snapshot_never_runs_on_real_path_or_after_stop(tmp_path):
    snapshot=published(tmp_path)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal',allowed_skills=SKILLS)
    backend=ReuseBackend(tmp_path/'reuse');store=EvidenceStore(tmp_path/'store')
    result=try_verified_programs(None,backend,task,snapshot,store,
        authority_valid=lambda:True,finish_completed=lambda:None)
    assert result['attempts']==[] and backend.steps==0
    with pytest.raises(HarnessError,match='program_reuse_authority_changed'):
        try_verified_programs(None,backend,task,snapshot,store,synthetic=True,
            authority_valid=lambda:False,finish_completed=lambda:None)
    assert backend.steps==0


@pytest.mark.parametrize('case', ['failed', 'published_failure', 'success', 'pending', 'different_request', 'unsubmitted'])
def test_lost_authority_reports_only_a_bound_terminal_owner_failure(tmp_path, monkeypatch, case):
    from f1_harness import cli, config, task_runner
    from f1_harness.program_reuse import try_validation_program

    reason = 'plan_revalidation_rejected:revalidation_camera_identity_changed'
    diagnostic = dict(code='revalidation_camera_identity_changed', error_type='RuntimeError', metrics={})
    receipt = dict(request_id='approach', lifecycle='completed', outcome='failure',
                   reason=reason, no_motion=case != 'published_failure', planning_diagnostic=diagnostic)
    if case == 'success':
        receipt.update(outcome='success', reason='owner_arrival_and_fresh_scene')
    backend = SimpleNamespace(pending=None)
    item = dict(id='candidate', goal='verify_grasp', source_sha256='source', candidate={})
    def execute(candidate, store, **kwargs):
        if case == 'pending':
            backend.pending = 'unknown-request'
        call = dict(name='execute_skill', request_id='approach')
        intent = dict(request_id='other' if case == 'different_request' else 'approach', skill='approach_grasp')
        if case != 'unsubmitted':
            chain = [dict(intent=intent, result=receipt)]
            call['request_chain_sha256'] = store.put_blob(json.dumps(chain).encode())
        reply = SimpleNamespace(result=dict(value="{'reason':'candidate_guessed_failure'}"))
        return reply, dict(backend_calls=[call])

    def run_task(args, store, credentials):
        authority = iter((True, False))
        return try_validation_program(item, SimpleNamespace(execute_candidate=execute), backend, None, store,
            authority_valid=lambda:next(authority), finish_completed=lambda:pytest.fail('continued after losing authority'))
    monkeypatch.setattr(task_runner, 'run_task', run_task)
    monkeypatch.setattr(config, 'load_config', lambda path: dict(planner=dict(credentials_file='unused'),
                                                               execution=dict(mode='hardware')))
    run = tmp_path / 'run'
    assert cli.main(['--run', str(run), 'run-task', 'task', '--gateway', 'unused', '--image-gateway', 'unused']) == 1
    failure = json.loads((run/'failure.json').read_text())
    if case in ('failed', 'published_failure'):
        assert failure['code'] == reason and failure['execution_authority_valid'] is False
        assert failure['skill'] == 'approach_grasp'
        assert failure['owner_result']['request_id'] == 'approach'
        assert failure['owner_result']['no_motion'] is receipt['no_motion']
        assert failure['owner_result']['planning_diagnostic'] == diagnostic
    else:
        assert failure == dict(type='HarnessError', code='program_reuse_authority_changed')
