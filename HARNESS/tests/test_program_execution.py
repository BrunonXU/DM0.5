"""Host-bound candidate execution through real isolated PyRUA; no hardware."""
import ast
import json

import pytest

from f1_harness.evidence import EvidenceStore
from f1_harness.planner import ResponsesPlanner
from f1_harness.program_experience import archive_run_programs, record_candidate, validate_candidate
from f1_harness.sandbox import SandboxedArm
from test_planner_skill_feedback import Backend, result


SOURCE = '''def applicable(scene):
    return scene.get("available") is True

def run_skill(robo):
    return robo.skill("reobserve")
'''


def candidate(source=SOURCE):
    return dict(title='Read-only example', claim='Request a new observation', source=source)


class ProgramBackend(Backend):
    def __init__(self, available=True):
        super().__init__(result(request_id='owner-request', outcome='success'))
        self.available = available

    def execute_tool(self, name, input_dict):
        if name == 'geometry_context':
            return dict(available=self.available)
        return super().execute_tool(name, input_dict)


@pytest.fixture
def runtime(tmp_path):
    backend = ProgramBackend()
    arm = SandboxedArm(backend, tmp_path/'task').start()
    store = EvidenceStore(tmp_path/'planner')
    try:
        yield backend, arm, store
    finally:
        arm.close()


def rows(store):
    return [json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]


def test_exact_source_ignores_old_cell_definition_and_binds_owner_receipts(runtime):
    backend, arm, store = runtime
    arm.execute_tool('python', dict(code='def run_skill(robo):\n    raise RuntimeError("old function")'))
    reply, binding = arm.execute_candidate(candidate(), store, call_id='call', request_id='model')
    assert not reply.result.get('exception'), reply.result
    assert ast.literal_eval(reply.result['value'])['program_invoked'] is True
    assert binding['status'] == 'cell_returned' and binding['verified'] is False
    assert binding['request_ids'] == ['owner-request']
    assert backend.calls == [('execute_skill', {'name':'reobserve'})]
    assert store.blob(binding['source_blob_sha256']).decode() == SOURCE
    assert store.blob(binding['code_sha256']).decode().startswith(SOURCE)
    records = rows(store)
    calls = [r for r in records if r['kind'] == 'program_backend_result']
    assert [r['name'] for r in calls] == ['geometry_context','execute_skill']
    receipt = json.loads(store.blob(calls[-1]['result_sha256']))
    assert receipt['request_id'] == 'owner-request'
    assert all(r['invocation_id'] == binding['invocation_id'] for r in records)
    # Ordinary cells after this invocation cannot inherit its identity.
    arm.execute_tool('python', dict(code='robo.observe()'))
    assert rows(store) == records


def test_not_applicable_does_not_call_program(runtime):
    backend, arm, store = runtime
    backend.available = False
    reply, binding = arm.execute_candidate(candidate(), store, call_id='call', request_id=None)
    assert ast.literal_eval(reply.result['value']) == dict(applicable=False, program_invoked=False)
    assert not backend.calls and not binding['request_ids']


def test_nonboolean_predicate_never_enters_run_skill(runtime):
    backend, arm, store = runtime
    source = SOURCE.replace('scene.get("available") is True', '1')
    reply, binding = arm.execute_candidate(candidate(source), store, call_id='call', request_id=None)
    assert reply.result.get('exception'), reply.result
    assert binding['status'] == 'cell_error' and not binding['request_ids']
    assert not backend.calls


def test_error_after_owner_request_preserves_execution_binding(runtime):
    backend, arm, store = runtime
    source = SOURCE.replace('return robo.skill("reobserve")',
                            'robo.skill("reobserve")\n    raise ValueError("after receipt")')
    reply, binding = arm.execute_candidate(candidate(source), store, call_id='call', request_id=None)
    assert reply.result.get('exception'), reply.result
    assert binding['status'] == 'cell_error' and binding['request_ids'] == ['owner-request']
    assert rows(store)[-1]['request_ids'] == ['owner-request']


