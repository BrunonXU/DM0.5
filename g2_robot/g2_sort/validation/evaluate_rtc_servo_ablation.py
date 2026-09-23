#!/usr/bin/env python3
"""Offline RTC arm smoothing and 100 Hz interpolation ablation.

This evaluator recursively rebuilds the committed RTC trajectory from a
powered-RTC telemetry log, then samples every executable suffix at the real
100 Hz servo cadence.  It never imports GDK or sends hardware commands.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, CHUNK_STEPS, MODEL_HZ
from g2_sort.deployment.g2_client.joint_layout import (
    POLICY_JOINT_LIMITS,
    clip_action_positions,
)
from g2_sort.deployment.g2_client.rtc import project_absolute_rtc_prefix
from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_arm_action_chunk,
    smooth_rtc_arm_action_suffix,
)


ARM_DIM = 14
SERVO_HZ = 100.0


@dataclass(frozen=True)
class Candidate:
    weight: float
    interpolation: str
    blend_seconds: float
    blend_mode: str = "fixed"

    @property
    def name(self) -> str:
        suffix = "" if self.blend_mode == "fixed" else f"_{self.blend_mode}"
        return (
            f"w{self.weight:g}_{self.interpolation}_b{self.blend_seconds:g}"
            f"{suffix}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--delay-steps", type=int, default=16)
    parser.add_argument("--execution-horizon", type=int, default=20)
    parser.add_argument("--weights", default="0,1,3,10,30,100")
    parser.add_argument("--blends", default="0,0.12")
    parser.add_argument(
        "--blend-modes",
        default="fixed",
        help="comma-separated fixed,moving; moving crossfades old/new trajectories",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_chunks(path: Path) -> list[tuple[int, np.ndarray]]:
    chunks: list[tuple[int, np.ndarray]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") != "action_chunk":
                continue
            actions = np.asarray(event.get("actions"), dtype=np.float64)
            if actions.shape != (CHUNK_STEPS, ACTION_DIM):
                raise ValueError(f"request {event.get('request_id')} has {actions.shape}")
            if not np.all(np.isfinite(actions)):
                raise ValueError("action chunk contains non-finite values")
            chunks.append((int(event["request_id"]), actions))
    if len(chunks) < 2:
        raise ValueError("at least two complete action chunks are required")
    return chunks


def clip(actions: np.ndarray) -> np.ndarray:
    result, _count, _max_delta = clip_action_positions(actions)
    return result


def derivatives(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return shared normalized-time velocity and acceleration at waypoints."""
    if len(values) < 3:
        raise ValueError("at least three waypoints are required")
    velocity = np.empty_like(values)
    acceleration = np.empty_like(values)
    velocity[0] = values[1] - values[0]
    velocity[-1] = values[-1] - values[-2]
    velocity[1:-1] = 0.5 * (values[2:] - values[:-2])
    acceleration[0] = values[2] - 2.0 * values[1] + values[0]
    acceleration[-1] = values[-1] - 2.0 * values[-2] + values[-3]
    acceleration[1:-1] = values[2:] - 2.0 * values[1:-1] + values[:-2]
    return velocity, acceleration


def pchip_velocity(values: np.ndarray) -> np.ndarray:
    """Uniform-grid shape-preserving PCHIP slopes for every joint."""
    delta = np.diff(values, axis=0)
    velocity = np.zeros_like(values)
    same_direction = delta[:-1] * delta[1:] > 0.0
    harmonic = np.zeros_like(delta[:-1])
    np.divide(
        2.0 * delta[:-1] * delta[1:],
        delta[:-1] + delta[1:],
        out=harmonic,
        where=same_direction,
    )
    velocity[1:-1] = harmonic

    first = 0.5 * (3.0 * delta[0] - delta[1])
    first = np.where(np.sign(first) != np.sign(delta[0]), 0.0, first)
    first = np.where(
        (np.sign(delta[0]) != np.sign(delta[1]))
        & (np.abs(first) > 3.0 * np.abs(delta[0])),
        3.0 * delta[0],
        first,
    )
    last = 0.5 * (3.0 * delta[-1] - delta[-2])
    last = np.where(np.sign(last) != np.sign(delta[-1]), 0.0, last)
    last = np.where(
        (np.sign(delta[-1]) != np.sign(delta[-2]))
        & (np.abs(last) > 3.0 * np.abs(delta[-1])),
        3.0 * delta[-1],
        last,
    )
    velocity[0] = first
    velocity[-1] = last
    return velocity


