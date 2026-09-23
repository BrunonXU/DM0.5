from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts/19_analyze_f710_timing_ab.py"
    spec = importlib.util.spec_from_file_location("timing_ab_analysis", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timing_ab_report_uses_true_dispatch_intervals(tmp_path) -> None:
    path = tmp_path / "timing.jsonl"
    records = [
        {
            "event": "runtime_start",
            "profile": "pink_direct",
            "diagnostic_profile": "timing-ab",
            "otg_enabled": False,
        }
    ]
    for index in range(101):
        records.append(
            {
                "event": (
                    "f710_control_sample"
                    if index % 5 == 0
                    else "f710_dispatch_sample"
                ),
                "send_started_monotonic_ns": index * 10_000_000,
                "send_interval_seconds": None if index == 0 else 0.01,
                "control_interval_seconds": None if index == 0 else 0.01,
                "control_lateness_ms": 0.01,
                "ik_duration_ms": 1.0,
                "gdk_send_duration_ms": 0.3,
                "packet_age_ms": 10.0,
                "enabled": True,
                "reversal_count_total": 2,
                "control_sequence": index,
                "telemetry_drop_count": 0,
            }
        )
    records.append(
        {
            "event": "runtime_summary",
            "status": "closed",
            "drop_count_total": 0,
        }
    )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = _module().analyze(path)
    assert report["dispatch_samples"] == 101
    assert report["full_layer_samples"] == 21
    assert report["dispatch_interval_ms_p50_p95_p99_max"] == [10.0] * 4
    assert report["runtime_complete"] is True
    assert report["run_integrity_pass"] is True
    assert report["candidate_timing_gate_pass"] is True


def test_timing_gate_rejects_incomplete_faulted_or_dropped_run(tmp_path) -> None:
    module = _module()
    for label, terminal, drop, extra_event in (
        ("incomplete", None, 0, None),
        (
            "faulted",
            {"event": "runtime_summary", "status": "fault", "drop_count_total": 0},
            0,
            {"event": "runtime_fault", "error": "test fault"},
        ),
        (
            "dropped",
            {"event": "runtime_summary", "status": "closed", "drop_count_total": 3},
            3,
            None,
        ),
        (
            "watchdog",
            {"event": "runtime_summary", "status": "closed", "drop_count_total": 0},
            0,
            {"event": "f710_control_event", "kind": "watchdog_hold"},
        ),
    ):
        path = tmp_path / f"{label}.jsonl"
        records = [
            {
                "event": "runtime_start",
                "diagnostic_profile": "timing-ab",
                "otg_enabled": False,
            }
        ]
        for index in range(101):
            records.append(
                {
                    "event": "f710_dispatch_sample",
                    "send_started_monotonic_ns": index * 10_000_000,
                    "send_interval_seconds": None if index == 0 else 0.01,
                    "control_sequence": index,
                    "telemetry_drop_count": drop,
                    "enabled": True,
                }
            )
        if extra_event is not None:
            records.append(extra_event)
        if terminal is not None:
            records.append(terminal)
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

        report = module.analyze(path)
        assert report["timing_thresholds_pass"] is True
        assert report["run_integrity_pass"] is False
        assert report["candidate_timing_gate_pass"] is False


def test_timing_gate_rejects_short_or_wrong_profile_run(tmp_path) -> None:
    path = tmp_path / "short.jsonl"
    records = [
        {"event": "runtime_start", "diagnostic_profile": "full"},
        *[
            {
                "event": "f710_dispatch_sample",
                "send_started_monotonic_ns": index * 10_000_000,
                "send_interval_seconds": None if index == 0 else 0.01,
                "control_sequence": index,
                "telemetry_drop_count": 0,
            }
            for index in range(10)
        ],
        {"event": "runtime_summary", "status": "closed", "drop_count_total": 0},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = _module().analyze(path)
    assert report["timing_thresholds_pass"] is False
    assert report["run_integrity_pass"] is False
    assert report["candidate_timing_gate_pass"] is False


def test_timing_gate_rejects_missing_control_sequence(tmp_path) -> None:
    path = tmp_path / "missing-sequence.jsonl"
    records = [
        {"event": "runtime_start", "diagnostic_profile": "timing-ab"},
        *[
            {
                "event": "f710_dispatch_sample",
                "send_started_monotonic_ns": index * 10_000_000,
                "send_interval_seconds": None if index == 0 else 0.01,
                **({"control_sequence": index} if index != 50 else {}),
                "telemetry_drop_count": 0,
            }
            for index in range(101)
        ],
        {"event": "runtime_summary", "status": "closed", "drop_count_total": 0},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = _module().analyze(path)
    assert report["timing_thresholds_pass"] is True
    assert report["control_sequence_missing"] == 1
    assert report["run_integrity_pass"] is False
    assert report["candidate_timing_gate_pass"] is False


def test_dispatch_delta_ik_correlation_detects_pre_ik_pacer_signature() -> None:
    correlate = _module().dispatch_delta_ik_correlation
    ik = [1.0, 2.0, 1.5, 3.0, 1.0]
    delta = [None, 1.0, -0.5, 1.5, -2.0]
    records = [
        {
            "ik_duration_ms": value,
            "send_interval_seconds": (
                None if change is None else (10.0 + change) / 1000.0
            ),
        }
        for value, change in zip(ik, delta)
    ]
    assert correlate(records) == pytest.approx(1.0)
