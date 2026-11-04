"""Unanswered startup reads may retry; deadlines and failures remain closed."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from robot import runtime_contract as rc


def harness(monkeypatch, *, answers=(), ready=True):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(rc.time, 'monotonic', lambda: clock.now)
    futures = []
    destroyed = []

    def call_async(request):
        future = Future()
        futures.append(future)
        if len(futures) <= len(answers) and answers[len(futures)-1] is not ...:
            answer = answers[len(futures)-1]
            if isinstance(answer, Exception):
                future.set_exception(answer)
            else:
                future.set_result(answer)
        return future

    node = SimpleNamespace(create_client=lambda *_: SimpleNamespace(
        service_is_ready=lambda: ready, call_async=call_async), destroy_client=destroyed.append)
    executor = SimpleNamespace(spin_once=lambda timeout_sec: setattr(clock, 'now', clock.now+timeout_sec))
    return node, executor, clock, futures, destroyed


def invoke(h, deadline=6.0):
    return rc._read_only_parameter_call(h[0], h[1], object, '/motor_ctl/list_parameters', object(), deadline)


def test_unanswered_first_request_recovers_with_fresh_future(monkeypatch):
    response = object()
    h = harness(monkeypatch, answers=(..., response))
    assert invoke(h) == (response, 2)
    assert h[3][0].cancelled() and len(h[3]) == 2
    assert h[2].now == pytest.approx(1.0)
    assert len(h[4]) == 2
    assert h[4][0] is not h[4][1]


def test_normal_response_has_no_retry_or_extra_wait(monkeypatch):
    response = object()
    h = harness(monkeypatch, answers=(response,))
    assert invoke(h) == (response, 1)
    assert h[2].now == 0


def test_persistent_silence_stops_at_original_deadline(monkeypatch):
    h = harness(monkeypatch)
    with pytest.raises(RuntimeError, match='响应超时'):
        invoke(h)
    assert h[2].now == pytest.approx(6.0)
    assert len(h[3]) == 3 and all(f.cancelled() for f in h[3])
    assert len(h[4]) == 3


def test_discovery_failure_never_sends_request(monkeypatch):
    h = harness(monkeypatch, ready=False)
    with pytest.raises(RuntimeError, match='发现超时'):
        invoke(h)
    assert h[2].now == pytest.approx(6.0)
    assert h[3] == [] and len(h[4]) == 1


def test_remaining_shared_budget_is_not_reset(monkeypatch):
    h = harness(monkeypatch)
    h[2].now = 5.7
    with pytest.raises(RuntimeError, match='响应超时'):
        invoke(h)
    assert h[2].now == pytest.approx(6.0)
    assert len(h[3]) == 1


@pytest.mark.parametrize('answer', [None, ValueError('invalid response')])
def test_actual_response_failure_is_not_retried(monkeypatch, answer):
    h = harness(monkeypatch, answers=(answer,))
    with pytest.raises((ValueError, RuntimeError)):
        invoke(h)
    assert len(h[3]) == 1 and len(h[4]) == 1


def test_late_reply_does_not_pass_deadline(monkeypatch):
    h = harness(monkeypatch)
    def late_spin(timeout_sec):
        h[2].now = 6.01
        h[3][-1].set_result(object())
    h[1].spin_once = late_spin
    with pytest.raises(RuntimeError, match='响应超时'):
        invoke(h)
    assert len(h[3]) == 1 and len(h[4]) == 1
