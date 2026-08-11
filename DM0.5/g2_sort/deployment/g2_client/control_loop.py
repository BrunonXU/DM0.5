"""Shadow、powered-sync和powered-async三种控制状态机。

用途：协调观测、DM0.5请求、安全门、chunk切换、hold和telemetry。
输入：``ObservationSource``、``PolicyBackend``、运行选项，以及powered所需robot/servo。
输出：shadow统计或100Hz本地动作播放；所有错误路径输出明确hold原因。
方法：sync只执行短前缀；async单请求预取、跳过陈旧前缀并用播放ID拒绝迟到响应。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .contracts import (
    CHUNK_STEPS,
    ActionChunk,
    ObservationError,
    PolicyError,
    RobotControlError,
)
from .interpolation import resample_action_intervals
from .joint_layout import clip_action_positions
from .observation import ObservationSource
from .policy import PolicyBackend
from .prefetch import SingleFlightPrefetcher
from .servo import ServoStreamer
from .telemetry import Telemetry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeOptions:
    mode: str = "shadow"
    execute_prefix_steps: int = 6
    chunk_prefetch_lookahead: int = 20
    async_min_fresh_steps: int = 12
    start_jump_limit_radians: float = 0.35
    boundary_blend_seconds: float = 0.06
    observation_retry_seconds: float = 0.1
    max_requests: int = 0

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
        if self.observation_retry_seconds < 0:
            raise ValueError("observation_retry_seconds cannot be negative")
        if self.max_requests < 0:
            raise ValueError("max_requests cannot be negative")


@dataclass(frozen=True)
class _PreparedChunk:
    actions: np.ndarray
    clip_count: int
    max_clip_delta: float
    start_jump: float


class ControlLoop:
    """Coordinate observations, policy requests, safety gates, and the servo."""

    def __init__(
        self,
        *,
        options: RuntimeOptions,
        observations: ObservationSource,
        backend: PolicyBackend,
        telemetry: Telemetry,
        robot: object | None = None,
        servo: ServoStreamer | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.options = options
        self.observations = observations
        self.backend = backend
        self.telemetry = telemetry
        self.robot = robot
        self.servo = servo
        self.stop_event = stop_event or threading.Event()
        self._request_count = 0
        self._last_accepted_request_id = -1
        if options.mode == "shadow":
            if servo is not None:
                raise ValueError("shadow mode must not receive a ServoStreamer")
        elif robot is None or servo is None:
            raise ValueError("powered modes require robot and ServoStreamer")

    def run(self) -> dict[str, float | int]:
        if self.options.mode == "shadow":
            self._run_shadow()
        elif self.options.mode == "powered-sync":
            self._run_powered_sync()
        else:
            self._run_powered_async()
        return self.telemetry.finish(self.options.mode)

    def _budget_available(self) -> bool:
        limit = self.options.max_requests
        return limit == 0 or self._request_count < limit

    def _capture_and_infer(self) -> ActionChunk | None:
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
            chunk = self._capture_and_infer()
            if chunk is None:
                self._retry_pause()
                continue
            clipped, clip_count, max_clip_delta = clip_action_positions(
                chunk.actions
            )
            # Exercise the exact interpolation path without creating a sender.
            _, interpolated = resample_action_intervals(clipped)
            start_jump = None
            try:
                state = self.observations.read_state()
                start_jump = float(np.max(np.abs(clipped[0] - state)))
            except Exception as exc:
                self.telemetry.emit(
                    "shadow_state_recheck_failed",
                    request_id=chunk.request_id,
                    reason=str(exc),
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
            )

    def _run_powered_sync(self) -> None:
        assert self.servo is not None
        self.servo.start()
        while not self.stop_event.is_set() and self._budget_available():
            self.servo.hold()
            chunk = self._capture_and_infer()
            if chunk is None:
                self.servo.hold()
                self._retry_pause()
                continue
            if self.stop_event.is_set():
                self.servo.hold()
                break
            prepared = self._prepare_for_execution(chunk, start_index=0)
            if prepared is None:
                self.servo.hold()
                continue
            self._last_accepted_request_id = chunk.request_id
            playback_id = self.servo.play(
                prepared.actions,
                start_index=0,
                interval_count=self.options.execute_prefix_steps,
                blend_seconds=self.options.boundary_blend_seconds,
            )
            self._record_prepared(
                chunk,
                prepared,
                start_index=0,
                used_intervals=self.options.execute_prefix_steps,
            )
            self._wait_for_playback(playback_id)
            self.servo.hold()

    def _run_powered_async(self) -> None:
        assert self.servo is not None
        self.servo.start()
        prefetcher = SingleFlightPrefetcher(self.backend)
        current_playback_id: int | None = None
        need_sync_request = True
        try:
            while not self.stop_event.is_set():
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
                                ready.chunk, start_index=stale_steps
                            )
                            if prepared is None:
                                self._hold(
                                    "async response failed execution safety gates",
                                    request_id=ready.chunk.request_id,
                                )
                                current_playback_id = None
                                need_sync_request = True
                            else:
                                current_playback_id = self.servo.play(
                                    prepared.actions,
                                    start_index=stale_steps,
                                    interval_count=fresh_steps,
                                    blend_seconds=self.options.boundary_blend_seconds,
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
                    prepared = self._prepare_for_execution(chunk, start_index=0)
                    if prepared is None:
                        self._hold(
                            "synchronous recovery chunk failed safety gates",
                            request_id=chunk.request_id,
                        )
                        continue
                    current_playback_id = self.servo.play(
                        prepared.actions,
                        start_index=0,
                        interval_count=CHUNK_STEPS,
                        blend_seconds=self.options.boundary_blend_seconds,
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
        self, chunk: ActionChunk, *, start_index: int
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
        except Exception as exc:
            self.telemetry.emit(
                "action_safety_invalid",
                request_id=chunk.request_id,
                reason=str(exc),
            )
            return None
        start_jump = float(np.max(np.abs(actions[start_index] - current_state)))
        if start_jump > self.options.start_jump_limit_radians:
            self.telemetry.emit(
                "start_jump_rejected",
                request_id=chunk.request_id,
                max_start_jump=start_jump,
                limit=self.options.start_jump_limit_radians,
                reason=(
                    f"start jump {start_jump:.6f} exceeds "
                    f"{self.options.start_jump_limit_radians:.6f} rad"
                ),
            )
            return None
        return _PreparedChunk(
            actions=actions,
            clip_count=clip_count,
            max_clip_delta=max_clip_delta,
            start_jump=start_jump,
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
        )

    def _wait_for_playback(self, playback_id: int) -> None:
        assert self.servo is not None
        while (
            not self.stop_event.is_set()
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

    def _hold(self, reason: str, request_id: int | None = None) -> None:
        if self.servo is not None:
            self.servo.hold()
        fields: dict[str, object] = {"reason": reason}
        if request_id is not None:
            fields["request_id"] = request_id
        self.telemetry.emit("safe_hold", **fields)

    def _retry_pause(self, *, short: bool = False) -> None:
        delay = 0.005 if short else self.options.observation_retry_seconds
        self.stop_event.wait(max(0.0, delay))
