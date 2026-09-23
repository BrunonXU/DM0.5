#!/usr/bin/env python3
"""Offline replay of recorded Pink dq through Ruckig; never imports GDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from g2teleop.joint_otg import OTGError, RuckigVelocityOTG, ruckig_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("telemetry", type=Path)
    parser.add_argument(
        "--synchronization",
        nargs="+",
        choices=("phase", "time", "time_if_necessary", "none"),
        default=("phase", "time", "none"),
    )
    parser.add_argument("--control-period-s", type=float, default=0.01)
    parser.add_argument("--max-velocity-rad-s", type=float, default=0.35)
    parser.add_argument("--max-acceleration-rad-s2", type=float, default=2.0)
    parser.add_argument("--max-jerk-rad-s3", type=float, default=20.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float | None:
    return None if not values else float(np.percentile(values, percentile))


def _small_reversals(delta: np.ndarray, previous_delta: np.ndarray) -> int:
    reversal = (
        (delta * previous_delta < 0.0)
        & (np.abs(delta) >= 1e-5)
        & (np.abs(delta) <= 0.003)
        & (np.abs(previous_delta) >= 1e-5)
        & (np.abs(previous_delta) <= 0.003)
    )
    return int(np.count_nonzero(reversal))


def load_samples(path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"malformed JSONL line {number}: {exc}") from exc
            if record.get("event") == "f710_control_sample":
                samples.append(record)
    if not samples:
        raise RuntimeError("no f710_control_sample records")
    return samples


def replay(
    samples: list[dict[str, Any]],
    synchronization: str,
    *,
    control_period_s: float,
    max_velocity_rad_s: float,
    max_acceleration_rad_s2: float,
    max_jerk_rad_s3: float,
) -> dict[str, object]:
    otg: RuckigVelocityOTG | None = None
    previous_side: str | None = None
    previous_sequence: int | None = None
    previous_direct: np.ndarray | None = None
    previous_direct_delta = np.zeros(7)
    previous_otg: np.ndarray | None = None
    previous_otg_delta = np.zeros(7)
    previous_direct_velocity: np.ndarray | None = None
    previous_direct_acceleration: np.ndarray | None = None
    direct_reversals = 0
    otg_reversals = 0
    segments = 0
    replayed = 0
    position_error: list[float] = []
    velocity_error: list[float] = []
    calculation_duration_us: list[float] = []
    direct_acceleration: list[float] = []
    direct_jerk: list[float] = []
    otg_acceleration: list[float] = []
    otg_jerk: list[float] = []
    max_output_velocity = 0.0
    max_output_acceleration = 0.0
    max_output_jerk = 0.0

    for record in samples:
        valid = bool(
            record.get("enabled")
            and record.get("ik_solved_this_tick")
            and isinstance(record.get("ik_q_ref_rad"), list)
            and isinstance(record.get("ik_dq_ref_rad_s"), list)
        )
        if not valid:
            otg = None
            previous_side = None
            previous_sequence = None
            previous_direct = None
            previous_otg = None
            previous_direct_delta[:] = 0.0
            previous_otg_delta[:] = 0.0
            previous_direct_velocity = None
            previous_direct_acceleration = None
            continue

        side = str(record["active_side"])
        sequence = int(record["control_sequence"])
        q_ref = np.asarray(record["ik_q_ref_rad"], dtype=np.float64)
        dq_ref = np.asarray(record["ik_dq_ref_rad_s"], dtype=np.float64)
        new_segment = bool(
            otg is None
            or side != previous_side
            or previous_sequence is None
            or sequence != previous_sequence + 1
        )
        if new_segment:
            lower = q_ref - np.asarray(record["joint_lower_margin_rad"], dtype=np.float64)
            upper = q_ref + np.asarray(record["joint_upper_margin_rad"], dtype=np.float64)
            command = np.asarray(record["arm_command_rad"], dtype=np.float64)
            initial = command[:7] if side == "left" else command[7:14]
            otg = RuckigVelocityOTG(
                7,
                control_period_s,
                max_velocity_rad_s=max_velocity_rad_s,
                max_acceleration_rad_s2=max_acceleration_rad_s2,
                max_jerk_rad_s3=max_jerk_rad_s3,
                lower_position_rad=lower,
                upper_position_rad=upper,
                synchronization=synchronization,
            )
            otg.reset(initial)
            previous_direct = None
            previous_otg = None
            previous_direct_delta[:] = 0.0
            previous_otg_delta[:] = 0.0
            previous_direct_velocity = None
            previous_direct_acceleration = None
            segments += 1

        assert otg is not None
        output = otg.step(dq_ref)
        if previous_direct is not None:
            direct_delta = q_ref - previous_direct
            direct_reversals += _small_reversals(direct_delta, previous_direct_delta)
            previous_direct_delta = direct_delta
            direct_velocity = direct_delta / control_period_s
            if previous_direct_velocity is not None:
                acceleration = (
                    direct_velocity - previous_direct_velocity
                ) / control_period_s
                direct_acceleration.append(float(np.max(np.abs(acceleration))))
                if previous_direct_acceleration is not None:
                    jerk = (
                        acceleration - previous_direct_acceleration
                    ) / control_period_s
                    direct_jerk.append(float(np.max(np.abs(jerk))))
                previous_direct_acceleration = acceleration
            previous_direct_velocity = direct_velocity
        if previous_otg is not None:
            otg_delta = output.position - previous_otg
            otg_reversals += _small_reversals(otg_delta, previous_otg_delta)
            previous_otg_delta = otg_delta
        previous_direct = q_ref
        previous_otg = output.position
        previous_side = side
        previous_sequence = sequence
        replayed += 1
        position_error.append(float(np.max(np.abs(output.position - q_ref))))
        velocity_error.append(float(np.max(np.abs(output.velocity - dq_ref))))
        calculation_duration_us.append(output.calculation_duration_us)
        otg_acceleration.append(float(np.max(np.abs(output.acceleration))))
        otg_jerk.append(float(np.max(np.abs(output.jerk))))
        max_output_velocity = max(max_output_velocity, float(np.max(np.abs(output.velocity))))
        max_output_acceleration = max(
            max_output_acceleration, float(np.max(np.abs(output.acceleration)))
        )
        max_output_jerk = max(max_output_jerk, float(np.max(np.abs(output.jerk))))

    if replayed == 0:
        raise RuntimeError("no enabled solved Pink samples were replayable")
    return {
        "synchronization": synchronization,
        "samples": replayed,
        "segments": segments,
        "direct_small_reversals": direct_reversals,
        "otg_small_reversals": otg_reversals,
        "reversal_change_percent": (
            None
            if direct_reversals == 0
            else 100.0 * (otg_reversals - direct_reversals) / direct_reversals
        ),
        "position_error_p50_rad": _percentile(position_error, 50),
        "position_error_p95_rad": _percentile(position_error, 95),
        "position_error_max_rad": max(position_error),
        "velocity_error_p95_rad_s": _percentile(velocity_error, 95),
        "direct_acceleration_p95_rad_s2": _percentile(direct_acceleration, 95),
        "direct_acceleration_max_rad_s2": (
            None if not direct_acceleration else max(direct_acceleration)
        ),
        "direct_jerk_p95_rad_s3": _percentile(direct_jerk, 95),
        "direct_jerk_max_rad_s3": None if not direct_jerk else max(direct_jerk),
        "otg_acceleration_p95_rad_s2": _percentile(otg_acceleration, 95),
        "otg_jerk_p95_rad_s3": _percentile(otg_jerk, 95),
        "max_output_velocity_rad_s": max_output_velocity,
        "max_output_acceleration_rad_s2": max_output_acceleration,
        "max_output_jerk_rad_s3": max_output_jerk,
        "calculation_duration_p95_us": _percentile(calculation_duration_us, 95),
        "calculation_duration_max_us": max(calculation_duration_us),
    }


def main() -> int:
    cli = parse_args()
    if not cli.telemetry.is_file():
        raise RuntimeError(f"telemetry file not found: {cli.telemetry}")
    if min(
        cli.control_period_s,
        cli.max_velocity_rad_s,
        cli.max_acceleration_rad_s2,
        cli.max_jerk_rad_s3,
    ) <= 0.0:
        raise RuntimeError("period and limits must be positive")
    samples = load_samples(cli.telemetry)
    report = {
        "source": str(cli.telemetry),
        "ruckig_version": ruckig_version(),
        "control_period_s": cli.control_period_s,
        "max_velocity_rad_s": cli.max_velocity_rad_s,
        "max_acceleration_rad_s2": cli.max_acceleration_rad_s2,
        "max_jerk_rad_s3": cli.max_jerk_rad_s3,
        "candidates": [
            replay(
                samples,
                synchronization,
                control_period_s=cli.control_period_s,
                max_velocity_rad_s=cli.max_velocity_rad_s,
                max_acceleration_rad_s2=cli.max_acceleration_rad_s2,
                max_jerk_rad_s3=cli.max_jerk_rad_s3,
            )
            for synchronization in cli.synchronization
        ],
    }
    if cli.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"source={cli.telemetry} Ruckig={report['ruckig_version']}")
        for candidate in report["candidates"]:
            print(
                "sync={synchronization} samples={samples} segments={segments} "
                "reversals={direct_small_reversals}->{otg_small_reversals} "
                "({reversal_change_percent:+.1f}%) q_error_p95={position_error_p95_rad:.6f} "
                "dq_error_p95={velocity_error_p95_rad_s:.6f} "
                "a_p95={direct_acceleration_p95_rad_s2:.2f}->{otg_acceleration_p95_rad_s2:.2f} "
                "jerk_p95={direct_jerk_p95_rad_s3:.1f}->{otg_jerk_p95_rad_s3:.1f} "
                "calc_p95={calculation_duration_p95_us:.2f}us".format(**candidate)
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OTGError as exc:
        raise SystemExit(f"OTG replay failed: {exc}") from exc
