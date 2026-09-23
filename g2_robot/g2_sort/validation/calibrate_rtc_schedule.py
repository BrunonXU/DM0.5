#!/usr/bin/env python3
"""Turn measured observation-age telemetry into RTC d/s candidates.

Latency can reject impossible candidates; it cannot select trajectory quality.
The output therefore recommends d for a requested coverage quantile and lists
feasible s values, but deliberately leaves the final d/s decision to the
fixed-seed RTC action/trajectory A/B.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import CHUNK_STEPS, MODEL_HZ


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--coverage-percentile", type=float, default=99.0)
    parser.add_argument("--margin-steps", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def _load_ages(paths: list[Path]) -> list[float]:
    ages = []
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            record = json.loads(line)
            if record.get("event") != "action_chunk":
                continue
            age = float(record["observation_age_seconds"])
            if not np.isfinite(age) or age < 0:
                raise ValueError(f"{path}:{line_number}: invalid observation age")
            ages.append(age)
    if not ages:
        raise RuntimeError("no action_chunk observation ages found")
    return ages


def build_calibration(
    ages: list[float],
    *,
    coverage_percentile: float,
    margin_steps: int,
) -> dict:
    if not 50.0 <= coverage_percentile <= 100.0:
        raise ValueError("coverage percentile must be in [50,100]")
    if margin_steps < 0:
        raise ValueError("margin steps cannot be negative")
    real_steps = np.asarray(
        [int(math.ceil(age * MODEL_HZ - 1e-12)) for age in ages],
        dtype=np.int64,
    )
    quantile_age = _percentile(ages, coverage_percentile)
    recommended_d = int(math.ceil(quantile_age * MODEL_HZ - 1e-12)) + margin_steps
    recommended_d = min(CHUNK_STEPS // 2, recommended_d)

    candidates = []
    for delay in range(0, CHUNK_STEPS // 2 + 1):
        viable_s = []
        for execution in range(delay, CHUNK_STEPS - delay + 1):
            max_real_delay = CHUNK_STEPS - execution
            viable_s.append(
                {
                    "s": execution,
                    "committed_prefix_steps": CHUNK_STEPS - execution,
                    "post_response_nominal_steps": execution - delay,
                    "observed_schedule_miss_fraction": float(
                        np.mean(real_steps > max_real_delay)
                    ),
                }
            )
        candidates.append(
            {
                "d": delay,
                "observed_at_or_before_d_fraction": float(
                    np.mean(real_steps <= delay)
                ),
                "observed_later_than_d_fraction": float(
                    np.mean(real_steps > delay)
                ),
                "mean_early_buffer_steps": float(
                    np.maximum(0, delay - real_steps).mean()
                ),
                "viable_s": viable_s,
            }
        )
    return {
        "num_observations": len(ages),
        "age_seconds": {
            "mean": float(np.mean(ages)),
            "p50": _percentile(ages, 50),
            "p95": _percentile(ages, 95),
            "p99": _percentile(ages, 99),
            "max": float(np.max(ages)),
        },
        "real_delay_steps": {
            "p50": _percentile(real_steps.tolist(), 50),
            "p95": _percentile(real_steps.tolist(), 95),
            "p99": _percentile(real_steps.tolist(), 99),
            "max": int(real_steps.max()),
        },
        "coverage_percentile": coverage_percentile,
        "margin_steps": margin_steps,
        "latency_recommended_d": recommended_d,
        "claim_boundary": (
            "Latency selects a d coverage floor and rejects s values that miss "
            "H-s; final d/s still requires fixed-seed RTC trajectory A/B."
        ),
        "candidates": candidates,
    }


def main() -> None:
    args = parse_args()
    ages = _load_ages(args.logs)
    result = {
        "schema": "g2_rtc_schedule_calibration_v1",
        "logs": [str(path) for path in args.logs],
        **build_calibration(
            ages,
            coverage_percentile=args.coverage_percentile,
            margin_steps=args.margin_steps,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    summary = {key: value for key, value in result.items() if key != "candidates"}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
