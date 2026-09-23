#!/usr/bin/env python3
"""Check fixed-prefix padding against unpadded eager inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from transformers import AutoProcessor

from g2_sort.validation.newlora_common import (
    ACTION_DIM,
    ACTION_HORIZON,
    NewLoraValidationDataConfig,
    build_model,
    denormalize_quantile,
    load_norm_stats,
)
from opendm.data.transforms import PadModelInput


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--diffusion-steps", type=int, default=5)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _infer(model, sample: dict, steps: int, seed: int) -> np.ndarray:
    torch.manual_seed(seed)
    result = model.inference_action(
        input_ids=sample["input_ids"],
        attention_mask=sample["attention_mask"],
        pixel_values=sample["pixel_values"],
        token_type_ids=sample["token_type_ids"],
        diffusion_steps=steps,
        action_mask=sample["action_mask"],
    )
    return result[:, :, :ACTION_DIM].float().cpu().numpy()[0]


def main() -> None:
    args = parse_args()
    if args.samples < 1:
        raise ValueError("samples must be positive")
    device = torch.device(args.device)
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    dataset, _collator = NewLoraValidationDataConfig(
        norm_stats_root=args.norm_stats_root
    ).build_dataset(
        processor=processor,
        action_horizon=ACTION_HORIZON,
        tokenizer_max_length=args.max_length,
    )
    model = build_model(args.model, device)
    _norm_path, stats = load_norm_stats(args.norm_stats_root)
    pad = PadModelInput(
        max_length=args.max_length,
        pad_token_id=processor.tokenizer.pad_token_id,
    )

    records = []
    for index in range(min(args.samples, len(dataset))):
        raw_cpu = dataset[index]
        raw = {
            key: value.to(device)
            for key, value in raw_cpu.items()
            if isinstance(value, torch.Tensor)
        }
        padded = {key: value.clone() for key, value in raw.items()}
        pad(padded)
        raw_output = _infer(model, raw, args.diffusion_steps, args.seed + index)
        padded_output = _infer(
            model, padded, args.diffusion_steps, args.seed + index
        )
        raw_physical = denormalize_quantile(raw_output, stats["action"])
        padded_physical = denormalize_quantile(padded_output, stats["action"])
        delta = np.abs(padded_physical - raw_physical)
        records.append(
            {
                "index": index,
                "valid_prefix_tokens": int(raw["input_ids"].shape[1]),
                "fixed_prefix_tokens": int(padded["input_ids"].shape[1]),
                "physical_mae_rad": float(delta.mean()),
                "physical_max_abs_rad": float(delta.max()),
            }
        )
        print(json.dumps(records[-1]), flush=True)

    result = {
        "schema": "NewLora_compile_static_padding_validation_v1",
        "samples": len(records),
        "diffusion_steps": args.diffusion_steps,
        "max_length": args.max_length,
        "physical_mae_rad": float(
            np.mean([record["physical_mae_rad"] for record in records])
        ),
        "physical_max_abs_rad": float(
            max(record["physical_max_abs_rad"] for record in records)
        ),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
