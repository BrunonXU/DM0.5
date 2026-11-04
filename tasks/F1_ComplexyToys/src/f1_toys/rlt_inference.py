"""Supervised C16 RLT proposals using the serving process's one frozen VLA.

The actor predicts the original observation-relative sixteen-step chunk.
There is no RTC shift, reference tail, residual addition, or critic routing.
Only the client owns hold/execute timing, intervention and actual submission.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import time
import threading

import numpy as np
import torch

from .eef import decode_prefix, physical_actions, solver
from .inference import batched_observation
from .input_optimization import require_right_only
from .rlt import FrozenTokenReader
from .rlt_contract import file_sha, load_rlt_identity
from .rlt_stage2 import make_policy

REFERENCE_COMPUTE_PATHS=('training_eager_prefix_and_suffix',
                         'serving_graph_prefix_eager_suffix',
                         'serving_graph_prefix_and_suffix')


class RLTInference:
    """Immutable BC sidecar; the owning Server serializes all VLA access."""

    def __init__(self, policy, head_root, encoder_root, core_root, *, rtc=False):
        require_right_only(policy)
        self.policy = policy
        head_root, core_root = Path(head_root), Path(core_root)
        self.core_root = core_root
        self.rtc = rtc
        self.identity = load_rlt_identity(head_root, encoder_root, policy.bundle_id, rtc=rtc)
        # Frozen feature caches identify B/encoder computation, independently
        # of the rollout execution protocol and online actor version.
        self.feature_identity = load_rlt_identity(head_root, encoder_root, policy.bundle_id)
        self.manifest = json.loads((head_root/"manifest.json").read_text())
        if file_sha(Path(__file__).with_name("rlt_stage2.py")) != self.manifest["adapter_sha256"]:
            raise ValueError("RLT actor adapter differs from training")
        core_policy = core_root/"rlinf/models/embodiment/mlp_policy/rlt_mlp_policy.py"
        if file_sha(core_policy) != self.manifest["core_rlt_policy_sha256"]:
            raise ValueError("RLT core differs from training")
        if policy.stats["action"] != self.manifest["action_norm_stats"]:
            raise ValueError("RLT and base action normalization differ")
        self.reader = FrozenTokenReader(encoder_root, policy, core_root)
        self.actor = make_policy(core_root, self.manifest["model_config"])
        from safetensors.torch import load_file
        self.actor.load_state_dict(load_file(str(head_root/"head.safetensors")), strict=True)
        self.actor = self.actor.to(policy.device).eval().requires_grad_(False)
        self.initial_identity = self.identity
        self.initial_actor_weights = {k: v.detach().cpu().clone() for k, v in self.actor.state_dict().items()
                                      if k.startswith(('backbone.', 'actor_mean.'))}
        self.snapshots = None
        self.snapshot_stop = threading.Event()
        self.snapshot_thread = None
        self.segment_binding = None
        self.ready = False
        # Candidate optimization is opt-in. The default remains the frozen
        # training implementation until numerical equivalence is audited.
        self.reference_compute_path = 'training_eager_prefix_and_suffix'

    def configure_reference_compute(self, mode):
        """Select a versioned compute implementation before service warmup."""
        if self.ready:
            raise ValueError('Reference computation cannot change in a ready service')
        if mode not in REFERENCE_COMPUTE_PATHS:
            raise ValueError('Unknown RLT reference compute path')
        if mode!=REFERENCE_COMPUTE_PATHS[0] and not getattr(self.policy,'rtc_prefix_graph',None):
            raise ValueError('RLT graph candidate requires the prepared shared prefix graph')
        self.reference_compute_path=mode

    def configure_snapshots(self, root, encoder_root):
        from .rlt_snapshots import SnapshotRegistry
        self.snapshots = SnapshotRegistry(root, encoder_root, self.policy.bundle_id,
            self.policy.stats['action'], self.manifest['adapter_sha256'], self.manifest['core_rlt_policy_sha256'], rtc=self.rtc)
        self.snapshots.expected_shapes = {k: tuple(v.shape) for k, v in self.initial_actor_weights.items()}
        def watch():
            while not self.snapshot_stop.is_set():
                self.snapshots.refresh()
                self.snapshot_stop.wait(2.)
        self.snapshot_thread = threading.Thread(target=watch, daemon=True, name='rlt-snapshot-validator')
        self.snapshot_thread.start()

    def select_snapshot(self, version, segment_id=None):
        """Run on the existing serialized inference worker, before any forward."""
        if self.segment_binding and self.segment_binding[0] == segment_id and self.segment_binding[1] != version:
            raise ValueError('Actor version changed inside a local task')
        if version != self.initial_identity['model_version'] and not segment_id:
            raise ValueError('Online actor requires a local segment identity')
        if version == self.identity['model_version']:
            self.segment_binding = (segment_id, version)
            return
        if version == self.initial_identity['model_version']:
            weights, identity = self.initial_actor_weights, self.initial_identity
        else:
            if self.snapshots is None:
                raise ValueError('Online snapshots are not enabled')
            path, identity = self.snapshots.resolve(version)
            # Immutable assets are rechecked at activation, not trusted by name.
            from safetensors.torch import load_file
            if file_sha(path/'head.safetensors') != identity['rlt']['actor_version']:
                raise ValueError('Actor changed after validation')
            weights = load_file(str(path/'head.safetensors'))
        if set(weights) != set(self.initial_actor_weights) or any(
                weights[k].shape != self.initial_actor_weights[k].shape or not torch.isfinite(weights[k]).all()
                for k in weights):
            raise ValueError('Incompatible actor snapshot')
        # Build a complete replacement before exchanging the reference. A load
        # error leaves the prior module/identity available for rollback.
        replacement = make_policy(self.core_root,
                                  self.manifest['model_config'])
        state = replacement.state_dict()
        state.update(weights)
        replacement.load_state_dict(state, strict=True)
        replacement = replacement.to(self.policy.device).eval().requires_grad_(False)
        self.actor, self.identity = replacement, identity
        self.segment_binding = (segment_id, version)

    def close(self):
        self.snapshot_stop.set()
        if self.snapshot_thread is not None:
            self.snapshot_thread.join(timeout=3.)

    @torch.no_grad()
    def predict(self, images, state16, held16, *, prepared_pixels=False, seed=None, noise=None, features_only=False, actor_seed=None, rtc=None):
        policy = self.policy
        if not hasattr(policy, "ik"):
            policy.ik = solver()
        started = time.perf_counter()
        data = (policy.observation_from_pixels(images, state16) if prepared_pixels else
                policy.processor.observation(images, np.asarray(state16, float)))
        obs = batched_observation(data, policy.observation_type, policy.device)
        processed = time.perf_counter()
        seed = secrets.randbits(63) if seed is None else int(seed)
        generator = torch.Generator(device=policy.device).manual_seed(seed)
        # Capture the exact prefix used for this fresh unguided B reference.
        # The callback is restored even on failure. The server's shared busy
        # guard and single warmed worker prohibit concurrent B/RLT calls.
        original = policy.model.build_prefix_cache
        original_suffix = policy.model.run_suffix
        if (getattr(policy, "rtc_optimization", "eager") != "eager"
                and not all(hasattr(policy, key) for key in
                            ("rlt_reference_eager_prefix", "rlt_reference_eager_suffix"))):
            raise ValueError("Optimized B service lacks verified eager RLT reference paths")
        eager_prefix = getattr(policy, "rlt_reference_eager_prefix", original)
        eager_suffix = getattr(policy, "rlt_reference_eager_suffix", original_suffix)
        compute_path = getattr(self, 'reference_compute_path', 'training_eager_prefix_and_suffix')
        if features_only:
            # Historical human caches retain the frozen feature implementation.
            # Autonomous replay stores the exact feature inputs used at runtime.
            compute_path='training_eager_prefix_and_suffix'
        if compute_path in ('serving_graph_prefix_and_suffix', 'serving_graph_prefix_eager_suffix'):
            if not getattr(policy, 'rtc_prefix_graph', None):
                raise ValueError('RLT graph candidate requires the prepared shared prefix graph')
            # Keep Pi0.sample_actions and its exact Euler time grid. Only the
            # mathematical implementations are shared with the warmed B path.
            eager_prefix = original
            if compute_path == 'serving_graph_prefix_and_suffix':
                eager_suffix = original_suffix
            torch.compiler.cudagraph_mark_step_begin()
        elif compute_path != 'training_eager_prefix_and_suffix':
            raise ValueError('Unknown RLT reference compute path')
        had_override = "build_prefix_cache" in policy.model.__dict__
        had_suffix_override = "run_suffix" in policy.model.__dict__
        captured = []

        def capture(observation):
            prefix, mask, cache = eager_prefix(observation)
            captured.append((prefix.detach(), mask.detach(), observation.state.detach()))
            return prefix, mask, cache

        policy.model.build_prefix_cache = capture
        policy.model.run_suffix = eager_suffix
        try:
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=str(policy.device).startswith("cuda")):
                # Exactly the offline cache's Pi0.sample_actions entrypoint,
                # including its Euler time grid. Do not substitute RTC's loop.
                reference = policy.model.sample_actions(obs, num_steps=5, noise=noise, rng=generator)
                if len(captured) != 1:
                    raise ValueError("RLT requires exactly one matching VLA prefix")
                prefix, mask, state = captured[0]
                token = self.reader.encoder(prefix, mask).flatten(1).float()
        finally:
            if had_override:
                policy.model.build_prefix_cache = original
            else:
                del policy.model.build_prefix_cache
            if had_suffix_override:
                policy.model.run_suffix = original_suffix
            else:
                del policy.model.run_suffix
        proprio = state[:, policy.contract["active_state_indices"]].float()
        ref = reference.float()
        if ref.shape != (1, 50, 32) or not torch.isfinite(ref).all() or torch.any(ref[..., 10:] != 0):
            raise ValueError("RLT reference violates frozen H50 EEF10 mask")
        if token.shape != (1, 2048) or proprio.shape != (1, 8):
            raise ValueError("RLT feature dimensions changed")
        if not torch.isfinite(token).all() or not torch.isfinite(proprio).all():
            raise ValueError("Nonfinite RLT features")
        features = dict(z_rl=token, proprio=proprio, ref_chunk=ref[..., :10])
        if features_only:
            return {key: value[0].cpu().numpy().tolist() for key, value in features.items()}
        committed = [] if rtc is None else rtc['committed_actions16']
        if rtc is not None:
            if not getattr(self, 'rtc', False):
                raise ValueError('RLT RTC is not enabled')
            from .eef import encode_actions, normalize_actions
            from .rlt_prefix import add_empty_prefix
            features = add_empty_prefix(features)
            planned = rtc.get('next_decision_steps', 8)
            if type(planned) is not int or not len(committed) <= planned <= 16-len(committed):
                raise ValueError('Invalid RLT RTC next decision horizon')
            features['decision_steps'].fill_(planned)
            n = len(committed)
            if n:
                known = normalize_actions(encode_actions(policy.ik.model, state16, committed), policy.stats['action'])[:, :10]
                known = torch.as_tensor(known, device=ref.device, dtype=torch.float32)
                features['committed_actions'][:, :n] = known
                features['committed_mask'][:, :n] = True
                features['ref_chunk'] = features['ref_chunk'].clone()
                features['ref_chunk'][:, :n] = known
        # Preserve the trained FP32 actor math, including unbounded XYZ.
        from .rlt_sampling import sample_actor
        sampling = getattr(self, 'identity', {}).get('rlt', {}).get('actor_sampling')
        sampled, actor_seed = sample_actor(self.actor, features, sampling, seed=actor_seed)
        original_sampled = sampled.clone()
        from .rlt_prefix import with_prefix
        sampled = with_prefix(sampled, features)
        candidate = sampled.reshape(1, 16, 10)
        if not torch.isfinite(candidate).all():
            raise ValueError("Nonfinite RLT actor proposal")
        raw = np.zeros((16, 32), dtype=np.float32)
        raw[:, :10] = candidate[0].float().cpu().numpy()
        inferred = time.perf_counter()
        physical = physical_actions(raw, policy.stats["action"])
        if rtc is None:
            decoded = decode_prefix(policy.ik, physical, state16, held16, execute_steps=16)
        else:
            from .rtc import decode_window, absolute_eef
            decoded = decode_window(policy.ik, physical, state16, held16, committed, 16)
            decoded['absolute_eef10'] = absolute_eef(policy.ik, state16, raw, policy.stats['action']).tolist()
        finished = time.perf_counter()
        decoded.update(prediction_horizon=16, reference_horizon=50, execute_steps=16,
            timing_ms=dict(preprocess=(processed-started)*1000, model_h2d_d2h=(inferred-processed)*1000,
                           ik=(finished-inferred)*1000, policy=(finished-started)*1000),
            state_diagnostics=getattr(policy.processor, "last_state_diagnostics", {}))
        reference_for_actor = ref.clone()
        reference_for_actor[..., :10] = features['ref_chunk']
        replay = dict(reference_normalized_actions32=reference_for_actor[0].cpu().numpy().tolist(),
            actor_normalized_actions32=raw.tolist(), z_rl=token[0].cpu().numpy().tolist(),
            proprio=proprio[0].cpu().numpy().tolist(), reference_seed=seed,
            reference_denoise_steps=5, prefix_forward_calls=1, actor_deterministic=actor_seed is None,
            actor_sampling=sampling, actor_seed=actor_seed,
            reference_compute_path=compute_path,
            action_space="f1_normalized_relative_eef10_v1", observation_anchor_state16=list(state16),
            executed_transition=False, actor_inference_finished_monotonic_ns=time.monotonic_ns())
        if rtc is not None:
            replay.update(rtc=rtc, prefix_contract='known_prefix_detached_v1',
                unconstrained_actor_actions10=original_sampled.reshape(16,10).cpu().tolist(),
                committed_actions=features['committed_actions'][0].cpu().tolist(),
                committed_mask=features['committed_mask'][0].cpu().tolist(),
                decision_steps=int(features['decision_steps'][0]))
        return decoded, raw, replay

    def warm(self, images, state16, held16):
        # IK rejection is a valid response, not failure to load the network.
        self.predict(images, state16, held16, seed=20261026)
        self.ready = True