def natural_cubic_velocity(
    values: np.ndarray,
    *,
    start_index: int = 0,
    start_velocity: np.ndarray | None = None,
) -> np.ndarray:
    """Return slopes for a C2 cubic spline on a uniform waypoint grid.

    RTC may clamp the first suffix slope to the velocity of the old committed
    plan.  The remaining suffix slopes are solved together, rather than using
    independent centered estimates, so acceleration is continuous at every
    internal waypoint.  The far end uses the natural (zero curvature)
    condition because it is outside the executed RTC horizon.
    """

    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or len(array) < 2:
        raise ValueError("natural cubic spline needs at least two waypoints")
    if not 0 <= start_index < len(array) - 1:
        raise ValueError("natural cubic start index is outside the trajectory")
    suffix = array[start_index:]
    count = len(suffix)
    system = np.zeros((count, count), dtype=np.float64)
    rhs = np.zeros_like(suffix)
    if start_velocity is None:
        system[0, 0] = 2.0
        system[0, 1] = 1.0
        rhs[0] = 3.0 * (suffix[1] - suffix[0])
    else:
        velocity = np.asarray(start_velocity, dtype=np.float64)
        if velocity.shape != (array.shape[1],):
            raise ValueError("natural cubic start velocity has wrong shape")
        system[0, 0] = 1.0
        rhs[0] = velocity
    for index in range(1, count - 1):
        system[index, index - 1 : index + 2] = (1.0, 4.0, 1.0)
        rhs[index] = 3.0 * (suffix[index + 1] - suffix[index - 1])
    system[-1, -2] = 1.0
    system[-1, -1] = 2.0
    rhs[-1] = 3.0 * (suffix[-1] - suffix[-2])
    solved = np.linalg.solve(system, rhs)
    result = derivatives(array)[0]
    result[start_index:] = solved
    return result


def interpolation_velocity(values: np.ndarray, method: str) -> np.ndarray:
    if method == "pchip":
        return pchip_velocity(values)
    if method in {"cubic", "quintic"}:
        return derivatives(values)[0]
    if method == "spline":
        return natural_cubic_velocity(values)
    if method == "linear":
        velocity = np.empty_like(values)
        velocity[0] = values[1] - values[0]
        velocity[1:] = values[1:] - values[:-1]
        return velocity
    raise ValueError(f"unknown interpolation method {method}")


