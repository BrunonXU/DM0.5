#!/usr/bin/env python3
"""Generate deployment-path NewLora chunks and score frozen validation data."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import numpy as np
import torch
from transformers import AutoProcessor

from g2_sort.validation.newlora_common import (
    ACTION_DIM,
    ACTION_HORIZON,
    GROUPS,
    HORIZON_INDICES,
    NewLoraValidationDataConfig,
    build_model,
    denormalize_quantile,
    load_norm_stats,
    mixed_to_absolute,
    sha256_file,
    validate_static_contract,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--seed-repeats", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--compile-action-expert", action="store_true")
    parser.add_argument("--max-episodes", type=int)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if min(args.batch_size, args.seed_repeats, args.diffusion_steps) < 1:
        raise ValueError("batch-size, seed-repeats and diffusion-steps must be positive")
    validate_static_contract()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("NewLora action validation requires CUDA")

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode_groups = manifest["episodes"]
    if args.max_episodes is not None:
        episode_groups = episode_groups[: args.max_episodes]
    norm_path, stats = load_norm_stats(args.norm_stats_root)

    processor = AutoProcessor.from_pretrained(
        args.processor,
        local_files_only=True,
        trust_remote_code=True,
    )
    data_config = NewLoraValidationDataConfig(norm_stats_root=args.norm_stats_root)
    dataset, collator = data_config.build_dataset(
        processor=processor,
        action_horizon=ACTION_HORIZON,
        tokenizer_max_length=1024,
    )
    torch.manual_seed(args.seed)
    model = build_model(args.model, device)
    if args.compile_action_expert:
        model.model.action_expert = torch.compile(
            model.model.action_expert,
            mode="reduce-overhead",
            dynamic=False,
            fullgraph=False,
        )

    raw_line_cache: dict[str, list[str]] = {}

    def physical_states(indices: list[int]) -> np.ndarray:
        values = []
        for index in indices:
            file_id, frame_index = dataset.sample_index[index]
            jsonl_path = dataset.id_to_jsonl[file_id]
            if jsonl_path not in raw_line_cache:
                raw_line_cache[jsonl_path] = Path(jsonl_path).read_text(
                    encoding="utf-8"
                ).splitlines()
            values.append(json.loads(raw_line_cache[jsonl_path][frame_index])["state"])
        result = np.asarray(values, dtype=np.float32)
        if result.shape[-1] != ACTION_DIM:
            raise RuntimeError(f"raw state is not 34D: {result.shape}")
        return result

    episode_records: list[dict] = []
    with torch.inference_mode():
        for episode_offset, group in enumerate(episode_groups):
            indices = group["indices"]
            metric_lists: dict[str, list[float]] = {}
            first_by_batch: dict[int, np.ndarray] = {}

            def add(name: str, value: float) -> None:
                metric_lists.setdefault(name, []).append(float(value))

            for batch_offset, start in enumerate(range(0, len(indices), args.batch_size)):
                batch_indices = indices[start : start + args.batch_size]
                state = physical_states(batch_indices)
                batch = {
                    key: value.to(device)
                    for key, value in collator(
                        [dataset[index] for index in batch_indices]
                    ).items()
                }
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
                    pred_norm = prediction[:, :, :ACTION_DIM].float().cpu().numpy()
                    target_norm = batch["action"][:, :, :ACTION_DIM].float().cpu().numpy()
                    if pred_norm.shape[1:] != (ACTION_HORIZON, ACTION_DIM):
                        raise RuntimeError(f"invalid generated shape: {pred_norm.shape}")
                    if not np.isfinite(pred_norm).all():
                        raise RuntimeError(f"non-finite prediction: {group['episode']}")

                    pred_mixed = denormalize_quantile(pred_norm, stats["action"])
                    target_mixed = denormalize_quantile(target_norm, stats["action"])
                    pred_abs = mixed_to_absolute(pred_mixed, state)
                    target_abs = mixed_to_absolute(target_mixed, state)

                    for name, group_slice in GROUPS.items():
                        add(
                            f"normalized_mae_{name}",
                            np.abs(
                                pred_norm[..., group_slice]
                                - target_norm[..., group_slice]
                            ).mean(),
                        )
                        add(
                            f"physical_mae_{name}",
                            np.abs(
                                pred_abs[..., group_slice]
                                - target_abs[..., group_slice]
                            ).mean(),
                        )
                    for horizon in HORIZON_INDICES:
                        add(
                            f"physical_mae_h{horizon + 1}",
                            np.abs(pred_abs[:, horizon] - target_abs[:, horizon]).mean(),
                        )
                    add("first_action_jump_mae", np.abs(pred_abs[:, 0] - state).mean())
                    add("first_action_jump_max", np.abs(pred_abs[:, 0] - state).max())

                    pred_v = np.diff(pred_abs, axis=1) * 30.0
                    target_v = np.diff(target_abs, axis=1) * 30.0
                    pred_a = np.diff(pred_v, axis=1) * 30.0
                    target_a = np.diff(target_v, axis=1) * 30.0
                    pred_j = np.diff(pred_a, axis=1) * 30.0
                    target_j = np.diff(target_a, axis=1) * 30.0
                    add("velocity_mse", np.square(pred_v - target_v).mean())
                    add("acceleration_mse", np.square(pred_a - target_a).mean())
                    add("jerk_mse", np.square(pred_j - target_j).mean())
                    add("out_of_train_q01_q99_fraction", (np.abs(pred_norm) > 1.0).mean())

                    if repeat == 0:
                        first_by_batch[batch_offset] = pred_abs
                    else:
                        add(
                            "seed_dispersion_mae",
                            np.abs(pred_abs - first_by_batch[batch_offset]).mean(),
                        )

            episode_metrics = {
                name: statistics.mean(values) for name, values in metric_lists.items()
            }
            episode_records.append(
                {
                    "episode": group["episode"],
                    "num_unique_samples": len(indices),
                    "metrics": episode_metrics,
                }
            )
            print(
                f"episode={group['episode']} "
                f"physical_mae={episode_metrics['physical_mae_all']:.6f} "
                f"jump={episode_metrics['first_action_jump_mae']:.6f}"
            )

    metric_names = sorted(
        {name for episode in episode_records for name in episode["metrics"]}
    )
    macro = {
        name: statistics.mean(
            episode["metrics"][name]
            for episode in episode_records
            if name in episode["metrics"]
        )
        for name in metric_names
    }
    for source, target in (
        ("velocity_mse", "velocity_rmse_vs_target"),
        ("acceleration_mse", "acceleration_rmse_vs_target"),
        ("jerk_mse", "jerk_rmse_vs_target"),
    ):
        macro[target] = math.sqrt(macro.pop(source))

    result = {
        "schema": "NewLora_generated_action_validation_v1",
        "model": args.model,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "norm_stats": str(norm_path),
        "norm_stats_sha256": sha256_file(norm_path),
        "action_contract": "arm_relative_14_hand_absolute_20",
        "augmentation": "NoAugmentationPipeline",
        "diffusion_steps": args.diffusion_steps,
        "compile_action_expert": args.compile_action_expert,
        "seed": args.seed,
        "seed_repeats": args.seed_repeats,
        "batch_size": args.batch_size,
        "num_episodes": len(episode_records),
        "num_unique_samples": sum(e["num_unique_samples"] for e in episode_records),
        "episode_macro_metrics": macro,
        "episodes": episode_records,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    print(json.dumps({k: v for k, v in result.items() if k != "episodes"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
