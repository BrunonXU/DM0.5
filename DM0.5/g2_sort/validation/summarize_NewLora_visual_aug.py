#!/usr/bin/env python3
"""Summarize paired 70K/75K NewLora visual augmentation robustness results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def ci_record(values: np.ndarray, bootstrap_indices: np.ndarray) -> dict:
    boot = values[bootstrap_indices].mean(axis=1)
    return {
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "ci95_low": float(np.quantile(boot, 0.025)),
        "ci95_high": float(np.quantile(boot, 0.975)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--bootstrap-repeats", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    results = {
        step: json.loads((Path(args.results_dir) / f"checkpoint-{step}.json").read_text(encoding="utf-8"))
        for step in (70000, 75000)
    }
    for step, result in results.items():
        if result["num_episodes"] != 80 or result["num_unique_samples"] != 320 or result["seed_repeats"] != 3:
            raise RuntimeError(f"incomplete visual result: {step}")
    episode_maps = {
        step: {record["episode"]: record["conditions"] for record in result["episodes"]}
        for step, result in results.items()
    }
    episodes = sorted(episode_maps[70000])
    if sorted(episode_maps[75000]) != episodes:
        raise RuntimeError("70K and 75K episode identities differ")
    rng = np.random.default_rng(args.seed)
    boot_idx = rng.integers(0, len(episodes), size=(args.bootstrap_repeats, len(episodes)))
    conditions = results[70000]["conditions"]
    condition_records = {}

    for condition in conditions:
        condition_record = {"checkpoints": {}}
        for step in (70000, 75000):
            metrics = episode_maps[step]
            original_mae = np.asarray([metrics[e]["original"]["physical_mae_all"] for e in episodes])
            condition_mae = np.asarray([metrics[e][condition]["physical_mae_all"] for e in episodes])
            record = {
                "physical_mae": ci_record(condition_mae, boot_idx),
                "physical_mae_degradation_vs_original": ci_record(condition_mae - original_mae, boot_idx),
                "first_action_jump_mae": ci_record(
                    np.asarray([metrics[e][condition]["first_action_jump_mae"] for e in episodes]), boot_idx
                ),
                "out_of_train_q01_q99_fraction": ci_record(
                    np.asarray([metrics[e][condition]["out_of_train_q01_q99_fraction"] for e in episodes]), boot_idx
                ),
            }
            if condition == "original":
                record["seed_dispersion_mae"] = ci_record(
                    np.asarray([metrics[e][condition]["seed_dispersion_mae"] for e in episodes]), boot_idx
                )
            else:
                for metric in (
                    "visual_delta_mae_all",
                    "visual_delta_mae_arm",
                    "visual_delta_mae_hand",
                    "visual_delta_mae_h1",
                    "visual_delta_mae_h20",
                    "visual_delta_max",
                    "pixel_tensor_delta_mae",
                    "arm_reach_cosine_h20",
                ):
                    record[metric] = ci_record(
                        np.asarray([metrics[e][condition][metric] for e in episodes]), boot_idx
                    )
            condition_record["checkpoints"][str(step)] = record

        comparisons = {}
        metric_names = list(condition_record["checkpoints"]["70000"])
        def episode_metric(step: int, episode: str, metric: str) -> float:
            condition_metrics = episode_maps[step][episode][condition]
            if metric == "physical_mae":
                return condition_metrics["physical_mae_all"]
            if metric == "physical_mae_degradation_vs_original":
                return (
                    condition_metrics["physical_mae_all"]
                    - episode_maps[step][episode]["original"]["physical_mae_all"]
                )
            return condition_metrics[metric]

        for metric in metric_names:
            values_75 = np.asarray([episode_metric(75000, e, metric) for e in episodes])
            values_70 = np.asarray([episode_metric(70000, e, metric) for e in episodes])
            delta = values_75 - values_70
            comparisons[metric] = {
                **ci_record(delta, boot_idx),
                "75k_episode_lower_rate": float((delta < 0).mean()),
            }
        condition_record["comparison_75k_minus_70k"] = comparisons
        condition_records[condition] = condition_record

    output = {
        "schema": "NewLora_visual_aug_paired_summary_v1",
        "steps": [70000, 75000],
        "num_episodes": 80,
        "samples_per_episode": 4,
        "generation_seeds": 3,
        "diffusion_steps": 10,
        "bootstrap_repeats": args.bootstrap_repeats,
        "bootstrap_seed": args.seed,
        "conditions": condition_records,
    }
    output_path = Path(args.output)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)

    for condition, record in condition_records.items():
        print(condition)
        for step in (70000, 75000):
            metrics = record["checkpoints"][str(step)]
            line = f"  {step}: mae={metrics['physical_mae']['mean']:.8f} degradation={metrics['physical_mae_degradation_vs_original']['mean']:+.8f}"
            if condition != "original":
                line += f" visual_delta={metrics['visual_delta_mae_all']['mean']:.8f} cosine={metrics['arm_reach_cosine_h20']['mean']:.6f}"
            print(line)
        comparison = record["comparison_75k_minus_70k"]["physical_mae_degradation_vs_original"]
        print(
            f"  degradation 75K-70K={comparison['mean']:+.8f} "
            f"CI=[{comparison['ci95_low']:+.8f},{comparison['ci95_high']:+.8f}]"
        )


if __name__ == "__main__":
    main()
