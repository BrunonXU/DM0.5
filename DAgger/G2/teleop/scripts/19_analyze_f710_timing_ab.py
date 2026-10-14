#!/usr/bin/env python3
"""Compare F710 timing-A/B telemetry; never imports GDK or commands hardware."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("telemetry", type=Path, nargs="+")
    return parser.parse_args()


def percentile(values: list[float], points: tuple[int, ...]) -> list[float] | None:
    if not values:
        return None
    return np.percentile(np.asarray(values, dtype=np.float64), points).tolist()


def dispatch_delta_ik_correlation(records: list[dict[str, Any]]) -> float | None:
    """Measure the old pre-IK-pacer signature without mixing invalid ticks."""
    intervals: list[float] = []
    delta_ik: list[float] = []
    previous_ik: float | None = None
    for record in records:
        raw_ik = record.get("ik_duration_ms")
        raw_interval = record.get("send_interval_seconds")
        current_ik = float(raw_ik) if isinstance(raw_ik, (int, float)) else None
        if (
            current_ik is not None
            and previous_ik is not None
            and isinstance(raw_interval, (int, float))
        ):
            intervals.append(float(raw_interval) * 1000.0)
            delta_ik.append(current_ik - previous_ik)
        previous_ik = current_ik
    if len(intervals) < 3:
        return None
    interval_array = np.asarray(intervals, dtype=np.float64)
    delta_array = np.asarray(delta_ik, dtype=np.float64)
    if np.std(interval_array) == 0.0 or np.std(delta_array) == 0.0:
        return None
    return float(np.corrcoef(interval_array, delta_array)[0, 1])


def analyze(path: Path) -> dict[str, Any]:
    start: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    dispatch: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    faults: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}: malformed JSONL line {number}: {exc}") from exc
            event = record.get("event")
            if event == "runtime_start":
                start = record
            elif event == "runtime_summary":
                summary = record
            elif event in {"f710_control_sample", "f710_dispatch_sample"}:
                dispatch.append(record)
            elif event == "f710_control_event":
                events.append(record)
            elif event == "runtime_fault":
                faults.append(record)
    if start is None or not dispatch:
        raise RuntimeError(f"{path}: missing runtime_start or dispatch samples")

    def values(key: str, scale: float = 1.0) -> list[float]:
        return [
            float(record[key]) * scale
            for record in dispatch
            if isinstance(record.get(key), (int, float))
        ]

    intervals = values("send_interval_seconds", 1000.0)
    control = values("control_interval_seconds", 1000.0)
    lateness = values("control_lateness_ms")
    ik = values("ik_duration_ms")
    gdk = values("gdk_send_duration_ms")
    packet_age = values("packet_age_ms")
    enabled_count = sum(bool(record.get("enabled")) for record in dispatch)
    first_ns = int(dispatch[0]["send_started_monotonic_ns"])
    last_ns = int(dispatch[-1]["send_started_monotonic_ns"])
    duration = max(0.0, (last_ns - first_ns) / 1e9)
    final_reversals = int(dispatch[-1].get("reversal_count_total", 0))
    holds = [
        str(record.get("kind"))
        for record in events
        if str(record.get("kind", "")).endswith("_hold")
    ]
    watchdogs = sum(record.get("kind") == "watchdog_hold" for record in events)
    control_sequences = [
        int(record["control_sequence"])
        for record in dispatch
        if isinstance(record.get("control_sequence"), int)
    ]
    control_sequence_missing = len(dispatch) - len(control_sequences)
    sequence_gaps = sum(
        max(0, current - previous - 1)
        for previous, current in zip(control_sequences, control_sequences[1:])
    )
    sequence_nonmonotonic = sum(
        current <= previous
        for previous, current in zip(control_sequences, control_sequences[1:])
    )
    telemetry_drop_total = max(
        [
            int(record.get("telemetry_drop_count", 0))
            for record in dispatch
            if isinstance(record.get("telemetry_drop_count"), int)
        ]
        + [
            int(summary.get("drop_count_total", 0))
            if summary is not None
            and isinstance(summary.get("drop_count_total"), int)
            else 0
        ]
    )
    timing_only_gate = bool(
        intervals
        and len(intervals) >= 100
        and np.percentile(intervals, 95) <= 10.8
        and np.percentile(intervals, 99) <= 12.0
        and not any(value > 15.0 for value in intervals)
    )
    run_complete = bool(
        summary is not None
        and summary.get("status") == "closed"
        and not bool(summary.get("recovered"))
        and not faults
    )
    profile_valid = bool(start.get("diagnostic_profile") == "timing-ab")
    integrity_gate = bool(
        run_complete
        and profile_valid
        and telemetry_drop_total == 0
        and control_sequence_missing == 0
        and sequence_gaps == 0
        and sequence_nonmonotonic == 0
        and watchdogs == 0
        and not holds
    )
    candidate_gate = timing_only_gate and integrity_gate
    return {
        "path": str(path),
        "profile": start.get("profile"),
        "diagnostic_profile": start.get("diagnostic_profile"),
        "otg_enabled": bool(start.get("otg_enabled")),
        "duration_seconds": duration,
        "dispatch_samples": len(dispatch),
        "full_layer_samples": sum(
            record.get("event") == "f710_control_sample" for record in dispatch
        ),
        "enabled_samples": enabled_count,
        "runtime_complete": run_complete,
        "runtime_summary_status": None if summary is None else summary.get("status"),
        "runtime_faults": [str(record.get("error")) for record in faults],
        "telemetry_drop_total": telemetry_drop_total,
        "control_sequence_gaps": sequence_gaps,
        "control_sequence_missing": control_sequence_missing,
        "control_sequence_nonmonotonic": sequence_nonmonotonic,
        "dispatch_interval_ms_p50_p95_p99_max": percentile(
            intervals, (50, 95, 99, 100)
        ),
        "dispatch_intervals_over_11_12_15_ms": [
            sum(value > threshold for value in intervals)
            for threshold in (11.0, 12.0, 15.0)
        ],
        "control_interval_ms_p50_p95_p99_max": percentile(
            control, (50, 95, 99, 100)
        ),
        "pacer_lateness_ms_p50_p95_p99_max": percentile(
            lateness, (50, 95, 99, 100)
        ),
        "ik_ms_p50_p95_p99_max": percentile(ik, (50, 95, 99, 100)),
        "gdk_send_ms_p50_p95_p99_max": percentile(gdk, (50, 95, 99, 100)),
        "dispatch_interval_delta_ik_correlation": dispatch_delta_ik_correlation(
            dispatch
        ),
        "packet_age_ms_p50_p95_p99_max": percentile(
            packet_age, (50, 95, 99, 100)
        ),
        "small_reversals": final_reversals,
        "small_reversals_per_enabled_second": (
            None
            if enabled_count == 0
            else final_reversals / (enabled_count / 100.0)
        ),
        "hold_events": holds,
        "watchdog_holds": watchdogs,
        "timing_thresholds_pass": timing_only_gate,
        "run_integrity_pass": integrity_gate,
        "candidate_timing_gate_pass": candidate_gate,
    }


def main() -> int:
    reports = [analyze(path) for path in parse_args().telemetry]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
