"""Shadow、同步、RTC和实验异步控制状态机。

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
    ACTION_DIM,
    CHUNK_STEPS,
    MODEL_HZ,
    ActionChunk,
    ObservationError,
    PolicyError,
    RobotControlError,
)
from .interpolation import arm_cubic_spline_tangents, resample_action_intervals
from .joint_layout import POLICY_JOINT_NAMES, clip_action_positions
from .observation import ObservationSource
from .policy import InferencePipeline, PolicyBackend
from .prefetch import SingleFlightPrefetcher
from .servo import ServoStreamer
from .telemetry import DispatchTelemetry, Telemetry
from .trajectory_smoothing import (
    ArmSmoothingStats,
    smooth_arm_action_chunk,
    smooth_rtc_arm_action_suffix,
)
from .rtc import (
    RTCParameters,
    RTCRequest,
    conservative_delay_steps,
    project_absolute_rtc_prefix,
    request_from_committed_chunk,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeOptions:
    mode: str = "shadow"
    execute_prefix_steps: int = 6
    chunk_prefetch_lookahead: int = 20
    async_min_fresh_steps: int = 12
    rtc_predicted_delay_steps: int = 20
    rtc_execution_horizon: int = 25
    start_jump_limit_radians: float = 0.05
    hand_start_jump_limit_radians: float = 0.35
    boundary_blend_seconds: float = 0.06
    boundary_blend_profile: str = "linear"
    arm_interpolation_profile: str = "linear"
    arm_internal_jerk_weight: float = 0.0
    arm_micro_motion_profile: str = "off"
    arm_micro_motion_threshold_rad: float = 0.0035
    observation_retry_seconds: float = 0.1
    max_requests: int = 0
    max_clip_delta_radians: float = 0.02
    max_arm_velocity_rad_s: float = 3.0
    max_hand_velocity_rad_s: float = 8.0
    max_arm_acceleration_rad_s2: float = 120.0
    max_hand_acceleration_rad_s2: float = 300.0
    max_jerk_rad_s3: float = 5000.0
    enforce_action_safety_gates: bool = True
    executed_action_dim: int = ACTION_DIM

    def __post_init__(self) -> None:
        if self.mode not in {
            "shadow",
            "rtc-shadow",
            "powered-rtc",
            "powered-sync",
            "powered-async",
        }:
            raise ValueError(f"unsupported runtime mode {self.mode}")
        if not 1 <= self.execute_prefix_steps < CHUNK_STEPS:
            raise ValueError("execute_prefix_steps must be in [1, 49]")
        if not 1 <= self.chunk_prefetch_lookahead < CHUNK_STEPS:
            raise ValueError("chunk_prefetch_lookahead must be in [1, 49]")
        if not 1 <= self.async_min_fresh_steps <= CHUNK_STEPS:
            raise ValueError("async_min_fresh_steps must be in [1, 50]")
        RTCParameters(
            predicted_delay_steps=self.rtc_predicted_delay_steps,
            execution_horizon=self.rtc_execution_horizon,
        )
        if self.start_jump_limit_radians <= 0:
            raise ValueError("start_jump_limit_radians must be positive")
        if self.hand_start_jump_limit_radians <= 0:
            raise ValueError("hand_start_jump_limit_radians must be positive")
        if self.boundary_blend_seconds < 0:
            raise ValueError("boundary_blend_seconds cannot be negative")
        if self.boundary_blend_profile not in {
            "linear", "cubic_smoothstep", "quintic_smootherstep"
        }:
            raise ValueError("unsupported boundary_blend_profile")
        if self.arm_interpolation_profile not in {
            "linear",
            "cubic_hermite",
            "cubic_spline",
        }:
            raise ValueError(
                "arm_interpolation_profile must be linear, cubic_hermite, "
                "or cubic_spline"
            )
        if not 0.0 <= self.arm_internal_jerk_weight <= 100.0:
            raise ValueError("arm_internal_jerk_weight must be in [0, 100]")
        if self.arm_micro_motion_profile not in {
            "off",
            "stationary_minjerk_v1",
        }:
            raise ValueError("unsupported arm_micro_motion_profile")
        if (
            not np.isfinite(self.arm_micro_motion_threshold_rad)
            or self.arm_micro_motion_threshold_rad <= 0.0
        ):
            raise ValueError(
                "arm_micro_motion_threshold_rad must be finite and positive"
            )
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
        if self.mode == "powered-rtc" and not self.enforce_action_safety_gates:
            raise ValueError("powered RTC requires hard action safety gates")
        if self.executed_action_dim not in {14, ACTION_DIM}:
            raise ValueError("executed_action_dim must be 14 or 34")


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
    arm_start_velocity_per_step: np.ndarray | None


class ControlLoop:
    """Coordinate observations, policy requests, safety gates, and the servo."""

    def __init__(
        self,
        *,
        options: RuntimeOptions,
        observations: ObservationSource,
        backend: PolicyBackend,
        inference_pipeline: InferencePipeline | None = None,
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
        self.inference_pipeline = inference_pipeline
        self.telemetry = telemetry
        self.dispatch_telemetry = dispatch_telemetry
        self.robot = robot
        self.servo = servo
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self._pause_reported = False
        self._request_count = 0
        self._last_accepted_request_id = -1
        if options.mode in {"shadow", "rtc-shadow"}:
            if servo is not None:
                raise ValueError("shadow mode must not receive a ServoStreamer")
        elif robot is None or servo is None:
            raise ValueError("powered modes require robot and ServoStreamer")

    def _postprocess_action_chunk(
        self,
        actions_input: np.ndarray,
        *,
        start_index: int,
        rtc_prefix_absolute: np.ndarray | None = None,
        rtc_inference_delay: int | None = None,
    ) -> tuple[
        np.ndarray,
        int,
        float,
        ArmSmoothingStats,
        np.ndarray | None,
    ]:
        """Apply the exact arm postprocess shared by Shadow and Powered."""

        raw_actions = np.asarray(actions_input, dtype=np.float64)
        actions, _, _ = clip_action_positions(raw_actions)
        executed = self.options.executed_action_dim
        clip_delta = np.abs(actions[:, :executed] - raw_actions[:, :executed])
        clip_count = int(np.count_nonzero(clip_delta))
        max_clip_delta = float(clip_delta.max()) if clip_delta.size else 0.0
        arm_start_velocity_per_step: np.ndarray | None = None
        if rtc_prefix_absolute is not None:
            if rtc_inference_delay is None:
                raise ValueError("RTC projection requires inference delay")
            if rtc_inference_delay != start_index:
                raise ValueError("RTC projection delay must equal the switch index")
            actions = project_absolute_rtc_prefix(
                actions,
                rtc_prefix_absolute,
                inference_delay=rtc_inference_delay,
            )
            if (
                self.options.arm_interpolation_profile == "cubic_spline"
                and 0 < rtc_inference_delay < len(rtc_prefix_absolute) - 1
            ):
                boundary_velocity = arm_cubic_spline_tangents(
                    rtc_prefix_absolute,
                )[rtc_inference_delay]
            elif (
                self.options.arm_interpolation_profile == "cubic_hermite"
                and 0 < rtc_inference_delay < len(rtc_prefix_absolute) - 1
            ):
                boundary_velocity = 0.5 * (
                    rtc_prefix_absolute[rtc_inference_delay + 1, :14]
                    - rtc_prefix_absolute[rtc_inference_delay - 1, :14]
                )
            elif rtc_inference_delay > 0:
                boundary_velocity = (
                    rtc_prefix_absolute[rtc_inference_delay, :14]
                    - rtc_prefix_absolute[rtc_inference_delay - 1, :14]
                )
            else:
                boundary_velocity = (
                    rtc_prefix_absolute[1, :14]
                    - rtc_prefix_absolute[0, :14]
                )
            arm_start_velocity_per_step = boundary_velocity.copy()
            actions, smoothing = smooth_rtc_arm_action_suffix(
                actions,
                start_index=start_index,
                boundary_arm_position=rtc_prefix_absolute[start_index, :14],
                boundary_arm_velocity_per_step=boundary_velocity,
                jerk_weight=self.options.arm_internal_jerk_weight,
            )
        else:
            actions, smoothing = smooth_arm_action_chunk(
                actions,
                jerk_weight=self.options.arm_internal_jerk_weight,
            )
        before_final_clip = actions
        actions, _, _ = clip_action_positions(before_final_clip)
        extra_clip_delta = np.abs(
            actions[:, :executed] - before_final_clip[:, :executed]
        )
        extra_clip_count = int(np.count_nonzero(extra_clip_delta))
        extra_clip_max = (
            float(extra_clip_delta.max()) if extra_clip_delta.size else 0.0
        )
        return (
            actions,
            clip_count + extra_clip_count,
            max(max_clip_delta, extra_clip_max),
            smoothing,
            arm_start_velocity_per_step,
        )

    def run(self) -> dict[str, float | int]:
        try:
            if self.options.mode == "shadow":
                self._run_shadow()
            elif self.options.mode == "rtc-shadow":
                self._run_rtc_shadow()
            elif self.options.mode == "powered-rtc":
                self._run_powered_rtc()
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

    def _capture_and_infer(
        self,
        rtc_request: RTCRequest | None = None,
    ) -> ActionChunk | None:
        if not self._wait_until_resumed():
            return None
        if self.inference_pipeline is not None:
            expected_request_id = self._request_count
            self._request_count += 1
            try:
                chunk = self.inference_pipeline.infer(rtc_request)
            except Exception as exc:
                self.telemetry.emit(
                    "policy_failure",
                    request_id=expected_request_id,
                    reason=str(exc),
                )
                return None
            if self.pause_event.is_set() or self.stop_event.is_set():
                self.telemetry.emit(
                    "response_discarded",
                    request_id=chunk.request_id,
                    reason="runtime paused or stopped while inference was in flight",
                )
                return None
            if chunk.request_id != expected_request_id:
                self.telemetry.emit(
                    "response_out_of_order",
                    request_id=chunk.request_id,
                    expected_request_id=expected_request_id,
                    reason="process worker returned the wrong request ID",
                )
                return None
            return chunk
        try:
            observation = self.observations.capture()
        except Exception as exc:
            self.telemetry.emit("observation_invalid", reason=str(exc))
            return None
        self._request_count += 1
        try:
            chunk = (
                self.backend.infer(observation)
                if rtc_request is None
                else self.backend.infer(observation, rtc_request)
            )
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

    def _run_rtc_shadow(self) -> None:
        """Exercise RTC HTTP/model/timing without creating a ServoStreamer."""

        parameters = RTCParameters(
            predicted_delay_steps=self.options.rtc_predicted_delay_steps,
            execution_horizon=self.options.rtc_execution_horizon,
        )
        committed: np.ndarray | None = None
        wait_steps = parameters.execution_horizon
        while not self.stop_event.is_set() and self._budget_available():
            if committed is not None:
                if self.stop_event.wait(wait_steps / MODEL_HZ):
                    break
            if not self._wait_until_resumed():
                break
            try:
                observation = self.observations.capture()
            except Exception as exc:
                self.telemetry.emit("observation_invalid", reason=str(exc))
                self._retry_pause()
                continue
            self._request_count += 1
            rtc_request = (
                None
                if committed is None
                else request_from_committed_chunk(committed, parameters)
            )
            try:
                if rtc_request is None:
                    chunk = self.backend.infer(observation)
                else:
                    chunk = self.backend.infer(observation, rtc_request)
            except Exception as exc:
                self.telemetry.emit(
                    "policy_failure",
                    request_id=observation.request_id,
                    reason=str(exc),
                )
                self._retry_pause()
                continue
            if chunk.request_id != observation.request_id:
                self.telemetry.emit(
                    "response_out_of_order",
                    request_id=chunk.request_id,
                    expected_request_id=observation.request_id,
                    reason="RTC backend returned the wrong request ID",
                )
                continue

            real_delay = conservative_delay_steps(
                max(0.0, chunk.response_received_at - chunk.observation_at)
            )
            switch_delay = (
                0
                if rtc_request is None
                else max(real_delay, parameters.predicted_delay_steps)
            )
            feasible = (
                rtc_request is None
                or (
                    switch_delay < len(rtc_request.prefix_actions_absolute)
                    and parameters.execution_horizon + switch_delay <= CHUNK_STEPS
                )
            )
            if not feasible:
                self.telemetry.emit(
                    "rtc_shadow_result",
                    request_id=chunk.request_id,
                    predicted_delay_steps=parameters.predicted_delay_steps,
                    real_delay_steps=real_delay,
                    switch_delay_steps=switch_delay,
                    execution_horizon=parameters.execution_horizon,
                    schedule_feasible=False,
                    summary=(
                        f"d预测={parameters.predicted_delay_steps} d实际={real_delay} "
                        f"切换={switch_delay} s={parameters.execution_horizon} 可行=False"
                    ),
                )
                committed = None
                wait_steps = parameters.execution_horizon
                continue
            try:
                (
                    clipped,
                    clip_count,
                    max_clip_delta,
                    smoothing,
                    _arm_start_velocity,
                ) = (
                    self._postprocess_action_chunk(
                        chunk.actions,
                        start_index=switch_delay,
                        rtc_prefix_absolute=(
                            None
                            if rtc_request is None
                            else rtc_request.prefix_actions_absolute
                        ),
                        rtc_inference_delay=(
                            None if rtc_request is None else switch_delay
                        ),
                    )
                )
            except Exception as exc:
                self.telemetry.emit(
                    "action_safety_invalid",
                    request_id=chunk.request_id,
                    reason=str(exc),
                )
                committed = None
                wait_steps = parameters.execution_horizon
                continue
            _, interpolated = resample_action_intervals(clipped)
            if max_clip_delta > self.options.max_clip_delta_radians:
                self.telemetry.emit(
                    "shadow_clip_delta_warning",
                    request_id=chunk.request_id,
                    max_limit_clip_delta=max_clip_delta,
                    limit=self.options.max_clip_delta_radians,
                    reason=(
                        f"limit clip delta {max_clip_delta:.6f} exceeds "
                        f"{self.options.max_clip_delta_radians:.6f} rad"
                    ),
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
            used_start = switch_delay
            used_intervals = max(0, CHUNK_STEPS - used_start)
            if dynamic_error is not None:
                self.telemetry.emit(
                    "shadow_dynamics_warning",
                    request_id=chunk.request_id,
                    reason=dynamic_error,
                )
            self.telemetry.record_chunk(
                mode="rtc-shadow",
                request_id=chunk.request_id,
                observation_at=chunk.observation_at,
                request_started_at=chunk.request_started_at,
                response_received_at=chunk.response_received_at,
                latency_seconds=chunk.latency_seconds,
                observation_age_seconds=max(
                    0.0, chunk.response_received_at - chunk.observation_at
                ),
                actions=chunk.actions,
                used_start_index=used_start,
                used_intervals=used_intervals,
                skipped_steps=used_start,
                clip_count=clip_count,
                max_clip_delta=max_clip_delta,
                start_jump=None,
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
            if rtc_request is not None:
                prefix = rtc_request.prefix_actions_absolute
                overlap = min(real_delay, len(prefix))
                old_index = parameters.execution_horizon + switch_delay
                new_index = switch_delay
                arm_seam = np.abs(
                    clipped[new_index, :14] - committed[old_index, :14]
                )
                hand_seam = np.abs(
                    clipped[new_index, 14:] - committed[old_index, 14:]
                )
                old_arm_velocity = (
                    committed[old_index, :14] - committed[old_index - 1, :14]
                )
                new_arm_velocity = (
                    clipped[new_index + 1, :14] - clipped[new_index, :14]
                )
                arm_velocity_jump = np.abs(new_arm_velocity - old_arm_velocity)
                direction_reversals = int(
                    np.count_nonzero(
                        (np.abs(old_arm_velocity) > 1e-9)
                        & (np.abs(new_arm_velocity) > 1e-9)
                        & (old_arm_velocity * new_arm_velocity < 0.0)
                    )
                )
                fields = {
                    "request_id": chunk.request_id,
                    "predicted_delay_steps": parameters.predicted_delay_steps,
                    "real_delay_steps": real_delay,
                    "switch_delay_steps": switch_delay,
                    "response_buffered_steps": switch_delay - real_delay,
                    "execution_horizon": parameters.execution_horizon,
                    "schedule_feasible": feasible,
                    "interpolated_servo_samples": len(interpolated),
                    "arm_seam_mae_rad": float(arm_seam.mean()),
                    "arm_seam_max_rad": float(arm_seam.max()),
                    "hand_seam_mae_rad": float(hand_seam.mean()),
                    "hand_seam_max_rad": float(hand_seam.max()),
                    "arm_velocity_jump_max_rad_s": float(
                        arm_velocity_jump.max() * MODEL_HZ
                    ),
                    "arm_direction_reversal_count": direction_reversals,
                }
                if overlap:
                    fields.update(
                        {
                            "hard_prefix_arm_mae": float(
                                np.abs(clipped[:overlap, :14] - prefix[:overlap, :14]).mean()
                            ),
                            "hard_prefix_hand_mae": float(
                                np.abs(clipped[:overlap, 14:] - prefix[:overlap, 14:]).mean()
                            ),
                        }
                    )
                self.telemetry.emit(
                    "rtc_shadow_result",
                    **fields,
                    summary=(
                        f"d预测={parameters.predicted_delay_steps} d实际={real_delay} "
                        f"切换={switch_delay} s={parameters.execution_horizon} "
                        f"可行={feasible}"
                    ),
                )
            committed = clipped
            wait_steps = (
                parameters.execution_horizon
                if rtc_request is None
                else max(0, parameters.execution_horizon - real_delay)
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
                actions=prepared.actions,
                start_index=0,
                anchor=prepared.anchor_state,
            )
            playback_id = self.servo.play(
                prepared.actions,
                start_index=0,
                interval_count=self.options.execute_prefix_steps,
                blend_seconds=self.options.boundary_blend_seconds,
                anchor=prepared.anchor_state,
            )
            self._record_arm_micro_motion(chunk.request_id, playback_id)
            self._record_prepared(
                chunk,
                prepared,
                start_index=0,
                used_intervals=self.options.execute_prefix_steps,
            )
            self._wait_for_playback(playback_id)
            self.servo.hold()
            self._finish_dispatch_segment("playback_completed")

    def _run_powered_rtc(self) -> None:
        # TODO(RTC_UNIFIED_34D_MIGRATION): migrate RTC hardware output to canonical single-34D normal Servo at 100Hz; preserve one Control Robot owner. Pending implementation/validation; current RTC behavior unchanged.
        """Continuously replan while the previously committed chunk keeps moving."""

        assert self.servo is not None
        parameters = RTCParameters(
            predicted_delay_steps=self.options.rtc_predicted_delay_steps,
            execution_horizon=self.options.rtc_execution_horizon,
        )
        self.servo.start()

        # Bootstrap has no prior trajectory to constrain, so it is one ordinary
        # request made before the first Servo target is activated.
        bootstrap = self._capture_and_infer()
        if bootstrap is None or self.stop_event.is_set():
            return
        prepared = self._prepare_for_execution(
            bootstrap,
            start_index=0,
            interval_count=CHUNK_STEPS,
        )
        if prepared is None:
            return
        self._last_accepted_request_id = bootstrap.request_id
        self._begin_dispatch_segment(
            bootstrap.request_id,
            interval_count=CHUNK_STEPS,
            actions=prepared.actions,
            start_index=0,
            anchor=prepared.anchor_state,
        )
        playback_id = self.servo.play(
            prepared.actions,
            start_index=0,
            interval_count=CHUNK_STEPS,
            blend_seconds=self.options.boundary_blend_seconds,
            anchor=prepared.anchor_state,
        )
        self._record_arm_micro_motion(bootstrap.request_id, playback_id)
        self._record_prepared(
            bootstrap,
            prepared,
            start_index=0,
            used_intervals=CHUNK_STEPS,
        )
        committed = prepared.actions

        try:
            while not self.stop_event.is_set() and self._budget_available():
                if not self._wait_until_rtc_request_point(
                    playback_id,
                    remaining_intervals=(
                        CHUNK_STEPS - parameters.execution_horizon
                    ),
                ):
                    break
                if not self._wait_until_resumed():
                    break
                rtc_request = request_from_committed_chunk(
                    committed,
                    parameters,
                )
                expected_request_id = self._request_count
                self.telemetry.emit(
                    "rtc_request_started",
                    request_id=expected_request_id,
                    source_playback_id=playback_id,
                    predicted_delay_steps=parameters.predicted_delay_steps,
                    execution_horizon=parameters.execution_horizon,
                    remaining_intervals=self.servo.remaining_intervals(),
                    summary=(
                        f"旧chunk继续执行；d={parameters.predicted_delay_steps} "
                        f"s={parameters.execution_horizon}"
                    ),
                )
                chunk = self._capture_and_infer(rtc_request)
                if chunk is None:
                    self._hold("RTC observation or policy request failed")
                    self.stop_event.set()
                    break

                if chunk.request_id != expected_request_id:
                    self._hold(
                        "RTC backend returned the wrong request ID",
                        request_id=chunk.request_id,
                    )
                    self.stop_event.set()
                    break
                if self.pause_event.is_set() or self.stop_event.is_set():
                    self._hold("RTC response discarded after pause or stop")
                    break

                real_delay = conservative_delay_steps(
                    max(0.0, chunk.response_received_at - chunk.observation_at)
                )
                max_delay = CHUNK_STEPS - parameters.execution_horizon
                if (
                    real_delay > max_delay
                    or not self.servo.is_playing(playback_id)
                ):
                    self._hold(
                        "RTC response missed the committed old-chunk window",
                        request_id=chunk.request_id,
                    )
                    self.telemetry.emit(
                        "rtc_schedule_missed",
                        request_id=chunk.request_id,
                        real_delay_steps=real_delay,
                        max_delay_steps=max_delay,
                        source_playback_id=playback_id,
                        latency_seconds=chunk.latency_seconds,
                        observation_age_seconds=max(
                            0.0,
                            chunk.response_received_at
                            - chunk.observation_at,
                        ),
                        request_timing_seconds=chunk.timing_seconds,
                        summary=(
                            f"d实际={real_delay} > 可续接上限={max_delay}，已保持"
                        ),
                    )
                    self.stop_event.set()
                    break

                # The server generated this chunk for the configured delay d.
                # If the response arrives early, keep executing the committed
                # old chunk until that same logical index instead of switching
                # a d=20 trajectory at index 14--16.
                switch_delay = max(
                    real_delay,
                    parameters.predicted_delay_steps,
                )
                early_wait_steps = switch_delay - real_delay
                if early_wait_steps:
                    switch_remaining = (
                        CHUNK_STEPS
                        - parameters.execution_horizon
                        - switch_delay
                    )
                    self.telemetry.emit(
                        "rtc_response_buffered",
                        request_id=chunk.request_id,
                        real_delay_steps=real_delay,
                        switch_delay_steps=switch_delay,
                        buffered_steps=early_wait_steps,
                        summary=(
                            f"响应提前{early_wait_steps}步；旧chunk继续到d={switch_delay}"
                        ),
                    )
                    if not self._wait_until_rtc_request_point(
                        playback_id,
                        remaining_intervals=switch_remaining,
                    ):
                        break

                prepared = self._prepare_for_execution(
                    chunk,
                    start_index=switch_delay,
                    interval_count=CHUNK_STEPS - switch_delay,
                    rtc_prefix_absolute=rtc_request.prefix_actions_absolute,
                    rtc_inference_delay=switch_delay,
                )
                if prepared is None:
                    self._hold(
                        "RTC response failed execution checks",
                        request_id=chunk.request_id,
                    )
                    self.stop_event.set()
                    break

                old_index = min(
                    CHUNK_STEPS - 1,
                    parameters.execution_horizon + switch_delay,
                )
                new_index = switch_delay
                seam = np.abs(
                    prepared.actions[new_index] - committed[old_index]
                )
                source_playback_id = playback_id
                self._finish_dispatch_segment("rtc_replanned")
                self._begin_dispatch_segment(
                    chunk.request_id,
                    interval_count=CHUNK_STEPS - switch_delay,
                    actions=prepared.actions,
                    start_index=switch_delay,
                    anchor=prepared.anchor_state,
                    blend_seconds=(
                        0.0
                        if self.options.arm_interpolation_profile != "linear"
                        else self.options.boundary_blend_seconds
                    ),
                )
                playback_id = self.servo.play(
                    prepared.actions,
                    start_index=switch_delay,
                    interval_count=CHUNK_STEPS - switch_delay,
                    blend_seconds=(
                        0.0
                        if self.options.arm_interpolation_profile != "linear"
                        else self.options.boundary_blend_seconds
                    ),
                    anchor=prepared.anchor_state,
                    arm_start_velocity_per_step=(
                        prepared.arm_start_velocity_per_step
                    ),
                )
                self._record_arm_micro_motion(chunk.request_id, playback_id)
                self._last_accepted_request_id = chunk.request_id
                self._record_prepared(
                    chunk,
                    prepared,
                    start_index=switch_delay,
                    used_intervals=CHUNK_STEPS - switch_delay,
                )
                self.telemetry.emit(
                    "rtc_switch",
                    request_id=chunk.request_id,
                    source_playback_id=source_playback_id,
                    playback_id=playback_id,
                    predicted_delay_steps=parameters.predicted_delay_steps,
                    real_delay_steps=real_delay,
                    switch_delay_steps=switch_delay,
                    response_buffered_steps=early_wait_steps,
                    execution_horizon=parameters.execution_horizon,
                    old_switch_index=old_index,
                    new_start_index=new_index,
                    arm_seam_mae_rad=float(seam[:14].mean()),
                    arm_seam_max_rad=float(seam[:14].max()),
                    hand_seam_mae_rad=float(seam[14:].mean()),
                    hand_seam_max_rad=float(seam[14:].max()),
                    summary=(
                        f"d实际={real_delay} 旧索引={old_index} "
                        f"新索引={new_index} 双臂接缝max={seam[:14].max():.4f}rad "
                        f"双手接缝max={seam[14:].max():.4f}rad"
                    ),
                )
                committed = prepared.actions
        finally:
            self.servo.hold()
            self._finish_dispatch_segment("run_finished")

    def _wait_until_rtc_request_point(
        self,
        playback_id: int,
        *,
        remaining_intervals: int,
    ) -> bool:
        """Wait for logical index ``s`` while checking pause/stop/Servo faults."""

        assert self.servo is not None
        while (
            not self.stop_event.is_set()
            and not self.pause_event.is_set()
            and self.servo.is_playing(playback_id)
            and self.servo.remaining_intervals() > remaining_intervals
            and not self.servo.has_fault()
        ):
            time.sleep(0.005)
        if self.servo.has_fault():
            self.telemetry.emit(
                "servo_failure",
                reason=self.servo.fault_message() or "unknown servo fault",
            )
            self.stop_event.set()
            return False
        if self.pause_event.is_set():
            self.servo.hold()
            return False
        if not self.servo.is_playing(playback_id):
            self._hold("RTC source chunk exhausted before request point")
            self.stop_event.set()
            return False
        return not self.stop_event.is_set()

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
                                    actions=prepared.actions,
                                    start_index=stale_steps,
                                    anchor=prepared.anchor_state,
                                )
                                current_playback_id = self.servo.play(
                                    prepared.actions,
                                    start_index=stale_steps,
                                    interval_count=fresh_steps,
                                    blend_seconds=self.options.boundary_blend_seconds,
                                    anchor=prepared.anchor_state,
                                )
                                self._record_arm_micro_motion(
                                    ready.chunk.request_id,
                                    current_playback_id,
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
                        actions=prepared.actions,
                        start_index=0,
                        anchor=prepared.anchor_state,
                    )
                    current_playback_id = self.servo.play(
                        prepared.actions,
                        start_index=0,
                        interval_count=CHUNK_STEPS,
                        blend_seconds=self.options.boundary_blend_seconds,
                        anchor=prepared.anchor_state,
                    )
                    self._record_arm_micro_motion(
                        chunk.request_id, current_playback_id
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
        rtc_prefix_absolute: np.ndarray | None = None,
        rtc_inference_delay: int | None = None,
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
            (
                actions,
                clip_count,
                max_clip_delta,
                smoothing,
                arm_start_velocity_per_step,
            ) = (
                self._postprocess_action_chunk(
                    chunk.actions,
                    start_index=start_index,
                    rtc_prefix_absolute=rtc_prefix_absolute,
                    rtc_inference_delay=rtc_inference_delay,
                )
            )
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
        all_start_deltas = np.abs(actions[start_index] - current_state)
        start_deltas = all_start_deltas[: self.options.executed_action_dim]
        start_joint_index = int(np.argmax(start_deltas))
        start_jump = float(start_deltas[start_joint_index])
        start_jump_joint = POLICY_JOINT_NAMES[start_joint_index]
        arm_start_joint_index = int(np.argmax(start_deltas[:14]))
        arm_start_jump = float(start_deltas[arm_start_joint_index])
        arm_start_jump_joint = POLICY_JOINT_NAMES[arm_start_joint_index]
        hand_start_joint_index = int(np.argmax(all_start_deltas[14:])) + 14
        hand_start_jump = float(all_start_deltas[hand_start_joint_index])
        hand_start_jump_joint = POLICY_JOINT_NAMES[hand_start_joint_index]
        arm_start_exceeds = (
            arm_start_jump > self.options.start_jump_limit_radians
        )
        is_rtc_replan = rtc_prefix_absolute is not None
        hand_start_exceeds = (
            self.options.executed_action_dim == ACTION_DIM
            and hand_start_jump > self.options.hand_start_jump_limit_radians
        )
        if (
            self.options.executed_action_dim == ACTION_DIM
            and is_rtc_replan
            and hand_start_jump > self.options.hand_start_jump_limit_radians
        ):
            # The RTC boundary is projected onto the already committed logical
            # prefix. O10 hand feedback is much slower than the 30 Hz policy
            # timeline, so comparing every RTC boundary with instantaneous hand
            # feedback can report normal tracking lag as a new-plan start jump.
            # Keep this observable, but only arm discontinuities remain a hard
            # rejection after bootstrap.
            self.telemetry.emit(
                "rtc_hand_feedback_gap_warning",
                request_id=chunk.request_id,
                hand_start_jump_rad=hand_start_jump,
                hand_start_jump_joint=hand_start_jump_joint,
                current=float(current_state[hand_start_joint_index]),
                target=float(actions[start_index, hand_start_joint_index]),
                delta=float(
                    actions[start_index, hand_start_joint_index]
                    - current_state[hand_start_joint_index]
                ),
                hand_limit=self.options.hand_start_jump_limit_radians,
                reason=(
                    "RTC hand boundary follows the committed logical prefix; "
                    "physical hand feedback gap is telemetry-only"
                ),
            )
        rtc_start_jump_warning_only = self.options.mode == "powered-rtc"
        if arm_start_exceeds or hand_start_exceeds:
            self.telemetry.emit(
                (
                    "start_jump_rejected"
                    if (
                        self.options.enforce_action_safety_gates
                        and not rtc_start_jump_warning_only
                    )
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
                limit=(
                    self.options.start_jump_limit_radians
                    if arm_start_exceeds
                    else self.options.hand_start_jump_limit_radians
                ),
                arm_limit=self.options.start_jump_limit_radians,
                hand_limit=self.options.hand_start_jump_limit_radians,
                reason=(
                    f"arm/hand start jump {arm_start_jump:.6f}/"
                    f"{hand_start_jump:.6f} exceeds limits "
                    f"{self.options.start_jump_limit_radians:.6f}/"
                    f"{self.options.hand_start_jump_limit_radians:.6f} rad"
                ),
            )
            if (
                self.options.enforce_action_safety_gates
                and not rtc_start_jump_warning_only
            ):
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
            rtc_dynamics_warning_only = self.options.mode == "powered-rtc"
            self.telemetry.emit(
                (
                    "action_dynamics_rejected"
                    if (
                        self.options.enforce_action_safety_gates
                        and not rtc_dynamics_warning_only
                    )
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
            if (
                self.options.enforce_action_safety_gates
                and not rtc_dynamics_warning_only
            ):
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
            arm_start_velocity_per_step=arm_start_velocity_per_step,
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

    def _record_arm_micro_motion(
        self, request_id: int, playback_id: int
    ) -> None:
        assert self.servo is not None
        stats = self.servo.arm_micro_motion_stats(playback_id)
        if stats is None or stats.profile == "off":
            return
        joint_names = [
            POLICY_JOINT_NAMES[index]
            for index in stats.active_joint_indices
        ]
        self.telemetry.emit(
            "arm_micro_motion_projection",
            request_id=request_id,
            playback_id=playback_id,
            profile=stats.profile,
            threshold_rad=stats.threshold_rad,
            active_joint_count=len(stats.active_joint_indices),
            active_joints=joint_names,
            baseline_reversal_count=stats.baseline_reversal_count,
            projected_reversal_count=stats.projected_reversal_count,
            max_deviation_rad=stats.max_deviation_rad,
            summary=(
                f"双臂近静止投影={stats.profile} "
                f"触发轴={len(joint_names)} "
                f"原反向={stats.baseline_reversal_count} "
                f"最大偏差={stats.max_deviation_rad:.6f}rad"
            ),
        )

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
        actions: np.ndarray,
        start_index: int,
        anchor: np.ndarray,
        blend_seconds: float | None = None,
    ) -> None:
        started_at = time.monotonic()
        if self.robot is not None:
            begin_hand_segment = getattr(
                self.robot, "begin_hand_segment", None
            )
            if begin_hand_segment is not None:
                begin_hand_segment(
                    started_at=started_at,
                    reference_actions=actions,
                    start_index=start_index,
                    interval_count=interval_count,
                    anchor=anchor,
                    blend_seconds=(
                        self.options.boundary_blend_seconds
                        if blend_seconds is None
                        else blend_seconds
                    ),
                    blend_profile=self.options.boundary_blend_profile,
                )
        if self.dispatch_telemetry is None:
            return
        self.dispatch_telemetry.begin_segment(
            mode=self.options.mode,
            request_id=request_id,
            blend_seconds=(
                self.options.boundary_blend_seconds
                if blend_seconds is None
                else blend_seconds
            ),
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
        segment = segment[:, : self.options.executed_action_dim]

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
            float(np.max(np.abs(velocity[:, 14:])))
            if velocity.size and velocity.shape[1] > 14
            else 0.0
        )
        arm_acceleration = (
            float(np.max(np.abs(acceleration[:, :14])))
            if acceleration.size
            else 0.0
        )
        hand_acceleration = (
            float(np.max(np.abs(acceleration[:, 14:])))
            if acceleration.size and acceleration.shape[1] > 14
            else 0.0
        )
        failures: list[str] = []
        if arm_velocity > self.options.max_arm_velocity_rad_s:
            failures.append(
                f"arm velocity {arm_velocity:.3f} exceeds "
                f"{self.options.max_arm_velocity_rad_s:.3f} rad/s"
            )
        if (
            self.options.executed_action_dim > 14
            and hand_velocity > self.options.max_hand_velocity_rad_s
        ):
            failures.append(
                f"hand velocity {hand_velocity:.3f} exceeds "
                f"{self.options.max_hand_velocity_rad_s:.3f} rad/s"
            )
        if arm_acceleration > self.options.max_arm_acceleration_rad_s2:
            failures.append(
                f"arm acceleration {arm_acceleration:.3f} exceeds "
                f"{self.options.max_arm_acceleration_rad_s2:.3f} rad/s^2"
            )
        if (
            self.options.executed_action_dim > 14
            and hand_acceleration > self.options.max_hand_acceleration_rad_s2
        ):
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