def sample(
    values: np.ndarray,
    positions: np.ndarray,
    method: str,
    *,
    velocity_override_index: int | None = None,
    velocity_override: np.ndarray | None = None,
) -> np.ndarray:
    lower = np.minimum(np.floor(positions).astype(int), len(values) - 1)
    upper = np.minimum(lower + 1, len(values) - 1)
    u = positions - np.floor(positions)
    u[lower == upper] = 0.0
    p0 = values[lower]
    p1 = values[upper]
    column = u[:, None]
    if method == "linear":
        return (1.0 - column) * p0 + column * p1

    velocity, acceleration = derivatives(values)
    if method == "pchip":
        velocity = pchip_velocity(values)
    if method == "spline":
        velocity = natural_cubic_velocity(
            values,
            start_index=(
                0 if velocity_override_index is None else velocity_override_index
            ),
            start_velocity=velocity_override,
        )
    elif velocity_override_index is not None:
        if velocity_override is None or velocity_override.shape != (values.shape[1],):
            raise ValueError("velocity override must match trajectory dimensions")
        velocity = velocity.copy()
        velocity[velocity_override_index] = velocity_override
    v0 = velocity[lower]
    v1 = velocity[upper]
    if method in {"cubic", "pchip", "spline"}:
        u2 = column * column
        u3 = u2 * column
        return (
            (2.0 * u3 - 3.0 * u2 + 1.0) * p0
            + (u3 - 2.0 * u2 + column) * v0
            + (-2.0 * u3 + 3.0 * u2) * p1
            + (u3 - u2) * v1
        )
    if method != "quintic":
        raise ValueError(f"unknown interpolation method {method}")
    a0 = acceleration[lower]
    a1 = acceleration[upper]
    delta = p1 - p0
    c0 = p0
    c1 = v0
    c2 = 0.5 * a0
    c3 = 10.0 * delta - 6.0 * v0 - 4.0 * v1 - 1.5 * a0 + 0.5 * a1
    c4 = -15.0 * delta + 8.0 * v0 + 7.0 * v1 + 1.5 * a0 - a1
    c5 = 6.0 * delta - 3.0 * v0 - 3.0 * v1 - 0.5 * a0 + 0.5 * a1
    return c0 + column * (
        c1 + column * (c2 + column * (c3 + column * (c4 + column * c5)))
    )


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean": float(np.mean(values)),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def prepare_bootstrap(raw: np.ndarray, weight: float) -> np.ndarray:
    actions, _stats = smooth_arm_action_chunk(clip(raw), jerk_weight=weight)
    return clip(actions)


def prepare_rtc(
    raw: np.ndarray,
    committed: np.ndarray,
    *,
    delay: int,
    execution: int,
    weight: float,
    incoming_velocity: np.ndarray,
) -> np.ndarray:
    prefix = committed[execution:]
    actions = project_absolute_rtc_prefix(
        clip(raw), prefix, inference_delay=delay
    )
    actions, _stats = smooth_rtc_arm_action_suffix(
        actions,
        start_index=delay,
        boundary_arm_position=prefix[delay, :ARM_DIM],
        boundary_arm_velocity_per_step=incoming_velocity,
        jerk_weight=weight,
    )
    return clip(actions)


