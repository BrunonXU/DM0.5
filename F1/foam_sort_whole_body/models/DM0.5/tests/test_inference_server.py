"""F1-only input/IK/transport boundaries; no model weights or robot dispatch."""
from io import BytesIO
import json
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from f1_data.reader import Normalizer, PARTIAL_BASE_CONTRACT, PROMPT, sha256
from f1_inference import CONTRACT
from f1_inference.policy import load_binding, load_config, observation_sample
from f1_inference.server import action_response, create_app, decode_images, validate_observation


def observation():
    return {'contract': CONTRACT, 'request_id': 'observation-17', 'observation_monotonic_ns': 123456789,
            'state19': [70, -85, -80, -70, 4, -2, 1]*2 + [10, 100, .1, -.03, 0]}


def normalizer():
    from f1_training.execution import FREEZE
    manifest = json.loads((FREEZE / 'manifest.json').read_text())
    identity = {'kinematics_contract': {'config_sha256': manifest['files'][1]['sha256'],
                                      'frame': manifest['contract']['frame']},
                'pose_unit': 'm', 'arm_order': ['left', 'right'],
                'execution_mapping': {key: manifest['contract'][key] for key in ('lower_deg', 'upper_deg')}}
    return Normalizer({'schema': PARTIAL_BASE_CONTRACT, 'source_split': 'train', 'training_approved': True,
                       'kinematics_identity': json.dumps(identity),
                       'state': {'center': [0]*14 + [50, 50, 0, 0, 0], 'scale': [100]*14 + [50, 50, 1, 1, 1]},
                       'action': {'center': [0]*23, 'scale': [1]*23}})


def camera_payloads():
    result = {}
    for i, name in enumerate(('head', 'left', 'right')):
        stream = BytesIO()
        Image.new('RGB', (16, 12), (i*100, 20, 30)).save(stream, format='PNG')
        result[name] = stream.getvalue()
    return result


def solved():
    return {'success': True, 'joint_targets_deg': [observation()['state19'][:14]]*20,
            'gripper_targets': [[10, 100]]*20, 'base_velocity_targets': [[.1, -.03, 0]]*20}


def test_live_observation_has_explicit_fixed_anchor_and_reference():
    original = observation()
    parsed = validate_observation(original)
    assert parsed['reference_joints14_deg'] == original['state19'][:14]
    assert 'reference_joints14_deg' not in original
    original['reference_joints14_deg'] = [1.0]*14
    parsed = validate_observation(original)
    response = action_response(parsed, solved(), {'statistics_sha256': 'frozen'})
    assert response['anchor_joints14_deg'] == original['state19'][:14]
    assert response['reference_joints14_deg'] == [1.0]*14
    assert response['observation_monotonic_ns'] == original['observation_monotonic_ns']
    assert response['status'] == 'ready' and response['execute_steps'] == 20


def test_optional_seed_is_echoed_and_does_not_mutate_default_rng():
    from f1_inference.policy import request_seed
    torch = pytest.importorskip('torch')
    raw = observation()
    assert 'seed' not in validate_observation(raw)
    for bad in (-1, 2**32, True, 1.5, None):
        with pytest.raises(ValueError, match='seed'):
            validate_observation({**raw, 'seed': bad})
    seeded = validate_observation({**raw, 'seed': 123})
    assert action_response(seeded, solved(), {})['seed'] == 123
    initial = torch.random.get_rng_state().clone()
    with request_seed(123, 'cpu'):
        first = torch.randn(8)
    torch.testing.assert_close(torch.random.get_rng_state(), initial)
    with request_seed(123, 'cpu'):
        second = torch.randn(8)
    torch.testing.assert_close(first, second)
    with request_seed(None, 'cpu'):
        torch.randn(8)
    assert not torch.equal(torch.random.get_rng_state(), initial)


