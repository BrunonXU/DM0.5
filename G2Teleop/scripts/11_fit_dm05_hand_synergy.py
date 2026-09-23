#!/usr/bin/env python3
"""Fit the two-control O10 mapping from current-AID DM0.5 bottle-sort actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EPISODES = Path("/mnt/dm05_nvme/dm05/staged_episodes/full/episodes")
CURRENT_AID = "G2A0004C203358"
PIP = np.asarray([4, 5, 7, 9], dtype=np.intp)
ABDUCTION = np.asarray([3, 6, 8], dtype=np.intp)
THUMB = np.asarray([0, 1, 2], dtype=np.intp)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
    parser.add_argument("--aid", default=CURRENT_AID)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--max-episodes", type=int)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "configs/hand_synergy_dm05_current_aid.json",
    )
    return parser.parse_args()


def numeric_frames(handle: h5py.File) -> list[str]:
    return sorted((key for key in handle.keys() if key.isdigit()), key=int)


def load_samples(args: argparse.Namespace) -> tuple[dict[str, list[np.ndarray]], dict[str, Any]]:
    samples = {"left_action": [], "right_action": [], "left_state": [], "right_state": []}
    selected: list[Path] = []
    task_counts: dict[str, int] = {}
    for episode in sorted(args.episodes.glob("sorting_*")):
        meta_path = episode / "meta_info.json"
        h5_path = episode / "record/aligned_joints.h5"
        if not meta_path.is_file() or not h5_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("AID") != args.aid or not meta.get("data_validate", {}).get("validate", False):
            continue
        selected.append(h5_path)
        task = str(meta.get("task_id"))
        task_counts[task] = task_counts.get(task, 0) + 1
        if args.max_episodes is not None and len(selected) >= args.max_episodes:
            break
    if not selected:
        raise RuntimeError(f"no validated sorting episodes for AID {args.aid}")

    for index, h5_path in enumerate(selected, 1):
        with h5py.File(h5_path, "r") as handle:
            frames = numeric_frames(handle)[:: args.stride]
            for frame in frames:
                group = handle[frame]
                for side in ("left", "right"):
                    for kind in ("action", "state"):
                        value = np.asarray(
                            group[f"{kind}/{side}_effector/position"], dtype=np.float64
                        )
                        if value.shape == (10,) and np.isfinite(value).all():
                            samples[f"{side}_{kind}"].append(value)
        if index % 100 == 0:
            print(f"loaded {index}/{len(selected)} episodes", flush=True)
    arrays = {key: np.vstack(value) for key, value in samples.items()}
    return arrays, {
        "aid": args.aid,
        "episode_count": len(selected),
        "task_episode_counts": task_counts,
        "stride": args.stride,
    }


def pca_ratio(values: np.ndarray) -> list[float]:
    centered = values - np.mean(values, axis=0)
    singular = np.linalg.svd(centered, full_matrices=False, compute_uv=False)
    variance = singular * singular
    return (variance / np.sum(variance))[:4].tolist()


def fit_side(action: np.ndarray, state: np.ndarray) -> dict[str, Any]:
    low = np.quantile(action[:, PIP], 0.02, axis=0)
    high = np.quantile(action[:, PIP], 0.98, axis=0)
    span = np.maximum(high - low, 1e-6)
    grip = np.clip(np.mean((action[:, PIP] - low) / span, axis=1), 0.0, 1.0)
    open_mask = grip <= np.quantile(grip, 0.10)
    closed_mask = grip >= np.quantile(grip, 0.90)
    open_target = np.median(action[open_mask], axis=0)
    closed_target = np.median(action[closed_mask], axis=0)

    fixed_abduction = np.median(action[:, ABDUCTION], axis=0)
    open_target[ABDUCTION] = fixed_abduction
    closed_target[ABDUCTION] = fixed_abduction
    clearance = open_target[THUMB].copy()
    tracking = state - action[: len(state)] if len(state) == len(action) else np.empty((0, 10))
    return {
        "sample_count": int(len(action)),
        "joint_order": [
            "thumb_roll", "thumb_abad", "thumb_mcp", "index_abad", "index_pip",
            "middle_pip", "ring_abad", "ring_pip", "pinky_abad", "pinky_pip",
        ],
        "open_g0": open_target.tolist(),
        "closed_g1": closed_target.tolist(),
        "thumb_clearance_c1": clearance.tolist(),
        "thumb_clearance_semantics": "blend thumb 0:3 toward historical open-hand median",
        "fixed_abduction_indices": ABDUCTION.tolist(),
        "fixed_abduction": fixed_abduction.tolist(),
        "pca_explained_variance_ratio_first4": pca_ratio(action),
        "action_quantile_02": np.quantile(action, 0.02, axis=0).tolist(),
        "action_quantile_50": np.quantile(action, 0.50, axis=0).tolist(),
        "action_quantile_98": np.quantile(action, 0.98, axis=0).tolist(),
        "action_std": np.std(action, axis=0).tolist(),
        "state_minus_action_abs_quantile_95": (
            np.quantile(np.abs(tracking), 0.95, axis=0).tolist() if len(tracking) else None
        ),
    }


def main() -> int:
    args = parse_args()
    if args.stride <= 0:
        raise ValueError("--stride must be positive")
    arrays, source = load_samples(args)
    result = {
        "schema_version": 1,
        "status": "dm05_fitted_hardware_microtest_required",
        "powered_allowed": False,
        "source": source,
        "control_dimensions_per_hand": ["grip_g", "thumb_clearance_c"],
        "mapping": "linear g0->g1; c blends thumb joints 0:3 to clearance target; abduction fixed",
        "left": fit_side(arrays["left_action"], arrays["left_state"]),
        "right": fit_side(arrays["right_action"], arrays["right_state"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "source": source,
        "left_samples": result["left"]["sample_count"],
        "right_samples": result["right"]["sample_count"],
        "left_pca": result["left"]["pca_explained_variance_ratio_first4"],
        "right_pca": result["right"]["pca_explained_variance_ratio_first4"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
