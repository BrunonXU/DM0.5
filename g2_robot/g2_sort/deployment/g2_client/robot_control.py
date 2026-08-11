"""G2严格状态读取与三种显式硬件下发模式。

用途：向上层提供唯一的34D``read_state``和``send_action``接口。
输入：GDK Robot、34D absolute joint-position、control period、通道和输出模式。
输出：严格measured state或经过计时的Servo结果；shadow禁止发送。
方法：默认把模型34D拆成双臂100Hz joint_servo_control与O10双手5Hz
move_ee_pos两个独立通道；统一34D Servo和仅双臂只保留为显式诊断模式。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import ACTION_DIM, RobotControlError
from .hand_command_store import (
    build_hand_command_record,
    save_hand_command_record,
)
from .joint_layout import (
    POLICY_JOINT_NAMES,
    clip_action_positions,
)
from .robot_state import StrictRobotStateReader
from .telemetry import DispatchTelemetry
from .trajectory_smoothing import (
    CausalHandCommandTracker,
    HandTrackingDecision,
)

logger = logging.getLogger(__name__)

G2_SERVO_STATUS_MODE = 5
EXPECTED_END_EFFECTOR_MODEL = "o10_t2"
MAX_LOW_LATENCY_STEP_RAD = 0.03
ARM_ACTION_DIM = 14
HAND_STREAM_HZ = 5.0
OUTPUT_MODES = {"unified-34", "arms-only", "split"}


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
        enforce_low_latency_step_limit: bool = True,
        last_hand_command_path: Path | None = None,
        hand_command_run_id: str = "runtime",
        hand_startup_delay_seconds: float = 0.08,
        hand_smoothing_profile: str = "off",
        hand_boundary_blend_seconds: float = 0.4,
        hand_max_velocity_rad_s: float = 2.0,
        hand_max_acceleration_rad_s2: float = 10.0,
        hand_max_delta_radians: float = 0.4,
        hand_reversal_error_radians: float = 0.15,
        dispatch_telemetry: DispatchTelemetry | None = None,
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
        if hand_smoothing_profile not in {"off", "v1"}:
            raise ValueError("hand_smoothing_profile must be off or v1")
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
        self._hand_target_lock = threading.Lock()
        self._hand_target: np.ndarray | None = None
        self._hand_target_event = threading.Event()
        self._hand_stop_event = threading.Event()
        self._hand_fault: RobotControlError | None = None
        self._hand_send_count = 0
        self._last_hand_send_seconds = 0.0
        self._last_hand_command_path = last_hand_command_path
        self._hand_command_run_id = hand_command_run_id
        self._hand_startup_delay_seconds = float(hand_startup_delay_seconds)
        self._hand_smoothing_profile = hand_smoothing_profile
        self._hand_tracker = CausalHandCommandTracker(
            enabled=hand_smoothing_profile == "v1",
            boundary_blend_seconds=hand_boundary_blend_seconds,
            max_velocity_rad_s=hand_max_velocity_rad_s,
            max_acceleration_rad_s2=hand_max_acceleration_rad_s2,
            max_delta_rad=hand_max_delta_radians,
            reversal_error_rad=hand_reversal_error_radians,
        )
        self._hand_segment_generation = 0
        self._hand_segment_started_at = time.monotonic()
        self._dispatch_telemetry = dispatch_telemetry
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
                    name="g2-o10-move-ee-5hz",
                    daemon=True,
                )
                self._hand_thread.start()

    def read_state(self) -> np.ndarray:
        return self._reader.read()

    def begin_hand_segment(self, *, started_at: float) -> None:
        """Mark a policy boundary; the 5 Hz worker owns tracker state changes."""
        if not np.isfinite(started_at):
            raise ValueError("hand segment start time must be finite")
        with self._hand_target_lock:
            self._hand_segment_generation += 1
            self._hand_segment_started_at = float(started_at)

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
            if self._servo_output_mode == "unified-34"
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
            raise RobotControlError(
                "GDK servo send exceeded watchdog: "
                f"{self._last_send_seconds:.6f}s > "
                f"{self._max_send_seconds:.6f}s"
            )
        self._last_commanded_target = clipped.copy()
        self._record_dispatch_arm(clipped[:ARM_ACTION_DIM])
        if self._servo_output_mode == "unified-34":
            self._record_dispatch_hand(
                clipped[ARM_ACTION_DIM:],
                source="unified_34_joint_servo",
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
            and self._last_servo_joint_count in {ARM_ACTION_DIM, ACTION_DIM}
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
                HAND_STREAM_HZ if self._servo_output_mode == "split" else 0.0
            ),
            "hand_send_count": self._hand_send_count,
            "last_hand_send_seconds": self._last_hand_send_seconds,
            "hand_smoothing_profile": self._hand_smoothing_profile,
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
        decision: HandTrackingDecision | None = None,
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
        self._record_dispatch_hand(
            positions,
            source=(
                "split_move_ee_pos_hand_tracker_v1"
                if decision is not None
                else "split_move_ee_pos"
            ),
            decision=decision,
        )
        self._hand_send_count += 1
        self._persist_hand_command(positions)

    def _record_dispatch_arm(self, positions: np.ndarray) -> None:
        """Keep observability failures outside the 100 Hz command gate."""
        if self._dispatch_telemetry is None:
            return
        try:
            self._dispatch_telemetry.record_arm_command(positions)
        except Exception:
            logger.exception("双臂实际下发telemetry记录失败；控制继续")

    def _record_dispatch_hand(
        self,
        positions: np.ndarray,
        *,
        source: str,
        decision: HandTrackingDecision | None = None,
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
                direction_reversal_holds=(
                    0
                    if decision is None
                    else decision.direction_reversal_holds
                ),
                emergency_cap_hits=(
                    0 if decision is None else decision.emergency_cap_hits
                ),
            )
        except Exception:
            logger.exception("O10实际下发telemetry记录失败；控制继续")

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
        """Sample raw targets at 5 Hz and optionally apply the causal V1."""
        period = 1.0 / HAND_STREAM_HZ
        next_send = time.monotonic()
        first_send_not_before: float | None = None
        active_segment_generation = -1
        while not self._hand_stop_event.is_set():
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
            if now < next_send:
                self._hand_stop_event.wait(next_send - now)
                continue
            with self._hand_target_lock:
                target = None if self._hand_target is None else self._hand_target.copy()
                segment_generation = self._hand_segment_generation
                segment_started_at = self._hand_segment_started_at
            if target is None:
                continue
            try:
                if segment_generation != active_segment_generation:
                    self._hand_tracker.begin_segment(
                        started_at=segment_started_at
                    )
                    active_segment_generation = segment_generation
                decision = self._hand_tracker.step(target, now=now)
                self._send_hands_move_ee(
                    decision.command,
                    decision=(
                        decision
                        if self._hand_smoothing_profile == "v1"
                        else None
                    ),
                )
            except RobotControlError as exc:
                self._hand_fault = exc
                self._hand_stop_event.set()
                logger.exception("O10 5 Hz hand output faulted and stopped")
                return
            next_send = time.monotonic() + period

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
            raise RobotControlError(
                "G2 must already be in healthy servo mode 5 before powered "
                f"startup; got mode={mode!r}, error_code={error_code!r}. "
                "The client will not call an undocumented mode-switch API."
            )

    def close(self) -> None:
        """Stop the optional hand worker without sending a reset target."""
        self._hand_stop_event.set()
        self._hand_target_event.set()
        if self._hand_thread is not None and self._hand_thread.is_alive():
            # A native GDK call cannot be cancelled from Python. Allow the
            # known multi-second failure mode to return before gdk_release.
            self._hand_thread.join(timeout=5.0)


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
