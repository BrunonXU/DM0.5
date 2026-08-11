"""Offline comparison of arm-only smoothing candidates from G2 telemetry.

This module never imports a robot adapter and never sends hardware commands.  It
reads the recorded ``action_chunk`` arrays, applies candidate filters to the
first 14 dimensions, and evaluates only the portion after the recorded chunk
boundary blend window.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_arm_action_chunk,
)


ARM_DIM = 14
MODEL_HZ = 30.0
SERVO_HZ = 100.0
FIR5_KERNEL = np.asarray([1.0, 4.0, 6.0, 4.0, 1.0]) / 16.0


@dataclass(frozen=True)
class ExecutedChunk:
    actions: np.ndarray
    start_index: int
    interval_count: int
    blend_seconds: float


def zero_phase_fir5(arms: np.ndarray) -> np.ndarray:
    """Apply one symmetric FIR pass while pinning the policy endpoints."""
    values = np.asarray(arms, dtype=np.float64)
    padded = np.pad(values, ((2, 2), (0, 0)), mode="reflect")
    result = np.empty_like(values)
    for joint in range(values.shape[1]):
        result[:, joint] = np.convolve(
            padded[:, joint], FIR5_KERNEL, mode="valid"
        )
    result[0] = values[0]
    result[-1] = values[-1]
    return result


def endpoint_constrained_regularization(
    arms: np.ndarray,
    *,
    acceleration_weight: float,
    jerk_weight: float,
) -> np.ndarray:
    """Smooth waypoints with fixed endpoints and D2/D3 regularization."""
    if acceleration_weight != 0:
        raise ValueError("the runtime candidate currently supports jerk-only regularization")
    result, _ = smooth_arm_action_chunk(arms, jerk_weight=jerk_weight)
    return result


def _sample_linear(values: np.ndarray, elapsed: float) -> np.ndarray:
    position = max(0.0, elapsed) * MODEL_HZ
    lower = min(int(np.floor(position)), len(values) - 1)
    upper = min(lower + 1, len(values) - 1)
    alpha = 0.0 if lower == upper else position - np.floor(position)
    return (1.0 - alpha) * values[lower] + alpha * values[upper]


def _resample(chunk: ExecutedChunk, arms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    duration = chunk.interval_count / MODEL_HZ
    times = np.arange(max(1, int(np.ceil(duration * SERVO_HZ)))) / SERVO_HZ
    active = arms[chunk.start_index :]
    targets = np.stack([_sample_linear(active, float(t)) for t in times])
    return times, targets


def load_executed_chunks(path: Path) -> list[ExecutedChunk]:
    """Join action chunks to dispatch summaries so rejected chunks are skipped."""
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

    chunks: list[ExecutedChunk] = []
    for request_id in sorted(actions.keys() & dispatch.keys()):
        event = actions[request_id]
        array = np.asarray(event.get("actions"), dtype=np.float64)
        if array.ndim != 2 or array.shape[1] < ARM_DIM or not np.all(np.isfinite(array)):
            raise ValueError(f"{path}: request {request_id} has invalid actions")
        start = int(event.get("used_start_index") or 0)
        intervals = int(event.get("used_intervals") or 0)
        if start < 0 or intervals <= 0 or start + intervals > len(array):
            raise ValueError(f"{path}: request {request_id} has invalid executed range")
        chunks.append(
            ExecutedChunk(
                actions=array,
                start_index=start,
                interval_count=intervals,
                blend_seconds=float(dispatch[request_id].get("blend_seconds") or 0.0),
            )
        )
    return chunks


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def evaluate_candidate(
    chunks: list[ExecutedChunk],
    smoother: Callable[[np.ndarray], np.ndarray],
) -> dict[str, float | int]:
    servo_steps: list[float] = []
    chunk_step_maxima: list[float] = []
    chunk_acceleration_maxima: list[float] = []
    chunk_jerk_maxima: list[float] = []
    deviations: list[float] = []
    endpoint_errors: list[float] = []

    for chunk in chunks:
        original = chunk.actions[:, :ARM_DIM]
        smoothed = np.asarray(smoother(original), dtype=np.float64)
        if smoothed.shape != original.shape or not np.all(np.isfinite(smoothed)):
            raise ValueError("smoother returned an invalid arm trajectory")
        deviations.extend(np.abs(smoothed - original).ravel().tolist())
        endpoint_errors.append(
            float(np.max(np.abs(smoothed[[0, -1]] - original[[0, -1]])))
        )

        times, targets = _resample(chunk, smoothed)
        first_internal_tick = int(np.searchsorted(times, chunk.blend_seconds, side="right"))
        # Include one sample before the internal region so its first delta is retained.
        internal = targets[max(0, first_internal_tick - 1) :]
        deltas = np.max(np.abs(np.diff(internal, axis=0)), axis=1)
        if not len(deltas):
            continue
        servo_steps.extend(deltas.tolist())
        chunk_step_maxima.append(float(np.max(deltas)))

        acceleration = np.diff(internal, n=2, axis=0) * SERVO_HZ**2
        jerk = np.diff(internal, n=3, axis=0) * SERVO_HZ**3
        if acceleration.size:
            chunk_acceleration_maxima.append(float(np.max(np.abs(acceleration))))
        if jerk.size:
            chunk_jerk_maxima.append(float(np.max(np.abs(jerk))))

    if not servo_steps:
        raise ValueError("no evaluable post-blend servo intervals were found")
    return {
        "chunks": len(chunk_step_maxima),
        "servo_step_p95_rad": _percentile(servo_steps, 95),
        "chunk_max_step_p50_rad": _percentile(chunk_step_maxima, 50),
        "chunk_max_step_p95_rad": _percentile(chunk_step_maxima, 95),
        "global_max_step_rad": max(chunk_step_maxima),
        "chunk_max_acceleration_p95_rad_s2": _percentile(
            chunk_acceleration_maxima, 95
        ),
        "chunk_max_jerk_p95_rad_s3": _percentile(chunk_jerk_maxima, 95),
        "trajectory_deviation_p95_rad": _percentile(deviations, 95),
        "trajectory_deviation_max_rad": max(deviations),
        "endpoint_error_max_rad": max(endpoint_errors),
    }


def candidate_smoothers() -> dict[str, Callable[[np.ndarray], np.ndarray]]:
    candidates: dict[str, Callable[[np.ndarray], np.ndarray]] = {
        "original": lambda values: values.copy(),
        "fir5": zero_phase_fir5,
    }
    for weight in (0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0):
        candidates[f"jerk_{weight:g}"] = lambda values, w=weight: (
            endpoint_constrained_regularization(
                values, acceleration_weight=0.0, jerk_weight=w
            )
        )
    return candidates


def evaluate_logs(paths: list[Path]) -> dict[str, object]:
    chunks: list[ExecutedChunk] = []
    per_log: list[dict[str, object]] = []
    for path in paths:
        loaded = load_executed_chunks(path)
        chunks.extend(loaded)
        per_log.append(
            {
                "path": str(path),
                "chunks": len(loaded),
                "blend_seconds": sorted({chunk.blend_seconds for chunk in loaded}),
            }
        )
    if not chunks:
        raise ValueError("the selected logs contain no executed action chunks")
    results = {
        name: evaluate_candidate(chunks, smoother)
        for name, smoother in candidate_smoothers().items()
    }
    return {"logs": per_log, "candidate_results": results}


def _print_report(report: dict[str, object]) -> None:
    print("Offline arm-only chunk-internal smoothing evaluation")
    for item in report["logs"]:  # type: ignore[index]
        print(
            f"- {item['path']}: chunks={item['chunks']} "
            f"blend={item['blend_seconds']}s"
        )
    print()
    print(
        "candidate   stepP95  chunkMaxP95  globalMax  jerkP95   "
        "devP95   devMax    endpoint"
    )
    results = report["candidate_results"]  # type: ignore[index]
    for name, metrics in results.items():  # type: ignore[union-attr]
        print(
            f"{name:<11} "
            f"{metrics['servo_step_p95_rad']:<8.5f} "
            f"{metrics['chunk_max_step_p95_rad']:<12.5f} "
            f"{metrics['global_max_step_rad']:<10.5f} "
            f"{metrics['chunk_max_jerk_p95_rad_s3']:<10.1f} "
            f"{metrics['trajectory_deviation_p95_rad']:<8.5f} "
            f"{metrics['trajectory_deviation_max_rad']:<9.5f} "
            f"{metrics['endpoint_error_max_rad']:.1e}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline arm-only smoothing evaluation from G2 JSONL telemetry"
    )
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate_logs(args.logs)
    _print_report(report)
    if args.json_output is not None:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
