#!/usr/bin/env python3
"""Evaluate NewLora held-out flow-matching loss on a frozen manifest."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import torch
from transformers import AutoProcessor

from g2_sort.validation.newlora_common import (
    ACTION_HORIZON,
    NewLoraValidationDataConfig,
    build_model,
    load_norm_stats,
    sha256_file,
    validate_static_contract,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--seed-repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260817)
    parser.add_argument("--max-episodes", type=int)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size < 1 or args.seed_repeats < 1:
        raise ValueError("batch-size and seed-repeats must be positive")
    validate_static_contract()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("NewLora FM validation requires CUDA")

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode_groups = manifest["episodes"]
    if args.max_episodes is not None:
        episode_groups = episode_groups[: args.max_episodes]
    norm_path, _ = load_norm_stats(args.norm_stats_root)

    torch.manual_seed(args.seed)
    processor = AutoProcessor.from_pretrained(
        args.processor,
        local_files_only=True,
        trust_remote_code=True,
    )
    data_config = NewLoraValidationDataConfig(
        norm_stats_root=args.norm_stats_root,
    )
    dataset, collator = data_config.build_dataset(
        processor=processor,
        action_horizon=ACTION_HORIZON,
        tokenizer_max_length=1024,
    )
    model = build_model(args.model, device)

    batch_records: list[dict] = []
    episode_records: list[dict] = []
    with torch.inference_mode():
        for episode_offset, group in enumerate(episode_groups):
            indices = group["indices"]
            for index in indices:
                if not 0 <= index < len(dataset):
                    raise IndexError(f"sample index {index} outside {len(dataset)}")
            losses: list[float] = []
            for batch_offset, start in enumerate(range(0, len(indices), args.batch_size)):
                batch_indices = indices[start : start + args.batch_size]
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
                    with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                        output = model(**batch)
                    loss = float(output.loss.detach())
                    if not math.isfinite(loss):
                        raise RuntimeError(
                            f"non-finite loss: {group['episode']} {batch_indices}"
                        )
                    losses.append(loss)
                    batch_records.append(
                        {
                            "episode": group["episode"],
                            "repeat": repeat,
                            "indices": batch_indices,
                            "seed": seed,
                            "loss": loss,
                        }
                    )
            record = {
                "episode": group["episode"],
                "num_unique_samples": len(indices),
                "seed_repeats": args.seed_repeats,
                "mean_loss": statistics.mean(losses),
                "median_batch_loss": statistics.median(losses),
                "max_batch_loss": max(losses),
            }
            episode_records.append(record)
            print(f"episode={record['episode']} mean_loss={record['mean_loss']:.6f}")

    episode_means = [record["mean_loss"] for record in episode_records]
    result = {
        "schema": "NewLora_FM_validation_v1",
        "model": args.model,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "norm_stats": str(norm_path),
        "norm_stats_sha256": sha256_file(norm_path),
        "action_contract": "arm_relative_14_hand_absolute_20",
        "augmentation": "NoAugmentationPipeline",
        "seed": args.seed,
        "batch_size": args.batch_size,
        "seed_repeats": args.seed_repeats,
        "num_episodes": len(episode_records),
        "num_unique_samples": sum(r["num_unique_samples"] for r in episode_records),
        "episode_macro_mean_loss": statistics.mean(episode_means),
        "episode_macro_median_loss": statistics.median(episode_means),
        "episode_macro_p90_loss": sorted(episode_means)[
            math.ceil(0.9 * len(episode_means)) - 1
        ],
        "episodes": episode_records,
        "batches": batch_records,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    print(json.dumps({k: v for k, v in result.items() if k not in {"episodes", "batches"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
