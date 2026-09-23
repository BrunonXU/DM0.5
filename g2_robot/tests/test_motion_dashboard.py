import json
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.joint_feedback import (
    JointFeedbackWriter,
    load_validated_feedback_hz,
)
from g2_sort.validation.build_motion_dashboard import build_dashboard


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _run_records(wall_time: float, mode: str = "powered-sync") -> list[dict]:
    actions = np.zeros((8, 34), dtype=float)
    actions[:, 0] = np.linspace(0.0, 0.1, len(actions))
    actions[:, 14] = np.sin(np.linspace(0.0, np.pi, len(actions))) * 0.1
    return [
        {
            "event": "runtime_start",
            "wall_time": wall_time,
            "mode": mode,
            "hand_stream_hz": 10.0,
            "endpoint": "ws://example:7892/process_frame",
        },
        {
            "event": "action_chunk",
            "wall_time": wall_time + 0.1,
            "mode": mode,
            "request_id": 0,
            "latency_seconds": 0.2,
            "response_received_at_monotonic": 10.0,
            "used_start_index": 0,
            "used_intervals": 3,
            "actions": actions.tolist(),
        },
        {
            "event": "dispatch_summary",
            "wall_time": wall_time + 0.4,
            "mode": mode,
            "request_id": 0,
            "segment_started_at_monotonic": 10.0,
            "arm_commands_rad": np.zeros((4, 14)).tolist(),
            "arm_command_time_offsets_seconds": [0.0, 0.01, 0.02, 0.03],
            "hand_commands_rad": np.zeros((2, 20)).tolist(),
            "hand_command_time_offsets_seconds": [0.0, 0.1],
            "arm_send_interval_p95_seconds": 0.01,
            "hand_send_interval_p95_seconds": 0.1,
        },
    ]


def test_dashboard_is_reverse_chronological_and_idempotent(tmp_path):
    log_root = tmp_path / "logs"
    older = log_root / "telemetry" / "powered" / "older.jsonl"
    newer = log_root / "telemetry" / "powered" / "newer.jsonl"
    _write_jsonl(older, _run_records(1000.0))
    _write_jsonl(newer, _run_records(2000.0))
    output = log_root / "analysis" / "motion_dashboard"

    first = build_dashboard(log_root, output)
    second = build_dashboard(log_root, output)

    assert [run["source_relative"] for run in first] == [
        "telemetry/powered/newer.jsonl",
        "telemetry/powered/older.jsonl",
    ]
    assert [run["run_id"] for run in second] == [run["run_id"] for run in first]
    assert (output / "index.html").exists()
    assert (output / "compare.html").exists()
    assert all(
        (output / "runs" / run["run_id"] / "report.html").exists()
        for run in first
    )
    index_html = (output / "index.html").read_text(encoding="utf-8")
    assert 'id="baseline"' not in index_html
    report_dir = output / "runs" / first[0]["run_id"]
    report_html = (report_dir / "report.html").read_text(encoding="utf-8")
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert "选择另一轮对比" in report_html
    assert report["diagnostic_coverage"][0]["key"] == "A"
    assert report["ranking"][0]["travel_rad"] <= 0.1


def test_dashboard_tolerates_malformed_and_model_only_log(tmp_path):
    log_root = tmp_path / "logs"
    source = log_root / "telemetry" / "shadow" / "legacy.jsonl"
    records = _run_records(1000.0, mode="shadow")[:2]
    source.parent.mkdir(parents=True)
    source.write_text(
        "not-json\n" + "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    runs = build_dashboard(log_root, log_root / "analysis" / "motion_dashboard")
    assert runs[0]["malformed_lines"] == 1
    assert runs[0]["layers"] == {
        "model": True,
        "command": False,
        "feedback": False,
    }


def test_feedback_probe_contract_and_sidecar(tmp_path):
    probe = tmp_path / "probe.json"
    probe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_hz": 10.0,
                "results": [{"hz": 10.0, "passed": True}],
            }
        ),
        encoding="utf-8",
    )
    assert load_validated_feedback_hz(probe) == 10.0
    probe.write_text('{"schema_version":1,"selected_hz":20,"results":[]}', encoding="utf-8")
    assert load_validated_feedback_hz(probe) == 0.0

    sidecar = tmp_path / "feedback.jsonl"
    writer = JointFeedbackWriter(sidecar, run_id="run-1", capacity=8)
    assert writer.submit(
        status="ok",
        request_id=3,
        joints_rad=[0.0] * 34,
        valid=[True] * 34,
    )
    writer.close()
    record = json.loads(sidecar.read_text(encoding="utf-8").splitlines()[0])
    assert record["run_id"] == "run-1"
    assert record["sequence"] == 0
    assert len(record["joints_rad"]) == 34


