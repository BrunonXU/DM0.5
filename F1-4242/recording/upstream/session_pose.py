"""Validated per-launch F1 joint snapshot used as the X+Y home pose."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Mapping, Sequence


ARM_JOINT_NAMES = tuple(
    [f"arm_l_j{i}" for i in range(1, 8)]
    + [f"arm_r_j{i}" for i in range(1, 8)]
)
HEAD_BODY_JOINT_NAMES = tuple(
    [f"head_j{i}" for i in range(1, 4)]
    + [f"body_j{i}" for i in range(1, 6)]
)
JOINT_MOTOR_NAMES = ARM_JOINT_NAMES + HEAD_BODY_JOINT_NAMES
GRIPPER_NAMES = ("left_gripper_position", "right_gripper_position")
# F1_D070 has two OmniPicker single-active-DoF grippers. The hand_* entries
# still present in /hal/joint_states are unused interface placeholders and are
# intentionally excluded.
SESSION_JOINT_NAMES = JOINT_MOTOR_NAMES + GRIPPER_NAMES
# X+Y intentionally returns only the two arms and the operator-controlled
# torso pitch. Head, body_j1/j2/j4/j5, and both OmniPickers remain untouched.
RESET_JOINT_NAMES = ARM_JOINT_NAMES + ("body_j3",)


def default_session_pose_path() -> Path:
    configured = os.environ.get("PICO_F1_INITIAL_POSE")
    if configured:
        return Path(configured)
    root = Path(os.environ.get("PICO_F1_ROOT", Path(__file__).resolve().parents[2]))
    return root / "run" / "session_initial_pose.json"


def validate_joint_map(values: Mapping[str, float]) -> dict[str, float]:
    missing = [name for name in SESSION_JOINT_NAMES if name not in values]
    if missing:
        raise ValueError(f"initial pose missing F1 joints: {missing}")
    result = {name: float(values[name]) for name in SESSION_JOINT_NAMES}
    invalid = [name for name, value in result.items() if not math.isfinite(value)]
    if invalid:
        raise ValueError(f"initial pose has non-finite joints: {invalid}")
    if result["body_j4"] > 0.0 or result["body_j5"] > 0.0:
        raise ValueError("F1 contract requires body_j4 and body_j5 to be non-positive")
    invalid_grippers = [name for name in GRIPPER_NAMES if not 0.0 <= result[name] <= 100.0]
    if invalid_grippers:
        raise ValueError(f"OmniPicker positions must be within [0, 100]: {invalid_grippers}")
    return result


def joint_map_from_sample(names: Sequence[str], positions: Sequence[float]) -> dict[str, float]:
    if len(names) != len(positions) or len(set(names)) != len(names):
        raise ValueError("joint names/positions are inconsistent")
    return validate_joint_map(dict(zip(names, positions)))


def load_session_pose(path: str | os.PathLike[str] | None = None) -> dict:
    pose_path = Path(path) if path is not None else default_session_pose_path()
    try:
        payload = json.loads(pose_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"missing or invalid per-launch initial pose: {pose_path}") from exc
    if payload.get("schema") != "pico-f1-session-pose-v1":
        raise ValueError(f"unsupported initial-pose schema in {pose_path}")
    names = payload.get("name", [])
    positions = payload.get("position", [])
    values = joint_map_from_sample(names, positions)
    payload["name"] = list(SESSION_JOINT_NAMES)
    payload["position"] = [values[name] for name in SESSION_JOINT_NAMES]
    payload["joints"] = values
    payload["path"] = str(pose_path)
    return payload
