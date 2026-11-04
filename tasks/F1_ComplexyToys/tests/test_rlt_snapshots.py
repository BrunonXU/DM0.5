"""Snapshot validation and atomic, segment-pinned activation without robot IO."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
torch = pytest.importorskip('torch')
pytest.importorskip('safetensors')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_snapshots import SnapshotRegistry, publish_actor
from f1_toys.rlt_contract import file_sha
from f1_toys import rlt_inference


class TinyActor(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.backbone = torch.nn.Linear(2, 2)
        self.actor_mean = torch.nn.Linear(2, 2)
        self.q_head = torch.nn.Linear(2, 1)


def test_snapshot_roundtrip_boundary_pin_and_corruption_fallback(tmp_path, monkeypatch):
    enc = tmp_path/'encoder'; enc.mkdir()
    (enc/'encoder.safetensors').write_bytes(b'encoder-fixture')
    (enc/'manifest.json').write_text(json.dumps(dict(schema='f1_rlt_encoder_v1',
        parent_bundle_sha256='base', encoder_sha256=file_sha(enc/'encoder.safetensors'))))
    model = TinyActor()
    initial = dict(model_id='old', model_version='old-version', rlt={})
    initial_weights = {k: v.detach().clone() for k,v in model.state_dict().items() if not k.startswith('q_head.')}
    manifest = dict(parent_bundle_sha256='base', encoder_manifest_sha256=file_sha(enc/'manifest.json'),
        model_config=dict(z_dim=2048, proprio_dim=8, action_dim=10, chunk_len=16, ref_chunk_len=50),
        output_parameterization='linear_normalized_xyz_tanh_rotation6d_gripper',
        action_norm_stats={'test': 1}, adapter_sha256='adapter', core_rlt_policy_sha256='core')
    learner = SimpleNamespace(model=model, step=4, q_updates=0, cfg={'actor_update_interval':4})
    with torch.no_grad(): model.actor_mean.bias.add_(1.)
    root = tmp_path/'snapshots'
    from f1_toys.rlt_evaluation import actor_digest
    evaluation = dict(schema='f1_rlt_candidate_evaluation_v1', passed=True,
        actor_state_sha256=actor_digest(model), validation_identity={'synthetic_unit_fixture':True}, checks={'fixture':True})
    with pytest.raises(ValueError, match='evaluation'):
        publish_actor(learner, manifest, root, deployment_approved=True)
    path = publish_actor(learner, manifest, root, deployment_approved=True, evaluation=evaluation)
    registry = SnapshotRegistry(root, enc, 'base', {'test':1}, 'adapter', 'core')
    registry.expected_shapes = {k: tuple(v.shape) for k,v in initial_weights.items()}
    assert registry.refresh()
    candidate = registry.candidate()
    (root/'baseline.json').write_bytes((root/'latest.json').read_bytes())
    learner.step = 8
    learner.q_updates = 1
    learner.cfg.update(stochastic_actor=True, model={'fixed_std':.002})
    manifest['model_config']['fixed_std'] = .002
    trial = publish_actor(learner, manifest, root, deployment_approved=True, evaluation=evaluation)
    assert registry.refresh()
    assert registry.candidate()['rlt']['actor_sampling']['mode'] == 'fixed_gaussian'
    (root/'selection.json').write_text(json.dumps({'channel':'baseline'}))
    assert registry.refresh() and registry.candidate() == candidate
    (root/'selection.json').write_text(json.dumps({'channel':'invalid'}))
    assert not registry.refresh() and registry.candidate() == candidate
    (root/'selection.json').write_text(json.dumps({'channel':'baseline'}))
    sidecar = rlt_inference.RLTInference.__new__(rlt_inference.RLTInference)
    sidecar.initial_identity = sidecar.identity = initial
    sidecar.initial_actor_weights = initial_weights
    sidecar.segment_binding = None
    sidecar.snapshots = registry
    sidecar.policy = SimpleNamespace(device='cpu')
    sidecar.manifest = manifest; sidecar.core_root = tmp_path; sidecar.actor = TinyActor()
    monkeypatch.setattr(rlt_inference, 'make_policy', lambda *args: TinyActor())
    sidecar.select_snapshot(candidate['model_version'], 'episode-1:placement:1')
    torch.testing.assert_close(sidecar.actor.actor_mean.bias, model.actor_mean.bias)
    with pytest.raises(ValueError, match='inside a local task'):
        sidecar.select_snapshot('old-version', 'episode-1:placement:1')
    sidecar.select_snapshot('old-version', 'episode-2:placement:1')
    torch.testing.assert_close(sidecar.actor.actor_mean.bias, initial_weights['actor_mean.bias'])
    old_actor = sidecar.actor
    (path/'head.safetensors').write_bytes(b'corrupt-after-validation')
    assert not registry.refresh()
    assert registry.candidate() == candidate  # last validated metadata remains, activation rechecks bytes
    with pytest.raises(ValueError, match='changed after validation'):
        sidecar.select_snapshot(candidate['model_version'], 'episode-3:placement:1')
    assert sidecar.actor is old_actor and sidecar.identity == initial


def test_unapproved_snapshot_is_never_advertised(tmp_path):
    learner = SimpleNamespace(model=TinyActor(), step=1, q_updates=0, cfg={'actor_update_interval':4})
    publish_actor(learner, {}, tmp_path, deployment_approved=False)
    assert not (tmp_path/'latest.json').exists()


def test_online_export_does_not_claim_ancestor_run_or_replay(tmp_path):
    learner = SimpleNamespace(model=TinyActor(), step=3, q_updates=0, cfg={'actor_update_interval':1})
    initial = dict(checkpoint_run_id='old-bc', source_checkpoint='/old/best.pt',
                   checkpoint_sha256='old-sha', recipe_sha256='old-recipe',
                   replay_manifest_sha256='old-data', training_phase='old')
    source = dict(run_id='new-online', replay_catalog={'new-episode': {'split':'train','manifest_sha256':'new'}})
    path = publish_actor(learner, initial, tmp_path, provenance=source)
    manifest = json.loads((path/'manifest.json').read_text())
    assert manifest['checkpoint_run_id'] == 'new-online'
    assert manifest['initial_training_provenance']['checkpoint_run_id'] == 'old-bc'
    assert manifest['training_provenance'] == source
    assert 'source_checkpoint' not in manifest and 'checkpoint_sha256' not in manifest
    assert manifest['recipe_sha256'] != 'old-recipe' and manifest['replay_manifest_sha256'] != 'old-data'
    assert manifest['training_phase'] == 'bc'
