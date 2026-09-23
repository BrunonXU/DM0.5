#!/usr/bin/env python3
"""Benchmark eager vs torch.compile for the static NewLora action expert."""

from __future__ import annotations

import argparse
import json
import statistics
import time
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
    validate_static_contract,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument(
        "--compile-mode",
        choices=("default", "reduce-overhead", "max-autotune", "max-autotune-no-cudagraphs"),
        default="reduce-overhead",
    )
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _timed_inference(model, batch: dict, steps: int, seed: int) -> tuple[np.ndarray, float]:
    torch.manual_seed(seed)
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    prediction = model.inference_action(
        input_ids=batch["input_ids"],
        attention_mask=batch["attention_mask"],
        pixel_values=batch["pixel_values"],
        token_type_ids=batch["token_type_ids"],
        diffusion_steps=steps,
        action_mask=batch["action_mask"],
    )
    end.record()
    result = prediction[:, :, :ACTION_DIM].float().cpu().numpy()[0]
    torch.cuda.synchronize()
    return result, float(start.elapsed_time(end))


def _summary(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(array.mean()),
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "min": float(array.min()),
        "max": float(array.max()),
    }


def main() -> None:
    args = parse_args()
    validate_static_contract()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    index = int(manifest["episodes"][0]["indices"][0])
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    dataset, collator = NewLoraValidationDataConfig(
        norm_stats_root=args.norm_stats_root
    ).build_dataset(
        processor=processor, action_horizon=ACTION_HORIZON, tokenizer_max_length=1024
    )
    batch = {
        key: value.to(device) for key, value in collator([dataset[index]]).items()
    }
    model = build_model(args.model, device)
    _norm_path, stats = load_norm_stats(args.norm_stats_root)

    for warmup in range(args.warmup):
        _timed_inference(model, batch, args.diffusion_steps, args.seed + warmup)
    eager_outputs = []
    eager_ms = []
    for repeat in range(args.repeats):
        output, elapsed = _timed_inference(
            model, batch, args.diffusion_steps, args.seed + 10_000 + repeat
        )
        eager_outputs.append(output)
        eager_ms.append(elapsed)

    compile_started = time.perf_counter()
    model.model.action_expert = torch.compile(
        model.model.action_expert,
        mode=args.compile_mode,
        dynamic=False,
        fullgraph=False,
    )
    compile_warmup_ms = []
    for warmup in range(args.warmup):
        _output, elapsed = _timed_inference(
            model, batch, args.diffusion_steps, args.seed + warmup
        )
        compile_warmup_ms.append(elapsed)
    compile_wall_seconds = time.perf_counter() - compile_started

    compiled_outputs = []
    compiled_ms = []
    for repeat in range(args.repeats):
        output, elapsed = _timed_inference(
            model, batch, args.diffusion_steps, args.seed + 10_000 + repeat
        )
        compiled_outputs.append(output)
        compiled_ms.append(elapsed)

    deltas = [
        np.abs(compiled - eager)
        for eager, compiled in zip(eager_outputs, compiled_outputs, strict=True)
    ]
    physical_deltas = [
        np.abs(
            denormalize_quantile(compiled, stats["action"])
            - denormalize_quantile(eager, stats["action"])
        )
        for eager, compiled in zip(eager_outputs, compiled_outputs, strict=True)
    ]
    result = {
        "schema": "NewLora_compile_benchmark_v1",
        "diffusion_steps": args.diffusion_steps,
        "compile_mode": args.compile_mode,
        "warmup": args.warmup,
        "repeats": args.repeats,
        "eager_gpu_ms": _summary(eager_ms),
        "compiled_gpu_ms": _summary(compiled_ms),
        "stable_speedup": float(statistics.mean(eager_ms) / statistics.mean(compiled_ms)),
        "compile_and_warmup_wall_seconds": compile_wall_seconds,
        "compile_warmup_gpu_ms": compile_warmup_ms,
        "normalized_output_mae": float(np.mean([delta.mean() for delta in deltas])),
        "normalized_output_max_abs": float(max(delta.max() for delta in deltas)),
        "physical_output_mae_rad": float(
            np.mean([delta.mean() for delta in physical_deltas])
        ),
        "physical_output_max_abs_rad": float(
            max(delta.max() for delta in physical_deltas)
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
