"""Unit tests for H-33 planning_joint_envelope_deg tightening."""
from pathlib import Path
import numpy as np
import pytest

from f1_harness.contracts import HarnessError, MotionParameters, Observation, SkillIntent, Task
from f1_harness.joint_motion import SEARCHABLE_FAILURES
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.skill_services import (
    CANDIDATE_FAILURES,
    PlanService,
    validate_planning_joint_envelope,
)


def _identity_frame_chain():
    eye = np.eye(4).ravel().tolist()
    return {
        "accepted": True,
        "synthetic": False,
        "evidence_ids": ["ev-h33"],
        "holdout_hash": "h33-test",
        "native_frame": "world",
        "ik_world_frame": "world",
        "native_eef_frame": "arm_R_Link7",
        "ik_eef_frame": "arm_R_Link7",
        "T_ik_world_native": eye,
        "T_ik_eef_native_eef": eye,
    }


def _make_fixture(**overrides):
    fixture = {
        "native_frame": "world",
        "frame_chain": _identity_frame_chain(),
        "joint_tolerance_deg": 1.0,
        "settle_ms": 100,
        "gripper_tolerance": 2.0,
        "planning_margin_m": 0.01,
        "approach_clearance_m": 0.05,
        "attached_radius_m": 0.05,
        "transfer_clearance_m": 0.12,
        "insertion_depth_m": 0.03,
        "grasp_opening": 20.0,
        "release_opening": 90.0,
        "object_dimensions_m": [0.04, 0.04, 0.06],
        "hole_inner_dimensions_m": [0.05, 0.05],
        "object_grasp_eef": np.eye(4).ravel().tolist(),
        "transfer_policy": "registered_transport",
        "joint_space_transport": True,
    }
    fixture.update(overrides)
    return fixture


def _make_task():
    return Task(
        task_id="test-h33",
        object_id="square-1",
        hole_id="square-hole-1",
        goal="placed",
        admission_profile="development_coarse",
        calibration_id="cal-1",
        allowed_skills=("approach_grasp", "grasp", "verify_grasp", "transfer_above_hole", "align", "insert", "release", "retreat"),
        local_corrections=2,
        regrasp_limit=1,
    )


def _make_intent():
    return SkillIntent(
        request_id="req-h33",
        task_id="test-h33",
        task_hash="th-1",
        observation_id="obs-1",
        session_id="sess-1",
        owner_instance_id="owner-1",
        epoch=1,
        scene_version="1",
        calibration_id="cal-1",
        skill="transfer_above_hole",
        parameters=MotionParameters(),
        budget_ms=5000,
    )


def _row_with_r_j6(val_deg):
    row = [0.0] * 16
    row[12] = float(val_deg)
    return row


class _StubNumerics:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "solve_path":
            if not self.responses:
                raise AssertionError("unexpected extra solve_path call")
            return self.responses.pop(0)
        raise AssertionError(f"unexpected method {method}")


class _DummyScene:
    attached_radius_m = 0.05
    uncertainty_m = 0.01
    object_planning_margin_m = 0.01
    tool_local_boxes = None
    eef_tip_reference = None
    object_local_box = None
    maximum_approach_translation_m = 0.03
    local_collision_evidence = None
    grasp_candidate_descriptor = None
    grasp_candidate_variant = None
    release_opening = 90.0
    aperture_measured = False
    transfer_policy = "registered_transport"
    home_right_deg = [0.0] * 7


def _dummy_proposal():
    return {
        "kind": "motion",
        "skill": "transfer_above_hole",
        "stages": [
            {
                "name": "transport_above_bucket",
                "route_family": "registered_transport",
                "goals": [np.eye(4).ravel().tolist()],
                "gripper": 20.0,
                "allow_target_contact": False,
            }
        ],
    }


def test_reason_code_registered_in_candidate_and_searchable_sets():
    assert "planning_joint_envelope_exceeded" in CANDIDATE_FAILURES
    assert "planning_joint_envelope_exceeded" in SEARCHABLE_FAILURES


