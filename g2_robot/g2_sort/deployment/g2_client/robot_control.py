"""G2严格状态读取与显式硬件下发模式。

用途：向上层提供唯一的34D``read_state``和``send_action``接口。
输入：GDK Robot、34D absolute joint-position、control period、通道和输出模式。
输出：严格measured state或经过计时的Servo结果；shadow禁止发送。
方法：默认Split保留旧14D Servo + O10 move_ee_pos；连续手轨迹候选把34D
重排成左右各17D动态名称Servo；统一34D和仅双臂只保留为显式诊断模式。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import ACTION_DIM, MODEL_HZ, RobotControlError
from .hand_command_store import (
    build_hand_command_record,
    save_hand_command_record,
)
from .joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    POLICY_JOINT_LIMITS,
    POLICY_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
    clip_action_positions,
)
from .interpolation import sample_linear_action
from .joint_feedback import JointFeedbackWriter
from .robot_state import StrictRobotStateReader
from .telemetry import DispatchTelemetry
from .trajectory_smoothing import (
    CausalHandBoundaryCapTracker,
    CausalHandCommandTracker,
    CausalHandTrajectoryPlanner,
    HAND_V11_MAX_ACCELERATION_RAD_S2,
    HAND_V11_MAX_DELTA_RAD,
    HAND_V11_MAX_VELOCITY_RAD_S,
    HandTrackingDecision,
    HandTrajectoryConfig,
    HandTrajectoryDecision,
    SHARED_PREVIEW_ACCELERATION_PENALTY,
    SHARED_PREVIEW_DECAY,
    SHARED_PREVIEW_HORIZON_STEPS,
    SHARED_PREVIEW_JERK_PENALTY,
    SHARED_PREVIEW_MAX_ACCELERATION_RAD_S2,
    SHARED_PREVIEW_MAX_JERK_RAD_S3,
    SHARED_PREVIEW_MAX_VELOCITY_RAD_S,
)

logger = logging.getLogger(__name__)

G2_SERVO_STATUS_MODE = 5
EXPECTED_END_EFFECTOR_MODEL = "o10_t2"
MAX_LOW_LATENCY_STEP_RAD = 0.03
ARM_ACTION_DIM = 14
DEFAULT_HAND_STREAM_HZ = 5.0
MAX_HAND_STREAM_HZ = 50.0
OUTPUT_MODES = {"unified-34", "arms-only", "split", "dual-side-17d"}


class G2RobotController:
    """Expose strict 34D reads while making hardware dispatch explicit."""

    def __init__(
        self,
        *,
        powered: bool,
        control_period: float = 0.01,
        servo_channel: str = "normal",
        servo_output_mode: str = "split",
        stabilization_seconds: float = 2.0,
        max_send_seconds: float = 0.03,
        timing_warning_callback: Any | None = None,
        enforce_low_latency_step_limit: bool = True,
        last_hand_command_path: Path | None = None,
        hand_command_run_id: str = "runtime",
        hand_startup_delay_seconds: float = 0.08,
        hand_stream_hz: float = DEFAULT_HAND_STREAM_HZ,
        hand_smoothing_profile: str = "off",
        hand_boundary_blend_seconds: float = 0.4,
        hand_max_velocity_rad_s: float = 2.0,
        hand_max_acceleration_rad_s2: float = 10.0,
        hand_max_delta_radians: float = 0.4,
        hand_boundary_max_delta_radians: float = 0.15,
        hand_reversal_error_radians: float = 0.15,
        split_hand_output_enabled: bool = True,
        dispatch_telemetry: DispatchTelemetry | None = None,
        joint_feedback_writer: JointFeedbackWriter | None = None,
        joint_feedback_hz: float = 0.0,
        gdk_module: Any | None = None,
        robot: Any | None = None,
    ) -> None:
        if control_period <= 0:
            raise ValueError("servo control period must be positive")
        if servo_channel not in {"normal", "low-latency"}:
            raise ValueError(f"unsupported servo channel {servo_channel!r}")
        if servo_output_mode not in OUTPUT_MODES:
            raise ValueError(f"unsupported servo output mode {servo_output_mode!r}")
        if max_send_seconds <= 0:
            raise ValueError("max_send_seconds must be positive")
        if not 0.0 <= hand_startup_delay_seconds <= 1.0:
            raise ValueError("hand_startup_delay_seconds must be in [0, 1]")
        if not (
            np.isfinite(hand_stream_hz)
            and 0 < hand_stream_hz <= MAX_HAND_STREAM_HZ
        ):
            raise ValueError(
                f"hand_stream_hz must be finite and in (0, {MAX_HAND_STREAM_HZ:g}]"
            )
        if hand_smoothing_profile not in {
            "off", "v1", "v1_1", "rtc_cap_v2", "shared_preview_v1"
        }:
            raise ValueError(
                "hand_smoothing_profile must be off, v1, v1_1, "
                "rtc_cap_v2, or shared_preview_v1"
            )
        if gdk_module is None:
            try:
                import agibot_gdk as gdk_module
            except ImportError as exc:  # pragma: no cover - robot-only dependency
                raise RobotControlError(
                    "agibot_gdk is required for real G2 hardware"
                ) from exc
        self._gdk = gdk_module
        self._robot = robot if robot is not None else gdk_module.Robot()
        self._reader = StrictRobotStateReader(self._robot)
        self._powered = bool(powered)
        self._control_period = float(control_period)
        self._servo_channel = servo_channel
        self._servo_output_mode = servo_output_mode
        if timing_warning_callback is not None and (servo_output_mode != "unified-34" or servo_channel != "normal"):
            raise ValueError("timing warning-only requires unified-34 normal")
        self._timing_warning_callback = timing_warning_callback
        self._max_send_seconds = float(max_send_seconds)
        self._enforce_low_latency_step_limit = bool(
            enforce_low_latency_step_limit
        )
        self._last_commanded_target: np.ndarray | None = None
        self._emergency_stop = threading.Event()
        self._last_clip_count = 0
        self._last_clip_max_delta = 0.0
        self._last_send_seconds = 0.0
        self._last_collision_pairs: list[tuple[str, str]] = []
        self._servo_request_count = 0
        self._servo_requests_all_complete = True
        self._last_servo_joint_count = 0
        self._last_servo_position_count = 0
        self._last_servo_velocity_count = 0
        self._dual_side_servo_layouts: tuple[
            tuple[tuple[str, ...], tuple[int, ...]],
            tuple[tuple[str, ...], tuple[int, ...]],
        ] | None = None
        self._hand_target_lock = threading.Lock()
        self._hand_target: np.ndarray | None = None
        self._hand_reference_actions: np.ndarray | None = None
        self._hand_reference_start_index = 0
        self._hand_reference_interval_count = 0
        self._hand_reference_anchor: np.ndarray | None = None
        self._hand_reference_blend_seconds = 0.0
        self._hand_reference_blend_profile = "linear"
        self._hand_target_event = threading.Event()
        self._hand_output_enabled = threading.Event()
        if split_hand_output_enabled:
            self._hand_output_enabled.set()
        self._hand_stop_event = threading.Event()
        self._hand_fault: RobotControlError | None = None
        self._hand_send_count = 0
        self._hand_unchanged_skip_count = 0
        self._last_acknowledged_hand_command: np.ndarray | None = None
        self._last_hand_send_seconds = 0.0
        self._last_hand_command_path = last_hand_command_path
        self._hand_command_run_id = hand_command_run_id
        self._hand_startup_delay_seconds = float(hand_startup_delay_seconds)
        self._hand_stream_hz = float(hand_stream_hz)
        self._hand_smoothing_profile = hand_smoothing_profile
        self._hand_trajectory_planner: CausalHandTrajectoryPlanner | None = None
        if hand_smoothing_profile == "shared_preview_v1":
            self._hand_trajectory_planner = CausalHandTrajectoryPlanner(
                HandTrajectoryConfig(
                    strategy="preview_qp",
                    nominal_hz=self._hand_stream_hz,
                    horizon_steps=SHARED_PREVIEW_HORIZON_STEPS,
                    max_velocity_rad_s=SHARED_PREVIEW_MAX_VELOCITY_RAD_S,
                    max_acceleration_rad_s2=(
                        SHARED_PREVIEW_MAX_ACCELERATION_RAD_S2
                    ),
                    max_jerk_rad_s3=SHARED_PREVIEW_MAX_JERK_RAD_S3,
                    acceleration_penalty=(
                        SHARED_PREVIEW_ACCELERATION_PENALTY
                    ),
                    jerk_penalty=SHARED_PREVIEW_JERK_PENALTY,
                    preview_decay=SHARED_PREVIEW_DECAY,
                    enforce_derivative_limits=False,
                    position_lower_rad=tuple(
                        POLICY_JOINT_LIMITS[ARM_ACTION_DIM:, 0]
                    ),
                    position_upper_rad=tuple(
                        POLICY_JOINT_LIMITS[ARM_ACTION_DIM:, 1]
                    ),
                )
            )
            self._hand_tracker = None
        elif hand_smoothing_profile == "rtc_cap_v2":
            self._hand_tracker = CausalHandBoundaryCapTracker(
                boundary_max_delta_rad=hand_boundary_max_delta_radians,
                internal_max_delta_rad=hand_max_delta_radians,
            )
        else:
            if hand_smoothing_profile == "v1_1":
                hand_max_velocity_rad_s = HAND_V11_MAX_VELOCITY_RAD_S
                hand_max_acceleration_rad_s2 = HAND_V11_MAX_ACCELERATION_RAD_S2
                hand_max_delta_radians = HAND_V11_MAX_DELTA_RAD
            self._hand_tracker = CausalHandCommandTracker(
                enabled=hand_smoothing_profile in {"v1", "v1_1"},
                boundary_blend_seconds=hand_boundary_blend_seconds,
                max_velocity_rad_s=hand_max_velocity_rad_s,
                max_acceleration_rad_s2=hand_max_acceleration_rad_s2,
                max_delta_rad=hand_max_delta_radians,
                reversal_error_rad=hand_reversal_error_radians,
                reversal_boundary_only=hand_smoothing_profile == "v1_1",
                nominal_hz=self._hand_stream_hz,
            )
        self._hand_segment_generation = 0
        self._hand_segment_started_at = time.monotonic()
        self._dispatch_telemetry = dispatch_telemetry
        self._joint_feedback_writer = joint_feedback_writer
        self._joint_feedback_hz = float(joint_feedback_hz)
        if self._joint_feedback_hz not in {0.0, 10.0, 20.0}:
            raise ValueError("joint_feedback_hz must be 0, 10, or 20")
        self._joint_feedback_enabled = bool(
            self._powered
            and self._joint_feedback_writer is not None
            and self._joint_feedback_hz > 0
        )
        self._joint_feedback_next_at = time.monotonic()
        self._last_arm_command_at: float | None = None
        self._long_arm_interval_streak = 0
        self._hand_thread: threading.Thread | None = None
        if stabilization_seconds > 0:
            time.sleep(stabilization_seconds)
        if self._powered:
            self._validate_end_effectors()
            self._require_g2_servo_mode()
            if self._servo_channel == "low-latency":
                logger.warning(
                    "LOW-LATENCY SERVO ENABLED: GDK 2.6.3 provides no "
                    "collision protection on this channel"
                )
            if self._servo_output_mode == "split":
                self._require_move_ee_pos()
                self._hand_thread = threading.Thread(
                    target=self._hand_stream_loop,
                    name=f"g2-o10-move-ee-{self._hand_stream_hz:g}hz",
                    daemon=True,
                )
                self._hand_thread.start()
            elif self._servo_output_mode == "dual-side-17d":
                self._dual_side_servo_layouts = self._read_dual_side_servo_layouts()

    def read_state(self) -> np.ndarray:
        return self._reader.read()

    def begin_hand_segment(
        self,
        *,
        started_at: float,
        reference_actions: np.ndarray | None = None,
        start_index: int = 0,
        interval_count: int | None = None,
        anchor: np.ndarray | None = None,
        blend_seconds: float = 0.0,
        blend_profile: str = "linear",
    ) -> None:
        """Mark a policy boundary; the hand worker owns tracker state changes."""
        if not np.isfinite(started_at):
            raise ValueError("hand segment start time must be finite")
        actions: np.ndarray | None = None
        anchor_hand: np.ndarray | None = None
        intervals = 0
        if reference_actions is not None:
            actions = np.asarray(reference_actions, dtype=np.float64)
            if (
                actions.ndim != 2
                or actions.shape[1] != ACTION_DIM
                or not np.all(np.isfinite(actions))
            ):
                raise ValueError("hand preview reference must be finite Nx34")
            intervals = (
                len(actions) - start_index
                if interval_count is None
                else int(interval_count)
            )
            if (
                start_index < 0
                or start_index >= len(actions)
                or intervals <= 0
                or intervals > len(actions) - start_index
            ):
                raise ValueError("hand preview range is outside the action chunk")
            if anchor is not None:
                anchor_array = np.asarray(anchor, dtype=np.float64)
                if (
                    anchor_array.shape != (ACTION_DIM,)
                    or not np.all(np.isfinite(anchor_array))
                ):
                    raise ValueError("hand preview anchor must be finite 34D")
                anchor_hand = anchor_array[ARM_ACTION_DIM:].copy()
        if blend_profile not in {
            "linear", "cubic_smoothstep", "quintic_smootherstep"
        }:
            raise ValueError("unsupported hand preview blend profile")
        if self._hand_trajectory_planner is not None and (
            actions is None or anchor_hand is None
        ):
            raise ValueError(
                "shared_preview_v1 requires future actions and a 34D anchor"
            )
        with self._hand_target_lock:
            if (
                self._hand_trajectory_planner is not None
                and not self._hand_trajectory_planner.initialized
            ):
                self._hand_trajectory_planner.reset(anchor_hand)
            self._hand_segment_generation += 1
            self._hand_segment_started_at = float(started_at)
            self._hand_reference_actions = (
                None if actions is None else actions.copy()
            )
            self._hand_reference_start_index = int(start_index)
            self._hand_reference_interval_count = intervals
            self._hand_reference_anchor = anchor_hand
            self._hand_reference_blend_seconds = max(0.0, float(blend_seconds))
            self._hand_reference_blend_profile = blend_profile

    def enable_split_hand_output(self) -> None:
        """Open a deliberately deferred Split hand gate after an arm-only phase."""
        if self._servo_output_mode != "split":
            raise RobotControlError("hand output gate is only available in split mode")
        if not self._powered or self._hand_thread is None:
            raise RobotControlError("split hand output is unavailable")
        with self._hand_target_lock:
            if self._hand_target is None:
                raise RobotControlError(
                    "cannot enable split hand output before a finite target exists"
                )
        self._hand_output_enabled.set()
        self._hand_target_event.set()

    def send_action(self, action: np.ndarray) -> bool:
        """Send one 34D absolute position target; unavailable in shadow."""
        if not self._powered:
            raise RobotControlError("shadow robot adapter cannot send servo commands")
        if self._emergency_stop.is_set():
            raise RobotControlError("software emergency stop is latched")
        array = np.asarray(action, dtype=np.float64)
        if array.shape != (ACTION_DIM,):
            raise RobotControlError(
                f"servo target must be ({ACTION_DIM},), got {array.shape}"
            )
        clipped, clip_count, max_clip_delta = clip_action_positions(array)
        self._last_clip_count = clip_count
        self._last_clip_max_delta = max_clip_delta
        if clip_count:
            logger.warning(
                "Clipped %d G2 servo positions to physical limits "
                "(max_delta=%.6f)",
                clip_count,
                max_clip_delta,
            )
        baseline = self._last_commanded_target
        if baseline is None:
            baseline = np.asarray(self.read_state(), dtype=np.float64)

        compared = (
            slice(None)
            if self._servo_output_mode in {"unified-34", "dual-side-17d"}
            else slice(0, ARM_ACTION_DIM)
        )
        max_step = float(np.max(np.abs(clipped[compared] - baseline[compared])))
        if (
            self._servo_channel == "low-latency"
            and self._enforce_low_latency_step_limit
            and max_step > MAX_LOW_LATENCY_STEP_RAD
        ):
            raise RobotControlError(
                "low-latency dispatched step exceeds software safety limit: "
                f"{max_step:.6f} > {MAX_LOW_LATENCY_STEP_RAD:.6f} rad"
            )

        started_at = time.monotonic()
        if self._hand_fault is not None:
            raise self._hand_fault
        if self._servo_output_mode == "unified-34":
            self._send_servo_group(
                "both arms + both hands",
                POLICY_JOINT_NAMES,
                clipped,
                enable_low_latency=self._servo_channel == "low-latency",
            )
        elif self._servo_output_mode == "dual-side-17d":
            if self._dual_side_servo_layouts is None:
                raise RobotControlError("dual-side 17D dynamic layouts are unavailable")
            for label, (names, indices) in zip(
                ("left arm + dynamic left O10", "right arm + dynamic right O10"),
                self._dual_side_servo_layouts,
                strict=True,
            ):
                self._send_servo_group(
                    label,
                    names,
                    clipped[np.asarray(indices, dtype=np.intp)],
                    enable_low_latency=self._servo_channel == "low-latency",
                )
        else:
            self._send_servo_group(
                "both arms only",
                POLICY_JOINT_NAMES[:ARM_ACTION_DIM],
                clipped[:ARM_ACTION_DIM],
                enable_low_latency=self._servo_channel == "low-latency",
            )
            if self._servo_output_mode == "split":
                with self._hand_target_lock:
                    self._hand_target = clipped[ARM_ACTION_DIM:].copy()
                self._hand_target_event.set()
        self._last_send_seconds = time.monotonic() - started_at
        if self._last_send_seconds > self._max_send_seconds:
            reason = (
                "GDK servo send exceeded watchdog: "
                f"{self._last_send_seconds:.6f}s > "
                f"{self._max_send_seconds:.6f}s"
            )
            if self._timing_warning_callback is None:
                raise RobotControlError(reason)
            logger.warning("%s; warning-only, continuing", reason)
            self._timing_warning_callback("servo_send_overrun", self._last_send_seconds, self._max_send_seconds)
        self._last_commanded_target = clipped.copy()
        self._record_dispatch_arm(
            clipped[:ARM_ACTION_DIM],
            sent_at=started_at,
            send_duration_seconds=self._last_send_seconds,
        )
        self._maybe_record_joint_feedback(command_sent_at=started_at)
        if self._servo_output_mode in {"unified-34", "dual-side-17d"}:
            if self._servo_output_mode in {"dual-side-17d", "unified-34"}:
                self._hand_send_count += 1
                self._last_hand_send_seconds = self._last_send_seconds
            self._record_dispatch_hand(
                clipped[ARM_ACTION_DIM:],
                source=(
                    "unified_34_joint_servo"
                    if self._servo_output_mode == "unified-34"
                    else "dual_side_17d_joint_servo_100hz"
                ),
            )
        return True

    def _read_dual_side_servo_layouts(
        self,
    ) -> tuple[
        tuple[tuple[str, ...], tuple[int, ...]],
        tuple[tuple[str, ...], tuple[int, ...]],
    ]:
        """Validate feedback names and freeze canonical Servo names in paired order."""
        try:
            payload = self._robot.get_end_state()
        except Exception as exc:
            raise RobotControlError(
                f"dual-side 17D get_end_state failed: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise RobotControlError("dual-side 17D get_end_state returned non-mapping")
        policy_index = {name: index for index, name in enumerate(POLICY_JOINT_NAMES)}
        layouts: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
        specifications = (
            ("left", LEFT_ARM_JOINT_NAMES, LEFT_HAND_JOINT_NAMES),
            ("right", RIGHT_ARM_JOINT_NAMES, RIGHT_HAND_JOINT_NAMES),
        )
        for side, arm_names, expected_hand_names in specifications:
            side_payload = payload.get(f"{side}_end_state")
            if not isinstance(side_payload, Mapping):
                raise RobotControlError(f"{side} dynamic O10 state is missing")
            raw_names = side_payload.get("names")
            end_states = side_payload.get("end_states")
            if (
                not isinstance(raw_names, Sequence)
                or isinstance(raw_names, (str, bytes))
                or len(raw_names) != 10
                or not isinstance(end_states, Sequence)
                or isinstance(end_states, (str, bytes))
                or len(end_states) != 10
            ):
                raise RobotControlError(
                    f"{side} dynamic O10 names/states must be parallel 10D"
                )
            exact_hand_names = tuple(str(name) for name in raw_names)
            normalized_hand_names = tuple(name.strip() for name in exact_hand_names)
            if (
                any(not name for name in normalized_hand_names)
                or len(set(normalized_hand_names)) != 10
                or set(normalized_hand_names) != set(expected_hand_names)
            ):
                raise RobotControlError(
                    f"{side} dynamic O10 names do not match the frozen policy set: "
                    f"{exact_hand_names}"
                )
            for exact_name, state in zip(exact_hand_names, end_states, strict=True):
                if not isinstance(state, Mapping):
                    raise RobotControlError(
                        f"{side} dynamic O10 state for {exact_name!r} is invalid"
                    )
                if state.get("enable") is not True:
                    raise RobotControlError(
                        f"{side} dynamic O10 joint {exact_name!r} is disabled"
                    )
                if int(state.get("status", -1)) != 0 or int(
                    state.get("err_code", -1)
                ) != 0:
                    raise RobotControlError(
                        f"{side} dynamic O10 joint {exact_name!r} is unhealthy"
                    )
            # get_end_state may return a trailing blank (right index PIP).
            # The normal motion controller rejects that exact string. Keep
            # raw names in diagnostics, but send the validated canonical names.
            names = arm_names + normalized_hand_names
            logger.warning(
                "【17D名称映射】side=%s feedback_raw=%r servo_names=%r",
                side, exact_hand_names, normalized_hand_names,
            )
            indices = tuple(policy_index[name] for name in arm_names) + tuple(
                policy_index[name] for name in normalized_hand_names
            )
            layouts.append((names, indices))
        logger.warning(
            "【动态17D合同】left=%s right=%s",
            layouts[0][0],
            layouts[1][0],
        )
        return layouts[0], layouts[1]

    def send_hands_once(
        self,
        positions: np.ndarray,
        *,
        source: str = "diagnostic_move_ee_pos_once",
        decision: HandTrackingDecision | HandTrajectoryDecision | None = None,
    ) -> bool:
        """Submit exactly one O10 point target without starting a hand worker.

        Event-driven control and paired diagnostics both use this primitive.
        Requiring ``arms-only`` ensures no background Split thread can silently
        refresh the same target after this method returns.
        """
        if not self._powered:
            raise RobotControlError("shadow robot adapter cannot send O10 commands")
        if self._emergency_stop.is_set():
            raise RobotControlError("software emergency stop is latched")
        if self._servo_output_mode != "arms-only" or self._hand_thread is not None:
            raise RobotControlError(
                "one-shot O10 requires arms-only mode without a hand worker"
            )
        self._require_move_ee_pos()
        values = np.asarray(positions, dtype=np.float64)
        if values.shape != (ACTION_DIM - ARM_ACTION_DIM,) or not np.all(
            np.isfinite(values)
        ):
            raise RobotControlError("one-shot O10 target must be finite 20D")
        lower = POLICY_JOINT_LIMITS[ARM_ACTION_DIM:, 0]
        upper = POLICY_JOINT_LIMITS[ARM_ACTION_DIM:, 1]
        clipped = np.clip(values, lower, upper)
        clip_count = int(np.count_nonzero(clipped != values))
        if clip_count:
            logger.warning(
                "Clipped %d one-shot O10 positions to physical limits",
                clip_count,
            )
        self._send_hands_move_ee(
            clipped,
            decision=decision,
            source_override=source,
        )
        return True

    def _send_servo_group(
        self,
        label: str,
        joint_names: tuple[str, ...],
        joint_positions: np.ndarray,
        *,
        enable_low_latency: bool,
    ) -> None:
        if not hasattr(self._gdk, "JointServoControlReq") or not hasattr(
            self._robot, "joint_servo_control"
        ):
            raise RobotControlError("GDK joint_servo_control path is unavailable")
        try:
            names = tuple(joint_names)
            positions = np.asarray(joint_positions, dtype=np.float64)
            if (
                positions.ndim != 1
                or len(names) != len(positions)
                or not np.all(np.isfinite(positions))
            ):
                self._servo_requests_all_complete = False
                raise RobotControlError(
                    f"{label} request must have equal finite 1D names/positions; "
                    f"names={len(names)} positions_shape={positions.shape}"
                )
            request = self._gdk.JointServoControlReq()
            request.control_period = self._control_period
            request.joint_names = list(names)
            request.joint_positions = positions.tolist()
            # GDK 2.6.3 marks this field reserved and permits an empty list.
            request.joint_velocities = []
            result = self._robot.joint_servo_control(
                request,
                enable_low_latency=enable_low_latency,
            )
        except RobotControlError:
            raise
        except Exception as exc:
            self._last_collision_pairs = self._read_collision_pairs()
            raise RobotControlError(
                f"{label} joint_servo_control failed: {exc}; "
                f"collision_pairs={self._last_collision_pairs}"
            ) from exc
        if result != 0:
            raise RobotControlError(
                f"{label} joint_servo_control returned non-zero status {result}"
            )
        self._servo_request_count += 1
        self._last_servo_joint_count = len(request.joint_names)
        self._last_servo_position_count = len(request.joint_positions)
        self._last_servo_velocity_count = len(request.joint_velocities)
        self._servo_requests_all_complete = (
            self._servo_requests_all_complete
            and self._last_servo_joint_count == self._last_servo_position_count
            and self._last_servo_joint_count in {17, ARM_ACTION_DIM, ACTION_DIM}
        )

    def emergency_stop(self) -> None:
        """Latch the software command gate; hardware emergency stop remains external."""
        self._emergency_stop.set()
        self._hand_stop_event.set()
        self._hand_target_event.set()
        logger.critical("G2 software emergency stop latched; servo sends disabled")

    def is_emergency_stopped(self) -> bool:
        return self._emergency_stop.is_set()

    def clip_stats(self) -> tuple[int, float]:
        return self._last_clip_count, self._last_clip_max_delta

    def control_stats(self) -> dict[str, object]:
        return {
            "servo_channel": self._servo_channel,
            "request_layout": self._servo_output_mode,
            "servo_control_period_seconds": self._control_period,
            "servo_request_count": self._servo_request_count,
            "servo_requests_all_complete": self._servo_requests_all_complete,
            "last_servo_joint_count": self._last_servo_joint_count,
            "last_servo_position_count": self._last_servo_position_count,
            "last_servo_velocity_count": self._last_servo_velocity_count,
            "last_send_seconds": self._last_send_seconds,
            "hand_stream_hz": (
                100.0
                if self._servo_output_mode in {"dual-side-17d", "unified-34"}
                else (
                    self._hand_stream_hz
                    if self._servo_output_mode == "split"
                    else 0.0
                )
            ),
            "hand_send_count": self._hand_send_count,
            "hand_unchanged_skip_count": self._hand_unchanged_skip_count,
            "last_hand_send_seconds": self._last_hand_send_seconds,
            "hand_smoothing_profile": self._hand_smoothing_profile,
            "joint_feedback_hz": (
                self._joint_feedback_hz if self._joint_feedback_enabled else 0.0
            ),
            "joint_feedback_dropped": (
                0
                if self._joint_feedback_writer is None
                else self._joint_feedback_writer.dropped
            ),
            "hand_derivative_constraints_enforced": (
                None
                if self._hand_trajectory_planner is None
                else self._hand_trajectory_planner.config.enforce_derivative_limits
            ),
            "hand_position_constraints_enforced": (
                None
                if self._hand_trajectory_planner is None
                else self._hand_trajectory_planner.config.position_lower_rad
                is not None
            ),
            "hand_jerk_penalty": (
                None
                if self._hand_trajectory_planner is None
                else self._hand_trajectory_planner.config.jerk_penalty
            ),
            "collision_pairs": list(self._last_collision_pairs),
        }

    def _require_move_ee_pos(self) -> None:
        if not all(
            (
                hasattr(self._gdk, "JointStates"),
                hasattr(self._gdk, "JointState"),
                hasattr(self._robot, "move_ee_pos"),
            )
        ):
            raise RobotControlError(
                "split output requires GDK JointStates/JointState and move_ee_pos"
            )

    def _build_dual_hand_request(self, positions: np.ndarray) -> Any:
        values = np.asarray(positions, dtype=np.float64)
        if values.shape != (20,) or not np.all(np.isfinite(values)):
            raise RobotControlError("O10 dual hand target must be finite 20D")
        request = self._gdk.JointStates()
        request.group = "dual_tool"
        request.target_type = EXPECTED_END_EFFECTOR_MODEL
        states = []
        for value in values.tolist():
            state = self._gdk.JointState()
            state.position = float(value)
            states.append(state)
        request.states = states
        request.nums = len(states)
        return request

    def _send_hands_move_ee(
        self,
        positions: np.ndarray,
        *,
        decision: HandTrackingDecision | HandTrajectoryDecision | None = None,
        source_override: str | None = None,
    ) -> None:
        started_at = time.monotonic()
        try:
            result = self._robot.move_ee_pos(
                self._build_dual_hand_request(positions)
            )
        except Exception as exc:
            raise RobotControlError(
                f"O10 dual_tool move_ee_pos failed: {exc}"
            ) from exc
        self._last_hand_send_seconds = time.monotonic() - started_at
        if result != 0:
            raise RobotControlError(
                f"O10 dual_tool move_ee_pos returned non-zero status {result}"
            )
        if self._last_hand_send_seconds > self._max_send_seconds:
            raise RobotControlError(
                "GDK hand move_ee_pos exceeded watchdog: "
                f"{self._last_hand_send_seconds:.6f}s > {self._max_send_seconds:.6f}s"
            )
        self._last_acknowledged_hand_command = np.asarray(
            positions, dtype=np.float64
        ).copy()
        self._record_dispatch_hand(
            positions,
            source=(
                source_override
                if source_override is not None
                else (
                    f"split_move_ee_pos_hand_{self._hand_smoothing_profile}"
                    if decision is not None
                    else "split_move_ee_pos"
                )
            ),
            decision=decision,
        )
        self._hand_send_count += 1
        self._persist_hand_command(positions)

    def _record_dispatch_arm(
        self,
        positions: np.ndarray,
        *,
        sent_at: float,
        send_duration_seconds: float,
    ) -> None:
        """Keep observability failures outside the 100 Hz command gate."""
        if self._dispatch_telemetry is None:
            return
        try:
            self._dispatch_telemetry.record_arm_command(
                positions,
                sent_at=sent_at,
                send_duration_seconds=send_duration_seconds,
            )
        except Exception:
            logger.exception("双臂实际下发telemetry记录失败；控制继续")

    def _disable_joint_feedback(self, reason: str) -> None:
        if not self._joint_feedback_enabled:
            return
        self._joint_feedback_enabled = False
        logger.error("【反馈采样自动关闭】%s；机器人控制继续", reason)
        if self._joint_feedback_writer is not None:
            self._joint_feedback_writer.submit(
                status="disabled",
                error=reason,
                joints_rad=None,
                valid=[False] * ACTION_DIM,
            )

    def _maybe_record_joint_feedback(self, *, command_sent_at: float) -> None:
        """Sample after an acknowledged arm send; never create another GDK owner."""
        if self._last_arm_command_at is not None:
            interval = command_sent_at - self._last_arm_command_at
            self._long_arm_interval_streak = (
                self._long_arm_interval_streak + 1 if interval > 0.015 else 0
            )
            if self._long_arm_interval_streak >= 2:
                self._disable_joint_feedback(
                    f"连续两个手臂发送间隔超过15ms（末次={interval:.6f}s）"
                )
        self._last_arm_command_at = command_sent_at
        if not self._joint_feedback_enabled or self._joint_feedback_writer is None:
            return
        now = time.monotonic()
        if now < self._joint_feedback_next_at:
            return
        period = 1.0 / self._joint_feedback_hz
        self._joint_feedback_next_at = max(self._joint_feedback_next_at + period, now)
        started_ns = time.monotonic_ns()
        mode: str | None = None
        request_id: int | None = None
        if self._dispatch_telemetry is not None:
            mode, request_id = self._dispatch_telemetry.active_identity()
        try:
            state = np.asarray(self._reader.read(), dtype=np.float64)
            completed_ns = time.monotonic_ns()
            duration = (completed_ns - started_ns) / 1e9
            self._joint_feedback_writer.submit(
                status="ok",
                mode=mode,
                request_id=request_id,
                playback_id=request_id,
                read_started_monotonic_ns=started_ns,
                read_completed_monotonic_ns=completed_ns,
                read_duration_seconds=duration,
                joints_rad=state.tolist(),
                valid=[True] * ACTION_DIM,
                error=None,
            )
            if duration > 0.005:
                self._disable_joint_feedback(
                    f"单次34维读取{duration:.6f}s超过5ms"
                )
        except Exception as exc:
            completed_ns = time.monotonic_ns()
            self._joint_feedback_writer.submit(
                status="error",
                mode=mode,
                request_id=request_id,
                playback_id=request_id,
                read_started_monotonic_ns=started_ns,
                read_completed_monotonic_ns=completed_ns,
                read_duration_seconds=(completed_ns - started_ns) / 1e9,
                joints_rad=None,
                valid=[False] * ACTION_DIM,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._disable_joint_feedback(f"34维读取失败：{type(exc).__name__}: {exc}")

    def _record_dispatch_hand(
        self,
        positions: np.ndarray,
        *,
        source: str,
        decision: HandTrackingDecision | HandTrajectoryDecision | None = None,
    ) -> None:
        """Never turn a successful native hand command into a control fault."""
        if self._dispatch_telemetry is None:
            return
        try:
            self._dispatch_telemetry.record_hand_command(
                positions,
                source=source,
                raw_positions=(
                    None if decision is None else decision.raw_target
                ),
                boundary_active=(
                    False if decision is None else decision.boundary_active
                ),
                velocity_limit_hits=(
                    0 if decision is None else decision.velocity_limit_hits
                ),
                acceleration_limit_hits=(
                    0
                    if decision is None
                    else decision.acceleration_limit_hits
                ),
                jerk_limit_hits=(
                    0
                    if not isinstance(decision, HandTrajectoryDecision)
                    else decision.jerk_limit_hits
                ),
                direction_reversal_holds=(
                    0
                    if decision is None
                    else decision.direction_reversal_holds
                ),
                emergency_cap_hits=(
                    0 if decision is None else decision.emergency_cap_hits
                ),
                planner_converged=(
                    None
                    if not isinstance(decision, HandTrajectoryDecision)
                    else decision.converged
                ),
                planner_iterations=(
                    None
                    if not isinstance(decision, HandTrajectoryDecision)
                    else decision.iterations
                ),
            )
        except Exception:
            logger.exception("O10实际下发telemetry记录失败；控制继续")

    def _record_unchanged_hand_skip(
        self,
        positions: np.ndarray,
        *,
        last_acknowledged: np.ndarray,
    ) -> None:
        """Keep changed-only observability outside the native command gate."""
        if self._dispatch_telemetry is None:
            return
        try:
            self._dispatch_telemetry.record_hand_unchanged_skip(
                positions,
                last_acknowledged=last_acknowledged,
            )
        except Exception:
            logger.exception("O10未变化跳过telemetry记录失败；控制继续")

    def _persist_hand_command(self, positions: np.ndarray) -> None:
        """Record only commands that move_ee_pos already acknowledged."""
        try:
            if self._last_hand_command_path is not None:
                latest = build_hand_command_record(
                    positions,
                    kind="last_success",
                    run_id=self._hand_command_run_id,
                    command_index=self._hand_send_count,
                )
                save_hand_command_record(
                    self._last_hand_command_path,
                    latest,
                    overwrite=True,
                )
        except Exception:
            # Recording is recovery telemetry, not part of the native actuator
            # acknowledgement. Keep control alive but make the loss conspicuous.
            logger.exception("【灵巧手命令留档失败】本轮最后成功命令不可用")

    def _hand_stream_loop(self) -> None:
        """Sample continuous-time hand targets at the configured output rate."""
        period = 1.0 / self._hand_stream_hz
        next_send = time.monotonic()
        first_send_not_before: float | None = None
        hand_schedule_started = False
        active_segment_generation = -1
        while not self._hand_stop_event.is_set():
            if not self._hand_output_enabled.wait(timeout=0.1):
                continue
            if self._hand_stop_event.is_set():
                return
            if not self._hand_target_event.wait(timeout=0.1):
                continue
            if self._hand_stop_event.is_set():
                return
            now = time.monotonic()
            if first_send_not_before is None:
                first_send_not_before = now + self._hand_startup_delay_seconds
            if now < first_send_not_before:
                if self._hand_stop_event.wait(first_send_not_before - now):
                    return
                continue
            now = time.monotonic()
            next_send = self._align_hand_start_deadline(
                next_send=next_send,
                now=now,
                schedule_started=hand_schedule_started,
            )
            if now < next_send:
                self._hand_stop_event.wait(next_send - now)
                continue
            with self._hand_target_lock:
                target = None if self._hand_target is None else self._hand_target.copy()
                segment_generation = self._hand_segment_generation
                segment_started_at = self._hand_segment_started_at
                reference_actions = (
                    None
                    if self._hand_reference_actions is None
                    else self._hand_reference_actions.copy()
                )
                reference_start_index = self._hand_reference_start_index
                reference_interval_count = self._hand_reference_interval_count
                reference_anchor = (
                    None
                    if self._hand_reference_anchor is None
                    else self._hand_reference_anchor.copy()
                )
                reference_blend_seconds = self._hand_reference_blend_seconds
                reference_blend_profile = self._hand_reference_blend_profile
            if target is None:
                continue
            try:
                if (
                    self._hand_trajectory_planner is None
                    and segment_generation != active_segment_generation
                ):
                    assert self._hand_tracker is not None
                    self._hand_tracker.begin_segment(
                        started_at=segment_started_at
                    )
                active_segment_generation = segment_generation
                if self._hand_trajectory_planner is None:
                    assert self._hand_tracker is not None
                    decision = self._hand_tracker.step(target, now=now)
                else:
                    if reference_actions is None:
                        raise RobotControlError(
                            "shared_preview_v1 has no future action reference"
                        )
                    reference = self._sample_hand_reference_horizon(
                        now=now,
                        current_target=target,
                        segment_started_at=segment_started_at,
                        actions=reference_actions,
                        start_index=reference_start_index,
                        interval_count=reference_interval_count,
                        anchor=reference_anchor,
                        blend_seconds=reference_blend_seconds,
                        blend_profile=reference_blend_profile,
                    )
                    decision = self._hand_trajectory_planner.step(reference)
                last_acknowledged = self._last_acknowledged_hand_command
                if (
                    last_acknowledged is not None
                    and np.array_equal(decision.command, last_acknowledged)
                ):
                    self._hand_unchanged_skip_count += 1
                    self._record_unchanged_hand_skip(
                        decision.command,
                        last_acknowledged=last_acknowledged,
                    )
                else:
                    self._send_hands_move_ee(
                        decision.command,
                        decision=(
                            decision
                            if self._hand_smoothing_profile != "off"
                            else None
                        ),
                        source_override=(
                            "split_move_ee_pos_raw_5hz_changed_only"
                            if self._hand_smoothing_profile == "off"
                            and self._hand_stream_hz == 5.0
                            else None
                        ),
                    )
                hand_schedule_started = True
            except RobotControlError as exc:
                self._hand_fault = exc
                self._hand_stop_event.set()
                logger.exception(
                    "O10 %g Hz hand output faulted and stopped",
                    self._hand_stream_hz,
                )
                return
            # Advance from the prior deadline so GDK/telemetry time does not
            # accumulate into a lower effective rate. If work overruns one or
            # more periods, skip those slots instead of sending catch-up bursts.
            next_send += period
            finished_at = time.monotonic()
            if next_send <= finished_at:
                skipped_periods = int(
                    (finished_at - next_send) // period
                ) + 1
                next_send += skipped_periods * period

    @staticmethod
    def _align_hand_start_deadline(
        *, next_send: float, now: float, schedule_started: bool
    ) -> float:
        """Anchor cadence to the first actual send, not thread creation."""
        return float(next_send) if schedule_started else float(now)

    def _sample_hand_reference_horizon(
        self,
        *,
        now: float,
        current_target: np.ndarray,
        segment_started_at: float,
        actions: np.ndarray,
        start_index: int,
        interval_count: int,
        anchor: np.ndarray | None,
        blend_seconds: float,
        blend_profile: str,
    ) -> np.ndarray:
        """Sample the same hand reference timeline seen by the 100 Hz Servo."""
        assert self._hand_trajectory_planner is not None
        horizon = self._hand_trajectory_planner.config.horizon_steps
        duration = interval_count / MODEL_HZ
        base_elapsed = max(0.0, now - segment_started_at)
        references = np.empty((horizon, 20), dtype=np.float64)
        for offset in range(horizon):
            elapsed = base_elapsed + offset / self._hand_stream_hz
            if elapsed >= duration:
                endpoint = min(
                    start_index + interval_count,
                    len(actions) - 1,
                )
                sampled = actions[endpoint].copy()
            else:
                sampled = sample_linear_action(
                    actions[start_index:],
                    elapsed,
                    source_hz=MODEL_HZ,
                )
            hand = sampled[ARM_ACTION_DIM:].copy()
            if anchor is not None and blend_seconds > 0 and elapsed < blend_seconds:
                alpha = elapsed / blend_seconds
                if blend_profile == "cubic_smoothstep":
                    alpha = alpha * alpha * (3.0 - 2.0 * alpha)
                elif blend_profile == "quintic_smootherstep":
                    alpha = alpha**3 * (
                        alpha * (alpha * 6.0 - 15.0) + 10.0
                    )
                hand = (1.0 - alpha) * anchor + alpha * hand
            references[offset] = hand
        # The actual current hand target has already passed clipping and the
        # Servo sampler. Keep tick zero exact; preview only supplies the future.
        references[0] = current_target
        return references

    def _read_collision_pairs(self) -> list[tuple[str, str]]:
        try:
            status = self._robot.get_motion_control_status()
        except Exception:
            return []
        left = list(getattr(status, "collision_pairs_1", []) or [])
        right = list(getattr(status, "collision_pairs_2", []) or [])
        return [
            (str(first), str(second))
            for first, second in zip(left, right)
        ]

    def _validate_end_effectors(self) -> None:
        if not hasattr(self._robot, "get_whole_body_status"):
            raise RobotControlError(
                "powered mode requires get_whole_body_status for end-effector checks"
            )
        try:
            status = self._robot.get_whole_body_status()
        except Exception as exc:
            raise RobotControlError(
                f"failed to read whole-body status: {exc}"
            ) from exc
        if not isinstance(status, Mapping):
            raise RobotControlError(
                "get_whole_body_status returned a non-mapping payload"
            )
        for side in ("left", "right"):
            model = str(status.get(f"{side}_end_model", "")).strip()
            if model != EXPECTED_END_EFFECTOR_MODEL:
                raise RobotControlError(
                    f"{side} end-effector model must be "
                    f"{EXPECTED_END_EFFECTOR_MODEL}, got {model!r}"
                )
            try:
                error_code = int(status.get(f"{side}_end_error", -1))
            except (TypeError, ValueError) as exc:
                raise RobotControlError(
                    f"{side} end-effector error code is invalid"
                ) from exc
            if error_code != 0:
                raise RobotControlError(
                    f"{side} end-effector reports error {error_code}"
                )

    def _require_g2_servo_mode(self) -> None:
        try:
            status = self._robot.get_motion_control_status()
        except Exception as exc:
            raise RobotControlError(
                f"failed to read G2 control mode: {exc}"
            ) from exc
        mode = getattr(status, "mode", None)
        error_code = getattr(status, "error_code", 0)
        if mode != G2_SERVO_STATUS_MODE or error_code != 0:
            error_msg = getattr(status, "error_msg", "<unavailable>")
            collision_pairs_1 = getattr(status, "collision_pairs_1", [])
            collision_pairs_2 = getattr(status, "collision_pairs_2", [])
            raise RobotControlError(
                "G2 must already be in healthy servo mode 5 before powered "
                f"startup; got mode={mode!r}, error_code={error_code!r}. "
                f"error_msg={error_msg!r}, "
                f"collision_pairs_1={collision_pairs_1!r}, "
                f"collision_pairs_2={collision_pairs_2!r}. "
                "Home recovery moves joints; it does not clear controller faults. "
                "No motion was sent by this controller. "
                "The client will not call an undocumented fault-clear or mode-switch API."
            )

    def close(self) -> None:
        """Stop the optional hand worker without sending a reset target."""
        self._hand_stop_event.set()
        self._hand_output_enabled.set()
        self._hand_target_event.set()
        if self._hand_thread is not None and self._hand_thread.is_alive():
            # A native GDK call cannot be cancelled from Python. Allow the
            # known multi-second failure mode to return before gdk_release.
            self._hand_thread.join(timeout=5.0)
        if self._joint_feedback_writer is not None:
            self._joint_feedback_writer.close()


class MockRobotController:
    """In-memory G2 adapter used by tests and endpoint diagnostics."""

    def __init__(self, state: np.ndarray | None = None, *, powered: bool) -> None:
        initial = np.zeros(ACTION_DIM, dtype=np.float32) if state is None else state
        self._state = np.asarray(initial, dtype=np.float32).copy()
        if self._state.shape != (ACTION_DIM,) or not np.all(np.isfinite(self._state)):
            raise ValueError("mock state must be finite 34D")
        self._powered = powered
        self._emergency_stop = False
        self.sent_actions: list[np.ndarray] = []

    def read_state(self) -> np.ndarray:
        return self._state.copy()

    def send_action(self, action: np.ndarray) -> bool:
        if not self._powered or self._emergency_stop:
            raise RobotControlError("mock servo command gate is closed")
        clipped, _, _ = clip_action_positions(action)
        self._state = clipped.astype(np.float32)
        self.sent_actions.append(clipped.copy())
        return True

    def emergency_stop(self) -> None:
        self._emergency_stop = True

    def is_emergency_stopped(self) -> bool:
        return self._emergency_stop

    def control_stats(self) -> dict[str, object]:
        return {
            "servo_channel": "mock",
            "request_layout": "mock-34",
            "last_send_seconds": 0.0,
            "hand_stream_hz": 0.0,
            "hand_send_count": 0,
            "last_hand_send_seconds": 0.0,
            "collision_pairs": [],
        }

    def close(self) -> None:
        return None
