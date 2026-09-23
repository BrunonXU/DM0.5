from __future__ import annotations

import json

import numpy as np

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    CausalHandTrajectoryPlanner,
    CausalHandCommandTracker,
    HandTrajectoryConfig,
)
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_LIMITS
from g2_sort.validation.evaluate_hand_v11_sweep import (
    Candidate,
    load_trace,
    metrics,
    replay,
)


def test_reversal_boundary_only_does_not_hold_internal_reversal() -> None:
    common = dict(
        enabled=True,
        boundary_blend_seconds=0.15,
        max_velocity_rad_s=2.0,
        max_acceleration_rad_s2=100.0,
        max_delta_rad=0.4,
        reversal_error_rad=0.05,
        nominal_hz=10.0,
    )
    all_segment = CausalHandCommandTracker(
        **common, reversal_boundary_only=False
    )
    boundary_only = CausalHandCommandTracker(
        **common, reversal_boundary_only=True
    )
    for tracker in (all_segment, boundary_only):
        tracker.reset(np.zeros(20))
        tracker.begin_segment(started_at=0.0)
        tracker.step(np.ones(20), now=0.1)
        tracker.step(np.ones(20), now=0.2)

    held = all_segment.step(-np.ones(20), now=0.3)
    followed = boundary_only.step(-np.ones(20), now=0.3)

    assert held.direction_reversal_holds == 20
    assert followed.direction_reversal_holds == 0
    assert np.all(followed.command < held.command)


