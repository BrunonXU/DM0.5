import json
from pathlib import Path
import time

import httpx
import pytest

from f1_harness.evidence import EvidenceStore
from f1_harness.model_pool import ModelPool
from f1_harness.planner import ResponsesPlanner, MODEL, EFFORT
from f1_harness.subscription_client import SubscriptionClient, completed_response, subscription_payload


def event(value):
    return b'data: ' + json.dumps(value).encode() + b'\r\n\r\n'


def result():
    return dict(id='fixture-response', model=MODEL, reasoning={'effort': EFFORT}, status='completed',
        output=[dict(type='function_call', namespace='harness', name='finish', call_id='fixture-call',
                     arguments='{"status":"done"}')], usage={})


def test_observed_missing_content_type_still_requires_completed_sse():
    response = httpx.Response(200, content=event(dict(type='response.completed', response=result())))
    assert completed_response(response, deadline=time.monotonic()+1) == result()
    with pytest.raises(RuntimeError, match='incomplete'):
        completed_response(httpx.Response(200, content=b'<html>error</html>'), deadline=time.monotonic()+1)


def test_actual_failed_request_adapts_without_mutating_history():
    path = Path(__file__).parents[1] / 'runs/workspace/web-c65a00d9890345c5918a4876c3ab87ce/planner/llm-141ba94e2c4444c595de179de766d885-request.json'
    if not path.exists():
        pytest.skip('saved field run not available')
    source = json.loads(path.read_text())['content']
    original = json.dumps(source, sort_keys=True)
    adapted = subscription_payload(source)
    assert adapted['model'] == source['model']
    assert adapted['reasoning'] == source['reasoning']
    assert adapted['stream'] and adapted['store'] is False
    assert 'max_output_tokens' not in adapted
    assert adapted['tools'][0]['tools'] == source['tools']
    assert json.dumps(source, sort_keys=True) == original


@pytest.mark.parametrize('terminal', ['missing', 'response.failed', 'response.incomplete', 'error'])
def test_partial_tool_never_returned(terminal):
    data = event(dict(type='response.output_item.done', item=result()['output'][0]))
    if terminal != 'missing':
        data += event(dict(type=terminal, response={'error': {'code': 'unknown', 'message': 'PRIVATE'}}))
    response = httpx.Response(200, headers={'content-type':'text/event-stream'}, content=data)
    with pytest.raises(RuntimeError, match='subscription_stream_') as exc:
        completed_response(response, deadline=time.monotonic()+1)
    assert 'PRIVATE' not in str(exc.value)


def test_formal_planner_pool_stream_and_tool_history(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('f1_harness.subscription_client.access_token', lambda *_: 'fixture-access')
    def handle(req):
        calls.append(req)
        if req.method == 'GET':
            return httpx.Response(200, json={'models':[{'slug':MODEL}]})
        return httpx.Response(200, headers={'content-type':'text/event-stream'},
                              content=event(dict(type='response.completed', response=result())))
    client = SubscriptionClient('not-a-real-file', client=httpx.Client(
        base_url='https://api.openai.com/v1', transport=httpx.MockTransport(handle)))
    pool = ModelPool(tmp_path/'pool.sock', None, client=client, subscription_path='fixture', interval=60).start()
    monkeypatch.setenv('F1_MODEL_POOL_SOCKET', str(pool.path))
    planner = ResponsesPlanner(Path('/must-not-read-api-key'), EvidenceStore(tmp_path/'evidence'))
    tools = [dict(type='function', name='finish', parameters={'type':'object'})]
    try:
        value = planner.request(input=[{'role':'user','content':'test'}], tools=tools)
        assert value == result()
        history = value['output'] + [dict(type='function_call_output', call_id='fixture-call', output='done')]
        planner.request(input=history, tools=tools)
        requests = [json.loads(req.content) for req in calls if req.method == 'POST']
        assert len(requests) == 2 and requests[1]['input'][0]['namespace'] == 'harness'
        assert requests[1]['input'][1]['call_id'] == 'fixture-call'
        assert all(req['service_tier'] == 'default' for req in requests)
        trace = (tmp_path/'evidence/trace.jsonl').read_text()
        assert 'chatgpt_subscription' in trace and 'fixture-access' not in trace
    finally:
        pool.close()


def test_failed_subscription_stream_through_pool_never_retries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('f1_harness.subscription_client.access_token', lambda *_: 'fixture-access')
    def handle(req):
        calls.append(req.method)
        if req.method == 'GET':
            return httpx.Response(200, json={'models':[{'slug':MODEL}]})
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=event(dict(
            type='response.failed', response={'error':{'code':'subscription_sharing_usage_limit_exceeded'}})))
    client = SubscriptionClient('fixture', client=httpx.Client(base_url='https://api.openai.com/v1',
        transport=httpx.MockTransport(handle)))
    pool = ModelPool(tmp_path/'pool.sock', None, client=client, subscription_path='fixture', interval=60).start()
    monkeypatch.setenv('F1_MODEL_POOL_SOCKET', str(pool.path))
    planner = ResponsesPlanner(Path('/must-not-read-api-key'), EvidenceStore(tmp_path/'evidence'))
    try:
        with pytest.raises(RuntimeError, match='subscription_sharing_usage_limit_exceeded'):
            planner.request(input=[], tools=[])
        assert calls.count('POST') == 1
    finally:
        pool.close()
