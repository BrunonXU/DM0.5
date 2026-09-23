"""独立于HTTP推理的100Hz轨迹streamer和已激活目标hold。

用途：首个安全chunk到达前零发送；激活后在模型等待时hold并播放30Hz action chunk。
输入：可选34D初始目标、动作chunk、起始索引、区间数和可选边界融合时间。
输出：每个100Hz tick把一个34D逻辑目标交给robot adapter，并提供播放/故障/统计；
正式adapter再拆成14D双臂100Hz与20D双手独立频率硬件请求。
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
from .interpolation import (
    arm_cubic_spline_tangents,
    arm_cubic_tangents,
    sample_cubic_arm_linear_hand_action,
    sample_linear_action,
)

logger = logging.getLogger(__name__)


def project_stationary_arm_minimum_jerk(
    command: np.ndarray,
    threshold_rad: float,
) -> tuple[np.ndarray, tuple[int, ...], int, float]:
    """Project repeated low-range reversals onto a same-endpoint C2 trend.

    This pure function is shared by runtime and offline evaluation.  It acts on
    the final sampled arm target, after interpolation and boundary blending.
    """
    values = np.asarray(command, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 14 or len(values) < 4:
        raise ValueError("arm command must be an Nx14 array with at least 4 rows")
    if not np.all(np.isfinite(values)):
        raise ValueError("arm command must be finite")
    if not np.isfinite(threshold_rad) or threshold_rad <= 0.0:
        raise ValueError("arm micro-motion threshold must be finite and positive")
    result = values.copy()
    delta = np.diff(result, axis=0)
    reversals = np.count_nonzero(delta[:-1] * delta[1:] < 0.0, axis=0)
    span = np.ptp(result, axis=0)
    net = np.abs(result[-1] - result[0])
    active = (
        (reversals >= 2)
        & (span <= threshold_rad)
        & (net <= 0.6 * span)
    )
    indices = tuple(int(index) for index in np.flatnonzero(active))
    max_deviation = 0.0
    if indices:
        u = np.linspace(0.0, 1.0, len(result))[:, None]
        alpha = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
        trend = (1.0 - alpha) * result[0] + alpha * result[-1]
        max_deviation = float(
            np.max(np.abs(trend[:, indices] - result[:, indices]))
        )
        result[:, indices] = trend[:, indices]
    reversal_count = int(np.sum(reversals[list(indices)])) if indices else 0
    return result, indices, reversal_count, max_deviation


@dataclass
class _Playback:
    actions: np.ndarray
    start_index: int
    interval_count: int
    started_at: float
    anchor: np.ndarray
    blend_seconds: float
    playback_id: int
    arm_tangents: np.ndarray | None
    arm_micro_motion_active_indices: tuple[int, ...]
    arm_micro_motion_end: np.ndarray | None

    @property
    def duration_seconds(self) -> float:
        return self.interval_count / MODEL_HZ


@dataclass(frozen=True)
class ArmMicroMotionProjectionStats:
    playback_id: int
    profile: str
    threshold_rad: float
    active_joint_indices: tuple[int, ...]
    baseline_reversal_count: int
    projected_reversal_count: int
    max_deviation_rad: float


class ServoStreamer:
    """Continuously send the latest safe target at exactly one local cadence."""

    def __init__(
        self,
        sender: Callable[[np.ndarray], bool],
        initial_hold_target: np.ndarray | None,
        *,
        servo_hz: float = SERVO_HZ,
        max_tick_lateness_seconds: float = 0.03,
        timing_warning_callback=None,
        smoothstep_blend: bool = False,
        boundary_blend_profile: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        arm_interpolation_profile: str = "linear",
        arm_micro_motion_profile: str = "off",
        arm_micro_motion_threshold_rad: float = 0.0035,
    ) -> None:
        self._timing_warning_callback = timing_warning_callback
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
        if arm_interpolation_profile not in {
            "linear",
            "cubic_hermite",
            "cubic_spline",
        }:
            raise ValueError(
                "arm_interpolation_profile must be linear, cubic_hermite, "
                "or cubic_spline"
            )
        self._arm_interpolation_profile = arm_interpolation_profile
        if arm_micro_motion_profile not in {"off", "stationary_minjerk_v1"}:
            raise ValueError(
                "arm_micro_motion_profile must be off or stationary_minjerk_v1"
            )
        if (
            not np.isfinite(arm_micro_motion_threshold_rad)
            or arm_micro_motion_threshold_rad <= 0.0
        ):
            raise ValueError("arm micro-motion threshold must be finite and positive")
        self._arm_micro_motion_profile = arm_micro_motion_profile
        self._arm_micro_motion_threshold_rad = float(
            arm_micro_motion_threshold_rad
        )
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
        self._arm_micro_motion_stats: dict[
            int, ArmMicroMotionProjectionStats
        ] = {}

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
        arm_start_velocity_per_step: np.ndarray | None = None,
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
        arm_tangents: np.ndarray | None = None
        if self._arm_interpolation_profile == "cubic_hermite":
            arm_tangents = arm_cubic_tangents(array)
            if arm_start_velocity_per_step is not None:
                velocity = np.asarray(
                    arm_start_velocity_per_step, dtype=np.float64
                )
                if velocity.shape != (14,) or not np.all(np.isfinite(velocity)):
                    raise ValueError(
                        "arm_start_velocity_per_step must be finite 14D"
                    )
                arm_tangents[start_index] = velocity
        elif self._arm_interpolation_profile == "cubic_spline":
            arm_tangents = arm_cubic_spline_tangents(
                array,
                start_index=start_index,
                start_velocity_per_step=arm_start_velocity_per_step,
            )
        with self._lock:
            if self._target is None:
                if anchor_array is None:
                    raise ValueError(
                        "first playback requires a finite 34D anchor"
                    )
                self._target = anchor_array.copy()
            playback_id = self._next_playback_id
            self._next_playback_id += 1
            playback = _Playback(
                actions=array.copy(),
                start_index=start_index,
                interval_count=interval_count,
                started_at=self._clock(),
                anchor=self._target.copy(),
                blend_seconds=max(0.0, float(blend_seconds)),
                playback_id=playback_id,
                arm_tangents=arm_tangents,
                arm_micro_motion_active_indices=(),
                arm_micro_motion_end=None,
            )
            stats = self._configure_arm_micro_motion(playback)
            self._arm_micro_motion_stats = {playback_id: stats}
            self._playback = playback
            self._target_ready.set()
            return playback_id

    def arm_micro_motion_stats(
        self, playback_id: int
    ) -> ArmMicroMotionProjectionStats | None:
        """Return immutable projection telemetry for a prepared playback."""
        with self._lock:
            return self._arm_micro_motion_stats.get(playback_id)

    def _sample_nominal_target(
        self, playback: _Playback, elapsed: float
    ) -> np.ndarray:
        if elapsed >= playback.duration_seconds:
            end_index = min(
                playback.start_index + playback.interval_count,
                len(playback.actions) - 1,
            )
            return playback.actions[end_index].copy()
        if playback.arm_tangents is None:
            sampled = sample_linear_action(
                playback.actions[playback.start_index :],
                elapsed,
                source_hz=MODEL_HZ,
            )
        else:
            sampled = sample_cubic_arm_linear_hand_action(
                playback.actions,
                elapsed,
                start_index=playback.start_index,
                source_hz=MODEL_HZ,
                arm_tangents=playback.arm_tangents,
            )
        if playback.blend_seconds > 0 and elapsed < playback.blend_seconds:
            alpha = elapsed / playback.blend_seconds
            if self._boundary_blend_profile == "cubic_smoothstep":
                alpha = alpha * alpha * (3.0 - 2.0 * alpha)
            elif self._boundary_blend_profile == "quintic_smootherstep":
                alpha = alpha**3 * (alpha * (alpha * 6.0 - 15.0) + 10.0)
            sampled = (1.0 - alpha) * playback.anchor + alpha * sampled
        return sampled

    def _configure_arm_micro_motion(
        self, playback: _Playback
    ) -> ArmMicroMotionProjectionStats:
        if self._arm_micro_motion_profile == "off":
            return ArmMicroMotionProjectionStats(
                playback_id=playback.playback_id,
                profile="off",
                threshold_rad=self._arm_micro_motion_threshold_rad,
                active_joint_indices=(),
                baseline_reversal_count=0,
                projected_reversal_count=0,
                max_deviation_rad=0.0,
            )
        sample_count = int(
            np.ceil(playback.duration_seconds * self._servo_hz)
        )
        times = np.arange(sample_count, dtype=np.float64) / self._servo_hz
        detection_times = np.append(times, playback.duration_seconds)
        nominal = np.stack(
            [self._sample_nominal_target(playback, time) for time in detection_times]
        )[:, :14]
        projected, indices, reversal_count, max_deviation = (
            project_stationary_arm_minimum_jerk(
                nominal, self._arm_micro_motion_threshold_rad
            )
        )
        if indices:
            playback.arm_micro_motion_active_indices = indices
            playback.arm_micro_motion_end = projected[-1].copy()
        return ArmMicroMotionProjectionStats(
            playback_id=playback.playback_id,
            profile=self._arm_micro_motion_profile,
            threshold_rad=self._arm_micro_motion_threshold_rad,
            active_joint_indices=indices,
            baseline_reversal_count=reversal_count,
            projected_reversal_count=0,
            max_deviation_rad=max_deviation,
        )

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
                    sampled = self._sample_nominal_target(playback, elapsed)
                    if playback.arm_micro_motion_active_indices:
                        assert playback.arm_micro_motion_end is not None
                        u = np.clip(
                            elapsed / playback.duration_seconds, 0.0, 1.0
                        )
                        alpha = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
                        indices = list(playback.arm_micro_motion_active_indices)
                        projected = (
                            (1.0 - alpha) * playback.anchor[:14]
                            + alpha * playback.arm_micro_motion_end
                        )
                        sampled[indices] = projected[indices]
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
            tick_started_at = self._clock()
            try:
                self.tick_once(now=tick_started_at)
            except RobotControlError:
                logger.exception("Servo streamer faulted and stopped")
                break
            deadline += period
            # Never recover absolute phase with a sub-period command.  If this
            # tick woke late but did not overrun the *next* old deadline, the
            # previous implementation slept only the small remainder (for
            # example 16 ms followed by 4 ms).  Keep at least one complete
            # period between target sample times and drop the stale phase.
            deadline = max(deadline, tick_started_at + period)
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
                    reason = (
                        "100 Hz servo tick lateness exceeded watchdog: "
                        f"{lateness:.6f}s > "
                        f"{self._max_tick_lateness_seconds:.6f}s"
                    )
                    if self._timing_warning_callback is None:
                        self._fault_message = reason
                        self._fault.set()
                        logger.error(self._fault_message)
                        break
                    logger.warning("%s; warning-only, continuing", reason)
                    self._timing_warning_callback("servo_tick_overrun", lateness, self._max_tick_lateness_seconds)
                # A missed deadline must not be followed by an immediate
                # catch-up command.  The old reset-to-now behavior produced
                # long/near-zero interval pairs (for example 19.6 ms then
                # 0.36 ms) and physically dispatched a velocity spike.  Drop
                # the missed slot and establish a fresh full-period deadline.
                deadline = self._clock() + period
                self._sleep(period)
