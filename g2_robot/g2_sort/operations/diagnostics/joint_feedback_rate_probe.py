"""Read-only 34D GDK feedback rate probe; this module never sends motion."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from g2_sort.deployment.g2_client.joint_feedback import PROBE_SCHEMA_VERSION
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader


def measure(reader: Any, hz: float, samples: int) -> dict[str, Any]:
    period = 1.0 / hz
    durations: list[float] = []
    failures: list[str] = []
    overruns = 0
    next_read = time.monotonic()
    for _ in range(samples):
        remaining = next_read - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        started = time.monotonic()
        try:
            state = np.asarray(reader.read(), dtype=np.float64)
            if state.shape != (34,) or not np.all(np.isfinite(state)):
                raise ValueError(f"invalid state shape={state.shape}")
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
        duration = time.monotonic() - started
        durations.append(duration)
        if duration > period:
            overruns += 1
        next_read += period
        if time.monotonic() > next_read + period:
            next_read = time.monotonic()
    array = np.asarray(durations, dtype=np.float64)
    p95 = float(np.percentile(array, 95))
    maximum = float(np.max(array))
    overrun_ratio = overruns / samples
    passed = not failures and p95 <= 0.002 and maximum <= 0.005 and overrun_ratio < 0.01
    return {
        "hz": hz, "samples": samples, "failure_count": len(failures),
        "failure_examples": failures[:3],
        "read_p50_seconds": float(np.percentile(array, 50)),
        "read_p95_seconds": p95, "read_max_seconds": maximum,
        "period_overrun_count": overruns, "period_overrun_ratio": overrun_ratio,
        "passed": passed,
    }


def run_probe(output: Path, samples: int = 300) -> dict[str, Any]:
    import agibot_gdk as gdk
    result = gdk.gdk_init()
    if result != gdk.GDKRes.kSuccess:
        raise RuntimeError(f"gdk_init failed: {result}")
    try:
        reader = StrictRobotStateReader(gdk.Robot())
        results = [measure(reader, hz, samples) for hz in (5.0, 10.0, 20.0)]
    finally:
        gdk.gdk_release()
    passing = [float(item["hz"]) for item in results if item["passed"]]
    selected = max(passing, default=0.0)
    if selected < 10.0:
        selected = 0.0
    payload = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "created_at": time.time(), "selected_hz": selected,
        "thresholds": {"read_p95_seconds": 0.002, "read_max_seconds": 0.005,
                       "period_overrun_ratio_exclusive": 0.01, "failure_count": 0},
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("logs/diagnostics/joint_feedback_probe_latest.json"))
    parser.add_argument("--samples", type=int, default=300)
    args = parser.parse_args(argv)
    if args.samples < 10:
        parser.error("--samples must be at least 10")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_probe(args.output, args.samples)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["selected_hz"]:
        print("STOP: 10/20Hz均未通过；Powered保持command-only。")
        return 1
    print(f"PASS: Powered反馈候选={payload['selected_hz']:g}Hz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
