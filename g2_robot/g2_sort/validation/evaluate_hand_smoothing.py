"""Offline 5 Hz hand smoothing comparison from G2 powered telemetry.

Older logs contain policy chunks and aggregate hand statistics, so their
candidate replay uses the nominal 80 ms first sample and 5 Hz cadence.  Newer
logs additionally contain every acknowledged 20D command and relative send
time; those exact traces are reported and can be replayed through causal caps.
No GDK module is imported and no hardware command is sent.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    CausalHandCommandTracker,
    smooth_fixed_endpoint_trajectory,
)


ARM_DIM = 14
HAND_DIM = 20
MODEL_HZ = 30.0
HAND_HZ = 5.0
DEFAULT_FIRST_SAMPLE_SECONDS = 0.08


@dataclass(frozen=True)
class HandChunk:
    request_id: int
    hands: np.ndarray
    start_index: int
    interval_count: int
    blend_seconds: float
    actual_commands: np.ndarray | None
    actual_times: np.ndarray | None
    previous_actual: np.ndarray | None


@dataclass(frozen=True)
class HandTrackerStats:
    """Aggregate decisions made by the offline causal hand tracker."""

    boundary_commands: int
    velocity_limit_hits: int
    acceleration_limit_hits: int
    direction_reversal_holds: int
    emergency_cap_hits: int


def load_hand_chunks(path: Path) -> list[HandChunk]:
    actions: dict[int, dict[str, object]] = {}
    dispatch: dict[int, dict[str, object]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            request_id = event.get("request_id")
            if not isinstance(request_id, int):
                continue
            if event.get("event") == "action_chunk":
                actions[request_id] = event
            elif event.get("event") == "dispatch_summary":
                dispatch[request_id] = event

    chunks: list[HandChunk] = []
    for request_id in sorted(actions.keys() & dispatch.keys()):
        action_event = actions[request_id]
        dispatch_event = dispatch[request_id]
        array = np.asarray(action_event.get("actions"), dtype=np.float64)
        if array.ndim != 2 or array.shape[1] < ARM_DIM + HAND_DIM:
            raise ValueError(f"{path}: request {request_id} has invalid actions")
        start = int(action_event.get("used_start_index") or 0)
        intervals = int(action_event.get("used_intervals") or 0)
        if start < 0 or intervals <= 0 or start + intervals > len(array):
            raise ValueError(f"{path}: request {request_id} has invalid range")

        commands_payload = dispatch_event.get("hand_commands_rad")
        times_payload = dispatch_event.get("hand_command_time_offsets_seconds")
        actual_commands: np.ndarray | None = None
        actual_times: np.ndarray | None = None
        if commands_payload is not None and times_payload is not None:
            commands = np.asarray(commands_payload, dtype=np.float64)
            times = np.asarray(times_payload, dtype=np.float64)
            if commands.size == 0:
                commands = np.empty((0, HAND_DIM), dtype=np.float64)
            if (
                commands.ndim != 2
                or commands.shape[1] != HAND_DIM
                or times.shape != (len(commands),)
                or not np.all(np.isfinite(commands))
                or not np.all(np.isfinite(times))
            ):
                raise ValueError(
                    f"{path}: request {request_id} has invalid hand trace"
                )
            actual_commands = commands
            actual_times = times
        previous_payload = dispatch_event.get("hand_previous_command_rad")
        previous = (
            None
            if previous_payload is None
            else np.asarray(previous_payload, dtype=np.float64)
        )
        if previous is not None and previous.shape != (HAND_DIM,):
            raise ValueError(f"{path}: request {request_id} has invalid prior hand")
        chunks.append(
            HandChunk(
                request_id=request_id,
                hands=array[:, ARM_DIM : ARM_DIM + HAND_DIM],
                start_index=start,
                interval_count=intervals,
                blend_seconds=float(dispatch_event.get("blend_seconds") or 0.0),
                actual_commands=actual_commands,
                actual_times=actual_times,
                previous_actual=previous,
            )
        )
    return chunks


def _sample_linear(values: np.ndarray, elapsed: float) -> np.ndarray:
    position = max(0.0, elapsed) * MODEL_HZ
    lower = min(int(np.floor(position)), len(values) - 1)
    upper = min(lower + 1, len(values) - 1)
    alpha = 0.0 if lower == upper else position - np.floor(position)
    return (1.0 - alpha) * values[lower] + alpha * values[upper]


def nominal_hand_targets(
    chunks: list[HandChunk],
    transform: Callable[[np.ndarray], np.ndarray],
) -> list[np.ndarray]:
    result: list[np.ndarray] = []
    anchor: np.ndarray | None = None
    for chunk in chunks:
        hands = np.asarray(transform(chunk.hands), dtype=np.float64)
        duration = chunk.interval_count / MODEL_HZ
        times = np.arange(
            DEFAULT_FIRST_SAMPLE_SECONDS,
            duration,
            1.0 / HAND_HZ,
            dtype=np.float64,
        )
        active = hands[chunk.start_index :]
        targets = np.stack([_sample_linear(active, float(t)) for t in times])
        if anchor is not None and chunk.blend_seconds > 0:
            for index, elapsed in enumerate(times):
                if elapsed >= chunk.blend_seconds:
                    break
                u = elapsed / chunk.blend_seconds
                alpha = u * u * (3.0 - 2.0 * u)
                targets[index] = (1.0 - alpha) * anchor + alpha * targets[index]
        result.append(targets)
        end_index = min(
            chunk.start_index + chunk.interval_count,
            len(hands) - 1,
        )
        anchor = hands[end_index].copy()
    return result


def apply_causal_delta_cap(
    targets: list[np.ndarray],
    *,
    max_delta_rad: float,
    initial_commands: list[np.ndarray | None] | None = None,
) -> list[np.ndarray]:
    if not np.isfinite(max_delta_rad) or max_delta_rad <= 0:
        raise ValueError("max_delta_rad must be finite and positive")
    result: list[np.ndarray] = []
    previous: np.ndarray | None = None
    if initial_commands is not None and len(initial_commands) != len(targets):
        raise ValueError("initial_commands must align with target chunks")
    for chunk_index, target_chunk in enumerate(targets):
        if initial_commands is not None:
            initial = initial_commands[chunk_index]
            previous = None if initial is None else np.asarray(initial).copy()
        commands = np.empty_like(target_chunk)
        for index, target in enumerate(target_chunk):
            if previous is None:
                command = target.copy()
            else:
                command = previous + np.clip(
                    target - previous, -max_delta_rad, max_delta_rad
                )
            commands[index] = command
            previous = command
        result.append(commands)
    return result


def apply_causal_boundary_internal_cap(
    targets: list[np.ndarray],
    *,
    boundary_max_delta_rad: float,
    internal_max_delta_rad: float,
    initial_commands: list[np.ndarray | None],
) -> list[np.ndarray]:
    """Limit the first command of each chunk more than internal commands."""
    if not 0 < boundary_max_delta_rad <= internal_max_delta_rad:
        raise ValueError("require 0 < boundary cap <= internal cap")
    if len(initial_commands) != len(targets):
        raise ValueError("initial_commands must align with target chunks")
    result: list[np.ndarray] = []
    previous: np.ndarray | None = None
    previous_raw: np.ndarray | None = None
    for target_chunk, initial in zip(targets, initial_commands, strict=True):
        recorded_anchor = None if initial is None else np.asarray(initial).copy()
        continuous_trace = (
            previous_raw is not None
            and recorded_anchor is not None
            and np.allclose(recorded_anchor, previous_raw, atol=1e-9, rtol=0.0)
        )
        if not continuous_trace:
            previous = recorded_anchor
        commands = np.empty_like(target_chunk)
        for index, target in enumerate(target_chunk):
            if previous is None:
                command = target.copy()
            else:
                limit = (
                    boundary_max_delta_rad
                    if index == 0
                    else internal_max_delta_rad
                )
                command = previous + np.clip(target - previous, -limit, limit)
            commands[index] = command
            previous = command
        result.append(commands)
        if len(target_chunk):
            previous_raw = target_chunk[-1].copy()
    return result


def apply_causal_hand_tracker(
    targets: list[np.ndarray],
    *,
    initial_commands: list[np.ndarray | None],
    command_times: list[np.ndarray] | None = None,
    boundary_blend_seconds: float = 0.4,
    max_velocity_rad_s: float = 2.0,
    max_acceleration_rad_s2: float = 10.0,
    max_delta_rad: float = 0.4,
    reversal_error_rad: float = 0.15,
) -> tuple[list[np.ndarray], HandTrackerStats]:
    """Replay a stateful 5 Hz hand tracker without sending hardware commands.

    Each chunk starts from the last simulated command with zero velocity, which
    matches the powered Sync hold.  A cubic boundary guide is used for the
    first ``boundary_blend_seconds``; the rate/acceleration tracker remains
    active after that guide ends, so residual error cannot be released as one
    discontinuous step.  Recorded previous commands are used only to start a
    trace or to re-anchor when concatenated log files are discontinuous.
    """
    finite_positive = {
        "boundary_blend_seconds": boundary_blend_seconds,
        "max_velocity_rad_s": max_velocity_rad_s,
        "max_acceleration_rad_s2": max_acceleration_rad_s2,
        "max_delta_rad": max_delta_rad,
    }
    for name, value in finite_positive.items():
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not np.isfinite(reversal_error_rad) or reversal_error_rad < 0:
        raise ValueError("reversal_error_rad must be finite and non-negative")
    if len(initial_commands) != len(targets):
        raise ValueError("initial_commands must align with target chunks")
    if command_times is not None and len(command_times) != len(targets):
        raise ValueError("command_times must align with target chunks")

    tracker = CausalHandCommandTracker(
        enabled=True,
        boundary_blend_seconds=boundary_blend_seconds,
        max_velocity_rad_s=max_velocity_rad_s,
        max_acceleration_rad_s2=max_acceleration_rad_s2,
        max_delta_rad=max_delta_rad,
        reversal_error_rad=reversal_error_rad,
    )
    result: list[np.ndarray] = []
    previous_raw: np.ndarray | None = None
    boundary_commands = 0
    velocity_hits = 0
    acceleration_hits = 0
    reversal_holds = 0
    cap_hits = 0
    nominal_dt = 1.0 / HAND_HZ

    for chunk_index, target_chunk_value in enumerate(targets):
        target_chunk = np.asarray(target_chunk_value, dtype=np.float64)
        if target_chunk.ndim != 2 or target_chunk.shape[1] != HAND_DIM:
            raise ValueError("every target chunk must be an Nx20 array")
        if not np.all(np.isfinite(target_chunk)):
            raise ValueError("target chunks must contain only finite values")
        recorded_anchor_value = initial_commands[chunk_index]
        recorded_anchor = (
            None
            if recorded_anchor_value is None
            else np.asarray(recorded_anchor_value, dtype=np.float64)
        )
        if recorded_anchor is not None and recorded_anchor.shape != (HAND_DIM,):
            raise ValueError("every initial command must contain 20 values")

        # A mismatch means evaluate_logs concatenated a new powered run.  Do
        # not carry simulated state across that real-world discontinuity.
        continuous_trace = (
            previous_raw is not None
            and recorded_anchor is not None
            and np.allclose(recorded_anchor, previous_raw, atol=1e-9, rtol=0.0)
        )
        if not continuous_trace:
            tracker.reset(recorded_anchor)
        tracker.begin_segment(started_at=0.0)

        if command_times is None:
            times = DEFAULT_FIRST_SAMPLE_SECONDS + nominal_dt * np.arange(
                len(target_chunk), dtype=np.float64
            )
        else:
            times = np.asarray(command_times[chunk_index], dtype=np.float64)
            if times.shape != (len(target_chunk),) or not np.all(np.isfinite(times)):
                raise ValueError("every command-time array must align with its chunk")
            if len(times) > 1 and np.any(np.diff(times) <= 0):
                raise ValueError("command times must be strictly increasing")

        commands = np.empty_like(target_chunk)
        for index, (raw_target, elapsed) in enumerate(
            zip(target_chunk, times, strict=True)
        ):
            decision = tracker.step(raw_target, now=float(elapsed))
            commands[index] = decision.command
            if decision.boundary_active:
                boundary_commands += 1
            velocity_hits += decision.velocity_limit_hits
            acceleration_hits += decision.acceleration_limit_hits
            reversal_holds += decision.direction_reversal_holds
            cap_hits += decision.emergency_cap_hits

        result.append(commands)
        if len(target_chunk):
            previous_raw = target_chunk[-1].copy()

    return result, HandTrackerStats(
        boundary_commands=boundary_commands,
        velocity_limit_hits=velocity_hits,
        acceleration_limit_hits=acceleration_hits,
        direction_reversal_holds=reversal_holds,
        emergency_cap_hits=cap_hits,
    )


def _halfway_timing_shifts(
    targets: list[np.ndarray],
    commands: list[np.ndarray],
) -> tuple[list[float], int]:
    shifts: list[float] = []
    missed = 0
    for target_chunk, command_chunk in zip(targets, commands, strict=True):
        if len(target_chunk) < 2:
            continue
        movement = target_chunk[-1] - target_chunk[0]
        for joint, total in enumerate(movement):
            if abs(total) < 0.1:
                continue
            threshold = target_chunk[0, joint] + 0.5 * total
            if total > 0:
                target_hits = np.flatnonzero(target_chunk[:, joint] >= threshold)
                command_hits = np.flatnonzero(command_chunk[:, joint] >= threshold)
            else:
                target_hits = np.flatnonzero(target_chunk[:, joint] <= threshold)
                command_hits = np.flatnonzero(command_chunk[:, joint] <= threshold)
            if not len(target_hits):
                continue
            if not len(command_hits):
                missed += 1
                continue
            shifts.append(
                float(command_hits[0] - target_hits[0]) / HAND_HZ
            )
    return shifts, missed


def evaluate_commands(
    targets: list[np.ndarray],
    commands: list[np.ndarray],
    *,
    initial_commands: list[np.ndarray | None] | None = None,
) -> dict[str, float | int]:
    steps: list[float] = []
    boundary_steps: list[float] = []
    internal_steps: list[float] = []
    chunk_maxima: list[float] = []
    tracking: list[float] = []
    final_errors: list[float] = []
    previous: np.ndarray | None = None
    if initial_commands is not None and len(initial_commands) != len(targets):
        raise ValueError("initial_commands must align with target chunks")
    for chunk_index, (target_chunk, command_chunk) in enumerate(
        zip(targets, commands, strict=True)
    ):
        if len(command_chunk) == 0:
            continue
        if initial_commands is not None:
            initial = initial_commands[chunk_index]
            previous = None if initial is None else np.asarray(initial)
        sequence = command_chunk if previous is None else np.vstack([previous, command_chunk])
        deltas = np.max(np.abs(np.diff(sequence, axis=0)), axis=1)
        if len(deltas):
            steps.extend(deltas.tolist())
            chunk_maxima.append(float(np.max(deltas)))
            if previous is not None:
                boundary_steps.append(float(deltas[0]))
                internal_steps.extend(deltas[1:].tolist())
            else:
                internal_steps.extend(deltas.tolist())
        tracking.extend(np.max(np.abs(command_chunk - target_chunk), axis=1).tolist())
        final_errors.append(float(np.max(np.abs(command_chunk[-1] - target_chunk[-1]))))
        previous = command_chunk[-1]
    if not steps:
        raise ValueError("no hand command transitions were available")
    shifts, missed = _halfway_timing_shifts(targets, commands)
    abs_shifts = np.abs(np.asarray(shifts, dtype=np.float64))
    return {
        "commands": sum(len(chunk) for chunk in commands),
        "step_p95_rad": float(np.percentile(steps, 95)),
        "chunk_max_step_p95_rad": float(np.percentile(chunk_maxima, 95)),
        "global_max_step_rad": max(chunk_maxima),
        "boundary_step_p95_rad": (
            float(np.percentile(boundary_steps, 95)) if boundary_steps else 0.0
        ),
        "boundary_step_max_rad": max(boundary_steps) if boundary_steps else 0.0,
        "internal_step_p95_rad": (
            float(np.percentile(internal_steps, 95)) if internal_steps else 0.0
        ),
        "internal_step_max_rad": max(internal_steps) if internal_steps else 0.0,
        "tracking_error_p95_rad": float(np.percentile(tracking, 95)),
        "tracking_error_max_rad": max(tracking),
        "chunk_final_error_p95_rad": float(np.percentile(final_errors, 95)),
        "chunk_final_error_max_rad": max(final_errors),
        "timing_shift_abs_p95_seconds": (
            float(np.percentile(abs_shifts, 95)) if len(abs_shifts) else 0.0
        ),
        "max_early_shift_seconds": min(shifts) if shifts else 0.0,
        "max_late_shift_seconds": max(shifts) if shifts else 0.0,
        "missed_halfway_events": missed,
    }


def evaluate_nominal(chunks: list[HandChunk]) -> dict[str, dict[str, float | int]]:
    original = nominal_hand_targets(chunks, lambda values: values.copy())
    candidates: dict[str, list[np.ndarray]] = {"original": original}
    for weight in (0.1, 1.0, 10.0):
        candidates[f"jerk_{weight:g}"] = nominal_hand_targets(
            chunks,
            lambda values, w=weight: smooth_fixed_endpoint_trajectory(
                values, jerk_weight=w
            ),
        )
    for cap in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        candidates[f"cap_{cap:.2f}"] = apply_causal_delta_cap(
            original, max_delta_rad=cap
        )
    return {
        name: evaluate_commands(original, commands)
        for name, commands in candidates.items()
    }


def evaluate_recorded(
    chunks: list[HandChunk],
) -> tuple[dict[str, dict[str, float | int]], HandTrackerStats] | None:
    traced = [chunk for chunk in chunks if chunk.actual_commands is not None]
    if not traced:
        return None
    targets = [chunk.actual_commands for chunk in traced]
    assert all(target is not None for target in targets)
    actual = [np.asarray(target) for target in targets]
    initials = [chunk.previous_actual for chunk in traced]
    candidates: dict[str, list[np.ndarray]] = {"recorded_actual": actual}
    for cap in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        candidates[f"recorded_cap_{cap:.2f}"] = apply_causal_delta_cap(
            actual,
            max_delta_rad=cap,
            initial_commands=initials,
        )
    for boundary_cap in (0.10, 0.15, 0.20):
        for internal_cap in (0.25, 0.30):
            candidates[
                f"recorded_boundary_{boundary_cap:.2f}_internal_{internal_cap:.2f}"
            ] = apply_causal_boundary_internal_cap(
                actual,
                boundary_max_delta_rad=boundary_cap,
                internal_max_delta_rad=internal_cap,
                initial_commands=initials,
            )
    tracker, tracker_stats = apply_causal_hand_tracker(
        actual,
        initial_commands=initials,
        command_times=[
            np.asarray(chunk.actual_times, dtype=np.float64) for chunk in traced
        ],
    )
    candidates["recorded_tracker_v1"] = tracker
    metrics = {
        name: evaluate_commands(
            actual,
            commands,
            initial_commands=initials,
        )
        for name, commands in candidates.items()
    }
    return metrics, tracker_stats


def evaluate_logs(paths: list[Path]) -> dict[str, object]:
    chunks: list[HandChunk] = []
    metadata: list[dict[str, object]] = []
    for path in paths:
        loaded = load_hand_chunks(path)
        chunks.extend(loaded)
        metadata.append(
            {
                "path": str(path),
                "chunks": len(loaded),
                "exact_hand_trace_chunks": sum(
                    chunk.actual_commands is not None for chunk in loaded
                ),
            }
        )
    if not chunks:
        raise ValueError("selected logs contain no executed chunks")
    recorded = evaluate_recorded(chunks)
    recorded_candidates = None if recorded is None else recorded[0]
    tracker_stats = None if recorded is None else recorded[1]
    return {
        "logs": metadata,
        "nominal_5hz_candidates": evaluate_nominal(chunks),
        "recorded_5hz_candidates": recorded_candidates,
        "tracker_v1_decisions": (
            None
            if tracker_stats is None
            else {
                "boundary_commands": tracker_stats.boundary_commands,
                "velocity_limit_hits": tracker_stats.velocity_limit_hits,
                "acceleration_limit_hits": tracker_stats.acceleration_limit_hits,
                "direction_reversal_holds": (
                    tracker_stats.direction_reversal_holds
                ),
                "emergency_cap_hits": tracker_stats.emergency_cap_hits,
            }
        ),
    }


def _print_table(title: str, results: dict[str, dict[str, float | int]]) -> None:
    print(title)
    print("candidate       stepP95 chunkMaxP95 globalMax boundaryP95 boundaryMax internalP95 internalMax trackP95 trackMax finalP95 finalMax timeP95 missed")
    for name, metrics in results.items():
        print(
            f"{name:<15} "
            f"{metrics['step_p95_rad']:<7.4f} "
            f"{metrics['chunk_max_step_p95_rad']:<11.4f} "
            f"{metrics['global_max_step_rad']:<9.4f} "
            f"{metrics['boundary_step_p95_rad']:<11.4f} "
            f"{metrics['boundary_step_max_rad']:<11.4f} "
            f"{metrics['internal_step_p95_rad']:<11.4f} "
            f"{metrics['internal_step_max_rad']:<11.4f} "
            f"{metrics['tracking_error_p95_rad']:<8.4f} "
            f"{metrics['tracking_error_max_rad']:<8.4f} "
            f"{metrics['chunk_final_error_p95_rad']:<8.4f} "
            f"{metrics['chunk_final_error_max_rad']:<8.4f} "
            f"{metrics['timing_shift_abs_p95_seconds']:<7.3f} "
            f"{metrics['missed_halfway_events']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline comparison of 5 Hz hand smoothing candidates"
    )
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate_logs(args.logs)
    for item in report["logs"]:
        print(
            f"- {item['path']}: chunks={item['chunks']} "
            f"exact_traces={item['exact_hand_trace_chunks']}"
        )
    print()
    _print_table("Nominal 5 Hz policy replay", report["nominal_5hz_candidates"])
    recorded = report["recorded_5hz_candidates"]
    if recorded is None:
        print("\nRecorded 5 Hz trace: unavailable in these older logs")
    else:
        print()
        _print_table("Recorded acknowledged 5 Hz replay", recorded)
        decisions = report["tracker_v1_decisions"]
        assert decisions is not None
        print(
            "tracker_v1 decisions: "
            + " ".join(f"{key}={value}" for key, value in decisions.items())
        )
    if args.json_output is not None:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
