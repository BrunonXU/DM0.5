"""G2运维工具的纯逻辑测试。

用途：回归通道路由、复位目标和操作者控制。
输入：mock Robot、复位配置和键盘事件；输出：pytest断言结果；方法：验证通道路由、
目标选择和安全授权，不连接真实机器人。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import time

import numpy as np
import pytest

from g2_sort.deployment.g2_client.app import (
    _acquire_pid_file,
    _release_pid_file,
)
from g2_sort.deployment.g2_client.cli import parse_args
from g2_sort.deployment.g2_client.control_loop import ControlLoop, RuntimeOptions
from g2_sort.deployment.g2_client.hand_command_store import (
    build_hand_command_record,
    load_hand_command_record,
    save_hand_command_record,
    validate_hand_command_record,
)
from g2_sort.deployment.g2_client.joint_layout import (
    POLICY_JOINT_LIMITS,
    POLICY_JOINT_NAMES,
)
from g2_sort.deployment.g2_client.keyboard import RuntimeCommandState
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown
from g2_sort.operations.hardware_tests.o10_move_ee_smoke import (
    run as run_o10_move_ee_smoke,
)
from g2_sort.operations.recovery.hand_home import (
    build_arm_recovery_trajectory,
    build_bounded_hand_trajectory,
    run as run_hand_home,
)
import g2_sort.operations.recovery.hand_home as hand_home_module
from g2_sort.operations.recovery.reset_pose import (
    build_reset_stages,
    build_reset_target,
    load_reset_config,
    part_execution_enabled,
)


class _Request:
    def __init__(self):
        self.control_period = 0.0
        self.joint_names = []
        self.joint_positions = []
        self.joint_velocities = []


class _JointStates:
    def __init__(self):
        self.group = ""
        self.target_type = ""
        self.states = []
        self.nums = 0


class _JointState:
    def __init__(self):
        self.position = 0.0


class _FakeRobot:
    def __init__(self):
        self.calls = []
        self.servo_requests = []
        self.hand_calls = []
        self.output_events = []

    def get_whole_body_status(self):
        return {
            "left_end_model": "o10_t2",
            "right_end_model": "o10_t2",
            "left_end_error": 0,
            "right_end_error": 0,
        }

    def get_motion_control_status(self):
        return SimpleNamespace(
            mode=5,
            collision_pairs_1=[],
            collision_pairs_2=[],
        )

    def get_joint_states(self):
        return {
            "states": [
                {
                    "name": name,
                    "motor_position": 0.0,
                    "position": 99.0,
                }
                for name in POLICY_JOINT_NAMES[:14]
            ]
        }

    def get_end_state(self):
        right_names = list(POLICY_JOINT_NAMES[24:])
        right_names[4] += " "
        return {
            "left_end_state": {
                "names": list(POLICY_JOINT_NAMES[14:24]),
                "end_states": [
                    {"position": 0.0, "enable": True, "status": 0, "err_code": 0}
                ] * 10,
            },
            "right_end_state": {
                "names": right_names,
                "end_states": [
                    {"position": 0.0, "enable": True, "status": 0, "err_code": 0}
                ] * 10,
            },
        }

    def joint_servo_control(self, request, enable_low_latency=False):
        self.output_events.append(
            ("arm", tuple(request.joint_positions))
        )
        self.servo_requests.append(
            SimpleNamespace(
                control_period=request.control_period,
                joint_names=tuple(request.joint_names),
                joint_positions=tuple(request.joint_positions),
                joint_velocities=tuple(request.joint_velocities),
            )
        )
        self.calls.append(
            (
                tuple(request.joint_names),
                tuple(request.joint_positions),
                enable_low_latency,
            )
        )
        return 0

    def move_ee_pos(self, request):
        self.output_events.append(
            ("hand", tuple(state.position for state in request.states))
        )
        self.hand_calls.append(request)
        return 0


def test_powered_startup_fault_reports_sdk_details_without_sending():
    from g2_sort.deployment.g2_client.contracts import RobotControlError

    robot = _FakeRobot()
    robot.get_motion_control_status = lambda: SimpleNamespace(
        mode=5, error_code=2, error_msg="test controller stopped",
        collision_pairs_1=["left_link"], collision_pairs_2=["right_link"],
    )
    with pytest.raises(RobotControlError) as caught:
        G2RobotController(
            powered=True, stabilization_seconds=0.0,
            gdk_module=SimpleNamespace(), robot=robot,
        )
    message = str(caught.value)
    assert "error_code=2" in message
    assert "test controller stopped" in message
    assert "left_link" in message and "right_link" in message
    assert not robot.output_events


def _controller(
    channel: str,
    *,
    enforce_low_latency_step_limit: bool = True,
    output_mode: str = "split",
    last_hand_command_path=None,
    hand_startup_delay_seconds: float = 0.0,
    hand_stream_hz: float = 5.0,
    hand_smoothing_profile: str = "off",
    split_hand_output_enabled: bool = True,
) -> tuple[G2RobotController, _FakeRobot]:
    robot = _FakeRobot()
    gdk = SimpleNamespace(
        Robot=lambda: robot,
        JointServoControlReq=_Request,
        JointStates=_JointStates,
        JointState=_JointState,
    )
    controller = G2RobotController(
        powered=True,
        servo_channel=channel,
        servo_output_mode=output_mode,
        stabilization_seconds=0.0,
        max_send_seconds=1.0,
        enforce_low_latency_step_limit=enforce_low_latency_step_limit,
        last_hand_command_path=last_hand_command_path,
        hand_command_run_id="test-run",
        hand_startup_delay_seconds=hand_startup_delay_seconds,
        hand_stream_hz=hand_stream_hz,
        hand_smoothing_profile=hand_smoothing_profile,
        split_hand_output_enabled=split_hand_output_enabled,
        gdk_module=gdk,
        robot=robot,
    )
    return controller, robot


def test_normal_routes_one_collision_protected_unified_34d_group():
    controller, robot = _controller("normal", output_mode="unified-34")
    controller.send_action(np.zeros(34))
    assert len(robot.calls) == 1
    assert robot.calls[0][0] == POLICY_JOINT_NAMES
    assert robot.calls[0][2] is False
    assert controller.control_stats()["request_layout"] == "unified-34"


def test_low_latency_routes_one_unprotected_unified_34d_group():
    controller, robot = _controller("low-latency", output_mode="unified-34")
    controller.send_action(np.zeros(34))
    assert len(robot.calls) == 1
    assert robot.calls[0][0] == POLICY_JOINT_NAMES
    assert robot.calls[0][2] is True
    assert controller.control_stats()["request_layout"] == "unified-34"


def test_diagnostic_unified_low_latency_allows_large_first_step_when_unbounded():
    controller, robot = _controller(
        "low-latency",
        output_mode="unified-34",
        enforce_low_latency_step_limit=False,
    )
    target = np.zeros(34)
    target[15] = 0.93
    controller.send_action(target)
    assert len(robot.calls) == 1
    assert robot.calls[0][1][15] == pytest.approx(0.93)
    assert robot.calls[0][2] is True


def test_arms_only_never_places_o10_names_in_servo_request():
    controller, robot = _controller(
        "low-latency",
        output_mode="arms-only",
        enforce_low_latency_step_limit=False,
    )
    target = np.zeros(34)
    controller.send_action(target)
    controller.close()
    assert robot.calls[0][0] == POLICY_JOINT_NAMES[:14]
    assert len(robot.calls[0][1]) == 14
    assert robot.hand_calls == []
    assert controller.control_stats()["request_layout"] == "arms-only"


@pytest.mark.parametrize("reverse_hand_order", [False, True])
def test_dual_side_17d_routes_two_dynamic_complete_servo_requests_per_tick(monkeypatch, reverse_hand_order):
    original_get_end_state = _FakeRobot.get_end_state
    def reordered_end_state(self):
        payload = original_get_end_state(self)
        if reverse_hand_order:
            for side in ("left", "right"):
                state = payload[f"{side}_end_state"]
                state["names"] = state["names"][::-1]
                state["end_states"] = state["end_states"][::-1]
        return payload
    monkeypatch.setattr(_FakeRobot, "get_end_state", reordered_end_state)
    controller, robot = _controller(
        "normal",
        output_mode="dual-side-17d",
        enforce_low_latency_step_limit=False,
    )
    # Distinct in-limit values detect even swaps between same-limit PIP axes.
    target = POLICY_JOINT_LIMITS[:, 0] + np.linspace(0.3, 0.6, 34) * np.ptp(POLICY_JOINT_LIMITS, axis=1)
    controller.send_action(target)
    controller.close()
    assert len(robot.calls) == 2
    left_names, left_positions, left_low_latency = robot.calls[0]
    right_names, right_positions, right_low_latency = robot.calls[1]
    left_hand = POLICY_JOINT_NAMES[14:24]
    right_hand = POLICY_JOINT_NAMES[24:34]
    if reverse_hand_order:
        left_hand, right_hand = left_hand[::-1], right_hand[::-1]
    assert left_names == POLICY_JOINT_NAMES[:7] + left_hand
    assert right_names == POLICY_JOINT_NAMES[7:14] + right_hand
    by_name = dict(zip(POLICY_JOINT_NAMES, target, strict=True))
    assert left_positions == pytest.approx(tuple(by_name[name] for name in left_names))
    assert right_positions == pytest.approx(tuple(by_name[name] for name in right_names))
    assert left_low_latency is False
    assert right_low_latency is False
    stats = controller.control_stats()
    assert stats["request_layout"] == "dual-side-17d"
    assert stats["servo_request_count"] == 2
    assert stats["last_servo_joint_count"] == 17
    assert stats["servo_requests_all_complete"] is True
    assert stats["hand_stream_hz"] == 100.0
    assert stats["hand_send_count"] == 1


def test_arms_only_one_shot_o10_submits_exactly_one_request():
    controller, robot = _controller("normal", output_mode="arms-only")
    target = np.zeros(20)
    controller.send_hands_once(target, source="test_one_shot")
    time.sleep(0.03)
    controller.close()
    assert len(robot.hand_calls) == 1
    request = robot.hand_calls[0]
    assert request.group == "dual_tool"
    assert request.target_type == "o10_t2"
    assert request.nums == 20
    assert [state.position for state in request.states] == pytest.approx(target)
    assert controller.control_stats()["hand_send_count"] == 1


def test_one_shot_o10_rejects_split_mode_with_background_worker():
    controller, _ = _controller("normal", output_mode="split")
    try:
        with pytest.raises(Exception, match="requires arms-only mode"):
            controller.send_hands_once(np.zeros(20))
    finally:
        controller.close()


def test_split_routes_arms_to_servo_and_hands_to_dual_tool_move_ee():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
    )
    target = np.zeros(34)
    controller.send_action(target)
    deadline = time.monotonic() + 1.0
    while not robot.hand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()
    assert robot.calls[0][0] == POLICY_JOINT_NAMES[:14]
    assert len(robot.calls[0][1]) == 14
    request = robot.servo_requests[0]
    assert request.control_period == pytest.approx(0.01)
    assert request.joint_names == POLICY_JOINT_NAMES[:14]
    assert len(request.joint_positions) == 14
    assert request.joint_velocities == ()
    assert len(robot.hand_calls) == 1
    hand_request = robot.hand_calls[0]
    assert hand_request.group == "dual_tool"
    assert hand_request.target_type == "o10_t2"
    assert hand_request.nums == 20
    assert [state.position for state in hand_request.states] == pytest.approx(
        target[14:]
    )
    stats = controller.control_stats()
    assert stats["request_layout"] == "split"
    assert stats["servo_control_period_seconds"] == pytest.approx(0.01)
    assert stats["servo_request_count"] == 1
    assert stats["servo_requests_all_complete"] is True
    assert stats["last_servo_joint_count"] == 14
    assert stats["last_servo_position_count"] == 14
    assert stats["last_servo_velocity_count"] == 0
    assert stats["hand_send_count"] == 1


def test_split_raw_hand_stream_does_not_resend_unchanged_point_target():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_stream_hz=50.0,
        hand_smoothing_profile="off",
    )
    target = np.zeros(34)
    target[14] = -0.2
    controller.send_action(target)
    deadline = time.monotonic() + 1.0
    while not robot.hand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.08)
    controller.close()

    assert len(robot.hand_calls) == 1
    stats = controller.control_stats()
    assert stats["hand_send_count"] == 1
    assert stats["hand_unchanged_skip_count"] >= 1


def test_split_raw_hand_stream_sends_next_changed_point_target():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_stream_hz=50.0,
        hand_smoothing_profile="off",
    )
    first = np.zeros(34)
    first[14] = -0.2
    controller.send_action(first)
    deadline = time.monotonic() + 1.0
    while len(robot.hand_calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    second = first.copy()
    second[14] = -0.3
    controller.send_action(second)
    deadline = time.monotonic() + 1.0
    while len(robot.hand_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()

    assert len(robot.hand_calls) == 2
    assert robot.hand_calls[0].states[0].position == pytest.approx(-0.2)
    assert robot.hand_calls[1].states[0].position == pytest.approx(-0.3)


def test_split_hand_stream_accepts_50hz_without_changing_other_modes():
    controller, _ = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_stream_hz=50.0,
        hand_smoothing_profile="v1",
    )
    try:
        stats = controller.control_stats()
        assert stats["hand_stream_hz"] == pytest.approx(50.0)
        assert stats["servo_control_period_seconds"] == pytest.approx(0.01)
        assert stats["hand_smoothing_profile"] == "v1"
    finally:
        controller.close()


def test_split_hand_v11_uses_frozen_offline_candidate() -> None:
    controller, _ = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_stream_hz=10.0,
        hand_smoothing_profile="v1_1",
    )
    try:
        tracker = controller._hand_tracker
        assert tracker.max_delta_rad == pytest.approx(0.175)
        assert tracker.max_velocity_rad_s == pytest.approx(1.75)
        assert tracker.max_acceleration_rad_s2 == pytest.approx(7.5)
        assert tracker.reversal_boundary_only is True
    finally:
        controller.close()


def test_split_shared_preview_uses_frozen_candidate_and_real_future() -> None:
    controller, _ = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_stream_hz=10.0,
        hand_smoothing_profile="shared_preview_v1",
        split_hand_output_enabled=False,
    )
    try:
        planner = controller._hand_trajectory_planner
        assert planner is not None
        assert planner.config.strategy == "preview_qp"
        assert planner.config.horizon_steps == 6
        assert planner.config.max_velocity_rad_s == pytest.approx(1.75)
        assert planner.config.max_acceleration_rad_s2 == pytest.approx(7.5)
        assert planner.config.max_jerk_rad_s3 == pytest.approx(80.0)
        assert planner.config.jerk_penalty == pytest.approx(5.0)
        assert planner.config.preview_decay == pytest.approx(0.2)
        assert planner.config.enforce_derivative_limits is False
        assert planner.config.position_lower_rad is not None
        assert planner.config.position_upper_rad is not None

        actions = np.zeros((10, 34), dtype=np.float64)
        actions[3:, 14:] = 0.6
        started_at = time.monotonic()
        controller.begin_hand_segment(
            started_at=started_at,
            reference_actions=actions,
            start_index=0,
            interval_count=9,
            anchor=np.zeros(34),
        )
        reference = controller._sample_hand_reference_horizon(
            now=started_at,
            current_target=np.zeros(20),
            segment_started_at=started_at,
            actions=actions,
            start_index=0,
            interval_count=9,
            anchor=np.zeros(20),
            blend_seconds=0.0,
            blend_profile="linear",
        )
        np.testing.assert_allclose(reference[0], 0.0)
        np.testing.assert_allclose(reference[1], 0.6)
        assert np.any(reference[1:] != reference[0])
    finally:
        controller.close()


def test_split_hand_output_can_be_deferred_until_explicit_enable():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        split_hand_output_enabled=False,
    )
    target = np.zeros(34)
    controller.send_action(target)
    time.sleep(0.1)
    assert robot.calls
    assert robot.hand_calls == []

    controller.enable_split_hand_output()
    deadline = time.monotonic() + 1.0
    while not robot.hand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()

    assert len(robot.hand_calls) == 1
    assert [state.position for state in robot.hand_calls[0].states] == pytest.approx(
        target[14:]
    )


def test_hand_startup_deadline_preserves_full_first_period():
    assert G2RobotController._align_hand_start_deadline(
        next_send=10.0,
        now=10.106,
        schedule_started=False,
    ) == pytest.approx(10.106)
    assert G2RobotController._align_hand_start_deadline(
        next_send=10.2,
        now=10.106,
        schedule_started=True,
    ) == pytest.approx(10.2)


def test_split_persists_only_latest_successful_hand_command(tmp_path):
    latest_path = tmp_path / "latest.json"
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        last_hand_command_path=latest_path,
    )
    first = np.zeros(34)
    first[14] = -0.2
    controller.send_action(first)
    deadline = time.monotonic() + 1.0
    while not latest_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert latest_path.exists()
    latest = load_hand_command_record(latest_path)
    assert latest["kind"] == "last_success"
    assert latest["command_index"] == 1
    assert latest["positions"][0] == pytest.approx(-0.2)

    second = first.copy()
    second[14] = -0.3
    controller.send_action(second)
    deadline = time.monotonic() + 1.0
    while len(robot.hand_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()
    assert load_hand_command_record(latest_path)["positions"][0] == pytest.approx(
        -0.3
    )


def test_split_startup_delay_skips_the_initial_near_zero_hand_target():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_startup_delay_seconds=0.08,
    )
    near_zero = np.zeros(34)
    controller.send_action(near_zero)
    time.sleep(0.02)
    settled = near_zero.copy()
    settled[14] = -0.30
    settled[15] = 0.93
    controller.send_action(settled)
    deadline = time.monotonic() + 1.0
    while not robot.hand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()
    assert len(robot.hand_calls) == 1
    sent = [state.position for state in robot.hand_calls[0].states]
    assert sent[0] == pytest.approx(-0.30)
    assert sent[1] == pytest.approx(0.93)


def test_split_hand_tracker_v1_blends_from_last_acknowledged_command():
    controller, robot = _controller(
        "low-latency",
        output_mode="split",
        enforce_low_latency_step_limit=False,
        hand_smoothing_profile="v1",
    )
    first = np.zeros(34)
    controller.begin_hand_segment(started_at=time.monotonic())
    controller.send_action(first)
    deadline = time.monotonic() + 1.0
    while len(robot.hand_calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    second = first.copy()
    second[14] = -0.2
    controller.begin_hand_segment(started_at=time.monotonic())
    controller.send_action(second)
    deadline = time.monotonic() + 1.0
    while len(robot.hand_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.close()

    assert len(robot.hand_calls) == 2
    sent = np.asarray(
        [state.position for state in robot.hand_calls[1].states]
    )
    assert -0.2 < sent[0] < 0.0
    np.testing.assert_allclose(sent[1:], 0.0)
    assert controller.control_stats()["hand_smoothing_profile"] == "v1"


def test_hand_command_store_first_write_is_immutable(tmp_path):
    path = tmp_path / "candidate.json"
    first = build_hand_command_record(
        np.zeros(20),
        kind="home_candidate",
        run_id="one",
        command_index=1,
    )
    second_positions = np.zeros(20)
    second_positions[0] = -0.2
    second = build_hand_command_record(
        second_positions,
        kind="home_candidate",
        run_id="two",
        command_index=1,
    )
    assert save_hand_command_record(path, first, overwrite=False)
    assert not save_hand_command_record(path, second, overwrite=False)
    assert load_hand_command_record(path)["run_id"] == "one"


def test_hand_command_store_rejects_string_approved_flag():
    payload = build_hand_command_record(
        np.zeros(20),
        kind="home_candidate",
        run_id="one",
        command_index=1,
    )
    payload["approved"] = "false"
    with pytest.raises(ValueError, match="JSON boolean"):
        validate_hand_command_record(payload)


def test_state_reader_prioritizes_official_motor_position():
    state = StrictRobotStateReader(_FakeRobot()).read()
    assert np.all(state[:14] == 0.0)


def test_cli_defaults_split_normal_and_supports_deprecated_channel_alias():
    assert parse_args([]).servo_channel == "normal"
    assert parse_args([]).servo_output_mode == "split"
    assert parse_args([]).hand_smoothing_profile == "off"
    assert (
        parse_args(["--servo-channel", "low-latency"]).servo_channel
        == "low-latency"
    )
    hand_v1 = parse_args(
        [
            "--mode",
            "powered-sync",
            "--hand-smoothing-profile",
            "v1",
        ]
    )
    assert hand_v1.hand_boundary_blend_seconds == pytest.approx(0.4)
    assert hand_v1.hand_max_velocity_rad_s == pytest.approx(2.0)
    assert hand_v1.hand_max_acceleration_rad_s2 == pytest.approx(10.0)
    assert hand_v1.hand_max_delta_radians == pytest.approx(0.4)
    assert hand_v1.hand_reversal_error_radians == pytest.approx(0.15)
    hand_v11 = parse_args(
        [
            "--mode",
            "powered-sync",
            "--hand-smoothing-profile",
            "v1_1",
        ]
    )
    assert hand_v11.hand_max_velocity_rad_s == pytest.approx(1.75)
    assert hand_v11.hand_max_acceleration_rad_s2 == pytest.approx(7.5)
    assert hand_v11.hand_max_delta_radians == pytest.approx(0.175)
    with pytest.raises(SystemExit):
        parse_args(["--hand-smoothing-profile", "v1"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "powered-async",
                "--hand-smoothing-profile",
                "v1",
            ]
        )
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "powered-async",
                "--hand-smoothing-profile",
                "v1_1",
            ]
        )
    assert (
        parse_args(["--enable-low-latency-servo"]).servo_channel
        == "low-latency"
    )
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--servo-channel",
                "normal",
                "--enable-low-latency-servo",
            ]
        )
    split_profile = parse_args(
        ["--mode", "powered-sync", "--split-control-profile"]
    )
    assert split_profile.servo_channel == "low-latency"
    assert split_profile.split_control_profile is True
    assert split_profile.servo_output_mode == "split"
    with pytest.raises(SystemExit):
        parse_args(["--split-control-profile"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "powered-sync",
                "--split-control-profile",
                "--servo-output-mode",
                "unified-34",
            ]
        )
    split_arms_only = parse_args(
        [
            "--mode",
            "powered-sync",
            "--split-control-profile",
            "--servo-output-mode",
            "arms-only",
        ]
    )
    assert split_arms_only.servo_channel == "low-latency"
    assert split_arms_only.servo_output_mode == "arms-only"
    dual_side = parse_args(
        [
            "--mode",
            "powered-sync",
            "--split-control-profile",
            "--servo-output-mode",
            "dual-side-17d",
            "--servo-channel",
            "normal",
        ]
    )
    assert dual_side.servo_channel == "normal"
    assert dual_side.servo_output_mode == "dual-side-17d"
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--mode",
                "powered-sync",
                "--split-control-profile",
                "--servo-output-mode",
                "dual-side-17d",
                "--servo-channel",
                "low-latency",
            ]
        )
    assert (
        parse_args(
            ["--mode", "powered-sync", "--servo-output-mode", "arms-only"]
        ).servo_output_mode
        == "arms-only"
    )


def test_runtime_command_state_pause_resume_and_stop():
    state = RuntimeCommandState()
    state.pause()
    assert state.pause_event.is_set()
    state.resume()
    assert not state.pause_event.is_set()
    state.pause()
    state.stop()
    assert state.stop_event.is_set()
    assert not state.pause_event.is_set()


def test_pid_file_is_exclusive_and_owned_cleanup(tmp_path):
    path = tmp_path / "runtime.pid"
    _acquire_pid_file(path)
    assert path.exists()
    with pytest.raises(RuntimeError, match="already active"):
        _acquire_pid_file(path)
    _release_pid_file(path)
    assert not path.exists()


def test_reset_target_changes_only_selected_part_and_arms_stay_locked():
    config = load_reset_config()
    current = np.zeros(34)
    target = build_reset_target(current, part="left-hand", config=config)
    assert np.all(target[:14] == current[:14])
    assert np.any(target[14:24] != current[14:24])
    assert np.all(target[24:] == current[24:])
    assert not part_execution_enabled(config, "left-hand")
    assert not part_execution_enabled(config, "right-hand")
    assert not part_execution_enabled(config, "hands")
    assert not part_execution_enabled(config, "arms")
    assert not part_execution_enabled(config, "all")


def test_o10_move_ee_execute_is_locked_before_gdk_import(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        run_o10_move_ee_smoke(
            [
                "--execute",
                "--side",
                "left",
                "--report-path",
                str(tmp_path / "report.json"),
            ]
        )
    assert exc_info.value.code == 2


def test_hand_home_rejects_unapproved_preview_and_execute_before_gdk(tmp_path):
    candidate_path = tmp_path / "candidate.json"
    positions = np.zeros(20)
    positions[0] = -0.2
    payload = build_hand_command_record(
        positions,
        kind="home_candidate",
        run_id="preview",
        command_index=1,
    )
    save_hand_command_record(candidate_path, payload, overwrite=False)
    with pytest.raises(SystemExit) as preview_exc:
        run_hand_home(["--home-json", str(candidate_path)])
    assert preview_exc.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        run_hand_home(
            [
                "--execute",
                "--approved-home-json",
                str(candidate_path),
            ]
        )
    assert exc_info.value.code == 2


def test_hand_home_trajectory_is_bounded_and_reaches_target():
    start = np.zeros(20)
    target = np.linspace(-0.3, 0.3, 20)
    trajectory = build_bounded_hand_trajectory(
        start,
        target,
        max_step_radians=0.03,
    )
    assert np.allclose(trajectory[0], start)
    assert np.allclose(trajectory[-1], target)
    assert np.max(np.abs(np.diff(trajectory, axis=0))) <= 0.03 + 1e-12


def test_hand_home_runtime_caps_large_recovery_to_eight_steps():
    start = np.zeros(20)
    target = np.zeros(20)
    target[1] = 0.93
    trajectory = build_bounded_hand_trajectory(
        start,
        target,
        max_step_radians=0.03,
        max_intervals=8,
    )
    assert len(trajectory) == 9
    assert np.allclose(trajectory[0], start)
    assert np.allclose(trajectory[-1], target)


def test_arm_recovery_reaches_h5_target_while_holding_hand_home():
    start = np.zeros(14)
    target = np.linspace(-1.0, 1.0, 14)
    hands = np.linspace(-0.3, 0.3, 20)
    trajectory = build_arm_recovery_trajectory(
        start,
        target,
        hands,
        duration_seconds=2.0,
    )
    assert trajectory.shape == (61, 34)
    assert np.allclose(trajectory[0, :14], start)
    assert np.allclose(trajectory[-1, :14], target)
    assert np.allclose(trajectory[:, 14:], hands[None, :])


def test_approved_hand_home_matches_the_confirmed_command_nine():
    path = Path(__file__).resolve().parents[1] / "g2_sort/config/hand_home_approved.json"
    home = load_hand_command_record(path)
    assert home["approved"] is True
    assert home["command_index"] == 9
    h5 = load_reset_config()["action_median"][14:]
    assert np.max(np.abs(np.asarray(home["positions"]) - np.asarray(h5))) < 0.008


@pytest.mark.parametrize("same_target", [False, True])
def test_hand_home_execute_reuses_split_arm_hold_and_hand_worker(
    tmp_path,
    monkeypatch,
    same_target,
):
    latest_path = tmp_path / "latest.json"
    home_path = tmp_path / "home.json"
    start = np.zeros(20)
    start[0] = -0.20
    home_positions = start.copy()
    home_positions[0] = -0.20 if same_target else -0.23
    latest = build_hand_command_record(
        start,
        kind="last_success",
        run_id="powered",
        command_index=4,
    )
    home = build_hand_command_record(
        home_positions,
        kind="home_candidate",
        run_id="approved",
        command_index=9,
    )
    home["approved"] = True
    save_hand_command_record(latest_path, latest, overwrite=False)
    save_hand_command_record(home_path, home, overwrite=False)

    robot = _FakeRobot()
    released = []
    fake_gdk = SimpleNamespace(
        GDKRes=SimpleNamespace(kSuccess=0),
        gdk_init=lambda: 0,
        gdk_release=lambda: released.append(True),
        Robot=lambda: robot,
        JointServoControlReq=_Request,
        JointStates=_JointStates,
        JointState=_JointState,
    )
    monkeypatch.setitem(sys.modules, "agibot_gdk", fake_gdk)
    monkeypatch.setattr(
        hand_home_module,
        "confirm_enter_and_countdown",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        hand_home_module,
        "_h5_arm_median",
        lambda: np.full(14, 0.1),
    )
    monkeypatch.setattr(
        hand_home_module,
        "build_arm_recovery_trajectory",
        lambda start, target, hands, **kwargs: np.vstack(
            (np.concatenate((start, hands)), np.concatenate((target, hands)))
        ),
    )

    assert run_hand_home(
        [
            "--execute",
            "--approved-home-json",
            str(home_path),
            "--last-command-json",
            str(latest_path),
            "--gdk-stabilization-seconds",
            "0",
        ]
    ) == 0
    assert released == [True]
    assert robot.calls
    assert all(len(call[0]) == 14 for call in robot.calls)
    assert all(call[2] is True for call in robot.calls)
    assert len(robot.hand_calls) >= (1 if same_target else 2)
    first_hand_event = next(
        index for index, event in enumerate(robot.output_events) if event[0] == "hand"
    )
    preceding_arm_targets = [
        np.asarray(event[1])
        for event in robot.output_events[:first_hand_event]
        if event[0] == "arm"
    ]
    assert preceding_arm_targets
    assert any(np.allclose(values, 0.1) for values in preceding_arm_targets)
    final = load_hand_command_record(latest_path)
    assert final["source"] == "hand_home_recovery_successful_move_ee_pos"
    assert final["positions"] == pytest.approx(home_positions)


def test_all_reset_is_staged_hands_then_arms():
    config = load_reset_config()
    current = np.zeros(34)
    target = build_reset_target(current, part="all", config=config)
    stages = build_reset_stages(
        current,
        target,
        part="all",
        duration_seconds=2.0,
    )
    assert [name for name, _ in stages] == ["hands", "arms"]
    hand_end = stages[0][1][-1]
    assert np.allclose(hand_end[:14], current[:14])
    assert np.allclose(hand_end[14:], target[14:])
    assert np.allclose(stages[1][1][-1], target)


def test_dynamic_gate_accepts_smooth_motion_and_rejects_hand_jump():
    loop = ControlLoop(
        options=RuntimeOptions(mode="shadow"),
        observations=object(),
        backend=object(),
        telemetry=object(),
    )
    smooth = np.zeros((7, 34))
    smooth[:, 0] = np.linspace(0.0, 0.1, len(smooth))
    assert loop._dynamic_metrics(smooth)[-1] is None

    jump = np.zeros((7, 34))
    jump[1:, 14] = 1.0
    metrics = loop._dynamic_metrics(jump)
    assert metrics[1] == POLICY_JOINT_NAMES[14]
    assert "hand velocity" in (metrics[-1] or "")


def test_operator_confirmation_accepts_ok_case_insensitively_and_counts_down():
    sleeps = []
    confirm_ok_and_countdown(
        "test",
        supplied=" OK ",
        seconds=5,
        sleep_fn=sleeps.append,
    )
    assert sleeps == [1.0] * 5


def test_operator_confirmation_rejects_everything_except_ok():
    with pytest.raises(PermissionError, match="未输入ok"):
        confirm_ok_and_countdown(
            "test",
            supplied="yes",
            sleep_fn=lambda _seconds: None,
        )


@pytest.mark.parametrize('warning_only', [False, True])
def test_unified_send_overrun_records_or_stops(monkeypatch, warning_only):
    from g2_sort.deployment.g2_client.contracts import RobotControlError
    controller, robot = _controller('normal', output_mode='unified-34')
    events = []
    controller._max_send_seconds = .2
    controller._timing_warning_callback = (lambda *args: events.append(args)) if warning_only else None
    clock = [0.0]
    original = robot.joint_servo_control
    def slow_send(*args, **kwargs):
        clock[0] += .24
        return original(*args, **kwargs)
    monkeypatch.setattr(robot, 'joint_servo_control', slow_send)
    monkeypatch.setattr('g2_sort.deployment.g2_client.robot_control.time.monotonic', lambda: clock[0])
    target = np.mean(POLICY_JOINT_LIMITS, axis=1)
    if warning_only:
        assert controller.send_action(target)
        assert controller.send_action(target)
        assert len(events) == 2
        assert events[0][0] == 'servo_send_overrun'
        assert events[0][1] == pytest.approx(.24)
        assert controller.control_stats()['hand_send_count'] == 2
        assert controller.control_stats()['hand_stream_hz'] == 100
        monkeypatch.setattr(robot, 'joint_servo_control', lambda *a, **kw: 1)
        with pytest.raises(RobotControlError):
            controller.send_action(target)
    else:
        with pytest.raises(RobotControlError, match='watchdog'):
            controller.send_action(target)
    controller.close()


@pytest.mark.parametrize('warning_only', [False, True])
def test_tick_overrun_warning_continues_without_catchup(warning_only):
    from g2_sort.deployment.g2_client.servo import ServoStreamer
    clock = [0.0]
    calls, events = [], []
    def sender(target):
        calls.append(clock[0])
        clock[0] += .24
        if len(calls) == 2:
            servo._stop.set()
        return True
    servo = ServoStreamer(sender, np.zeros(34), max_tick_lateness_seconds=.2,
        clock=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0]+dt),
        timing_warning_callback=(lambda *a: events.append(a)) if warning_only else None)
    servo._run()
    assert len(calls) == (2 if warning_only else 1)
    assert servo.has_fault() is (not warning_only)
    if warning_only:
        assert calls[1]-calls[0] >= .25
        assert events[0][0] == 'servo_tick_overrun'


def test_home_enter_confirmation_requires_empty_input():
    from g2_sort.operations.operator_confirmation import confirm_enter_and_countdown
    sleeps = []
    confirm_enter_and_countdown('offline', input_fn=lambda _: '', sleep_fn=sleeps.append)
    assert sleeps == [1.] * 5
    for answer in ('ok', 'yes', ' '):
        with pytest.raises(PermissionError):
            confirm_enter_and_countdown('offline', input_fn=lambda _: answer,
                                        sleep_fn=lambda _: None)
