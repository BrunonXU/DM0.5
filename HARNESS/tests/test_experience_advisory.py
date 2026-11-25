"""Saved failure recall and ordinary cell archives, never physical success evidence."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from f1_harness.contracts import Task
from f1_harness.evidence import EvidenceStore
from f1_harness.failure_corpus import record_failures
from f1_harness.memory import ExperienceStore
from f1_harness.program_experience import archive_exploration_cells


SAVED = Path(__file__).resolve().parents[1]/'runs/workspace/web-8821213efb1747d5a5b3bf52830a55d5'


def saved_failure(tmp_path,source=SAVED):
    if not source.is_dir():
        pytest.skip('Saved V27 failure is only available in the local acceptance workspace')
    run = tmp_path/'recorded-failure'
    for relative in ('collected/owner/harness/trace.jsonl', 'planner/trace.jsonl', 'experience-review.json'):
        target = run/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/relative, target)
    rows = [json.loads(line) for line in (run/'collected/owner/harness/trace.jsonl').read_text().splitlines()]
    task = Task.model_validate(next(row['task'] for row in rows if row['kind'] == 'task_prepared'))
    root = tmp_path/'experience'
    record_failures(run, root)
    return run, root, task


def test_latest_real_failure_is_scoped_bounded_advice_not_a_verified_recipe(tmp_path):
    run, root, task = saved_failure(tmp_path)
    source = (run/'planner/trace.jsonl').read_bytes()
    ids = archive_exploration_cells(run, root)
    assert len(ids) == 3 and archive_exploration_cells(run, root) == ids
    # Some filesystems give a whole review batch the same mtime. Preserve the
    # actual planner call order instead of selecting by a content hash.
    for path in (root/'exploration').glob('*.json'):
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert (run/'planner/trace.jsonl').read_bytes() == source
    memory = ExperienceStore(root)
    context = memory.advisory_context(task)
    assert len(context['failures']) == 1 and len(context['explorations']) == 1
    assert 'localize_and_pick' in context['explorations'][0]['historical_code']
    assert [call['skill'] for call in context['explorations'][0]['observed_calls']
            if call['tool'] == 'execute_skill'] == ['approach_grasp', 'grasp', 'regrasp']
    facts = context['failures'][0]['facts']
    assert [f['reason'] for f in facts] == ['grasp_not_attached',
        'numerical_plan_rejected:regrasp_requires_confirmed_empty_gripper_or_registered_station']
    assert [f['publication_recorded'] for f in facts] == [True, False]
    assert not context['executable']
    assert context['cause_status'] == 'reported_codes_only_not_physical_root_cause'
    assert len(json.dumps(context, ensure_ascii=False).encode()) <= 6000
    assert memory.snapshot()['count'] == 0
    assert not (root/'program-candidates').exists()
    reworded = memory.advisory_context(task.model_copy(update={'operator_instruction': 'Place the square block'}))
    assert reworded['scope']['operator_instruction_sha256'] != context['scope']['operator_instruction_sha256']
    assert reworded['failures'] == context['failures']
    assert reworded['explorations'] == context['explorations']
    for changes in ({'calibration_id': 'other'}, {'shape': 'circle'}, {'goal': 'verify_grasp'}):
        unrelated = memory.advisory_context(task.model_copy(update=changes))
        assert not unrelated['failures'] and not unrelated['explorations']
    synthetic = ExperienceStore(root, synthetic=True).advisory_context(task)
    assert not synthetic['failures'] and not synthetic['explorations']
    # An unbound modification cannot masquerade as an original failure or code.
    failure = next((root/'failures').glob('*.json'))
    failure.write_text(failure.read_text().replace('grasp_not_attached', 'model_claimed_success'))
    assert not memory.advisory_context(task)['failures']


@pytest.mark.parametrize('history_unavailable', [False, True])
def test_real_failure_reaches_next_model_request_through_task_runner(tmp_path, monkeypatch, history_unavailable):
    from test_real_bootstrap_contract import owner_rpc_fixture
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.task_runner import run_task

    _, root, task = saved_failure(tmp_path)
    snapshot = ExperienceStore(root).snapshot()
    if history_unavailable:
        def unavailable(*args, **kwargs): raise OSError('isolated history-store test')
        monkeypatch.setattr(ExperienceStore, 'advisory_context', unavailable)
    requests = []

    def make_arm(backend, workspace, **kwargs):
        arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=workspace, images='on-demand')
        arm.start = lambda: arm
        arm.close = lambda: None
        return arm

    class NoNetworkPlanner(ResponsesPlanner):
        def __init__(self, credentials, store): self.store = store
        def prepare_connection(self): pass
        def close(self): pass
        def request(self, **kwargs):
            requests.append(kwargs)
            return dict(output=[dict(type='function_call', name='finish', call_id='read-only',
                arguments=json.dumps(dict(status='stuck', summary='No-motion history contract test')))])

    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm', make_arm)
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner', NoNetworkPlanner)
    with owner_rpc_fixture(tmp_path/'rpc') as (engine, observer, gateway, images, wire_calls):
        task_path = tmp_path/'task.json'
        task_path.write_text(task.model_copy(update={'task_id': 'history-contract'}).model_dump_json())
        args = SimpleNamespace(task=task_path, gateway=gateway, image_gateway=images,
            authorization_source='web', start_timeout=2, planner='astra',
            experience_snapshot=Path(snapshot['path']))
        store = EvidenceStore(tmp_path/'next-planner')
        run_task(args, store, None)
        assert len(requests) == 1
        text = json.dumps(requests[0]['input'], ensure_ascii=False)
        if history_unavailable:
            assert 'grasp_not_attached' not in text
        else:
            assert 'grasp_not_attached' in text and 'UNVERIFIED DATA' in text
            assert 'reported_codes_only_not_physical_root_cause' in text
        assert 'input_image' not in text
        assert not engine.request_publications
        assert 'submit_skill' not in wire_calls
        rows = [json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]
        event = 'experience_advisory_unavailable' if history_unavailable else 'experience_advisory_read'
        assert any(row['kind'] == event for row in rows)


def test_current_processor_supplements_old_review_without_overwriting_it(tmp_path):
    from f1_harness.export_worker import review_collected_run
    run, root, task = saved_failure(tmp_path)
    before = (run/'experience-review.json').read_bytes()
    receipt = review_collected_run(run, root)
    value = json.loads((run/receipt['path']).read_text())
    assert value['processor']['id'] == receipt['processor']
    assert len(value['review']['agent_programs']['exploration_ids']) == 3
    assert value['review']['agent_programs']['candidate_ids'] == []
    assert (run/'experience-review.json').read_bytes() == before
    assert ExperienceStore(root).snapshot()['count'] == 0


@pytest.mark.parametrize('audit_unavailable',[False,True])
def test_latest_v29_history_bound_to_actual_request_and_ordinary_state_does_not_block_images(tmp_path,monkeypatch,audit_unavailable):
    import httpx
    from copy import deepcopy
    from test_real_bootstrap_contract import owner_rpc_fixture
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.task_runner import run_task
    source=SAVED.parent/'web-0731f656c8614760a4a02d0f48230f3a'
    run,root,task=saved_failure(tmp_path,source)
    assert len(archive_exploration_cells(run,root))==4
    snapshot=ExperienceStore(root).snapshot()
    advisory=ExperienceStore(root).advisory_context(task)
    assert len(advisory['failures'])==1 and advisory['explorations']
    captured=[]
    original_calls=[r for r in map(json.loads,(source/'planner/trace.jsonl').read_text().splitlines())
                    if r['kind']=='planner_tool_call']
    def respond(request):
        payload=json.loads(request.content);captured.append(deepcopy(payload))
        first=len(captured)==1
        arguments=original_calls[0]['arguments'] if first else dict(status='stuck',summary='Synthetic request-contract completion')
        return httpx.Response(200,json=dict(id='synthetic-'+str(len(captured)),model=payload['model'],
            reasoning=payload['reasoning'],status='completed',usage={},output=[dict(type='function_call',
            name='python' if first else 'finish',call_id='call-'+str(len(captured)),arguments=json.dumps(arguments))]))
    class NoNetworkPlanner(ResponsesPlanner):
        def __init__(self,credentials,store):
            self.store=store;self.connection_pool=None;self.connection_future=None
            self.client=httpx.Client(base_url='https://synthetic.invalid',transport=httpx.MockTransport(respond))
        def prepare_connection(self):pass
    def make_arm(backend,workspace,**kwargs):
        arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=workspace,images='on-demand')
        arm.start=lambda:arm;arm.close=lambda:None
        return arm
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',make_arm)
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner',NoNetworkPlanner)
    with owner_rpc_fixture(tmp_path/'rpc') as (engine,observer,gateway,images,wire_calls):
        task_path=tmp_path/'task.json';task_path.write_text(task.model_dump_json())
        store=EvidenceStore(tmp_path/'next-planner')
        if audit_unavailable:
            event=store.event
            def unavailable(kind,**data):
                if kind=='model_experience_input':raise OSError('optional audit storage unavailable')
                return event(kind,**data)
            monkeypatch.setattr(store,'event',unavailable)
        args=SimpleNamespace(task=task_path,gateway=gateway,image_gateway=images,
            authorization_source='web',start_timeout=2,planner='astra',experience_snapshot=Path(snapshot['path']))
        run_task(args,store,None)
        assert len(captured)==2 and not engine.request_publications and 'submit_skill' not in wire_calls
    initial=json.dumps(captured[0]['input'],ensure_ascii=False)
    assert 'registered_candidates_exhausted' in initial and 'UNVERIFIED DATA' in initial
    assert 'Reading task/state again is not a prerequisite' in initial
    assert 'input_image' not in initial
    second=captured[1]['input']
    assert sum(c.get('type')=='input_image' for r in second if isinstance(r.get('content'),list)
               for c in r['content'])==2
    rows=[json.loads(s) for s in (store.root/'trace.jsonl').read_text().splitlines()]
    bindings=[r for r in rows if r['kind']=='model_experience_input']
    assert len(bindings)==(0 if audit_unavailable else 2) and all(r['included'] for r in bindings)
    if bindings:
        assert bindings[0]['failure_ids']==[v['id'] for v in advisory['failures']]
        assert bindings[0]['exploration_ids']==[v['id'] for v in advisory['explorations']]
    assert all(not r['strategy_change_established'] and not r['success_promotion_established'] for r in bindings)
    actual_ids={r['request_id'] for r in rows if r['kind']=='model_request_started'}
    if not audit_unavailable:assert {r['request_id'] for r in bindings}==actual_ids
    first_tool=next(r for r in rows if r['kind']=='planner_tool_result')
    assert not first_tool['result'].get('exception')
    assert ExperienceStore(root).snapshot()['count']==0


def test_saved_v29_five_requests_have_one_explicit_image_batch():
    from f1_harness.planner_observations import retire_images
    source=SAVED.parent/'web-0731f656c8614760a4a02d0f48230f3a/planner'
    if not source.exists():pytest.skip('local V29 physical trace unavailable')
    rows=[json.loads(s) for s in (source/'trace.jsonl').read_text().splitlines()]
    def count(value):
        if isinstance(value,dict):return int(value.get('type')=='input_image')+sum(count(v) for v in value.values())
        return sum(map(count,value)) if isinstance(value,list) else 0
    inputs=[json.loads((source/f'llm-{r["request_id"]}-request.json').read_text())['content']['input']
            for r in rows if r['kind']=='model_request_started']
    assert list(map(count,inputs))==[0,0,2,0,0]
    assert count(retire_images(inputs[2]))==0
    shown=next(r for r in rows if r['kind']=='planner_image_grant')
    call=next(r for r in rows if r['kind']=='planner_tool_call' and r['call_id']==shown['call_id'])
    assert call['arguments']['image_reason']==shown['reason']
