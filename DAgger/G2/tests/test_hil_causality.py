import importlib.util
import json
from pathlib import Path
import sys

import pytest

from g2dagger.hil_episode import audit_hil_episode
from g2dagger.hil_supervisor import HILSupervisor


@pytest.fixture
def episode(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "scripts/33_hil_mock_episode.py"
    spec = importlib.util.spec_from_file_location("hil_mock", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "episode"
    monkeypatch.setattr(sys, "argv", [str(path), str(output)])
    assert module.main() == 0
    return output


@pytest.mark.parametrize("stream,index,field,value,expected", [
    ("policy_chunks", 1, "observation_request_id", 999, "observation reference"),
    ("policy_chunks", 1, "observation_monotonic_ns", 0, "timestamp mismatch"),
    ("policy_chunks", 0, "response_monotonic_ns", 10**30, "precedes policy response"),
    ("control", 0, "policy_request_id", 99, "control policy reference"),
    ("control", 0, "control_sequence", 99, "control_sequence"),
    ("control", 0, "hil_state", "hil_latched", "source/state mismatch"),
    ("observations", 1, "request_id", 0, "duplicate/invalid observation"),
])
def test_cross_stream_faults(episode, stream, index, field, value, expected):
    path = episode / f"{stream}.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows[index][field] = value
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = audit_hil_episode(episode)
    assert not report["valid"]
    assert any(expected in error for error in report["errors"])


def test_fresh_request_requires_new_observation_and_rejects_late_chunk():
    s = HILSupervisor()
    for toggle in range(3):
        s.observe_operator(session_id="s", hil_toggle_id=toggle, rb_deadman=False,
                           hand_motion_pending=False, monotonic_ns=100 + toggle)
    assert s.accept_fresh_policy_chunk(policy_epoch=2, request_id=1).state == "handoff_hold"
    for t in (101, 102):
        assert not s.register_fresh_policy_request(policy_epoch=2, request_id=1,
            observation_id=0, observation_monotonic_ns=t, request_monotonic_ns=104)
    assert s.register_fresh_policy_request(policy_epoch=2, request_id=1,
        observation_id=1, observation_monotonic_ns=103, request_monotonic_ns=104)
    assert s.accept_fresh_policy_chunk(policy_epoch=2, request_id=1, response_monotonic_ns=103).state == "handoff_hold"
    s.observe_operator(session_id="s", hil_toggle_id=3, rb_deadman=False,
                       hand_motion_pending=False, monotonic_ns=105)
    assert s.accept_fresh_policy_chunk(policy_epoch=2, request_id=1, response_monotonic_ns=106).state == "hil_latched"


def test_submission_can_differ_from_selection_without_fabricating_feedback(episode):
    path = episode / "control.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    row = rows[0]
    row["submitted_command_34d"] = [.123] * 34
    row["executed_command_34d"] = None
    row["measured_34d"] = None
    row["sdk_calls"] = [dict(interface="synthetic_partial_servo", joint_indices=list(range(7)),
        submitted_positions=[.123] * 7, started_monotonic_ns=row["monotonic_ns"],
        finished_monotonic_ns=row["monotonic_ns"] + 1, sdk_return="synthetic_success")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert audit_hil_episode(episode)["valid"]


def test_invalid_manifest_fails_closed(episode):
    (episode / "manifest.json").write_text("{")
    assert not audit_hil_episode(episode)["valid"]


def test_recording_modes_preserve_same_hil_control_main_chain(tmp_path, monkeypatch):
    import runpy
    script = Path(__file__).parents[1] / "scripts/33_hil_mock_episode.py"
    main = runpy.run_path(str(script))["main"]
    traces = []
    for mode in ("inference-only", "hil-record", "prebuffer-record"):
        output = tmp_path / mode
        monkeypatch.setattr(sys, "argv", [str(script), str(output), "--recording-mode", mode])
        assert main() == 0
        rows = [json.loads(line) for line in (output / "control.jsonl").read_text().splitlines()]
        traces.append([(r["control_sequence"], r["control_source"], r["selected_command_34d"]) for r in rows])
    assert traces[0] == traces[1] == traces[2]