def evaluate(
    chunks: list[tuple[int, np.ndarray]],
    candidate: Candidate,
    *,
    delay: int,
    execution: int,
) -> dict[str, object]:
    committed = prepare_bootstrap(chunks[0][1], candidate.weight)
    step_p95: list[float] = []
    step_max: list[float] = []
    velocity_max: list[float] = []
    acceleration_max: list[float] = []
    jerk_p95: list[float] = []
    jerk_max: list[float] = []
    servo_deviation_p95: list[float] = []
    servo_deviation_max: list[float] = []
    waypoint_deviation_max: list[float] = []
    interpolation_overshoot_max: list[float] = []
    physical_limit_violation_max: list[float] = []
    records: list[dict[str, float | int]] = []

    tick_count = int(np.ceil(execution / MODEL_HZ * SERVO_HZ))
    local_times = np.arange(tick_count, dtype=np.float64) / SERVO_HZ
    positions = delay + local_times * MODEL_HZ
    for (request_id, raw) in chunks[1:]:
        old_switch_index = execution + delay
        old_velocity = interpolation_velocity(
            committed[:, :ARM_DIM], candidate.interpolation
        )[old_switch_index]
        prepared = prepare_rtc(
            raw,
            committed,
            delay=delay,
            execution=execution,
            weight=candidate.weight,
            incoming_velocity=old_velocity,
        )
        arm = prepared[:, :ARM_DIM]
        raw_arm = raw[:, :ARM_DIM]
        sampled = sample(
            arm,
            positions.copy(),
            candidate.interpolation,
            velocity_override_index=(
                None if candidate.interpolation == "linear" else delay
            ),
            velocity_override=(
                None if candidate.interpolation == "linear" else old_velocity
            ),
        )
        raw_linear = sample(raw_arm, positions.copy(), "linear")
        prior_positions = old_switch_index + np.asarray([-0.9, -0.6, -0.3])
        prior = sample(
            committed[:, :ARM_DIM],
            prior_positions,
            candidate.interpolation,
        )
        anchor = committed[old_switch_index, :ARM_DIM]
        if candidate.blend_seconds > 0:
            active = local_times < candidate.blend_seconds
            u = np.clip(
                local_times[active] / candidate.blend_seconds,
                0.0,
                1.0,
            )
            if candidate.blend_mode == "moving":
                # Quintic alpha has zero first and second derivatives at both
                # ends.  Crossfading two moving paths therefore changes plan
                # without the periodic stop caused by a fixed anchor.
                alpha = u**3 * (u * (u * 6.0 - 15.0) + 10.0)
                old_positions = (
                    old_switch_index + local_times[active] * MODEL_HZ
                )
                old_continuation = sample(
                    committed[:, :ARM_DIM],
                    old_positions,
                    candidate.interpolation,
                )
                sampled[active] = (
                    (1.0 - alpha[:, None]) * old_continuation
                    + alpha[:, None] * sampled[active]
                )
            elif candidate.blend_mode == "fixed":
                alpha = u * u * (3.0 - 2.0 * u)
                sampled[active] = anchor + alpha[:, None] * (
                    sampled[active] - anchor
                )
            else:
                raise ValueError(f"unknown blend mode {candidate.blend_mode}")
        stitched = np.vstack([prior, sampled])
        delta = np.diff(stitched, axis=0)
        velocity = delta * SERVO_HZ
        acceleration = np.diff(stitched, n=2, axis=0) * SERVO_HZ**2
        jerk = np.diff(stitched, n=3, axis=0) * SERVO_HZ**3
        absolute_step = np.max(np.abs(delta), axis=1)
        absolute_jerk = np.abs(jerk).ravel()
        deviation = np.abs(sampled - raw_linear)
        waypoint_deviation = np.abs(
            prepared[delay:, :ARM_DIM] - raw[delay:, :ARM_DIM]
        )

        lower = np.floor(positions).astype(int)
        upper = np.minimum(lower + 1, len(arm) - 1)
        lo = np.minimum(arm[lower], arm[upper])
        hi = np.maximum(arm[lower], arm[upper])
        overshoot = np.maximum(lo - sampled, sampled - hi)
        overshoot = np.maximum(overshoot, 0.0)
        physical_violation = np.maximum(
            POLICY_JOINT_LIMITS[:ARM_DIM, 0] - sampled,
            sampled - POLICY_JOINT_LIMITS[:ARM_DIM, 1],
        )
        physical_violation = np.maximum(physical_violation, 0.0)

        values = {
            "request_id": request_id,
            "servo_step_p95_rad": float(np.percentile(absolute_step, 95)),
            "servo_step_max_rad": float(np.max(absolute_step)),
            "servo_velocity_max_rad_s": float(np.max(np.abs(velocity))),
            "servo_acceleration_max_rad_s2": float(np.max(np.abs(acceleration))),
            "servo_jerk_p95_rad_s3": float(np.percentile(absolute_jerk, 95)),
            "servo_jerk_max_rad_s3": float(np.max(absolute_jerk)),
            "servo_policy_deviation_p95_rad": float(np.percentile(deviation, 95)),
            "servo_policy_deviation_max_rad": float(np.max(deviation)),
            "waypoint_policy_deviation_max_rad": float(np.max(waypoint_deviation)),
            "interpolation_overshoot_max_rad": float(np.max(overshoot)),
            "physical_limit_violation_max_rad": float(
                np.max(physical_violation)
            ),
        }
        records.append(values)
        step_p95.append(values["servo_step_p95_rad"])
        step_max.append(values["servo_step_max_rad"])
        velocity_max.append(values["servo_velocity_max_rad_s"])
        acceleration_max.append(values["servo_acceleration_max_rad_s2"])
        jerk_p95.append(values["servo_jerk_p95_rad_s3"])
        jerk_max.append(values["servo_jerk_max_rad_s3"])
        servo_deviation_p95.append(values["servo_policy_deviation_p95_rad"])
        servo_deviation_max.append(values["servo_policy_deviation_max_rad"])
        waypoint_deviation_max.append(values["waypoint_policy_deviation_max_rad"])
        interpolation_overshoot_max.append(values["interpolation_overshoot_max_rad"])
        physical_limit_violation_max.append(
            values["physical_limit_violation_max_rad"]
        )
        committed = prepared

    metrics = {
        "servo_step_p95_rad": summarize(step_p95),
        "servo_step_max_rad": summarize(step_max),
        "servo_velocity_max_rad_s": summarize(velocity_max),
        "servo_acceleration_max_rad_s2": summarize(acceleration_max),
        "servo_jerk_p95_rad_s3": summarize(jerk_p95),
        "servo_jerk_max_rad_s3": summarize(jerk_max),
        "servo_policy_deviation_p95_rad": summarize(servo_deviation_p95),
        "servo_policy_deviation_max_rad": summarize(servo_deviation_max),
        "waypoint_policy_deviation_max_rad": summarize(waypoint_deviation_max),
        "interpolation_overshoot_max_rad": summarize(interpolation_overshoot_max),
        "physical_limit_violation_max_rad": summarize(physical_limit_violation_max),
    }
    return {"candidate": candidate.__dict__, "metrics": metrics, "records": records}