def test_v11_replay_carries_simulated_state_across_chunks(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    rows = []
    previous = np.zeros(20)
    for request_id in (0, 1):
        raw = np.asarray(
            [
                [float(request_id)] * 20,
                [float(request_id) + 0.5] * 20,
                [float(request_id) + 1.0] * 20,
            ]
        )
        recorded = np.full((3, 20), 0.1 * (request_id + 1))
        actions = np.zeros((50, 34))
        actions[:, 14:] = raw[-1]
        rows.append(
            {
                "event": "action_chunk",
                "request_id": request_id,
                "wall_time": float(request_id),
                "used_start_index": 0,
                "used_intervals": 20,
                "actions": actions.tolist(),
            }
        )
        rows.append(
            {
                "event": "dispatch_summary",
                "request_id": request_id,
                "hand_previous_command_rad": previous.tolist(),
                "hand_raw_targets_rad": raw.tolist(),
                "hand_commands_rad": recorded.tolist(),
                "hand_command_time_offsets_seconds": [0.1, 0.2, 0.3],
            }
        )
        previous = recorded[-1]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    chunks = load_trace(path)
    commands, anchors, decisions = replay(
        chunks,
        Candidate("test", 0.05, 0.5, 5.0, True),
    )

    assert decisions.discontinuity_resets == 0
    assert decisions.reconstructed_hold_commands > 0
    assert anchors[1][0] > chunks[1].previous_command[0]
    result = metrics(chunks, commands, anchors=anchors)
    assert result["step_max_rad"] <= 0.05 + 1e-12
    assert result["tracking_error_p95_rad"] > 0
    assert result["halfway_events"] > 0


def test_shared_hand_planner_enforces_velocity_acceleration_and_jerk() -> None:
    config = HandTrajectoryConfig(
        strategy="preview_qp",
        nominal_hz=10.0,
        horizon_steps=6,
        max_velocity_rad_s=1.5,
        max_acceleration_rad_s2=6.0,
        max_jerk_rad_s3=30.0,
    )
    planner = CausalHandTrajectoryPlanner(config)
    planner.reset(np.zeros(20))
    reference = np.ones((6, 20))

    previous_position = np.zeros(20)
    previous_velocity = np.zeros(20)
    previous_acceleration = np.zeros(20)
    for _ in range(8):
        decision = planner.step(reference)
        dt = 0.1
        velocity = (decision.command - previous_position) / dt
        acceleration = (velocity - previous_velocity) / dt
        jerk = (acceleration - previous_acceleration) / dt
        assert np.max(np.abs(velocity)) <= 1.5 + 1e-6
        assert np.max(np.abs(acceleration)) <= 6.0 + 1e-6
        assert np.max(np.abs(jerk)) <= 30.0 + 1e-5
        previous_position = decision.command
        previous_velocity = velocity
        previous_acceleration = acceleration


def test_preview_and_point_to_point_are_two_inputs_to_same_stateful_planner() -> None:
    common = dict(
        nominal_hz=10.0,
        horizon_steps=6,
        max_velocity_rad_s=2.0,
        max_acceleration_rad_s2=10.0,
        max_jerk_rad_s3=60.0,
    )
    point = CausalHandTrajectoryPlanner(
        HandTrajectoryConfig(strategy="jerk_limited_p2p", **common)
    )
    preview = CausalHandTrajectoryPlanner(
        HandTrajectoryConfig(strategy="preview_qp", **common)
    )
    point.reset(np.zeros(20))
    preview.reset(np.zeros(20))
    future = np.zeros((6, 20))
    future[1:] = 1.0

    point_decision = point.step(future)
    preview_decision = preview.step(future)

    np.testing.assert_allclose(point_decision.command, 0.0, atol=1e-8)
    assert np.all(preview_decision.command > point_decision.command)
    assert np.all(preview_decision.command < 1.0)


def test_shared_hand_planner_does_not_reset_at_chunk_boundaries() -> None:
    planner = CausalHandTrajectoryPlanner(
        HandTrajectoryConfig(
            strategy="preview_qp",
            nominal_hz=10.0,
            horizon_steps=6,
            max_velocity_rad_s=1.75,
            max_acceleration_rad_s2=7.5,
            max_jerk_rad_s3=50.0,
        )
    )
    planner.reset(np.zeros(20))
    first = planner.step(np.ones((6, 20)))
    second = planner.step(np.ones((6, 20)))

    assert np.all(second.command > first.command)
    assert np.all(second.velocity >= first.velocity - 1e-8)
    assert np.max(np.abs(second.jerk)) <= 50.0 + 1e-5


def test_soft_preview_does_not_clip_derivatives_but_keeps_physical_positions():
    lower = tuple(POLICY_JOINT_LIMITS[14:, 0])
    upper = tuple(POLICY_JOINT_LIMITS[14:, 1])
    planner = CausalHandTrajectoryPlanner(
        HandTrajectoryConfig(
            strategy="preview_qp",
            nominal_hz=10.0,
            horizon_steps=6,
            max_velocity_rad_s=0.05,
            max_acceleration_rad_s2=0.1,
            max_jerk_rad_s3=0.2,
            acceleration_penalty=0.0,
            jerk_penalty=0.001,
            enforce_derivative_limits=False,
            position_lower_rad=lower,
            position_upper_rad=upper,
        )
    )
    initial = np.clip(np.zeros(20), lower, upper)
    planner.reset(initial)
    reference = np.repeat(np.asarray(upper)[None, :], 6, axis=0)

    decision = planner.step(reference)

    assert np.max(np.abs(decision.velocity)) > 0.05
    assert np.all(decision.command >= np.asarray(lower) - 1e-12)
    assert np.all(decision.command <= np.asarray(upper) + 1e-12)
    assert decision.velocity_limit_hits > 0


def test_position_constraints_prevent_tiny_gdk_limit_overshoot():
    lower = tuple(POLICY_JOINT_LIMITS[14:, 0])
    upper = tuple(POLICY_JOINT_LIMITS[14:, 1])
    planner = CausalHandTrajectoryPlanner(
        HandTrajectoryConfig(
            strategy="preview_qp",
            enforce_derivative_limits=False,
            position_lower_rad=lower,
            position_upper_rad=upper,
        )
    )
    initial = np.clip(np.zeros(20), lower, upper)
    planner.reset(initial)
    reference = np.repeat((np.asarray(lower) - 0.01)[None, :], 6, axis=0)

    for _ in range(12):
        decision = planner.step(reference)
        assert np.all(decision.command >= np.asarray(lower))
        assert np.all(decision.command <= np.asarray(upper))
