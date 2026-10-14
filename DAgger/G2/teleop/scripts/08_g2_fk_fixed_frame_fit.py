#!/usr/bin/env python3
"""Fit and validate fixed vendor-frame -> recorded-end transforms; no GDK."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from g2teleop.model_audit import sha256_file


DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "configs" / "g2_kinematics_model.json"


def rotation_error_deg(a: np.ndarray, b: np.ndarray) -> float:
    cosine = max(-1.0, min(1.0, (float(np.trace(a.T @ b)) - 1.0) * 0.5))
    return math.degrees(math.acos(cosine))


def stats(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "max": float(np.max(array)),
    }


def set_scalar(model: object, q: np.ndarray, name: str, value: float) -> None:
    joint_id = model.getJointId(name)
    if joint_id == 0 or model.joints[joint_id].nq != 1:
        raise RuntimeError(f"missing/non-scalar joint {name}")
    q[model.idx_qs[joint_id]] = value


def make_q(pin: object, model: object, sample: dict[str, object]) -> np.ndarray:
    q = pin.neutral(model)
    q[:3] = np.asarray(sample["state/robot/position"], dtype=np.float64)
    q[3:7] = np.asarray(sample["state/robot/orientation"], dtype=np.float64)
    for i, value in enumerate(sample["state/waist/position"], 1):
        set_scalar(model, q, f"idx0{i}_body_joint{i}", float(value))
    arm = sample["state/joint/position"]
    for i, value in enumerate(arm[:7], 1):
        set_scalar(model, q, f"idx2{i}_arm_l_joint{i}", float(value))
    for i, value in enumerate(arm[7:], 1):
        set_scalar(model, q, f"idx6{i}_arm_r_joint{i}", float(value))
    return q


def measured_pose(pin: object, position: object, quaternion: object, order: str) -> object:
    values = np.asarray(quaternion, dtype=np.float64)
    if order == "xyzw":
        x, y, z, w = values
    elif order == "wxyz":
        w, x, y, z = values
    else:
        raise ValueError(order)
    quat = pin.Quaternion(float(w), float(x), float(y), float(z))
    quat.normalize()
    return pin.SE3(quat.matrix(), np.asarray(position, dtype=np.float64))


def evaluate_scope(offsets: list[object]) -> dict[str, object]:
    # Translation mean and projected rotation mean are deterministic least-squares
    # representatives.  Do not use the first frame as the fit: it can be a
    # transient and makes cross-episode comparisons unnecessarily fragile.
    translation = np.mean([item.translation for item in offsets], axis=0)
    rotation_sum = np.sum([item.rotation for item in offsets], axis=0)
    u, _, vt = np.linalg.svd(rotation_sum)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0.0:
        u[:, -1] *= -1.0
        rotation = u @ vt
    position_errors = [float(np.linalg.norm(item.translation - translation)) for item in offsets]
    rotation_errors = [rotation_error_deg(rotation, item.rotation) for item in offsets]
    quaternion = __import__("pinocchio").Quaternion(rotation).coeffs().tolist()
    return {
        "samples": len(offsets),
        "fitted_offset": {
            "translation_m": translation.tolist(),
            "quaternion_xyzw": quaternion,
        },
        "fixed_offset_residual": {
            "position_m": stats(position_errors),
            "orientation_deg": stats(rotation_errors),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only fixed frame fit on G2")
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
    frames = {
        "left": ("arm_l_link7", "hand_l_base_link"),
        "right": ("arm_r_link7", "hand_r_base_link"),
    }
    collected: dict[str, dict[str, list[object]]] = {}
    hypotheses: list[dict[str, object]] = []
    measured_fields = {
        "end": ("state/end/position", "state/end/orientation"),
        "arm": ("state/end/arm_position", "state/end/arm_orientation"),
    }
    for quaternion_order in ("xyzw", "wxyz"):
        for side_order in (("left", "right"), ("right", "left")):
            for measured_kind, (position_field, orientation_field) in measured_fields.items():
                for side in ("left", "right"):
                    measured_index = side_order.index(side)
                    for frame in frames[side]:
                        key = (
                            f"q={quaternion_order}|array={side_order[0]},{side_order[1]}"
                            f"|measured={measured_kind}|{side}|{frame}"
                        )
                        collected[key] = {"pooled": []}
                        for episode in payload["episodes"]:
                            collected[key][episode["episode"]] = []
                            for sample in episode["samples"]:
                                q = make_q(pin, model, sample)
                                pin.forwardKinematics(model, data, q)
                                pin.updateFramePlacements(model, data)
                                measured = measured_pose(
                                    pin,
                                    sample[position_field][measured_index],
                                    sample[orientation_field][measured_index],
                                    quaternion_order,
                                )
                                model_pose = data.oMf[model.getFrameId(frame)]
                                offset = model_pose.inverse() * measured
                                collected[key]["pooled"].append(offset)
                                collected[key][episode["episode"]].append(offset)
                        pooled = evaluate_scope(collected[key]["pooled"])
                        per_episode = {
                            episode: evaluate_scope(values)
                            for episode, values in collected[key].items()
                            if episode != "pooled"
                        }
                        score = (
                            pooled["fixed_offset_residual"]["position_m"]["p95"]
                            + math.radians(
                                pooled["fixed_offset_residual"]["orientation_deg"]["p95"]
                            )
                        )
                        hypotheses.append(
                            {
                                "key": key,
                                "quaternion_order": quaternion_order,
                                "source_array_order": list(side_order),
                                "measured_kind": measured_kind,
                                "robot_side": side,
                                "model_frame": frame,
                                "score": score,
                                "pooled": pooled,
                                "per_episode": per_episode,
                            }
                        )
    hypotheses.sort(key=lambda item: item["score"])
    report = {
        "read_only": True,
        "gdk_imported": False,
        "samples_sha256": sha256_file(args.samples),
        "hypothesis_count": len(hypotheses),
        "best_hypotheses": hypotheses[:8],
        "all_hypotheses": hypotheses,
        "result": "FIXED_FRAME_HYPOTHESES_RANKED_REVIEW_BEFORE_FREEZE",
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(json.dumps({"best_hypotheses": hypotheses[:8]}, indent=2, ensure_ascii=False))
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
