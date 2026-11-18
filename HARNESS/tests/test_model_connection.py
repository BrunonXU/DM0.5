import json
from types import SimpleNamespace

import httpx
import pytest

from f1_harness.model_transport import ModelTransportTrace
from test_responses_transport import make_planner


def response():
    from f1_harness.planner import MODEL, EFFORT
    return httpx.Response(200, json=dict(id='response-fixture', model=MODEL,
        reasoning={'effort': EFFORT}, status='completed', output=[], usage={}),
        headers={'x-request-id': 'req-fixture', 'openai-processing-ms': '15'})


def test_connect_retry_keeps_one_logical_request_and_body(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout('private detail')
        return response()
    planner, events = make_planner(monkeypatch, handler)
    try:
        planner.request(input=[{'role': 'user', 'content': 'fixture'}], tools=[])
    finally:
        planner.close()
    assert len(calls) == 2 and calls[0].content == calls[1].content
    assert calls[0].headers['x-client-request-id'] == calls[1].headers['x-client-request-id']
    assert events.rows[-1]['provider_request_id'] == 'req-fixture'
    assert events.rows[-1]['provider_processing_ms'] == 15
    assert 'private detail' not in json.dumps(events.rows)


def test_stop_during_connect_prevents_retry(monkeypatch):
    active = [True]
    calls = []
    def handler(request):
        calls.append(request)
        active[0] = False
        raise httpx.ConnectError('fixture')
    planner, events = make_planner(monkeypatch, handler)
    planner.continue_run = lambda: active[0]
    try:
        with pytest.raises(RuntimeError, match='model_connect_error'):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert len(calls) == 1
    assert events.rows[-1]['automatic_retry'] is False


@pytest.mark.parametrize('error', [httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError])
def test_origin_write_start_prevents_replay_even_if_exception_says_connect(monkeypatch, error):
    calls = []
    def handler(request):
        calls.append(request)
        request.extensions['trace']('http11.send_request_headers.started',
                                    {'request': SimpleNamespace(method=b'POST')})
        raise error('fixture')
    planner, events = make_planner(monkeypatch, handler)
    try:
        with pytest.raises(RuntimeError):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert len(calls) == 1
    assert events.rows[-1]['origin_write_started']
    assert events.rows[-1]['automatic_retry'] is False


def test_proxy_connect_has_connect_budget_and_full_model_read_restores():
    trace = ModelTransportTrace()
    limits = dict(connect=5., read=90.)
    proxy = SimpleNamespace(method=b'CONNECT', extensions={'timeout': limits})
    trace('http11.send_request_headers.started', {'request': proxy})
    assert limits['read'] == 5.
    trace('http11.send_request_headers.complete', {})
    trace('http11.receive_response_headers.started', {})
    trace('http11.receive_response_headers.complete', {})
    assert not trace.origin_write_started
    trace('proxy.start_tls.started', {})
    assert limits['read'] == 90.
    trace('proxy.start_tls.complete', {})
    trace('http11.send_request_headers.started', {'request': SimpleNamespace(method=b'POST')})
    assert trace.origin_write_started
    assert 'proxy_connect' in trace.summary()['phase_s']


def test_proxy_handshake_timeout_retries_before_origin_request(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        trace = request.extensions['trace']
        trace('http11.send_request_headers.started',
              {'request': SimpleNamespace(method=b'CONNECT', extensions=request.extensions)})
        assert request.extensions['timeout']['read'] == 5.
        trace('http11.receive_response_headers.started', {})
        raise httpx.ReadTimeout('proxy secret')
    planner, events = make_planner(monkeypatch, handler)
    try:
        with pytest.raises(RuntimeError, match='model_proxy_connect_error'):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert len(calls) == 2
    assert all(r.extensions['timeout']['read'] == 90. for r in calls)
    assert not events.rows[-1]['origin_write_started']
    assert 'proxy secret' not in json.dumps(events.rows)


@pytest.mark.parametrize('fail', [False, True])
def test_preconnect_uses_same_client_without_generation_and_failure_is_advisory(monkeypatch, fail):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == 'GET':
            if fail:
                raise httpx.ConnectError('private failure')
            return httpx.Response(200, json={'id': 'fixture'})
        return response()
    planner, events = make_planner(monkeypatch, handler)
    try:
        planner.prepare_connection()
        planner.prepare_connection()
        planner.connection_future.result(timeout=1)
        planner.request(input=[], tools=[])
    finally:
        planner.close()
    assert [c[0] for c in calls] == ['GET', 'POST']
    assert events.rows[0]['generation_requested'] is False
    assert events.rows[-1]['kind'] == 'model_response'


def test_trace_does_not_store_request_urls_headers_or_exception_objects():
    trace = ModelTransportTrace()
    trace('connection.connect_tcp.started', {'host': 'sensitive-proxy', 'port': 123})
    trace('connection.connect_tcp.failed', {'exception': ValueError('secret')})
    text = json.dumps(trace.summary())
    assert 'sensitive' not in text and 'secret' not in text


def test_stop_before_request_does_not_send_anything(monkeypatch):
    planner, _ = make_planner(monkeypatch, lambda _: pytest.fail('request after STOP'))
    planner.continue_run = lambda: False
    try:
        with pytest.raises(RuntimeError, match='model_request_cancelled'):
            planner.request(input=[], tools=[])
    finally:
        planner.close()
