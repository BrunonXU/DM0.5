#!/usr/bin/env python3
"""Two-request RTC binary WebSocket smoke using an archived G2 observation."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from g2_sort.transport.dm05_binary import (
    BinaryRequest,
    EncodedImage,
    decode_response,
    encode_request,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="ws://127.0.0.1:7896/process_frame")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--image-set", choices=("current", "native"), default="native")
    parser.add_argument("--delay-steps", type=int, default=20)
    parser.add_argument("--execution-horizon", type=int, default=25)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    preflight = json.loads((args.archive / "preflight.json").read_text())
    state = np.asarray(preflight["state"]["values"], dtype=np.float32)
    if state.shape != (34,):
        raise ValueError(f"preflight state is not 34D: {state.shape}")
    image_root = args.archive if args.image_set == "current" else args.archive / "native"
    images = tuple(
        EncodedImage((image_root / f"{name}.jpg").read_bytes(), "image/jpeg")
        for name in ("head", "left_wrist", "right_wrist")
    )

    from websockets.sync.client import connect

    connection = connect(
        args.endpoint,
        compression=None,
        open_timeout=10,
        close_timeout=1,
        max_size=32 * 1024 * 1024,
    )

    def send(request: BinaryRequest, timeout: float) -> tuple[np.ndarray, dict, float]:
        payload = encode_request(request)
        started = time.perf_counter()
        connection.send(payload)
        response_payload = connection.recv(timeout=timeout)
        elapsed = time.perf_counter() - started
        if not isinstance(response_payload, bytes):
            raise RuntimeError("WebSocket response was not binary")
        response = decode_response(response_payload)
        if response.request_id != request.request_id:
            raise RuntimeError("WebSocket response request_id mismatch")
        return response.actions, response.timing_seconds, elapsed

    try:
        baseline, baseline_timing, baseline_seconds = send(
            BinaryRequest(request_id=1, state=state, images=images),
            120.0,
        )
        prefix = baseline[args.execution_horizon :]
        guided, guided_timing, guided_seconds = send(
            BinaryRequest(
                request_id=2,
                state=state,
                images=images,
                rtc_prefix=prefix,
                rtc_delay=args.delay_steps,
                rtc_execution=args.execution_horizon,
            ),
            120.0,
        )
    finally:
        connection.close()

    if baseline.shape != (50, 34) or guided.shape != (50, 34):
        raise ValueError(
            f"invalid action shapes: baseline={baseline.shape}, rtc={guided.shape}"
        )
    d = args.delay_steps
    result = {
        "schema": "NewLora_rtc_websocket_smoke_v1",
        "endpoint": args.endpoint,
        "archive": str(args.archive),
        "image_set": args.image_set,
        "delay_steps": d,
        "execution_horizon": args.execution_horizon,
        "baseline_seconds": baseline_seconds,
        "rtc_seconds": guided_seconds,
        "hard_prefix_arm_mae": float(np.abs(guided[:d, :14] - prefix[:d, :14]).mean()),
        "hard_prefix_hand_mae": float(np.abs(guided[:d, 14:] - prefix[:d, 14:]).mean()),
        "switch_arm_max": float(np.abs(guided[d, :14] - prefix[d, :14]).max()),
        "switch_hand_max": float(np.abs(guided[d, 14:] - prefix[d, 14:]).max()),
        "baseline_server_timing": baseline_timing,
        "rtc_server_timing": guided_timing,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
