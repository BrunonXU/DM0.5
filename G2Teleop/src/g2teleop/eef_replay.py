"""Convert frozen dual-arm joint demonstrations into an ideal EEF input stream.

This module is deliberately free of GDK calls.  It owns only the deterministic
joint -> FK -> Cartesian-twist conversion used by the diagnostic replay.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np


ARM_DIM = 14
ACTION_DIM = 34


@dataclass(frozen=True)
class FrozenReplay:
    episode_id: str
    sample_hz: float
    states: np.ndarray
    actions: np.ndarray
    source_sha256: str
    artifact_sha256: str

    @property
    def duration_s(self) -> float:
        return (len(self.actions) - 1) / self.sample_hz

    def trajectory(self, source: str) -> np.ndarray:
        """Return a copy of the explicitly selected recorded 34D stream."""
        if source == "states":
            return self.states.copy()
        if source == "actions":
            return self.actions.copy()
        raise ValueError("path source must be 'states' or 'actions'")


@dataclass(frozen=True)
class ReplayTimeline:
    """Monotonic timestamps assigned to the original demonstration frames."""

    frame_times_s: np.ndarray

    def __post_init__(self) -> None:
        times = np.asarray(self.frame_times_s, dtype=np.float64)
        if (
            times.ndim != 1
            or len(times) < 2
            or not np.isfinite(times).all()
            or times[0] != 0.0
            or np.any(np.diff(times) <= 0.0)
        ):
            raise ValueError("frame_times_s must be finite, increasing, and start at zero")
        object.__setattr__(self, "frame_times_s", times.copy())

    @property
    def duration_s(self) -> float:
        return float(self.frame_times_s[-1])

    def locate(self, time_s: float) -> tuple[int, int, float]:
        if not math.isfinite(time_s):
            raise ValueError("time_s must be finite")
        value = float(np.clip(time_s, 0.0, self.duration_s))
        upper = int(np.searchsorted(self.frame_times_s, value, side="right"))
        upper = min(max(upper, 1), len(self.frame_times_s) - 1)
        lower = upper - 1
        span = self.frame_times_s[upper] - self.frame_times_s[lower]
        alpha = float((value - self.frame_times_s[lower]) / span)
        if value >= self.duration_s:
            lower = upper = len(self.frame_times_s) - 1
            alpha = 0.0
        return lower, upper, alpha


@dataclass(frozen=True)
class CausalCommandArtifact:
    """Frozen, common-clock arm references for one-variable hardware A/B tests."""

    metadata: dict[str, Any]
    time_s: np.ndarray
    q_direct: np.ndarray
    q_pink: np.ndarray
    q_otg: np.ndarray
    hands: np.ndarray
    source_frame_times_s: np.ndarray
    artifact_sha256: str

    def stage(self, name: str) -> np.ndarray:
        stages = {
            "direct": self.q_direct,
            "pink": self.q_pink,
            "otg": self.q_otg,
        }
        try:
            return stages[name].copy()
        except KeyError as exc:
            raise ValueError("command stage must be direct, pink, or otg") from exc


def load_causal_command_artifact(path: Path) -> CausalCommandArtifact:
    """Load and strictly validate an offline-generated causal command artifact."""
    artifact_path = Path(path)
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    with np.load(artifact_path, allow_pickle=False) as payload:
        required = {
            "metadata_json",
            "time_s",
            "q_direct",
            "q_pink",
            "q_otg",
            "hands",
            "source_frame_times_s",
        }
        if not required.issubset(payload.files):
            raise ValueError("causal command artifact is missing required arrays")
        metadata = json.loads(str(payload["metadata_json"].item()))
        time_s = np.asarray(payload["time_s"], dtype=np.float64)
        q_direct = np.asarray(payload["q_direct"], dtype=np.float64)
        q_pink = np.asarray(payload["q_pink"], dtype=np.float64)
        q_otg = np.asarray(payload["q_otg"], dtype=np.float64)
        hands = np.asarray(payload["hands"], dtype=np.float64)
        source_times = np.asarray(payload["source_frame_times_s"], dtype=np.float64)
    if metadata.get("version") not in {
        "g2-causal-command-replay-v1",
        "g2-causal-command-replay-v2",
    }:
        raise ValueError("unsupported causal command artifact version")
    if (
        time_s.ndim != 1
        or len(time_s) < 2
        or time_s[0] != 0.0
        or not np.isfinite(time_s).all()
        or np.any(np.diff(time_s) <= 0.0)
    ):
        raise ValueError("artifact time_s must be finite, increasing, and start at zero")
    expected_arm_shape = (len(time_s), ARM_DIM)
    expected_hand_shape = (len(time_s), ACTION_DIM - ARM_DIM)
    if any(values.shape != expected_arm_shape for values in (q_direct, q_pink, q_otg)):
        raise ValueError("artifact arm stages must all be Nx14")
    if hands.shape != expected_hand_shape:
        raise ValueError("artifact hands must be Nx20")
    if not all(
        np.isfinite(values).all()
        for values in (q_direct, q_pink, q_otg, hands, source_times)
    ):
        raise ValueError("causal command artifact contains non-finite values")
    ReplayTimeline(source_times)
    return CausalCommandArtifact(
        metadata=metadata,
        time_s=time_s.copy(),
        q_direct=q_direct.copy(),
        q_pink=q_pink.copy(),
        q_otg=q_otg.copy(),
        hands=hands.copy(),
        source_frame_times_s=source_times.copy(),
        artifact_sha256=digest,
    )


def load_frozen_replay(path: Path) -> FrozenReplay:
    raw = Path(path).read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("version") != "g2-training-replay-v1":
        raise ValueError("unsupported replay artifact version")
    sample_hz = float(payload.get("sample_hz", 0.0))
    states = np.asarray(payload.get("states"), dtype=np.float64)
    actions = np.asarray(payload.get("actions"), dtype=np.float64)
    if not math.isfinite(sample_hz) or sample_hz <= 0.0:
        raise ValueError("replay sample_hz must be finite and positive")
    if actions.ndim != 2 or actions.shape[1] != ACTION_DIM or len(actions) < 2:
        raise ValueError("replay actions must be Nx34 with N >= 2")
    if states.shape != actions.shape:
        raise ValueError("replay states must have the same Nx34 shape as actions")
    if not np.isfinite(states).all() or not np.isfinite(actions).all():
        raise ValueError("replay states/actions contain non-finite values")
    declared_frames = int(payload.get("frame_count", len(actions)))
    if declared_frames != len(actions):
        raise ValueError("replay frame_count disagrees with states/actions")
    return FrozenReplay(
        episode_id=str(payload.get("episode_id", "unknown")),
        sample_hz=sample_hz,
        states=states.copy(),
        actions=actions.copy(),
        source_sha256=str(payload.get("source_jsonl_sha256", "")),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )


def sample_vector(samples: np.ndarray, sample_hz: float, time_s: float) -> np.ndarray:
    """Linearly sample a fixed-rate vector trajectory, clamped at both ends."""
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 2 or len(values) < 1 or not np.isfinite(values).all():
        raise ValueError("samples must be a non-empty finite 2D array")
    if not math.isfinite(sample_hz) or sample_hz <= 0.0:
        raise ValueError("sample_hz must be finite and positive")
    if not math.isfinite(time_s):
        raise ValueError("time_s must be finite")
    position = float(np.clip(time_s * sample_hz, 0.0, len(values) - 1))
    lower = int(math.floor(position))
    upper = min(lower + 1, len(values) - 1)
    alpha = position - lower
    return values[lower] * (1.0 - alpha) + values[upper] * alpha


def sample_timed_vector(samples: np.ndarray, timeline: ReplayTimeline, time_s: float) -> np.ndarray:
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 2 or len(values) != len(timeline.frame_times_s):
        raise ValueError("samples must match the replay timeline")
    lower, upper, alpha = timeline.locate(time_s)
    return values[lower] * (1.0 - alpha) + values[upper] * alpha


def _set_scalar_joint(model: Any, q: np.ndarray, name: str, value: float) -> None:
    joint_id = model.getJointId(name)
    if joint_id == 0 or model.joints[joint_id].nq != 1:
        raise ValueError(f"missing/non-scalar model joint {name}")
    q[model.idx_qs[joint_id]] = value


def dual_eef_fk(
    pin: Any,
    model: Any,
    waist: Sequence[float],
    arm_actions: np.ndarray,
    *,
    left_frame: str = "hand_l_base_link",
    right_frame: str = "hand_r_base_link",
) -> tuple[list[Any], list[Any]]:
    """Compute both hand-base SE(3) trajectories from a 14D arm trajectory."""
    waist_array = np.asarray(waist, dtype=np.float64)
    arms = np.asarray(arm_actions, dtype=np.float64)
    if waist_array.shape != (5,) or not np.isfinite(waist_array).all():
        raise ValueError("waist must be finite 5D")
    if arms.ndim != 2 or arms.shape[1] != ARM_DIM or not np.isfinite(arms).all():
        raise ValueError("arm_actions must be finite Nx14")
    frame_ids = (model.getFrameId(left_frame), model.getFrameId(right_frame))
    if any(frame_id >= len(model.frames) for frame_id in frame_ids):
        raise ValueError("required hand-base frame is absent from model")
    data = model.createData()
    q = pin.neutral(model)
    q[:3] = 0.0
    q[3:7] = np.asarray([0.0, 0.0, 0.0, 1.0])
    for index, value in enumerate(waist_array, 1):
        _set_scalar_joint(model, q, f"idx0{index}_body_joint{index}", float(value))
    left: list[Any] = []
    right: list[Any] = []
    for arm in arms:
        for index, value in enumerate(arm[:7], 1):
            _set_scalar_joint(model, q, f"idx2{index}_arm_l_joint{index}", float(value))
        for index, value in enumerate(arm[7:], 1):
            _set_scalar_joint(model, q, f"idx6{index}_arm_r_joint{index}", float(value))
        pin.forwardKinematics(model, data, q)
        pin.updateFramePlacements(model, data)
        left.append(data.oMf[frame_ids[0]].copy())
        right.append(data.oMf[frame_ids[1]].copy())
    return left, right


def sample_pose(pin: Any, poses: Sequence[Any], sample_hz: float, time_s: float) -> Any:
    """Sample SE(3) with linear translation and shortest-log SO(3) interpolation."""
    if not poses:
        raise ValueError("poses must not be empty")
    position = float(np.clip(time_s * sample_hz, 0.0, len(poses) - 1))
    lower = int(math.floor(position))
    upper = min(lower + 1, len(poses) - 1)
    alpha = position - lower
    start = poses[lower]
    end = poses[upper]
    result = start.copy()
    result.translation = (
        np.asarray(start.translation) * (1.0 - alpha)
        + np.asarray(end.translation) * alpha
    )
    delta_base = pin.log3(np.asarray(end.rotation) @ np.asarray(start.rotation).T)
    result.rotation = pin.exp3(delta_base * alpha) @ np.asarray(start.rotation)
    return result


def sample_timed_pose(
    pin: Any, poses: Sequence[Any], timeline: ReplayTimeline, time_s: float
) -> Any:
    if len(poses) != len(timeline.frame_times_s):
        raise ValueError("poses must match the replay timeline")
    lower, upper, alpha = timeline.locate(time_s)
    start = poses[lower]
    end = poses[upper]
    result = start.copy()
    result.translation = (
        np.asarray(start.translation) * (1.0 - alpha)
        + np.asarray(end.translation) * alpha
    )
    delta_base = pin.log3(np.asarray(end.rotation) @ np.asarray(start.rotation).T)
    result.rotation = pin.exp3(delta_base * alpha) @ np.asarray(start.rotation)
    return result


def ideal_twist(
    pin: Any,
    poses: Sequence[Any],
    sample_hz: float,
    time_s: float,
    dt: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the base-frame twist of the EEF demonstration over one control tick."""
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError("dt must be finite and positive")
    current = sample_pose(pin, poses, sample_hz, time_s)
    following = sample_pose(pin, poses, sample_hz, time_s + dt)
    linear = (np.asarray(following.translation) - np.asarray(current.translation)) / dt
    angular = pin.log3(
        np.asarray(following.rotation) @ np.asarray(current.rotation).T
    ) / dt
    if not np.isfinite(linear).all() or not np.isfinite(angular).all():
        raise ValueError("FK trajectory produced a non-finite Cartesian twist")
    return np.asarray(linear), np.asarray(angular)


