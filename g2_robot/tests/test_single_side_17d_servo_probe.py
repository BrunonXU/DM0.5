from __future__ import annotations

import numpy as np
import pytest

from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.operations.hardware_tests.single_side_17d_servo_probe import (
    build_dynamic_side_target,
    build_motion_targets,
    build_servo_request,
    build_right_index_candidate,
    build_right_finger_candidate,
)


def _body_payload() -> dict[str, object]:
    return {
        "states": [
            {"name": name, "motor_position": value}
            for value, name in enumerate(
                LEFT_ARM_JOINT_NAMES + RIGHT_ARM_JOINT_NAMES, start=1
            )
        ]
    }


def _end_payload(names: tuple[str, ...], side: str) -> dict[str, object]:
    return {
        f"{side}_end_state": {
            "names": list(names),
            "end_states": [
                {
                    "enable": True,
                    "status": 0,
                    "err_code": 0,
                    "position": 0.0,
                }
                for _ in names
            ],
        }
    }


def test_dynamic_right_end_names_preserve_gdk_whitespace(monkeypatch):
    names = list(RIGHT_HAND_JOINT_NAMES)
    names[4] += " "
    monkeypatch.setattr(
        "g2_sort.operations.hardware_tests.single_side_17d_servo_probe.POLICY_JOINT_LIMITS",
        np.tile(np.asarray([[-20.0, 20.0]]), (34, 1)),
    )
    target_names, positions = build_dynamic_side_target(
        _body_payload(), _end_payload(tuple(names), "right"), "right"
    )
    assert target_names[:7] == RIGHT_ARM_JOINT_NAMES
    assert target_names[11] == RIGHT_HAND_JOINT_NAMES[4] + " "
    assert positions.shape == (17,)


def test_dynamic_side_target_rejects_unexpected_end_name(monkeypatch):
    names = list(LEFT_HAND_JOINT_NAMES)
    names[-1] = "unexpected_joint"
    monkeypatch.setattr(
        "g2_sort.operations.hardware_tests.single_side_17d_servo_probe.POLICY_JOINT_LIMITS",
        np.tile(np.asarray([[-20.0, 20.0]]), (34, 1)),
    )
    with pytest.raises(ValueError, match="do not match"):
        build_dynamic_side_target(
            _body_payload(), _end_payload(tuple(names), "left"), "left"
        )


def test_servo_request_is_complete_17d():
    class Request:
        pass

    class GDK:
        JointServoControlReq = Request

    names = tuple(f"joint_{index}" for index in range(17))
    request = build_servo_request(GDK, names, np.zeros(17))
    assert request.control_period == 0.01
    assert request.joint_names == list(names)
    assert request.joint_positions == [0.0] * 17
    assert request.joint_velocities == []


def test_left_index_micro_motion_is_minimum_jerk_out_and_back():
    baseline = np.zeros(17)
    targets = build_motion_targets("left-index-pip-minjerk", baseline)
    assert targets.shape == (201, 17)
    np.testing.assert_array_equal(targets[0], baseline)
    np.testing.assert_array_equal(targets[-1], baseline)
    assert targets[100, 11] == pytest.approx(0.05)
    assert np.count_nonzero(np.max(np.abs(targets), axis=0)) == 1
    np.testing.assert_allclose(targets[:, 11], targets[::-1, 11])


def test_left_index_visible_motion_has_peak_and_baseline_holds():
    baseline = np.zeros(17)
    targets = build_motion_targets("left-index-pip-visible", baseline)
    assert targets.shape == (401, 17)
    np.testing.assert_array_equal(targets[0], baseline)
    np.testing.assert_array_equal(targets[-1], baseline)
    np.testing.assert_allclose(targets[100:201, 11], 0.20)
    np.testing.assert_allclose(targets[300:, 11], 0.0)
    assert np.count_nonzero(np.max(np.abs(targets), axis=0)) == 1


@pytest.mark.parametrize("finger,joint", [
    ("index", "idx77_hand_r_index_pip_joint"),
    ("middle", "idx79_hand_r_middle_pip_joint"),
    ("ring", "idx82_hand_r_ring_pip_joint"),
    ("pinky", "idx85_hand_r_pinky_pip_joint"),
])
def test_right_candidate_preserves_value_pairing_with_reordered_feedback(finger, joint):
    from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_LIMITS
    canonical = RIGHT_ARM_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
    limits = POLICY_JOINT_LIMITS[list(range(7, 14)) + list(range(24, 34))]
    baseline = limits.mean(axis=1)
    order = list(range(7)) + list(range(16, 6, -1))
    names = tuple(canonical[i] + (" " if i == 11 else "") for i in order)
    values = baseline[order]
    sent, targets, index = build_right_finger_candidate(names, values, finger)
    assert sent[index] == joint
    np.testing.assert_array_equal(targets[0], values)
    np.testing.assert_array_equal(targets[-1], values)
    assert targets[150, index] == pytest.approx(values[index] + 0.20)
    unchanged = [i for i in range(17) if i != index]
    np.testing.assert_array_equal(targets[:, unchanged], np.tile(values[unchanged], (401, 1)))
    assert sent == tuple(canonical[i] for i in order)


def test_right_candidate_rejects_zero_feedback_unknown_name_and_limit_overrun():
    names = RIGHT_ARM_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
    with pytest.raises(ValueError, match="all-zero"):
        build_right_index_candidate(names, np.zeros(17))
    with pytest.raises(ValueError, match="known 17D"):
        build_right_index_candidate(names[:-1] + ("unknown",), np.zeros(17))
    values = np.zeros(17)
    values[11] = 1.4
    with pytest.raises(ValueError, match="limits"):
        build_right_index_candidate(names, values)
