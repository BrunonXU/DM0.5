import json
import struct
import threading
import time

import numpy as np
import pytest

from f1dagger.rlt_policy import PROTOCOL, RLTPolicy
from f1dagger.session import capabilities
from test_f1_policy import observation


IDENTITY = dict(base_model_version='base-v', encoder_manifest_sha256='encoder',
    actor_manifest_sha256='manifest', actor_version='head', checkpoint_step=400,
    training_phase='bc', q_guidance_updates=0, execution_mode='sync_c16_hold_then_execute')


def make_client():
    obs = observation()
    config = dict(endpoint='http://127.0.0.1:9999/rlt', timeout_s=2., max_age_s=2.,
        chunk_steps=16, frequency_hz=30., model_id='composite', model_version='cv',
        expected_rlt=IDENTITY, require_capabilities=True, shadow_only=False)
    client = RLTPolicy(config, 'synthetic', lambda a:None, held_getter=lambda:np.zeros(16),
                       clock=lambda:obs.available_monotonic_ns)
    health = capabilities('composite', 'cv', 16)
    health.update(protocol=PROTOCOL, rtc=False, sync=True, prediction_horizon=16,
                  reference_horizon=50, execute_steps=16, rlt=IDENTITY,
                  control_mode='rlt_hil_supervised')
    client.read = lambda request: health
    return client, obs, health


def test_evaluation_pin_is_fixed_before_start_and_rejects_changed_base():
    c, _, _ = make_client()
    try:
        c.health()
        candidate = dict(model_id='fixed', model_version='fixed-version',
                         rlt=dict(IDENTITY, actor_version='fixed-head', q_guidance_updates=256))
        c.pin_evaluation(candidate)
        assert c.rlt_identity['actor_version'] == 'fixed-head'
        c.local_segment_id = 'one'
        with pytest.raises(ValueError, match='before'): c.pin_evaluation(candidate)
        c.local_segment_id = None
        candidate['rlt']['base_model_version'] = 'wrong-base'
        with pytest.raises(ValueError, match='frozen'): c.pin_evaluation(candidate)
    finally: c.close()


def reply(payload):
    size, = struct.unpack('!I', payload[:4])
    meta = json.loads(payload[4:4+size])
    assert not any(k in meta for k in ('rtc','prefix','delay_steps'))
    assert meta['reference_horizon'] == 50 and meta['control_mode'] == 'rlt_hil_supervised'
    return dict(meta, success=True, rtc=False, sync=True, proposal_only=True, rlt=IDENTITY,
                actions=[[0.]*16]*16, active_action_indices=[7,8,9,10,11,12,13,15])


def test_bound_C16_transport_keeps_reference50_and_no_RTC_context():
    c, obs, _ = make_client()
    try:
        c.health(); c._exchange = reply
        assert c.infer(obs, 1, 2).shape == (16,16)
        assert c.last_timing['response_verified'] is True
    finally: c.close()


@pytest.mark.parametrize('seed', [None, True, -1, 2**63, 123])
def test_exploration_seed_bound_to_verified_response(seed):
    c, obs, _ = make_client()
    sampling = dict(schema='f1_rlt_sampling_v1',mode='fixed_gaussian',fixed_std=.002)
    try:
        c.health()
        c.rlt_identity = dict(IDENTITY,actor_sampling=sampling)
        c._exchange = lambda payload:dict(reply(payload),rlt=c.rlt_identity,
                                         actor_sampling=sampling,actor_seed=seed)
        if seed == 123:
            c.infer(obs, 1, 2)
            assert c.last_actor_sampling['seed'] == 123
            assert c.last_actor_sampling['model_version'] == 'cv'
        else:
            with pytest.raises(ValueError,match='seed'):
                c.infer(obs, 1, 2)
    finally:
        c.close()


@pytest.mark.parametrize('change', [dict(model_version='different'), dict(prediction_horizon=50),
    dict(execute_steps=20), dict(reference_horizon=16), dict(protocol='f1_eef_rtc_v2'),
    dict(rtc=True), dict(rlt={**IDENTITY,'actor_version':'different'})])
def test_changed_health_fails_closed(change):
    c, _, health = make_client()
    try:
        health.update(change)
        with pytest.raises(ValueError): c.health()
    finally: c.close()


@pytest.mark.parametrize('change', [dict(model_version='different'),dict(policy_epoch=999),
    dict(actions=[[0.]*16]*20),dict(proposal_only=False),dict(rtc=True),
    dict(control_mode='autonomous'),dict(rlt={**IDENTITY,'actor_version':'different'})])
def test_changed_response_or_horizon_cannot_publish(change):
    c, obs, _ = make_client()
    try:
        c.health(); c._exchange=lambda payload:dict(reply(payload),**change)
        with pytest.raises(ValueError): c.infer(obs,1,2)
        assert not c.inflight.locked()
        assert c.last_timing['response_verified'] is False
    finally: c.close()


def test_network_child_uses_rlt_infer_path_and_binary_request():
    from websockets.sync.server import serve
    paths=[]
    def handler(ws):
        paths.append(ws.request.path)
        for payload in ws:
            ws.send(json.dumps(reply(payload)).encode())
    server=serve(handler,'127.0.0.1',0,compression=None)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    c, obs, _=make_client()
    c.endpoint='http://127.0.0.1:'+str(server.socket.getsockname()[1])+'/rlt'
    try:
        c.health()
        assert c.infer(obs,1,2).shape==(16,16)
        assert paths==['/rlt/infer']
    finally:c.close();server.shutdown();worker.join(2)


def test_transport_close_retains_bound_preprocessor_for_next_episode():
    from types import SimpleNamespace
    c, obs, _ = make_client()
    try:
        c.health()
        closed = []
        encoder = SimpleNamespace(close=lambda:closed.append(True), encode=lambda frames:frames,
                                  identity={'bound_processor':'same'}, masked_left_transport=False)
        c.pixel_encoder = encoder
        c.close()
        def exchange(payload):
            size, = struct.unpack('!I',payload[:4])
            assert json.loads(payload[4:4+size])['prepared_pixels'] == encoder.identity
            return reply(payload)
        c._exchange = exchange
        assert c.infer(obs,2,3).shape == (16,16)
        assert closed == [True]
    finally:c.close()
