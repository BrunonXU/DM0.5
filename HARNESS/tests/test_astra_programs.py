"""Saved Astra recovery bug and real PyRUA cells; all outcomes are synthetic."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyrualean.arms import ArmResult, ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.astra_programs import install_recovery_helpers, recovery_instructions
from f1_harness.contracts import HarnessError, Task, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.sandbox import SandboxedArm


STALL = dict(outcome='failure', reason='motion_stalled', cancel_stage='measured_stationary',
             resume_request_id='known-motion')
PENDING = dict(outcome='pending_review', verification_request_id='completed-motion')


class ScriptedBackend:
    def __init__(self, results):
        self.results = deepcopy(results)
        self.calls = []

    def execute_tool(self, name, input_dict, *, prompt_handoff=False):
        self.calls.append((name, input_dict.copy()))
        if name == 'observe':
            return dict(observation=dict(observation_id='synthetic', objects=[]),
                        status=dict(phase='executing', authorized=True))
        assert name == 'execute_skill'
        return self.results.pop(0)

    def solved(self): return False
    def _progress(self,state,**details):
        self.last_progress=dict(state=state,**details)
    def env_steps(self): return 0
    def cancel(self): pass

    @property
    def motions(self):
        return [args for name, args in self.calls if name == 'execute_skill']


def cell(arm, code):
    result = arm.execute_tool('python', {'code': code}).result
    assert not result.get('exception'), result
    assert not result.get('error'), result
    return result


def setup_cells(tmp_path, results, budget=2):
    backend = ScriptedBackend(results)
    arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=tmp_path/'task', images=False)
    receipt = install_recovery_helpers(arm, budget, EvidenceStore(tmp_path/'evidence'))
    assert not backend.motions
    return backend, arm, receipt


def test_saved_877_condition_misses_valid_stall_but_installed_helper_resumes(tmp_path):
    fixture = json.loads((Path(__file__).parent/'fixtures/astra_877_recovery.json').read_text())
    # Extract the actual saved function without executing its top-level robot task.
    tree = ast.parse(fixture['code'])
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    old_calls = []
    robot = SimpleNamespace(skill=lambda name: (old_calls.append(('skill', name)) or deepcopy(STALL)),
        resume=lambda *args: (old_calls.append(('resume', *args)) or {'outcome': 'success'}))
    namespace = {'robo': robot, 'last_result': None}
    exec(compile(tree, fixture['run_id'], 'exec'), namespace)
    with pytest.raises(namespace['GuardStop']):
        namespace['run_checked']('approach_grasp')
    assert old_calls == [('skill', 'approach_grasp')]

    backend, arm, _ = setup_cells(tmp_path, [STALL, {'outcome': 'success'}])
    result = cell(arm, 'motion("approach_grasp")')
    assert ast.literal_eval(result['value']) == {'outcome': 'success'}
    assert backend.motions == [dict(name='approach_grasp'),
        dict(name='approach_grasp', resume_request_id='known-motion')]


@pytest.mark.parametrize('budget', [0, 1, 2])
def test_shared_helper_honors_prepared_budget_and_persistent_namespace(tmp_path, budget):
    backend, arm, receipt = setup_cells(tmp_path, [STALL]*3, budget)
    result = ast.literal_eval(cell(arm, 'motion("transfer_above_hole")')['value'])
    assert result == STALL
    assert backend.motions == [dict(name='transfer_above_hole')] + [
        dict(name='transfer_above_hole', resume_request_id='known-motion')]*budget
    assert receipt['local_corrections'] == budget
    assert not receipt['motion_requested']
    trace = [json.loads(line) for line in (tmp_path/'evidence/trace.jsonl').read_text().splitlines()]
    assert trace[0]['kind'] == 'planner_recovery_helpers_ready'
    assert trace[0]['source_sha256'] == receipt['source_sha256']


@pytest.mark.parametrize('change', [dict(reason='unknown_publication'),
    dict(cancel_stage='owner_hold_committed'), dict(outcome='interrupted'),
    dict(resume_request_id=None), dict(cancel_stage=None, measured_stationary=True)])
def test_helper_does_not_resume_unknown_or_unconfirmed_motion(tmp_path, change):
    initial = dict(STALL, **change)
    backend, arm, _ = setup_cells(tmp_path, [initial])
    assert ast.literal_eval(cell(arm, 'motion("grasp")')['value']) == initial
    assert backend.motions == [dict(name='grasp')]


def test_verification_checkpoint_is_rechecked_without_replaying_motion(tmp_path):
    backend, arm, _ = setup_cells(tmp_path, [PENDING, {'outcome': 'success'}])
    assert ast.literal_eval(cell(arm, 'motion("approach_grasp")')['value'])['outcome'] == 'success'
    assert backend.motions == [dict(name='approach_grasp'),
        dict(name='approach_grasp', verification_request_id='completed-motion')]


def test_unresolved_planning_result_keeps_diagnostic_and_does_not_advance(tmp_path):
    rejected = dict(outcome='pending_review', reason='numerical_plan_rejected:registered_candidates_exhausted',
        no_motion=True, planning_diagnostic=dict(code='registered_candidates_exhausted',
            candidates=[dict(variant=0, code='scene_sweep_blocked')]))
    backend, arm, _ = setup_cells(tmp_path, [rejected])
    result = cell(arm, 'result = motion("approach_grasp")\n'
                       'if result["outcome"] == "success":\n'
                       '    result = motion("grasp")\nresult')
    assert ast.literal_eval(result['value']) == rejected
    assert backend.motions == [dict(name='approach_grasp')]


@pytest.mark.parametrize('change', [dict(exception='SyntaxError'), dict(error='worker unavailable'),
    dict(episode_finished='cancelled'), dict(value='wrong-marker'), dict(moved=True),
    dict(primitive_calls=[dict(tool='execute_skill')])])
def test_installation_failure_does_not_claim_ready(tmp_path, change):
    class BrokenArm:
        def execute_tool(self, name, args):
            marker = ast.literal_eval(args['code'].splitlines()[-1])
            return ArmResult(dict(dict(value=repr(marker), moved=False, primitive_calls=[]), **change))
    store = EvidenceStore(tmp_path/'evidence')
    with pytest.raises(HarnessError, match='planner_recovery_helpers_not_installed'):
        install_recovery_helpers(BrokenArm(), 2, store)
    rows = [json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]
    assert [row['kind'] for row in rows] == ['planner_recovery_helpers_failed']


def test_same_helpers_work_in_actual_isolated_worker_without_new_tools(tmp_path):
    backend = ScriptedBackend([STALL, PENDING, {'outcome': 'success'}])
    arm = SandboxedArm(backend, tmp_path/'task').start()
    try:
        tools = arm.get_tools_spec()
        receipt = install_recovery_helpers(arm, 2, EvidenceStore(tmp_path/'evidence'))
        assert not backend.motions
        result = cell(arm, 'motion("grasp")')
        assert ast.literal_eval(result['value'])['outcome'] == 'success'
        assert backend.motions == [dict(name='grasp'),
            dict(name='grasp', resume_request_id='known-motion'),
            dict(name='grasp', verification_request_id='completed-motion')]
        assert arm.get_tools_spec() == tools
        assert receipt['cell'] == 1
    finally:
        arm.close()


def test_instructions_name_actual_fields_and_use_compiled_helpers():
    text = recovery_instructions(1)
    assert 'reason="motion_stalled"' in text
    assert 'cancel_stage="measured_stationary"' in text
    assert 'outcome="failure"' in text
    assert 'at most 1 local corrections' in text
    # The provided example must also be executable Python, not a pseudo-code trap.
    ast.parse(text.split('Example of guarding the next skill:\n')[1])


def test_real_task_runner_installs_helpers_before_first_model_turn(tmp_path, monkeypatch):
    from f1_harness.task_runner import run_task
    task = Task(task_id='synthetic', object_id='block', hole_id='hole', calibration_id='cal',
                local_corrections=1)
    backend = ScriptedBackend([STALL, {'outcome': 'success'}])
    backend.task = task
    backend.instance, backend.session, backend.pending = 'fake-owner', 'fake-session', None
    backend.prepared_authority = dict(task_hash=digest(task), authorized=False, epoch=0, task_generation=1)
    owner_status = dict(owner_instance_id=backend.instance, task_hash=digest(task), epoch=1,
        authorized=True, hold_requested=False, hardware_ready=True, task_generation=1,
        perception_context=dict(owner_instance_id=backend.instance, session_id=backend.session,
            task_id=task.task_id, task_hash=digest(task), task_generation=1, epoch=1))
    def call(method, **kwargs):
        if method == 'capabilities': return dict(mode='hardware', hardware_ready=True)
        if method == 'status': return deepcopy(owner_status)
        if method == 'execution_events': return dict(events=[], next=kwargs['after'])
        if method == 'finish_task': return dict(outcome='pending_review', synthetic=True)
        raise AssertionError(method)
    backend.call = call
    backend.heartbeat = lambda: None
    backend.close_images = lambda: None
    arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=tmp_path/'cells', images=False)
    arm.start = lambda: arm
    arm.close = lambda: None
    monkeypatch.setattr('f1_harness.task_runner.EvidenceRemoteBackend', lambda *args: backend)
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm', lambda *args, **kwargs: arm)
    seen = []
    class OfflinePlanner:
        def __init__(self, path, store): self.store = store
        def prepare_connection(self):
            assert not backend.motions
        def run(self, arm, *, task_text, snapshot_text, continue_run, complete_run=None):
            assert not backend.motions
            assert continue_run()
            from f1_harness.prompt_contract import instructions
            assert self.local_corrections == 1
            assert recovery_instructions(1) in instructions(self.local_corrections)
            trace = [json.loads(line) for line in (self.store.root/'trace.jsonl').read_text().splitlines()]
            assert any(row['kind'] == 'planner_recovery_helpers_ready' for row in trace)
            seen.append(task_text)
            return ast.literal_eval(cell(arm, 'motion("approach_grasp")')['value'])
        def close(self): pass
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner', OfflinePlanner)
    task_path = tmp_path/'task.json'
    task_path.write_text(task.model_dump_json())
    args = SimpleNamespace(task=task_path, gateway='unused', image_gateway='unused',
        authorization_source='web', start_timeout=1, planner='astra', experience_snapshot=None)
    result = run_task(args, EvidenceStore(tmp_path/'run'), None)
    assert seen and result['planner_declaration']['outcome'] == 'success'
    assert backend.motions == [dict(name='approach_grasp'),
        dict(name='approach_grasp', resume_request_id='known-motion')]

@pytest.mark.parametrize('held,released,first',[(True,False,'verify_grasp'),(False,True,'verify_placed')])
def test_external_execution_verifies_before_any_new_motion(tmp_path,held,released,first):
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'pending_review','reason':'missing_evidence'}])
    original=backend.execute_tool
    def execute(name,args,**kw):
        if name=='observe':return dict(observation={},status=dict(external_execution=dict(
            motion_ended=True,old_actions_discarded=True,closed=held,released=released)))
        return original(name,args,**kw)
    backend.execute_tool=execute
    result=ast.literal_eval(cell(arm,'continue_external_task()')['value'])
    assert result['program_stage']==first
    assert [c['name'] for c in backend.motions]==[first]
