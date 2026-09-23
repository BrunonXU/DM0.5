"""State19 / anchored EEF23 reader, independent of ROS and shared model code.

The source pack stays state21/action19. Candidate use must be explicit; passing
this reader is not data or robot acceptance. Cached FK uses mapped execution
targets and metres, with hashes binding it to the exact source pack.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

CONTRACT = "f1_state19_eef23_rot6d_rows_v1"
PARTIAL_BASE_CONTRACT = "f1_state19_eef23_partial_base_v2"
SUPPORTED_CONTRACTS = (CONTRACT, PARTIAL_BASE_CONTRACT)
PROMPT = "将泡沫棉从取料位置抓起，移动到放置位置放下，再返回取料侧。"
CAMERAS = ("head", "left", "right")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_npz(path):
    with np.load(path, allow_pickle=False) as archive:
        return {key: archive[key].copy() for key in archive.files}


def relative_pose(anchor, target):
    """Exact inv(anchor) @ target, with independent left/right anchors."""
    rotation = anchor[..., :3, :3].swapaxes(-1, -2)
    output = np.broadcast_to(np.eye(4), target.shape).copy()
    output[..., :3, :3] = rotation @ target[..., :3, :3]
    output[..., :3, 3] = (rotation @ (target[..., :3, 3] - anchor[..., :3, 3])[..., None])[..., 0]
    return output


def decode_rotation6d(rows, epsilon=1e-8):
    rows = np.asarray(rows, dtype=np.float64)
    a, b = rows[..., :3], rows[..., 3:]
    na = np.linalg.norm(a, axis=-1, keepdims=True)
    require(np.isfinite(rows).all() and (na > epsilon).all(), "invalid rotation6D first row")
    a = a / na
    b = b - (a * b).sum(axis=-1, keepdims=True) * a
    nb = np.linalg.norm(b, axis=-1, keepdims=True)
    require((nb > epsilon).all(), "degenerate rotation6D rows")
    b = b / nb
    return np.stack((a, b, np.cross(a, b)), axis=-2)


def decode_eef(action, anchor):
    """Decode unnormalized (...,23) labels to (...,2,4,4) targets."""
    action = np.asarray(action)
    output = np.broadcast_to(np.eye(4), action.shape[:-1] + (2, 4, 4)).copy()
    for arm, offset in enumerate((0, 9)):
        output[..., arm, :3, 3] = action[..., offset:offset + 3]
        output[..., arm, :3, :3] = decode_rotation6d(action[..., offset + 3:offset + 9])
    return anchor @ output


class Episode:
    def __init__(self, source, pose_cache, *, allow_candidates=False, horizon=50):
        self.source = Path(source).resolve()
        self.pose_cache = Path(pose_cache).resolve()
        self.data = read_npz(self.source)
        self.poses = read_npz(self.pose_cache)
        self.meta = json.loads(self.source.with_suffix(".json").read_text())
        self.contract = self.meta.get("reader_contract", CONTRACT)
        require(self.contract in SUPPORTED_CONTRACTS, "unknown reader contract")
        self.partial_base = self.contract == PARTIAL_BASE_CONTRACT
        self.pose_meta = json.loads(self.pose_cache.with_suffix(".json").read_text())
        self.horizon = int(horizon)
        require(self.horizon == 50, "this contract fixes H50; regenerate source windows for other horizons")
        self.source_sha256 = sha256(self.source)
        require(self.pose_meta["source_npz_sha256"] == self.source_sha256, "FK cache/source SHA mismatch")
        require(self.pose_meta["pose_cache_sha256"] == sha256(self.pose_cache), "FK cache artifact SHA mismatch")
        require(self.pose_meta["pose_unit"] == "m", "FK cache must use metres")
        require(self.pose_meta["arm_order"] == ["left", "right"], "FK arm order mismatch")
        require(bool(self.pose_meta.get("execution_mapping")), "FK execution mapping is missing")
        require(bool(self.pose_meta.get("kinematics_contract")), "FK contract is missing")
        self.kinematics_identity = json.dumps({key: self.pose_meta[key] for key in
                                               ("kinematics_contract", "execution_mapping", "arm_order", "pose_unit")}, sort_keys=True)
        self.episode = self.meta["episode"]
        self.split = self.meta["split"]
        require(self.split in ("train", "validation"), "unknown inherited split")
        self.training_approved = bool(self.meta.get("training_approved", False)) and bool(self.data["training_approved"])
        require(allow_candidates or self.training_approved, "candidate data requires explicit allow_candidates=True for engineering validation")
        n = len(self.data["time_ns"])
        require(np.array_equal(self.data["time_ns"], self.poses["time_ns"]), "FK time alignment mismatch")
        require(np.all(np.diff(self.data["time_ns"]) > 0), "source time must strictly increase")
        require(self.data["state"].shape == (n, 21) and self.data["action"].shape == (n, 19), "source layout must be state21/action19")
        for key, shape in {"state_valid": (n, 21), "action_candidate_valid": (n, 19),
                           "camera_timestamp_candidate_valid": (n, 3), "observation_candidate_valid": (n,),
                           "chunk_start_candidate_valid": (n,), "base_candidate_present": (n,),
                           "base_timing_reviewed": (n,)}.items():
            require(self.data[key].shape == shape and self.data[key].dtype == bool, f"invalid bool mask {key}")
        for kind in ("feedback", "command"):
            pose, valid = self.poses[kind + "_pose"], self.poses[kind + "_valid"]
            require(pose.shape == (n, 2, 4, 4) and valid.shape == (n, 2) and valid.dtype == bool, f"invalid {kind} FK layout")
            active = pose[valid]
            require(np.isfinite(active).all(), f"nonfinite valid {kind} FK")
            require(np.allclose(active[..., 3, :], [0, 0, 0, 1], atol=1e-6), "invalid homogeneous FK matrices")
            rotation = active[..., :3, :3]
            require(np.allclose(rotation @ rotation.swapaxes(-1, -2), np.eye(3), atol=2e-5), "FK rotation is not orthogonal")
            require(np.allclose(np.linalg.det(rotation), 1, atol=2e-5), "FK rotation determinant is not +1")
        base_valid = self.data["base_candidate_present"] & self.data["base_timing_reviewed"]
        base_valid &= self.data["action_candidate_valid"][:, 16:19].all(axis=1)
        base_valid &= np.isfinite(self.data["action"][:, 16:19]).all(axis=1)
        observation = self.data["observation_candidate_valid"] & self.data["state_valid"][:, :16].all(axis=1)
        observation &= np.isfinite(self.data["state"][:, :16]).all(axis=1)
        if not self.partial_base:
            observation &= base_valid
        observation &= self.data["camera_timestamp_candidate_valid"].all(axis=1)
        observation &= (self.data["camera_source_indices"] >= 0).all(axis=1)
        observation &= self.poses["feedback_valid"].all(axis=1)
        width = 16 if self.partial_base else 19
        future = self.data["action_candidate_valid"][:, :width].all(axis=1) & self.poses["command_valid"].all(axis=1)
        future &= np.isfinite(self.data["action"][:, :width]).all(axis=1)
        # Keep legacy body/state constraints embodied by source windows; never relax them.
        chunk_key = "upper_body_chunk_start_candidate_valid" if self.partial_base else "chunk_start_candidate_valid"
        require(chunk_key in self.data and self.data[chunk_key].shape == (n,) and self.data[chunk_key].dtype == bool,
                "missing independently derived upper-body window mask")
        valid = self.data[chunk_key].copy() & observation
        for t in np.flatnonzero(valid):
            valid[t] = t + self.horizon <= n and future[t:t + self.horizon].all()
        self.starts = np.flatnonzero(valid)
        self.base_valid = base_valid

    def sample(self, t):
        require(int(t) in self.starts, f"invalid sample start {t}")
        t = int(t)
        target = self.poses["command_pose"][t:t + self.horizon]
        anchor = self.poses["feedback_pose"][t]
        relative = relative_pose(anchor, target)
        action = np.zeros((self.horizon, 32), dtype=np.float32)
        mask = np.zeros_like(action, dtype=bool)
        source_mask = self.data["action_candidate_valid"][t:t + self.horizon]
        for arm, offset in enumerate((0, 9)):
            action[:, offset:offset + 3] = relative[:, arm, :3, 3]
            action[:, offset + 3:offset + 9] = relative[:, arm, :2, :3].reshape(-1, 6)
            mask[:, offset:offset + 9] = source_mask[:, arm * 7:(arm + 1) * 7].all(axis=1, keepdims=True)
        action[:, 18:23] = self.data["action"][t:t + self.horizon, 14:19]
        mask[:, 18:23] = source_mask[:, 14:19]
        mask[:, 20:23] &= self.base_valid[t:t + self.horizon, None]
        action[~mask] = 0
        state = np.concatenate((self.data["state"][t, :16], self.data["action"][t, 16:19])).astype(np.float32)
        state_mask = np.ones(19, dtype=bool)
        if self.partial_base and not self.base_valid[t]:
            state_mask[16:19] = False
            state[16:19] = 0  # Numeric container only; tokenizer emits 'unknown'.
        require(np.isfinite(state).all() and np.isfinite(action).all(), "nonfinite training sample")
        return {"state": state, "state_mask": state_mask, "action": action, "action_mask": mask, "prompt": PROMPT,
                "anchor": anchor.copy(), "camera_source_indices": self.data["camera_source_indices"][t].copy(),
                "meta_data": {"episode": self.episode, "frame_index": t, "time_ns": int(self.data["time_ns"][t]),
                              "split": self.split, "contract": self.contract, "robot_type": "F1", "control_mode": "EEF relative",
                              "base_state_known": bool(self.base_valid[t]),
                              "base_state_origin": "reconstructed_velocity_estimate", "training_approved": self.training_approved}}


def fit_statistics(episodes, std_floor=1e-6, state_arm_range="train_minmax"):
    """Stats over accepted starts and their H50 labels, train split only."""
    require(std_floor > 0, "std_floor must be positive")
    require(state_arm_range in ("train_minmax", "q01_q99"), "unknown arm state range policy")
    require(episodes and all(ep.split == "train" for ep in episodes), "statistics must receive only training episodes")
    require(len({ep.kinematics_identity for ep in episodes}) == 1, "mixed FK/execution contracts")
    require(len({getattr(ep, 'contract', CONTRACT) for ep in episodes}) == 1, "mixed reader contracts")
    # Keep exact state quantiles, but stream H50 action moments. Materializing
    # every overlapping action chunk exhausted memory on the full 772 pool.
    states = np.empty((sum(len(ep.starts) for ep in episodes), 19), dtype=np.float64)
    cursor = 0
    counts = np.zeros(23, dtype=np.int64)
    means, m2 = np.zeros(23), np.zeros(23)
    amin, amax = np.full(23, np.inf), np.full(23, -np.inf)
    actions, masks = [], []

    def accumulate():
        if not actions:
            return
        a = np.concatenate(actions).astype(np.float64)
        m = np.concatenate(masks)
        for dim in range(23):
            values = a[m[:, dim], dim]
            n = len(values)
            if not n:
                continue
            mean = values.mean()
            delta = mean - means[dim]
            total = counts[dim] + n
            m2[dim] += np.square(values - mean).sum() + delta * delta * counts[dim] * n / total
            means[dim] += delta * n / total
            counts[dim] = total
            amin[dim] = min(amin[dim], values.min())
            amax[dim] = max(amax[dim], values.max())
        actions.clear()
        masks.clear()
    for ep in episodes:
        for t in ep.starts:
            sample = ep.sample(t)
            states[cursor] = np.where(sample.get('state_mask', np.ones(19, bool)), sample["state"], np.nan)
            cursor += 1
            actions.append(sample["action"][:, :23])
            masks.append(sample["action_mask"][:, :23])
            if len(actions) >= 256:
                accumulate()
    accumulate()
    require(cursor > 0, "no valid training starts")
    state = states
    state_valid_count = np.isfinite(state).sum(axis=0)
    require((state_valid_count > 0).all(), "no known training state for a dimension")
    q01, q99 = np.nanquantile(state, [.01, .99], axis=0)
    low, high = q01.copy(), q99.copy()
    minimum, maximum = np.nanmin(state, axis=0), np.nanmax(state, axis=0)
    fallback = (q01 == q99) & (minimum != maximum)
    low[fallback], high[fallback] = minimum[fallback], maximum[fallback]
    # User-confirmed 2026-09-17: valid rare arm poses must remain representable.
    # Base input keeps its existing quantile rule; validation never fits ranges.
    if state_arm_range == "train_minmax":
        low[:14], high[:14] = minimum[:14], maximum[:14]
    constant = minimum == maximum
    center, scale = (low + high) / 2, (high - low) / 2
    center[constant], scale[constant] = minimum[constant], 1
    center[14:16], scale[14:16] = 50, 50
    acenter, ascale = np.zeros(23), np.ones(23)
    count, action_std, actual_constant = [], [], []
    for dim in range(23):
        require(counts[dim] > 0, f"no valid action labels for dim {dim}")
        std = float(np.sqrt(max(0., m2[dim] / counts[dim])))
        count.append(int(counts[dim])); action_std.append(std)
        actual_constant.append(bool(amin[dim] == amax[dim]))
        if dim in (0, 1, 2, 9, 10, 11, 20, 21, 22):
            acenter[dim] = means[dim]
            ascale[dim] = 1 if actual_constant[-1] else max(std, std_floor)
    acenter[18:20], ascale[18:20] = 50, 50
    return {"schema": getattr(episodes[0], 'contract', CONTRACT), "kinematics_identity": episodes[0].kinematics_identity,
            "training_approved": all(ep.training_approved for ep in episodes),
            "source_split": "train", "state_count": len(state), "state_valid_count": state_valid_count.tolist(), "std_floor": std_floor,
            "source_packs": [{"episode": ep.episode, "sha256": ep.source_sha256, "pose_sha256": sha256(ep.pose_cache),
                              "valid_starts": len(ep.starts)} for ep in episodes],
            "state": {"center": center.tolist(), "scale": scale.tolist(), "q01": q01.tolist(), "q99": q99.tolist(),
                      "range_policy": {"arms": state_arm_range, "grippers": "fixed_0_100", "base": "q01_q99_with_degenerate_minmax_fallback"},
                      "observed_min": minimum.tolist(), "observed_max": maximum.tolist(),
                      "minmax_fallback": fallback.tolist(), "constant": constant.tolist(), "n_bins": 256},
            "action": {"center": acenter.tolist(), "scale": ascale.tolist(), "valid_count": count,
                       "raw_std": action_std, "constant": actual_constant}}


class Normalizer:
    def __init__(self, stats):
        require(stats["schema"] in SUPPORTED_CONTRACTS and stats["source_split"] == "train", "normalizer contract mismatch")
        for field, dimension in (("state", 19), ("action", 23)):
            center, scale = np.asarray(stats[field]["center"]), np.asarray(stats[field]["scale"])
            require(center.shape == scale.shape == (dimension,), "normalizer shape mismatch")
            require(np.isfinite(center).all() and np.isfinite(scale).all() and (scale > 0).all(), "invalid normalizer affine parameters")
        self.stats = stats

    def __call__(self, sample):
        result = dict(sample)
        s, a = self.stats["state"], self.stats["action"]
        result["state"] = np.clip((sample["state"] - np.array(s["center"])) / np.array(s["scale"]), -1, 1).astype(np.float32)
        result["state"][~sample.get('state_mask', np.ones(19, bool))] = 0
        result["action"] = sample["action"].copy()
        result["action"][..., :23] = (result["action"][..., :23] - np.array(a["center"])) / np.array(a["scale"])
        result["action"][~sample["action_mask"]] = 0
        return result

    def denormalize_action(self, action):
        a = self.stats["action"]
        return np.asarray(action)[..., :23] * np.array(a["scale"]) + np.array(a["center"])


class F1Dataset:
    """Map-style torch-compatible dataset; imports torch only at model boundary.

    Supply a task image loader and shared ChatTokenization callable. Do not run
    shared BuildAction, ActionRelative, Normalize or PadAction a second time.
    """
    def __init__(self, episodes, normalizer=None, image_loader=None, tokenizer=None):
        require(bool(episodes), "empty dataset")
        require(len({ep.split for ep in episodes}) == 1, "do not mix train and validation")
        require(len({ep.episode for ep in episodes}) == len(episodes), "duplicate source recording")
        require(len({ep.kinematics_identity for ep in episodes}) == 1, "mixed FK/execution contracts")
        if normalizer is not None:
            require(normalizer.stats["kinematics_identity"] == episodes[0].kinematics_identity, "stats/FK contract mismatch")
            require(all(getattr(ep, 'contract', CONTRACT) == normalizer.stats['schema'] for ep in episodes), "stats/reader contract mismatch")
        self.episodes = list(episodes)
        self.normalizer, self.image_loader, self.tokenizer = normalizer, image_loader, tokenizer
        require(tokenizer is None or (normalizer is not None and image_loader is not None), "model input requires normalization and three images")
        self.index = [(i, int(t)) for i, ep in enumerate(episodes) for t in ep.starts]

    def __len__(self):
        return len(self.index)

    def __getitem__(self, index):
        i, t = self.index[index]
        ep = self.episodes[i]
        sample = ep.sample(t)
        if self.image_loader is not None:
            sample.update(self.image_loader(ep, sample["camera_source_indices"]))
        if self.normalizer is not None:
            sample = self.normalizer(sample)
        if self.tokenizer is not None:
            result = self.tokenizer(sample)
            # Shared TrainingCollator concatenates along batch, not horizon.
            result["action"] = result["action"].unsqueeze(0)
            result["action_mask"] = result["action_mask"].unsqueeze(0)
            return result
        return sample

    def balanced_indices(self, count, seed=42, epoch=0, rank=0, world_size=1):
        """Uniform source, then uniform valid start; deterministic DDP sharding."""
        require(count >= 0 and world_size > 0 and 0 <= rank < world_size, "invalid sampler arguments")
        groups = {}
        for i, (episode_index, _) in enumerate(self.index):
            groups.setdefault(episode_index, []).append(i)
        require(bool(groups), "no valid starts")
        rng = np.random.default_rng(np.random.SeedSequence([seed, epoch]))
        keys = list(groups)
        for draw in range(count * world_size):
            group = groups[keys[int(rng.integers(len(keys)))]]
            index = group[int(rng.integers(len(group)))]
            if draw % world_size == rank:
                yield index


class EpisodeBalancedSampler:
    """PyTorch DataLoader sampler protocol without importing torch on audit hosts."""
    def __init__(self, dataset, count, seed=42, rank=0, world_size=1):
        self.dataset, self.count, self.seed = dataset, count, seed
        self.rank, self.world_size, self.epoch = rank, world_size, 0

    def set_epoch(self, epoch):
        self.epoch = int(epoch)

    def __len__(self):
        return self.count

    def __iter__(self):
        return self.dataset.balanced_indices(self.count, self.seed, self.epoch, self.rank, self.world_size)
