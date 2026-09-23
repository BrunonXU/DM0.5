#!/usr/bin/env python3
"""Fixed-seed offline RTC rollout evaluation for NewLora 75K.

No G2/GDK or Servo interface is imported.  Consecutive frozen validation
samples provide observations; the prior generated absolute chunk supplies the
committed RTC tail for the next observation.
"""

from __future__ import annotations

import argparse
import importlib
import json
import statistics
import sys
import time
import types
from pathlib import Path

import numpy as np
import torch
from transformers import AutoProcessor

from g2_sort.validation.newlora_common import (
    ACTION_DIM,
    ACTION_HORIZON,
    NewLoraValidationDataConfig,
    absolute_to_mixed,
    build_model,
    denormalize_quantile,
    load_norm_stats,
    mixed_to_absolute,
    normalize_quantile,
    sha256_file,
    validate_static_contract,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--delay-steps", type=int, default=20)
    parser.add_argument("--execution-horizon", type=int, default=25)
    parser.add_argument("--max-guidance-weight", type=float, default=10.0)
    parser.add_argument(
        "--guidance-mode",
        choices=("exact_vjp", "approx_identity"),
        default="exact_vjp",
    )
    parser.add_argument("--max-pairs", type=int, default=8)
    parser.add_argument(
        "--couple-adjacent-noise",
        action="store_true",
        help=(
            "Reuse the same initial diffusion noise for the previous and "
            "current observations to measure stochastic mode switching."
        ),
    )
    parser.add_argument(
        "--g2-client-dir",
        help=(
            "Optional canonical g2_client package directory. When supplied, "
            "run deployment-faithful client post-processing groups."
        ),
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _load_g2_client_postprocess(package_dir: str):
    path = Path(package_dir).resolve()
    if not (path / "contracts.py").is_file():
        raise FileNotFoundError(f"invalid G2 client package directory: {path}")
    # DM0.5 and g2_robot intentionally use the same top-level ``g2_sort``
    # package name. Extend the already imported package path only for this
    # evaluator so the client's absolute config import remains canonical.
    import g2_sort

    client_g2_sort_root = str(path.parents[1])
    if client_g2_sort_root not in g2_sort.__path__:
        g2_sort.__path__.append(client_g2_sort_root)
    package_name = "_newlora_rtc_g2_client"
    package = types.ModuleType(package_name)
    package.__path__ = [str(path)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    joint_layout = importlib.import_module(f"{package_name}.joint_layout")
    rtc = importlib.import_module(f"{package_name}.rtc")
    smoothing = importlib.import_module(f"{package_name}.trajectory_smoothing")
    return {
        "clip": joint_layout.clip_action_positions,
        "measure_disagreement": rtc.measure_rtc_plan_disagreement,
        "project": rtc.project_absolute_rtc_prefix,
        "smooth_chunk": smoothing.smooth_arm_action_chunk,
        "smooth_suffix": smoothing.smooth_rtc_arm_action_suffix,
    }


def _clip(client: dict, actions: np.ndarray) -> np.ndarray:
    clipped, _count, _max_delta = client["clip"](actions)
    return clipped


def _prepare_bootstrap(client: dict, actions: np.ndarray) -> np.ndarray:
    result = _clip(client, actions)
    result, _stats = client["smooth_chunk"](result, jerk_weight=10.0)
    return _clip(client, result)


def _prepare_group(
    client: dict,
    actions: np.ndarray,
    prefix: np.ndarray,
    *,
    delay: int,
    kind: str,
    weight: float,
) -> np.ndarray:
    result = _clip(client, actions)
    if kind == "baseline":
        result, _stats = client["smooth_chunk"](result, jerk_weight=weight)
    elif kind == "legacy":
        result, _stats = client["smooth_chunk"](result, jerk_weight=weight)
        result = client["project"](result, prefix, inference_delay=delay)
    elif kind == "c0_only":
        result = client["project"](result, prefix, inference_delay=delay)
    elif kind == "suffix_c1":
        result = client["project"](result, prefix, inference_delay=delay)
        incoming_velocity = prefix[delay, :14] - prefix[delay - 1, :14]
        result, _stats = client["smooth_suffix"](
            result,
            start_index=delay,
            boundary_arm_position=prefix[delay, :14],
            boundary_arm_velocity_per_step=incoming_velocity,
            jerk_weight=weight,
        )
    else:
        raise ValueError(f"unsupported post-processing group {kind}")
    return _clip(client, result)


def _physical_state(dataset, raw_cache: dict[str, list[str]], index: int) -> np.ndarray:
    file_id, frame_index = dataset.sample_index[index]
    path = dataset.id_to_jsonl[file_id]
    if path not in raw_cache:
        raw_cache[path] = Path(path).read_text(encoding="utf-8").splitlines()
    state = np.asarray(json.loads(raw_cache[path][frame_index])["state"], dtype=np.float32)
    if state.shape != (ACTION_DIM,):
        raise RuntimeError(f"validation state is not 34D: {state.shape}")
    return state


def _generate(
    model,
    batch: dict,
    *,
    seed: int,
    diffusion_steps: int,
    rtc: dict | None,
) -> tuple[np.ndarray, float]:
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    started = time.perf_counter()
    kwargs = {} if rtc is None else rtc
    prediction = model.inference_action(
        input_ids=batch["input_ids"],
        attention_mask=batch["attention_mask"],
        pixel_values=batch["pixel_values"],
        token_type_ids=batch["token_type_ids"],
        diffusion_steps=diffusion_steps,
        action_mask=batch["action_mask"],
        **kwargs,
    )
    result = prediction[:, :, :ACTION_DIM].float().detach().cpu().numpy()[0]
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    return result, time.perf_counter() - started


def main() -> None:
    args = parse_args()
    validate_static_contract()
    client = (
        None
        if args.g2_client_dir is None
        else _load_g2_client_postprocess(args.g2_client_dir)
    )
    d, s, horizon = args.delay_steps, args.execution_horizon, ACTION_HORIZON
    if not 0 <= d <= s <= horizon - d:
        raise ValueError(f"RTC requires d <= s <= H-d, got {d}, {s}, {horizon}")
    if args.max_pairs < 1:
        raise ValueError("max-pairs must be positive")
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("NewLora RTC rollout evaluation requires CUDA")

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    norm_path, stats = load_norm_stats(args.norm_stats_root)
    processor = AutoProcessor.from_pretrained(
        args.processor, local_files_only=True, trust_remote_code=True
    )
    config = NewLoraValidationDataConfig(norm_stats_root=args.norm_stats_root)
    dataset, collator = config.build_dataset(
        processor=processor,
        action_horizon=ACTION_HORIZON,
        tokenizer_max_length=1024,
    )
    model = build_model(args.model, device)
    raw_cache: dict[str, list[str]] = {}
    records: list[dict] = []
    sample_lookup = {
        (file_id, frame_index): dataset_index
        for dataset_index, (file_id, frame_index) in enumerate(dataset.sample_index)
    }

    for episode_index, episode in enumerate(manifest["episodes"]):
        indices = episode["indices"]
        for pair_index, previous_index in enumerate(indices):
            if len(records) >= args.max_pairs:
                break
            file_id, previous_frame = dataset.sample_index[previous_index]
            current_index = sample_lookup.get((file_id, previous_frame + s))
            if current_index is None:
                continue
            previous_batch = {
                key: value.to(device)
                for key, value in collator([dataset[previous_index]]).items()
            }
            current_batch = {
                key: value.to(device)
                for key, value in collator([dataset[current_index]]).items()
            }
            previous_state = _physical_state(dataset, raw_cache, previous_index)
            current_state = _physical_state(dataset, raw_cache, current_index)
            seed = args.seed + episode_index * 10_000 + pair_index

            previous_norm, previous_seconds = _generate(
                model,
                previous_batch,
                seed=seed if args.couple_adjacent_noise else seed - 1,
                diffusion_steps=args.diffusion_steps,
                rtc=None,
            )
            baseline_norm, baseline_seconds = _generate(
                model,
                current_batch,
                seed=seed,
                diffusion_steps=args.diffusion_steps,
                rtc=None,
            )
            previous_abs = mixed_to_absolute(
                denormalize_quantile(previous_norm, stats["action"]), previous_state
            )
            previous_committed = (
                previous_abs
                if client is None
                else _prepare_bootstrap(client, previous_abs)
            )
            committed_prefix_abs = previous_committed[s:]
            prefix_norm = normalize_quantile(
                absolute_to_mixed(committed_prefix_abs, current_state),
                stats["action"],
            )
            rtc_norm, rtc_seconds = _generate(
                model,
                current_batch,
                seed=seed,
                diffusion_steps=args.diffusion_steps,
                rtc={
                    "rtc_prefix_actions": torch.as_tensor(
                        prefix_norm, device=device, dtype=previous_batch["action"].dtype
                    ).unsqueeze(0),
                    "rtc_inference_delay": d,
                    "rtc_overlap_steps": len(committed_prefix_abs),
                    "rtc_max_guidance_weight": args.max_guidance_weight,
                    "rtc_guidance_mode": args.guidance_mode,
                },
            )
            baseline_abs = mixed_to_absolute(
                denormalize_quantile(baseline_norm, stats["action"]), current_state
            )
            rtc_abs = mixed_to_absolute(
                denormalize_quantile(rtc_norm, stats["action"]), current_state
            )
            target_abs = mixed_to_absolute(
                denormalize_quantile(
                    current_batch["action"][:, :, :ACTION_DIM].float().cpu().numpy()[0],
                    stats["action"],
                ),
                current_state,
            )
            old_before_switch = committed_prefix_abs[max(0, d - 1)]

            def mae(left, right) -> float:
                return float(np.abs(left - right).mean())

            def max_abs(left) -> float:
                return float(np.abs(left).max())

            baseline_stitched = np.concatenate(
                [committed_prefix_abs[:d], baseline_abs[d:]], axis=0
            )
            rtc_stitched = np.concatenate(
                [committed_prefix_abs[:d], rtc_abs[d:]], axis=0
            )

            def dynamics(actions: np.ndarray, group: slice) -> tuple[float, float, float]:
                velocity = np.diff(actions[:, group], axis=0) * 30.0
                acceleration = np.diff(velocity, axis=0) * 30.0
                jerk = np.diff(acceleration, axis=0) * 30.0
                return max_abs(velocity), max_abs(acceleration), max_abs(jerk)

            baseline_arm_dyn = dynamics(baseline_stitched, slice(0, 14))
            rtc_arm_dyn = dynamics(rtc_stitched, slice(0, 14))
            baseline_hand_dyn = dynamics(baseline_stitched, slice(14, 34))
            rtc_hand_dyn = dynamics(rtc_stitched, slice(14, 34))

            postprocess_groups = {}
            if client is not None:
                group_specs = {
                    "baseline_full50_w10": (baseline_abs, "baseline", 10.0),
                    "rtc_legacy_full50_w10_c0": (rtc_abs, "legacy", 10.0),
                    "rtc_c0_only_w0": (rtc_abs, "c0_only", 0.0),
                    "rtc_suffix_c1_w0": (rtc_abs, "suffix_c1", 0.0),
                    "rtc_suffix_c1_w10": (rtc_abs, "suffix_c1", 10.0),
                }
                old_velocity = (
                    committed_prefix_abs[d, :14]
                    - committed_prefix_abs[d - 1, :14]
                ) * 30.0
                for group_name, (source, kind, weight) in group_specs.items():
                    prepared_group = _prepare_group(
                        client,
                        source,
                        committed_prefix_abs,
                        delay=d,
                        kind=kind,
                        weight=weight,
                    )
                    group_stitched = np.concatenate(
                        [committed_prefix_abs[:d], prepared_group[d:]], axis=0
                    )
                    group_arm_dyn = dynamics(group_stitched, slice(0, 14))
                    disagreement = client["measure_disagreement"](
                        prepared_group,
                        committed_prefix_abs,
                        switch_delay=d,
                        horizon_steps=10,
                    )
                    new_velocity = (
                        prepared_group[d + 1, :14]
                        - prepared_group[d, :14]
                    ) * 30.0
                    postprocess_groups[group_name] = {
                        "arm_c0_seam_max_rad": max_abs(
                            prepared_group[d, :14]
                            - committed_prefix_abs[d, :14]
                        ),
                        "arm_velocity_jump_max_rad_s": max_abs(
                            new_velocity - old_velocity
                        ),
                        "arm_direction_reversal_count": int(
                            np.count_nonzero(old_velocity * new_velocity < 0.0)
                        ),
                        "future_horizon_steps": disagreement.horizon_steps,
                        "future_arm_direction_reversal_count": (
                            disagreement.arm_direction_reversal_count
                        ),
                        "future_arm_reverse_displacement_max_rad": (
                            disagreement.arm_reverse_displacement_max_rad
                        ),
                        "future_arm_plan_divergence_max_rad": (
                            disagreement.arm_plan_divergence_max_rad
                        ),
                        "future_hand_plan_divergence_max_rad": (
                            disagreement.hand_plan_divergence_max_rad
                        ),
                        "stitched_arm_velocity_max_rad_s": group_arm_dyn[0],
                        "stitched_arm_acceleration_max_rad_s2": group_arm_dyn[1],
                        "stitched_arm_jerk_max_rad_s3": group_arm_dyn[2],
                        "executable_arm_target_mae": mae(
                            prepared_group[d:, :14], target_abs[d:, :14]
                        ),
                        "fresh_arm_target_mae": mae(
                            prepared_group[len(committed_prefix_abs) :, :14],
                            target_abs[len(committed_prefix_abs) :, :14],
                        ),
                        "executable_arm_policy_deviation_max_rad": max_abs(
                            prepared_group[d:, :14] - source[d:, :14]
                        ),
                        "stale_arm_prefix_mutation_max_rad": max_abs(
                            prepared_group[:d, :14] - source[:d, :14]
                        ),
                    }

            records.append(
                {
                    "episode": episode["episode"],
                    "previous_dataset_index": int(previous_index),
                    "current_dataset_index": int(current_index),
                    "latency_seconds": {
                        "previous_baseline": previous_seconds,
                        "current_baseline": baseline_seconds,
                        "rtc_guided": rtc_seconds,
                    },
                    **(
                        {"postprocess_groups": postprocess_groups}
                        if postprocess_groups
                        else {}
                    ),
                    "metrics": {
                        "hard_prefix_mae_baseline": mae(
                            baseline_abs[:d], committed_prefix_abs[:d]
                        ),
                        "hard_prefix_mae_rtc": mae(rtc_abs[:d], committed_prefix_abs[:d]),
                        "hard_prefix_arm_mae_baseline": mae(
                            baseline_abs[:d, :14], committed_prefix_abs[:d, :14]
                        ),
                        "hard_prefix_arm_mae_rtc": mae(
                            rtc_abs[:d, :14], committed_prefix_abs[:d, :14]
                        ),
                        "hard_prefix_hand_mae_baseline": mae(
                            baseline_abs[:d, 14:], committed_prefix_abs[:d, 14:]
                        ),
                        "hard_prefix_hand_mae_rtc": mae(
                            rtc_abs[:d, 14:], committed_prefix_abs[:d, 14:]
                        ),
                        "soft_overlap_mae_baseline": mae(
                            baseline_abs[d : len(committed_prefix_abs)],
                            committed_prefix_abs[d:],
                        ),
                        "soft_overlap_mae_rtc": mae(
                            rtc_abs[d : len(committed_prefix_abs)],
                            committed_prefix_abs[d:],
                        ),
                        "switch_jump_baseline": mae(baseline_abs[d], old_before_switch),
                        "switch_jump_rtc": mae(rtc_abs[d], old_before_switch),
                        "switch_arm_jump_max_baseline": max_abs(
                            baseline_abs[d, :14] - old_before_switch[:14]
                        ),
                        "switch_arm_jump_max_rtc": max_abs(
                            rtc_abs[d, :14] - old_before_switch[:14]
                        ),
                        "switch_hand_jump_max_baseline": max_abs(
                            baseline_abs[d, 14:] - old_before_switch[14:]
                        ),
                        "switch_hand_jump_max_rtc": max_abs(
                            rtc_abs[d, 14:] - old_before_switch[14:]
                        ),
                        "target_mae_baseline": mae(baseline_abs, target_abs),
                        "target_mae_rtc": mae(rtc_abs, target_abs),
                        "target_arm_mae_baseline": mae(
                            baseline_abs[:, :14], target_abs[:, :14]
                        ),
                        "target_arm_mae_rtc": mae(
                            rtc_abs[:, :14], target_abs[:, :14]
                        ),
                        "target_hand_mae_baseline": mae(
                            baseline_abs[:, 14:], target_abs[:, 14:]
                        ),
                        "target_hand_mae_rtc": mae(
                            rtc_abs[:, 14:], target_abs[:, 14:]
                        ),
                        "fresh_suffix_change_from_baseline": mae(
                            rtc_abs[len(committed_prefix_abs) :],
                            baseline_abs[len(committed_prefix_abs) :],
                        ),
                        "fresh_arm_change_from_baseline": mae(
                            rtc_abs[len(committed_prefix_abs) :, :14],
                            baseline_abs[len(committed_prefix_abs) :, :14],
                        ),
                        "fresh_hand_change_from_baseline": mae(
                            rtc_abs[len(committed_prefix_abs) :, 14:],
                            baseline_abs[len(committed_prefix_abs) :, 14:],
                        ),
                        "stitched_arm_velocity_max_baseline": baseline_arm_dyn[0],
                        "stitched_arm_velocity_max_rtc": rtc_arm_dyn[0],
                        "stitched_arm_acceleration_max_baseline": baseline_arm_dyn[1],
                        "stitched_arm_acceleration_max_rtc": rtc_arm_dyn[1],
                        "stitched_arm_jerk_max_baseline": baseline_arm_dyn[2],
                        "stitched_arm_jerk_max_rtc": rtc_arm_dyn[2],
                        "stitched_hand_velocity_max_baseline": baseline_hand_dyn[0],
                        "stitched_hand_velocity_max_rtc": rtc_hand_dyn[0],
                        "stitched_hand_acceleration_max_baseline": baseline_hand_dyn[1],
                        "stitched_hand_acceleration_max_rtc": rtc_hand_dyn[1],
                        "stitched_hand_jerk_max_baseline": baseline_hand_dyn[2],
                        "stitched_hand_jerk_max_rtc": rtc_hand_dyn[2],
                    },
                }
            )
            print(
                f"pair={len(records)} episode={episode['episode']} "
                f"hard {records[-1]['metrics']['hard_prefix_mae_baseline']:.5f}->"
                f"{records[-1]['metrics']['hard_prefix_mae_rtc']:.5f} "
                f"latency {baseline_seconds:.3f}s->{rtc_seconds:.3f}s"
            )
        if len(records) >= args.max_pairs:
            break

    if not records:
        raise RuntimeError("manifest did not provide any validation sample pairs")

    metric_names = records[0]["metrics"]
    latency_names = records[0]["latency_seconds"]
    postprocess_macro = {}
    if client is not None:
        for group_name, group_metrics in records[0]["postprocess_groups"].items():
            postprocess_macro[group_name] = {
                metric_name: statistics.mean(
                    record["postprocess_groups"][group_name][metric_name]
                    for record in records
                )
                for metric_name in group_metrics
            }
    result = {
        "schema": (
            "NewLora_rtc_offline_rollout_postprocess_groups_v2"
            if client is not None
            else "NewLora_rtc_offline_rollout_v1"
        ),
        "model": args.model,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "norm_stats": str(norm_path),
        "norm_stats_sha256": sha256_file(norm_path),
        "action_contract": "arm_relative_14_hand_absolute_20",
        "rtc": {
            "algorithm": f"{args.guidance_mode}_linear_prefix",
            "delay_steps": d,
            "execution_horizon": s,
            "committed_tail_steps": horizon - s,
            "max_guidance_weight": args.max_guidance_weight,
            "diffusion_steps": args.diffusion_steps,
            "couple_adjacent_noise": args.couple_adjacent_noise,
        },
        "num_pairs": len(records),
        "macro_metrics": {
            name: statistics.mean(record["metrics"][name] for record in records)
            for name in metric_names
        },
        **(
            {"postprocess_group_macro_metrics": postprocess_macro}
            if postprocess_macro
            else {}
        ),
        "latency_seconds": {
            name: {
                "mean": statistics.mean(
                    record["latency_seconds"][name] for record in records
                ),
                "max": max(record["latency_seconds"][name] for record in records),
            }
            for name in latency_names
        },
        "records": records,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
