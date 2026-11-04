"""Explicit legacy model input completion; never substitutes robot feedback.

The optional profile retains the original three-camera/state16 processor. Only
the model's private state copy receives the removed left gripper's proven train
constant. Physical state, FK anchors and held targets remain caller-owned.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

PROFILE = "legacy_left_gripper_zero_v1"
SEMANTICS = "zero_storage_placeholder_not_measurement"
MASK = [True] * 14 + [False, True]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ModelStateCompletion:
    """Delegate to the unchanged processor with a separate model-only copy."""

    def __init__(self, processor):
        self.original = processor

    def __getattr__(self, name):
        return getattr(self.original, name)

    @staticmethod
    def model_copy(state16):
        state = np.asarray(state16)
        if state.shape != (16,) or not np.isfinite(state).all():
            raise ValueError("Invalid physical state for model completion")
        model_state = state.copy()
        model_state[14] = 0
        return model_state

    def observation(self, images, state16, **kwargs):
        return self.original.observation(images, self.model_copy(state16), **kwargs)

    def model_state(self, state16):
        return self.original.model_state(self.model_copy(state16))


class LegacyLeftGripperZero:
    def __init__(self, policy, dataset, bundle):
        contract, stats = policy.contract, policy.stats
        if (contract.get("schema") != "f1_toys_eef_contract_v2"
                or contract.get("state_dim") != 16
                or contract.get("cameras") != ["head", "left_wrist", "right_wrist"]
                or any(k in contract for k in ("input_version", "active_state_indices", "active_cameras"))):
            raise ValueError("Legacy completion requires the original three-camera/state16 contract")
        low, high = (np.asarray(stats["state"][key], float) for key in ("low", "high"))
        if low.shape != (16,) or high.shape != (16,) or low[14] != 0 or high[14] != 100:
            raise ValueError("Legacy completion requires the original physical gripper normalization")

        dataset, bundle = Path(dataset), Path(bundle)
        manifest_sha = digest(dataset / "manifest.json")
        if manifest_sha != digest(bundle / "dataset_manifest.json"):
            raise ValueError("Completion evidence dataset does not match the frozen bundle")
        if (json.loads((dataset / "contract.json").read_text()) != contract
                or json.loads((dataset / "norm_stats.json").read_text()) != stats):
            raise ValueError("Completion evidence contract/statistics do not match the frozen bundle")
        with np.load(dataset / "arrays.npz", allow_pickle=False) as arrays:
            states = arrays["states"]
            if states.ndim != 2 or states.shape[1] != 16 or not len(states):
                raise ValueError("Completion evidence requires nonempty state16 anchors")
            if not np.all(states[:, 14] == 0):
                raise ValueError("Removed left gripper was not constant zero in the bound training data")
            count = len(states)
        self.identity = dict(
            name=PROFILE, model_only_indices=[14], model_only_values=[0.0],
            physical_state_semantics=SEMANTICS, state_valid_mask=MASK.copy(),
            cameras="original_three_camera_contract", physical_feedback_modified=False,
            training_evidence=dict(dataset_manifest_sha256=manifest_sha,
                arrays_sha256=digest(dataset / "arrays.npz"), anchors=count,
                left_gripper_min=0.0, left_gripper_max=0.0))
        policy.processor = ModelStateCompletion(policy.processor)

    def validate_request(self, meta):
        mask = meta.get("state_valid_mask")
        if (not isinstance(mask, list) or len(mask) != 16
                or any(type(v) is not bool for v in mask) or mask != MASK):
            raise ValueError("Legacy completion requires exactly the removed-left-gripper validity mask")
        if meta.get("absent_state_semantics") != SEMANTICS:
            raise ValueError("Missing explicit absent-hardware semantics")
        for key in ("state16", "last_submitted16"):
            value = np.asarray(meta.get(key), float)
            if value.shape != (16,) or not np.isfinite(value).all() or value[14] != 0:
                raise ValueError("Absent left gripper must have zero storage placeholder: " + key)
        stamps = meta.get("state_timestamps_ns")
        if (not isinstance(stamps, list) or len(stamps) != 16
                or any(type(v) is not int for v in stamps)
                or stamps[14] != 0
                or any(stamps[i] <= 0 for i, valid in enumerate(MASK) if valid)):
            raise ValueError("Absent gripper must have no measurement timestamp; present fields require timestamps")
        # The robot's existing observation-age and feedback checks remain the
        # authority for freshness. Cross-machine monotonic clocks are not compared.
        return dict(self.identity)


def install_state_input_profile(name, policy, dataset, bundle):
    if name is None:
        return None
    if name != PROFILE:
        raise ValueError("Unsupported state input profile")
    return LegacyLeftGripperZero(policy, dataset, bundle)
