#!/usr/bin/env python3
"""Summarize and pairwise-bootstrap completed NewLora validation results."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np


def checkpoint_step(path: Path) -> int:
    match = re.fullmatch(r"checkpoint-(\d+)\.json", path.name)
    if match is None:
        raise ValueError(f"unexpected result filename: {path}")
    return int(match.group(1))


def load_results(directory: Path) -> dict[int, dict]:
    return {
        checkpoint_step(path): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("checkpoint-*.json"))
    }


def percentile_interval(values: np.ndarray) -> tuple[float, float]:
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluation-root", required=True)
    parser.add_argument("--bootstrap-repeats", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--output")
    args = parser.parse_args()

    root = Path(args.evaluation_root)
    fm = load_results(root / "results" / "fm")
    actions = load_results(root / "results" / "actions")
    if set(fm) != set(actions):
        raise RuntimeError(
            f"FM/action checkpoint mismatch: {sorted(fm)} vs {sorted(actions)}"
        )
    if len(actions) != 15:
        raise RuntimeError(f"expected 15 checkpoints, got {len(actions)}")

    rows = []
    for step in sorted(actions):
        fm_result = fm[step]
        action_result = actions[step]
        if fm_result["num_episodes"] != 80 or action_result["num_episodes"] != 80:
            raise RuntimeError(f"checkpoint {step} is not a complete 80-episode result")
        metrics = action_result["episode_macro_metrics"]
        rows.append(
            {
                "step": step,
                "fm_loss": fm_result["episode_macro_mean_loss"],
                "fm_p90": fm_result["episode_macro_p90_loss"],
                "physical_mae": metrics["physical_mae_all"],
                "normalized_mae": metrics["normalized_mae_all"],
                "left_arm_mae": metrics["physical_mae_left_arm"],
                "right_arm_mae": metrics["physical_mae_right_arm"],
                "left_hand_mae": metrics["physical_mae_left_hand"],
                "right_hand_mae": metrics["physical_mae_right_hand"],
                "h1_mae": metrics["physical_mae_h1"],
                "h20_mae": metrics["physical_mae_h20"],
                "h50_mae": metrics["physical_mae_h50"],
                "first_jump_mean": metrics["first_action_jump_mae"],
                "first_jump_batch_max_mean": metrics["first_action_jump_max"],
                "out_of_q_fraction": metrics["out_of_train_q01_q99_fraction"],
                "seed_dispersion": metrics["seed_dispersion_mae"],
                "velocity_rmse": metrics["velocity_rmse_vs_target"],
                "acceleration_rmse": metrics["acceleration_rmse_vs_target"],
                "jerk_rmse": metrics["jerk_rmse_vs_target"],
            }
        )

    best_action_step = min(rows, key=lambda row: row["physical_mae"])["step"]
    best_fm_step = min(rows, key=lambda row: row["fm_loss"])["step"]
    best_episode = {
        record["episode"]: record["metrics"]["physical_mae_all"]
        for record in actions[best_action_step]["episodes"]
    }
    episode_order = sorted(best_episode)
    rng = np.random.default_rng(args.seed)
    bootstrap_indices = rng.integers(
        0,
        len(episode_order),
        size=(args.bootstrap_repeats, len(episode_order)),
    )
    paired = []
    for step in sorted(actions):
        candidate_episode = {
            record["episode"]: record["metrics"]["physical_mae_all"]
            for record in actions[step]["episodes"]
        }
        if set(candidate_episode) != set(best_episode):
            raise RuntimeError(f"episode mismatch for checkpoint {step}")
        differences = np.asarray(
            [candidate_episode[e] - best_episode[e] for e in episode_order],
            dtype=np.float64,
        )
        bootstrap_means = differences[bootstrap_indices].mean(axis=1)
        ci_low, ci_high = percentile_interval(bootstrap_means)
        paired.append(
            {
                "step": step,
                "reference_step": best_action_step,
                "mean_delta_physical_mae": float(differences.mean()),
                "median_delta_physical_mae": float(np.median(differences)),
                "ci95_low": ci_low,
                "ci95_high": ci_high,
                "episode_win_rate_vs_reference": float((differences < 0).mean()),
            }
        )

    summary = {
        "schema": "NewLora_validation_summary_v1",
        "num_checkpoints": len(rows),
        "num_episodes": 80,
        "best_fm_step": best_fm_step,
        "best_action_step": best_action_step,
        "bootstrap_repeats": args.bootstrap_repeats,
        "bootstrap_seed": args.seed,
        "rows": rows,
        "paired_vs_best_action": paired,
        "fm_ranking": [r["step"] for r in sorted(rows, key=lambda r: r["fm_loss"])],
        "action_ranking": [
            r["step"] for r in sorted(rows, key=lambda r: r["physical_mae"])
        ],
    }

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)

    print(
        "step fm_loss physical_mae norm_mae h1 h20 h50 jump out_q seed_disp"
    )
    for row in sorted(rows, key=lambda item: item["physical_mae"]):
        print(
            f"{row['step']:5d} {row['fm_loss']:.8f} {row['physical_mae']:.8f} "
            f"{row['normalized_mae']:.8f} {row['h1_mae']:.8f} "
            f"{row['h20_mae']:.8f} {row['h50_mae']:.8f} "
            f"{row['first_jump_mean']:.8f} {row['out_of_q_fraction']:.8f} "
            f"{row['seed_dispersion']:.8f}"
        )
    print(f"best_fm={best_fm_step} best_action={best_action_step}")
    print("paired_vs_best step mean_delta ci95_low ci95_high win_rate")
    for record in paired:
        print(
            f"{record['step']:5d} {record['mean_delta_physical_mae']:+.8f} "
            f"{record['ci95_low']:+.8f} {record['ci95_high']:+.8f} "
            f"{record['episode_win_rate_vs_reference']:.3f}"
        )


if __name__ == "__main__":
    main()
