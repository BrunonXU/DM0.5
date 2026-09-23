#!/usr/bin/env python3
"""Benchmark matched HTTP or binary WebSocket endpoints with archived G2 inputs."""

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
from g2_sort.validation.smoke_NewLora_rtc_http import PROMPT


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("http", "websocket"), required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--image-set", choices=("current", "native"), required=True)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _summary(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(array.mean()),
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "p99": float(np.percentile(array, 99)),
        "max": float(array.max()),
    }


def main() -> None:
    args = _args()
    if args.requests < 1 or args.warmup < 1:
        raise ValueError("requests and warmup must be positive")
    preflight = json.loads((args.archive / "preflight.json").read_text())
    state = np.asarray(preflight["state"]["values"], dtype=np.float32)
    image_root = args.archive if args.image_set == "current" else args.archive / "native"
    names = ("head", "left_wrist", "right_wrist")
    payloads = tuple((image_root / f"{name}.jpg").read_bytes() for name in names)
    records: list[dict[str, object]] = []

    if args.transport == "http":
        import requests

        session = requests.Session()

        def send(request_id: int, timeout: float) -> tuple[float, int, int, dict[str, float]]:
            files = [
                ("image", (f"camera{index}.jpg", payload, "image/jpeg"))
                for index, payload in enumerate(payloads)
            ]
            started = time.perf_counter()
            response = session.post(
                args.endpoint,
                files=files,
                data={
                    "states": json.dumps(state.tolist(), separators=(",", ":")),
                    "text": PROMPT,
                },
                timeout=timeout,
            )
            elapsed = time.perf_counter() - started
            response.raise_for_status()
            body = response.json()
            actions = np.asarray(body["response"], dtype=np.float64)
            if actions.shape != (50, 34) or not np.isfinite(actions).all():
                raise RuntimeError(f"invalid HTTP action response: {actions.shape}")
            timing = {str(k): float(v) for k, v in body.get("timing_seconds", {}).items()}
            header = response.headers.get("X-DM05-Handler-Seconds")
            if header is not None:
                timing["server_handler_seconds"] = float(header)
            request_bytes = sum(len(payload) for payload in payloads)
            return elapsed, request_bytes, len(response.content), timing

        close = session.close
    else:
        from websockets.sync.client import connect

        connection = connect(
            args.endpoint,
            compression=None,
            open_timeout=10,
            close_timeout=1,
            max_size=32 * 1024 * 1024,
        )

        def send(request_id: int, timeout: float) -> tuple[float, int, int, dict[str, float]]:
            request = encode_request(
                BinaryRequest(
                    request_id=request_id,
                    state=state,
                    images=tuple(
                        EncodedImage(payload, "image/jpeg") for payload in payloads
                    ),  # type: ignore[arg-type]
                )
            )
            started = time.perf_counter()
            connection.send(request)
            response_payload = connection.recv(timeout=timeout)
            elapsed = time.perf_counter() - started
            if not isinstance(response_payload, bytes):
                raise RuntimeError("WebSocket response was not binary")
            response = decode_response(response_payload)
            if response.request_id != request_id:
                raise RuntimeError("WebSocket request_id mismatch")
            return elapsed, len(request), len(response_payload), response.timing_seconds

        close = connection.close

    try:
        warmups = [
            send(10_000_000 + index, 120.0)[0] for index in range(args.warmup)
        ]
        for index in range(args.requests):
            elapsed, request_bytes, response_bytes, timing = send(index + 1, 15.0)
            records.append(
                {
                    "request": index + 1,
                    "elapsed_seconds": elapsed,
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "server": timing,
                }
            )
            print(f"request={index + 1}/{args.requests} elapsed={elapsed:.3f}s", flush=True)
    finally:
        close()

    timing_keys = sorted({key for record in records for key in record["server"]})
    result = {
        "schema": "NewLora_transport_benchmark_v1",
        "transport": args.transport,
        "endpoint": args.endpoint,
        "archive": str(args.archive),
        "image_set": args.image_set,
        "image_payload_bytes": sum(len(payload) for payload in payloads),
        "warmup_elapsed_seconds": warmups,
        "requests": args.requests,
        "client_elapsed_seconds": _summary(
            [float(record["elapsed_seconds"]) for record in records]
        ),
        "request_bytes": _summary([float(record["request_bytes"]) for record in records]),
        "response_bytes": _summary([float(record["response_bytes"]) for record in records]),
        "server_timing_seconds": {
            key: _summary(
                [
                    float(record["server"][key])
                    for record in records
                    if key in record["server"]
                ]
            )
            for key in timing_keys
        },
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
