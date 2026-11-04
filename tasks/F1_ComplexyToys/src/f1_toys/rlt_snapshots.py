"""Immutable actor snapshots; activation is explicit at local-task boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from .rlt_contract import load_rlt_identity, file_sha


class SnapshotRegistry:
    def __init__(self, root, encoder, base_version, action_stats, adapter_sha, core_sha, *, rtc=False):
        self.rtc = rtc
        self.root, self.encoder = Path(root), Path(encoder)
        self.base_version, self.action_stats = base_version, action_stats
        self.adapter_sha, self.core_sha = adapter_sha, core_sha
        self.lock = threading.Lock()
        self.entries = {}
        self.latest = None
        self.error = None
        self.expected_shapes = None

    def refresh(self):
        """A publisher advances latest.json only after validating a whole export."""
        try:
            selection_path = self.root/'selection.json'
            channel = json.loads(selection_path.read_text())['channel'] if selection_path.exists() else 'trial'
            if channel not in ('latest', 'baseline', 'trial', 'stable'):
                raise ValueError('Invalid actor registry channel')
            pointer_path = self.root/(channel+'.json')
            if channel == 'trial' and not pointer_path.exists():
                pointer_path = self.root/'latest.json'
            pointer = json.loads(pointer_path.read_text())
            if channel == 'stable' and pointer.get('evaluation_conclusion') != 'verified_improvement':
                raise ValueError('Stable selection needs task-level evaluation evidence')
            name = pointer['directory']
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('Snapshot path escaped registry')
            source = self.root/name
            path = source.resolve()
            if path.parent != self.root.resolve() or source.is_symlink():
                raise ValueError('Snapshot escaped registry')
            if file_sha(path/'manifest.json') != pointer['manifest_sha256']:
                raise ValueError('Snapshot manifest checksum mismatch')
            head = json.loads((path/'manifest.json').read_text())
            if (head.get('deployment_approved') is not True
                    or head.get('action_norm_stats') != self.action_stats
                    or head.get('adapter_sha256') != self.adapter_sha
                    or head.get('core_rlt_policy_sha256') != self.core_sha):
                raise ValueError('Snapshot is not an approved compatible actor')
            identity = load_rlt_identity(path, self.encoder, self.base_version, allow_online=True, rtc=self.rtc)
            from safetensors.torch import load_file
            import torch
            weights = load_file(str(path/'head.safetensors'))
            if not weights or any(not k.startswith(('backbone.', 'actor_mean.')) for k in weights):
                raise ValueError('Expected actor-only snapshot')
            if self.expected_shapes is None or set(weights) != set(self.expected_shapes) or any(
                    tuple(weights[k].shape) != self.expected_shapes[k] for k in weights):
                raise ValueError('Snapshot actor keys or shapes differ')
            if not all(torch.isfinite(value).all() for value in weights.values()):
                raise ValueError('Nonfinite snapshot')
            with self.lock:
                self.entries[identity['model_version']] = (path, identity)
                self.latest, self.error = identity, None
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def candidate(self):
        with self.lock:
            return None if self.latest is None else dict(self.latest)

    def resolve(self, version):
        with self.lock:
            return self.entries[version]


def snapshot_name(learner, evaluation=None, provenance=None):
    import hashlib
    from .rlt_evaluation import actor_digest
    evidence = hashlib.sha256(json.dumps(dict(evaluation=evaluation, provenance=provenance), sort_keys=True).encode()).hexdigest()[:8]
    return f'actor-{learner.step:09d}-{actor_digest(learner.model)[:12]}-{evidence}'


def publish_actor(learner, initial_manifest, output, *, deployment_approved=False, evaluation=None, provenance=None):
    """Publish a distinct immutable directory; never replace a loaded file."""
    import os
    from safetensors.torch import save_file
    from .rlt_online import atomic_json, sha
    if deployment_approved:
        from .rlt_evaluation import actor_digest
        if (not evaluation or evaluation.get('schema') != 'f1_rlt_candidate_evaluation_v1'
                or evaluation.get('passed') is not True
                or evaluation.get('actor_state_sha256') != actor_digest(learner.model)
                or not evaluation.get('validation_identity')
                or not evaluation.get('checks') or not all(evaluation['checks'].values())):
            raise ValueError('Actor promotion requires matching passed held-out evaluation')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name = snapshot_name(learner, evaluation, provenance)
    target, partial = output/name, output/(name+'.partial')
    if target.exists() or partial.exists():
        raise FileExistsError('Snapshot already exists')
    partial.mkdir()
    from .rlt_conditioning import export_actor_state
    weights = export_actor_state(learner.model)
    save_file(weights, str(partial/'head.safetensors'))
    manifest = dict(initial_manifest, schema='f1_rlt_local_head_v2', weights_scope='actor_only',
                    candidate_status='trial_eligible' if deployment_approved else 'candidate',
                    performance_verified=False,
                    objective='approach_place_release_v1', checkpoint_step=learner.step,
                    phase='q' if learner.q_updates else 'bc', q_guidance_updates=learner.q_updates,
                    head_sha256=sha(partial/'head.safetensors'), deployment_approved=deployment_approved)
    if learner.cfg.get('stochastic_actor', False):
        manifest['actor_sampling'] = dict(schema='f1_rlt_sampling_v1', mode='fixed_gaussian',
                                          fixed_std=learner.cfg['model']['fixed_std'])
    if evaluation and evaluation.get('promotion_policy') == 'supervised_online_v1':
        manifest['deployment_scope'] = 'supervised_online_trial'
    # Initial BC400 provenance is ancestry, never the identity of this export.
    ancestry_keys = ('checkpoint_sha256', 'checkpoint_run_id', 'source_checkpoint',
                     'recipe_sha256', 'replay_manifest_sha256', 'inherited_actor_updates',
                     'inherited_critic_updates', 'export_reload_critic_exact')
    manifest['initial_training_provenance'] = {k: manifest.pop(k) for k in ancestry_keys if k in manifest}
    provenance = dict(provenance or {})
    manifest['training_provenance'] = provenance
    manifest['checkpoint_run_id'] = provenance.get('run_id')
    manifest['training_phase'] = manifest['phase']
    manifest['recipe_sha256'] = __import__('hashlib').sha256(
        json.dumps(learner.cfg, sort_keys=True).encode()).hexdigest()
    if provenance.get('replay_catalog'):
        manifest['replay_manifest_sha256'] = __import__('hashlib').sha256(
            json.dumps(provenance['replay_catalog'], sort_keys=True).encode()).hexdigest()
    manifest.update(asynchronous_runner=True, reference_mode='frozen_B1250_online_reference',
                    q_branch_has_new_updates=bool(learner.q_updates),
                    actor_updates=(learner.step + learner.cfg['actor_update_interval'] - 1)//learner.cfg['actor_update_interval'],
                    critic_updates=learner.step, training_recipe=dict(learner.cfg),
                    initial_actor_manifest_sha256=__import__('hashlib').sha256(
                        json.dumps(initial_manifest, sort_keys=True).encode()).hexdigest())
    if evaluation is not None:
        atomic_json(partial/'evaluation.json', evaluation)
        manifest['evaluation_sha256'] = sha(partial/'evaluation.json')
    atomic_json(partial/'manifest.json', manifest)
    os.replace(partial, target)
    if deployment_approved:
        pointer = dict(directory=name, manifest_sha256=sha(target/'manifest.json'), channel='trial',
                       performance_verified=False)
        atomic_json(output/'trial.json', pointer)
        atomic_json(output/'latest.json', pointer)  # Legacy alias; same sole publisher.
    return target
