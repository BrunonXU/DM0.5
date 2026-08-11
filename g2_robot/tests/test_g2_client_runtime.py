"""解耦DM0.5客户端的纯逻辑测试。

用途：回归正式客户端的非硬件行为。
输入：mock观测、HTTP响应和临时日志路径；输出：pytest断言结果；方法：覆盖合同、
插值、调度和失败回退，不导入真实GDK也不发送动作。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from g2_sort.config.dm05_contract import PROMPT
from g2_sort.deployment.g2_client.cameras import G2CameraRig
from g2_sort.deployment.g2_client.cli import parse_args
from g2_sort.deployment.g2_client.contracts import (
    ACTION_DIM,
    CHUNK_STEPS,
    ActionChunk,
    ContractError,
    Observation,
    ObservationError,
    PolicyError,
)
from g2_sort.deployment.g2_client.control_loop import ControlLoop, RuntimeOptions
from g2_sort.deployment.g2_client.dm05_backend import (
    DM05HTTPBackend,
    encode_rgb_jpeg,
)
from g2_sort.deployment.g2_client.interpolation import (
    resample_action_intervals,
    sample_linear_action,
)
from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.deployment.g2_client.mock import MockCameraRig
from g2_sort.deployment.g2_client.observation import ObservationSource
from g2_sort.deployment.g2_client.prefetch import SingleFlightPrefetcher
from g2_sort.deployment.g2_client.robot_control import MockRobotController
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.deployment.g2_client.telemetry import DispatchTelemetry, Telemetry
from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_arm_action_chunk,
)


def test_pure_runtime_import_does_not_load_gdk_or_openpi():
    assert "agibot_gdk" not in sys.modules
    assert "openpi_client" not in sys.modules


def _observation(request_id: int = 7, captured_at: float = 1.0) -> Observation:
    images = tuple(
        np.full((4, 6, 3), fill, dtype=np.uint8) for fill in (11, 22, 33)
    )
    return Observation(
        request_id=request_id,
        captured_at=captured_at,
        images=images,
        state=np.arange(ACTION_DIM, dtype=np.float32),
        prompt=PROMPT,
    )


class _Response:
    def __init__(
        self,
        payload=None,
        json_error: Exception | None = None,
        headers=None,
    ):
        self.payload = payload
        self.json_error = json_error
        self.headers = {} if headers is None else headers

    def raise_for_status(self):
        return None

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, endpoint, *, files, data, timeout):
        self.calls.append((endpoint, files, data, timeout))
        return self.response

    def close(self):
        return None


def test_dm05_backend_serializes_frozen_contract_and_image_order():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float).tolist()
    session = _Session(_Response({"response": actions}))
    times = iter((1.1, 1.4))
    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        3.0,
        session=session,
        clock=lambda: next(times),
        image_encoder=lambda image: bytes([int(image[0, 0, 0])]),
    )

    chunk = backend.infer(_observation())

    assert chunk.actions.shape == (CHUNK_STEPS, ACTION_DIM)
    assert chunk.latency_seconds == pytest.approx(0.3)
    endpoint, files, data, timeout = session.calls[0]
    assert endpoint == "http://server:7892/process_frame"
    assert [field for field, _ in files] == ["image", "image", "image"]
    assert [payload[1] for _, payload in files] == [b"\x0b", b"\x16", b"!"]
    assert [payload[0] for _, payload in files] == [
        "head.jpg",
        "left_wrist.jpg",
        "right_wrist.jpg",
    ]
    assert json.loads(data["states"]) == list(map(float, range(ACTION_DIM)))
    assert data["text"] == PROMPT
    assert timeout == 3.0
    assert chunk.timing_seconds["client_backend_total_seconds"] >= 0.0
    assert chunk.timing_seconds["client_http_post_seconds"] >= 0.0


def test_dm05_backend_records_optional_server_timing_without_affecting_actions():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float).tolist()
    response = _Response(
        {
            "response": actions,
            "timing_seconds": {
                "server_prepare_input_seconds": 0.012,
                "server_model_and_device_to_cpu_seconds": 0.345,
                "invalid": "ignored",
            },
        },
        headers={"X-DM05-Handler-Seconds": "0.400"},
    )
    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        session=_Session(response),
        clock=iter((1.1, 1.6)).__next__,
        image_encoder=lambda _image: b"jpeg",
    )

    chunk = backend.infer(_observation())

    assert chunk.timing_seconds["server_prepare_input_seconds"] == pytest.approx(
        0.012
    )
    assert chunk.timing_seconds[
        "server_model_and_device_to_cpu_seconds"
    ] == pytest.approx(0.345)
    assert chunk.timing_seconds["server_handler_seconds"] == pytest.approx(0.4)
    assert "invalid" not in chunk.timing_seconds


def test_observation_source_records_camera_and_state_capture_timing():
    timer = iter((10.0, 10.02, 10.03))
    source = ObservationSource(
        MockCameraRig(),
        MockRobotController(np.zeros(ACTION_DIM), powered=False),
        clock=lambda: 1.0,
        timer=timer.__next__,
    )

    observation = source.capture()

    assert observation.timing_seconds[
        "client_camera_capture_seconds"
    ] == pytest.approx(0.02)
    assert observation.timing_seconds["client_state_read_seconds"] == pytest.approx(
        0.01
    )
    assert observation.timing_seconds[
        "client_observation_capture_seconds"
    ] == pytest.approx(0.03)


def test_jpeg_encoder_works_without_opencv_and_preserves_jpeg_container():
    encoded = encode_rgb_jpeg(np.full((8, 9, 3), 127, dtype=np.uint8))
    assert encoded.startswith(b"\xff\xd8")
    assert encoded.endswith(b"\xff\xd9")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"response": []},
        {"response": np.zeros((49, ACTION_DIM)).tolist()},
        {"response": np.zeros((CHUNK_STEPS, ACTION_DIM - 1)).tolist()},
        {
            "response": np.full(
                (CHUNK_STEPS, ACTION_DIM), np.nan
            ).tolist()
        },
        {
            "response": np.full(
                (CHUNK_STEPS, ACTION_DIM), np.inf
            ).tolist()
        },
        {
            "response": [
                ["0.0", *([0.0] * (ACTION_DIM - 1))],
                *np.zeros((CHUNK_STEPS - 1, ACTION_DIM)).tolist(),
            ]
        },
        {
            "response": [
                [True, *([0.0] * (ACTION_DIM - 1))],
                *np.zeros((CHUNK_STEPS - 1, ACTION_DIM)).tolist(),
            ]
        },
    ],
)
def test_dm05_backend_rejects_invalid_response_contract(payload):
    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        session=_Session(_Response(payload)),
        clock=iter((1.1, 1.2)).__next__,
        image_encoder=lambda _image: b"jpeg",
    )
    with pytest.raises(PolicyError):
        backend.infer(_observation())


def test_dm05_backend_rejects_non_json_response():
    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        session=_Session(_Response(json_error=ValueError("not JSON"))),
        clock=iter((1.1, 1.2)).__next__,
        image_encoder=lambda _image: b"jpeg",
    )
    with pytest.raises(PolicyError, match="HTTP request failed"):
        backend.infer(_observation())


def test_dm05_backend_wraps_network_timeout_as_policy_error():
    class TimeoutSession(_Session):
        def post(self, endpoint, *, files, data, timeout):
            raise TimeoutError("synthetic request timeout")

    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        session=TimeoutSession(None),
        clock=lambda: 1.1,
        image_encoder=lambda _image: b"jpeg",
    )
    with pytest.raises(PolicyError, match="synthetic request timeout"):
        backend.infer(_observation())


def test_observation_rejects_wrong_image_shape_and_dtype():
    valid = np.zeros((4, 5, 3), dtype=np.uint8)
    with pytest.raises(ContractError, match="uint8"):
        Observation(
            request_id=0,
            captured_at=1.0,
            images=(
                valid.astype(np.float32),
                valid,
                valid,
            ),
            state=np.zeros(ACTION_DIM),
        )
    with pytest.raises(ContractError, match="RGB HWC"):
        Observation(
            request_id=0,
            captured_at=1.0,
            images=(
                np.zeros((4, 5), dtype=np.uint8),
                valid,
                valid,
            ),
            state=np.zeros(ACTION_DIM),
        )


def test_interpolation_constant_ramp_endpoints_duration_and_no_overrun():
    constant = np.full((CHUNK_STEPS, ACTION_DIM), 0.2)
    assert np.allclose(sample_linear_action(constant, 0.417), 0.2)

    ramp = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float)
    ramp[:, 0] = np.arange(CHUNK_STEPS)
    at_10ms = sample_linear_action(ramp, 0.01)
    assert at_10ms[0] == pytest.approx(0.3)
    assert np.all(at_10ms[1:] == 0.0)
    assert sample_linear_action(ramp, 99.0)[0] == 49.0

    times, targets = resample_action_intervals(ramp)
    assert len(times) == 167
    assert times[0] == 0.0
    assert times[-1] == pytest.approx(1.66)
    assert targets[:, 0].min() >= 0.0
    assert targets[:, 0].max() <= 49.0
    assert np.all(np.diff(targets[:, 0]) >= 0.0)

    multi_joint = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float)
    multi_joint[:, 0] = np.arange(CHUNK_STEPS)
    multi_joint[:, 1] = -2.0 * np.arange(CHUNK_STEPS)
    sampled = sample_linear_action(multi_joint, 0.01)
    assert sampled[0] == pytest.approx(0.3)
    assert sampled[1] == pytest.approx(-0.6)


def test_split_control_entry_uses_sixty_ms_smoothstep():
    sent = []
    clock_value = 0.0
    servo = ServoStreamer(
        lambda target: sent.append(target.copy()) or True,
        None,
        clock=lambda: clock_value,
        smoothstep_blend=True,
    )
    actions = np.ones((CHUNK_STEPS, ACTION_DIM), dtype=float)
    servo.play(
        actions,
        start_index=0,
        interval_count=1,
        blend_seconds=0.1,
        anchor=np.zeros(ACTION_DIM),
    )
    target = servo.tick_once(now=0.025)
    assert target[0] == pytest.approx(0.15625)


def test_quintic_boundary_profile_is_switchable_and_has_expected_shape():
    servo = ServoStreamer(
        lambda _target: True,
        None,
        clock=lambda: 0.0,
        boundary_blend_profile="quintic_smootherstep",
    )
    actions = np.ones((CHUNK_STEPS, ACTION_DIM), dtype=float)
    servo.play(
        actions,
        start_index=0,
        interval_count=1,
        blend_seconds=0.1,
        anchor=np.zeros(ACTION_DIM),
    )
    # smootherstep(0.25) = 0.103515625; it eases in more gently than cubic.
    target = servo.tick_once(now=0.025)
    assert target[0] == pytest.approx(0.103515625)


def test_cli_boundary_profile_defaults_to_cubic_and_accepts_quintic():
    assert parse_args([]).boundary_blend_profile == "cubic_smoothstep"
    args = parse_args(
        ["--boundary-blend-profile", "quintic_smootherstep"]
    )
    assert args.boundary_blend_profile == "quintic_smootherstep"


def test_dispatch_telemetry_summarizes_acknowledged_targets_per_chunk(tmp_path):
    telemetry_path = tmp_path / "dispatch.jsonl"
    dispatch = DispatchTelemetry(Telemetry(telemetry_path))
    arm0 = np.zeros(14)
    hand0 = np.zeros(20)

    dispatch.begin_segment(
        mode="powered-sync",
        request_id=1,
        blend_seconds=0.12,
        expected_duration_seconds=0.04,
        started_at=10.0,
    )
    dispatch.record_arm_command(arm0, sent_at=10.00)
    dispatch.record_arm_command(arm0 + 0.01, sent_at=10.01)
    dispatch.record_arm_command(arm0 + 0.03, sent_at=10.02)
    dispatch.record_arm_command(arm0 + 0.04, sent_at=10.03)
    dispatch.record_hand_command(hand0 + 0.2, sent_at=10.00)
    dispatch.record_hand_command(hand0 + 0.5, sent_at=10.20)
    dispatch.finish_segment(reason="playback_completed")

    dispatch.begin_segment(
        mode="powered-sync",
        request_id=2,
        blend_seconds=0.12,
        started_at=20.0,
    )
    dispatch.record_arm_command(arm0 + 0.06, sent_at=20.00)
    dispatch.record_arm_command(arm0 + 0.07, sent_at=20.01)
    dispatch.record_hand_command(hand0 + 0.8, sent_at=20.00)
    dispatch.finish_segment(reason="playback_completed")

    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    first_hand = next(
        event for event in events if event["event"] == "first_hand_command"
    )
    assert first_hand["request_id"] == 1
    assert first_hand["source"] == "split_move_ee_pos"
    assert first_hand["positions"] == pytest.approx([0.2] * 20)

    summaries = [
        event for event in events if event["event"] == "dispatch_summary"
    ]
    assert len(summaries) == 2
    first, second = summaries
    assert first["arm_send_count"] == 4
    assert first["arm_expected_command_dimension"] == 14
    assert first["arm_command_dimension_min"] == 14
    assert first["arm_command_dimension_max"] == 14
    assert first["arm_all_commands_complete"] is True
    assert first["arm_all_commands_finite"] is True
    assert first["arm_expected_send_count_at_nominal_rate"] == pytest.approx(4.0)
    assert first["arm_send_coverage_ratio"] == pytest.approx(1.0)
    assert first["arm_effective_send_hz"] == pytest.approx(100.0)
    assert first["arm_send_interval_max_seconds"] == pytest.approx(0.01)
    assert first["arm_send_interval_over_1_5x_period_count"] == 0
    assert first["arm_send_interval_over_2x_period_count"] == 0
    assert first["arm_blend_window_send_count"] == 4
    assert first["arm_blend_window_actual_max_velocity_rad_s"] == pytest.approx(
        2.0
    )
    assert first["arm_max_delta_rad"] == pytest.approx(0.02)
    assert first["arm_max_delta_joint"] == LEFT_ARM_JOINT_NAMES[0]
    assert first["arm_send_interval_p50_seconds"] == pytest.approx(0.01)
    assert first["hand_send_count"] == 2
    assert first["hand_max_delta_rad"] == pytest.approx(0.3)
    assert first["hand_previous_command_rad"] is None
    np.testing.assert_allclose(
        first["hand_commands_rad"], [[0.2] * 20, [0.5] * 20]
    )
    assert first["hand_command_time_offsets_seconds"] == pytest.approx(
        [0.0, 0.2]
    )
    np.testing.assert_allclose(
        first["hand_raw_targets_rad"], [[0.2] * 20, [0.5] * 20]
    )
    assert first["hand_tracking_error_p95_rad"] == pytest.approx(0.0)
    assert first["hand_boundary_active_command_count"] == 0
    assert first["hand_velocity_limit_hit_count"] == 0
    assert first["hand_acceleration_limit_hit_count"] == 0
    assert first["hand_direction_reversal_hold_count"] == 0
    assert first["hand_emergency_cap_hit_count"] == 0
    assert second["arm_boundary_first_delta_rad"] == pytest.approx(0.02)
    assert second["hand_boundary_first_delta_rad"] == pytest.approx(0.3)
    assert second["hand_previous_command_rad"] == pytest.approx([0.5] * 20)
    assert second["arm_blend_window_max_delta_rad"] == pytest.approx(0.02)


def _body_entries(missing: str | None = None):
    names = (*LEFT_ARM_JOINT_NAMES, *RIGHT_ARM_JOINT_NAMES)
    return [
        {"name": name, "motor_position": index * 0.01}
        for index, name in enumerate(names)
        if name != missing
    ]


def _hand_payload(names):
    return {
        "names": list(reversed(names)),
        "end_states": [
            {"position": index * 0.02}
            for index, _ in enumerate(reversed(names))
        ],
    }


def test_strict_state_reader_uses_names_and_never_zero_fills_missing_joint():
    robot = SimpleNamespace(
        get_joint_states=lambda: {"states": _body_entries()},
        get_end_state=lambda: {
            "left_end_state": _hand_payload(LEFT_HAND_JOINT_NAMES),
            "right_end_state": _hand_payload(RIGHT_HAND_JOINT_NAMES),
        },
    )
    state = StrictRobotStateReader(robot).read()
    assert state.shape == (ACTION_DIM,)
    # Reversed SDK order is mapped back by name, not consumed positionally.
    assert state[14] == pytest.approx(0.18)
    assert state[23] == pytest.approx(0.0)

    missing_robot = SimpleNamespace(
        get_joint_states=lambda: {
            "states": _body_entries(missing=LEFT_ARM_JOINT_NAMES[0])
        },
        get_end_state=robot.get_end_state,
    )
    with pytest.raises(ObservationError, match="missing required joints"):
        StrictRobotStateReader(missing_robot).read()


def test_strict_state_reader_rejects_ambiguous_unnamed_hand_order():
    robot = SimpleNamespace(
        get_joint_states=lambda: {"states": _body_entries()},
        get_end_state=lambda: {
            "left_end_state": {
                "end_states": [{"position": 0.0}] * 10,
            },
            "right_end_state": _hand_payload(RIGHT_HAND_JOINT_NAMES),
        },
    )
    with pytest.raises(ObservationError, match="unnamed joint"):
        StrictRobotStateReader(robot).read()


def test_camera_failure_raises_instead_of_generating_black_frame():
    fake_gdk = SimpleNamespace(
        CameraType=SimpleNamespace(
            kHeadColor=1, kHandLeftColor=2, kHandRightColor=3
        )
    )
    camera = SimpleNamespace(get_latest_image=lambda *_args: None)
    rig = G2CameraRig(
        stabilization_seconds=0,
        gdk_module=fake_gdk,
        camera=camera,
    )
    with pytest.raises(ObservationError, match="returned no frame"):
        rig.capture()


def test_cli_defaults_shadow_and_safe_test_conflicts_with_powered():
    args = parse_args([])
    assert args.mode == "shadow"
    assert args.policy_backend == "dm05"
    assert args.policy_endpoint == "http://10.130.153.114:7892/process_frame"
    assert args.servo_output_mode == "split"
    assert args.last_hand_command_json.as_posix() == (
        "logs/runtime/last_hand_command.json"
    )
    assert args.hand_startup_delay_seconds == pytest.approx(0.08)
    assert args.hand_smoothing_profile == "off"
    assert args.hand_boundary_blend_seconds == pytest.approx(0.4)
    assert args.hand_max_velocity_rad_s == pytest.approx(2.0)
    assert args.hand_max_acceleration_rad_s2 == pytest.approx(10.0)
    assert args.hand_max_delta_radians == pytest.approx(0.4)
    assert args.hand_reversal_error_radians == pytest.approx(0.15)
    assert args.arm_internal_jerk_weight == 0.0
    assert args.execute_prefix_steps == 6
    assert args.chunk_prefetch_lookahead == 20
    assert args.max_start_jump_radians == pytest.approx(0.05)
    assert parse_args(["--safe-test"]).mode == "shadow"
    with pytest.raises(SystemExit):
        parse_args(["--safe-test", "--mode", "powered-sync"])
    assert parse_args(["--arm-internal-jerk-weight", "10"]).arm_internal_jerk_weight == 10
    with pytest.raises(SystemExit):
        parse_args(["--arm-internal-jerk-weight", "101"])


def test_arm_internal_jerk_smoothing_preserves_endpoints_and_hands():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    actions[24:, 5] = 0.08
    actions[:, 14:] = np.arange(20, dtype=np.float64)

    smoothed, stats = smooth_arm_action_chunk(actions, jerk_weight=10.0)

    np.testing.assert_array_equal(smoothed[[0, -1]], actions[[0, -1]])
    np.testing.assert_array_equal(smoothed[:, 14:], actions[:, 14:])
    assert np.max(np.abs(np.diff(smoothed[:, 5]))) < 0.08
    assert stats.jerk_weight == 10.0
    assert stats.max_delta_joint_index == 5
    assert stats.max_delta_rad > 0


class _TrajectoryBackend:
    def __init__(self, delay: float = 0.0, fail_after: int | None = None):
        self.delay = delay
        self.fail_after = fail_after
        self.calls = 0
        self.max_concurrent = 0
        self._concurrent = 0
        self.sent_during_infer = 0
        self.robot = None

    def infer(self, observation):
        self.calls += 1
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        before = len(self.robot.sent_actions) if self.robot is not None else 0
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_after is not None and self.calls > self.fail_after:
                raise PolicyError("synthetic network failure")
            actions = np.repeat(
                observation.state.astype(float)[None, :],
                CHUNK_STEPS,
                axis=0,
            )
            actions[:, 0] += np.arange(CHUNK_STEPS) * 0.001
            now = time.monotonic()
            return ActionChunk(
                request_id=observation.request_id,
                observation_at=observation.captured_at,
                request_started_at=observation.captured_at,
                response_received_at=now,
                actions=actions,
            )
        finally:
            if self.robot is not None:
                self.sent_during_infer += len(self.robot.sent_actions) - before
            self._concurrent -= 1

    def close(self):
        return None


def test_shadow_runs_full_contract_without_any_servo_instance():
    robot = MockRobotController(powered=False)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend()
    loop = ControlLoop(
        options=RuntimeOptions(mode="shadow", max_requests=1),
        observations=source,
        backend=backend,
        telemetry=Telemetry(),
        robot=None,
        servo=None,
    )
    summary = loop.run()
    assert backend.calls == 1
    assert robot.sent_actions == []
    assert summary["count"] == 1


def test_powered_sync_sends_nothing_during_first_http_then_executes_six_intervals():
    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend(delay=0.05)
    backend.robot = robot
    servo = ServoStreamer(robot.send_action, None)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-sync",
            max_requests=1,
            execute_prefix_steps=6,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=source,
        backend=backend,
        telemetry=Telemetry(),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    assert backend.sent_during_infer == 0
    assert servo.stats()["send_count"] > 0
    assert robot.read_state()[0] == pytest.approx(0.006, abs=1e-5)


def test_powered_sync_start_jump_rejection_sends_zero_servo_commands(tmp_path):
    class LargeStartJumpBackend(_TrajectoryBackend):
        def infer(self, observation):
            self.calls += 1
            actions = np.repeat(
                observation.state.astype(float)[None, :],
                CHUNK_STEPS,
                axis=0,
            )
            actions[:, 15] = 0.9
            now = time.monotonic()
            return ActionChunk(
                request_id=observation.request_id,
                observation_at=observation.captured_at,
                request_started_at=observation.captured_at,
                response_received_at=now,
                actions=actions,
            )

    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    telemetry_path = tmp_path / "start_jump_zero_send.jsonl"
    servo = ServoStreamer(robot.send_action, None)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-sync",
            max_requests=1,
            start_jump_limit_radians=0.2,
        ),
        observations=source,
        backend=LargeStartJumpBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "start_jump_rejected" for event in events)
    assert robot.sent_actions == []
    assert servo.stats()["send_count"] == 0


def test_split_profile_warns_but_executes_large_start_jump(tmp_path):
    class LargeStartJumpBackend(_TrajectoryBackend):
        def infer(self, observation):
            actions = np.repeat(
                observation.state.astype(float)[None, :],
                CHUNK_STEPS,
                axis=0,
            )
            actions[:, 15] = 0.93
            now = time.monotonic()
            return ActionChunk(
                request_id=observation.request_id,
                observation_at=observation.captured_at,
                request_started_at=observation.captured_at,
                response_received_at=now,
                actions=actions,
            )

    robot = MockRobotController(powered=True)
    telemetry_path = tmp_path / "split_jump_warning.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-sync",
            max_requests=1,
            execute_prefix_steps=1,
            start_jump_limit_radians=0.05,
            boundary_blend_seconds=0.0,
            enforce_action_safety_gates=False,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=LargeStartJumpBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    warning = next(event for event in events if event["event"] == "start_jump_warning")
    assert warning["arm_start_jump_rad"] == pytest.approx(0.0)
    assert warning["hand_start_jump_rad"] == pytest.approx(0.93)
    action = next(event for event in events if event["event"] == "action_chunk")
    assert action["arm_start_jump_rad"] == pytest.approx(0.0)
    assert action["hand_start_jump_rad"] == pytest.approx(0.93)
    assert servo.stats()["send_count"] > 0


def test_prefetcher_enforces_one_request_in_flight():
    gate = threading.Event()

    class BlockingBackend(_TrajectoryBackend):
        def infer(self, observation):
            gate.wait(0.5)
            return super().infer(observation)

    backend = BlockingBackend()
    prefetcher = SingleFlightPrefetcher(backend)
    observation = _observation(captured_at=time.monotonic())
    assert prefetcher.submit(observation, playback_id=1)
    assert not prefetcher.submit(observation, playback_id=1)
    assert prefetcher.has_inflight()
    gate.set()
    deadline = time.monotonic() + 1.0
    while prefetcher.has_inflight() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert prefetcher.take_ready() is not None
    prefetcher.close()


def test_action_chunk_stale_prefix_and_fresh_gate_math():
    chunk = ActionChunk(
        request_id=1,
        observation_at=10.0,
        request_started_at=10.1,
        response_received_at=10.2,
        actions=np.zeros((CHUNK_STEPS, ACTION_DIM)),
    )
    assert chunk.stale_prefix_steps(10.0) == 0
    assert chunk.stale_prefix_steps(10.01) == 1
    assert chunk.stale_prefix_steps(10.5) == 15
    assert CHUNK_STEPS - chunk.stale_prefix_steps(11.3) == 11


def test_powered_async_prefetches_once_and_never_overlaps_requests(monkeypatch):
    # Speed up only the local playback clock for this state-machine test.
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)
    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend(delay=0.01)
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-async",
            max_requests=2,
            chunk_prefetch_lookahead=20,
            async_min_fresh_steps=12,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=source,
        backend=backend,
        telemetry=Telemetry(),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    assert backend.calls == 2
    assert backend.max_concurrent == 1
    assert len(robot.sent_actions) > 10


def test_powered_async_discards_response_that_arrives_after_chunk(monkeypatch, tmp_path):
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)
    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend(delay=0.15)
    telemetry_path = tmp_path / "telemetry.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-async",
            max_requests=2,
            chunk_prefetch_lookahead=20,
            async_min_fresh_steps=12,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=source,
        backend=backend,
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    holds = [
        event.get("reason", "")
        for event in events
        if event["event"] == "safe_hold"
    ]
    assert any("late async response discarded" in reason for reason in holds)


def test_powered_async_network_failure_clears_chunk_and_holds(monkeypatch, tmp_path):
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)
    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend(delay=0.01, fail_after=1)
    telemetry_path = tmp_path / "network_failure.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-async",
            max_requests=2,
            chunk_prefetch_lookahead=20,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=source,
        backend=backend,
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event"] == "safe_hold"
        and "async policy request failed" in event.get("reason", "")
        for event in events
    )
    assert not servo.is_playing()


def test_powered_sync_rejects_mismatched_response_request_id(tmp_path):
    class WrongIDBackend(_TrajectoryBackend):
        def infer(self, observation):
            chunk = super().infer(observation)
            return ActionChunk(
                request_id=observation.request_id + 1,
                observation_at=chunk.observation_at,
                request_started_at=chunk.request_started_at,
                response_received_at=chunk.response_received_at,
                actions=chunk.actions,
            )

    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    telemetry_path = tmp_path / "wrong_id.jsonl"
    servo = ServoStreamer(robot.send_action, None)
    loop = ControlLoop(
        options=RuntimeOptions(mode="powered-sync", max_requests=1),
        observations=source,
        backend=WrongIDBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "response_out_of_order" for event in events)
    assert robot.sent_actions == []
    assert servo.stats()["send_count"] == 0


def test_powered_async_enforces_twelve_fresh_interval_gate(monkeypatch, tmp_path):
    import g2_sort.deployment.g2_client.contracts as contracts_module
    import g2_sort.deployment.g2_client.servo as servo_module

    # Accelerate both playback and stale-age math equally.  With lookahead 49,
    # the second response lands before the old chunk ends but leaves <12 steps.
    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)
    monkeypatch.setattr(contracts_module, "MODEL_HZ", 300.0)
    robot = MockRobotController(powered=True)
    source = ObservationSource(MockCameraRig(), robot)
    backend = _TrajectoryBackend(delay=0.13)
    telemetry_path = tmp_path / "fresh_gate.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-async",
            max_requests=2,
            chunk_prefetch_lookahead=49,
            async_min_fresh_steps=12,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=source,
        backend=backend,
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )
    loop.run()
    servo.stop()
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event"] == "safe_hold"
        and "fresh intervals" in event.get("reason", "")
        for event in events
    )
