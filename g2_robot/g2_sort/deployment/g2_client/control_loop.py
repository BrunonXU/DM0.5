"""Shadow、powered-sync和powered-async三种控制状态机。

用途：协调观测、DM0.5请求、安全门、chunk切换、hold和telemetry。
输入：``ObservationSource``、``PolicyBackend``、运行选项，以及powered所需robot/servo。
输出：shadow统计或100Hz本地动作播放；首个安全chunk前Servo零发送。
方法：sync执行配置的chunk前缀；async单请求预取、跳过陈旧前缀并用播放ID拒绝迟到响应；
激活后的错误路径才重复最后安全目标hold。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .contracts import (
    CHUNK_STEPS,
    MODEL_HZ,
    ActionChunk,
    ObservationError,
    PolicyError,
    RobotControlError,
)
from .interpolation import resample_action_intervals
from .joint_layout import POLICY_JOINT_NAMES, clip_action_positions
from .observation import ObservationSource
from .policy import PolicyBackend
from .prefetch import SingleFlightPrefetcher
from .servo import ServoStreamer
from .telemetry import DispatchTelemetry, Telemetry
from .trajectory_smoothing import ArmSmoothingStats, smooth_arm_action_chunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeOptions:
    mode: str = "shadow"
    execute_prefix_steps: int = 6
    chunk_prefetch_lookahead: int = 20
    async_min_fresh_steps: int = 12
    start_jump_limit_radians: float = 0.05
    boundary_blend_seconds: float = 0.06
    arm_internal_jerk_weight: float = 0.0
    observation_retry_seconds: float = 0.1
    max_requests: int = 0
    max_clip_delta_radians: float = 0.02
    max_arm_velocity_rad_s: float = 3.0
    max_hand_velocity_rad_s: float = 8.0
    max_arm_acceleration_rad_s2: float = 120.0
    max_hand_acceleration_rad_s2: float = 300.0
    max_jerk_rad_s3: float = 5000.0
    enforce_action_safety_gates: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"shadow", "powered-sync", "powered-async"}:
            raise ValueError(f"unsupported runtime mode {self.mode}")
        if not 1 <= self.execute_prefix_steps < CHUNK_STEPS:
            raise ValueError("execute_prefix_steps must be in [1, 49]")
        if not 1 <= self.chunk_prefetch_lookahead < CHUNK_STEPS:
            raise ValueError("chunk_prefetch_lookahead must be in [1, 49]")
        if not 1 <= self.async_min_fresh_steps <= CHUNK_STEPS:
            raise ValueError("async_min_fresh_steps must be in [1, 50]")
        if self.start_jump_limit_radians <= 0:
            raise ValueError("start_jump_limit_radians must be positive")
        if self.boundary_blend_seconds < 0:
            raise ValueError("boundary_blend_seconds cannot be negative")
        if not 0.0 <= self.arm_internal_jerk_weight <= 100.0:
            raise ValueError("arm_internal_jerk_weight must be in [0, 100]")
        if self.observation_retry_seconds < 0:
            raise ValueError("observation_retry_seconds cannot be negative")
        if self.max_requests < 0:
            raise ValueError("max_requests cannot be negative")
        for name in (
            "max_clip_delta_radians",
            "max_arm_velocity_rad_s",
            "max_hand_velocity_rad_s",
            "max_arm_acceleration_rad_s2",
            "max_hand_acceleration_rad_s2",
            "max_jerk_rad_s3",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class _PreparedChunk:
    actions: np.ndarray
    anchor_state: np.ndarray
    clip_count: int
    max_clip_delta: float
    start_jump: float
    start_jump_joint: str
    arm_start_jump: float
    arm_start_jump_joint: str
    hand_start_jump: float
    hand_start_jump_joint: str
    max_velocity: float
    max_velocity_joint: str
    max_acceleration: float
    max_acceleration_joint: str
    max_jerk: float
    max_jerk_joint: str
    smoothing: ArmSmoothingStats


class ControlLoop:
    """Coordinate observations, policy requests, safety gates, and the servo."""

    def __init__(
        self,
        *,
        options: RuntimeOptions,
        observations: ObservationSource,
        backend: PolicyBackend,
        telemetry: Telemetry,
        dispatch_telemetry: DispatchTelemetry | None = None,
        robot: object | None = None,
        servo: ServoStreamer | None = None,
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
    ) -> None:
        self.options = options
        self.observations = observations
        self.backend = backend
        self.telemetry = telemetry
        self.dispatch_telemetry = dispatch_telemetry
        self.robot = robot
        self.servo = servo
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self._pause_reported = False
        self._request_count = 0
        self._last_accepted_request_id = -1
        if options.mode == "shadow":
            if servo is not None:
                raise ValueError("shadow mode must not receive a ServoStreamer")
        elif robot is None or servo is None:
            raise ValueError("powered modes require robot and ServoStreamer")

    def run(self) -> dict[str, float | int]:
        try:
            if self.options.mode == "shadow":
                self._run_shadow()
            elif self.options.mode == "powered-sync":
                self._run_powered_sync()
            else:
                self._run_powered_async()
        finally:
            if self.dispatch_telemetry is not None:
                self.dispatch_telemetry.finish_segment(reason="run_finished")
        return self.telemetry.finish(self.options.mode)

    def _budget_available(self) -> bool:
        limit = self.options.max_requests
        return limit == 0 or self._request_count < limit

    def _capture_and_infer(self) -> ActionChunk | None:
        if not self._wait_until_resumed():
            return None
        try:
            observation = self.observations.capture()
        except Exception as exc:
            self.telemetry.emit("observation_invalid", reason=str(exc))
            return None
        self._request_count += 1
        try:
            chunk = self.backend.infer(observation)
        except Exception as exc:
            self.telemetry.emit(
                "policy_failure",
                request_id=observation.request_id,
                reason=str(exc),
            )
            return None
        if self.pause_event.is_set() or self.stop_event.is_set():
            self.telemetry.emit(
                "response_discarded",
                request_id=observation.request_id,
                reason="runtime paused or stopped while inference was in flight",
            )
            return None
        if chunk.request_id != observation.request_id:
            self.telemetry.emit(
                "response_out_of_order",
                request_id=chunk.request_id,
                expected_request_id=observation.request_id,
                reason="backend returned the wrong request ID",
            )
            return None
        return chunk

    def _run_shadow(self) -> None:
        while not self.stop_event.is_set() and self._budget_available():
            if not self._wait_until_resumed():
                break
            chunk = self._capture_and_infer()
            if chunk is None:
                self._retry_pause()
                continue
            clipped, clip_count, max_clip_delta = clip_action_positions(
                chunk.actions
            )
            clipped, smoothing = smooth_arm_action_chunk(
                clipped,
                jerk_weight=self.options.arm_internal_jerk_weight,
            )
            clipped, smoothing_clip_count, smoothing_clip_delta = (
                clip_action_positions(clipped)
            )
            clip_count += smoothing_clip_count
            max_clip_delta = max(max_clip_delta, smoothing_clip_delta)
            # Exercise the exact interpolation path without creating a sender.
            _, interpolated = resample_action_intervals(clipped)
            start_jump = None
            start_jump_joint = None
            arm_start_jump = None
            arm_start_jump_joint = None
            hand_start_jump = None
            hand_start_jump_joint = None
            try:
                state = self.observations.read_state()
                start_deltas = np.abs(clipped[0] - state)
                start_index = int(np.argmax(start_deltas))
                start_jump = float(start_deltas[start_index])
                start_jump_joint = POLICY_JOINT_NAMES[start_index]
                arm_index = int(np.argmax(start_deltas[:14]))
                arm_start_jump = float(start_deltas[arm_index])
                arm_start_jump_joint = POLICY_JOINT_NAMES[arm_index]
                hand_offset = 14
                hand_index = int(np.argmax(start_deltas[hand_offset:])) + hand_offset
                hand_start_jump = float(start_deltas[hand_index])
                hand_start_jump_joint = POLICY_JOINT_NAMES[hand_index]
            except Exception as exc:
                self.telemetry.emit(
                    "shadow_state_recheck_failed",
                    request_id=chunk.request_id,
                    reason=str(exc),
                )
            (
                max_velocity,
                max_velocity_joint,
                max_acceleration,
                max_acceleration_joint,
                max_jerk,
                max_jerk_joint,
                dynamic_error,
            ) = self._dynamic_metrics(clipped)
            if dynamic_error is not None:
                self.telemetry.emit(
                    "shadow_dynamics_warning",
                    request_id=chunk.request_id,
                    max_velocity_rad_s=max_velocity,
                    max_velocity_joint=max_velocity_joint,
                    max_acceleration_rad_s2=max_acceleration,
                    max_acceleration_joint=max_acceleration_joint,
                    max_jerk_rad_s3=max_jerk,
                    max_jerk_joint=max_jerk_joint,
                    reason=dynamic_error,
                )
            self.telemetry.record_chunk(
                mode="shadow",
                request_id=chunk.request_id,
                observation_at=chunk.observation_at,
                request_started_at=chunk.request_started_at,
                response_received_at=chunk.response_received_at,
                latency_seconds=chunk.latency_seconds,
                observation_age_seconds=max(
                    0.0, time.monotonic() - chunk.observation_at
                ),
                actions=chunk.actions,
                used_start_index=None,
                used_intervals=CHUNK_STEPS,
                skipped_steps=0,
                clip_count=clip_count,
                max_clip_delta=max_clip_delta,
                start_jump=start_jump,
                start_jump_joint=start_jump_joint,
                arm_start_jump=arm_start_jump,
                arm_start_jump_joint=arm_start_jump_joint,
                hand_start_jump=hand_start_jump,
                hand_start_jump_joint=hand_start_jump_joint,
                max_velocity=max_velocity,
                max_velocity_joint=max_velocity_joint,
                max_acceleration=max_acceleration,
                max_acceleration_joint=max_acceleration_joint,
                max_jerk=max_jerk,
                max_jerk_joint=max_jerk_joint,
                arm_internal_jerk_weight=smoothing.jerk_weight,
                arm_smoothing_delta_p95=smoothing.delta_p95_rad,
                arm_smoothing_max_delta=smoothing.max_delta_rad,
                arm_smoothing_max_delta_joint=(
                    None
                    if smoothing.max_delta_joint_index is None
                    else POLICY_JOINT_NAMES[smoothing.max_delta_joint_index]
                ),
                request_timing_seconds=chunk.timing_seconds,
            )

    def _run_powered_sync(self) -> None:
        assert self.servo is not None
        self.servo.start()
        while not self.stop_event.is_set() and self._budget_available():
            if not self._wait_until_resumed():
                break
            self.servo.hold()
            chunk = self._capture_and_infer()
            if chunk is None:
                self.servo.hold()
                self._retry_pause()
                continue
            if self.stop_event.is_set():
                self.servo.hold()
                break
            prepared = self._prepare_for_execution(
                chunk,
                start_index=0,
                interval_count=self.options.execute_prefix_steps,
            )
            if prepared is None:
                self.servo.hold()
                continue
            self._last_accepted_request_id = chunk.request_id
            self._begin_dispatch_segment(
                chunk.request_id,
                interval_count=self.options.execute_prefix_steps,
            )
            playback_id = self.servo.play(
                prepared.actions,
                start_index=0,
                interval_count=self.options.execute_prefix_steps,
                blend_seconds=self.options.boundary_blend_seconds,
                anchor=prepared.anchor_state,
            )
            self._record_prepared(
                chunk,
                prepared,
                start_index=0,
                used_intervals=self.options.execute_prefix_steps,
            )
            self._wait_for_playback(playback_id)
            self.servo.hold()
            self._finish_dispatch_segment("playback_completed")

    def _run_powered_async(self) -> None:
        assert self.servo is not None
        self.servo.start()
        prefetcher = SingleFlightPrefetcher(self.backend)
        current_playback_id: int | None = None
        need_sync_request = True
        try:
            while not self.stop_event.is_set():
                if not self._wait_until_resumed():
                    break
                if self.servo.has_fault():
                    self.telemetry.emit(
                        "servo_failure",
                        reason=self.servo.fault_message() or "unknown servo fault",
                    )
                    self.stop_event.set()
                    break

                ready = prefetcher.take_ready()
                if ready is not None:
                    if ready.error is not None or ready.chunk is None:
                        self._hold("async policy request failed: " + str(ready.error))
                        current_playback_id = None
                        need_sync_request = True
                    elif (
                        current_playback_id is None
                        or ready.playback_id != current_playback_id
                        or not self.servo.is_playing(current_playback_id)
                    ):
                        self._hold(
                            "late async response discarded after source chunk ended",
                            request_id=ready.chunk.request_id,
                        )
                        current_playback_id = None
                        need_sync_request = True
                    elif ready.chunk.request_id <= self._last_accepted_request_id:
                        self._hold(
                            "out-of-order async response discarded",
                            request_id=ready.chunk.request_id,
                        )
                        current_playback_id = None
                        need_sync_request = True
                    else:
                        now = time.monotonic()
                        stale_steps = ready.chunk.stale_prefix_steps(now)
                        fresh_steps = CHUNK_STEPS - stale_steps
                        if fresh_steps < self.options.async_min_fresh_steps:
                            self._hold(
                                f"async response has only {fresh_steps} fresh intervals",
                                request_id=ready.chunk.request_id,
                            )
                            current_playback_id = None
                            need_sync_request = True
                        else:
                            prepared = self._prepare_for_execution(
                                ready.chunk,
                                start_index=stale_steps,
                                interval_count=fresh_steps,
                            )
                            if prepared is None:
                                self._hold(
                                    "async response failed execution safety gates",
                                    request_id=ready.chunk.request_id,
                                )
                                current_playback_id = None
                                need_sync_request = True
                            else:
                                self._begin_dispatch_segment(
                                    ready.chunk.request_id,
                                    interval_count=fresh_steps,
                                )
                                current_playback_id = self.servo.play(
                                    prepared.actions,
                                    start_index=stale_steps,
                                    interval_count=fresh_steps,
                                    blend_seconds=self.options.boundary_blend_seconds,
                                    anchor=prepared.anchor_state,
                                )
                                self._last_accepted_request_id = (
                                    ready.chunk.request_id
                                )
                                self._record_prepared(
                                    ready.chunk,
                                    prepared,
                                    start_index=stale_steps,
                                    used_intervals=fresh_steps,
                                )
                                need_sync_request = False

                if need_sync_request:
                    if prefetcher.has_inflight():
                        self._retry_pause(short=True)
                        continue
                    if not self._budget_available():
                        break
                    chunk = self._capture_and_infer()
                    if chunk is None:
                        self._hold("synchronous recovery request failed")
                        self._retry_pause()
                        continue
                    if self.stop_event.is_set():
                        self._hold("stop requested during synchronous inference")
                        break
                    prepared = self._prepare_for_execution(
                        chunk,
                        start_index=0,
                        interval_count=CHUNK_STEPS,
                    )
                    if prepared is None:
                        self._hold(
                            "synchronous recovery chunk failed safety gates",
                            request_id=chunk.request_id,
                        )
                        continue
                    self._begin_dispatch_segment(
                        chunk.request_id,
                        interval_count=CHUNK_STEPS,
                    )
                    current_playback_id = self.servo.play(
                        prepared.actions,
                        start_index=0,
                        interval_count=CHUNK_STEPS,
                        blend_seconds=self.options.boundary_blend_seconds,
                        anchor=prepared.anchor_state,
                    )
                    self._last_accepted_request_id = chunk.request_id
                    self._record_prepared(
                        chunk,
                        prepared,
                        start_index=0,
                        used_intervals=CHUNK_STEPS,
                    )
                    need_sync_request = False
                    continue

                if (
                    current_playback_id is None
                    or not self.servo.is_playing(current_playback_id)
                ):
                    self._hold("active async chunk exhausted")
                    current_playback_id = None
                    need_sync_request = True
                    if not prefetcher.has_inflight() and not self._budget_available():
                        break
                    self._retry_pause(short=True)
                    continue

                if (
                    self.servo.remaining_intervals()
                    <= self.options.chunk_prefetch_lookahead
                    and prefetcher.is_idle()
                    and self._budget_available()
                ):
                    try:
                        observation = self.observations.capture()
                    except Exception as exc:
                        self._hold(f"async prefetch observation failed: {exc}")
                        current_playback_id = None
                        need_sync_request = True
                        continue
                    self._request_count += 1
                    if not prefetcher.submit(
                        observation, playback_id=current_playback_id
                    ):
                        self._hold("async single-flight invariant was violated")
                        current_playback_id = None
                        need_sync_request = True
                        continue
                    self.telemetry.emit(
                        "async_prefetch_started",
                        request_id=observation.request_id,
                        remaining_intervals=self.servo.remaining_intervals(),
                        playback_id=current_playback_id,
                        summary=(
                            f"lookahead={self.options.chunk_prefetch_lookahead}"
                        ),
                    )
                self._retry_pause(short=True)
        finally:
            self.servo.hold()
            prefetcher.close()

    def _prepare_for_execution(
        self,
        chunk: ActionChunk,
        *,
        start_index: int,
        interval_count: int,
    ) -> _PreparedChunk | None:
        assert self.robot is not None
        if chunk.request_id <= self._last_accepted_request_id:
            self.telemetry.emit(
                "response_out_of_order",
                request_id=chunk.request_id,
                last_accepted_request_id=self._last_accepted_request_id,
                reason="request ID is not newer than the last accepted chunk",
            )
            return None
        try:
            current_state = np.asarray(
                self.robot.read_state(), dtype=np.float64
            )
        except Exception as exc:
            self.telemetry.emit(
                "state_invalid_before_execute",
                request_id=chunk.request_id,
                reason=str(exc),
            )
            return None
        if current_state.shape != (chunk.actions.shape[1],) or not np.all(
            np.isfinite(current_state)
        ):
            self.telemetry.emit(
                "state_invalid_before_execute",
                request_id=chunk.request_id,
                reason=(
                    "measured state must be a finite 34D vector, got "
                    f"shape={current_state.shape}"
                ),
            )
            return None
        try:
            actions, clip_count, max_clip_delta = clip_action_positions(
                chunk.actions
            )
            actions, smoothing = smooth_arm_action_chunk(
                actions,
                jerk_weight=self.options.arm_internal_jerk_weight,
            )
            actions, smoothing_clip_count, smoothing_clip_delta = (
                clip_action_positions(actions)
            )
            clip_count += smoothing_clip_count
            max_clip_delta = max(max_clip_delta, smoothing_clip_delta)
        except Exception as exc:
            self.telemetry.emit(
                "action_safety_invalid",
                request_id=chunk.request_id,
                reason=str(exc),
            )
            return None
        if max_clip_delta > self.options.max_clip_delta_radians:
            self.telemetry.emit(
                (
                    "clip_delta_rejected"
                    if self.options.enforce_action_safety_gates
                    else "clip_delta_warning"
                ),
                request_id=chunk.request_id,
                max_limit_clip_delta=max_clip_delta,
                limit=self.options.max_clip_delta_radians,
                reason=(
                    f"limit clip delta {max_clip_delta:.6f} exceeds "
                    f"{self.options.max_clip_delta_radians:.6f} rad"
                ),
            )
            if self.options.enforce_action_safety_gates:
                return None
        start_deltas = np.abs(actions[start_index] - current_state)
        start_joint_index = int(np.argmax(start_deltas))
        start_jump = float(start_deltas[start_joint_index])
        start_jump_joint = POLICY_JOINT_NAMES[start_joint_index]
        arm_start_joint_index = int(np.argmax(start_deltas[:14]))
        arm_start_jump = float(start_deltas[arm_start_joint_index])
        arm_start_jump_joint = POLICY_JOINT_NAMES[arm_start_joint_index]
        hand_start_joint_index = int(np.argmax(start_deltas[14:])) + 14
        hand_start_jump = float(start_deltas[hand_start_joint_index])
        hand_start_jump_joint = POLICY_JOINT_NAMES[hand_start_joint_index]
        if start_jump > self.options.start_jump_limit_radians:
            self.telemetry.emit(
                (
                    "start_jump_rejected"
                    if self.options.enforce_action_safety_gates
                    else "start_jump_warning"
                ),
                request_id=chunk.request_id,
                max_start_jump=start_jump,
                joint_name=start_jump_joint,
                current=float(current_state[start_joint_index]),
                target=float(actions[start_index, start_joint_index]),
                delta=float(
                    actions[start_index, start_joint_index]
                    - current_state[start_joint_index]
                ),
                arm_start_jump_rad=arm_start_jump,
                arm_start_jump_joint=arm_start_jump_joint,
                hand_start_jump_rad=hand_start_jump,
                hand_start_jump_joint=hand_start_jump_joint,
                limit=self.options.start_jump_limit_radians,
                reason=(
                    f"start jump {start_jump:.6f} exceeds "
                    f"{self.options.start_jump_limit_radians:.6f} rad"
                ),
            )
            if self.options.enforce_action_safety_gates:
                return None
        end = min(len(actions), start_index + interval_count + 1)
        segment = actions[start_index:end]
        (
            max_velocity,
            max_velocity_joint,
            max_acceleration,
            max_acceleration_joint,
            max_jerk,
            max_jerk_joint,
            dynamic_error,
        ) = self._dynamic_metrics(segment)
        if dynamic_error is not None:
            self.telemetry.emit(
                (
                    "action_dynamics_rejected"
                    if self.options.enforce_action_safety_gates
                    else "action_dynamics_warning"
                ),
                request_id=chunk.request_id,
                max_velocity_rad_s=max_velocity,
                max_velocity_joint=max_velocity_joint,
                max_acceleration_rad_s2=max_acceleration,
                max_acceleration_joint=max_acceleration_joint,
                max_jerk_rad_s3=max_jerk,
                max_jerk_joint=max_jerk_joint,
                reason=dynamic_error,
            )
            if self.options.enforce_action_safety_gates:
                return None
        return _PreparedChunk(
            actions=actions,
            anchor_state=current_state.copy(),
            clip_count=clip_count,
            max_clip_delta=max_clip_delta,
            start_jump=start_jump,
            start_jump_joint=start_jump_joint,
            arm_start_jump=arm_start_jump,
            arm_start_jump_joint=arm_start_jump_joint,
            hand_start_jump=hand_start_jump,
            hand_start_jump_joint=hand_start_jump_joint,
            max_velocity=max_velocity,
            max_velocity_joint=max_velocity_joint,
            max_acceleration=max_acceleration,
            max_acceleration_joint=max_acceleration_joint,
            max_jerk=max_jerk,
            max_jerk_joint=max_jerk_joint,
            smoothing=smoothing,
        )

    def _record_prepared(
        self,
        chunk: ActionChunk,
        prepared: _PreparedChunk,
        *,
        start_index: int,
        used_intervals: int,
    ) -> None:
        self.telemetry.record_chunk(
            mode=self.options.mode,
            request_id=chunk.request_id,
            observation_at=chunk.observation_at,
            request_started_at=chunk.request_started_at,
            response_received_at=chunk.response_received_at,
            latency_seconds=chunk.latency_seconds,
            observation_age_seconds=max(
                0.0, time.monotonic() - chunk.observation_at
            ),
            actions=chunk.actions,
            used_start_index=start_index,
            used_intervals=used_intervals,
            skipped_steps=start_index,
            clip_count=prepared.clip_count,
            max_clip_delta=prepared.max_clip_delta,
            start_jump=prepared.start_jump,
            start_jump_joint=prepared.start_jump_joint,
            arm_start_jump=prepared.arm_start_jump,
            arm_start_jump_joint=prepared.arm_start_jump_joint,
            hand_start_jump=prepared.hand_start_jump,
            hand_start_jump_joint=prepared.hand_start_jump_joint,
            max_velocity=prepared.max_velocity,
            max_velocity_joint=prepared.max_velocity_joint,
            max_acceleration=prepared.max_acceleration,
            max_acceleration_joint=prepared.max_acceleration_joint,
            max_jerk=prepared.max_jerk,
            max_jerk_joint=prepared.max_jerk_joint,
            arm_internal_jerk_weight=prepared.smoothing.jerk_weight,
            arm_smoothing_delta_p95=prepared.smoothing.delta_p95_rad,
            arm_smoothing_max_delta=prepared.smoothing.max_delta_rad,
            arm_smoothing_max_delta_joint=(
                None
                if prepared.smoothing.max_delta_joint_index is None
                else POLICY_JOINT_NAMES[
                    prepared.smoothing.max_delta_joint_index
                ]
            ),
            request_timing_seconds=chunk.timing_seconds,
        )

    def _wait_for_playback(self, playback_id: int) -> None:
        assert self.servo is not None
        while (
            not self.stop_event.is_set()
            and not self.pause_event.is_set()
            and self.servo.is_playing(playback_id)
            and not self.servo.has_fault()
        ):
            time.sleep(0.005)
        if self.servo.has_fault():
            self.telemetry.emit(
                "servo_failure",
                reason=self.servo.fault_message() or "unknown servo fault",
            )
            self.stop_event.set()
        if self.pause_event.is_set():
            self.servo.hold()

    def _hold(self, reason: str, request_id: int | None = None) -> None:
        if self.servo is not None:
            self.servo.hold()
        self._finish_dispatch_segment("safe_hold")
        fields: dict[str, object] = {"reason": reason}
        if request_id is not None:
            fields["request_id"] = request_id
        self.telemetry.emit("safe_hold", **fields)

    def _begin_dispatch_segment(
        self,
        request_id: int,
        *,
        interval_count: int,
    ) -> None:
        started_at = time.monotonic()
        if self.robot is not None:
            begin_hand_segment = getattr(
                self.robot, "begin_hand_segment", None
            )
            if begin_hand_segment is not None:
                begin_hand_segment(started_at=started_at)
        if self.dispatch_telemetry is None:
            return
        self.dispatch_telemetry.begin_segment(
            mode=self.options.mode,
            request_id=request_id,
            blend_seconds=self.options.boundary_blend_seconds,
            expected_duration_seconds=interval_count / MODEL_HZ,
            started_at=started_at,
        )

    def _finish_dispatch_segment(self, reason: str) -> None:
        if self.dispatch_telemetry is not None:
            self.dispatch_telemetry.finish_segment(reason=reason)

    def _retry_pause(self, *, short: bool = False) -> None:
        delay = 0.005 if short else self.options.observation_retry_seconds
        self.stop_event.wait(max(0.0, delay))

    def _wait_until_resumed(self) -> bool:
        if not self.pause_event.is_set():
            if self._pause_reported:
                self.telemetry.emit(
                    "runtime_resumed",
                    reason="fresh observation required after resume",
                )
                self._pause_reported = False
            return not self.stop_event.is_set()
        if self.servo is not None:
            self.servo.hold()
        self._finish_dispatch_segment("operator_pause")
        if not self._pause_reported:
            self.telemetry.emit(
                "runtime_paused",
                reason="operator pause; active chunk discarded and hold engaged",
            )
            self._pause_reported = True
        while self.pause_event.is_set() and not self.stop_event.wait(0.05):
            if self.servo is not None and self.servo.has_fault():
                self.stop_event.set()
                return False
        return not self.stop_event.is_set()

    def _dynamic_metrics(
        self,
        segment: np.ndarray,
    ) -> tuple[float, str, float, str, float, str, str | None]:
        def maximum(values: np.ndarray) -> tuple[float, int]:
            if values.size == 0:
                return 0.0, 0
            per_joint = np.max(np.abs(values), axis=0)
            index = int(np.argmax(per_joint))
            return float(per_joint[index]), index

        velocity = np.diff(segment, axis=0) * MODEL_HZ
        acceleration = np.diff(segment, n=2, axis=0) * (MODEL_HZ**2)
        jerk = np.diff(segment, n=3, axis=0) * (MODEL_HZ**3)
        max_velocity, velocity_index = maximum(velocity)
        max_acceleration, acceleration_index = maximum(acceleration)
        max_jerk, jerk_index = maximum(jerk)
        arm_velocity = (
            float(np.max(np.abs(velocity[:, :14]))) if velocity.size else 0.0
        )
        hand_velocity = (
            float(np.max(np.abs(velocity[:, 14:]))) if velocity.size else 0.0
        )
        arm_acceleration = (
            float(np.max(np.abs(acceleration[:, :14])))
            if acceleration.size
            else 0.0
        )
        hand_acceleration = (
            float(np.max(np.abs(acceleration[:, 14:])))
            if acceleration.size
            else 0.0
        )
        failures: list[str] = []
        if arm_velocity > self.options.max_arm_velocity_rad_s:
            failures.append(
                f"arm velocity {arm_velocity:.3f} exceeds "
                f"{self.options.max_arm_velocity_rad_s:.3f} rad/s"
            )
        if hand_velocity > self.options.max_hand_velocity_rad_s:
            failures.append(
                f"hand velocity {hand_velocity:.3f} exceeds "
                f"{self.options.max_hand_velocity_rad_s:.3f} rad/s"
            )
        if arm_acceleration > self.options.max_arm_acceleration_rad_s2:
            failures.append(
                f"arm acceleration {arm_acceleration:.3f} exceeds "
                f"{self.options.max_arm_acceleration_rad_s2:.3f} rad/s^2"
            )
        if hand_acceleration > self.options.max_hand_acceleration_rad_s2:
            failures.append(
                f"hand acceleration {hand_acceleration:.3f} exceeds "
                f"{self.options.max_hand_acceleration_rad_s2:.3f} rad/s^2"
            )
        if max_jerk > self.options.max_jerk_rad_s3:
            failures.append(
                f"jerk {max_jerk:.3f} exceeds "
                f"{self.options.max_jerk_rad_s3:.3f} rad/s^3"
            )
        return (
            max_velocity,
            POLICY_JOINT_NAMES[velocity_index],
            max_acceleration,
            POLICY_JOINT_NAMES[acceleration_index],
            max_jerk,
            POLICY_JOINT_NAMES[jerk_index],
            "; ".join(failures) if failures else None,
        )
