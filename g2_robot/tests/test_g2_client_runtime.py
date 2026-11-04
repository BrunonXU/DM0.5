"""解耦DM0.5客户端的纯逻辑测试。

用途：回归正式客户端的非硬件行为。
输入：mock观测、HTTP响应和临时日志路径；输出：pytest断言结果；方法：覆盖合同、
插值、调度和失败回退，不导入真实GDK也不发送动作。
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from g2_sort.config.dm05_contract import PROMPT
from g2_sort.deployment.g2_client.cameras import G2CameraRig
from g2_sort.deployment.g2_client.binary_protocol import (
    BinaryRequest,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
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
from g2_sort.deployment.g2_client.dm05_websocket_backend import (
    DM05WebSocketBackend,
)
from g2_sort.deployment.g2_client.interpolation import (
    arm_cubic_spline_tangents,
    arm_cubic_tangents,
    resample_action_intervals,
    sample_cubic_arm_linear_hand_action,
    sample_linear_action,
)
from g2_sort.deployment.g2_client.image_transport import EncodedImage
from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.deployment.g2_client.mock import MockCameraRig
from g2_sort.deployment.g2_client.observation import (
    ObservationSource,
    StateOnlyObservationSource,
)
from g2_sort.deployment.g2_client.prefetch import SingleFlightPrefetcher
from g2_sort.deployment.g2_client.robot_control import MockRobotController
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader
from g2_sort.deployment.g2_client.rtc import (
    RTCParameters,
    RTCRequest,
    arrived_chunk_suffix,
    conservative_delay_steps,
    measure_rtc_plan_disagreement,
    mixed_prefix_from_absolute,
    project_absolute_rtc_prefix,
    request_from_committed_chunk,
)
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.deployment.g2_client.telemetry import DispatchTelemetry, Telemetry
from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_arm_action_chunk,
    smooth_rtc_arm_action_suffix,
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


def test_dm05_backend_serializes_rtc_contract_without_changing_baseline():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float).tolist()
    session = _Session(_Response({"response": actions}))
    backend = DM05HTTPBackend(
        "http://server:7892/process_frame",
        session=session,
        clock=iter((1.1, 1.2)).__next__,
        image_encoder=lambda _image: b"jpeg",
    )
    parameters = RTCParameters(predicted_delay_steps=20, execution_horizon=25)
    prefix = np.arange(25 * ACTION_DIM, dtype=np.float64).reshape(25, ACTION_DIM)

    backend.infer(_observation(), RTCRequest(prefix, parameters))

    data = session.calls[0][2]
    assert json.loads(data["rtc_prefix_actions"]) == prefix.tolist()
    assert data["rtc_inference_delay_steps"] == "20"
    assert data["rtc_execution_horizon"] == "25"


def test_rtc_timing_tail_and_mixed_action_contracts():
    parameters = RTCParameters(predicted_delay_steps=20, execution_horizon=25)
    chunk = np.arange(CHUNK_STEPS * ACTION_DIM, dtype=np.float64).reshape(
        CHUNK_STEPS, ACTION_DIM
    )
    request = request_from_committed_chunk(chunk, parameters)
    assert request.prefix_actions_absolute.shape == (25, ACTION_DIM)
    assert np.array_equal(request.prefix_actions_absolute, chunk[25:])
    assert parameters.post_response_steps_before_replan == 5
    assert conservative_delay_steps(20 / 30) == 20
    assert conservative_delay_steps(20.01 / 30) == 21
    assert np.array_equal(arrived_chunk_suffix(chunk, 20), chunk[20:])

    state = np.arange(ACTION_DIM, dtype=np.float32)
    absolute = np.tile(state + 2.0, (25, 1))
    mixed = mixed_prefix_from_absolute(absolute, state)
    assert np.allclose(mixed[:, :14], 2.0)
    assert np.array_equal(mixed[:, 14:], absolute[:, 14:])


def test_rtc_plan_disagreement_detects_delayed_arm_reversal():
    prefix = np.zeros((30, ACTION_DIM), dtype=np.float64)
    prefix[:, 0] = np.arange(30) * 0.01
    candidate = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    candidate[16:, 0] = prefix[16, 0]
    candidate[17:27, 0] -= np.linspace(0.01, 0.10, 10)
    candidate[16:27, 14] = prefix[16:27, 14] + np.linspace(0.0, 0.2, 11)

    result = measure_rtc_plan_disagreement(
        candidate,
        prefix,
        switch_delay=16,
        horizon_steps=10,
    )

    assert result.horizon_steps == 10
    assert result.arm_direction_reversal_count == 1
    assert result.arm_reverse_displacement_max_rad == pytest.approx(0.10)
    assert result.arm_plan_divergence_max_rad == pytest.approx(0.20)
    assert result.hand_plan_divergence_max_rad == pytest.approx(0.20)


def test_rtc_plan_disagreement_ignores_stationary_incoming_joint_noise():
    prefix = np.zeros((30, ACTION_DIM), dtype=np.float64)
    candidate = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    candidate[17:27, 0] = -np.linspace(1e-5, 1e-4, 10)

    result = measure_rtc_plan_disagreement(
        candidate,
        prefix,
        switch_delay=16,
    )

    assert result.arm_direction_reversal_count == 0
    assert result.arm_reverse_displacement_max_rad == 0.0


@pytest.mark.parametrize(
    "delay,execution",
    [(26, 26), (20, 31), (21, 20), (-1, 25)],
)
def test_rtc_parameters_reject_infeasible_schedule(delay, execution):
    with pytest.raises(ContractError, match="d <= s <= H-d"):
        RTCParameters(predicted_delay_steps=delay, execution_horizon=execution)


def test_powered_rtc_cannot_disable_hard_action_safety_gates():
    with pytest.raises(ValueError, match="powered RTC requires hard"):
        RuntimeOptions(mode="powered-rtc", enforce_action_safety_gates=False)


def test_dm05_backend_records_optional_server_timing_without_affecting_actions():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=float).tolist()
    response = _Response(
        {
            "response": actions,
            "timing_seconds": {
                "server_prepare_input_seconds": 0.012,
                "server_model_and_device_to_cpu_seconds": 0.345,
                "server_total_before_jsonify_seconds": 0.390,
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
    assert chunk.timing_seconds["server_jsonify_and_framework_seconds"] == pytest.approx(
        0.01
    )
    assert chunk.timing_seconds["server_non_model_handler_seconds"] == pytest.approx(
        0.055
    )
    assert chunk.timing_seconds["client_e2e_to_worker_seconds"] == pytest.approx(
        0.6
    )
    assert chunk.timing_seconds["client_non_model_to_worker_seconds"] == pytest.approx(
        0.255
    )
    assert chunk.timing_seconds["client_non_model_residual_worker_seconds"] >= 0
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


def test_cubic_arm_interpolation_is_c1_and_leaves_hands_linear():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    positions = np.arange(CHUNK_STEPS, dtype=np.float64)
    actions[:, 0] = 0.01 * positions**2
    actions[:, 14] = positions
    tangents = arm_cubic_tangents(actions)
    epsilon = 1e-6

    left = sample_cubic_arm_linear_hand_action(
        actions,
        (10.0 - epsilon) / 30.0,
        arm_tangents=tangents,
    )
    knot = sample_cubic_arm_linear_hand_action(
        actions,
        10.0 / 30.0,
        arm_tangents=tangents,
    )
    right = sample_cubic_arm_linear_hand_action(
        actions,
        (10.0 + epsilon) / 30.0,
        arm_tangents=tangents,
    )

    np.testing.assert_allclose(knot, actions[10], atol=1e-12)
    assert (knot[0] - left[0]) / epsilon == pytest.approx(
        (right[0] - knot[0]) / epsilon,
        rel=1e-4,
    )
    # The O10 target path intentionally retains the old linear semantics.
    assert left[14] == pytest.approx(10.0 - epsilon)
    assert right[14] == pytest.approx(10.0 + epsilon)


def test_cubic_spline_tangents_are_clamped_c1_and_internal_c2():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    x = np.arange(CHUNK_STEPS, dtype=np.float64)
    actions[:, 0] = 0.0001 * x**3 - 0.004 * x**2 + 0.03 * x
    actions[:, 1] = 0.02 * np.sin(x / 4.0)
    start = 16
    incoming = np.linspace(-0.01, 0.01, 14)

    tangents = arm_cubic_spline_tangents(
        actions,
        start_index=start,
        start_velocity_per_step=incoming,
    )

    np.testing.assert_allclose(tangents[start], incoming, atol=1e-12)
    for index in range(start + 1, CHUNK_STEPS - 1):
        left_acceleration = (
            6.0 * actions[index - 1, :14]
            + 2.0 * tangents[index - 1]
            - 6.0 * actions[index, :14]
            + 4.0 * tangents[index]
        )
        right_acceleration = (
            -6.0 * actions[index, :14]
            - 4.0 * tangents[index]
            + 6.0 * actions[index + 1, :14]
            - 2.0 * tangents[index + 1]
        )
        np.testing.assert_allclose(
            left_acceleration,
            right_acceleration,
            atol=1e-12,
        )


def test_cubic_servo_start_velocity_override_preserves_rtc_boundary_speed():
    servo = ServoStreamer(
        lambda _target: True,
        None,
        clock=lambda: 0.0,
        arm_interpolation_profile="cubic_hermite",
    )
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    actions[16:, 0] = np.linspace(0.5, 1.0, CHUNK_STEPS - 16)
    incoming = np.zeros(14, dtype=np.float64)
    incoming[0] = 0.02
    servo.play(
        actions,
        start_index=16,
        interval_count=20,
        anchor=actions[16],
        arm_start_velocity_per_step=incoming,
    )

    at_zero = servo.tick_once(now=0.0)
    at_small_step = servo.tick_once(now=1e-6)

    assert at_zero[0] == pytest.approx(0.5)
    assert (at_small_step[0] - at_zero[0]) / (30e-6) == pytest.approx(
        incoming[0], rel=1e-3
    )


def test_servo_missed_deadline_drops_catchup_tick():
    now = [0.0]
    tick_starts = []
    servo = None

    def sender(_target):
        tick_starts.append(now[0])
        # The first native call overruns one 10 ms period.  Later calls are
        # short, so a correct scheduler should recover at a normal cadence.
        now[0] += 0.012 if len(tick_starts) == 1 else 0.001
        if len(tick_starts) == 3:
            servo._stop.set()
        return True

    def sleep(seconds):
        now[0] += seconds

    servo = ServoStreamer(
        sender,
        np.zeros(ACTION_DIM),
        servo_hz=100.0,
        clock=lambda: now[0],
        sleep=sleep,
    )
    servo._run()

    assert tick_starts == pytest.approx([0.0, 0.022, 0.032])
    assert np.min(np.diff(tick_starts)) == pytest.approx(0.01)


def test_servo_late_wakeup_does_not_recover_phase_with_short_tick():
    now = [0.0]
    tick_starts = []
    sleep_count = [0]
    servo = None

    def sender(_target):
        tick_starts.append(now[0])
        now[0] += 0.001
        if len(tick_starts) == 3:
            servo._stop.set()
        return True

    def sleep(seconds):
        # First sleep wakes 6 ms late without crossing two full periods.  The
        # old absolute-phase scheduler then issued the next target after 4 ms.
        overshoot = 0.006 if sleep_count[0] == 0 else 0.0
        sleep_count[0] += 1
        now[0] += seconds + overshoot

    servo = ServoStreamer(
        sender,
        np.zeros(ACTION_DIM),
        servo_hz=100.0,
        clock=lambda: now[0],
        sleep=sleep,
    )
    servo._run()

    assert tick_starts == pytest.approx([0.0, 0.016, 0.026])
    assert np.min(np.diff(tick_starts)) == pytest.approx(0.01)


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


def test_stationary_arm_minjerk_projection_removes_only_micro_reversal():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    index = np.arange(CHUNK_STEPS, dtype=np.float64)
    actions[:, 0] = 0.001 * np.sin(index * np.pi / 3.0)
    actions[:, 14] = 0.1 * index
    candidate = ServoStreamer(
        lambda _target: True,
        None,
        clock=lambda: 0.0,
        arm_interpolation_profile="cubic_spline",
        arm_micro_motion_profile="stationary_minjerk_v1",
        arm_micro_motion_threshold_rad=0.0035,
    )
    baseline = ServoStreamer(
        lambda _target: True,
        None,
        clock=lambda: 0.0,
        arm_interpolation_profile="cubic_spline",
    )
    candidate_id = candidate.play(
        actions,
        start_index=0,
        interval_count=20,
        anchor=np.zeros(ACTION_DIM),
    )
    baseline.play(
        actions,
        start_index=0,
        interval_count=20,
        anchor=np.zeros(ACTION_DIM),
    )
    stats = candidate.arm_micro_motion_stats(candidate_id)
    assert stats is not None
    assert stats.active_joint_indices == (0,)
    assert stats.baseline_reversal_count >= 2
    assert stats.projected_reversal_count == 0
    assert stats.max_deviation_rad <= 0.0035

    times = np.arange(67, dtype=np.float64) / 100.0
    projected = np.stack([candidate.tick_once(now=time) for time in times])
    original = np.stack([baseline.tick_once(now=time) for time in times])
    assert np.all(np.diff(projected[:, 0]) >= -1e-12)
    np.testing.assert_allclose(projected[:, 14:], original[:, 14:])


def test_stationary_arm_minjerk_leaves_large_motion_unchanged():
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    index = np.arange(CHUNK_STEPS, dtype=np.float64)
    actions[:, 0] = 0.01 * np.sin(index * np.pi / 3.0)
    servo = ServoStreamer(
        lambda _target: True,
        None,
        clock=lambda: 0.0,
        arm_interpolation_profile="cubic_spline",
        arm_micro_motion_profile="stationary_minjerk_v1",
        arm_micro_motion_threshold_rad=0.0035,
    )
    playback_id = servo.play(
        actions,
        start_index=0,
        interval_count=20,
        anchor=np.zeros(ACTION_DIM),
    )
    stats = servo.arm_micro_motion_stats(playback_id)
    assert stats is not None
    assert stats.active_joint_indices == ()


def test_cli_boundary_profile_defaults_to_cubic_and_accepts_quintic():
    assert parse_args([]).boundary_blend_profile == "cubic_smoothstep"
    args = parse_args(
        ["--boundary-blend-profile", "quintic_smootherstep"]
    )
    assert args.boundary_blend_profile == "quintic_smootherstep"


def test_cli_arm_micro_motion_profile_is_explicit_and_defaults_off():
    assert parse_args([]).arm_micro_motion_profile == "off"
    args = parse_args(
        [
            "--arm-micro-motion-profile",
            "stationary_minjerk_v1",
            "--arm-micro-motion-threshold-rad",
            "0.0035",
        ]
    )
    assert args.arm_micro_motion_profile == "stationary_minjerk_v1"
    assert args.arm_micro_motion_threshold_rad == pytest.approx(0.0035)


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
    assert first["arm_actual_max_velocity_rad_s"] == pytest.approx(2.0)
    assert first["arm_actual_max_acceleration_rad_s2"] == pytest.approx(100.0)
    assert first["arm_actual_max_jerk_rad_s3"] == pytest.approx(20000.0)
    assert first["arm_max_delta_rad"] == pytest.approx(0.02)
    assert first["arm_max_delta_joint"] == LEFT_ARM_JOINT_NAMES[0]
    assert first["arm_send_interval_p50_seconds"] == pytest.approx(0.01)
    np.testing.assert_allclose(
        first["arm_commands_rad"],
        [arm0, arm0 + 0.01, arm0 + 0.03, arm0 + 0.04],
    )
    assert first["arm_command_time_offsets_seconds"] == pytest.approx(
        [0.0, 0.01, 0.02, 0.03]
    )
    assert first["segment_started_at_monotonic"] == pytest.approx(10.0)
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


def test_dispatch_telemetry_records_hand_commands_in_sync_wait_window(tmp_path):
    telemetry_path = tmp_path / "hand_wait.jsonl"
    dispatch = DispatchTelemetry(Telemetry(telemetry_path), hand_nominal_hz=10.0)
    hand = np.zeros(20)

    dispatch.begin_segment(
        mode="powered-sync",
        request_id=4,
        blend_seconds=0.12,
        started_at=10.0,
    )
    dispatch.record_hand_command(
        hand + 0.1,
        raw_positions=hand + 0.2,
        sent_at=10.5,
    )
    dispatch.finish_segment(reason="playback_completed")
    dispatch.record_hand_command(
        hand + 0.15,
        raw_positions=hand + 0.3,
        velocity_limit_hits=2,
        sent_at=10.7,
    )

    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    wait = next(event for event in events if event["event"] == "hand_wait_command")
    assert wait["mode"] == "powered-sync"
    assert wait["request_id"] == 4
    assert wait["phase"] == "wait"
    assert wait["sent_after_segment_seconds"] >= 0.0
    assert wait["delta_from_previous_rad"] == pytest.approx(0.05)
    assert wait["tracking_error_rad"] == pytest.approx(0.15)
    assert wait["velocity_limit_hits"] == 2
    assert wait["positions_rad"] == pytest.approx([0.15] * 20)
    assert wait["raw_target_rad"] == pytest.approx([0.3] * 20)


def test_powered_rtc_dispatch_records_arm_trace_and_uses_actual_time(tmp_path):
    telemetry_path = tmp_path / "rtc_dispatch.jsonl"
    dispatch = DispatchTelemetry(Telemetry(telemetry_path))
    arm = np.zeros(14)

    dispatch.begin_segment(
        mode="powered-rtc",
        request_id=7,
        blend_seconds=0.0,
        started_at=10.0,
    )
    # A constant 1 rad/s ramp sampled at deliberately uneven intervals.  The
    # nominal-100Hz metric sees uneven deltas, while the actual-time metric
    # must recover constant velocity with zero acceleration and jerk.
    for offset in (0.0, 0.125, 0.375, 0.5):
        command = arm.copy()
        command[0] = offset
        dispatch.record_arm_command(
            command,
            sent_at=10.0 + offset,
            send_duration_seconds=0.001 + offset / 100.0,
        )
    dispatch.finish_segment(reason="playback_completed")

    event = next(
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "dispatch_summary"
    )
    assert event["arm_actual_max_velocity_rad_s"] == pytest.approx(1.0)
    assert event["arm_actual_max_acceleration_rad_s2"] == pytest.approx(0.0)
    assert event["arm_actual_max_jerk_rad_s3"] == pytest.approx(0.0)
    assert event["arm_nominal_max_acceleration_rad_s2"] > 0.0
    assert event["arm_command_time_offsets_seconds"] == pytest.approx(
        [0.0, 0.125, 0.375, 0.5]
    )
    assert event["arm_send_duration_p50_seconds"] == pytest.approx(0.0035)
    assert event["arm_send_duration_p95_seconds"] == pytest.approx(0.0058125)
    assert event["arm_send_duration_max_seconds"] == pytest.approx(0.006)
    assert event["arm_send_durations_seconds"] == pytest.approx(
        [0.001, 0.00225, 0.00475, 0.006]
    )
    np.testing.assert_allclose(
        np.asarray(event["arm_commands_rad"])[:, 0],
        [0.0, 0.125, 0.375, 0.5],
    )


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


def _encoded_images():
    return tuple(
        EncodedImage(b"\xff\xd8" + bytes([value]), "image/jpeg", 640, 480)
        for value in (1, 2, 3)
    )


def test_binary_protocol_round_trips_rtc_images_state_and_float32_actions():
    prefix = np.arange(30 * ACTION_DIM, dtype=np.float32).reshape(30, ACTION_DIM)
    request = BinaryRequest(
        request_id=19,
        state=np.arange(ACTION_DIM, dtype=np.float32),
        images=_encoded_images(),
        rtc_prefix=prefix,
        rtc_delay=16,
        rtc_execution=20,
    )

    decoded = decode_request(encode_request(request))

    assert decoded.request_id == 19
    assert np.array_equal(decoded.state, request.state)
    assert np.array_equal(decoded.rtc_prefix, prefix)
    assert [image.data for image in decoded.images] == [
        image.data for image in request.images
    ]

    actions64 = np.linspace(-1.0, 1.0, CHUNK_STEPS * ACTION_DIM).reshape(
        CHUNK_STEPS, ACTION_DIM
    )
    payload = encode_response(19, actions64, {"server_handler_seconds": 0.125})
    response = decode_response(payload)
    assert response.request_id == 19
    assert response.actions.dtype == np.float32
    assert np.max(np.abs(response.actions.astype(np.float64) - actions64)) < 6e-8
    assert response.timing_seconds["server_handler_seconds"] == pytest.approx(0.125)


def test_binary_request_matches_server_golden_wire_hash():
    payload = encode_request(
        BinaryRequest(
            request_id=42,
            state=np.arange(ACTION_DIM, dtype=np.float32),
            images=tuple(
                EncodedImage(
                    b"\xff\xd8" + bytes([index]),
                    "image/jpeg",
                    1280,
                    720,
                )
                for index in (1, 2, 3)
            ),
        )
    )
    assert len(payload) == 208
    assert hashlib.sha256(payload).hexdigest() == (
        "e6fcee255fbbacb3ed4978fd4c7fccb677f02fe1d92e0628ce1f4ef106645e0f"
    )


def test_websocket_backend_reuses_connection_and_returns_binary_float32_actions():
    class Connection:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

        def recv(self, *, timeout):
            request = decode_request(self.sent[-1])
            actions = np.full((CHUNK_STEPS, ACTION_DIM), request.request_id, dtype=np.float32)
            return encode_response(
                request.request_id,
                actions,
                {
                    "server_handler_seconds": 0.10,
                    "server_model_and_device_to_cpu_seconds": 0.06,
                    "server_non_model_handler_seconds": 0.04,
                },
            )

        def close(self):
            return None

    connection = Connection()
    connector_calls = []

    def connector(endpoint, **kwargs):
        connector_calls.append((endpoint, kwargs))
        return connection

    times = iter((1.1, 1.25, 2.1, 2.25))
    backend = DM05WebSocketBackend(
        "ws://server:7894/process_frame",
        connector=connector,
        clock=lambda: next(times),
    )
    state = np.arange(ACTION_DIM, dtype=np.float32)
    for request_id, captured_at in ((7, 1.0), (8, 2.0)):
        chunk = backend.infer_encoded(
            request_id=request_id,
            captured_at=captured_at,
            state=state,
            images=_encoded_images(),
            observation_timing={"client_camera_capture_seconds": 0.01},
        )
        assert np.all(chunk.actions == request_id)
        assert chunk.timing_seconds["client_binary_response_bytes"] > 6800
        assert chunk.timing_seconds["client_non_model_to_worker_seconds"] == pytest.approx(0.19)

    assert len(connector_calls) == 1
    assert connector_calls[0][1]["compression"] is None
    assert len(connection.sent) == 2


def test_camera_compressed_passthrough_does_not_import_opencv():
    fake_gdk = SimpleNamespace(
        CameraType=SimpleNamespace(
            kHeadColor=1, kHandLeftColor=2, kHandRightColor=3
        ),
        Encoding=SimpleNamespace(JPEG=10, PNG=11, UNCOMPRESSED=12),
    )
    messages = {
        camera_type: SimpleNamespace(
            encoding=fake_gdk.Encoding.JPEG,
            data=b"\xff\xd8native" + bytes([camera_type]),
            width=1280,
            height=720,
        )
        for camera_type in (1, 2, 3)
    }
    camera = SimpleNamespace(
        get_latest_image=lambda camera_type, _timeout: messages[camera_type]
    )
    rig = G2CameraRig(
        stabilization_seconds=0,
        gdk_module=fake_gdk,
        camera=camera,
    )

    frames = rig.capture_encoded()

    assert [frame.data for frame in frames] == [messages[i].data for i in (1, 2, 3)]
    assert [frame.media_type for frame in frames] == ["image/jpeg"] * 3


def test_camera_transport_pair_uses_one_message_for_rgb_and_native_bytes():
    fake_gdk = SimpleNamespace(
        CameraType=SimpleNamespace(
            kHeadColor=1, kHandLeftColor=2, kHandRightColor=3
        ),
        Encoding=SimpleNamespace(JPEG=10, PNG=11, UNCOMPRESSED=12),
    )
    message = SimpleNamespace(
        encoding=fake_gdk.Encoding.JPEG,
        data=b"\xff\xd8native",
        width=640,
        height=400,
    )
    reads = []
    camera = SimpleNamespace(
        get_latest_image=lambda camera_type, _timeout: (
            reads.append(camera_type) or message
        )
    )
    rig = G2CameraRig(
        stabilization_seconds=0,
        gdk_module=fake_gdk,
        camera=camera,
    )
    expected_rgb = np.full((2, 3, 3), 17, dtype=np.uint8)
    rig._decode_rgb = lambda received, name: (  # type: ignore[method-assign]
        expected_rgb
        if received is message and name == "head"
        else None
    )

    rgb, native = rig.capture_transport_pair("head")

    assert rgb is expected_rgb
    assert native is not None
    assert native.data == message.data
    assert native.media_type == "image/jpeg"
    assert reads == [fake_gdk.CameraType.kHeadColor]


def test_cli_defaults_shadow_and_safe_test_conflicts_with_powered():
    args = parse_args([])
    assert args.mode == "shadow"
    assert args.policy_backend == "dm05"
    assert args.policy_transport == "http-multipart-json"
    assert args.policy_endpoint == "http://192.0.2.1:7892/process_frame"
    assert args.servo_output_mode == "split"
    assert args.last_hand_command_json.as_posix() == (
        "logs/runtime/last_hand_command.json"
    )
    assert args.hand_startup_delay_seconds == pytest.approx(0.08)
    assert args.hand_stream_hz == pytest.approx(5.0)
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
    assert parse_args(["--hand-stream-hz", "50"]).hand_stream_hz == pytest.approx(50.0)
    assert parse_args(["--hand-stream-hz", "10"]).hand_stream_hz == pytest.approx(10.0)
    assert parse_args(["--hand-stream-hz", "20"]).hand_stream_hz == pytest.approx(20.0)
    with pytest.raises(SystemExit):
        parse_args(["--hand-stream-hz", "100"])
    with pytest.raises(SystemExit):
        parse_args(["--arm-internal-jerk-weight", "101"])


def test_cli_binary_websocket_requires_process_isolation_and_ws_endpoint():
    common = [
        "--mode",
        "powered-rtc",
        "--policy-transport",
        "websocket-binary",
        "--policy-endpoint",
        "ws://server:7894/process_frame",
    ]
    with pytest.raises(SystemExit):
        parse_args(common)
    args = parse_args([*common, "--rtc-process-isolation"])
    assert args.policy_transport == "websocket-binary"
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "powered-rtc",
                "--policy-transport",
                "websocket-binary",
                "--policy-endpoint",
                "http://server:7894/process_frame",
                "--rtc-process-isolation",
            ]
        )


def test_cli_accepts_rtc_cubic_arms_and_boundary_cap_hands():
    args = parse_args(
        [
            "--mode",
            "powered-rtc",
            "--split-control-profile",
            "--arm-interpolation-profile",
            "cubic_hermite",
            "--hand-smoothing-profile",
            "rtc_cap_v2",
            "--hand-boundary-max-delta-radians",
            "0.15",
            "--hand-max-delta-radians",
            "0.30",
        ]
    )

    assert args.arm_interpolation_profile == "cubic_hermite"
    assert args.hand_smoothing_profile == "rtc_cap_v2"
    assert args.hand_boundary_max_delta_radians == pytest.approx(0.15)
    assert args.hand_max_delta_radians == pytest.approx(0.30)


@pytest.mark.parametrize("mode", ["powered-sync", "powered-rtc"])
def test_cli_shared_preview_profile_is_one_10hz_split_profile(mode):
    args = parse_args(
        [
            "--mode",
            mode,
            "--hand-stream-hz",
            "10",
            "--hand-smoothing-profile",
            "shared_preview_v1",
        ]
    )
    assert args.hand_smoothing_profile == "shared_preview_v1"

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                mode,
                "--hand-stream-hz",
                "5",
                "--hand-smoothing-profile",
                "shared_preview_v1",
            ]
        )


def test_formal_sync_script_selects_raw_5hz_changed_only_hands():
    project_root = Path(__file__).resolve().parents[1]
    powered = (project_root / "scripts/05_powered_sync.sh").read_text()
    assert "ws://192.0.2.1:7892/process_frame" in powered
    assert "--arm-internal-jerk-weight 10" in powered
    assert "--arm-interpolation-profile cubic_spline" in powered
    assert "--arm-micro-motion-profile stationary_minjerk_v1" in powered
    assert "--arm-micro-motion-threshold-rad 0.0035" in powered
    assert "--hand-smoothing-profile off" in powered
    assert "shared_preview_v1" not in powered


def test_formal_rtc_scripts_freeze_validated_network_and_arm_baseline():
    """Keep formal RTC aligned with the already validated Sync infrastructure."""
    project_root = Path(__file__).resolve().parents[1]
    powered = (project_root / "scripts/09_powered_rtc.sh").read_text()
    shadow = (project_root / "scripts/08_rtc_shadow.sh").read_text()
    arms_only = (
        project_root / "scripts/10_powered_rtc_arms_only_10s.sh"
    ).read_text()

    assert "NEWLORA_RTC_TRANSPORT:-websocket-binary" in powered
    assert "ws://192.0.2.1:7893/process_frame" in powered
    assert "arm_interpolation_profile=cubic_spline" in powered
    assert '--arm-internal-jerk-weight 10' in powered
    assert '--arm-interpolation-profile "$arm_interpolation_profile"' in powered
    assert '--hand-stream-hz 10' in powered
    assert '--hand-smoothing-profile rtc_cap_v2' in powered

    assert '--arm-internal-jerk-weight 10' in shadow
    assert '--arm-interpolation-profile cubic_spline' in shadow
    assert '--rtc-predicted-delay-steps 16' in arms_only
    assert '--rtc-execution-horizon 20' in arms_only
    assert '--arm-internal-jerk-weight 10' in arms_only
    assert '--arm-interpolation-profile cubic_spline' in arms_only


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


def test_rtc_arm_smoothing_uses_only_suffix_and_pins_c0_c1() -> None:
    start = 20
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    actions[:start, 4] = np.linspace(0.0, -0.7, start)
    actions[start:, 4] = 0.5 + np.linspace(0.0, 0.3, CHUNK_STEPS - start)
    actions[:, 14:] = np.arange(20, dtype=np.float64)
    original_prefix = actions[:start].copy()
    original_hands = actions[:, 14:].copy()
    boundary_position = np.linspace(-0.2, 0.2, 14)
    boundary_velocity = np.linspace(0.001, 0.014, 14)

    smoothed, stats = smooth_rtc_arm_action_suffix(
        actions,
        start_index=start,
        boundary_arm_position=boundary_position,
        boundary_arm_velocity_per_step=boundary_velocity,
        jerk_weight=10.0,
    )

    np.testing.assert_array_equal(smoothed[:start], original_prefix)
    np.testing.assert_array_equal(smoothed[:, 14:], original_hands)
    np.testing.assert_allclose(smoothed[start, :14], boundary_position)
    np.testing.assert_allclose(
        smoothed[start + 1, :14] - smoothed[start, :14],
        boundary_velocity,
        atol=1e-12,
    )
    np.testing.assert_array_equal(smoothed[-1, :14], actions[-1, :14])
    assert stats.max_delta_rad > 0.0


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


def test_rtc_shadow_second_request_carries_committed_prefix_without_servo():
    class RTCBackend(_TrajectoryBackend):
        def __init__(self):
            super().__init__()
            self.rtc_requests = []

        def infer(self, observation, rtc_request=None):
            self.rtc_requests.append(rtc_request)
            return super().infer(observation)

    robot = MockRobotController(powered=False)
    backend = RTCBackend()
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="rtc-shadow",
            max_requests=2,
            rtc_predicted_delay_steps=0,
            rtc_execution_horizon=0,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=backend,
        telemetry=Telemetry(),
        robot=None,
        servo=None,
    )

    summary = loop.run()

    assert summary["count"] == 2
    assert backend.rtc_requests[0] is None
    assert backend.rtc_requests[1].prefix_actions_absolute.shape == (
        CHUNK_STEPS,
        ACTION_DIM,
    )
    assert robot.sent_actions == []


def test_rtc_shadow_exercises_powered_fixed_delay_and_suffix_c1(
    monkeypatch, tmp_path
):
    import g2_sort.deployment.g2_client.control_loop as control_loop_module

    monkeypatch.setattr(control_loop_module, "MODEL_HZ", 30_000.0)
    monkeypatch.setattr(
        control_loop_module, "conservative_delay_steps", lambda _age: 14
    )

    class RTCBackend(_TrajectoryBackend):
        def infer(self, observation, rtc_request=None):
            return super().infer(observation)

    robot = MockRobotController(powered=False)
    telemetry_path = tmp_path / "rtc_shadow.jsonl"
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="rtc-shadow",
            max_requests=2,
            rtc_predicted_delay_steps=20,
            rtc_execution_horizon=25,
            arm_internal_jerk_weight=10.0,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=RTCBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=None,
        servo=None,
    )

    loop.run()

    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    result = next(event for event in events if event["event"] == "rtc_shadow_result")
    chunks = [event for event in events if event["event"] == "action_chunk"]
    assert result["real_delay_steps"] == 14
    assert result["switch_delay_steps"] == 20
    assert result["response_buffered_steps"] == 6
    assert result["schedule_feasible"] is True
    assert result["arm_seam_max_rad"] == pytest.approx(0.0, abs=1e-12)
    assert result["arm_velocity_jump_max_rad_s"] == pytest.approx(0.0, abs=1e-9)
    assert result["arm_direction_reversal_count"] == 0
    assert chunks[1]["used_start_index"] == 20
    assert robot.sent_actions == []


def test_powered_rtc_keeps_sending_during_inference_and_switches(monkeypatch, tmp_path):
    import g2_sort.deployment.g2_client.control_loop as control_loop_module
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(control_loop_module, "MODEL_HZ", 300.0)
    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)

    class RTCBackend(_TrajectoryBackend):
        def __init__(self):
            super().__init__(delay=0.02)
            self.rtc_requests = []

        def infer(self, observation, rtc_request=None):
            self.rtc_requests.append(rtc_request)
            return super().infer(observation)

    robot = MockRobotController(powered=True)
    backend = RTCBackend()
    backend.robot = robot
    telemetry_path = tmp_path / "powered_rtc.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-rtc",
            max_requests=2,
            rtc_predicted_delay_steps=20,
            rtc_execution_horizon=25,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=backend,
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=servo,
    )

    loop.run()
    servo.stop()

    assert backend.calls == 2
    assert backend.rtc_requests[0] is None
    assert backend.rtc_requests[1].prefix_actions_absolute.shape == (25, ACTION_DIM)
    assert backend.sent_during_infer > 0
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    switch = next(event for event in events if event["event"] == "rtc_switch")
    assert switch["real_delay_steps"] >= 1
    assert switch["switch_delay_steps"] == 20
    assert switch["new_start_index"] == 20
    assert switch["old_switch_index"] == 45
    assert switch["response_buffered_steps"] == 20 - switch["real_delay_steps"]
    assert switch["arm_seam_max_rad"] == pytest.approx(0.0, abs=1e-12)
    assert switch["hand_seam_max_rad"] == pytest.approx(0.0, abs=1e-12)


def test_powered_rtc_process_pipeline_never_captures_in_control_process(
    monkeypatch, tmp_path
):
    import g2_sort.deployment.g2_client.control_loop as control_loop_module
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(control_loop_module, "MODEL_HZ", 300.0)
    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)

    class Pipeline:
        def __init__(self, robot):
            self.robot = robot
            self.requests = []
            self.sent_during_infer = 0

        def infer(self, rtc_request=None):
            request_id = len(self.requests)
            self.requests.append(rtc_request)
            observation_at = time.monotonic()
            before = len(self.robot.sent_actions)
            time.sleep(0.02)
            self.sent_during_infer += len(self.robot.sent_actions) - before
            actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
            actions[:, 0] = np.arange(CHUNK_STEPS) * 0.001
            return ActionChunk(
                request_id=request_id,
                observation_at=observation_at,
                request_started_at=observation_at,
                response_received_at=time.monotonic(),
                actions=actions,
            )

        def close(self):
            return None

    robot = MockRobotController(powered=True)
    pipeline = Pipeline(robot)
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-rtc",
            max_requests=2,
            rtc_predicted_delay_steps=20,
            rtc_execution_horizon=25,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=StateOnlyObservationSource(robot),
        backend=_TrajectoryBackend(),
        inference_pipeline=pipeline,
        telemetry=Telemetry(tmp_path / "isolated_rtc.jsonl"),
        robot=robot,
        servo=servo,
    )

    loop.run()
    servo.stop()

    assert len(pipeline.requests) == 2
    assert pipeline.requests[0] is None
    assert pipeline.requests[1].prefix_actions_absolute.shape == (25, ACTION_DIM)
    assert pipeline.sent_during_infer > 0


def test_powered_rtc_holds_and_exits_when_response_misses_window(monkeypatch, tmp_path):
    import g2_sort.deployment.g2_client.control_loop as control_loop_module
    import g2_sort.deployment.g2_client.servo as servo_module

    monkeypatch.setattr(control_loop_module, "MODEL_HZ", 300.0)
    monkeypatch.setattr(servo_module, "MODEL_HZ", 300.0)
    monkeypatch.setattr(control_loop_module, "conservative_delay_steps", lambda _age: 26)

    class LateRTCBackend(_TrajectoryBackend):
        def infer(self, observation, rtc_request=None):
            return super().infer(observation)

    robot = MockRobotController(powered=True)
    backend = LateRTCBackend()
    telemetry_path = tmp_path / "powered_rtc_late.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-rtc",
            max_requests=2,
            start_jump_limit_radians=1.0,
            boundary_blend_seconds=0.0,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
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
    missed = next(event for event in events if event["event"] == "rtc_schedule_missed")
    assert missed["real_delay_steps"] == 26
    assert any(
        event["event"] == "safe_hold"
        and "missed" in event.get("reason", "")
        for event in events
    )
    assert not any(event["event"] == "rtc_switch" for event in events)
    assert not servo.is_playing()


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


def test_arms_only_safety_ignores_unexecuted_hand_jump(tmp_path):
    class LargeHandJumpBackend(_TrajectoryBackend):
        def infer(self, observation):
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
    telemetry_path = tmp_path / "arms_only_hand_jump.jsonl"
    servo = ServoStreamer(robot.send_action, None, servo_hz=500.0)
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-rtc",
            max_requests=1,
            start_jump_limit_radians=0.05,
            boundary_blend_seconds=0.0,
            executed_action_dim=14,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=LargeHandJumpBackend(),
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
    assert not any(event["event"] == "start_jump_rejected" for event in events)
    action = next(event for event in events if event["event"] == "action_chunk")
    assert action["max_start_jump"] == pytest.approx(0.0)
    assert action["hand_start_jump_rad"] == pytest.approx(0.9)
    assert servo.stats()["send_count"] > 0


def test_powered_rtc_start_jumps_only_warn_at_bootstrap_and_replan(tmp_path):
    robot = MockRobotController(powered=True)
    telemetry_path = tmp_path / "rtc_hand_feedback_gap.jsonl"
    loop = ControlLoop(
        options=RuntimeOptions(
            mode="powered-rtc",
            hand_start_jump_limit_radians=0.35,
            boundary_blend_seconds=0.0,
        ),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=_TrajectoryBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=ServoStreamer(robot.send_action, None),
    )
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    actions[:, 15] = 0.6
    now = time.monotonic()
    chunk = ActionChunk(
        request_id=1,
        observation_at=now,
        request_started_at=now,
        response_received_at=now,
        actions=actions,
    )

    bootstrap = loop._prepare_for_execution(
        chunk,
        start_index=0,
        interval_count=20,
    )
    rtc_replan = loop._prepare_for_execution(
        chunk,
        start_index=16,
        interval_count=34,
        rtc_prefix_absolute=actions[:20],
        rtc_inference_delay=16,
    )

    assert bootstrap is not None
    assert rtc_replan is not None
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert not any(event["event"] == "start_jump_rejected" for event in events)
    warnings = [event for event in events if event["event"] == "start_jump_warning"]
    assert len(warnings) == 2
    warning = next(
        event
        for event in events
        if event["event"] == "rtc_hand_feedback_gap_warning"
    )
    assert warning["hand_start_jump_rad"] == pytest.approx(0.6)


def test_powered_rtc_action_dynamics_are_warning_only(tmp_path):
    robot = MockRobotController(powered=True)
    telemetry_path = tmp_path / "rtc_dynamics_warning.jsonl"
    loop = ControlLoop(
        options=RuntimeOptions(mode="powered-rtc", boundary_blend_seconds=0.0),
        observations=ObservationSource(MockCameraRig(), robot),
        backend=_TrajectoryBackend(),
        telemetry=Telemetry(telemetry_path),
        robot=robot,
        servo=ServoStreamer(robot.send_action, None),
    )
    actions = np.zeros((CHUNK_STEPS, ACTION_DIM), dtype=np.float64)
    actions[1::2, 0] = 0.3
    actions[1::2, 15] = 1.0
    now = time.monotonic()
    chunk = ActionChunk(
        request_id=1,
        observation_at=now,
        request_started_at=now,
        response_received_at=now,
        actions=actions,
    )

    prepared = loop._prepare_for_execution(
        chunk,
        start_index=0,
        interval_count=20,
    )

    assert prepared is not None
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "action_dynamics_warning" for event in events)
    assert not any(
        event["event"] == "action_dynamics_rejected" for event in events
    )


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