def test_archive_retains_host_binding_without_claiming_validation(runtime,tmp_path):
    _, arm, store = runtime
    arm.execute_candidate(candidate(), store, call_id='call', request_id='model')
    record_candidate(store,candidate(),call_id='call',request_id='model')
    owner = EvidenceStore(tmp_path/'collected/owner/harness')
    owner.event('skill_accepted',intent=dict(calibration_id='cal'))
    owner.event('skill_result',skill='reobserve',result=dict(request_id='owner-request',outcome='success'))
    root = tmp_path/'experience'
    ids = archive_run_programs(tmp_path,root)
    archived = json.loads((root/'program-candidates'/f'{ids[0]}.json').read_text())
    assert archived['execution_of_proposed_source'] == 'host_bound_invocation'
    assert archived['execution_bindings'][0]['request_ids'] == ['owner-request']
    assert archived['verified'] is False and archived['executable_default'] is False


@pytest.mark.parametrize('overrides', [dict(code='robo.skill("grasp")'),
    dict(execute_candidate='true'), dict(skill_candidate=candidate('bad syntax !'))])
def test_planner_rejects_ambiguous_or_invalid_execution_without_running_code(runtime,overrides):
    backend, arm, store = runtime
    planner = object.__new__(ResponsesPlanner)
    planner.store = store
    requests=[]
    arguments = dict(code='',execute_candidate=True,skill_candidate=candidate()) | overrides
    def request(**kwargs):
        requests.append(kwargs)
        first = len(requests)==1
        return dict(output=[dict(type='function_call',name='python' if first else 'finish',
            call_id=str(len(requests)),arguments=json.dumps(arguments if first else
                dict(status='failure',summary='Read-only test')))])
    planner.request=request
    planner.run(arm,task_text='No physical action',max_turns=2)
    assert not backend.calls
    assert not any(r['kind']=='program_execution_started' for r in rows(store))


def test_planner_explicit_execution_uses_host_entrypoint(runtime):
    backend, arm, store = runtime
    planner = object.__new__(ResponsesPlanner)
    planner.store = store
    requests=[]
    def request(**kwargs):
        requests.append(kwargs)
        first = len(requests)==1
        if not first:
            feedback=json.loads(next(item['output'] for item in kwargs['input']
                if item.get('type')=='function_call_output'))
            assert feedback['program_execution']['request_ids']==['owner-request']
        return dict(output=[dict(type='function_call',name='python' if first else 'finish',
            call_id=str(len(requests)),arguments=json.dumps(
                dict(code='',execute_candidate=True,skill_candidate=candidate()) if first else
                dict(status='failure',summary='Read-only test')))])
    planner.request=request
    planner.run(arm,task_text='No physical action',max_turns=2)
    assert backend.calls == [('execute_skill',{'name':'reobserve'})]
    assert any(r['kind']=='program_candidate_proposed' for r in rows(store))


def test_predicate_builtins_cannot_be_replaced_by_side_effecting_helpers():
    source = SOURCE + '\ndef bool(value):\n    raise RuntimeError("not a builtin")\n'
    with pytest.raises(ValueError, match='builtin_shadowed'):
        validate_candidate(candidate(source))


def test_candidate_math_ignores_previous_cell_patch_and_restores_original_arm(runtime):
    _,arm,store=runtime
    arm.execute_tool('python',dict(code='import math\nmath.sqrt = lambda x: 99'))
    source=SOURCE.replace('return robo.skill("reobserve")','import math\n    return math.sqrt(4)')
    reply,_=arm.execute_candidate(candidate(source),store,call_id='math-call',request_id=None)
    assert not reply.result.get('exception'),reply.result
    assert ast.literal_eval(reply.result['value'])['result']==2.0
    # Candidate execution never replaces the persistent arm or its module.
    ordinary=arm.execute_tool('python',dict(code='math.sqrt(4)'))
    assert ast.literal_eval(ordinary.result['value'])==99


def test_candidate_math_is_restored_after_candidate_exception(runtime):
    _,arm,store=runtime
    arm.execute_tool('python',dict(code='import math\nmath.sqrt = lambda x: 77'))
    source=SOURCE.replace('return robo.skill("reobserve")','raise ValueError("candidate failure")')
    reply,_=arm.execute_candidate(candidate(source),store,call_id='math-error',request_id=None)
    assert reply.result.get('exception')
    ordinary=arm.execute_tool('python',dict(code='math.sqrt(4)'))
    assert ast.literal_eval(ordinary.result['value'])==77
