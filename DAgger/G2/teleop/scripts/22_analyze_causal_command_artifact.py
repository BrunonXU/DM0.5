#!/usr/bin/env python3
"""Offline FK and dynamics audit for a frozen causal command artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np

from g2teleop.eef_replay import dual_eef_fk, load_causal_command_artifact
from g2teleop.model_audit import sha256_file


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_npz", type=Path)
    parser.add_argument(
        "--contract", type=Path, default=ROOT / "configs/g2_kinematics_model.json"
    )
    parser.add_argument("--output-json", type=Path)
    return parser.parse_args()


def _dynamics(time_s: np.ndarray, commands: np.ndarray) -> dict[str, object]:
    derivative = commands
    derivative_times = time_s
    result: dict[str, object] = {}
    for order, label in ((1, "velocity_rad_s"), (2, "acceleration_rad_s2"), (3, "jerk_rad_s3")):
        dt = np.diff(derivative_times)
        derivative = np.diff(derivative, axis=0) / dt[:, None]
        derivative_times = (derivative_times[:-1] + derivative_times[1:]) / 2.0
        absolute = np.abs(derivative)
        result[label] = {
            "p95": float(np.percentile(absolute, 95)),
            "p99": float(np.percentile(absolute, 99)),
            "max": float(np.max(absolute)),
        }
    delta = np.diff(commands, axis=0)
    reversals = (
        (delta[1:] * delta[:-1] < 0.0)
        & (np.abs(delta[1:]) >= 1e-5)
        & (np.abs(delta[1:]) <= 0.003)
        & (np.abs(delta[:-1]) >= 1e-5)
        & (np.abs(delta[:-1]) <= 0.003)
    )
    result["small_reversal_count"] = int(np.count_nonzero(reversals))
    result["max_step_rad"] = float(np.max(np.abs(delta)))
    return result


def _eef_errors(pin, reference, candidate) -> dict[str, float]:
    position = np.asarray(
        [
            np.linalg.norm(np.asarray(actual.translation) - np.asarray(target.translation))
            for target, actual in zip(reference, candidate)
        ],
        dtype=np.float64,
    )
    orientation = np.asarray(
        [
            np.linalg.norm(pin.log3(np.asarray(actual.rotation) @ np.asarray(target.rotation).T))
            for target, actual in zip(reference, candidate)
        ],
        dtype=np.float64,
    )
    def endpoint(index: int) -> tuple[float, float]:
        target = reference[index]
        actual = candidate[index]
        return (
            float(np.linalg.norm(np.asarray(actual.translation) - np.asarray(target.translation))),
            float(
                np.linalg.norm(
                    pin.log3(np.asarray(actual.rotation) @ np.asarray(target.rotation).T)
                )
            ),
        )

    start_position, start_orientation = endpoint(0)
    end_position, end_orientation = endpoint(-1)
    return {
        "position_error_p95_m": float(np.percentile(position, 95)),
        "position_error_max_m": float(np.max(position)),
        "orientation_error_p95_rad": float(np.percentile(orientation, 95)),
        "orientation_error_max_rad": float(np.max(orientation)),
        "start_position_error_m": start_position,
        "start_orientation_error_rad": start_orientation,
        "end_position_error_m": end_position,
        "end_orientation_error_rad": end_orientation,
    }


def main() -> int:
    cli = parse_args()
    artifact = load_causal_command_artifact(cli.artifact_npz)
    contract = json.loads(cli.contract.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    model_hash = sha256_file(model_path)
    if model_hash != contract["sources"]["full_o10_mjcf"]["sha256"]:
        raise RuntimeError("vendor MJCF hash differs from frozen G2 contract")
    import pinocchio as pin

    model = pin.buildModelFromMJCF(str(model_path))
    waist = np.zeros(5, dtype=np.float64)
    poses = {
        name: dual_eef_fk(pin, model, waist, artifact.stage(name))
        for name in ("direct", "pink", "otg")
    }
    report: dict[str, object] = {
        "version": "g2-causal-command-analysis-v1",
        "hardware_output": False,
        "artifact_path": str(cli.artifact_npz),
        "artifact_sha256": artifact.artifact_sha256,
        "artifact_metadata": artifact.metadata,
        "model_path": str(model_path),
        "model_sha256": model_hash,
        "ticks": int(len(artifact.time_s)),
        "duration_s": float(artifact.time_s[-1]),
        "stages": {
            name: _dynamics(artifact.time_s, artifact.stage(name))
            for name in ("direct", "pink", "otg")
        },
        "time_aligned_eef_error_vs_direct": {
            stage: {
                side: _eef_errors(pin, poses["direct"][index], poses[stage][index])
                for index, side in enumerate(("left", "right"))
            }
            for stage in ("pink", "otg")
        },
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if cli.output_json is not None:
        cli.output_json.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(f"{cli.output_json}.partial")
        with partial.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
        os.replace(partial, cli.output_json)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
