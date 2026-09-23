#!/usr/bin/env python3
"""Replay RTC client post-processing groups from powered telemetry.

The input log must contain complete raw ``action_chunk.actions`` arrays from a
single powered-RTC run.  This evaluator never imports GDK or sends commands. It
recursively rebuilds each group's committed trajectory so every subsequent RTC
prefix comes from that same group's prior post-processing result.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, CHUNK_STEPS, MODEL_HZ
from g2_sort.deployment.g2_client.joint_layout import clip_action_positions
from g2_sort.deployment.g2_client.rtc import project_absolute_rtc_prefix
from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_arm_action_chunk,
    smooth_rtc_arm_action_suffix,
)


GROUPS = {
    "legacy_full50_w10_c0": {"kind": "legacy", "weight": 10.0},
    "c0_only_w0": {"kind": "c0_only", "weight": 0.0},
    "suffix_c1_w0": {"kind": "suffix_c1", "weight": 0.0},
    "suffix_c1_w10": {"kind": "suffix_c1", "weight": 10.0},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--delay-steps", type=int, default=20)
    parser.add_argument("--execution-horizon", type=int, default=25)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_raw_chunks(path: Path) -> list[tuple[int, np.ndarray]]:
    chunks: list[tuple[int, np.ndarray]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") != "action_chunk":
            continue
        actions = np.asarray(record.get("actions"), dtype=np.float64)
        if actions.shape != (CHUNK_STEPS, ACTION_DIM):
            raise ValueError(
                f"invalid action chunk request={record.get('request_id')}: "
                f"{actions.shape}"
            )
        if not np.all(np.isfinite(actions)):
            raise ValueError(f"non-finite action chunk request={record.get('request_id')}")
        chunks.append((int(record["request_id"]), actions))
    if len(chunks) < 2:
        raise RuntimeError("RTC post-processing replay requires at least two chunks")
    return chunks


def clip(actions: np.ndarray) -> np.ndarray:
    clipped, _count, _max_delta = clip_action_positions(actions)
    return clipped


def prepare_bootstrap(raw: np.ndarray) -> np.ndarray:
    actions = clip(raw)
    actions, _stats = smooth_arm_action_chunk(actions, jerk_weight=10.0)
    return clip(actions)


def prepare_rtc(
    raw: np.ndarray,
    prefix: np.ndarray,
    *,
    delay: int,
    kind: str,
    weight: float,
) -> np.ndarray:
    actions = clip(raw)
    if kind == "legacy":
        actions, _stats = smooth_arm_action_chunk(actions, jerk_weight=weight)
        actions = project_absolute_rtc_prefix(
            actions,
            prefix,
            inference_delay=delay,
        )
    elif kind == "c0_only":
        actions = project_absolute_rtc_prefix(
            actions,
            prefix,
            inference_delay=delay,
        )
    elif kind == "suffix_c1":
        actions = project_absolute_rtc_prefix(
            actions,
            prefix,
            inference_delay=delay,
        )
        incoming_velocity = prefix[delay, :14] - prefix[delay - 1, :14]
        actions, _stats = smooth_rtc_arm_action_suffix(
            actions,
            start_index=delay,
            boundary_arm_position=prefix[delay, :14],
            boundary_arm_velocity_per_step=incoming_velocity,
            jerk_weight=weight,
        )
    else:
        raise ValueError(f"unsupported group kind {kind}")
    return clip(actions)


def dynamics(actions: np.ndarray, start: int) -> tuple[float, float, float]:
    arm = actions[start:, :14]
    velocity = np.diff(arm, axis=0) * MODEL_HZ
    acceleration = np.diff(velocity, axis=0) * MODEL_HZ
    jerk = np.diff(acceleration, axis=0) * MODEL_HZ
    return (
        float(np.abs(velocity).max()),
        float(np.abs(acceleration).max()),
        float(np.abs(jerk).max()),
    )


def summarize(records: list[dict]) -> dict:
    metric_names = [key for key in records[0] if key != "request_id"]
    return {
        name: {
            "mean": statistics.mean(float(record[name]) for record in records),
            "p95": float(np.percentile([record[name] for record in records], 95)),
            "max": max(float(record[name]) for record in records),
        }
        for name in metric_names
    }


def main() -> None:
    args = parse_args()
    delay = int(args.delay_steps)
    execution = int(args.execution_horizon)
    if not 1 <= delay < CHUNK_STEPS - execution:
        raise ValueError("this C1 replay requires 1 <= d < H-s")
    chunks = load_raw_chunks(args.log)

    group_results = {}
    for name, spec in GROUPS.items():
        committed = prepare_bootstrap(chunks[0][1])
        records: list[dict] = []
        for request_id, raw in chunks[1:]:
            prefix = committed[execution:]
            prepared = prepare_rtc(
                raw,
                prefix,
                delay=delay,
                kind=str(spec["kind"]),
                weight=float(spec["weight"]),
            )
            old_index = execution + delay
            old_velocity = (
                committed[old_index, :14] - committed[old_index - 1, :14]
            ) * MODEL_HZ
            new_velocity = (
                prepared[delay + 1, :14] - prepared[delay, :14]
            ) * MODEL_HZ
            velocity_jump = new_velocity - old_velocity
            arm_velocity, arm_acceleration, arm_jerk = dynamics(prepared, delay)
            records.append(
                {
                    "request_id": request_id,
                    "arm_c0_seam_max_rad": float(
                        np.abs(prepared[delay, :14] - committed[old_index, :14]).max()
                    ),
                    "arm_velocity_jump_max_rad_s": float(
                        np.abs(velocity_jump).max()
                    ),
                    "arm_direction_reversal_count": int(
                        np.count_nonzero(old_velocity * new_velocity < 0.0)
                    ),
                    "arm_suffix_velocity_max_rad_s": arm_velocity,
                    "arm_suffix_acceleration_max_rad_s2": arm_acceleration,
                    "arm_suffix_jerk_max_rad_s3": arm_jerk,
                    "arm_executable_policy_deviation_max_rad": float(
                        np.abs(prepared[delay:, :14] - raw[delay:, :14]).max()
                    ),
                    "arm_stale_prefix_mutation_max_rad": float(
                        np.abs(prepared[:delay, :14] - raw[:delay, :14]).max()
                    ),
                }
            )
            committed = prepared
        group_results[name] = {
            "postprocess": spec,
            "num_switches": len(records),
            "macro_metrics": summarize(records),
            "records": records,
        }

    result = {
        "schema": "g2_rtc_postprocess_groups_v1",
        "scope": (
            "deterministic 30 Hz client post-processing replay; no model rerun, "
            "100 Hz Servo simulation, GDK, or hand V1"
        ),
        "source_log": str(args.log),
        "delay_steps": delay,
        "execution_horizon": execution,
        "groups": group_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        json.dumps(
            {
                name: value["macro_metrics"]
                for name, value in group_results.items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
