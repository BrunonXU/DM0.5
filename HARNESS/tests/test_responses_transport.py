"""No network or keys: exercise request identity, timeout phases, and redaction."""
import json
from pathlib import Path

import httpx
import pytest

from f1_harness import planner as module


class Events:
    def __init__(self):
        self.rows = []

    def event(self, kind, **value):
        self.rows.append(dict(kind=kind, **value))


def make_planner(monkeypatch, handler, **kwargs):
    events = Events()
    original_client = httpx.Client
    monkeypatch.setattr(module, "load_key", lambda _: "fixture-not-a-credential")
    monkeypatch.setattr(module.httpx, "Client", lambda **options: original_client(
        **options, transport=httpx.MockTransport(handler), trust_env=False))
    return module.ResponsesPlanner(Path("unused"), events, **kwargs), events


@pytest.mark.parametrize(("error", "code", "phase"), [
    (httpx.ConnectError, "model_connect_error", "connect"),
    (httpx.ConnectTimeout, "model_connect_timeout", "connect"),
    (httpx.ProxyError, "model_proxy_error", "proxy_connect"),
    (httpx.PoolTimeout, "model_pool_timeout", "pool"),
    (httpx.ReadTimeout, "model_read_timeout", "read"),
    (httpx.WriteTimeout, "model_write_timeout", "write"),
    (httpx.ReadError, "model_read_error", "read"),
    (httpx.WriteError, "model_write_error", "write"),
    (httpx.RemoteProtocolError, "model_transport_error", "transport"),
])
def test_transport_failure_retries_only_unsent_connection_once(monkeypatch, error, code, phase):
    calls = []

    def handle(request):
        calls.append(request)
        raise error("sensitive reflected request or proxy credential", request=request)

    planner, events = make_planner(monkeypatch, handle)
    try:
        with pytest.raises(RuntimeError, match="^" + code + "$"):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert len(calls) == (2 if phase == 'connect' else 1)
    started, failed = events.rows[0], events.rows[-1]
    assert started["kind"] == "model_request_started"
    assert failed["kind"] == "model_transport_error"
    assert started["request_id"] == failed["request_id"]
    assert (failed["code"], failed["phase"]) == (code, phase)
    assert failed["elapsed_s"] >= 0
    assert failed["usage_known"] is False and failed["automatic_retry"] is False
    serialized = json.dumps(events.rows)
    assert "sensitive" not in serialized and "fixture-not-a-credential" not in serialized


@pytest.mark.parametrize('actual_tier', ['fast', 'default'])
def test_connect_wait_is_short_without_shortening_model_read_budget(monkeypatch, actual_tier):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=dict(id="resp-fixture", model=module.MODEL,
            reasoning={"effort": module.EFFORT}, status="completed", output=[], service_tier=actual_tier,
            usage={"input_tokens": 5, "output_tokens": 7}))

    planner, events = make_planner(monkeypatch, handle)
    try:
        planner.request(input=[{"role": "user", "content": "fixture"}], tools=[])
    finally:
        planner.close()
    assert requests[0].extensions["timeout"] == dict(connect=5, read=90, write=15, pool=5)
    body = json.loads(requests[0].content)
    assert len(requests) == 1  # A different returned tier must never cause a second generation.
    assert body['service_tier'] == 'default'
    assert events.rows[0]['requested_service_tier'] == 'default'
    assert events.rows[-1]['service_tier'] == actual_tier
    assert body["model"] == module.MODEL and body["reasoning"] == {"effort": "high"}
    assert events.rows[-1]["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert events.rows[-1]["request_id"] == events.rows[0]["request_id"]


def test_short_explicit_timeout_is_not_extended(monkeypatch):
    planner, _ = make_planner(monkeypatch, lambda _: httpx.Response(200), timeout_s=.2)
    try:
        assert planner.client.timeout.as_dict() == dict(connect=.2, read=.2, write=.2, pool=.2)
    finally:
        planner.close()


@pytest.mark.parametrize('cancel',[False,True])
def test_unsent_connect_retry_uses_same_payload_and_obeys_stop(monkeypatch,cancel):
    requests=[];active=[True]
    def handle(request):
        requests.append(request)
        if len(requests)==1:
            if cancel:active[0]=False
            raise httpx.ConnectError('redacted',request=request)
        return httpx.Response(200,json=dict(id='response',model=module.MODEL,
            reasoning={'effort':module.EFFORT},status='completed',output=[]))
    planner,_=make_planner(monkeypatch,handle);planner.continue_run=lambda:active[0]
    try:
        if cancel:
            with pytest.raises(RuntimeError,match='model_connect_error'):planner.request(input=[],tools=[])
            assert len(requests)==1
        else:
            planner.request(input=[],tools=[])
            assert len(requests)==2 and requests[0].content==requests[1].content
            assert requests[0].headers['X-Client-Request-Id']==requests[1].headers['X-Client-Request-Id']
    finally:planner.close()


def test_proxy_and_http_error_body_are_not_logged(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:REDACTED@localhost:1234")
    planner, events = make_planner(monkeypatch, lambda _: httpx.Response(
        401, json={"error": "untrusted reflected credentials"}))
    try:
        with pytest.raises(RuntimeError, match="^model_http_401$"):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert events.rows[0]["proxy_environment_present"] is True
    assert events.rows[-1]["http_status"] == 401
    serialized = json.dumps(events.rows)
    assert "private-password" not in serialized and "private-user" not in serialized
    assert "untrusted" not in serialized


@pytest.mark.parametrize('cancel_at',['before_request','during_request','during_tool'])
def test_task_stop_never_causes_a_tail_model_call_or_another_tool(cancel_at):
    from types import SimpleNamespace
    planner=module.ResponsesPlanner.__new__(module.ResponsesPlanner);planner.store=Events()
    active=[cancel_at!='before_request'];calls=[];tools=[]
    def request(**kwargs):
        calls.append(kwargs)
        if cancel_at=='during_request':active[0]=False
        return {'output':[{'type':'function_call','name':'python','arguments':'{"code":"result = 1"}','call_id':'first'}]}
    planner.request=request
    def execute(name,args):
        tools.append(name)
        if cancel_at=='during_tool':active[0]=False
        return SimpleNamespace(result={'outcome':'interrupted'})
    arm=SimpleNamespace(get_tools_spec=lambda:[dict(name=n,description=n,input_schema={}) for n in ('python','finish')],execute_tool=execute)
    result=planner.run(arm,task_text='test',continue_run=lambda:active[0])
    assert result['outcome']=='interrupted'
    assert len(calls)==(0 if cancel_at=='before_request' else 1)
    assert tools==(['python'] if cancel_at=='during_tool' else [])
    assert planner.store.rows[-1]['kind']=='planner_cancelled'
