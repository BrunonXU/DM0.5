"""Replay 10 Hz powered hand targets through causal V1.1 candidates.

The evaluator reads only acknowledged dispatch telemetry.  It carries each
candidate's simulated command across consecutive chunks in the same powered
run, resets only at a recorded discontinuity, and never imports GDK or sends a
hardware command.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    HAND_DIM,
    CausalHandCommandTracker,
)


@dataclass(frozen=True)
class HandTraceChunk:
    request_id: int
    segment_started_at: float
    previous_command: np.ndarray | None
    raw_targets: np.ndarray
    hold_target: np.ndarray
    recorded_commands: np.ndarray
    command_times: np.ndarray


@dataclass(frozen=True)
class Candidate:
    name: str
    max_delta_rad: float
    max_velocity_rad_s: float
    max_acceleration_rad_s2: float
    reversal_boundary_only: bool


@dataclass
class Decisions:
    boundary_commands: int = 0
    velocity_limit_hits: int = 0
    acceleration_limit_hits: int = 0
    direction_reversal_holds: int = 0
    emergency_cap_hits: int = 0
    discontinuity_resets: int = 0
    reconstructed_hold_commands: int = 0


def _array(payload: object, *, shape_tail: int, label: str) -> np.ndarray:
    array = np.asarray(payload, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != shape_tail:
        raise ValueError(f"{label} must be Nx{shape_tail}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{label} must be finite")
    return array


def load_trace(path: Path) -> list[HandTraceChunk]:
    chunks: list[HandTraceChunk] = []
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            events.append(event)
    action_events = {
        int(event["request_id"]): event
        for event in events
        if event.get("event") == "action_chunk"
        and isinstance(event.get("request_id"), int)
    }
    for line_number, event in enumerate(events, start=1):
        if event.get("event") != "dispatch_summary":
            continue
        raw_payload = event.get("hand_raw_targets_rad")
        command_payload = event.get("hand_commands_rad")
        times_payload = event.get("hand_command_time_offsets_seconds")
        if raw_payload is None or command_payload is None or times_payload is None:
            continue
        if raw_payload == [] and command_payload == [] and times_payload == []:
            # A fail-closed segment can end before the first acknowledged hand
            # command. It carries no replayable hand samples and is not a
            # malformed trace.
            continue
        raw = _array(raw_payload, shape_tail=HAND_DIM, label="raw targets")
        commands = _array(
            command_payload, shape_tail=HAND_DIM, label="recorded commands"
        )
        times = np.asarray(times_payload, dtype=np.float64)
        if raw.shape != commands.shape or times.shape != (len(raw),):
            raise ValueError(
                f"{path}:{line_number}: hand trace arrays do not align"
            )
        if not np.all(np.isfinite(times)) or (
            len(times) > 1 and np.any(np.diff(times) <= 0)
        ):
            raise ValueError(f"{path}:{line_number}: invalid hand times")
        previous_payload = event.get("hand_previous_command_rad")
        previous = (
            None
            if previous_payload is None
            else np.asarray(previous_payload, dtype=np.float64)
        )
        if previous is not None and (
            previous.shape != (HAND_DIM,) or not np.all(np.isfinite(previous))
        ):
            raise ValueError(f"{path}:{line_number}: invalid previous hand command")
        request_id = int(event["request_id"])
        action_event = action_events.get(request_id)
        if action_event is None:
            raise ValueError(f"{path}: request {request_id} has no action_chunk")
        actions = _array(
            action_event.get("actions"), shape_tail=34, label="actions"
        )
        start = int(action_event.get("used_start_index") or 0)
        intervals = int(action_event.get("used_intervals") or 0)
        endpoint = min(start + intervals, len(actions) - 1)
        segment_started_at = float(action_event["wall_time"])
        if not np.isfinite(segment_started_at):
            raise ValueError(f"{path}: request {request_id} has invalid wall time")
        chunks.append(
            HandTraceChunk(
                request_id=request_id,
                segment_started_at=segment_started_at,
                previous_command=previous,
                raw_targets=raw,
                hold_target=actions[endpoint, 14:34].copy(),
                recorded_commands=commands,
                command_times=times,
            )
        )
    if not chunks:
        raise ValueError(f"{path}: no exact hand traces")
    return chunks


def replay(
    chunks: list[HandTraceChunk],
    candidate: Candidate,
    *,
    nominal_hz: float = 10.0,
    boundary_blend_seconds: float = 0.4,
    reversal_error_rad: float = 0.15,
) -> tuple[list[np.ndarray], list[np.ndarray | None], Decisions]:
    tracker = CausalHandCommandTracker(
        enabled=True,
        boundary_blend_seconds=boundary_blend_seconds,
        max_velocity_rad_s=candidate.max_velocity_rad_s,
        max_acceleration_rad_s2=candidate.max_acceleration_rad_s2,
        max_delta_rad=candidate.max_delta_rad,
        reversal_error_rad=reversal_error_rad,
        reversal_boundary_only=candidate.reversal_boundary_only,
        nominal_hz=nominal_hz,
    )
    decisions = Decisions()
    result: list[np.ndarray] = []
    anchors: list[np.ndarray | None] = []
    previous_request: int | None = None
    last_step_at: float | None = None
    held_target: np.ndarray | None = None
    simulated_command: np.ndarray | None = None
    nominal_dt = 1.0 / nominal_hz

    for chunk in chunks:
        continuous = (
            previous_request is not None
            and chunk.request_id == previous_request + 1
        )
        if previous_request is None or not continuous:
            tracker.reset(chunk.previous_command)
            simulated_command = (
                None
                if chunk.previous_command is None
                else chunk.previous_command.copy()
            )
            last_step_at = None
            held_target = None
            if previous_request is not None:
                decisions.discontinuity_resets += 1
        first_step_at = (
            chunk.segment_started_at + float(chunk.command_times[0])
            if len(chunk.command_times)
            else chunk.segment_started_at
        )
        if continuous and held_target is not None and last_step_at is not None:
            reconstructed_at = last_step_at + nominal_dt
            while reconstructed_at < first_step_at - 0.5 * nominal_dt:
                decision = tracker.step(held_target, now=reconstructed_at)
                simulated_command = decision.command.copy()
                decisions.reconstructed_hold_commands += 1
                decisions.velocity_limit_hits += decision.velocity_limit_hits
                decisions.acceleration_limit_hits += decision.acceleration_limit_hits
                decisions.direction_reversal_holds += decision.direction_reversal_holds
                decisions.emergency_cap_hits += decision.emergency_cap_hits
                last_step_at = reconstructed_at
                reconstructed_at += nominal_dt
        tracker.begin_segment(started_at=chunk.segment_started_at)
        anchors.append(
            None if simulated_command is None else simulated_command.copy()
        )
        commands = np.empty_like(chunk.raw_targets)
        for index, (target, elapsed) in enumerate(
            zip(chunk.raw_targets, chunk.command_times, strict=True)
        ):
            absolute_time = chunk.segment_started_at + float(elapsed)
            decision = tracker.step(target, now=absolute_time)
            commands[index] = decision.command
            simulated_command = decision.command.copy()
            last_step_at = absolute_time
            decisions.boundary_commands += int(decision.boundary_active)
            decisions.velocity_limit_hits += decision.velocity_limit_hits
            decisions.acceleration_limit_hits += decision.acceleration_limit_hits
            decisions.direction_reversal_holds += decision.direction_reversal_holds
            decisions.emergency_cap_hits += decision.emergency_cap_hits
        result.append(commands)
        held_target = chunk.hold_target.copy()
        previous_request = chunk.request_id
    return result, anchors, decisions


def _percentile(values: list[float], level: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), level))


def _percentile_or_zero(values: list[float], level: float) -> float:
    return 0.0 if not values else _percentile(values, level)


def _derivatives(
    commands: np.ndarray, times: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(commands) < 2:
        empty = np.empty((0, HAND_DIM), dtype=np.float64)
        return empty, empty, empty
    velocity = np.diff(commands, axis=0) / np.diff(times)[:, None]
    velocity_times = 0.5 * (times[1:] + times[:-1])
    if len(velocity) < 2:
        empty = np.empty((0, HAND_DIM), dtype=np.float64)
        return velocity, empty, empty
    acceleration = np.diff(velocity, axis=0) / np.diff(velocity_times)[:, None]
    acceleration_times = 0.5 * (velocity_times[1:] + velocity_times[:-1])
    if len(acceleration) < 2:
        empty = np.empty((0, HAND_DIM), dtype=np.float64)
        return velocity, acceleration, empty
    jerk = np.diff(acceleration, axis=0) / np.diff(acceleration_times)[:, None]
    return velocity, acceleration, jerk


def metrics(
    chunks: list[HandTraceChunk],
    commands_by_chunk: list[np.ndarray],
    *,
    anchors: list[np.ndarray | None] | None = None,
) -> dict[str, float | int]:
    steps: list[float] = []
    chunk_step_maxima: list[float] = []
    tracking_errors: list[float] = []
    final_errors: list[float] = []
    velocity_abs: list[float] = []
    acceleration_abs: list[float] = []
    jerk_abs: list[float] = []
    halfway_shifts: list[float] = []
    missed_halfway = 0
    halfway_events = 0
    command_deviation: list[float] = []
    if anchors is not None and len(anchors) != len(chunks):
        raise ValueError("anchors must align with chunks")

    for chunk_index, (chunk, commands) in enumerate(
        zip(chunks, commands_by_chunk, strict=True)
    ):
        anchor = (
            chunk.previous_command
            if anchors is None
            else anchors[chunk_index]
        )
        sequence = (
            commands
            if anchor is None
            else np.vstack([anchor, commands])
        )
        if len(sequence) > 1:
            chunk_steps = np.max(np.abs(np.diff(sequence, axis=0)), axis=1)
            steps.extend(chunk_steps.tolist())
            chunk_step_maxima.append(float(np.max(chunk_steps)))
        errors = np.max(np.abs(commands - chunk.raw_targets), axis=1)
        tracking_errors.extend(errors.tolist())
        command_deviation.extend(
            np.max(
                np.abs(commands - chunk.recorded_commands), axis=1
            ).tolist()
        )
        if len(commands):
            final_errors.append(
                float(np.max(np.abs(commands[-1] - chunk.raw_targets[-1])))
            )
        velocity, acceleration, jerk = _derivatives(commands, chunk.command_times)
        velocity_abs.extend(np.abs(velocity).ravel().tolist())
        acceleration_abs.extend(np.abs(acceleration).ravel().tolist())
        jerk_abs.extend(np.abs(jerk).ravel().tolist())

        if len(commands) < 2:
            continue
        movement = chunk.raw_targets[-1] - chunk.raw_targets[0]
        for joint, total in enumerate(movement):
            if abs(total) < 0.1:
                continue
            threshold = chunk.raw_targets[0, joint] + 0.5 * total
            if total > 0:
                target_hits = np.flatnonzero(
                    chunk.raw_targets[:, joint] >= threshold
                )
                command_hits = np.flatnonzero(commands[:, joint] >= threshold)
            else:
                target_hits = np.flatnonzero(
                    chunk.raw_targets[:, joint] <= threshold
                )
                command_hits = np.flatnonzero(commands[:, joint] <= threshold)
            if not len(target_hits):
                continue
            halfway_events += 1
            if not len(command_hits):
                missed_halfway += 1
                continue
            halfway_shifts.append(
                float(
                    chunk.command_times[command_hits[0]]
                    - chunk.command_times[target_hits[0]]
                )
            )

    if not steps or not tracking_errors:
        raise ValueError("trace has no hand transitions")
    return {
        "commands": int(sum(len(item) for item in commands_by_chunk)),
        "step_p95_rad": _percentile(steps, 95),
        "chunk_step_max_p95_rad": _percentile(chunk_step_maxima, 95),
        "step_max_rad": max(chunk_step_maxima),
        "velocity_p95_rad_s": _percentile_or_zero(velocity_abs, 95),
        "acceleration_p95_rad_s2": _percentile_or_zero(acceleration_abs, 95),
        "jerk_p95_rad_s3": _percentile_or_zero(jerk_abs, 95),
        "tracking_error_p95_rad": _percentile(tracking_errors, 95),
        "tracking_error_max_rad": max(tracking_errors),
        "recorded_command_deviation_p95_rad": _percentile(
            command_deviation, 95
        ),
        "recorded_command_deviation_max_rad": max(command_deviation),
        "final_error_p95_rad": _percentile(final_errors, 95),
        "final_error_max_rad": max(final_errors),
        "halfway_shift_abs_p95_seconds": (
            _percentile([abs(value) for value in halfway_shifts], 95)
            if halfway_shifts
            else 0.0
        ),
        "halfway_shift_max_late_seconds": (
            max(halfway_shifts) if halfway_shifts else 0.0
        ),
        "halfway_shift_max_early_seconds": (
            min(halfway_shifts) if halfway_shifts else 0.0
        ),
        "missed_halfway_events": int(missed_halfway),
        "halfway_events": int(halfway_events),
    }


def default_candidates() -> list[Candidate]:
    candidates = [
        Candidate("v1_current", 0.4, 2.0, 10.0, False),
        Candidate("v1_current_boundary_reversal", 0.4, 2.0, 10.0, True),
    ]
    for delta, velocity, acceleration in product(
        (0.05, 0.075, 0.10),
        (0.5, 0.75, 1.0),
        (3.0, 5.0),
    ):
        candidates.append(
            Candidate(
                f"v11_d{delta:g}_v{velocity:g}_a{acceleration:g}",
                delta,
                velocity,
                acceleration,
                True,
            )
        )
    for delta, velocity, acceleration in (
        (0.10, 1.25, 5.0),
        (0.10, 1.25, 7.5),
        (0.125, 1.25, 5.0),
        (0.125, 1.25, 7.5),
        (0.125, 1.5, 7.5),
        (0.15, 1.5, 5.0),
        (0.15, 1.5, 7.5),
        (0.16, 1.6, 7.5),
        (0.16, 1.6, 10.0),
        (0.175, 1.75, 7.5),
        (0.175, 1.75, 10.0),
        (0.18, 1.8, 10.0),
    ):
        candidates.append(
            Candidate(
                f"v11_refine_d{delta:g}_v{velocity:g}_a{acceleration:g}",
                delta,
                velocity,
                acceleration,
                True,
            )
        )
    return candidates


def evaluate(paths: list[Path]) -> dict[str, object]:
    all_results: dict[str, list[dict[str, float | int]]] = {
        candidate.name: [] for candidate in default_candidates()
    }
    recorded_results: list[dict[str, float | int]] = []
    run_metadata: list[dict[str, object]] = []
    decision_totals: dict[str, Decisions] = {
        candidate.name: Decisions() for candidate in default_candidates()
    }

    for path in paths:
        chunks = load_trace(path)
        recorded_results.append(
            metrics(chunks, [chunk.recorded_commands for chunk in chunks])
        )
        run_metadata.append(
            {
                "path": str(path),
                "chunks": len(chunks),
                "commands": sum(len(chunk.raw_targets) for chunk in chunks),
            }
        )
        for candidate in default_candidates():
            commands, anchors, decisions = replay(chunks, candidate)
            all_results[candidate.name].append(
                metrics(chunks, commands, anchors=anchors)
            )
            total = decision_totals[candidate.name]
            for field, value in asdict(decisions).items():
                setattr(total, field, getattr(total, field) + int(value))

    # Re-evaluate concatenated scalar populations would require retaining every
    # sample.  Weighted means are inappropriate for percentiles, so expose a
    # conservative run-max aggregation when more than one powered run is used.
    def aggregate(items: list[dict[str, float | int]]) -> dict[str, float | int]:
        if len(items) == 1:
            return items[0]
        result: dict[str, float | int] = {}
        for key in items[0]:
            values = [item[key] for item in items]
            result[key] = (
                sum(int(value) for value in values)
                if key in {"commands", "missed_halfway_events", "halfway_events"}
                else max(float(value) for value in values)
            )
        return result

    candidates_payload = []
    for candidate in default_candidates():
        candidates_payload.append(
            {
                "config": asdict(candidate),
                "metrics": aggregate(all_results[candidate.name]),
                "decisions": asdict(decision_totals[candidate.name]),
            }
        )
    return {
        "schema": "g2_hand_v11_sweep_v1",
        "scope": "exact recorded 10 Hz raw hand targets and command times; no GDK",
        "runs": run_metadata,
        "recorded_actual": aggregate(recorded_results),
        "candidates": candidates_payload,
    }


def _print(report: dict[str, object]) -> None:
    print("candidate                         stepP95 chunkP95 maxStep jerkP95 trackP95 finalP95 halfP95 missed/events velHits accHits")
    recorded = report["recorded_actual"]
    assert isinstance(recorded, dict)
    print(
        f"{'recorded_actual':<33} "
        f"{recorded['step_p95_rad']:<7.4f} "
        f"{recorded['chunk_step_max_p95_rad']:<8.4f} "
        f"{recorded['step_max_rad']:<7.4f} "
        f"{recorded['jerk_p95_rad_s3']:<7.1f} "
        f"{recorded['tracking_error_p95_rad']:<8.4f} "
        f"{recorded['final_error_p95_rad']:<8.4f} "
        f"{recorded['halfway_shift_abs_p95_seconds']:<7.3f} "
        f"{recorded['missed_halfway_events']}/{recorded['halfway_events']:<8} -       -"
    )
    candidates = report["candidates"]
    assert isinstance(candidates, list)
    for item in candidates:
        metrics_payload = item["metrics"]
        decisions = item["decisions"]
        config = item["config"]
        print(
            f"{config['name']:<33} "
            f"{metrics_payload['step_p95_rad']:<7.4f} "
            f"{metrics_payload['chunk_step_max_p95_rad']:<8.4f} "
            f"{metrics_payload['step_max_rad']:<7.4f} "
            f"{metrics_payload['jerk_p95_rad_s3']:<7.1f} "
            f"{metrics_payload['tracking_error_p95_rad']:<8.4f} "
            f"{metrics_payload['final_error_p95_rad']:<8.4f} "
            f"{metrics_payload['halfway_shift_abs_p95_seconds']:<7.3f} "
            f"{metrics_payload['missed_halfway_events']}/{metrics_payload['halfway_events']:<8} "
            f"{decisions['velocity_limit_hits']:<7} "
            f"{decisions['acceleration_limit_hits']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline sweep of causal 10 Hz hand V1.1 candidates"
    )
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args.logs)
    _print(report)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
