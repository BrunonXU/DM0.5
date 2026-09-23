#!/usr/bin/env python3
"""Two-request RTC HTTP smoke using an archived three-camera observation."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import requests


PROMPT = (
    "Use the left hand to pick up each bottle, hand it to the right hand, then "
    "place the Sprite bottle in the left tray compartment and the orange juice "
    "bottle in the right tray compartment."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:7893/process_frame")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--delay-steps", type=int, default=20)
    parser.add_argument("--execution-horizon", type=int, default=25)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def post(endpoint: str, archive: Path, state: list[float], extra: dict) -> tuple[dict, float]:
    files = []
    for name in ("head", "left_wrist", "right_wrist"):
        path = archive / f"{name}.jpg"
        files.append(("image", (path.name, path.read_bytes(), "image/jpeg")))
    data = {
        "states": json.dumps(state, separators=(",", ":")),
        "text": PROMPT,
        **extra,
    }
    started = time.perf_counter()
    response = requests.post(endpoint, files=files, data=data, timeout=15)
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return response.json(), elapsed


def main() -> None:
    args = parse_args()
    preflight = json.loads((args.archive / "preflight.json").read_text(encoding="utf-8"))
    state = preflight["state"]["values"]
    if len(state) != 34:
        raise ValueError("preflight state is not 34D")
    baseline, baseline_seconds = post(args.endpoint, args.archive, state, {})
    baseline_actions = np.asarray(baseline["response"], dtype=np.float64)
    if baseline_actions.shape != (50, 34):
        raise ValueError(f"baseline HTTP response is {baseline_actions.shape}")
    prefix = baseline_actions[args.execution_horizon :]
    guided, guided_seconds = post(
        args.endpoint,
        args.archive,
        state,
        {
            "rtc_prefix_actions": json.dumps(prefix.tolist(), separators=(",", ":")),
            "rtc_inference_delay_steps": str(args.delay_steps),
            "rtc_execution_horizon": str(args.execution_horizon),
        },
    )
    guided_actions = np.asarray(guided["response"], dtype=np.float64)
    if guided_actions.shape != (50, 34):
        raise ValueError(f"RTC HTTP response is {guided_actions.shape}")
    d = args.delay_steps
    result = {
        "schema": "NewLora_rtc_http_smoke_v1",
        "endpoint": args.endpoint,
        "archive": str(args.archive),
        "delay_steps": d,
        "execution_horizon": args.execution_horizon,
        "baseline_seconds": baseline_seconds,
        "rtc_seconds": guided_seconds,
        "hard_prefix_arm_mae": float(
            np.abs(guided_actions[:d, :14] - prefix[:d, :14]).mean()
        ),
        "hard_prefix_hand_mae": float(
            np.abs(guided_actions[:d, 14:] - prefix[:d, 14:]).mean()
        ),
        "switch_arm_max": float(
            np.abs(guided_actions[d, :14] - prefix[d, :14]).max()
        ),
        "switch_hand_max": float(
            np.abs(guided_actions[d, 14:] - prefix[d, 14:]).max()
        ),
        "baseline_server_timing": baseline.get("timing_seconds", {}),
        "rtc_server_timing": guided.get("timing_seconds", {}),
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
