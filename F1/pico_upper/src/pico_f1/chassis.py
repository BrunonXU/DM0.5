"""PICO body-velocity mapping and jerk-limited F1 chassis commands."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np


# XRoboToolkit/OpenXR: +x right, +y up, +z backward. F1 base_link:
# +x forward, +y left, +z up. This is the arm mapping too.
R_XR_TO_F1 = np.array(
    [[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    dtype=float,
)


@dataclass(frozen=True)
class ChassisCommand:
    linear_x: float
    linear_y: float
    angular_z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return self.linear_x, self.linear_y, self.angular_z


@dataclass(frozen=True)
class ButtonDecision:
    chassis_enabled: bool = False
    torso_enabled: bool = False


class ABButtonArbiter:
    """Resolve A-hold torso and the higher-priority A+B chassis chord.

    Recording is intentionally absent: v1.3.4 assigns it exclusively to X/Y
    double-clicks, while B is reserved for the post-recording save decision.
    """

    def __init__(self, chord_window_s: float, torso_hold_s: float) -> None:
        if not math.isfinite(chord_window_s) or chord_window_s <= 0.0:
            raise ValueError("chord_window_s must be positive and finite")
        if not math.isfinite(torso_hold_s) or torso_hold_s <= chord_window_s:
            raise ValueError("torso_hold_s must be finite and greater than chord_window_s")
        self.chord_window_s = chord_window_s
        self.torso_hold_s = torso_hold_s
        self._state = "idle"
        self._pending_since = 0.0

    def update(self, a_pressed: bool, b_pressed: bool, now: float) -> ButtonDecision:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        a_pressed = bool(a_pressed)
        b_pressed = bool(b_pressed)

        if self._state == "chord":
            if a_pressed and b_pressed:
                return ButtonDecision(chassis_enabled=True)
            self._state = "lockout"
            return ButtonDecision()

        if self._state == "lockout":
            if not a_pressed and not b_pressed:
                self._state = "idle"
            return ButtonDecision()

        if self._state == "single_b_held":
            if a_pressed and b_pressed:
                self._state = "chord"
                return ButtonDecision(chassis_enabled=True)
            if not a_pressed and not b_pressed:
                self._state = "idle"
            return ButtonDecision()

        if self._state == "torso":
            if a_pressed and b_pressed:
                self._state = "chord"
                return ButtonDecision(chassis_enabled=True)
            if a_pressed:
                return ButtonDecision(torso_enabled=True)
            self._state = "lockout" if b_pressed else "idle"
            return ButtonDecision()

        if a_pressed and b_pressed:
            self._state = "chord"
            return ButtonDecision(chassis_enabled=True)

        if self._state == "idle":
            if a_pressed:
                self._state = "pending_a"
                self._pending_since = now
            elif b_pressed:
                self._state = "pending_b"
                self._pending_since = now
            return ButtonDecision()

        if self._state == "pending_a":
            if not a_pressed:
                self._state = "lockout" if b_pressed else "idle"
                return ButtonDecision()
            if now - self._pending_since >= self.torso_hold_s:
                self._state = "torso"
                return ButtonDecision(torso_enabled=True)
            return ButtonDecision()

        if self._state == "pending_b":
            if not b_pressed:
                self._state = "lockout" if a_pressed else "idle"
                return ButtonDecision()
            if now - self._pending_since >= self.chord_window_s:
                self._state = "single_b_held"
                return ButtonDecision()
            return ButtonDecision()

        # Defensive recovery from an impossible state.
        if not a_pressed and not b_pressed:
            self._state = "idle"
        return ButtonDecision()


def _quaternion_matrix_xyzw(quaternion: Iterable[float]) -> np.ndarray:
    q = np.asarray(list(quaternion), dtype=float)
    if q.shape != (4,) or not np.all(np.isfinite(q)):
        raise ValueError("quaternion must contain four finite values")
    norm = float(np.linalg.norm(q))
    if norm < 0.5 or norm > 1.5:
        raise ValueError("quaternion norm is invalid")
    x, y, z, w = q / norm
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=float,
    )


def _wrap_angle(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def _rotation_z(angle: float) -> np.ndarray:
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return np.array(
        [[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )


def pose_in_headset_heading_frame(
    device_pose: Iterable[float], headset_pose: Iterable[float]
) -> tuple[np.ndarray, np.ndarray]:
    """Express an XR device pose in the operator's continuously rotating frame.

    The returned position and rotation use F1 axes (+x forward, +y left,
    +z up).  Headset translation and yaw are removed, while headset pitch and
    roll are deliberately ignored so looking around cannot tilt the arm frame.
    """
    device = np.asarray(list(device_pose), dtype=float)
    headset = np.asarray(list(headset_pose), dtype=float)
    if device.shape != (7,) or headset.shape != (7,):
        raise ValueError("device and headset poses must each contain seven values")
    if not np.all(np.isfinite(device)) or not np.all(np.isfinite(headset)):
        raise ValueError("device and headset poses must be finite")

    device_rotation = (
        R_XR_TO_F1 @ _quaternion_matrix_xyzw(device[3:7]) @ R_XR_TO_F1.T
    )
    headset_rotation = (
        R_XR_TO_F1 @ _quaternion_matrix_xyzw(headset[3:7]) @ R_XR_TO_F1.T
    )
    headset_yaw = math.atan2(
        float(headset_rotation[1, 0]), float(headset_rotation[0, 0])
    )
    world_device = R_XR_TO_F1 @ device[:3]
    world_headset = R_XR_TO_F1 @ headset[:3]
    world_to_heading = _rotation_z(-headset_yaw)
    return (
        world_to_heading @ (world_device - world_headset),
        world_to_heading @ device_rotation,
    )


class JerkLimitedVelocity:
    """Low-pass velocity target followed by online Ruckig S-curve planning."""

    def __init__(
        self,
        dt: float,
        max_velocity: Iterable[float],
        max_acceleration: Iterable[float],
        max_jerk: Iterable[float],
        cutoff_hz: float,
        deadband: Iterable[float],
        neutral_acceleration: Iterable[float] | None = None,
        neutral_jerk: Iterable[float] | None = None,
        neutral_stop_mode: str = "profiled",
    ) -> None:
        self.dt = float(dt)
        if neutral_stop_mode not in {"profiled", "zero_target"}:
            raise ValueError("neutral_stop_mode must be profiled or zero_target")
        self.neutral_stop_mode = neutral_stop_mode
        self.max_velocity = self._vector(max_velocity, "max_velocity")
        self.max_acceleration = self._vector(max_acceleration, "max_acceleration")
        self.max_jerk = self._vector(max_jerk, "max_jerk")
        self.deadband = self._vector(deadband, "deadband", allow_zero=True)
        if (neutral_acceleration is None) != (neutral_jerk is None):
            raise ValueError("neutral acceleration and jerk must be provided together")
        self.neutral_acceleration = (
            None if neutral_acceleration is None else self._vector(neutral_acceleration, "neutral_acceleration")
        )
        self.neutral_jerk = None if neutral_jerk is None else self._vector(neutral_jerk, "neutral_jerk")
        if not math.isfinite(self.dt) or self.dt <= 0.0:
            raise ValueError("dt must be positive and finite")
        if not math.isfinite(cutoff_hz) or cutoff_hz <= 0.0:
            raise ValueError("cutoff_hz must be positive and finite")
        self.cutoff_hz = float(cutoff_hz)
        self.alpha = 1.0 - math.exp(-2.0 * math.pi * self.cutoff_hz * self.dt)
        try:
            import ruckig
        except ImportError as exc:
            raise RuntimeError("ruckig is required for chassis trajectory planning") from exc
        self._ruckig = ruckig
        self.position = [0.0, 0.0, 0.0]
        self.velocity = [0.0, 0.0, 0.0]
        self.acceleration = [0.0, 0.0, 0.0]
        self.filtered_target = [0.0, 0.0, 0.0]

    @staticmethod
    def _vector(values: Iterable[float], name: str, allow_zero: bool = False) -> list[float]:
        vector = [float(value) for value in values]
        lower_ok = (lambda value: value >= 0.0) if allow_zero else (lambda value: value > 0.0)
        if len(vector) != 3 or any(not math.isfinite(v) or not lower_ok(v) for v in vector):
            raise ValueError(f"{name} must contain three valid finite values")
        return vector

    def reset(self) -> None:
        self.position = [0.0, 0.0, 0.0]
        self.velocity = [0.0, 0.0, 0.0]
        self.acceleration = [0.0, 0.0, 0.0]
        self.filtered_target = [0.0, 0.0, 0.0]

    def set_period(self, dt: float) -> None:
        """Use elapsed command time, not an assumed 50 Hz call count.

        The runtime stops on longer scheduling gaps instead of fast-forwarding
        a stale trajectory. This method never resets velocity/acceleration.
        """
        dt = float(dt)
        if not math.isfinite(dt) or not 0.0 < dt <= 0.1:
            raise ValueError("chassis elapsed period must be in (0, 0.1] seconds")
        self.dt = dt
        self.alpha = 1.0 - math.exp(-2.0 * math.pi * self.cutoff_hz * dt)

    def update(self, target: Iterable[float], *, immediate_target: bool = False) -> ChassisCommand:
        target_vector = [float(value) for value in target]
        if len(target_vector) != 3 or any(not math.isfinite(v) for v in target_vector):
            raise ValueError("target must contain three finite values")
        for index, value in enumerate(target_vector):
            value = float(np.clip(value, -self.max_velocity[index], self.max_velocity[index]))
            if abs(value) < self.deadband[index]:
                value = 0.0
            # Stick release must target exact zero now, not an exponential tail.
            # Keep the current velocity/acceleration: Ruckig brakes continuously.
            if immediate_target or (self.neutral_acceleration is not None and value == 0.0):
                self.filtered_target[index] = value
            else:
                self.filtered_target[index] += self.alpha * (value - self.filtered_target[index])
            if value == 0.0 and self.neutral_stop_mode == "zero_target":
                # A stop request to the supplier velocity controller must not
                # be followed by another .3s of nonzero setpoints. This is a
                # ZERO VELOCITY TARGET, not a torque/brake command. Its edge
                # intentionally is not software jerk-limited: physical braking
                # belongs to the supplier and requires attended verification.
                # Per-axis only; a released yaw must not reset active x/y.
                self.filtered_target[index] = 0.0
                self.velocity[index] = 0.0
                self.acceleration[index] = 0.0

        inp = self._ruckig.InputParameter(3)
        out = self._ruckig.OutputParameter(3)
        inp.control_interface = self._ruckig.ControlInterface.Velocity
        inp.current_position = self.position
        inp.current_velocity = self.velocity
        inp.current_acceleration = self.acceleration
        inp.target_velocity = self.filtered_target
        inp.target_acceleration = [0.0, 0.0, 0.0]
        inp.max_acceleration = self.max_acceleration
        inp.max_jerk = self.max_jerk
        if self.neutral_acceleration is not None:
            # One released axis must not wait for a still-commanded axis to ramp.
            inp.synchronization = self._ruckig.Synchronization.No
            neutral = [value == 0.0 for value in self.filtered_target]
            inp.max_acceleration = [
                self.neutral_acceleration[i] if neutral[i] else self.max_acceleration[i]
                for i in range(3)
            ]
            inp.max_jerk = [
                self.neutral_jerk[i] if neutral[i] else self.max_jerk[i]
                for i in range(3)
            ]
        result = self._ruckig.Ruckig(3, self.dt).update(inp, out)
        if int(result) < 0:
            raise RuntimeError(f"Ruckig chassis velocity update failed: {result}")
        self.position = list(out.new_position)
        self.velocity = list(out.new_velocity)
        self.acceleration = list(out.new_acceleration)
        if (max(abs(v) for v in self.filtered_target) == 0.0
                and max(abs(v) for v in self.velocity) < 1e-8
                and max(abs(a) for a in self.acceleration) < 1e-8):
            self.reset()
        return ChassisCommand(*self.velocity)


class JoystickChassisController:
    """Map both PICO sticks to a bounded, jerk-limited base command."""

    def __init__(self, config: dict, dt: float) -> None:
        self.config = config
        self.deadzone = float(config["joystick_deadzone"])
        if not 0.0 <= self.deadzone < 1.0:
            raise ValueError("joystick_deadzone must be within [0, 1)")
        self.trajectory = JerkLimitedVelocity(
            dt=dt,
            max_velocity=[
                config["joystick_max_forward_speed_m_s"],
                config["joystick_max_lateral_speed_m_s"],
                config["joystick_max_yaw_speed_rad_s"],
            ],
            max_acceleration=[
                config["max_linear_acceleration_m_s2"],
                config["max_linear_acceleration_m_s2"],
                config["max_yaw_acceleration_rad_s2"],
            ],
            max_jerk=[
                config["max_linear_jerk_m_s3"],
                config["max_linear_jerk_m_s3"],
                config["max_yaw_jerk_rad_s3"],
            ],
            cutoff_hz=config["joystick_input_cutoff_hz"],
            neutral_stop_mode=config.get("neutral_stop_mode", "profiled"),
            deadband=[0.0, 0.0, 0.0],
            neutral_acceleration=[
                config["joystick_brake_linear_acceleration_m_s2"],
                config["joystick_brake_linear_acceleration_m_s2"],
                config["joystick_brake_yaw_acceleration_rad_s2"],
            ],
            neutral_jerk=[
                config["joystick_brake_linear_jerk_m_s3"],
                config["joystick_brake_linear_jerk_m_s3"],
                config["joystick_brake_yaw_jerk_rad_s3"],
            ],
        )

    def _radial_deadzone(self, axes: Iterable[float]) -> np.ndarray:
        value = np.asarray(list(axes), dtype=float)
        if value.shape != (2,) or not np.all(np.isfinite(value)):
            raise ValueError("joystick axes must contain two finite values")
        magnitude = float(np.linalg.norm(value))
        if magnitude <= self.deadzone:
            return np.zeros(2, dtype=float)
        if magnitude > 1.0:
            value /= magnitude
            magnitude = 1.0
        return value * ((magnitude - self.deadzone) / ((1.0 - self.deadzone) * magnitude))

    def update(
        self,
        left_axes: Iterable[float],
        right_axes: Iterable[float],
    ) -> ChassisCommand:
        left = self._radial_deadzone(left_axes)
        right = self._radial_deadzone(right_axes)
        target = [
            float(left[1]) * float(self.config["joystick_max_forward_speed_m_s"]),
            -float(left[0]) * float(self.config["joystick_max_lateral_speed_m_s"]),
            -float(right[0]) * float(self.config["joystick_max_yaw_speed_rad_s"]),
        ]
        return self.trajectory.update(target, immediate_target=bool(self.config["immediate_response"]))

    @property
    def active(self) -> bool:
        return max(
            *(abs(value) for value in self.trajectory.filtered_target),
            *(abs(value) for value in self.trajectory.velocity),
        ) > 1e-5

    def emergency_stop(self) -> ChassisCommand:
        self.trajectory.reset()
        return ChassisCommand(0.0, 0.0, 0.0)


class BodyVelocityFollower:
    """Human velocity following, with opt-in legacy position catch-up.

    Velocity mode prioritizes prompt stopping over exact traveled distance.
    Position mode retains the 1:1 pose target and can continue after human stop.
    Both use validated odometry for heading and bounded S-curve commands.
    """

    def __init__(self, config: dict, dt: float) -> None:
        self.config = config
        self.enabled = False
        self._cached_linear_velocity = np.zeros(2, dtype=float)
        self._cached_yaw_velocity = 0.0
        self._human_reference: np.ndarray | None = None
        self._robot_reference: np.ndarray | None = None
        self._robot_pose: np.ndarray | None = None
        self._robot_raw_yaw: float | None = None
        self._robot_yaw_unwrapped: float | None = None
        self._robot_velocity = np.zeros(3, dtype=float)
        self._robot_timestamp = 0.0
        self._human_raw_yaw: float | None = None
        self._human_yaw_unwrapped: float | None = None
        self._last_pose: np.ndarray | None = None
        self._last_pose_yaw: float | None = None
        self._last_pose_timestamp_ns: int | None = None
        self.last_error = np.zeros(3, dtype=float)
        self.trajectory = JerkLimitedVelocity(
            dt=dt,
            max_velocity=[
                config["max_forward_speed_m_s"], config["max_lateral_speed_m_s"],
                config["max_yaw_speed_rad_s"],
            ],
            max_acceleration=[
                config["max_linear_acceleration_m_s2"], config["max_linear_acceleration_m_s2"],
                config["max_yaw_acceleration_rad_s2"],
            ],
            max_jerk=[
                config["max_linear_jerk_m_s3"], config["max_linear_jerk_m_s3"],
                config["max_yaw_jerk_rad_s3"],
            ],
            cutoff_hz=config["input_cutoff_hz"],
            neutral_stop_mode=config.get("neutral_stop_mode", "profiled"),
            deadband=[
                config["linear_deadband_m_s"], config["linear_deadband_m_s"],
                config["yaw_deadband_rad_s"],
            ],
            neutral_acceleration=[config["joystick_brake_linear_acceleration_m_s2"]] * 2
            + [config["joystick_brake_yaw_acceleration_rad_s2"]],
            neutral_jerk=[config["joystick_brake_linear_jerk_m_s3"]] * 2
            + [config["joystick_brake_yaw_jerk_rad_s3"]],
        )

    @staticmethod
    def _body_yaw(pose: np.ndarray) -> float:
        xr_rotation = _quaternion_matrix_xyzw(pose[3:7])
        f1_rotation = R_XR_TO_F1 @ xr_rotation @ R_XR_TO_F1.T
        return math.atan2(float(f1_rotation[1, 0]), float(f1_rotation[0, 0]))

    @classmethod
    def _human_planar_pose(cls, pose: np.ndarray) -> np.ndarray:
        world_position = R_XR_TO_F1 @ pose[:3]
        return np.array(
            [float(world_position[0]), float(world_position[1]), cls._body_yaw(pose)],
            dtype=float,
        )

    def set_robot_odometry(
        self,
        pose: Iterable[float],
        velocity: Iterable[float],
        timestamp: float,
    ) -> None:
        """Update actual F1 planar pose [x, y, yaw] and base-frame velocity."""
        pose_vector = np.asarray(list(pose), dtype=float)
        velocity_vector = np.asarray(list(velocity), dtype=float)
        timestamp = float(timestamp)
        if pose_vector.shape != (3,) or velocity_vector.shape != (3,):
            raise ValueError("robot pose and velocity must each contain three values")
        if not np.all(np.isfinite(pose_vector)) or not np.all(np.isfinite(velocity_vector)):
            raise ValueError("robot odometry must be finite")
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            raise ValueError("robot odometry timestamp must be positive and finite")
        raw_yaw = _wrap_angle(float(pose_vector[2]))
        yaw_step = 0.0
        if self._robot_raw_yaw is None or self._robot_yaw_unwrapped is None:
            unwrapped_yaw = raw_yaw
        else:
            yaw_step = _wrap_angle(raw_yaw - self._robot_raw_yaw)
            unwrapped_yaw = self._robot_yaw_unwrapped + yaw_step
        if self.enabled and self._robot_pose is not None:
            odometry_step = float(np.linalg.norm(pose_vector[:2] - self._robot_pose[:2]))
            if odometry_step > float(self.config["odometry_jump_m"]):
                raise ValueError(f"F1 odometry position discontinuity {odometry_step:.3f}m")
            if abs(yaw_step) > float(self.config["odometry_yaw_jump_rad"]):
                raise ValueError(f"F1 odometry yaw discontinuity {abs(yaw_step):.3f}rad")
        pose_vector[2] = unwrapped_yaw
        self._robot_pose = pose_vector
        self._robot_raw_yaw = raw_yaw
        self._robot_yaw_unwrapped = unwrapped_yaw
        self._robot_velocity = velocity_vector
        self._robot_timestamp = timestamp

    @staticmethod
    def _minimum_planar_speed(
        target: np.ndarray, minimum: float, error_norm: float, deadband: float
    ) -> np.ndarray:
        speed = float(np.linalg.norm(target))
        if error_norm > deadband and 1e-12 < speed < minimum:
            return target * (minimum / speed)
        return target

    def _track(
        self,
        pose_vector: np.ndarray,
        linear_velocity_world: np.ndarray,
        yaw_velocity: float,
        *, repeated: bool = False,
    ) -> ChassisCommand:
        if self._robot_pose is None:
            raise ValueError("F1 odometry pose is unavailable")
        velocity_mode = self.config["body_follow_mode"] == "velocity"
        if repeated and velocity_mode:
            # A velocity command is a rate, not a displacement increment. Hold
            # the last validated rate between XR samples (outer freshness gate
            # still stops a stale stream); never differentiate/reintegrate dt=0.
            linear_velocity_world = self._cached_linear_velocity
            yaw_velocity = self._cached_yaw_velocity
        elif not repeated:
            self._cached_linear_velocity = linear_velocity_world.copy()
            self._cached_yaw_velocity = yaw_velocity

        human_pose = self._human_planar_pose(pose_vector)
        raw_human_yaw = float(human_pose[2])
        if self._human_raw_yaw is None or self._human_yaw_unwrapped is None:
            self._human_yaw_unwrapped = raw_human_yaw
        else:
            self._human_yaw_unwrapped += _wrap_angle(
                raw_human_yaw - self._human_raw_yaw
            )
        self._human_raw_yaw = raw_human_yaw
        human_pose[2] = self._human_yaw_unwrapped
        if not self.enabled or self._human_reference is None or self._robot_reference is None:
            self.enabled = True
            self._human_reference = human_pose.copy()
            self._robot_reference = self._robot_pose.copy()
            self.last_error[:] = 0.0
            self.trajectory.reset()
            return ChassisCommand(0.0, 0.0, 0.0)

        alignment_yaw = _wrap_angle(
            float(self._robot_reference[2] - self._human_reference[2])
        )
        human_delta_world = human_pose[:2] - self._human_reference[:2]
        desired_delta_world = (
            _rotation_z(alignment_yaw)[:2, :2]
            @ human_delta_world
            * float(self.config["linear_position_scale"])
        )
        desired_world = self._robot_reference[:2] + desired_delta_world
        desired_yaw = (
            float(self._robot_reference[2])
            + float(self.config["yaw_position_scale"])
            * float(human_pose[2] - self._human_reference[2])
        )

        robot_yaw = _wrap_angle(float(self._robot_pose[2]))
        world_error = desired_world - self._robot_pose[:2]
        world_to_robot = _rotation_z(-robot_yaw)[:2, :2]
        local_error = world_to_robot @ world_error
        yaw_error = desired_yaw - float(self._robot_pose[2])
        self.last_error = np.array([local_error[0], local_error[1], yaw_error])

        position_deadband = float(self.config["position_deadband_m"])
        yaw_deadband = float(self.config["yaw_position_deadband_rad"])
        error_norm = float(np.linalg.norm(local_error))
        correction = np.zeros(2, dtype=float)
        if not velocity_mode and error_norm > position_deadband:
            correction = float(self.config["position_gain_s_inv"]) * local_error

        aligned_human_velocity = (
            _rotation_z(alignment_yaw)[:2, :2] @ linear_velocity_world[:2]
        )
        feedforward = (
            world_to_robot
            @ aligned_human_velocity
            * float(self.config["linear_velocity_scale"])
        )
        linear_target = correction + feedforward
        linear_target = linear_target if velocity_mode else self._minimum_planar_speed(
            linear_target,
            float(self.config["min_linear_speed_m_s"]),
            error_norm,
            position_deadband,
        )

        yaw_target = float(self.config["yaw_velocity_scale"]) * float(yaw_velocity)
        if not velocity_mode and abs(yaw_error) > yaw_deadband:
            yaw_target += float(self.config["yaw_position_gain_s_inv"]) * yaw_error
            if 0.0 < abs(yaw_target) < float(self.config["min_yaw_speed_rad_s"]):
                yaw_target = math.copysign(
                    float(self.config["min_yaw_speed_rad_s"]), yaw_target
                )

        return self.trajectory.update(
            [float(linear_target[0]), float(linear_target[1]), yaw_target],
            immediate_target=bool(self.config["immediate_response"]),
        )

    def update(
        self,
        pose: Iterable[float] | None,
        velocity: Iterable[float] | None,
        enabled: bool,
        timestamp_ns: int | None = None,
    ) -> ChassisCommand:
        if not enabled:
            # A+B is a deadman switch.  Its release must never leave a
            # non-zero software command in flight, even though ordinary body
            # input is smoothed while the chord remains held.
            return self.emergency_stop()

        pose_vector = np.asarray(list(pose) if pose is not None else [], dtype=float)
        velocity_vector = np.asarray(list(velocity) if velocity is not None else [], dtype=float)
        if (
            pose_vector.shape != (7,) or velocity_vector.shape != (6,)
            or not np.all(np.isfinite(pose_vector)) or not np.all(np.isfinite(velocity_vector))
        ):
            raise ValueError("body pose/velocity sample has invalid shape or values")

        yaw = self._body_yaw(pose_vector)
        derived_velocity = None
        if timestamp_ns is not None:
            timestamp_ns = int(timestamp_ns)
            if timestamp_ns <= 0:
                raise ValueError("body timestamp is invalid")
            if self._last_pose is not None and self._last_pose_timestamp_ns is not None:
                if timestamp_ns == self._last_pose_timestamp_ns:
                    # The ROS command clock can run faster than XR delivery.
                    # Hold the cached rate in velocity mode, or the anchored
                    # target in position mode; never accept a torn new pose.
                    # The caller must still enforce wall-clock input freshness.
                    return self._track(self._last_pose, np.zeros(2), 0.0, repeated=True)
                dt = (timestamp_ns - self._last_pose_timestamp_ns) * 1e-9
                if dt <= 1e-4 or dt > float(self.config["input_timeout_s"]):
                    raise ValueError(f"body timestamp discontinuity dt={dt:.6f}s")
                position_delta = pose_vector[:3] - self._last_pose[:3]
                yaw_delta = _wrap_angle(yaw - float(self._last_pose_yaw))
                if float(np.linalg.norm(position_delta)) > 0.25 or abs(yaw_delta) > 0.5:
                    raise ValueError("body pose discontinuity")
                # Some Body SDK builds return zero velocity despite moving
                # poses. Use the same validated finite difference as headset.
                derived_velocity = (R_XR_TO_F1 @ (position_delta / dt), yaw_delta / dt)
            self._last_pose = pose_vector.copy()
            self._last_pose_yaw = yaw
            self._last_pose_timestamp_ns = timestamp_ns

        world_linear = R_XR_TO_F1 @ velocity_vector[:3]
        world_angular = R_XR_TO_F1 @ velocity_vector[3:6]
        if derived_velocity is not None:
            world_linear, yaw_velocity = derived_velocity
        else:
            yaw_velocity = float(world_angular[2])
        return self._track(pose_vector, world_linear[:2], yaw_velocity)

    def update_pose(
        self, pose: Iterable[float] | None, timestamp_ns: int, enabled: bool
    ) -> ChassisCommand:
        """Derive planar velocity from a tracked pose when Body data is absent."""
        if not enabled:
            return self.emergency_stop()
        pose_vector = np.asarray(list(pose) if pose is not None else [], dtype=float)
        if pose_vector.shape != (7,) or not np.all(np.isfinite(pose_vector)):
            raise ValueError("headset pose sample has invalid shape or values")
        # Also validates and normalizes the quaternion.
        yaw = self._body_yaw(pose_vector)
        timestamp_ns = int(timestamp_ns)
        if timestamp_ns <= 0:
            raise ValueError("headset timestamp is invalid")
        if (
            not self.enabled
            or self._last_pose is None
            or self._last_pose_yaw is None
            or self._last_pose_timestamp_ns is None
        ):
            self._last_pose = pose_vector.copy()
            self._last_pose_yaw = yaw
            self._last_pose_timestamp_ns = timestamp_ns
            return self._track(pose_vector, np.zeros(2), 0.0)

        if timestamp_ns == self._last_pose_timestamp_ns:
            # A repeated, still-fresh XR frame is sample-and-hold, not a
            # tracking reset. Hold the cached rate/position-mode target;
            # never differentiate by zero or integrate a delta twice.
            return self._track(self._last_pose, np.zeros(2), 0.0, repeated=True)
        dt = (timestamp_ns - self._last_pose_timestamp_ns) * 1e-9
        if dt <= 1e-4 or dt > float(self.config["input_timeout_s"]):
            raise ValueError(f"headset timestamp discontinuity dt={dt:.6f}s")
        position_delta = pose_vector[:3] - self._last_pose[:3]
        yaw_delta = _wrap_angle(yaw - self._last_pose_yaw)
        # Reject tracking recenter/teleport instead of turning it into a
        # saturated robot command.
        if float(np.linalg.norm(position_delta)) > 0.25 or abs(yaw_delta) > 0.5:
            raise ValueError("headset pose discontinuity")
        world_linear = R_XR_TO_F1 @ (position_delta / dt)
        self._last_pose = pose_vector.copy()
        self._last_pose_yaw = yaw
        self._last_pose_timestamp_ns = timestamp_ns
        return self._track(pose_vector, world_linear[:2], yaw_delta / dt)

    def recenter(self) -> ChassisCommand:
        """Forget the accumulated human target and anchor at the next sample."""
        self.enabled = False
        self._human_reference = None
        self._robot_reference = None
        self._human_raw_yaw = None
        self._human_yaw_unwrapped = None
        self._last_pose = None
        self._last_pose_yaw = None
        self._last_pose_timestamp_ns = None
        self._cached_linear_velocity[:] = 0.0
        self._cached_yaw_velocity = 0.0
        self.last_error[:] = 0.0
        self.trajectory.reset()
        return ChassisCommand(0.0, 0.0, 0.0)

    def emergency_stop(self) -> ChassisCommand:
        return self.recenter()
