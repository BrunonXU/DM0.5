"""独立于HTTP推理的100Hz轨迹streamer和已激活目标hold。

用途：首个安全chunk到达前零发送；激活后在模型等待时hold并播放30Hz action chunk。
输入：可选34D初始目标、动作chunk、起始索引、区间数和可选边界融合时间。
输出：每个100Hz tick把一个34D逻辑目标交给robot adapter，并提供播放/故障/统计；
正式adapter再拆成14D双臂100Hz与20D双手5Hz硬件请求。
方法：后台线程先等待真实目标事件，再按monotonic deadline采样；结束后hold，
绝不在首个安全目标前发送current-hold，也绝不速度外推。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .contracts import ACTION_DIM, MODEL_HZ, SERVO_HZ, RobotControlError
from .interpolation import sample_linear_action

logger = logging.getLogger(__name__)


@dataclass
class _Playback:
    actions: np.ndarray
    start_index: int
    interval_count: int
    started_at: float
    anchor: np.ndarray
    blend_seconds: float
    playback_id: int

    @property
    def duration_seconds(self) -> float:
        return self.interval_count / MODEL_HZ


class ServoStreamer:
    """Continuously send the latest safe target at exactly one local cadence."""

    def __init__(
        self,
        sender: Callable[[np.ndarray], bool],
        initial_hold_target: np.ndarray | None,
        *,
        servo_hz: float = SERVO_HZ,
        max_tick_lateness_seconds: float = 0.03,
        smoothstep_blend: bool = False,
        boundary_blend_profile: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if servo_hz <= 0:
            raise ValueError("servo_hz must be positive")
        if max_tick_lateness_seconds <= 0:
            raise ValueError("max tick lateness must be positive")
        target: np.ndarray | None = None
        if initial_hold_target is not None:
            target = np.asarray(initial_hold_target, dtype=np.float64)
            if target.shape != (ACTION_DIM,) or not np.all(np.isfinite(target)):
                raise ValueError("initial hold target must be finite 34D")
        self._sender = sender
        self._servo_hz = float(servo_hz)
        self._max_tick_lateness_seconds = float(max_tick_lateness_seconds)
        if boundary_blend_profile is None:
            boundary_blend_profile = (
                "cubic_smoothstep" if smoothstep_blend else "linear"
            )
        if boundary_blend_profile not in {
            "linear",
            "cubic_smoothstep",
            "quintic_smootherstep",
        }:
            raise ValueError(
                "boundary_blend_profile must be linear, cubic_smoothstep, "
                "or quintic_smootherstep"
            )
        self._boundary_blend_profile = boundary_blend_profile
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._target = None if target is None else target.copy()
        self._target_ready = threading.Event()
        if self._target is not None:
            self._target_ready.set()
        self._playback: _Playback | None = None
        self._next_playback_id = 1
        self._stop = threading.Event()
        self._fault = threading.Event()
        self._fault_message: str | None = None
        self._thread: threading.Thread | None = None
        self._send_count = 0
        self._hold_send_count = 0
        self._max_tick_lateness_observed = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="g2-servo-100hz",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._target_ready.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
            self._thread = None

    def close(self) -> None:
        self.stop()

    def hold(self, target: np.ndarray | None = None) -> None:
        """Cancel playback and repeatedly send a fixed safe target."""
        with self._lock:
            if target is not None:
                array = np.asarray(target, dtype=np.float64)
                if array.shape != (ACTION_DIM,) or not np.all(np.isfinite(array)):
                    raise ValueError("hold target must be finite 34D")
                self._target = array.copy()
                self._target_ready.set()
            self._playback = None

    def play(
        self,
        actions: np.ndarray,
        *,
        start_index: int,
        interval_count: int,
        blend_seconds: float = 0.0,
        anchor: np.ndarray | None = None,
    ) -> int:
        """Start a time-based trajectory and return its monotonic playback ID."""
        array = np.asarray(actions, dtype=np.float64)
        if array.ndim != 2 or array.shape[1] != ACTION_DIM:
            raise ValueError(f"playback actions must be Nx{ACTION_DIM}")
        if not np.all(np.isfinite(array)):
            raise ValueError("playback actions must be finite")
        if start_index < 0 or start_index >= len(array):
            raise ValueError("playback start index is outside the chunk")
        if interval_count <= 0 or interval_count > len(array) - start_index:
            raise ValueError("playback interval count exceeds the chunk")
        anchor_array: np.ndarray | None = None
        if anchor is not None:
            anchor_array = np.asarray(anchor, dtype=np.float64)
            if (
                anchor_array.shape != (ACTION_DIM,)
                or not np.all(np.isfinite(anchor_array))
            ):
                raise ValueError("playback anchor must be finite 34D")
        with self._lock:
            if self._target is None:
                if anchor_array is None:
                    raise ValueError(
                        "first playback requires a finite 34D anchor"
                    )
                self._target = anchor_array.copy()
            playback_id = self._next_playback_id
            self._next_playback_id += 1
            self._playback = _Playback(
                actions=array.copy(),
                start_index=start_index,
                interval_count=interval_count,
                started_at=self._clock(),
                anchor=self._target.copy(),
                blend_seconds=max(0.0, float(blend_seconds)),
                playback_id=playback_id,
            )
            self._target_ready.set()
            return playback_id

    def tick_once(self, now: float | None = None) -> np.ndarray:
        """Sample and dispatch one tick; public for deterministic unit tests."""
        now = self._clock() if now is None else float(now)
        with self._lock:
            playback = self._playback
            playing = playback is not None
            if playback is not None:
                elapsed = max(0.0, now - playback.started_at)
                if elapsed >= playback.duration_seconds:
                    end_index = min(
                        playback.start_index + playback.interval_count,
                        len(playback.actions) - 1,
                    )
                    self._target = playback.actions[end_index].copy()
                    self._playback = None
                    playing = False
                else:
                    sampled = sample_linear_action(
                        playback.actions[playback.start_index :],
                        elapsed,
                        source_hz=MODEL_HZ,
                    )
                    if playback.blend_seconds > 0 and elapsed < playback.blend_seconds:
                        alpha = elapsed / playback.blend_seconds
                        if self._boundary_blend_profile == "cubic_smoothstep":
                            alpha = alpha * alpha * (3.0 - 2.0 * alpha)
                        elif self._boundary_blend_profile == "quintic_smootherstep":
                            alpha = alpha**3 * (
                                alpha * (alpha * 6.0 - 15.0) + 10.0
                            )
                        sampled = (
                            (1.0 - alpha) * playback.anchor + alpha * sampled
                        )
                    self._target = sampled
            if self._target is None:
                raise RobotControlError(
                    "servo target is unavailable before first safe playback"
                )
            target = self._target.copy()
        try:
            ok = self._sender(target)
            if ok is False:
                raise RobotControlError("servo sender returned False")
        except Exception as exc:
            self._fault_message = str(exc)
            self._fault.set()
            raise RobotControlError(f"100 Hz servo send failed: {exc}") from exc
        self._send_count += 1
        if not playing:
            self._hold_send_count += 1
        return target

    def is_playing(self, playback_id: int | None = None) -> bool:
        with self._lock:
            if self._playback is None:
                return False
            return playback_id is None or self._playback.playback_id == playback_id

    def remaining_intervals(self) -> float:
        with self._lock:
            playback = self._playback
            if playback is None:
                return 0.0
            elapsed = max(0.0, self._clock() - playback.started_at)
            return max(0.0, playback.interval_count - elapsed * MODEL_HZ)

    def current_target(self) -> np.ndarray:
        with self._lock:
            if self._target is None:
                raise RobotControlError("servo has no accepted target")
            return self._target.copy()

    def has_target(self) -> bool:
        """Return whether any accepted target has activated the sender."""
        with self._lock:
            return self._target is not None

    def has_fault(self) -> bool:
        return self._fault.is_set()

    def fault_message(self) -> str | None:
        return self._fault_message

    def stats(self) -> dict[str, int | float]:
        return {
            "send_count": self._send_count,
            "hold_send_count": self._hold_send_count,
            "max_tick_lateness_seconds": self._max_tick_lateness_observed,
        }

    def _run(self) -> None:
        period = 1.0 / self._servo_hz
        while not self._stop.is_set() and not self._target_ready.wait(0.1):
            pass
        if self._stop.is_set():
            return
        deadline = self._clock()
        while not self._stop.is_set() and not self._fault.is_set():
            try:
                self.tick_once()
            except RobotControlError:
                logger.exception("Servo streamer faulted and stopped")
                break
            deadline += period
            delay = deadline - self._clock()
            if delay > 0:
                self._sleep(delay)
            else:
                lateness = -delay
                self._max_tick_lateness_observed = max(
                    self._max_tick_lateness_observed,
                    lateness,
                )
                if lateness > self._max_tick_lateness_seconds:
                    self._fault_message = (
                        "100 Hz servo tick lateness exceeded watchdog: "
                        f"{lateness:.6f}s > "
                        f"{self._max_tick_lateness_seconds:.6f}s"
                    )
                    self._fault.set()
                    logger.error(self._fault_message)
                    break
                deadline = self._clock()
