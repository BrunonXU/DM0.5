"""Physical mapping and camera recovery without weakening robot failure gates."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_inference_execution import (BINDING, NOW, capture, profile, response, snapshot)
from f1_policy_client.config import load_config
from f1_policy_client.execution import prepare_prefix
from f1_policy_client.observation import CameraObservationError
from f1_policy_client import runtime
from f1_replay.runtime import ReplayError


def adapted(value, current=None):
    _, p = load_config()
    return prepare_prefix(value, capture(), current or snapshot(), p, now_ns=NOW,
                          request_id="request-7", expected_model_binding=BINDING,
                          max_observation_age_s=2.)


def test_saturation_preserves_raw_response_and_records_each_changed_command():
    value = response()
    value["gripper_targets"][0] = [100.78125, -2.]
    value["base_velocity_targets"][0][2] = -.02734375
    original = deepcopy(value)
    result = adapted(value)
    assert result.bundle.frames[0].action[14:16] == (100., 0.)
    assert result.bundle.frames[0].action[-1] == -.02734375
    assert value == original == result.response
    assert result.gripper_mapping == (
        {"frame": 0, "side": "left", "raw": 100.78125, "command": 100.},
        {"frame": 0, "side": "right", "raw": -2., "command": 0.})


@pytest.mark.parametrize("field,col,bad", [
    ("gripper_targets", 0, float("nan")), ("joint_targets_deg", 0, 200.),
    ("base_velocity_targets", 2, .80001), ("joint_targets_deg", 0, 5.02)])
def test_mapping_does_not_bypass_nonfinite_limits_or_first_arm_jump(field, col, bad):
    value = response()
    value[field][0][col] = bad
    with pytest.raises(ReplayError):
        adapted(value)


def test_inference_profile_requires_live_parameter_binding_for_yaw(tmp_path):
    task = Path(__file__).resolve().parents[1]
    doc = json.loads((task / "configs/inference.5k.json").read_text())
    doc["client"]["runtime_profile"] = str(task / "configs/replay_f1.json")
    doc["client"]["body_reference"] = str(task / "configs/inference_body_reference.json")
    doc["client"]["profile_overrides"].pop("base_controller_expected_parameters")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ReplayError, match="必须绑定"):
        load_config(path)


def test_expanded_inference_first_step_is_bounded_logged_and_preserves_targets():
    value = response()
    value["joint_targets_deg"][0][4] = -2.36606
    value["joint_targets_deg"][0][6] = -2.19319
    original = deepcopy(value)
    result = adapted(value)
    assert result.first_arm_delta_deg[4] == -2.36606
    assert result.bundle.frames[0].action[:14] == tuple(value["joint_targets_deg"][0])
    assert result.response == value == original
    value["joint_targets_deg"][0][4] = -5.01
    with pytest.raises(ReplayError, match=r"limit=5deg.*arm_l_j5.*delta=-5.01000"):
        adapted(value)
    _, p = load_config()
    assert p["start_arm_tolerance_deg"] == 2.0


def test_camera_retry_recovers_without_relaxing_observation_budget(monkeypatch):
    clock, ticks, events = [0.], [], []
    expected = capture()
    answers = iter([CameraObservationError("skew", {"head": {"age_s": .15}}), expected])
    def observe(*args):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(runtime, "observe", observe)
    got = runtime.wait_camera_observation(None, {"camera_wait_s": 1.}, {},
        lambda: ticks.append(True), lambda event, data: events.append((event, data)),
        now=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0]+dt))
    assert got is expected and len(ticks) == 2
    assert [e[0] for e in events] == ["camera_wait", "camera_wait_recovered"]
    assert events[0][1]["cameras"]["head"]["age_s"] == .15


def test_camera_wait_timeout_is_bounded_and_logs_last_snapshot(monkeypatch):
    clock, events = [0.], []
    def observe(*args):
        raise CameraObservationError("missing", {"head": {"age_s": None}})
    monkeypatch.setattr(runtime, "observe", observe)
    with pytest.raises(ReplayError, match="三图超时"):
        runtime.wait_camera_observation(None, {"camera_wait_s": .01}, {}, lambda: None,
            lambda event, data: events.append((event, data)), now=lambda: clock[0],
            sleep=lambda dt: clock.__setitem__(0, clock[0]+dt))
    assert clock[0] == .01
    assert events[-1][0] == "camera_wait_timeout"


@pytest.mark.parametrize("error_in_tick", [False, True])
def test_camera_wait_never_swallows_feedback_or_ownership_failure(monkeypatch, error_in_tick):
    def fail(*args):
        raise ReplayError("hardware/ownership failure")
    monkeypatch.setattr(runtime, "observe", fail)
    sleeps = []
    with pytest.raises(ReplayError, match="hardware/ownership"):
        runtime.wait_camera_observation(None, {}, {}, fail if error_in_tick else lambda: None,
                                        lambda *a: None, sleep=sleeps.append)
    assert sleeps == []