@pytest.mark.parametrize('key,value', [
    ('contract', 'g2_34d'), ('request_id', 3), ('request_id', ''),
    ('observation_monotonic_ns', True), ('observation_monotonic_ns', -1),
    ('state19', [0.0]*34), ('state19', [float('nan')]*19),
    ('state19', [True]*19), ('state19', ['0']*19),
    ('reference_joints14_deg', [0]*13), ('reference_joints14_deg', [float('inf')]*14),
    ('state_mask19', [True]*19),
])
def test_invalid_or_unknown_feedback_rejected(key, value):
    metadata = observation()
    metadata[key] = value
    with pytest.raises(ValueError):
        validate_observation(metadata)


def test_images_keep_camera_order_and_original_pixels():
    result = decode_images(camera_payloads())
    assert list(result) == ['head', 'left', 'right']
    assert result['head'].size == (16, 12)
    assert result['right'].getpixel((0, 0)) == (200, 20, 30)
    payloads = camera_payloads()
    payloads['left'] = b'not an image'
    with pytest.raises(ValueError, match='invalid left'):
        decode_images(payloads)
    payloads.pop('left')
    with pytest.raises(ValueError, match='head/left/right'):
        decode_images(payloads)


def test_sample_uses_training_normalizer_metadata_and_physical_generation_mask():
    raw = observation()['state19']
    stats = normalizer()
    sample = observation_sample(raw, stats)
    expected = np.clip((np.asarray(raw, np.float32)-stats.stats['state']['center']) /
                       stats.stats['state']['scale'], -1, 1).astype(np.float32)
    np.testing.assert_array_equal(sample['state'], expected)
    assert sample['prompt'] == PROMPT
    assert sample['meta_data'] == {'robot_type': 'F1', 'control_mode': 'EEF relative'}
    assert sample['state_mask'].all()
    assert sample['action_mask'].shape == (50, 32)
    assert sample['action_mask'][:, :23].all() and not sample['action_mask'][:, 23:].any()


def test_rejection_never_leaks_partial_targets_and_success_must_be_finite():
    bad = solved()
    bad.update(success=False, reason='ik_rejected', step=4, arm=1)
    response = action_response(validate_observation(observation()), bad, {})
    assert response['status'] == 'rejected'
    assert not any(key in response for key in ('joint_targets_deg', 'gripper_targets', 'base_velocity_targets'))
    bad = solved()
    bad['base_velocity_targets'] = [[float('nan'), 0, 0]]*20
    with pytest.raises(ValueError, match='base_velocity_targets'):
        action_response(validate_observation(observation()), bad, {})


def test_existing_frozen_ik_decodes_live_chunk_and_rejects_invalid_rotation():
    pytest.importorskip('scipy')
    from f1_training.execution import ActionDecoder
    decoder = ActionDecoder(normalizer())
    obs = validate_observation(observation())
    action = np.zeros((50, 32))
    action[:, [3, 7, 12, 16]] = 1
    action[:, 18:23] = obs['state19'][14:]
    result = decoder.decode(action, obs['state19'][:14], obs['reference_joints14_deg'])
    response = action_response(obs, result, {})
    assert response['status'] == 'ready'
    np.testing.assert_allclose(response['joint_targets_deg'], np.tile(obs['state19'][:14], (20, 1)), atol=1e-8)
    action[3, 12:18] = 0
    result = decoder.decode(action, obs['state19'][:14])
    assert action_response(obs, result, {})['status'] == 'rejected'


def test_release_binding_does_not_require_training_images_or_episodes(tmp_path):
    stats = tmp_path/'normalization.json'
    stats.write_text(json.dumps(normalizer().stats))
    release = tmp_path/'release.json'
    release.write_text(json.dumps({'schema': 'f1_dm05_release_v1', 'contract': PARTIAL_BASE_CONTRACT,
                                  'training_approved': True, 'statistics': {'path': stats.name, 'sha256': sha256(stats)},
                                  'image_root': 'absent-images', 'episodes': ['not-loaded-during-serving']}))
    config_path = tmp_path/'inference.json'
    config_path.write_text(json.dumps({'contract': CONTRACT, 'policy_contract': PARTIAL_BASE_CONTRACT,
                                      'checkpoint': None, 'release': release.name}))
    config = load_config(config_path)
    _, binding = load_binding(config)
    assert binding['release_sha256'] == sha256(release)
    assert binding['statistics_sha256'] == sha256(stats)
    assert binding['checkpoint_identity'] is None
    stats.write_text(stats.read_text()+'\n')
    with pytest.raises(ValueError, match='release file changed'):
        load_binding(config)


