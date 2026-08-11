"""G2严格状态读取与统一关节servo硬件适配。

用途：向上层提供唯一的34D``read_state``和``send_action``接口。
输入：GDK Robot、34D absolute joint-position、control period和低延迟开关。
输出：严格measured state或一次``joint_servo_control``结果；shadow禁止发送。
方法：powered时进入关节位置模式，按冻结关节名限位后构造统一GDK请求。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from .contracts import ACTION_DIM, RobotControlError
from .joint_layout import POLICY_JOINT_NAMES, clip_action_positions
from .robot_state import StrictRobotStateReader

logger = logging.getLogger(__name__)

G2_SERVO_STATUS_MODE = 5


class G2RobotController:
    """Expose only strict 34D state reads and unified 34D position servo sends."""

    def __init__(
        self,
        *,
        powered: bool,
        control_period: float = 0.01,
        enable_low_latency: bool = False,
        stabilization_seconds: float = 2.0,
        gdk_module: Any | None = None,
        robot: Any | None = None,
    ) -> None:
        if control_period <= 0:
            raise ValueError("servo control period must be positive")
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
        self._enable_low_latency = bool(enable_low_latency)
        self._emergency_stop = threading.Event()
        self._last_clip_count = 0
        self._last_clip_max_delta = 0.0
        if stabilization_seconds > 0:
            time.sleep(stabilization_seconds)
        if self._powered:
            self._configure_motion_control()

    def read_state(self) -> np.ndarray:
        return self._reader.read()

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
        if not hasattr(self._gdk, "JointServoControlReq") or not hasattr(
            self._robot, "joint_servo_control"
        ):
            raise RobotControlError("GDK joint_servo_control path is unavailable")
        try:
            request = self._gdk.JointServoControlReq()
            request.control_period = self._control_period
            request.joint_names = list(POLICY_JOINT_NAMES)
            request.joint_positions = clipped.tolist()
            try:
                result = self._robot.joint_servo_control(
                    request,
                    enable_low_latency=self._enable_low_latency,
                )
            except TypeError:
                if self._enable_low_latency:
                    logger.warning(
                        "Installed GDK lacks enable_low_latency; using normal channel"
                    )
                result = self._robot.joint_servo_control(request)
        except RobotControlError:
            raise
        except Exception as exc:
            raise RobotControlError(f"joint_servo_control failed: {exc}") from exc
        if result != 0:
            raise RobotControlError(
                f"joint_servo_control returned non-zero status {result}"
            )
        return True

    def emergency_stop(self) -> None:
        """Latch the software command gate; hardware emergency stop remains external."""
        self._emergency_stop.set()
        logger.critical("G2 software emergency stop latched; servo sends disabled")

    def is_emergency_stopped(self) -> bool:
        return self._emergency_stop.is_set()

    def clip_stats(self) -> tuple[int, float]:
        return self._last_clip_count, self._last_clip_max_delta

    def _configure_motion_control(self) -> None:
        try:
            status = self._robot.get_motion_control_status()
        except Exception as exc:
            logger.warning("Could not read initial G2 control mode: %s", exc)
            status = None
        if getattr(status, "mode", None) == G2_SERVO_STATUS_MODE:
            return
        if not all(
            hasattr(self._gdk, name)
            for name in ("MotionControlMode", "ControlMode")
        ) or not hasattr(self._robot, "set_control_mode"):
            raise RobotControlError(
                "cannot explicitly enter G2 joint-position servo mode"
            )
        try:
            request = self._gdk.MotionControlMode()
            request.control_mode = self._gdk.ControlMode.CTRL_JOINT_POSITION
            result = self._robot.set_control_mode(request)
        except Exception as exc:
            raise RobotControlError(
                f"failed to enter G2 joint-position servo mode: {exc}"
            ) from exc
        if result not in (None, 0):
            raise RobotControlError(f"set_control_mode returned status {result}")

    def close(self) -> None:
        """No destructive reset is attempted; the streamer holds before shutdown."""


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

    def close(self) -> None:
        return None
