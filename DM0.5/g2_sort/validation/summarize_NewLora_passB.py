#!/usr/bin/env python3
"""Pairwise episode-bootstrap report for NewLora dense Pass B."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np


METRICS = {
    "physical_mae_all": "identity",
    "normalized_mae_all": "identity",
    "physical_mae_left_arm": "identity",
    "physical_mae_right_arm": "identity",
    "physical_mae_left_hand": "identity",
    "physical_mae_right_hand": "identity",
    "physical_mae_h1": "identity",
    "physical_mae_h5": "identity",
    "physical_mae_h10": "identity",
    "physical_mae_h20": "identity",
    "physical_mae_h25": "identity",
    "physical_mae_h50": "identity",
    "first_action_jump_mae": "identity",
    "first_action_jump_max": "identity",
    "out_of_train_q01_q99_fraction": "identity",
    "seed_dispersion_mae": "identity",
    "velocity_mse": "sqrt",
    "acceleration_mse": "sqrt",
    "jerk_mse": "sqrt",
}


def step_from_path(path: Path) -> int:
    match = re.fullmatch(r"checkpoint-(\d+)\.json", path.name)
    if match is None:
        raise ValueError(path)
    return int(match.group(1))


def transformed(value: float, mode: str) -> float:
    return math.sqrt(value) if mode == "sqrt" else value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--bootstrap-repeats", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    results = {
        step_from_path(path): json.loads(path.read_text(encoding="utf-8"))
        for path in Path(args.results_dir).glob("checkpoint-*.json")
    }
    if set(results) != {65000, 70000, 75000}:
        raise RuntimeError(f"expected 65K/70K/75K, got {sorted(results)}")

    episode_maps: dict[int, dict[str, dict]] = {}
    for step, result in results.items():
        if result["num_episodes"] != 80 or result["num_unique_samples"] != 1280:
            raise RuntimeError(f"incomplete Pass B result: {step}")
        episode_maps[step] = {
            record["episode"]: record["metrics"] for record in result["episodes"]
        }
    episodes = sorted(episode_maps[65000])
    if any(sorted(mapping) != episodes for mapping in episode_maps.values()):
        raise RuntimeError("Pass B episode identities differ")

    rng = np.random.default_rng(args.seed)
    bootstrap_indices = rng.integers(
        0,
        len(episodes),
        size=(args.bootstrap_repeats, len(episodes)),
    )
    comparisons = []
    for left, right in ((65000, 70000), (75000, 70000), (75000, 65000)):
        metric_records = {}
        for metric, mode in METRICS.items():
            left_values = np.asarray(
                [transformed(episode_maps[left][e][metric], mode) for e in episodes]
            )
            right_values = np.asarray(
                [transformed(episode_maps[right][e][metric], mode) for e in episodes]
            )
            delta = left_values - right_values
            boot = delta[bootstrap_indices].mean(axis=1)
            metric_records[metric] = {
                "left_mean": float(left_values.mean()),
                "right_mean": float(right_values.mean()),
                "mean_delta_left_minus_right": float(delta.mean()),
                "median_delta_left_minus_right": float(np.median(delta)),
                "ci95_low": float(np.quantile(boot, 0.025)),
                "ci95_high": float(np.quantile(boot, 0.975)),
                "left_episode_win_rate": float((delta < 0).mean()),
            }
        comparisons.append(
            {"left_step": left, "right_step": right, "metrics": metric_records}
        )

    output = {
        "schema": "NewLora_dense_passB_paired_v1",
        "steps": sorted(results),
        "num_episodes": 80,
        "samples_per_episode": 16,
        "num_unique_samples": 1280,
        "generation_seeds": 5,
        "diffusion_steps": 10,
        "bootstrap_repeats": args.bootstrap_repeats,
        "bootstrap_seed": args.seed,
        "macro_metrics": {
            str(step): result["episode_macro_metrics"]
            for step, result in sorted(results.items())
        },
        "comparisons": comparisons,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)

    for comparison in comparisons:
        print(f"{comparison['left_step']} minus {comparison['right_step']}")
        for metric, record in comparison["metrics"].items():
            print(
                f"  {metric}: delta={record['mean_delta_left_minus_right']:+.9f} "
                f"ci95=[{record['ci95_low']:+.9f}, {record['ci95_high']:+.9f}] "
                f"win={record['left_episode_win_rate']:.3f}"
            )


if __name__ == "__main__":
    main()
