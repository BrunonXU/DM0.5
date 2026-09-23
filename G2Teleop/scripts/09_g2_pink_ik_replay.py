#!/usr/bin/env python3
"""Offline ±axis Pink IK replay on G2 vendor model; never imports GDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from g2teleop.model_audit import build_reduced_arm_model, sha256_file
from g2teleop.pink_ik import IKHold, PinkRelativeArmIK


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "configs" / "g2_kinematics_model.json"


def set_scalar(model: object, q: np.ndarray, name: str, value: float) -> None:
    joint_id = model.getJointId(name)
    if joint_id == 0 or model.joints[joint_id].nq != 1:
        raise RuntimeError(f"missing/non-scalar joint {name}")
    q[model.idx_qs[joint_id]] = value


def make_full_q(pin: object, model: object, sample: dict[str, object]) -> np.ndarray:
    q = pin.neutral(model)
    q[:3] = np.asarray(sample["state/robot/position"], dtype=np.float64)
    q[3:7] = np.asarray(sample["state/robot/orientation"], dtype=np.float64)
    for index, value in enumerate(sample["state/waist/position"], 1):
        set_scalar(model, q, f"idx0{index}_body_joint{index}", float(value))
    arm = sample["state/joint/position"]
    for index, value in enumerate(arm[:7], 1):
        set_scalar(model, q, f"idx2{index}_arm_l_joint{index}", float(value))
    for index, value in enumerate(arm[7:], 1):
        set_scalar(model, q, f"idx6{index}_arm_r_joint{index}", float(value))
    return q


def percentile(values: list[float], value: float) -> float | None:
    return None if not values else float(np.percentile(values, value))


def stats(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": None if not values else max(values),
    }


def trials() -> list[dict[str, object]]:
    result = []
    for kind, magnitude in (("translation", 0.001), ("rotation", math.radians(1.0))):
        for axis in range(3):
            for sign in (-1.0, 1.0):
                vector = np.zeros(3)
                vector[axis] = sign * magnitude
                result.append({"kind": kind, "axis": axis, "delta": vector})
    return result


def run_trial(
    model: object,
    frame: str,
    q0: np.ndarray,
    trial: dict[str, object],
    position_tolerance_m: float,
    orientation_tolerance_rad: float,
) -> dict[str, object]:
    ik = PinkRelativeArmIK(model, frame, q0)
    delta = trial["delta"]
    if trial["kind"] == "translation":
        ik.advance_target(delta / 0.05, np.zeros(3), 0.05)
    else:
        # 1 degree at the 10 deg/s Shadow cap is two 50 ms target updates.
        for _ in range(2):
            ik.advance_target(np.zeros(3), delta / 0.1, 0.05)
    q = q0.copy()
    solve_times_ms: list[float] = []
    last = None
    try:
        for iteration in range(100):
            start = time.perf_counter_ns()
            last = ik.solve(q, 0.01)
            solve_times_ms.append((time.perf_counter_ns() - start) / 1e6)
            q = last.q
            if (
                last.position_error_m <= position_tolerance_m
                and last.orientation_error_rad <= orientation_tolerance_rad
            ):
                break
        assert last is not None
        passed = (
            last.position_error_m <= position_tolerance_m
            and last.orientation_error_rad <= orientation_tolerance_rad
        )
        return {
            "passed": passed,
            "iterations": iteration + 1,
            "position_error_m": last.position_error_m,
            "orientation_error_deg": math.degrees(last.orientation_error_rad),
            "max_joint_delta_rad": float(np.max(np.abs(q - q0))),
            "solve_times_ms": solve_times_ms,
            "hold_reason": None,
        }
    except Exception as exc:
        return {
            "passed": False,
            "iterations": len(solve_times_ms),
            "position_error_m": None,
            "orientation_error_deg": None,
            "max_joint_delta_rad": float(np.max(np.abs(q - q0))),
            "solve_times_ms": solve_times_ms,
            "hold_reason": f"{type(exc).__name__}: {exc}",
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only G2 Pink IK replay")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episode-prefix", default="sorting_")
    parser.add_argument("--sample-stride", type=int, default=5)
    parser.add_argument("--position-tolerance-m", type=float, default=0.0001)
    parser.add_argument("--orientation-tolerance-deg", type=float, default=0.05)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import pinocchio as pin
    import pink
    import qpsolvers
    import proxsuite

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    payload = json.loads(args.samples.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    expected_hash = contract["sources"]["full_o10_mjcf"]["sha256"]
    if sha256_file(model_path) != expected_hash:
        raise RuntimeError("vendor MJCF hash differs from frozen contract")
    full_model = pin.buildModelFromMJCF(str(model_path))
    arm_joints = contract["controlled_arm_joints"]
    selected = [
        episode for episode in payload["episodes"]
        if episode["episode"].startswith(args.episode_prefix)
    ]
    if not selected:
        raise RuntimeError("no episodes matched prefix")

    rows: list[dict[str, object]] = []
    all_times: list[float] = []
    for episode in selected:
        for sample in episode["samples"][:: args.sample_stride]:
            full_q = make_full_q(pin, full_model, sample)
            for side, arm_slice, frame in (
                ("left", slice(0, 7), "hand_l_base_link"),
                ("right", slice(7, 14), "hand_r_base_link"),
            ):
                reduced = build_reduced_arm_model(
                    pin, full_model, arm_joints[side], reference_q=full_q
                )
                if list(reduced.names[1:]) != arm_joints[side]:
                    raise RuntimeError(f"unexpected {side} reduced joint order")
                q0 = np.asarray(sample["state/joint/position"][arm_slice], dtype=np.float64)
                for trial in trials():
                    result = run_trial(
                        reduced,
                        frame,
                        q0,
                        trial,
                        args.position_tolerance_m,
                        math.radians(args.orientation_tolerance_deg),
                    )
                    all_times.extend(result.pop("solve_times_ms"))
                    rows.append(
                        {
                            "episode": episode["episode"],
                            "frame": sample["frame"],
                            "side": side,
                            "kind": trial["kind"],
                            "axis": trial["axis"],
                            "delta": trial["delta"].tolist(),
                            **result,
                        }
                    )

    failures = [row for row in rows if not row["passed"]]
    report = {
        "read_only": True,
        "gdk_imported": False,
        "hardware_output": False,
        "powered_allowed": False,
        "versions": {
            "pinocchio": pin.__version__,
            "pink": pink.__version__,
            "qpsolvers": qpsolvers.__version__,
            "proxsuite": proxsuite.__version__,
        },
        "qp_solver": "proxqp",
        "available_solvers": qpsolvers.available_solvers,
        "samples_sha256": sha256_file(args.samples),
        "model_sha256": expected_hash,
        "episode_filter": args.episode_prefix,
        "episodes": [episode["episode"] for episode in selected],
        "sample_stride": args.sample_stride,
        "acceptance": {
            "position_tolerance_m": args.position_tolerance_m,
            "orientation_tolerance_deg": args.orientation_tolerance_deg,
            "max_iterations": 100,
        },
        "trial_count": len(rows),
        "pass_count": len(rows) - len(failures),
        "failure_count": len(failures),
        "solve_time_ms": stats(all_times),
        "failures": failures[:50],
        "rows_sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "rows": rows,
        "result": "IK_REPLAY_PASS" if not failures else "IK_REPLAY_REVIEW_FAILURES",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "versions", "episodes", "trial_count", "pass_count", "failure_count",
        "solve_time_ms", "result",
    )}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