def ideal_timed_twist(
    pin: Any,
    poses: Sequence[Any],
    timeline: ReplayTimeline,
    time_s: float,
    dt: float,
) -> tuple[np.ndarray, np.ndarray]:
    current = sample_timed_pose(pin, poses, timeline, time_s)
    following = sample_timed_pose(pin, poses, timeline, time_s + dt)
    linear = (np.asarray(following.translation) - np.asarray(current.translation)) / dt
    angular = pin.log3(
        np.asarray(following.rotation) @ np.asarray(current.rotation).T
    ) / dt
    return np.asarray(linear), np.asarray(angular)


def velocity_limited_timeline(
    pin: Any,
    left_poses: Sequence[Any],
    right_poses: Sequence[Any],
    arm_actions: np.ndarray,
    *,
    source_hz: float,
    max_linear_m_s: float,
    max_angular_rad_s: float,
    max_joint_rad_s: float,
    utilization: float = 0.80,
) -> ReplayTimeline:
    """Retiming only: preserve every Cartesian waypoint while avoiding clipping.

    The utilization margin leaves room for the existing 150 ms input slew and
    the OTG.  It does not replace either layer and does not smooth the path.
    """
    arms = np.asarray(arm_actions, dtype=np.float64)
    if len(left_poses) != len(right_poses) or len(left_poses) != len(arms):
        raise ValueError("pose and arm trajectories must have matching lengths")
    if not 0.0 < utilization < 1.0:
        raise ValueError("utilization must be in (0, 1)")
    nominal = 1.0 / source_hz
    linear_limit = max_linear_m_s * utilization
    angular_limit = max_angular_rad_s * utilization
    joint_limit = max_joint_rad_s * utilization
    intervals: list[float] = []
    for index in range(len(arms) - 1):
        required = nominal
        for poses in (left_poses, right_poses):
            linear_distance = float(
                np.linalg.norm(poses[index + 1].translation - poses[index].translation)
            )
            angular_distance = float(
                np.linalg.norm(
                    pin.log3(
                        poses[index + 1].rotation @ poses[index].rotation.T
                    )
                )
            )
            required = max(
                required,
                linear_distance / linear_limit,
                angular_distance / angular_limit,
            )
        required = max(
            required,
            float(np.max(np.abs(arms[index + 1] - arms[index]))) / joint_limit,
        )
        intervals.append(required)
    return ReplayTimeline(
        np.concatenate(([0.0], np.cumsum(np.asarray(intervals, dtype=np.float64))))
    )


def normalized_twist_axes(
    linear_m_s: Sequence[float],
    angular_rad_s: Sequence[float],
    *,
    max_linear_m_s: float,
    max_angular_rad_s: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float], bool]:
    """Map an ideal twist to the same normalized axes consumed by F710."""
    linear = np.asarray(linear_m_s, dtype=np.float64)
    angular = np.asarray(angular_rad_s, dtype=np.float64)
    if linear.shape != (3,) or angular.shape != (3,):
        raise ValueError("linear and angular twists must be 3D")
    if max_linear_m_s <= 0.0 or max_angular_rad_s <= 0.0:
        raise ValueError("axis speed limits must be positive")
    linear_axes = linear / max_linear_m_s
    angular_axes = angular / max_angular_rad_s
    saturated = bool(np.linalg.norm(linear_axes) > 1.0 or np.linalg.norm(angular_axes) > 1.0)
    return tuple(linear_axes), tuple(angular_axes), saturated
