#!/usr/bin/env python3
"""Replay changing G2 states against an isolated compile-enabled HTTP server."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import requests

from g2_sort.validation.smoke_NewLora_rtc_http import PROMPT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--telemetry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--requests", type=int, default=45)
    return parser.parse_args()


def _states_from_telemetry(path: Path) -> list[list[float]]:
    states = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("event") != "action_chunk":
                continue
            actions = np.asarray(record["actions"], dtype=np.float32)
            index = min(int(record.get("used_intervals", 0)), actions.shape[0] - 1)
            state = actions[index]
            if state.shape != (34,) or not np.isfinite(state).all():
                raise ValueError("telemetry action-derived state must be finite 34D")
            states.append(state.tolist())
    if not states:
        raise ValueError("telemetry contains no action_chunk states")
    return states


def main() -> None:
    args = parse_args()
    states = _states_from_telemetry(args.telemetry)
    images = [
        (args.archive / f"{name}.jpg").read_bytes()
        for name in ("head", "left_wrist", "right_wrist")
    ]
    session = requests.Session()
    records = []
    for index in range(args.requests):
        state = states[index % len(states)]
        files = [
            ("image", (f"camera{camera}.jpg", payload, "image/jpeg"))
            for camera, payload in enumerate(images)
        ]
        started = time.perf_counter()
        response = session.post(
            args.endpoint,
            files=files,
            data={"states": json.dumps(state, separators=(",", ":")), "text": PROMPT},
            timeout=60.0,
        )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        body = response.json()
        actions = np.asarray(body["response"], dtype=np.float32)
        if actions.shape != (50, 34) or not np.isfinite(actions).all():
            raise RuntimeError(f"invalid response shape or values: {actions.shape}")
        timing = body.get("timing_seconds", {})
        records.append(
            {
                "request": index,
                "elapsed_seconds": elapsed,
                "server": timing,
            }
        )
        print(
            f"request={index + 1} elapsed={elapsed:.3f}s "
            f"prefix={timing.get('server_model_prefix_tokens')} "
            f"valid={timing.get('server_model_valid_prefix_tokens')}",
            flush=True,
        )

    result = {
        "schema": "NewLora_compile_dynamic_state_http_v1",
        "endpoint": args.endpoint,
        "archive": str(args.archive),
        "telemetry": str(args.telemetry),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