def test_envelope_validation_at_init_and_site_limits():
    site_limits = [[-178.0, 178.0], [-94.0, 120.0], [-178.0, 178.0], [-145.0, 60.0],
                   [-178.0, 178.0], [-60.0, 60.0], [-90.0, 90.0]] * 2
    parsed = validate_planning_joint_envelope({"arm_r_j6": [-60.0, 50.0]}, limits=site_limits)
    assert parsed == {"arm_r_j6": (12, -60.0, 50.0)}

    # Unknown joint rejected
    with pytest.raises(ValueError, match="invalid_planning_joint_envelope_joint"):
        PlanService(_make_task(), _make_fixture(planning_joint_envelope_deg={"head_j1": [-10.0, 10.0]}), Path("/tmp/no.sock"))
    # Reversed / equal bounds rejected
    with pytest.raises(ValueError, match="invalid_planning_joint_envelope_bounds"):
        PlanService(_make_task(), _make_fixture(planning_joint_envelope_deg={"arm_r_j6": [50.0, 50.0]}), Path("/tmp/no.sock"))
    # Loosening beyond site limits rejected at freeze/site validation
    with pytest.raises(ValueError, match="planning_joint_envelope_exceeds_site_limits"):
        validate_planning_joint_envelope({"arm_r_j6": [-60.0, 60.5]}, limits=site_limits)
    with pytest.raises(ValueError, match="planning_joint_envelope_exceeds_site_limits"):
        validate_planning_joint_envelope({"arm_r_j6": [-61.0, 50.0]}, limits=site_limits)


def test_target_50_1_rejected_49_9_accepted_and_absent_key_unchecked(monkeypatch):
    import f1_harness.skill_services as ss
    monkeypatch.setattr(ss, "propose", lambda *a, **kw: _dummy_proposal())
    monkeypatch.setattr(ss, "stage_obstacles", lambda *a, **kw: [])
    monkeypatch.setattr(ss, "tool_margin", lambda *a, **kw: 0.01)

    # 1) 50.1 deg rejected with structured diagnostic
    svc = PlanService(_make_task(), _make_fixture(planning_joint_envelope_deg={"arm_r_j6": [-60.0, 50.0]}), Path("/tmp/no.sock"))
    svc.numerics = _StubNumerics([[_row_with_r_j6(45.0), _row_with_r_j6(50.1)]])
    with pytest.raises(HarnessError) as exc_info:
        svc._solve_route(_make_intent(), _DummyScene(), [0.0] * 16, [0.0] * 22, 0)
    detail = numerical_failure_detail(exc_info.value)
    assert detail["code"] == "planning_joint_envelope_exceeded"
    assert detail["metrics"]["joint"] == "arm_r_j6"
    assert detail["metrics"]["joint_index"] == 12
    assert detail["metrics"]["waypoint_index"] == 1
    assert detail["metrics"]["target_deg"] == pytest.approx(50.1)
    assert detail["metrics"]["envelope_deg"] == [-60.0, 50.0]

    # 2) 49.9 deg passes
    svc.numerics = _StubNumerics([[_row_with_r_j6(49.9)]])
    res = svc._solve_route(_make_intent(), _DummyScene(), [0.0] * 16, [0.0] * 22, 0)
    assert res["targets"][0][12] == pytest.approx(49.9)

    # 3) Absent planning_joint_envelope_deg does not check (59.87 deg passes)
    svc_open = PlanService(_make_task(), _make_fixture(), Path("/tmp/no.sock"))
    svc_open.numerics = _StubNumerics([[_row_with_r_j6(59.87)]])
    res_open = svc_open._solve_route(_make_intent(), _DummyScene(), [0.0] * 16, [0.0] * 22, 0)
    assert res_open["targets"][0][12] == pytest.approx(59.87)


def test_joint_space_search_output_also_checked(monkeypatch):
    import f1_harness.skill_services as ss
    import f1_harness.transport as tr
    monkeypatch.setattr(tr, "joint_transport_goal", lambda *a, **kw: {
        "kind": "motion",
        "skill": "transfer_above_hole",
        "stages": [
            {
                "name": "transport_above_bucket",
                "route_family": "joint_space_transport",
                "goals": [np.eye(4).ravel().tolist()],
                "gripper": 20.0,
                "allow_target_contact": False,
            }
        ],
    })
    monkeypatch.setattr(tr, "joint_transport_offsets", lambda *a, **kw: [[0.0, 0.0]])
    monkeypatch.setattr(ss, "stage_obstacles", lambda *a, **kw: [])
    monkeypatch.setattr(ss, "tool_margin", lambda *a, **kw: 0.01)

    svc = PlanService(_make_task(), _make_fixture(planning_joint_envelope_deg={"arm_r_j6": [-60.0, 50.0]}), Path("/tmp/no.sock"))
    svc.numerics = _StubNumerics([[_row_with_r_j6(52.3)]])
    with pytest.raises(HarnessError) as exc_info:
        svc._solve_route(_make_intent(), _DummyScene(), [0.0] * 16, [0.0] * 22, 2, joint_search=True)
    detail = numerical_failure_detail(exc_info.value)
    assert detail["code"] == "planning_joint_envelope_exceeded"
    assert detail["metrics"]["joint"] == "arm_r_j6"
    assert detail["metrics"]["route_family"] == "joint_space_transport"
    assert detail["metrics"]["target_deg"] == pytest.approx(52.3)
