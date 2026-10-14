"""HTTP cancellation and ownership span an entire long pose task."""
import threading
import time

import pytest

from core import ControlLease
from test_data_auth import http_app


def eventually(predicate, timeout=2.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.mark.parametrize('operation', ['expiry', 'stop', 'release'])
def test_long_task_keeps_lock_until_output_stops_and_never_resumes(http_app, monkeypatch, operation):
    app, request = http_app
    entered, cancelled, finish_cleanup = (threading.Event() for _ in range(3))
    calls = []

    def execute(spec, cancel, progress):
        calls.append(spec)
        progress({'elapsed_s':7., 'progress':.7, 'parts':[]})
        entered.set()
        assert cancel.wait(3), 'gateway did not cancel complete task'
        cancelled.set()
        assert finish_cleanup.wait(3), 'test cleanup was not released'
        return {'status':'cancelled', 'commands_sent':True, 'feedback_verified':False}

    monkeypatch.setattr(app.adapter, 'execute', execute)
    _, _, cookie = request('/api/session')
    _, auth, cookie = request('/api/control/acquire', {'control_code':'fixture-control-code'}, cookie)
    lease = auth['lease_id']
    spec = {'kind':'pose', 'targets':{'body_j3':-5, 'head_j2':0}, 'lease_id':lease}
    external = ControlLease(app.lease.path)
    try:
        status, result, _ = request('/api/actions', spec, cookie)
        assert status == 200 and entered.wait(1)
        action_id = result['action']['id']
        assert request('/api/actions', spec, cookie)[0] == 409  # no hidden queue
        assert app.actions.get(action_id)['progress_detail']['elapsed_s'] > 6
        if operation == 'expiry':
            with app.op_lock:
                app.lease.deadline = time.monotonic() - 1
        else:
            path = '/api/stop' if operation == 'stop' else '/api/control/release'
            assert request(path, {'lease_id':lease}, cookie)[0] == 200
        assert cancelled.wait(1)
        assert app.actions.busy()
        with pytest.raises(ValueError):
            external.acquire('other-controller')
        assert request('/api/actions', spec, cookie)[0] == 409
        finish_cleanup.set()
        app.actions.thread.join(1)
        assert app.actions.get(action_id)['status'] == 'cancelled'
        assert not app.actions.get(action_id)['result']['feedback_verified']
        if operation == 'stop':
            assert request('/api/control/heartbeat', {'lease_id':lease}, cookie)[0] == 200
            assert app.lease.fd is not None  # stop does not exit control mode
        else:
            eventually(lambda: app.lease.fd is None)
            assert request('/api/control/heartbeat', {'lease_id':lease}, cookie)[0] == 409
            external.acquire('other-controller')
        assert len(calls) == 1
        assert app.actions.latest()['id'] == action_id
    finally:
        finish_cleanup.set()
        app.actions.stop(wait=1)
        external.release()
