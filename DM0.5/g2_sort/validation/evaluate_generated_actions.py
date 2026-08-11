#!/usr/bin/env python3
"""Compare deployment-path DM0.5 action chunks against frozen validation data."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import numpy as np
import torch
from transformers import AutoProcessor

from g2_sort.training.dm05_lora import DM05DataConfig, DM05ModelConfig


GROUPS = {
    "left_arm": slice(0, 7),
    "right_arm": slice(7, 14),
    "left_hand": slice(14, 24),
    "right_hand": slice(24, 34),
    "all": slice(0, 34),
}
HORIZONS = [0, 4, 9, 24, 49]


def denormalize(array: np.ndarray, stats: dict) -> np.ndarray:
    lo = np.asarray(stats["q01"], dtype=np.float32)
    hi = np.asarray(stats["q99"], dtype=np.float32)
    output = (array + 1.0) / 2.0 * (hi - lo + 1e-6) + lo
    return np.where((lo == 0) & (hi == 0), 0.0, output).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--seed-repeats", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("generated-action evaluation requires CUDA")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    data_config = DM05DataConfig(
        dataset_name="g2_sort_validation",
        norm_stats_root=args.norm_stats_root,
    )
    train_norm_path = DM05DataConfig(
        dataset_name="g2_sort_train",
        norm_stats_root=args.norm_stats_root,
    ).norm_stats_path(action_horizon=50)
    data_config.norm_stats_path = lambda _action_horizon: train_norm_path
    dataset, collator = data_config.build_dataset(
        processor=processor,
        action_horizon=50,
        tokenizer_max_length=1024,
    )
    stats = json.loads(Path(train_norm_path).read_text(encoding="utf-8"))[
        "norm_stats"
    ]
    model = DM05ModelConfig(
        model_name_or_path=args.model,
        llm_attn_implementation="sdpa",
        vision_attn_implementation="flash_attention_2",
        action_attn_implementation="sdpa",
        liger_kernel=False,
        vlm_gradient_checkpointing=False,
        ae_gradient_checkpointing=False,
    ).build_model(use_lora=False).to(device).eval()

    normalized_abs = {name: [] for name in GROUPS}
    physical_abs = {name: [] for name in GROUPS}
    horizon_physical_abs = {str(step + 1): [] for step in HORIZONS}
    episode_physical_mae: dict[str, list[float]] = {}
    first_action_jump = []
    velocity_error_sq = []
    acceleration_error_sq = []
    jerk_error_sq = []
    out_of_train_quantile = []
    seed_dispersion = []
    first_repeat: dict[tuple[str, int], np.ndarray] = {}
    raw_line_cache: dict[str, list[str]] = {}

    def physical_states(indices: list[int]) -> np.ndarray:
        states = []
        for index in indices:
            file_id, frame_index = dataset.sample_index[index]
            jsonl_path = dataset.id_to_jsonl[file_id]
            if jsonl_path not in raw_line_cache:
                raw_line_cache[jsonl_path] = Path(jsonl_path).read_text(
                    encoding="utf-8"
                ).splitlines()
            states.append(
                json.loads(raw_line_cache[jsonl_path][frame_index])["state"]
            )
        return np.asarray(states, dtype=np.float32)

    with torch.inference_mode():
        for episode_offset, group in enumerate(manifest["episodes"]):
            episode = group["episode"]
            episode_physical_mae[episode] = []
            indices = group["indices"]
            for batch_offset, start in enumerate(
                range(0, len(indices), args.batch_size)
            ):
                batch_indices = indices[start : start + args.batch_size]
                instances = [dataset[index] for index in batch_indices]
                state = physical_states(batch_indices)
                batch = {
                    key: value.to(device)
                    for key, value in collator(instances).items()
                }
                # Reuse decoded/tokenized observations across generation seeds.
                for repeat in range(args.seed_repeats):
                    seed = (
                        args.seed
                        + repeat * 1_000_000
                        + episode_offset * 10_000
                        + batch_offset
                    )
                    torch.manual_seed(seed)
                    prediction = model.inference_action(
                        input_ids=batch["input_ids"],
                        attention_mask=batch["attention_mask"],
                        pixel_values=batch["pixel_values"],
                        token_type_ids=batch["token_type_ids"],
                        diffusion_steps=args.diffusion_steps,
                        action_mask=batch["action_mask"],
                    )
                    pred_norm = (
                        prediction[:, :, :34].float().cpu().numpy().astype(np.float32)
                    )
                    target_norm = (
                        batch["action"][:, :, :34]
                        .float()
                        .cpu()
                        .numpy()
                        .astype(np.float32)
                    )
                    if not np.isfinite(pred_norm).all():
                        raise RuntimeError(
                            f"non-finite prediction: {episode} {batch_indices}"
                        )
                    pred = denormalize(pred_norm, stats["action"])
                    target = denormalize(target_norm, stats["action"])

                    for name, group_slice in GROUPS.items():
                        normalized_abs[name].append(
                            float(np.abs(pred_norm[..., group_slice] - target_norm[..., group_slice]).mean())
                        )
                        physical_abs[name].append(
                            float(np.abs(pred[..., group_slice] - target[..., group_slice]).mean())
                        )
                    for horizon in HORIZONS:
                        horizon_physical_abs[str(horizon + 1)].append(
                            float(np.abs(pred[:, horizon] - target[:, horizon]).mean())
                        )
                    batch_mae = float(np.abs(pred - target).mean())
                    episode_physical_mae[episode].append(batch_mae)
                    first_action_jump.append(
                        float(np.abs(pred[:, 0] - state).mean())
                    )
                    pred_v = np.diff(pred, axis=1) * 30.0
                    target_v = np.diff(target, axis=1) * 30.0
                    pred_a = np.diff(pred_v, axis=1) * 30.0
                    target_a = np.diff(target_v, axis=1) * 30.0
                    pred_j = np.diff(pred_a, axis=1) * 30.0
                    target_j = np.diff(target_a, axis=1) * 30.0
                    velocity_error_sq.append(float(np.square(pred_v - target_v).mean()))
                    acceleration_error_sq.append(float(np.square(pred_a - target_a).mean()))
                    jerk_error_sq.append(float(np.square(pred_j - target_j).mean()))
                    out_of_train_quantile.append(float((np.abs(pred_norm) > 1.0).mean()))
                    key = (episode, batch_offset)
                    if repeat == 0:
                        first_repeat[key] = pred
                    else:
                        seed_dispersion.append(
                            float(np.abs(pred - first_repeat[key]).mean())
                        )
            print(
                f"episode={episode} "
                f"physical_mae={statistics.mean(episode_physical_mae[episode]):.6f}"
            )

    episode_means = {
        episode: statistics.mean(values)
        for episode, values in episode_physical_mae.items()
    }
    result = {
        "model": args.model,
        "manifest": args.manifest,
        "diffusion_steps": args.diffusion_steps,
        "seed_repeats": args.seed_repeats,
        "num_episodes": len(episode_means),
        "num_unique_samples": manifest["num_unique_samples"],
        "normalized_mae_by_group": {
            name: statistics.mean(values) for name, values in normalized_abs.items()
        },
        "physical_mae_by_group": {
            name: statistics.mean(values) for name, values in physical_abs.items()
        },
        "physical_mae_by_horizon": {
            step: statistics.mean(values)
            for step, values in horizon_physical_abs.items()
        },
        "episode_macro_physical_mae": statistics.mean(episode_means.values()),
        "episode_p90_physical_mae": sorted(episode_means.values())[
            math.ceil(0.9 * len(episode_means)) - 1
        ],
        "first_action_jump_mae": statistics.mean(first_action_jump),
        "velocity_rmse_vs_target": math.sqrt(statistics.mean(velocity_error_sq)),
        "acceleration_rmse_vs_target": math.sqrt(
            statistics.mean(acceleration_error_sq)
        ),
        "jerk_rmse_vs_target": math.sqrt(statistics.mean(jerk_error_sq)),
        "out_of_train_q01_q99_fraction": statistics.mean(out_of_train_quantile),
        "seed_dispersion_mae": (
            statistics.mean(seed_dispersion) if seed_dispersion else 0.0
        ),
        "episode_physical_mae": episode_means,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