def test_http_transport_success_validation_and_failed_ik():
    pytest.importorskip('flask')
    class Policy:
        fail = False

        def predict(self, obs, images):
            assert obs['state19'] == observation()['state19']
            assert list(images) == ['head', 'left', 'right']
            return {'success': False, 'reason': 'ik_rejected'} if self.fail else solved()

    policy = Policy()
    binding = {'statistics_sha256': 'stats', 'release_sha256': 'release', 'checkpoint_identity': {'checkpoint': 'chosen'}}
    client = create_app(policy, binding).test_client()
    health = client.get('/health').get_json()
    assert health['ready'] and health['model_binding'] == binding

    def data():
        return {'metadata': json.dumps(observation()),
                **{name: (BytesIO(payload), name+'.png') for name, payload in camera_payloads().items()}}

    response = client.post('/process_frame', data=data())
    assert response.status_code == 200
    assert response.get_json()['status'] == 'ready'
    assert response.get_json()['model_binding'] == binding
    policy.fail = True
    response = client.post('/process_frame', data=data())
    assert response.status_code == 200 and response.get_json()['status'] == 'rejected'
    assert 'base_velocity_targets' not in response.get_json()
    bad = data()
    del bad['left']
    assert client.post('/process_frame', data=bad).status_code == 400
    assert create_app(None, binding).test_client().get('/health').get_json()['ready'] is False
    assert create_app(None, binding).test_client().post('/process_frame').status_code == 503


def test_real_client_multipart_server_ik_and_robot_prefix_roundtrip():
    pytest.importorskip('flask')
    pytest.importorskip('scipy')
    import threading
    import time
    from werkzeug.serving import make_server
    from f1_training.execution import ActionDecoder
    task = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(task/'robot'))
    from f1_policy_client.observation import Observation
    from f1_policy_client.policy import HttpPolicy
    from f1_policy_client.execution import prepare_prefix
    from f1_replay.bundle import ARM_NAMES, BODY_NAMES
    from f1_replay.runtime import ReplayError

    class Policy:
        fail = False
        decoder = ActionDecoder(normalizer())

        def predict(self, obs, images):
            assert images['right'].getpixel((0, 0)) == (200, 20, 30)
            action = np.zeros((50, 32))
            action[:, [3, 7, 12, 16]] = 1
            action[:, 18:23] = obs['state19'][14:]
            if self.fail:
                action[3, 12:18] = 0
            result = self.decoder.decode(action, obs['state19'][:14], obs['reference_joints14_deg'])
            result['inference_diagnostics'] = {'physical_action23': action[:, :23].tolist()}
            return result

    policy = Policy()
    binding = {'statistics_sha256': 'fixture-stats', 'release_sha256': 'fixture-release',
               'checkpoint_identity': {'checkpoint': 'fixture-no-weights'}, 'ik_version': 'f1_ikfk_demo_v1'}
    server = make_server('127.0.0.1', 0, create_app(policy, binding))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HttpPolicy(f'http://127.0.0.1:{server.server_port}', timeout_s=2)
        assert client.health()['model_binding'] == binding
        stamp = time.monotonic_ns()
        state = observation()['state19']
        observed = Observation(tuple(state), (0.,)*5, camera_payloads(), stamp, {})
        snapshot = {'joints': {name: {'position': value, 'received_ns': stamp}
                               for name, value in zip(ARM_NAMES+BODY_NAMES, state[:14]+[0.]*5)},
                    'grippers': {side: {'position': value, 'received_ns': stamp}
                                 for side, value in zip(('left', 'right'), state[14:16])},
                    'base': {'received_ns': stamp, 'vx': state[16], 'vy': state[17], 'wz': state[18],
                             'is_soft_auto': False, 'is_hard_auto': True, 'is_parking_mode': False, 'is_avoid': False}}
        profile = json.loads((task/'configs/replay_f1.json').read_text())
        response = client.infer(observed, 'roundtrip')
        prepared = prepare_prefix(response, observed, snapshot, profile, now_ns=stamp,
                                  request_id='roundtrip', expected_model_binding=binding, max_observation_age_s=2)
        assert len(prepared.bundle.frames) == 20
        np.testing.assert_allclose(prepared.bundle.frames[0].action, state, atol=1e-8)
        policy.fail = True
        rejected = client.infer(observed, 'roundtrip-rejected')
        assert rejected['status'] == 'rejected'
        assert len(rejected['diagnostics']['physical_action23']) == 50
        assert not any(key in rejected for key in ('joint_targets_deg', 'gripper_targets', 'base_velocity_targets'))
        with pytest.raises(ReplayError, match='prefix rejected'):
            prepare_prefix(rejected, observed, snapshot, profile, now_ns=stamp,
                           request_id='roundtrip-rejected', expected_model_binding=binding, max_observation_age_s=2)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.skipif(not os.environ.get('F1_INFERENCE_PROCESSOR'), reason='optional real processor path not provided')
