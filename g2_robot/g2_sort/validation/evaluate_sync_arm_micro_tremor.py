#!/usr/bin/env python3
"""Offline ablation of arm near-stationary reversal suppression.

The production chain is frozen as position clip -> weight-10 waypoint smoother
-> candidate -> C2 spline -> 120 ms boundary blend.  This tool never imports
GDK and never sends robot commands.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import clip_action_positions
from g2_sort.deployment.g2_client.servo import (
    project_stationary_arm_minimum_jerk,
)
from g2_sort.deployment.g2_client.trajectory_smoothing import smooth_arm_action_chunk
from g2_sort.validation.evaluate_rtc_servo_ablation import sample


ARM_DIM = 14
MODEL_HZ = 30.0
SERVO_HZ = 100.0
EXECUTION_INTERVALS = 20
BLEND_SECONDS = 0.12


@dataclass(frozen=True)
class Candidate:
    family: str
    threshold_rad: float

    @property
    def name(self) -> str:
        if self.family == "baseline":
            return "baseline_weight10_c2"
        return f"{self.family}_{self.threshold_rad:g}rad"


def play_deadband(values: np.ndarray, threshold: float) -> np.ndarray:
    """Causal play-operator hysteresis with bounded position error."""
    result = np.asarray(values, dtype=np.float64).copy()
    state = result[0].copy()
    for index in range(1, len(result)):
        delta = values[index] - state
        above = delta > threshold
        below = delta < -threshold
        state[above] = values[index, above] - threshold
        state[below] = values[index, below] + threshold
        result[index] = state
    return result


def turning_point_projection(values: np.ndarray, threshold: float) -> np.ndarray:
    """Use one-waypoint lookahead to shrink only small local extrema."""
    result = np.asarray(values, dtype=np.float64).copy()
    for _ in range(2):
        previous = result[:-2]
        current = result[1:-1]
        following = result[2:]
        left = current - previous
        right = following - current
        small_extremum = (
            (left * right < 0.0)
            & (np.abs(left) <= threshold)
            & (np.abs(right) <= threshold)
        )
        midpoint = 0.5 * (previous + following)
        result[1:-1] = np.where(small_extremum, midpoint, current)
    return result


def median_extrema_filter(values: np.ndarray, threshold: float) -> np.ndarray:
    """Replace isolated small extrema by a three-point coordinate median."""
    result = np.asarray(values, dtype=np.float64).copy()
    for _ in range(2):
        previous = result[:-2]
        current = result[1:-1]
        following = result[2:]
        left = current - previous
        right = following - current
        small_extremum = (
            (left * right < 0.0)
            & (np.abs(left) <= threshold)
            & (np.abs(right) <= threshold)
        )
        median = np.median(
            np.stack([previous, current, following], axis=0), axis=0
        )
        result[1:-1] = np.where(small_extremum, median, current)
    return result


def stationary_trend_projection(values: np.ndarray, threshold: float) -> np.ndarray:
    """Linearize a low-range, repeatedly reversing executable prefix.

    The complete Sync prefix is already available before execution.  A joint is
    projected only when its 21 waypoints stay inside ``threshold`` and reverse
    at least twice.  Endpoints are preserved, so the operator removes local
    empty travel without erasing the prefix's net direction.
    """
    result = np.asarray(values, dtype=np.float64).copy()
    prefix = result[: EXECUTION_INTERVALS + 1]
    delta = np.diff(prefix, axis=0)
    reversals = np.count_nonzero(delta[:-1] * delta[1:] < 0.0, axis=0)
    span = np.ptp(prefix, axis=0)
    active = (reversals >= 2) & (span <= threshold)
    if np.any(active):
        alpha = np.linspace(0.0, 1.0, len(prefix))[:, None]
        trend = (1.0 - alpha) * prefix[0] + alpha * prefix[-1]
        result[: EXECUTION_INTERVALS + 1, active] = trend[:, active]
    return result


def reversal_gate(values: np.ndarray, threshold: float) -> np.ndarray:
    """Hold a small sign reversal until it exceeds a Schmitt-style band."""
    result = np.asarray(values, dtype=np.float64).copy()
    velocity = np.zeros(values.shape[1], dtype=np.float64)
    state = result[0].copy()
    for index in range(1, len(result)):
        proposal = values[index] - state
        reversal = velocity * proposal < 0.0
        hold = reversal & (np.abs(proposal) <= threshold)
        accepted = np.where(hold, state, values[index])
        step = accepted - state
        velocity = 0.65 * velocity + 0.35 * step
        state = accepted
        result[index] = state
    return result


def apply_candidate(values: np.ndarray, candidate: Candidate) -> np.ndarray:
    if candidate.family == "baseline":
        return np.asarray(values, dtype=np.float64).copy()
    if candidate.family == "play":
        return play_deadband(values, candidate.threshold_rad)
    if candidate.family == "turning":
        return turning_point_projection(values, candidate.threshold_rad)
    if candidate.family == "median":
        return median_extrema_filter(values, candidate.threshold_rad)
    if candidate.family == "stationary_trend":
        return stationary_trend_projection(values, candidate.threshold_rad)
    if candidate.family == "stationary_minjerk":
        return np.asarray(values, dtype=np.float64).copy()
    if candidate.family == "reversal_gate":
        return reversal_gate(values, candidate.threshold_rad)
    raise ValueError(f"unknown candidate family: {candidate.family}")


def load_records(paths: list[Path]) -> list[dict[str, object]]:
    records = []
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
            anchor = dispatches[request_id].get("arm_previous_command_rad")
            if anchor is None:
                continue
            raw = np.asarray(chunks[request_id]["actions"], dtype=np.float64)
            if raw.shape != (50, 34):
                continue
            records.append(
                {
                    "log": str(path),
                    "request_id": request_id,
                    "raw": raw,
                    "anchor": np.asarray(anchor, dtype=np.float64),
                }
            )
    if not records:
        raise ValueError("no paired action chunks with arm anchors")
    return records


def render(values: np.ndarray, anchor: np.ndarray) -> np.ndarray:
    duration = EXECUTION_INTERVALS / MODEL_HZ
    times = np.arange(
        int(np.ceil(duration * SERVO_HZ)), dtype=np.float64
    ) / SERVO_HZ
    # ServoStreamer commits the exact final waypoint when playback expires.
    # Include it so the offline candidate cannot hide a terminal jump between
    # the final regular 100 Hz sample and hold.
    times = np.append(times, duration)
    command = sample(values, times * MODEL_HZ, "spline")
    active = times < BLEND_SECONDS
    u = times[active] / BLEND_SECONDS
    alpha = u * u * (3.0 - 2.0 * u)
    command[active] = (
        (1.0 - alpha[:, None]) * anchor
        + alpha[:, None] * command[active]
    )
    return command


def micro_reversals(command: np.ndarray) -> int:
    return int(np.count_nonzero(micro_reversal_mask(command)))


def micro_reversal_mask(command: np.ndarray) -> np.ndarray:
    delta = np.diff(command, axis=0)
    return (
        (delta[:-1] * delta[1:] < 0.0)
        & (np.maximum(np.abs(delta[:-1]), np.abs(delta[1:])) <= 0.003)
        & (np.minimum(np.abs(delta[:-1]), np.abs(delta[1:])) >= 1e-5)
    )


def first_crossing(command: np.ndarray, joint: int, direction: float) -> int | None:
    displacement = direction * (command[:, joint] - command[0, joint])
    indices = np.flatnonzero(displacement >= 0.03)
    return None if not len(indices) else int(indices[0])


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def evaluate(records: list[dict[str, object]], candidate: Candidate) -> dict[str, object]:
    reversal_counts = []
    near_stationary_reversals = 0
    near_stationary_axis_count = 0
    near_stationary_extra = []
    stationary_extra = []
    deviations = []
    chunk_max_deviation = []
    endpoint_errors = []
    jerk_maxima = []
    crossing_delays = []
    missing_crossings = 0
    significant_net_ratios = []
    for record in records:
        clipped, _, _ = clip_action_positions(np.asarray(record["raw"]))
        weighted, _ = smooth_arm_action_chunk(clipped, jerk_weight=10.0)
        baseline = render(weighted[:, :ARM_DIM], np.asarray(record["anchor"]))
        filtered_waypoints = apply_candidate(weighted[:, :ARM_DIM], candidate)
        command = render(filtered_waypoints, np.asarray(record["anchor"]))
        if candidate.family == "stationary_minjerk":
            command, _, _, _ = project_stationary_arm_minimum_jerk(
                command, candidate.threshold_rad
            )
        reversal_counts.append(micro_reversals(command))
        delta = np.diff(command, axis=0)
        travel = np.sum(np.abs(delta), axis=0)
        net = np.abs(command[-1] - command[0])
        baseline_span = np.ptp(baseline, axis=0)
        baseline_net_abs = np.abs(baseline[-1] - baseline[0])
        near_stationary = (baseline_span <= 0.005) & (baseline_net_abs <= 0.003)
        near_stationary_axis_count += int(np.count_nonzero(near_stationary))
        near_stationary_reversals += int(
            np.count_nonzero(micro_reversal_mask(command)[:, near_stationary])
        )
        near_stationary_extra.extend(
            (travel[near_stationary] - net[near_stationary]).tolist()
        )
        stationary_extra.extend((travel[net < 0.03] - net[net < 0.03]).tolist())
        deviation = np.abs(command - baseline)
        deviations.extend(deviation.ravel().tolist())
        chunk_max_deviation.append(float(np.max(deviation)))
        endpoint_errors.extend(np.abs(command[-1] - baseline[-1]).tolist())
        jerk = np.diff(command, n=3, axis=0) * SERVO_HZ**3
        jerk_maxima.append(float(np.max(np.abs(jerk))))
        baseline_net = baseline[-1] - baseline[0]
        for joint in range(ARM_DIM):
            if abs(baseline_net[joint]) < 0.03:
                continue
            candidate_net = abs(command[-1, joint] - command[0, joint])
            significant_net_ratios.append(candidate_net / abs(baseline_net[joint]))
            direction = float(np.sign(baseline_net[joint]))
            baseline_crossing = first_crossing(baseline, joint, direction)
            candidate_crossing = first_crossing(command, joint, direction)
            if baseline_crossing is None:
                continue
            if candidate_crossing is None:
                missing_crossings += 1
            else:
                crossing_delays.append(
                    max(0.0, (candidate_crossing - baseline_crossing) / SERVO_HZ)
                )
    return {
        "name": candidate.name,
        "family": candidate.family,
        "threshold_rad": candidate.threshold_rad,
        "micro_reversals_total": int(sum(reversal_counts)),
        "micro_reversals_per_chunk": float(np.mean(reversal_counts)),
        "near_stationary_axis_count": near_stationary_axis_count,
        "near_stationary_micro_reversals": near_stationary_reversals,
        "near_stationary_extra_path_p95_rad": percentile(
            near_stationary_extra, 95
        ),
        "stationary_extra_path_p95_rad": percentile(stationary_extra, 95),
        "command_deviation_p95_rad": percentile(deviations, 95),
        "chunk_max_deviation_p95_rad": percentile(chunk_max_deviation, 95),
        "command_deviation_max_rad": max(chunk_max_deviation),
        "endpoint_error_p95_rad": percentile(endpoint_errors, 95),
        "jerk_max_p95_rad_s3": percentile(jerk_maxima, 95),
        "significant_net_ratio_p05": percentile(significant_net_ratios, 5),
        "crossing_delay_p95_seconds": percentile(crossing_delays, 95),
        "missing_crossing_count": missing_crossings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    records = load_records(args.logs)
    candidates = [Candidate("baseline", 0.0)]
    for family in (
        "play",
        "turning",
        "median",
        "stationary_trend",
        "stationary_minjerk",
        "reversal_gate",
    ):
        candidates.extend(
            Candidate(family, threshold)
            for threshold in (
                0.0005,
                0.001,
                0.002,
                0.003,
                0.0035,
                0.004,
                0.005,
                0.008,
            )
        )
    results = [evaluate(records, candidate) for candidate in candidates]
    baseline = results[0]
    for result in results:
        reduction = 1.0 - (
            result["micro_reversals_per_chunk"]
            / baseline["micro_reversals_per_chunk"]
        )
        result["micro_reversal_reduction_ratio"] = reduction
        result["near_stationary_reversal_reduction_ratio"] = 1.0 - (
            result["near_stationary_micro_reversals"]
            / baseline["near_stationary_micro_reversals"]
        )
        result["passes_intent_gate"] = bool(
            result["chunk_max_deviation_p95_rad"] <= 0.003
            and result["command_deviation_max_rad"] <= 0.008
            and result["significant_net_ratio_p05"] >= 0.95
            and result["crossing_delay_p95_seconds"] <= 0.02
            and result["missing_crossing_count"] == 0
        )
        result["passes_powered_candidate_gate"] = bool(
            result["passes_intent_gate"]
            and result["near_stationary_reversal_reduction_ratio"] >= 0.5
        )
    payload = {
        "schema": "g2.sync_arm_micro_tremor_ablation.v1",
        "logs": [str(path) for path in args.logs],
        "record_count": len(records),
        "frozen_chain": "clip -> arm weight10 -> candidate -> C2 -> 120ms blend",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print("candidate rev/chunk allRed nearRed devP95 devMax delayP95 netP05 jerkP95 gate")
    for result in results:
        print(
            f"{result['name']:<28} "
            f"{result['micro_reversals_per_chunk']:>6.2f} "
            f"{result['micro_reversal_reduction_ratio']*100:>7.1f}% "
            f"{result['near_stationary_reversal_reduction_ratio']*100:>7.1f}% "
            f"{result['chunk_max_deviation_p95_rad']:>7.4f} "
            f"{result['command_deviation_max_rad']:>7.4f} "
            f"{result['crossing_delay_p95_seconds']:>7.3f} "
            f"{result['significant_net_ratio_p05']:>7.3f} "
            f"{result['jerk_max_p95_rad_s3']:>8.1f} "
            f"{str(result['passes_powered_candidate_gate']):>5}"
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
