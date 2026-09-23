from __future__ import annotations

import json

from g2teleop.control_telemetry import ControlTelemetry


def test_control_telemetry_writes_versioned_records(tmp_path) -> None:
    path = tmp_path / "powered" / "f710.jsonl"
    telemetry = ControlTelemetry(path, run_id="f710-test", capacity=8)
    assert telemetry.emit(
        "runtime_start",
        mode="powered-f710",
        control_hz=100.0,
    )
    assert telemetry.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["event"] for record in records] == [
        "runtime_start",
        "runtime_summary",
    ]
    assert all(record["schema_version"] == 1 for record in records)
    assert all(record["telemetry_family"] == "f710_teleop" for record in records)
    assert records[0]["run_id"] == "f710-test"
    assert isinstance(records[0]["monotonic_ns"], int)
    assert not telemetry.partial_path.exists()


def test_control_telemetry_drains_a_large_shutdown_backlog(tmp_path) -> None:
    path = tmp_path / "powered" / "large.jsonl"
    telemetry = ControlTelemetry(path, run_id="large", capacity=8192)
    payload = [float(index) for index in range(34)]
    for index in range(5000):
        assert telemetry.emit("sample", index=index, command_q_rad=payload)
    assert telemetry.close()
    assert not telemetry.partial_path.exists()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 5001
    assert records[-1]["event"] == "runtime_summary"


def test_control_telemetry_preserves_shadow_mode_in_summary(tmp_path) -> None:
    path = tmp_path / "shadow.jsonl"
    telemetry = ControlTelemetry(path, run_id="shadow", mode="shadow-f710-otg")
    assert telemetry.emit("runtime_start", mode="shadow-f710-otg")
    assert telemetry.close()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["mode"] == "shadow-f710-otg"