def test_real_processor_matches_existing_validation_dataset_exactly():
    import torch
    from transformers import AutoProcessor
    from opendm.data.collator import TrainingCollator
    from f1_data.images import ImageLoader
    from f1_data.reader import F1Dataset
    from f1_data.state_tokens import make_tokenizer
    from f1_inference.policy import DM05Policy
    from f1_training.evaluation import generation_inputs

    torch.set_num_threads(2)
    processor = AutoProcessor.from_pretrained(os.environ['F1_INFERENCE_PROCESSOR'], local_files_only=True)
    stats = normalizer()
    images = decode_images(camera_payloads())
    tokenizer = make_tokenizer(processor, image_keys=['images_1', 'images_2', 'images_3'], max_length=1024)
    loader = ImageLoader('.', training=False)
    loader.raw_images = lambda episode, indices: list(images.values())
    loader._build_transforms()

    class Episode:
        split = 'validation'
        episode = 'fixture'
        starts = [0]
        contract = PARTIAL_BASE_CONTRACT
        kinematics_identity = stats.stats['kinematics_identity']

        def sample(self, index):
            assert index == 0
            mask = np.ones((50, 32), dtype=bool)
            mask[:, 23:] = False
            # Unknown FUTURE chassis supervision must not affect generation.
            mask[4:9, 20:23] = False
            return {'state': np.asarray(observation()['state19'], np.float32),
                    'state_mask': np.ones(19, bool), 'action': np.zeros((50, 32), np.float32),
                    'action_mask': mask, 'camera_source_indices': np.zeros(3, dtype=int),
                    'prompt': PROMPT, 'meta_data': {'robot_type': 'F1', 'control_mode': 'EEF relative'}}

    collator = TrainingCollator(processor.tokenizer.pad_token_id, max_length=1024)
    dataset = F1Dataset([Episode()], stats, loader, tokenizer)
    expected = generation_inputs(collator([dataset[0]]))
    # Do not call __init__: no model, checkpoint, GPU, or ROS is involved.
    policy = DM05Policy.__new__(DM05Policy)
    policy.normalizer, policy.tokenizer, policy.collator = stats, tokenizer, collator
    policy.transforms, policy.device = loader.transforms, 'cpu'
    actual = policy.prepare_inputs(validate_observation(observation()), images)
    assert actual.keys() == expected.keys()
    for key in expected:
        torch.testing.assert_close(actual[key], expected[key], rtol=0, atol=0)
    assert 'action' not in actual and actual['action_mask'][..., :23].all()
    assert not actual['action_mask'][..., 23:].any()
