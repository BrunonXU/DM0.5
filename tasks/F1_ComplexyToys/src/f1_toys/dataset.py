"""Consume audited anchor windows directly, without mixing LeRobot environments."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset, DistributedSampler, Subset, Sampler

from .processing import Processor, active_cameras, load_contract, model_actions, sha256


class ExactValidationSampler(Sampler):
    """Disjoint shards, including empty ranks; never pad or discard an index."""
    def __init__(self, dataset, num_replicas, rank):
        if not 0 <= rank < num_replicas:
            raise ValueError('Invalid validation rank')
        self.indices = range(rank, len(dataset), num_replicas)

    def __iter__(self):
        return iter(self.indices)

    def __len__(self):
        return len(self.indices)


class F1Dataset(Dataset):
    def __init__(self, root: str | Path, *, allow_smoke_only: bool = False, training=False):
        self.root = Path(root)
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        self.training = training
        if self.manifest["schema"] not in ("f1_toys_dataset_v1", "f1_toys_eef_dataset_v2", "f1_toys_eef_dataset_v3"):
            raise ValueError("Unsupported F1 dataset schema")
        if self.manifest["smoke_only"] and not allow_smoke_only:
            raise ValueError("This dataset is authorized for pipeline smoke only")
        for name, digest in self.manifest["files_sha256"].items():
            path = (self.root / name).resolve()
            if self.root.resolve() not in path.parents or sha256(path) != digest:
                raise ValueError(f"Dataset file integrity failure: {name}")
        self.contract = load_contract(self.root / "contract.json")
        self.stats = json.loads((self.root / "norm_stats.json").read_text())
        self.rows = [json.loads(line) for line in (self.root / "samples.jsonl").read_text().splitlines()]
        with np.load(self.root / "arrays.npz", allow_pickle=False) as arrays:
            self.states, self.actions = arrays["states"], arrays["actions"]
            self.time_masked = self.manifest['schema'] == 'f1_toys_eef_dataset_v3'
            self.action_time_mask = arrays['action_time_mask'] if self.time_masked else np.ones(self.actions.shape[:2], dtype=bool)
        mask = self.action_time_mask
        if (mask.dtype != np.bool_ or mask.shape != self.actions.shape[:2] or not mask[:, 0].all()
                or np.any(mask[:, 1:] & ~mask[:, :-1])):
            raise ValueError('Action time mask must be a nonempty contiguous boolean prefix')
        if self.time_masked and not self.contract.get('temporal_supervision'):
            raise ValueError('Missing temporal supervision contract')
        if self.time_masked and [r["valid_action_steps"] for r in self.rows] != mask.sum(1).tolist():
            raise ValueError('Sample metadata and time mask disagree')
        self.eef = self.contract["schema"] == "f1_toys_eef_contract_v2"
        if self.states.shape != (len(self.rows), 16) or self.actions.shape != (len(self.rows), self.contract["horizon"], 10 if self.eef else 16):
            raise ValueError("Dataset array shape mismatch")
        from .tokenizer import Tokenizer
        self.processor = Processor(self.contract, self.stats, Tokenizer(self.contract["max_token_len"]))

    def __len__(self):
        return len(self.rows)

    def raw_images(self, index: int):
        images = {}
        for camera, relative in self.rows[index]["images"].items():
            if camera not in active_cameras(self.contract):
                continue
            with Image.open(self.root / relative) as image:
                images[camera] = image.convert("RGB")
        return images

    def __getitem__(self, index):
        # num_workers=0: upstream checkpoint saves/restores this NumPy RNG exactly.
        seed = int(np.random.randint(0, 2**32)) if self.training and self.contract["train_image_augmentation"] else None
        observation = self.processor.observation(self.raw_images(index), self.states[index], augmentation_seed=seed)
        if self.manifest.get('sampling'):
            observation['_f1_metadata'] = {k: self.rows[index][k]
                for k in ('dataset_role', 'episode_id', 'supervision_phase', 'valid_action_steps')}
            if hasattr(self, 'sampling_focus_indices'):
                observation['_f1_metadata']['sampling_focus'] = index in self.sampling_focus_indices
        if self.eef:
            from .eef import normalize_actions
            labels = normalize_actions(self.actions[index], self.stats["action"])
            if self.time_masked:
                labels = np.where(self.action_time_mask[index, :, None], labels, 0.)
                return observation, labels, self.action_time_mask[index]
            return observation, labels
        return observation, model_actions(self.actions[index], self.stats["action"])


def collate(items):
    from rlinf.models.embodiment.openpi_rlinf.pi0_model.model import Observation
    if not items or len({len(item) for item in items}) != 1:
        raise ValueError('Cannot mix masked and unmasked batch structures')
    observations, actions = zip(*(item[:2] for item in items))
    batch = {}
    for key in observations[0]:
        if key == '_f1_metadata':
            continue
        if isinstance(observations[0][key], dict):
            batch[key] = {name: torch.from_numpy(np.stack([obs[key][name] for obs in observations])) for name in observations[0][key]}
        else:
            batch[key] = torch.from_numpy(np.stack([obs[key] for obs in observations]))
    result = (Observation.from_dict(batch), torch.from_numpy(np.stack(actions)))
    if '_f1_metadata' in observations[0]:
        result[0]._f1_metadata = [obs['_f1_metadata'] for obs in observations]
    if len(items[0]) == 3:
        return (*result, torch.from_numpy(np.stack([item[2] for item in items])))
    return result


def build_sft_dataloader(cfg, world_size, rank, data_paths, eval_dataset=False):
    from torchdata.stateful_dataloader import StatefulDataLoader

    expected_gpu = os.environ.get("F1_TOYS_GPU_INDEX")
    actual = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    if expected_gpu is not None and (not actual or not set(actual.split(",")).issubset(set(expected_gpu.split(",")))):
        raise ValueError("RLinf worker GPU placement differs from the reserved task GPU")
    if not isinstance(data_paths, str) and len(data_paths) != 1:
        raise ValueError("Use one audited multi-episode package; multiple paths would silently omit data")
    root = data_paths if isinstance(data_paths, str) else data_paths[0]
    dataset = F1Dataset(root, allow_smoke_only=bool(cfg.data.get("allow_smoke_only", False)), training=not eval_dataset)
    for key in ("contract_sha256", "norm_stats_sha256", "split_sha256"):
        if cfg.data.get(key) is not None and cfg.data[key] != dataset.manifest.get(key):
            raise ValueError("Dataset differs from the frozen recipe: " + key)
    if eval_dataset and dataset.manifest.get("split") != "validation":
        raise ValueError("Validation requires an independent episode split")
    if not eval_dataset and dataset.manifest.get("split") == "validation":
        raise ValueError("Cannot train on the held-out validation package")
    if int(cfg.data.num_workers) != 0:
        raise ValueError("Exact augmentation resume currently requires num_workers=0")
    contract = dataset.contract
    if int(cfg.actor.model.num_action_chunks) != contract["horizon"]:
        raise ValueError("Model and dataset horizons disagree")
    expected_mask = [i in contract["active_action_indices"] for i in range(32)]
    if list(cfg.actor.model.openpi.action_loss_mask) != expected_mask:
        raise ValueError("Model and dataset action loss masks disagree")
    if cfg.actor.model.openpi.train_image_augmentation or not cfg.actor.model.openpi.mask_inactive_action_inputs:
        raise ValueError("Model preprocessing does not match F1 contract")
    selected = dataset
    batch_size = int(cfg.actor.micro_batch_size)
    if eval_dataset:
        indices = json.loads((dataset.root / "eval_indices.json").read_text())
        if not indices or len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < len(dataset) for i in indices):
            raise ValueError("Invalid fixed validation index list")
        batch_size = int(cfg.actor.get("eval_batch_size", 2))
        selected = Subset(dataset, indices)
        train_paths = cfg.data.train_data_paths
        train_root = train_paths if isinstance(train_paths, str) else train_paths[0]
        train = json.loads((Path(train_root) / "manifest.json").read_text())
        if train.get("split") != "train" or any(train[k] != dataset.manifest[k] for k in ("split_sha256", "norm_stats_sha256", "contract_sha256")):
            raise ValueError("Train/validation contracts or train-only statistics disagree")
        if {r["episode"] for r in train["source_episodes"]} & {r["episode"] for r in dataset.manifest["source_episodes"]}:
            raise ValueError("Train/validation episode leakage")
    mixture = cfg.data.get('mixture_counts')
    focus_indices, focus_multiplier = [], 1.0
    focus_path = cfg.data.get('stage_focus_manifest')
    if focus_path and not eval_dataset:
        if mixture is None:
            raise ValueError('Stage focus requires explicit source/episode sampling')
        if sha256(Path(focus_path)) != cfg.data.get('stage_focus_sha256'):
            raise ValueError('Stage focus manifest differs from the recipe')
        focus = json.loads(Path(focus_path).read_text())
        if (focus.get('schema') != 'f1_release_focus_v1'
                or focus.get('dataset_manifest_sha256') != sha256(dataset.root / 'manifest.json')):
            raise ValueError('Stage focus belongs to another training dataset')
        focus_indices = focus['indices']
        if (len(set(focus_indices)) != len(focus_indices)
                or any(type(i) is not int or not 0 <= i < len(dataset) for i in focus_indices)):
            raise ValueError('Invalid stage focus indices')
        # Do not trust a hand-edited list: recompute the audited label criterion.
        grip = dataset.actions[:, :, 9] * 50 + 50
        expected = np.flatnonzero((dataset.states[:, 15] >= 90) &
            ((grip[:, :30] <= 10) & dataset.action_time_mask[:, :30]).any(axis=1)).tolist()
        if focus_indices != expected:
            raise ValueError('Stage focus does not match actual valid release labels')
        dataset.sampling_focus_indices = set(focus_indices)
        focus_multiplier = float(cfg.data.get('stage_focus_multiplier', 1.0))
    elif cfg.data.get('stage_focus_multiplier', 1.0) != 1.0 and not eval_dataset:
        raise ValueError('Stage weighting requires an audited focus manifest')
    if mixture is not None:
        expected = dict(method='source_episode_anchor_v1', counts=dict(mixture),
                        global_batch=int(cfg.actor.global_batch_size))
        if dataset.manifest.get('sampling') != expected:
            raise ValueError('Sampling recipe differs from the frozen dataset')
    elif dataset.manifest.get('sampling'):
        raise ValueError('Mixed dataset requires its explicit sampling recipe')
    if eval_dataset:
        sampler = ExactValidationSampler(selected, world_size, rank)
    elif mixture is not None:
        from .sampling import SourceEpisodeSampler
        sampler = SourceEpisodeSampler(dataset.rows, counts=dict(mixture),
            global_batch=int(cfg.actor.global_batch_size), num_replicas=world_size,
            rank=rank, seed=int(cfg.actor.seed), focus_indices=focus_indices,
            focus_multiplier=focus_multiplier)
    else:
        sampler = DistributedSampler(selected, num_replicas=world_size, rank=rank,
                                     shuffle=True, seed=int(cfg.actor.seed))
    loader = StatefulDataLoader(selected, batch_size=batch_size, sampler=sampler,
                        num_workers=int(cfg.data.num_workers), collate_fn=collate, drop_last=not eval_dataset)
    if not len(loader) and not eval_dataset:
        raise ValueError("Dataset has fewer samples than one training batch")
    return loader, dataset.manifest
