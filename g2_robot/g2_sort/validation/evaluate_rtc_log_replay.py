#!/usr/bin/env python3
"""Replay RTC timing and raw chunk seams from existing G2 telemetry.

This does not claim to rerun RTC denoising: historical telemetry has complete
action chunks and timing, but not the synchronized request images/state.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, CHUNK_STEPS
from g2_sort.deployment.g2_client.rtc import RTCParameters, conservative_delay_steps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--delay-steps", type=int, default=20)
    parser.add_argument("--execution-horizon", type=int, default=25)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def load_chunks(path: Path) -> list[dict]:
    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") != "action_chunk":
            continue
        actions = np.asarray(record.get("actions"), dtype=np.float64)
        if actions.shape != (CHUNK_STEPS, ACTION_DIM) or not np.all(np.isfinite(actions)):
            raise ValueError(f"{path}: invalid action chunk request={record.get('request_id')}")
        record["_actions"] = actions
        chunks.append(record)
    return chunks


def main() -> None:
    args = parse_args()
    parameters = RTCParameters(
        predicted_delay_steps=args.delay_steps,
        execution_horizon=args.execution_horizon,
    )
    records = []
    all_ages = []
    all_delays = []
    for path in args.logs:
        chunks = load_chunks(path)
        for previous, current in zip(chunks, chunks[1:], strict=False):
            old = previous["_actions"]
            new = current["_actions"]
            age = float(current["observation_age_seconds"])
            real_d = conservative_delay_steps(age)
            all_ages.append(age)
            all_delays.append(real_d)
            feasible = real_d <= parameters.execution_horizon <= CHUNK_STEPS - real_d
            tail = old[parameters.execution_horizon :]
            overlap = min(real_d, len(tail))
            if overlap:
                hard_arm = float(np.abs(new[:overlap, :14] - tail[:overlap, :14]).mean())
                hard_hand = float(np.abs(new[:overlap, 14:] - tail[:overlap, 14:]).mean())
            else:
                hard_arm = hard_hand = 0.0
            switch_index = min(real_d, CHUNK_STEPS - 1)
            old_index = min(parameters.execution_horizon + real_d - 1, CHUNK_STEPS - 1)
            records.append(
                {
                    "log": str(path),
                    "previous_request_id": previous.get("request_id"),
                    "current_request_id": current.get("request_id"),
                    "observation_age_seconds": age,
                    "real_delay_steps": real_d,
                    "paper_schedule_feasible": feasible,
                    "raw_hard_prefix_arm_mae": hard_arm,
                    "raw_hard_prefix_hand_mae": hard_hand,
                    "raw_switch_arm_jump_max": float(
                        np.abs(new[switch_index, :14] - old[old_index, :14]).max()
                    ),
                    "raw_switch_hand_jump_max": float(
                        np.abs(new[switch_index, 14:] - old[old_index, 14:]).max()
                    ),
                }
            )
    if not records:
        raise RuntimeError("no adjacent action chunks found")

    def mean(name: str) -> float:
        return statistics.mean(float(record[name]) for record in records)

    result = {
        "schema": "g2_rtc_historical_log_replay_v1",
        "scope": (
            "timing and raw model-output seams only; synchronized request images, "
            "measured state, and postprocessed dispatch trajectory were not logged"
        ),
        "logs": [str(path) for path in args.logs],
        "rtc_candidate": {
            "horizon": CHUNK_STEPS,
            "predicted_delay_steps": parameters.predicted_delay_steps,
            "execution_horizon": parameters.execution_horizon,
        },
        "num_adjacent_pairs": len(records),
        "observation_age_seconds": {
            "mean": statistics.mean(all_ages),
            "p50": percentile(all_ages, 50),
            "p95": percentile(all_ages, 95),
            "p99": percentile(all_ages, 99),
            "max": max(all_ages),
        },
        "real_delay_steps": {
            "mean": statistics.mean(all_delays),
            "p95": percentile(all_delays, 95),
            "p99": percentile(all_delays, 99),
            "max": max(all_delays),
        },
        "paper_schedule_feasible_fraction": statistics.mean(
            float(record["paper_schedule_feasible"]) for record in records
        ),
        "raw_seam_metrics": {
            name: mean(name)
            for name in (
                "raw_hard_prefix_arm_mae",
                "raw_hard_prefix_hand_mae",
                "raw_switch_arm_jump_max",
                "raw_switch_hand_jump_max",
            )
        },
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
