#!/usr/bin/env python3
"""Evaluate deterministic flow-matching loss on fixed real G2 samples."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import torch
from transformers import AutoProcessor

from g2_sort.training.dm05_lora import DM05DataConfig, DM05ModelConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--dataset-name", default="g2_sort_validation")
    parser.add_argument("--indices", type=int, nargs="+", default=[100, 500, 900, 1200])
    parser.add_argument(
        "--manifest",
        help="Optional episode-balanced JSON manifest; overrides --indices.",
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--seed-repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("This evaluation requires a CUDA GPU")

    # Keep the randomly initialized 32D -> 34D base projection repeatable.
    torch.manual_seed(args.seed)
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    data_config = DM05DataConfig(
        dataset_name=args.dataset_name,
        norm_stats_root=args.norm_stats_root,
    )
    # Validation must reuse the frozen train-only normalization statistics.
    # The upstream filename digest includes dataset_name, so a validation config
    # would otherwise look for (or tempt callers to generate) validation stats.
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
    model_config = DM05ModelConfig(
        model_name_or_path=args.model,
        llm_attn_implementation="sdpa",
        vision_attn_implementation="flash_attention_2",
        action_attn_implementation="sdpa",
        liger_kernel=False,
        vlm_gradient_checkpointing=False,
        ae_gradient_checkpointing=False,
    )
    model = model_config.build_model(use_lora=False).to(device).eval()

    if args.batch_size < 1 or args.seed_repeats < 1:
        raise ValueError("batch-size and seed-repeats must be positive")
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        episode_groups = manifest["episodes"]
    else:
        episode_groups = [{"episode": "fixed_indices", "indices": args.indices}]

    batch_records: list[dict] = []
    episode_records: list[dict] = []
    with torch.inference_mode():
        for episode_offset, group in enumerate(episode_groups):
            indices = group["indices"]
            if not indices:
                raise ValueError(f"episode {group['episode']} has no indices")
            for index in indices:
                if not 0 <= index < len(dataset):
                    raise IndexError(
                        f"sample index {index} outside dataset of {len(dataset)}"
                    )
            episode_losses: list[float] = []
            for batch_offset, start in enumerate(
                range(0, len(indices), args.batch_size)
            ):
                batch_indices = indices[start : start + args.batch_size]
                batch = {
                    key: value.to(device)
                    for key, value in collator(
                        [dataset[index] for index in batch_indices]
                    ).items()
                }
                # Decode and tokenize the frozen images once, then reuse the
                # identical batch for every flow-noise repeat.
                for repeat in range(args.seed_repeats):
                    # The frozen seed schedule and batch grouping make every
                    # checkpoint see identical flow times and noise.
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
                            f"non-finite loss for {group['episode']} "
                            f"indices={batch_indices}: {loss}"
                        )
                    episode_losses.append(loss)
                    batch_records.append(
                        {
                            "episode": group["episode"],
                            "repeat": repeat,
                            "indices": batch_indices,
                            "seed": seed,
                            "loss": loss,
                        }
                    )
            episode_record = {
                "episode": group["episode"],
                "num_unique_samples": len(indices),
                "seed_repeats": args.seed_repeats,
                "mean_loss": statistics.mean(episode_losses),
                "median_batch_loss": statistics.median(episode_losses),
                "max_batch_loss": max(episode_losses),
            }
            episode_records.append(episode_record)
            print(
                f"episode={group['episode']} "
                f"mean_loss={episode_record['mean_loss']:.6f}"
            )

    episode_means = [record["mean_loss"] for record in episode_records]
    result = {
        "model": args.model,
        "dataset_name": args.dataset_name,
        "manifest": args.manifest,
        "seed": args.seed,
        "batch_size": args.batch_size,
        "seed_repeats": args.seed_repeats,
        "num_episodes": len(episode_records),
        "num_unique_samples": sum(len(group["indices"]) for group in episode_groups),
        "episode_macro_mean_loss": statistics.mean(episode_means),
        "episode_macro_median_loss": statistics.median(episode_means),
        "episode_macro_p90_loss": sorted(episode_means)[
            math.ceil(0.9 * len(episode_means)) - 1
        ],
        "episodes": episode_records,
        "batches": batch_records,
    }
    print(json.dumps(result, ensure_ascii=False))
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
