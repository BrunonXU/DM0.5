"""F1 frozen RLT representation adapter; no robot I/O or online learner.

The token architecture is loaded verbatim from the pinned RLinf source. F1's
Policy owns preprocessing, the VLA and the EEF/RTC contract. Cached features
carry their parent identity so changing the VLA cannot silently reuse them.
"""
from __future__ import annotations

from collections import defaultdict
import importlib.util
import json
from pathlib import Path

import numpy as np
import torch

from .inference import batched_observation
from .processing import sha256


def token_class(core_root):
    path = Path(core_root) / "rlinf/models/embodiment/modules/rlt_token_transformer.py"
    spec = importlib.util.spec_from_file_location("f1_rlt_token_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RLTTokenTransformer


def select_anchors(rows, per_episode):
    """Deterministic temporal coverage, with no release-label oversampling."""
    if per_episode < 2:
        raise ValueError("Need at least two anchors per episode")
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row["episode_id"]].append(index)
    selected = []
    for episode in sorted(groups):
        indices = groups[episode]
        positions = np.linspace(0, len(indices) - 1, min(per_episode, len(indices)), dtype=int)
        selected.extend(indices[p] for p in positions)
    return selected


def validate_splits(train, validation):
    if train.manifest["split"] != "train" or validation.manifest["split"] != "validation":
        raise ValueError("Expected the original train/validation split")
    for key in ("split_sha256", "norm_stats_sha256", "contract_sha256"):
        if train.manifest[key] != validation.manifest[key]:
            raise ValueError("Split contract mismatch: " + key)
    for key in ("episode", "source_group"):
        a = {r.get(key, r["episode"]) for r in train.manifest["source_episodes"]}
        b = {r.get(key, r["episode"]) for r in validation.manifest["source_episodes"]}
        if a & b:
            raise ValueError("Train/validation leakage: " + key)


@torch.no_grad()
def extract_prefix(policy, observation):
    """Use the exact VLA prefix path, with evaluation preprocessing only."""
    if any(p.requires_grad for p in policy.model.parameters()):
        raise ValueError("The parent VLA must be explicitly frozen")
    obs = batched_observation(observation, policy.observation_type, policy.device)
    with torch.autocast("cuda", dtype=torch.bfloat16, enabled=str(policy.device).startswith("cuda")):
        prepared = policy.preprocess_observation(obs, train=False)
        prefix, mask, _ = policy.model.build_prefix_cache(prepared)
    if not torch.isfinite(prefix).all() or not mask.any(dim=1).all():
        raise ValueError("Invalid VLA prefix")
    return prefix.detach(), mask.detach(), obs.state.detach()


class PrefixCache:
    """Read immutable BF16 features without a VLA or action-label dependency."""
    def __init__(self, root, split, *, verify=True):
        self.root = Path(root)
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        if self.manifest["schema"] != "f1_rlt_prefix_cache_v1":
            raise ValueError("Unsupported RLT cache")
        info = self.manifest["splits"][split]
        self.records = info["records"]
        self.shape = tuple(info["shape"])
        if verify:
            for name, expected in info["files_sha256"].items():
                if sha256(self.root / name) != expected:
                    raise ValueError("Cache integrity failure: " + name)
        self.array = np.memmap(self.root / f"{split}.bf16", mode="c", dtype=np.uint16, shape=self.shape)
        self.masks = np.load(self.root / f"{split}-masks.npy", allow_pickle=False)
        if self.masks.shape != self.shape[:2] or self.masks.dtype != np.bool_:
            raise ValueError("Prefix mask shape/type mismatch")

    def __len__(self):
        return self.shape[0]

    def batch(self, indices, device):
        # Copy the selected rows; never mutate the memory-mapped source.
        data = torch.from_numpy(np.array(self.array[indices], copy=True)).view(torch.bfloat16)
        mask = torch.from_numpy(self.masks[indices].copy())
        return data.to(device), mask.to(device)


def reconstruction_mse(reconstruction, target, mask):
    """Per-observation masked MSE for episode-balanced evaluation."""
    errors = (reconstruction.float() - target.float()).square()
    return (errors * mask[..., None]).sum((1, 2)) / (mask.sum(1) * target.shape[-1]).clamp_min(1)


class FrozenTokenReader:
    """Verified encoder sidecar bound to one immutable F1 VLA bundle."""
    def __init__(self, artifact, policy, core_root):
        artifact = Path(artifact)
        manifest = json.loads((artifact / "manifest.json").read_text())
        if manifest["schema"] != "f1_rlt_encoder_v1" or manifest["parent_bundle_sha256"] != policy.bundle_id:
            raise ValueError("RLT encoder belongs to a different VLA bundle")
        source = Path(core_root) / "rlinf/models/embodiment/modules/rlt_token_transformer.py"
        if sha256(source) != manifest["token_source_sha256"]:
            raise ValueError("RLT source differs from the trained implementation")
        weights = artifact / "encoder.safetensors"
        if sha256(weights) != manifest["encoder_sha256"]:
            raise ValueError("Encoder weights changed")
        with torch.device("meta"):
            module = token_class(core_root)(**manifest["token_config"])
        self.encoder = module.encoder
        from safetensors.torch import load_file
        self.encoder.load_state_dict(load_file(str(weights)), strict=True, assign=True)
        self.encoder = self.encoder.to(policy.device).eval().requires_grad_(False)
        self.policy = policy
        self.policy.model.requires_grad_(False)

    @torch.no_grad()
    def encode(self, observation):
        prefix, mask, state = extract_prefix(self.policy, observation)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=str(self.policy.device).startswith("cuda")):
            z = self.encoder(prefix, mask).flatten(1)
        indices = self.policy.contract["active_state_indices"]
        return {"z_rl": z.float(), "proprio": state[:, indices].float()}
