#!/usr/bin/env python3
"""Profile NewLora GPU inference without touching G2 or the HTTP service.

The profiler keeps the model, input batch, and initial random seed fixed while
comparing diffusion-step counts. CUDA events attached to the vision tower,
language model, and action expert split the model time without synchronizing
between individual modules.
"""

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
    parser.add_argument("--diffusion-steps", type=int, nargs="+", default=[10, 5])
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _summary(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(array.mean()),
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "min": float(array.min()),
        "max": float(array.max()),
    }


class ModuleEventProfiler:
    def __init__(self, modules: dict[str, torch.nn.Module]) -> None:
        self._events: dict[str, list[tuple[torch.cuda.Event, torch.cuda.Event]]] = {}
        self._stacks: dict[str, list[torch.cuda.Event]] = {}
        self._handles = []
        for name, module in modules.items():
            self._handles.append(module.register_forward_pre_hook(self._pre(name)))
            self._handles.append(module.register_forward_hook(self._post(name)))

    def _pre(self, name: str):
        def hook(_module, _inputs) -> None:
            event = torch.cuda.Event(enable_timing=True)
            event.record()
            self._stacks.setdefault(name, []).append(event)

        return hook

    def _post(self, name: str):
        def hook(_module, _inputs, _output) -> None:
            end = torch.cuda.Event(enable_timing=True)
            end.record()
            start = self._stacks[name].pop()
            self._events.setdefault(name, []).append((start, end))

        return hook

    def reset(self) -> None:
        self._events.clear()
        self._stacks.clear()

    def milliseconds(self) -> dict[str, list[float]]:
        return {
            name: [float(start.elapsed_time(end)) for start, end in pairs]
            for name, pairs in self._events.items()
        }

    def close(self) -> None:
        for handle in self._handles:
            handle.remove()


def main() -> None:
    args = parse_args()
    validate_static_contract()
    if args.warmup < 1 or args.repeats < 1:
        raise ValueError("warmup and repeats must be positive")
    if any(step < 1 for step in args.diffusion_steps):
        raise ValueError("diffusion steps must be positive")
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    dataset_index = int(manifest["episodes"][0]["indices"][0])
    _norm_path, stats = load_norm_stats(args.norm_stats_root)
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    dataset, collator = NewLoraValidationDataConfig(
        norm_stats_root=args.norm_stats_root
    ).build_dataset(
        processor=processor,
        action_horizon=ACTION_HORIZON,
        tokenizer_max_length=1024,
    )

    preprocess_started = time.perf_counter()
    sample = dataset[dataset_index]
    batch_cpu = collator([sample])
    preprocess_seconds = time.perf_counter() - preprocess_started
    transfer_started = time.perf_counter()
    batch = {key: value.to(device) for key, value in batch_cpu.items()}
    torch.cuda.synchronize()
    transfer_seconds = time.perf_counter() - transfer_started

    model = build_model(args.model, device)
    modules = {
        "vision_tower": model.model.vlm.model.vision_tower,
        "language_model": model.model.vlm.model.language_model,
        "action_expert": model.model.action_expert,
    }
    profiler = ModuleEventProfiler(modules)
    records: dict[str, list[dict]] = {str(step): [] for step in args.diffusion_steps}
    predictions: dict[tuple[int, int], np.ndarray] = {}
    try:
        for step in args.diffusion_steps:
            for warmup_index in range(args.warmup):
                torch.manual_seed(args.seed + warmup_index)
                model.inference_action(
                    input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"],
                    pixel_values=batch["pixel_values"],
                    token_type_ids=batch["token_type_ids"],
                    diffusion_steps=step,
                    action_mask=batch["action_mask"],
                )
            torch.cuda.synchronize()

        # Interleave step counts per repeat so clock/thermal drift is shared.
        for repeat in range(args.repeats):
            for step in args.diffusion_steps:
                profiler.reset()
                torch.manual_seed(args.seed + 10_000 + repeat)
                start = torch.cuda.Event(enable_timing=True)
                end = torch.cuda.Event(enable_timing=True)
                wall_started = time.perf_counter()
                start.record()
                prediction = model.inference_action(
                    input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"],
                    pixel_values=batch["pixel_values"],
                    token_type_ids=batch["token_type_ids"],
                    diffusion_steps=step,
                    action_mask=batch["action_mask"],
                )
                end.record()
                prediction_cpu = prediction[:, :, :ACTION_DIM].float().cpu().numpy()[0]
                torch.cuda.synchronize()
                module_ms = profiler.milliseconds()
                action_calls = module_ms.get("action_expert", [])
                records[str(step)].append(
                    {
                        "gpu_total_ms": float(start.elapsed_time(end)),
                        "wall_total_ms": (time.perf_counter() - wall_started) * 1000.0,
                        "vision_tower_ms": float(sum(module_ms.get("vision_tower", []))),
                        "language_model_ms": float(sum(module_ms.get("language_model", []))),
                        "action_expert_total_ms": float(sum(action_calls)),
                        "action_expert_call_count": len(action_calls),
                        "action_expert_per_call_ms": (
                            float(statistics.mean(action_calls)) if action_calls else 0.0
                        ),
                    }
                )
                predictions[(step, repeat)] = prediction_cpu
    finally:
        profiler.close()

    reference_steps = max(args.diffusion_steps)
    summary = {}
    for step in args.diffusion_steps:
        step_records = records[str(step)]
        summary[str(step)] = {
            name: _summary([record[name] for record in step_records])
            for name in (
                "gpu_total_ms",
                "wall_total_ms",
                "vision_tower_ms",
                "language_model_ms",
                "action_expert_total_ms",
                "action_expert_per_call_ms",
            )
        }
        summary[str(step)]["action_expert_call_count"] = sorted(
            {record["action_expert_call_count"] for record in step_records}
        )
        summary[str(step)]["normalized_mae_vs_reference"] = float(
            np.mean(
                [
                    np.abs(
                        predictions[(step, repeat)]
                        - predictions[(reference_steps, repeat)]
                    ).mean()
                    for repeat in range(args.repeats)
                ]
            )
        )

    result = {
        "schema": "NewLora_inference_profile_v1",
        "model": args.model,
        "dataset_index": dataset_index,
        "diffusion_steps": args.diffusion_steps,
        "reference_steps": reference_steps,
        "warmup": args.warmup,
        "repeats": args.repeats,
        "one_sample_cpu_preprocess_seconds": preprocess_seconds,
        "one_batch_host_to_device_seconds": transfer_seconds,
        "summary": summary,
        "records": records,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
