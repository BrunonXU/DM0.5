#!/usr/bin/env python3
"""Compare DM0.5 numeric samples with vendor-model FK; no GDK or commands."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np


DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "configs" / "g2_kinematics_model.json"


def rotation_error_deg(rotation_a: np.ndarray, rotation_b: np.ndarray) -> float:
    relative = rotation_a.T @ rotation_b
    cosine = max(-1.0, min(1.0, (float(np.trace(relative)) - 1.0) * 0.5))
    return math.degrees(math.acos(cosine))


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def set_named_scalar(model: object, q: np.ndarray, name: str, value: float) -> None:
    joint_id = model.getJointId(name)
    if joint_id == 0:
        raise RuntimeError(f"joint missing: {name}")
    if model.joints[joint_id].nq != 1:
        raise RuntimeError(f"expected scalar joint: {name}")
    q[model.idx_qs[joint_id]] = value


def make_configuration(pin: object, model: object, sample: dict[str, object]) -> np.ndarray:
    q = pin.neutral(model)
    position = np.asarray(sample["state/robot/position"], dtype=np.float64)
    orientation_xyzw = np.asarray(sample["state/robot/orientation"], dtype=np.float64)
    q[0:3] = position
    q[3:7] = orientation_xyzw
    for index, value in enumerate(sample["state/waist/position"], start=1):
        set_named_scalar(model, q, f"idx0{index}_body_joint{index}", float(value))
    joints = sample["state/joint/position"]
    for index, value in enumerate(joints[:7], start=1):
        set_named_scalar(model, q, f"idx2{index}_arm_l_joint{index}", float(value))
    for index, value in enumerate(joints[7:], start=1):
        set_named_scalar(model, q, f"idx6{index}_arm_r_joint{index}", float(value))
    return q


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only FK candidate-frame audit on G2")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import pinocchio as pin

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    payload = json.loads(args.samples.read_text(encoding="utf-8"))
    model = pin.buildModelFromMJCF(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    data = model.createData()
    candidates = {
        "left": ["arm_l_link7", "hand_l_base_link"],
        "right": ["arm_r_link7", "hand_r_base_link"],
    }
    metrics = {
        side: {
            frame: {"position_m": [], "orientation_deg": []}
            for frame in frames
        }
        for side, frames in candidates.items()
    }
    worst: dict[str, dict[str, object]] = {}
    count = 0
    for episode in payload["episodes"]:
        for sample in episode["samples"]:
            q = make_configuration(pin, model, sample)
            pin.forwardKinematics(model, data, q)
            pin.updateFramePlacements(model, data)
            measured_positions = np.asarray(sample["state/end/position"], dtype=np.float64)
            measured_quaternions = np.asarray(sample["state/end/orientation"], dtype=np.float64)
            for side_index, side in enumerate(("left", "right")):
                measured_rotation = pin.Quaternion(measured_quaternions[side_index]).matrix()
                for frame in candidates[side]:
                    placement = data.oMf[model.getFrameId(frame)]
                    pos_error = float(np.linalg.norm(placement.translation - measured_positions[side_index]))
                    rot_error = rotation_error_deg(placement.rotation, measured_rotation)
                    metrics[side][frame]["position_m"].append(pos_error)
                    metrics[side][frame]["orientation_deg"].append(rot_error)
                    key = f"{side}:{frame}"
                    if key not in worst or pos_error > worst[key]["position_m"]:
                        worst[key] = {
                            "episode": episode["episode"], "frame": sample["frame"],
                            "position_m": pos_error, "orientation_deg": rot_error,
                        }
            count += 1
    summary: dict[str, object] = {}
    for side, frames in metrics.items():
        summary[side] = {}
        for frame, values in frames.items():
            summary[side][frame] = {
                key: {
                    "p50": percentile(items, 50),
                    "p95": percentile(items, 95),
                    "max": max(items),
                }
                for key, items in values.items()
            }
    report = {
        "read_only": True,
        "gdk_imported": False,
        "sample_count": count,
        "assumptions": {
            "source_quaternion": "xyzw",
            "source_side_order": ["left", "right"],
            "base_and_waist_included": True,
        },
        "summary": summary,
        "worst_position_samples": worst,
        "result": "CANDIDATE_COMPARISON_ONLY_REVIEW_METRICS_BEFORE_FK_PASS",
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