def main() -> None:
    args = parse_args()
    delay = int(args.delay_steps)
    execution = int(args.execution_horizon)
    if not 1 <= delay < CHUNK_STEPS - execution:
        raise ValueError("require 1 <= delay < H-execution")
    weights = [float(value) for value in args.weights.split(",")]
    blends = [float(value) for value in args.blends.split(",")]
    blend_modes = [value.strip() for value in args.blend_modes.split(",")]
    if not blend_modes or any(
        value not in {"fixed", "moving"} for value in blend_modes
    ):
        raise ValueError("blend modes must be fixed and/or moving")
    chunks = load_chunks(args.log)
    candidates = [
        Candidate(weight, interpolation, blend, blend_mode)
        for weight in weights
        for interpolation in ("linear", "cubic", "spline", "pchip", "quintic")
        for blend in blends
        for blend_mode in blend_modes
        if blend > 0.0 or blend_mode == "fixed"
    ]
    results = {
        candidate.name: evaluate(
            chunks,
            candidate,
            delay=delay,
            execution=execution,
        )
        for candidate in candidates
    }
    report = {
        "schema": "g2_rtc_servo_ablation_v1",
        "source_log": str(args.log),
        "scope": "recursive 30 Hz RTC postprocess and ideal 100 Hz arm sampling; no GDK",
        "delay_steps": delay,
        "execution_horizon": execution,
        "num_switches": len(chunks) - 1,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("candidate jerkP95 jerkMaxP95 accelP95 stepP95 devP95 devMaxP95 overshootMax limitMax")
    for name, result in results.items():
        metrics = result["metrics"]
        print(
            f"{name:<14} "
            f"{metrics['servo_jerk_p95_rad_s3']['mean']:<9.1f} "
            f"{metrics['servo_jerk_max_rad_s3']['p95']:<11.1f} "
            f"{metrics['servo_acceleration_max_rad_s2']['p95']:<8.2f} "
            f"{metrics['servo_step_p95_rad']['mean']:<8.5f} "
            f"{metrics['servo_policy_deviation_p95_rad']['mean']:<7.5f} "
            f"{metrics['servo_policy_deviation_max_rad']['p95']:<9.5f} "
            f"{metrics['interpolation_overshoot_max_rad']['max']:.6f} "
            f"{metrics['physical_limit_violation_max_rad']['max']:.6f}"
        )


if __name__ == "__main__":
    main()