def test_dashboard_generates_feedback_error_and_spectrum_when_valid(tmp_path):
    log_root = tmp_path / "logs"
    source = log_root / "telemetry" / "powered" / "full.jsonl"
    records = _run_records(1000.0)
    sample_count = 200
    offsets = np.arange(sample_count, dtype=float) / 20.0
    arm = np.zeros((sample_count, 14), dtype=float)
    hand = np.zeros((sample_count, 20), dtype=float)
    arm[:, 0] = 0.05 * np.sin(2 * np.pi * offsets)
    hand[:, 0] = 0.03 * np.sin(2 * np.pi * offsets)
    records[-1].update(
        {
            "arm_commands_rad": arm.tolist(),
            "arm_command_time_offsets_seconds": offsets.tolist(),
            "hand_commands_rad": hand.tolist(),
            "hand_command_time_offsets_seconds": offsets.tolist(),
        }
    )
    _write_jsonl(source, records)
    feedback = []
    combined = np.concatenate((arm, hand), axis=1)
    for index, (offset, row) in enumerate(zip(offsets, combined, strict=True)):
        feedback.append(
            {
                "event": "joint_feedback_sample",
                "request_id": 0,
                "sequence": index,
                "read_completed_monotonic_ns": int((10.0 + offset) * 1e9),
                "joints_rad": row.tolist(),
                "valid": [True] * 34,
            }
        )
    _write_jsonl(log_root / "telemetry" / "feedback" / "full.jsonl", feedback)
    runs = build_dashboard(log_root, log_root / "analysis" / "motion_dashboard")
    report = json.loads(
        (
            log_root
            / "analysis"
            / "motion_dashboard"
            / "runs"
            / runs[0]["run_id"]
            / "report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["layers"]["feedback"] is True
    assert "tracking_error.png" in report["figures"]
    assert "spectrum_coherence.png" in report["figures"]
    assert report["spectral_valid"] is True


def test_dashboard_keeps_f710_pipeline_separate_from_vla_model(tmp_path):
    log_root = tmp_path / "logs"
    source = log_root / "telemetry" / "powered" / "g2_powered_f710_test.jsonl"
    records: list[dict] = [
        {
            "event": "runtime_start",
            "wall_time": 3000.0,
            "mode": "powered-f710",
            "profile": "pink_direct_reference_no_otg",
            "endpoint": "windows-ssh-tunnel:7904",
            "control_hz": 100.0,
            "arm_hz": 100.0,
            "hand_stream_hz": 10.0,
            "otg_enabled": False,
        }
    ]
    for index in range(120):
        phase = index / 119.0
        left = (0.02 * np.sin(phase * np.pi * 2)).item()
        command = np.zeros(34)
        command[0] = left
        records.append(
            {
                "event": "f710_control_sample",
                "wall_time": 3000.0 + index / 100.0,
                "mode": "powered-f710",
                "control_monotonic_ns": int((50.0 + index / 100.0) * 1e9),
                "active_side": "left",
                "enabled": True,
                "shaped_linear_axes": [0.4, 0.0, 0.0],
                "shaped_angular_axes": [0.0, 0.0, 0.0],
                "desired_linear_velocity_m_s": [0.016, 0.0, 0.0],
                "desired_angular_velocity_rad_s": [0.0, 0.0, 0.0],
                "filtered_linear_velocity_m_s": [0.016 * phase, 0.0, 0.0],
                "filtered_angular_velocity_rad_s": [0.0, 0.0, 0.0],
                "ik_q_ref_rad": [left, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "ik_dq_ref_rad_s": [0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "ik_position_error_m": 0.001,
                "ik_orientation_error_rad": 0.002,
                "joint_lower_margin_rad": [0.4] * 7,
                "joint_upper_margin_rad": [0.5] * 7,
                "jacobian_singular_values_raw": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1],
                "workspace_saturated": False,
                "command_q_rad": command.tolist(),
                "clip_count": 0,
                "packet_age_ms": 4.0,
                "gdk_send_duration_ms": 0.4,
                "reversal_count_total": 0,
                "telemetry_drop_count": 0,
                "otg_enabled": False,
            }
        )
    records.append(
        {
            "event": "runtime_summary",
            "wall_time": 9999.0,
            "mode": "powered-f710",
            "status": "recovered_after_feedback_finalizer_error",
            "recovered": True,
        }
    )
    _write_jsonl(source, records)

    runs = build_dashboard(log_root, log_root / "analysis" / "motion_dashboard")
    assert len(runs) == 1
    run = runs[0]
    assert run["mode_family"] == "f710"
    assert run["layers"] == {
        "input": True,
        "cartesian": True,
        "ik": True,
        "command": True,
        "feedback": False,
    }
    assert 1.18 <= run["duration_seconds"] <= 1.20
    assert run["recovered"] is True
    report_dir = log_root / "analysis" / "motion_dashboard" / "runs" / run["run_id"]
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    report_html = (report_dir / "report.html").read_text(encoding="utf-8")
    index_html = (log_root / "analysis" / "motion_dashboard" / "index.html").read_text(encoding="utf-8")
    assert "f710_pipeline.png" in report["figures"]
    assert "f710_constraints.png" in report["figures"]
    assert "f710_joint_layers.png" in report["figures"]
    assert report["diagnostic_coverage"][-1]["status"] == "尚未接入"
    assert "不是VLA model输出" in report_html
    assert "F710 摇操" in index_html
    assert "family-tab sync" in index_html
    assert "family-tab rtc" in index_html


def test_dashboard_hides_f710_startup_fault_without_motion_samples(tmp_path):
    log_root = tmp_path / "logs"
    source = (
        log_root
        / "telemetry"
        / "powered"
        / "g2_powered_f710_startup_fault.jsonl"
    )
    _write_jsonl(
        source,
        [
            {
                "event": "runtime_fault",
                "wall_time": 3000.0,
                "mode": "powered-f710",
                "error": "servo mode unhealthy",
            },
            {
                "event": "runtime_summary",
                "wall_time": 3000.1,
                "mode": "powered-f710",
                "status": "fault",
            },
        ],
    )
    output = log_root / "analysis" / "motion_dashboard"
    stale = output / "runs" / "stale-generated-run"
    stale.mkdir(parents=True)
    (stale / "report.html").write_text("stale", encoding="utf-8")

    runs = build_dashboard(log_root, output)

    assert runs == []
    assert not stale.exists()
    assert "g2_powered_f710_startup_fault" not in (
        output / "index.html"
    ).read_text(encoding="utf-8")


def test_dashboard_recognizes_zero_output_f710_otg_shadow(tmp_path):
    log_root = tmp_path / "logs"
    source = (
        log_root
        / "telemetry"
        / "shadow"
        / "g2_shadow_f710_ruckig_test.jsonl"
    )
    records: list[dict] = [
        {
            "event": "runtime_start",
            "wall_time": 4000.0,
            "mode": "shadow-f710-otg",
            "profile": "pink_singularity_guard_v1_ruckig_velocity_otg_v1",
            "endpoint": "windows-ssh-tunnel:7904",
            "control_hz": 100.0,
            "hardware_output": False,
            "otg_enabled": True,
        }
    ]
    for index in range(40):
        q = np.zeros(34)
        q[0] = index * 0.0001
        records.append(
            {
                "event": "f710_control_sample",
                "wall_time": 4000.0 + index / 100.0,
                "mode": "shadow-f710-otg",
                "control_monotonic_ns": int((80.0 + index / 100.0) * 1e9),
                "active_side": "left",
                "enabled": True,
                "shaped_linear_axes": [0.1, 0.0, 0.0],
                "shaped_angular_axes": [0.0, 0.0, 0.0],
                "desired_linear_velocity_m_s": [0.004, 0.0, 0.0],
                "filtered_linear_velocity_m_s": [0.004, 0.0, 0.0],
                "ik_q_ref_rad": q[:7].tolist(),
                "ik_dq_ref_rad_s": [0.01] + [0.0] * 6,
                "otg_target_dq_rad_s": [0.01] + [0.0] * 6,
                "otg_dq_cmd_rad_s": [0.009] + [0.0] * 6,
                "otg_acceleration_limit_utilization": [0.2] + [0.0] * 6,
                "otg_jerk_limit_utilization": [0.5] + [0.0] * 6,
                "joint_lower_margin_rad": [0.5] * 7,
                "joint_upper_margin_rad": [0.5] * 7,
                "jacobian_singular_values_raw": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1],
                "command_q_rad": q.tolist(),
                "workspace_saturated": False,
                "clip_count": 0,
                "packet_age_ms": 4.0,
                "reversal_count_total": 0,
                "otg_enabled": True,
                "hardware_output": False,
            }
        )
    _write_jsonl(source, records)

    runs = build_dashboard(log_root, log_root / "analysis" / "motion_dashboard")
    assert len(runs) == 1
    run = runs[0]
    assert run["mode"] == "shadow-f710-otg"
    assert run["mode_family"] == "f710"
    assert run["otg_enabled"] is True
    assert run["ranking_origin"] == "virtual_100hz_command"
    report_dir = log_root / "analysis" / "motion_dashboard" / "runs" / run["run_id"]
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    report_html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "f710_pipeline.png" in report["figures"]
    assert report["diagnostic_coverage"][-1]["status"] == "完整"
    assert "ZERO-OUTPUT F710 OTG SHADOW" in report_html
    assert "虚拟双臂command活动排名" in report_html


def test_dashboard_uses_timing_ab_dispatch_rows_as_true_100hz_command(tmp_path):
    log_root = tmp_path / "logs"
    source = (
        log_root
        / "telemetry"
        / "powered"
        / "g2_powered_f710_timing_ab.jsonl"
    )
    records: list[dict] = [
        {
            "event": "runtime_start",
            "wall_time": 5000.0,
            "mode": "powered-f710",
            "profile": "pink_singularity_guard_v1_ruckig_velocity_otg_v1",
            "endpoint": "windows-ssh-tunnel:7904",
            "control_hz": 100.0,
            "diagnostic_profile": "timing-ab",
            "telemetry_dispatch_hz": 100.0,
            "telemetry_detail_hz": 20.0,
            "otg_enabled": True,
        }
    ]
    for index in range(101):
        arm = np.zeros(14)
        arm[0] = index * 0.0001
        record = {
            "event": (
                "f710_control_sample"
                if index % 5 == 0
                else "f710_dispatch_sample"
            ),
            "wall_time": 5000.0 + index / 100.0,
            "mode": "powered-f710",
            "control_monotonic_ns": int((100.0 + index / 100.0) * 1e9),
            "send_started_monotonic_ns": int((100.001 + index / 100.0) * 1e9),
            "send_interval_seconds": None if index == 0 else 0.01,
            "active_side": "left",
            "enabled": True,
            "shaped_linear_axes": [0.4, 0.0, 0.0],
            "shaped_angular_axes": [0.0, 0.0, 0.0],
            "arm_command_rad": arm.tolist(),
            "workspace_saturated": False,
            "clip_count": 0,
            "reversal_count_total": 0,
            "telemetry_drop_count": 0,
        }
        if index % 5 == 0:
            record.update(
                {
                    "desired_linear_velocity_m_s": [0.016, 0.0, 0.0],
                    "filtered_linear_velocity_m_s": [0.016, 0.0, 0.0],
                    "ik_q_ref_rad": arm[:7].tolist(),
                    "ik_dq_ref_rad_s": [0.01] + [0.0] * 6,
                    "otg_enabled": True,
                    "otg_target_dq_rad_s": [0.01] + [0.0] * 6,
                    "otg_dq_cmd_rad_s": [0.01] + [0.0] * 6,
                    "joint_lower_margin_rad": [0.5] * 7,
                    "joint_upper_margin_rad": [0.5] * 7,
                    "jacobian_singular_values_raw": [1.0] * 6,
                }
            )
        records.append(record)
    _write_jsonl(source, records)

    runs = build_dashboard(log_root, log_root / "analysis" / "motion_dashboard")
    report = runs[0]
    assert report["sample_count"] == 101
    assert report["detail_sample_count"] == 21
    assert report["dispatch_interval_p95_ms"] == 10.0
    assert report["layers"]["command"] is True
    assert report["diagnostic_coverage"][4]["status"] == "实验性关闭"
    assert report["ranking"][0]["joint"] == "idx21_arm_l_joint1"
