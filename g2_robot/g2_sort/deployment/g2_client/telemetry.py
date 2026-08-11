"""G2客户端的结构化telemetry与延迟分布。

用途：记录请求、动作、观测年龄、跳步、限位、首动作跳变和hold原因。
输入：运行时事件字段、ActionChunk统计及可选JSONL输出路径。
输出：人可读日志、逐事件JSONL和P50/P95/P99/mean延迟汇总。
方法：线程锁保护追加写入；完整动作只进入结构化记录，控制台输出紧凑摘要。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

EVENT_LABELS = {
    "runtime_start": "运行时已启动",
    "action_chunk": "推理成功",
    "run_summary": "运行汇总",
    "observation_invalid": "观测失败",
    "policy_failure": "推理请求失败",
    "response_discarded": "响应已丢弃",
    "response_out_of_order": "响应顺序错误",
    "shadow_state_recheck_failed": "Shadow状态复查失败",
    "shadow_dynamics_warning": "Shadow动作动态警告",
    "state_invalid_before_execute": "执行前状态无效",
    "action_safety_invalid": "动作安全检查失败",
    "clip_delta_rejected": "关节限位裁剪超限",
    "start_jump_rejected": "首动作跳变超限",
    "action_dynamics_rejected": "动作速度检查拒绝",
    "servo_failure": "Servo发送失败",
    "safe_hold": "机器人保持当前位置",
    "runtime_paused": "运行已暂停",
    "runtime_resumed": "运行已恢复",
    "operator_pause_requested": "收到暂停指令",
    "operator_resume_requested": "收到恢复指令",
    "async_prefetch_started": "异步预取已开始",
    "dispatch_summary": "实际下发汇总",
    "first_hand_command": "首条O10硬件命令",
}

WARNING_EVENTS = {
    "observation_invalid",
    "policy_failure",
    "response_discarded",
    "response_out_of_order",
    "shadow_state_recheck_failed",
    "shadow_dynamics_warning",
    "state_invalid_before_execute",
    "action_safety_invalid",
    "clip_delta_rejected",
    "start_jump_rejected",
    "action_dynamics_rejected",
    "servo_failure",
    "safe_hold",
}


def _number(fields: dict[str, Any], name: str, unit: str = "") -> str:
    value = fields.get(name)
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}{unit}"
    return "无"


def _joint_display(value: object) -> str:
    name = str(value or "未知")
    body = name.split("_", 1)[1] if name.startswith("idx") and "_" in name else name
    if body.startswith("arm_l_joint"):
        return f"左臂关节{body.removeprefix('arm_l_joint')}({name})"
    if body.startswith("arm_r_joint"):
        return f"右臂关节{body.removeprefix('arm_r_joint')}({name})"
    hand_names = {
        "thumb_roll": "拇指滚转",
        "thumb_abad": "拇指张合",
        "thumb_mcp": "拇指弯曲",
        "index_abad": "食指张合",
        "index_pip": "食指弯曲",
        "middle_pip": "中指弯曲",
        "ring_abad": "无名指张合",
        "ring_pip": "无名指弯曲",
        "pinky_abad": "小指张合",
        "pinky_pip": "小指弯曲",
    }
    for prefix, side in (("hand_l_", "左手"), ("hand_r_", "右手")):
        if body.startswith(prefix) and body.endswith("_joint"):
            key = body[len(prefix) : -len("_joint")]
            return f"{side}{hand_names.get(key, key)}({name})"
    return name


def _console_summary(event: str, fields: dict[str, Any]) -> str:
    if event == "start_jump_rejected":
        return (
            f"关节={_joint_display(fields.get('joint_name'))} "
            f"当前={_number(fields, 'current')} "
            f"目标={_number(fields, 'target')} "
            f"跳变={_number(fields, 'max_start_jump', 'rad')} "
            f"门限={_number(fields, 'limit', 'rad')}，已拒绝并保持"
        )
    if event == "clip_delta_rejected":
        return (
            f"最大裁剪={_number(fields, 'max_limit_clip_delta', 'rad')} "
            f"门限={_number(fields, 'limit', 'rad')}，已拒绝并保持"
        )
    if event == "run_summary":
        latency = fields.get("latency")
        if isinstance(latency, dict) and latency.get("count"):
            return (
                f"请求数={latency['count']} "
                f"P50={float(latency['p50_seconds']):.3f}s "
                f"P95={float(latency['p95_seconds']):.3f}s "
                f"P99={float(latency['p99_seconds']):.3f}s"
            )
        return "没有成功的推理请求"
    summary = fields.get("summary")
    if summary:
        return str(summary)
    reason = fields.get("reason")
    if reason:
        return f"原因={reason}"
    return ""


class Telemetry:
    """Emit compact logs and optional JSONL records for post-run inspection."""

    def __init__(self, jsonl_path: Path | None = None) -> None:
        self._path = jsonl_path
        self._lock = threading.Lock()
        self._latencies: list[float] = []
        self._request_timings: dict[str, list[float]] = {}
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "event": event,
            "wall_time": time.time(),
            **fields,
        }
        request_id = fields.get("request_id", "-")
        label = EVENT_LABELS.get(event, event)
        summary = _console_summary(event, fields)
        log_method = logger.warning if event in WARNING_EVENTS else logger.info
        log_method("【%s】请求=%s | %s", label, request_id, summary)
        if self._path is not None:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )

    def record_chunk(
        self,
        *,
        mode: str,
        request_id: int,
        observation_at: float,
        request_started_at: float,
        response_received_at: float,
        latency_seconds: float,
        observation_age_seconds: float,
        actions: np.ndarray,
        used_start_index: int | None,
        used_intervals: int,
        skipped_steps: int,
        clip_count: int,
        max_clip_delta: float,
        start_jump: float | None,
        start_jump_joint: str | None = None,
        arm_start_jump: float | None = None,
        arm_start_jump_joint: str | None = None,
        hand_start_jump: float | None = None,
        hand_start_jump_joint: str | None = None,
        max_velocity: float | None = None,
        max_velocity_joint: str | None = None,
        max_acceleration: float | None = None,
        max_acceleration_joint: str | None = None,
        max_jerk: float | None = None,
        max_jerk_joint: str | None = None,
        arm_internal_jerk_weight: float = 0.0,
        arm_smoothing_delta_p95: float = 0.0,
        arm_smoothing_max_delta: float = 0.0,
        arm_smoothing_max_delta_joint: str | None = None,
        request_timing_seconds: dict[str, float] | None = None,
    ) -> None:
        self._latencies.append(float(latency_seconds))
        timings = {
            str(key): float(value)
            for key, value in (request_timing_seconds or {}).items()
            if np.isfinite(float(value)) and float(value) >= 0
        }
        for key, value in timings.items():
            self._request_timings.setdefault(key, []).append(value)
        array = np.asarray(actions, dtype=np.float64)
        jump_text = (
            f"{start_jump:.4f}rad({_joint_display(start_jump_joint)})"
            if start_jump is not None
            else "无"
        )
        self.emit(
            "action_chunk",
            mode=mode,
            request_id=request_id,
            observation_at_monotonic=float(observation_at),
            request_started_at_monotonic=float(request_started_at),
            response_received_at_monotonic=float(response_received_at),
            latency_seconds=float(latency_seconds),
            observation_age_seconds=float(observation_age_seconds),
            shape=list(array.shape),
            action_min=float(np.min(array)),
            action_max=float(np.max(array)),
            used_start_index=used_start_index,
            used_intervals=used_intervals,
            skipped_steps=skipped_steps,
            limit_clip_count=clip_count,
            max_limit_clip_delta=max_clip_delta,
            max_start_jump=start_jump,
            max_start_jump_joint=start_jump_joint,
            arm_start_jump_rad=arm_start_jump,
            arm_start_jump_joint=arm_start_jump_joint,
            hand_start_jump_rad=hand_start_jump,
            hand_start_jump_joint=hand_start_jump_joint,
            max_velocity_rad_s=max_velocity,
            max_velocity_joint=max_velocity_joint,
            max_acceleration_rad_s2=max_acceleration,
            max_acceleration_joint=max_acceleration_joint,
            max_jerk_rad_s3=max_jerk,
            max_jerk_joint=max_jerk_joint,
            arm_internal_jerk_weight=arm_internal_jerk_weight,
            arm_smoothing_delta_p95_rad=arm_smoothing_delta_p95,
            arm_smoothing_max_delta_rad=arm_smoothing_max_delta,
            arm_smoothing_max_delta_joint=arm_smoothing_max_delta_joint,
            request_timing_seconds=timings,
            actions=array.tolist(),
            summary=(
                f"延迟={latency_seconds:.3f}s "
                f"观测年龄={observation_age_seconds:.3f}s "
                f"首跳={jump_text} "
                f"双臂首跳={arm_start_jump if arm_start_jump is not None else '无'} "
                f"双手首跳={hand_start_jump if hand_start_jump is not None else '无'} "
                f"执行区间={used_intervals} 跳过={skipped_steps} "
                f"内部jerk权重={arm_internal_jerk_weight:g} "
                f"平滑最大偏差={arm_smoothing_max_delta:.4f}rad "
                f"限位裁剪={clip_count}次 最大裁剪={max_clip_delta:.4f}rad"
            ),
        )

    def latency_summary(self) -> dict[str, float | int]:
        if not self._latencies:
            return {"count": 0}
        values = np.asarray(self._latencies, dtype=np.float64)
        return {
            "count": int(len(values)),
            "p50_seconds": float(np.percentile(values, 50)),
            "p95_seconds": float(np.percentile(values, 95)),
            "p99_seconds": float(np.percentile(values, 99)),
            "mean_seconds": float(np.mean(values)),
        }

    def request_timing_summary(self) -> dict[str, dict[str, float | int]]:
        summary: dict[str, dict[str, float | int]] = {}
        for key, samples in sorted(self._request_timings.items()):
            values = np.asarray(samples, dtype=np.float64)
            summary[key] = {
                "count": int(len(values)),
                "p50_seconds": float(np.percentile(values, 50)),
                "p95_seconds": float(np.percentile(values, 95)),
                "mean_seconds": float(np.mean(values)),
            }
        return summary

    def finish(self, mode: str) -> dict[str, float | int]:
        summary = self.latency_summary()
        self.emit(
            "run_summary",
            mode=mode,
            latency=summary,
            request_timing=self.request_timing_summary(),
        )
        return summary


@dataclass
class _DispatchSegment:
    mode: str
    request_id: int
    blend_seconds: float
    started_at: float
    expected_duration_seconds: float | None
    previous_arm: np.ndarray | None
    previous_arm_time: float | None
    previous_hand: np.ndarray | None
    previous_hand_time: float | None
    arm_commands: list[np.ndarray] = field(default_factory=list)
    arm_times: list[float] = field(default_factory=list)
    hand_commands: list[np.ndarray] = field(default_factory=list)
    hand_times: list[float] = field(default_factory=list)
    hand_raw_commands: list[np.ndarray] = field(default_factory=list)
    hand_boundary_active: list[bool] = field(default_factory=list)
    hand_velocity_limit_hits: int = 0
    hand_acceleration_limit_hits: int = 0
    hand_direction_reversal_holds: int = 0
    hand_emergency_cap_hits: int = 0


class DispatchTelemetry:
    """Summarize acknowledged arm/hand commands without changing control.

    One segment corresponds to one accepted action chunk.  Callers record only
    targets whose native hardware call already succeeded.  The class keeps
    compact in-memory samples and emits one summary per segment rather than one
    JSONL row per 100 Hz tick.
    """

    ARM_DIM = 14
    HAND_DIM = 20

    def __init__(self, telemetry: Telemetry) -> None:
        self._telemetry = telemetry
        self._lock = threading.Lock()
        self._active: _DispatchSegment | None = None
        self._last_arm: np.ndarray | None = None
        self._last_arm_time: float | None = None
        self._last_hand: np.ndarray | None = None
        self._last_hand_time: float | None = None
        self._first_hand_emitted = False

    def begin_segment(
        self,
        *,
        mode: str,
        request_id: int,
        blend_seconds: float,
        expected_duration_seconds: float | None = None,
        started_at: float | None = None,
    ) -> None:
        self.finish_segment(reason="replaced_by_new_chunk")
        now = time.monotonic() if started_at is None else float(started_at)
        with self._lock:
            self._active = _DispatchSegment(
                mode=mode,
                request_id=int(request_id),
                blend_seconds=max(0.0, float(blend_seconds)),
                started_at=now,
                expected_duration_seconds=(
                    None
                    if expected_duration_seconds is None
                    else max(0.0, float(expected_duration_seconds))
                ),
                previous_arm=(
                    None if self._last_arm is None else self._last_arm.copy()
                ),
                previous_arm_time=self._last_arm_time,
                previous_hand=(
                    None if self._last_hand is None else self._last_hand.copy()
                ),
                previous_hand_time=self._last_hand_time,
            )

    def record_arm_command(
        self,
        positions: np.ndarray,
        *,
        sent_at: float | None = None,
    ) -> None:
        values = self._validated(positions, self.ARM_DIM, "arm")
        now = time.monotonic() if sent_at is None else float(sent_at)
        with self._lock:
            self._last_arm = values.copy()
            self._last_arm_time = now
            if self._active is not None:
                self._active.arm_commands.append(values.copy())
                self._active.arm_times.append(now)

    def record_hand_command(
        self,
        positions: np.ndarray,
        *,
        source: str = "split_move_ee_pos",
        raw_positions: np.ndarray | None = None,
        boundary_active: bool = False,
        velocity_limit_hits: int = 0,
        acceleration_limit_hits: int = 0,
        direction_reversal_holds: int = 0,
        emergency_cap_hits: int = 0,
        sent_at: float | None = None,
    ) -> None:
        values = self._validated(positions, self.HAND_DIM, "hand")
        raw = (
            values.copy()
            if raw_positions is None
            else self._validated(raw_positions, self.HAND_DIM, "raw hand")
        )
        now = time.monotonic() if sent_at is None else float(sent_at)
        first_fields: dict[str, Any] | None = None
        with self._lock:
            active = self._active
            previous = None if self._last_hand is None else self._last_hand.copy()
            self._last_hand = values.copy()
            self._last_hand_time = now
            if active is not None:
                active.hand_commands.append(values.copy())
                active.hand_times.append(now)
                active.hand_raw_commands.append(raw.copy())
                active.hand_boundary_active.append(bool(boundary_active))
                active.hand_velocity_limit_hits += int(velocity_limit_hits)
                active.hand_acceleration_limit_hits += int(
                    acceleration_limit_hits
                )
                active.hand_direction_reversal_holds += int(
                    direction_reversal_holds
                )
                active.hand_emergency_cap_hits += int(emergency_cap_hits)
            if not self._first_hand_emitted:
                self._first_hand_emitted = True
                first_fields = {
                    "mode": None if active is None else active.mode,
                    "request_id": None if active is None else active.request_id,
                    "source": source,
                    "positions": values.tolist(),
                    "max_abs_position_rad": float(np.max(np.abs(values))),
                    "delta_from_previous_rad": self._boundary_delta(
                        previous, values
                    ),
                    "summary": (
                        f"来源={source} 最大绝对位置="
                        f"{float(np.max(np.abs(values))):.4f}rad"
                    ),
                }
        if first_fields is not None:
            self._telemetry.emit("first_hand_command", **first_fields)

    def finish_segment(self, *, reason: str) -> None:
        with self._lock:
            segment = self._active
            self._active = None
        if segment is None:
            return
        fields = self._summarize(segment)
        self._telemetry.emit(
            "dispatch_summary",
            mode=segment.mode,
            request_id=segment.request_id,
            completion_reason=reason,
            **fields,
        )

    @staticmethod
    def _validated(values: np.ndarray, size: int, label: str) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        if array.shape != (size,) or not np.all(np.isfinite(array)):
            raise ValueError(f"{label} dispatch target must be finite {size}D")
        return array

    @staticmethod
    def _boundary_delta(
        previous: np.ndarray | None,
        first: np.ndarray,
    ) -> float | None:
        if previous is None:
            return None
        return float(np.max(np.abs(first - previous)))

    @staticmethod
    def _percentile(values: np.ndarray, percentile: float) -> float | None:
        if values.size == 0:
            return None
        return float(np.percentile(values, percentile))

    @classmethod
    def _command_stats(
        cls,
        commands: list[np.ndarray],
        times: list[float],
        *,
        previous: np.ndarray | None,
        previous_time: float | None,
        nominal_hz: float,
        joint_names: tuple[str, ...],
        blend_seconds: float | None = None,
        segment_started_at: float | None = None,
        expected_duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not commands:
            return {
                "send_count": 0,
                "expected_command_dimension": len(joint_names),
                "command_dimension_min": None,
                "command_dimension_max": None,
                "all_commands_complete": True,
                "all_commands_finite": True,
                "expected_duration_seconds": expected_duration_seconds,
                "expected_send_count_at_nominal_rate": (
                    None
                    if expected_duration_seconds is None
                    else expected_duration_seconds * nominal_hz
                ),
                "send_coverage_ratio": None,
                "effective_send_hz": None,
                "boundary_first_delta_rad": None,
                "delta_p50_rad": None,
                "delta_p95_rad": None,
                "max_delta_rad": None,
                "max_delta_joint": None,
                "nominal_max_velocity_rad_s": None,
                "nominal_max_acceleration_rad_s2": None,
                "nominal_max_jerk_rad_s3": None,
                "send_interval_p50_seconds": None,
                "send_interval_p95_seconds": None,
                "send_interval_max_seconds": None,
                "send_interval_over_1_5x_period_count": 0,
                "send_interval_over_2x_period_count": 0,
                "blend_window_max_delta_rad": None,
                "blend_window_send_count": 0,
                "blend_window_actual_max_velocity_rad_s": None,
                "blend_window_actual_max_velocity_joint": None,
                "blend_window_actual_max_acceleration_rad_s2": None,
                "blend_window_actual_max_acceleration_joint": None,
                "blend_window_actual_max_jerk_rad_s3": None,
                "blend_window_actual_max_jerk_joint": None,
            }
        array = np.stack(commands)
        deltas = np.abs(np.diff(array, axis=0))
        per_send = (
            np.max(deltas, axis=1)
            if len(deltas)
            else np.empty(0, dtype=np.float64)
        )
        if deltas.size:
            flat_index = int(np.argmax(deltas))
            _, joint_index = np.unravel_index(flat_index, deltas.shape)
            max_delta = float(np.max(deltas))
            max_delta_joint = joint_names[joint_index]
        else:
            max_delta = None
            max_delta_joint = None
        intervals = np.diff(np.asarray(times, dtype=np.float64))

        def actual_derivative_max(
            samples: np.ndarray,
            sample_times: np.ndarray,
            order: int,
        ) -> tuple[float | None, str | None]:
            values = samples
            derivative_times = sample_times
            for _ in range(order):
                dt = np.diff(derivative_times)
                if values.shape[0] < 2 or not len(dt) or np.any(dt <= 0):
                    return None, None
                values = np.diff(values, axis=0) / dt[:, None]
                derivative_times = (
                    derivative_times[:-1] + derivative_times[1:]
                ) / 2.0
            if not values.size:
                return None, None
            flat_index = int(np.argmax(np.abs(values)))
            _, joint_index = np.unravel_index(flat_index, values.shape)
            return float(np.max(np.abs(values))), joint_names[joint_index]

        def derivative_max(order: int) -> float | None:
            derivative = np.diff(array, n=order, axis=0)
            if not derivative.size:
                return None
            return float(np.max(np.abs(derivative))) * (nominal_hz**order)

        blend_max: float | None = None
        blend_count = 0
        blend_dynamics: dict[int, tuple[float | None, str | None]] = {}
        if blend_seconds is not None and segment_started_at is not None:
            selected = [
                (command, sent_at)
                for command, sent_at in zip(commands, times)
                if sent_at - segment_started_at <= blend_seconds
            ]
            blend_count = len(selected)
            in_window = [command for command, _ in selected]
            in_window_times = [sent_at for _, sent_at in selected]
            if previous is not None and previous_time is not None:
                in_window.insert(0, previous)
                in_window_times.insert(0, previous_time)
            if len(in_window) >= 2:
                blend_array = np.stack(in_window)
                blend_time_array = np.asarray(in_window_times, dtype=np.float64)
                blend_max = float(np.max(np.abs(np.diff(blend_array, axis=0))))
                blend_dynamics = {
                    order: actual_derivative_max(
                        blend_array, blend_time_array, order
                    )
                    for order in (1, 2, 3)
                }
        expected_send_count = (
            None
            if expected_duration_seconds is None
            else expected_duration_seconds * nominal_hz
        )
        elapsed = float(times[-1] - times[0]) if len(times) >= 2 else 0.0
        dimensions = [int(command.size) for command in commands]
        nominal_period = 1.0 / nominal_hz
        return {
            "send_count": len(commands),
            "expected_command_dimension": len(joint_names),
            "command_dimension_min": min(dimensions),
            "command_dimension_max": max(dimensions),
            "all_commands_complete": all(
                command.shape == (len(joint_names),) for command in commands
            ),
            "all_commands_finite": all(
                bool(np.all(np.isfinite(command))) for command in commands
            ),
            "expected_duration_seconds": expected_duration_seconds,
            "expected_send_count_at_nominal_rate": expected_send_count,
            "send_coverage_ratio": (
                None
                if expected_send_count in (None, 0.0)
                else len(commands) / expected_send_count
            ),
            "effective_send_hz": (
                None if elapsed <= 0 else (len(commands) - 1) / elapsed
            ),
            "boundary_first_delta_rad": cls._boundary_delta(previous, array[0]),
            "delta_p50_rad": cls._percentile(per_send, 50),
            "delta_p95_rad": cls._percentile(per_send, 95),
            "max_delta_rad": max_delta,
            "max_delta_joint": max_delta_joint,
            "nominal_max_velocity_rad_s": derivative_max(1),
            "nominal_max_acceleration_rad_s2": derivative_max(2),
            "nominal_max_jerk_rad_s3": derivative_max(3),
            "send_interval_p50_seconds": cls._percentile(intervals, 50),
            "send_interval_p95_seconds": cls._percentile(intervals, 95),
            "send_interval_max_seconds": (
                None if not len(intervals) else float(np.max(intervals))
            ),
            "send_interval_over_1_5x_period_count": int(
                np.sum(intervals > 1.5 * nominal_period)
            ),
            "send_interval_over_2x_period_count": int(
                np.sum(intervals > 2.0 * nominal_period)
            ),
            "blend_window_max_delta_rad": blend_max,
            "blend_window_send_count": blend_count,
            "blend_window_actual_max_velocity_rad_s": blend_dynamics.get(
                1, (None, None)
            )[0],
            "blend_window_actual_max_velocity_joint": blend_dynamics.get(
                1, (None, None)
            )[1],
            "blend_window_actual_max_acceleration_rad_s2": blend_dynamics.get(
                2, (None, None)
            )[0],
            "blend_window_actual_max_acceleration_joint": blend_dynamics.get(
                2, (None, None)
            )[1],
            "blend_window_actual_max_jerk_rad_s3": blend_dynamics.get(
                3, (None, None)
            )[0],
            "blend_window_actual_max_jerk_joint": blend_dynamics.get(
                3, (None, None)
            )[1],
        }

    def _summarize(self, segment: _DispatchSegment) -> dict[str, Any]:
        # Local import avoids making telemetry depend on GDK-facing modules.
        from .joint_layout import POLICY_JOINT_NAMES

        arm = self._command_stats(
            segment.arm_commands,
            segment.arm_times,
            previous=segment.previous_arm,
            previous_time=segment.previous_arm_time,
            nominal_hz=100.0,
            joint_names=POLICY_JOINT_NAMES[: self.ARM_DIM],
            blend_seconds=segment.blend_seconds,
            segment_started_at=segment.started_at,
            expected_duration_seconds=segment.expected_duration_seconds,
        )
        hand = self._command_stats(
            segment.hand_commands,
            segment.hand_times,
            previous=segment.previous_hand,
            previous_time=segment.previous_hand_time,
            nominal_hz=5.0,
            joint_names=POLICY_JOINT_NAMES[self.ARM_DIM :],
        )
        arm_p95 = arm["delta_p95_rad"]
        hand_p95 = hand["delta_p95_rad"]
        hand_tracking_errors = (
            np.max(
                np.abs(
                    np.stack(segment.hand_commands)
                    - np.stack(segment.hand_raw_commands)
                ),
                axis=1,
            )
            if segment.hand_commands
            else np.empty(0, dtype=np.float64)
        )
        return {
            "blend_seconds": segment.blend_seconds,
            **{f"arm_{name}": value for name, value in arm.items()},
            **{f"hand_{name}": value for name, value in hand.items()},
            "hand_previous_command_rad": (
                None
                if segment.previous_hand is None
                else segment.previous_hand.tolist()
            ),
            "hand_commands_rad": [
                command.tolist() for command in segment.hand_commands
            ],
            "hand_raw_targets_rad": [
                command.tolist() for command in segment.hand_raw_commands
            ],
            "hand_command_time_offsets_seconds": [
                float(sent_at - segment.started_at)
                for sent_at in segment.hand_times
            ],
            "hand_tracking_error_p95_rad": self._percentile(
                hand_tracking_errors, 95
            ),
            "hand_tracking_error_max_rad": (
                None
                if not hand_tracking_errors.size
                else float(np.max(hand_tracking_errors))
            ),
            "hand_boundary_active_command_count": int(
                sum(segment.hand_boundary_active)
            ),
            "hand_velocity_limit_hit_count": (
                segment.hand_velocity_limit_hits
            ),
            "hand_acceleration_limit_hit_count": (
                segment.hand_acceleration_limit_hits
            ),
            "hand_direction_reversal_hold_count": (
                segment.hand_direction_reversal_holds
            ),
            "hand_emergency_cap_hit_count": segment.hand_emergency_cap_hits,
            "summary": (
                f"双臂发送={arm['send_count']}次 "
                f"步进P95={arm_p95 if arm_p95 is not None else '无'} "
                f"双手发送={hand['send_count']}次 "
                f"步进P95={hand_p95 if hand_p95 is not None else '无'}"
            ),
        }
