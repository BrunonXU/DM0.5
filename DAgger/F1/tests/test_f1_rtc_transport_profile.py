"""Explicit transport negotiation and real request framing; no network/robot IO."""
import json
import struct
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from dagger_core.recording_observation import DeviceTimestamp, TimestampedFrame
from f1dagger.observation import Observation
from f1dagger.rtc_policy import RTCPolicy
from test_f1_rtc_pixels import RIGHT_ONLY, encoder


def observation():
    output = BytesIO()
    Image.new('RGB', (16,16), (20,40,60)).save(output, format='JPEG')
    started = 1_000_000_000
    frames = tuple(TimestampedFrame(camera, 9, DeviceTimestamp(started, 'ns', 'ros'),
                   started+1, output.getvalue(), 'image/jpeg', 16, 16)
                   for camera in RIGHT_ONLY['cameras'])
    return Observation(9, started, started+10, frames, (0.,)*16, (started+2,)*16, (0.,)*22)


def make_policy(tmp_path, *, enabled=True, advertise=True, contract=None):
    reference = encoder(tmp_path, 3, contract=RIGHT_ONLY if contract is None else contract,
                        masked_left_transport=contract is None)
    identity, transport = reference.identity, reference.transport_identity
    reference.close()
    obs = observation()
    config = dict(endpoint='http://127.0.0.1:1', timeout_s=1., chunk_steps=20,
                  frequency_hz=30., max_age_s=2., rtc_pixels_directory=str(tmp_path),
                  rtc_pixel_workers=3, rtc_masked_left_transport=enabled)
    policy = RTCPolicy(config, 'task', lambda action: None,
                       held_getter=lambda: np.zeros(16), clock=lambda: obs.available_monotonic_ns)
    health = dict(contract='f1_upper_16d_v1', ready=True, model_id='fake', model_version='v1',
                  protocol='f1_eef_rtc_v2', rtc=True, prediction_horizon=50, execute_steps=20,
                  input_preprocessing=identity)
    if advertise:
        health['image_transport'] = transport
    policy.read = lambda request: json.loads(json.dumps(health))
    return policy, obs, health


def rtc_reply(payload):
    length, = struct.unpack('!I', payload[:4])
    metadata = json.loads(payload[4:4+length])
    context = metadata['rtc']
    return dict(metadata, success=True, rtc=True,
                rtc_context={key: context[key] for key in ('bootstrap', 'delay_steps', 'shift_steps',
                    'coverage_steps', 'plan_origin_ns', 'base_plan_id')},
                active_action_indices=[7,8,9,10,11,12,13,15],
                actions=[metadata['last_submitted16']]*context['coverage_steps'],
                absolute_eef10=np.zeros((50,10)).tolist())


CONTEXT = dict(bootstrap=True, delay_steps=14, shift_steps=16, coverage_steps=31,
               plan_origin_ns=1_000_000_000, base_plan_id=0,
               committed_actions16=[], previous_absolute_eef10=[])


@pytest.mark.parametrize('enabled', [False, True])
def test_explicit_mode_is_negotiated_and_preserved_in_request_reply_and_timing(tmp_path, enabled):
    policy, obs, health = make_policy(tmp_path, enabled=enabled)
    try:
        result = policy.health()
        assert result['client_image_transport'] == policy.pixel_encoder.transport_identity
        sent = []
        def exchange(payload):
            reply = rtc_reply(payload)
            sent.append(reply)
            assert [f['camera'] for f in reply['frames']] == RIGHT_ONLY['cameras']
            assert [f['host_received_monotonic_ns'] for f in reply['frames']] == [f.host_received_monotonic_ns for f in obs.frames]
            return reply
        policy._exchange = exchange
        actions, absolute = policy.infer_rtc(obs, 4, 2, CONTEXT)
        assert actions.shape == (31,16) and absolute.shape == (50,10)
        assert sent[0].get('image_transport') == (health['image_transport'] if enabled else None)
        assert sent[0]['frames'][1]['length'] == (738 if enabled else len(policy.pixel_encoder.encode(obs.frames)[1].payload))
        assert policy.last_timing['image_transport'] == sent[0].get('image_transport')
        assert policy.last_timing['response_verified'] is True
        assert obs.frames[1].media_type == 'image/jpeg'
    finally:
        policy.close()


@pytest.mark.parametrize('change', [None, {}, {'mode': 'wrong'}, {'constant_cameras': ['head']}])
def test_enabled_client_rejects_missing_or_wrong_server_capability(tmp_path, change):
    policy, _, health = make_policy(tmp_path)
    health['image_transport'] = change
    try:
        with pytest.raises(ValueError, match='capability mismatch'):
            policy.health()
        assert policy.model_id is None and policy.pixel_encoder is None
    finally:
        policy.close()


def test_legacy_default_remains_compatible_but_cannot_enable_masked_transport(tmp_path):
    legacy = dict(RIGHT_ONLY, input_version=None, active_cameras=RIGHT_ONLY['cameras'],
                  active_state_indices=list(range(16)))
    policy, _, _ = make_policy(tmp_path, enabled=False, advertise=False, contract=legacy)
    try:
        assert policy.health()['client_image_transport']['mode'] == 'png1_rgb224_v1'
        policy.config['rtc_masked_left_transport'] = True
        with pytest.raises(ValueError, match='right_only_v1'):
            policy.health()
        assert policy.model_id is None
    finally:
        policy.close()


@pytest.mark.parametrize('change', [None, {}, {'mode': 'wrong'}])
def test_missing_or_changed_response_marker_is_fatal_and_releases_inflight(tmp_path, change):
    policy, obs, _ = make_policy(tmp_path)
    try:
        policy.health()
        policy._exchange = lambda payload: dict(rtc_reply(payload), image_transport=change)
        with pytest.raises(ValueError, match='transport identity mismatch'):
            policy.infer_rtc(obs, 4, 2, CONTEXT)
        assert not policy.inflight.locked()
        assert policy.last_timing['response_verified'] is False
    finally:
        policy.close()


def test_flag_cannot_enable_raw_images_or_accept_a_string(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    try:
        policy.config.pop('rtc_pixels_directory')
        with pytest.raises(ValueError, match='bound pixel'):
            policy.health()
        policy.config['rtc_masked_left_transport'] = 'true'
        with pytest.raises(ValueError, match='boolean'):
            policy.health()
    finally:
        policy.close()
