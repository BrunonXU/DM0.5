import json
import struct

import numpy as np
import pytest

from f1dagger.websocket_policy import WebSocketPolicy, encode_request
from test_f1_policy import observation


def client():
    obs = observation()
    cfg = dict(endpoint='http://127.0.0.1:8096',timeout_s=2.,chunk_steps=20,frequency_hz=30.,max_age_s=2.)
    held = np.arange(16, dtype=float)
    policy = WebSocketPolicy(cfg, 'task', lambda a: None, held_getter=lambda:held.copy(), clock=lambda:obs.available_monotonic_ns)
    policy.model_id, policy.model_version = 'test', 'test-version'
    return policy, obs, held


def reply(payload):
    length, = struct.unpack('!I',payload[:4])
    meta = json.loads(payload[4:4+length])
    return dict(meta,success=True,actions=[meta['last_submitted16']]*20,
                active_action_indices=[7,8,9,10,11,12,13,15],rtc=False)


def test_binary_preserves_native_payload_and_controller_hold():
    policy, obs, held = client()
    def exchange(payload):
        assert payload.endswith(b''.join(f.payload for f in obs.frames))
        return reply(payload)
    policy._exchange = exchange
    np.testing.assert_array_equal(policy.infer(obs,4,2),np.broadcast_to(held,(20,16)))
    held[0] = 77
    assert policy.infer(obs,5,2)[0,0] == 77


@pytest.mark.parametrize('key,value', [('session_id','old'),('policy_epoch',0),('prediction_horizon',20),
    ('execute_steps',50),('model_version','changed'),('rtc',True),('shadow_only',True),('actions',[[0.]*16]*50)])
def test_stale_and_semantic_mismatch_rejected(key,value):
    policy,obs,_ = client()
    policy._exchange=lambda payload:dict(reply(payload),**{key:value})
    with pytest.raises(ValueError):policy.infer(obs,4,2)


def test_busy_is_not_retried_and_lock_released():
    policy,obs,_ = client()
    calls=[]
    def exchange(payload):
        calls.append(payload)
        return dict(success=False,error='busy')
    policy._exchange=exchange
    with pytest.raises(ValueError,match='busy'):policy.infer(obs,4,2)
    assert len(calls)==1 and not policy.inflight.locked()
    assert policy.last_timing['request_id'] == 4
    assert policy.last_timing['outcome'] == 'error'
    assert policy.last_timing['response_verified'] is False


def test_recorded_float32_observation_encodes_as_json_numbers():
    from dataclasses import replace
    policy,obs,held=client()
    obs=replace(obs,state16=tuple(np.asarray(obs.state16,dtype=np.float32)))
    policy._exchange=reply
    assert policy.infer(obs,4,2).shape==(20,16)


def rejection(payload):
    return dict(reply(payload), success=False, actions=None, reason='ik_rejected',
                diagnostics=[dict(success=False, joints=None, position_error_mm=3.0)])


def test_checked_ik_rejection_preserves_connection():
    from f1dagger.policy_errors import IKRejected
    policy, obs, _ = client()
    closed = []
    policy.close = lambda: closed.append(True)
    policy._exchange = rejection
    with pytest.raises(IKRejected): policy.infer(obs, 4, 2)
    assert policy.last_timing['outcome'] == 'ik_rejected'
    assert policy.last_timing['response_verified'] is True
    assert not closed and not policy.inflight.locked()
    policy._exchange = reply
    assert policy.infer(obs, 5, 3).shape == (20, 16)
    assert policy.last_timing['request_id'] == 5
    assert policy.last_timing['outcome'] == 'success'


@pytest.mark.parametrize('change', [
    {'session_id': 'other'}, {'model_version': 'other'}, {'request_id': 9},
    {'policy_epoch': 0}, {'contract': 'other'}, {'actions': [[0.]*16]},
    {'diagnostics': []}, {'diagnostics': [{'success': True}]}, {'rtc': True},
    {'shadow_only': True}, {'reason': 'collision'}, {'error': 'invalid response'},
])
def test_untrusted_failures_never_become_retryable_ik(change):
    from f1dagger.policy_errors import IKRejected
    policy, obs, _ = client()
    policy._exchange = lambda payload: dict(rejection(payload), **change)
    with pytest.raises(ValueError) as error: policy.infer(obs, 4, 2)
    assert not isinstance(error.value, IKRejected)


def test_expired_ik_rejection_is_fatal():
    from f1dagger.policy_errors import IKRejected
    policy, obs, _ = client()
    policy.clock = lambda: obs.capture_started_monotonic_ns + 3_000_000_000
    policy._exchange = rejection
    with pytest.raises(ValueError) as error: policy.infer(obs, 4, 2)
    assert not isinstance(error.value, IKRejected)


def test_network_child_sigint_does_not_interrupt_parent_cleanup(capfd):
    """Real spawned IPC child survives terminal SIGINT, then exits on parent EOF."""
    pytest.importorskip('websockets.sync.client')
    import multiprocessing
    import os
    from pathlib import Path
    import signal
    import time
    from f1dagger.websocket_policy import network_worker
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=network_worker, args=(child, 'ws://127.0.0.1:1/ws', .1))
    process.start(); child.close()
    try:
        deadline = time.monotonic()+10
        while time.monotonic() < deadline and process.is_alive():
            status = Path(f'/proc/{process.pid}/status').read_text()
            ignored = int(next(x.split()[1] for x in status.splitlines() if x.startswith('SigIgn:')),16)
            if ignored & (1 << (signal.SIGINT-1)):
                break
            time.sleep(.01)
        else:
            pytest.fail('network child did not install SIGINT ownership')
        os.kill(process.pid, signal.SIGINT)
        # Check the child is still able to process a request after Ctrl+C.
        parent.send_bytes(b'{}')
        assert parent.poll(5)
        assert json.loads(parent.recv_bytes())['success'] is False
        os.kill(process.pid, signal.SIGINT)
        parent.close()
        process.join(5)
        assert process.exitcode == 0
        assert 'KeyboardInterrupt' not in capfd.readouterr().err
    finally:
        parent.close()
        if process.is_alive():process.terminate();process.join(2)
