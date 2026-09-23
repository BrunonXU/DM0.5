#!/usr/bin/env python3
"""Compare 30-to-100 Hz arm interpolation on recorded powered-Sync chunks.

The recorded ``action_chunk.actions`` already contains the production arm
weight-10 waypoint smoothing.  This evaluator freezes those waypoints and the
120 ms boundary blend, then changes only the interpolation profile.  It never
imports GDK or sends robot commands.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, MODEL_HZ
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_LIMITS
from g2_sort.validation.evaluate_rtc_servo_ablation import sample


ARM_DIM = 14
SERVO_HZ = 100.0
METHODS = ("linear", "cubic", "spline", "pchip", "quintic")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--execution-intervals", type=int, default=20)
    parser.add_argument("--blend-seconds", type=float, default=0.12)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
        "mean": float(np.mean(values)),
    }


def load_records(paths: list[Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in paths:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        chunks = {
            int(event["request_id"]): event
            for event in events
            if event.get("event") == "action_chunk"
        }
        dispatches = {
            int(event["request_id"]): event
            for event in events
            if event.get("event") == "dispatch_summary"
        }
        for request_id in sorted(chunks.keys() & dispatches.keys()):
            chunk = chunks[request_id]
            dispatch = dispatches[request_id]
            actions = np.asarray(chunk["actions"], dtype=np.float64)
            anchor = np.asarray(dispatch["arm_previous_command_rad"], dtype=np.float64)
            if actions.shape != (50, ACTION_DIM):
                raise ValueError(f"{path}: request {request_id} has {actions.shape}")
            # The first chunk has no prior successfully sent command in the
            # process, so telemetry intentionally records a null anchor.  It
            # cannot participate in an exact boundary-blend counterfactual.
            if anchor.shape != (ARM_DIM,):
                continue
            if float(chunk.get("arm_internal_jerk_weight", -1.0)) != 10.0:
                raise ValueError(f"{path}: request {request_id} is not weight 10")
            records.append(
                {
                    "log": str(path),
                    "request_id": request_id,
                    "actions": actions,
                    "anchor": anchor,
                }
            )
    if not records:
        raise RuntimeError("no paired action_chunk/dispatch_summary records")
    return records


def evaluate(
    records: list[dict[str, object]],
    *,
    method: str,
    execution_intervals: int,
    blend_seconds: float,
) -> dict[str, object]:
    tick_count = int(np.ceil(execution_intervals / MODEL_HZ * SERVO_HZ))
    times = np.arange(tick_count, dtype=np.float64) / SERVO_HZ
    positions = times * MODEL_HZ
    chunk_rows: list[dict[str, float | int | str]] = []
    all_jerk: list[float] = []
    all_post_blend_jerk: list[float] = []
    for record in records:
        actions = np.asarray(record["actions"], dtype=np.float64)
        anchor = np.asarray(record["anchor"], dtype=np.float64)
        arms = actions[:, :ARM_DIM]
        sampled = sample(arms, positions.copy(), method)
        linear = sample(arms, positions.copy(), "linear")
        unblended_sampled = sampled.copy()
        if blend_seconds > 0:
            active = times < blend_seconds
            u = np.clip(times[active] / blend_seconds, 0.0, 1.0)
            alpha = u * u * (3.0 - 2.0 * u)
            sampled[active] = (
                (1.0 - alpha[:, None]) * anchor
                + alpha[:, None] * sampled[active]
            )
            linear[active] = (
                (1.0 - alpha[:, None]) * anchor
                + alpha[:, None] * linear[active]
            )

        stitched = np.vstack([np.repeat(anchor[None, :], 3, axis=0), sampled])
        delta = np.diff(stitched, axis=0)
        velocity = delta * SERVO_HZ
        acceleration = np.diff(stitched, n=2, axis=0) * SERVO_HZ**2
        jerk = np.diff(stitched, n=3, axis=0) * SERVO_HZ**3
        absolute_jerk = np.abs(jerk).ravel()
        # The first jerk sample ending at least 30 ms after the blend is
        # independent of the boundary cross-fade stencil.
        post_blend = np.abs(jerk[3:][times[3:] >= blend_seconds + 0.03]).ravel()
        deviation = np.abs(sampled - linear)

        lower_index = np.floor(positions).astype(int)
        upper_index = np.minimum(lower_index + 1, len(arms) - 1)
        lower = np.minimum(arms[lower_index], arms[upper_index])
        upper = np.maximum(arms[lower_index], arms[upper_index])
        overshoot = np.maximum(
            lower - unblended_sampled,
            unblended_sampled - upper,
        )
        overshoot = np.maximum(overshoot, 0.0)
        physical_violation = np.maximum(
            POLICY_JOINT_LIMITS[:ARM_DIM, 0] - sampled,
            sampled - POLICY_JOINT_LIMITS[:ARM_DIM, 1],
        )
        physical_violation = np.maximum(physical_violation, 0.0)

        all_jerk.extend(absolute_jerk.tolist())
        all_post_blend_jerk.extend(post_blend.tolist())
        chunk_rows.append(
            {
                "log": str(record["log"]),
                "request_id": int(record["request_id"]),
                "step_max_rad": float(np.max(np.abs(delta))),
                "velocity_max_rad_s": float(np.max(np.abs(velocity))),
                "acceleration_max_rad_s2": float(np.max(np.abs(acceleration))),
                "jerk_p95_rad_s3": float(np.percentile(absolute_jerk, 95)),
                "jerk_max_rad_s3": float(np.max(absolute_jerk)),
                "post_blend_jerk_p95_rad_s3": float(
                    np.percentile(post_blend, 95)
                ),
                "post_blend_jerk_max_rad_s3": float(np.max(post_blend)),
                "linear_deviation_p95_rad": float(np.percentile(deviation, 95)),
                "linear_deviation_max_rad": float(np.max(deviation)),
                "overshoot_max_rad": float(np.max(overshoot)),
                "physical_limit_violation_max_rad": float(
                    np.max(physical_violation)
                ),
            }
        )

    metric_names = [
        "step_max_rad",
        "velocity_max_rad_s",
        "acceleration_max_rad_s2",
        "jerk_p95_rad_s3",
        "jerk_max_rad_s3",
        "post_blend_jerk_p95_rad_s3",
        "post_blend_jerk_max_rad_s3",
        "linear_deviation_p95_rad",
        "linear_deviation_max_rad",
        "overshoot_max_rad",
        "physical_limit_violation_max_rad",
    ]
    return {
        "method": method,
        "num_chunks": len(chunk_rows),
        "global_jerk_p95_rad_s3": percentile(all_jerk, 95),
        "global_post_blend_jerk_p95_rad_s3": percentile(
            all_post_blend_jerk, 95
        ),
        "metrics": {
            name: summarize([float(row[name]) for row in chunk_rows])
            for name in metric_names
        },
        "records": chunk_rows,
    }


def main() -> None:
    args = parse_args()
    if not 1 <= args.execution_intervals <= 50:
        raise ValueError("execution intervals must be in [1,50]")
    if args.blend_seconds < 0:
        raise ValueError("blend seconds cannot be negative")
    records = load_records(args.logs)
    results = {
        method: evaluate(
            records,
            method=method,
            execution_intervals=args.execution_intervals,
            blend_seconds=args.blend_seconds,
        )
        for method in METHODS
    }
    report = {
        "schema": "g2_sync_arm_interpolation_ablation_v1",
        "logs": [str(path) for path in args.logs],
        "scope": "recorded weight-10 arm waypoints, ideal 100 Hz, no GDK",
        "execution_intervals": args.execution_intervals,
        "blend_seconds": args.blend_seconds,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(args.output)

    print(
        "method postJerkP95 chunkJerkMaxP95 accelMaxP95 stepMaxP95 "
        "devMaxP95 overshootMax limitMax"
    )
    for method, result in results.items():
        metrics = result["metrics"]
        print(
            f"{method:<8} "
            f"{result['global_post_blend_jerk_p95_rad_s3']:<11.2f} "
            f"{metrics['post_blend_jerk_max_rad_s3']['p95']:<16.2f} "
            f"{metrics['acceleration_max_rad_s2']['p95']:<11.2f} "
            f"{metrics['step_max_rad']['p95']:<10.5f} "
            f"{metrics['linear_deviation_max_rad']['p95']:<9.5f} "
            f"{metrics['overshoot_max_rad']['max']:<12.6f} "
            f"{metrics['physical_limit_violation_max_rad']['max']:.6f}"
        )


if __name__ == "__main__":
    main()
