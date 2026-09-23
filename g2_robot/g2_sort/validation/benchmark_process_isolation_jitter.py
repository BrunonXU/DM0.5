#!/usr/bin/env python3
"""Offline A/B of 100 Hz tick jitter during image/JPEG/HTTP policy work.

This benchmark does not use GDK and cannot certify real-robot 100 Hz.  It runs
the same mock camera and HTTP backend first in the Servo process and then in a
spawned policy process, producing a JSON gate that isolates Python-process
interference from model and robot effects.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, CHUNK_STEPS
from g2_sort.deployment.g2_client.dm05_backend import DM05HTTPBackend
from g2_sort.deployment.g2_client.mock import MockCameraRig
from g2_sort.deployment.g2_client.observation import ObservationSource
from g2_sort.deployment.g2_client.process_pipeline import (
    PolicyWorkerConfig,
    ProcessInferencePipeline,
)
from g2_sort.deployment.g2_client.servo import ServoStreamer


class _StateSource:
    def __init__(self) -> None:
        self.read_count = 0

    def read_state(self) -> np.ndarray:
        self.read_count += 1
        return np.zeros(ACTION_DIM, dtype=np.float32)


class _PolicyHandler(BaseHTTPRequestHandler):
    response_payload = b""
    delay_seconds = 0.0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if type(self).delay_seconds > 0:
            time.sleep(type(self).delay_seconds)
        payload = type(self).response_payload
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


def _interval_summary(send_times: list[float]) -> dict[str, float | int]:
    intervals = np.diff(np.asarray(send_times, dtype=np.float64))
    if len(intervals) == 0:
        raise RuntimeError("Servo benchmark produced no intervals")
    return {
        "send_count": len(send_times),
        "mean_hz": float(1.0 / np.mean(intervals)),
        "interval_p50_ms": float(np.percentile(intervals, 50) * 1000.0),
        "interval_p95_ms": float(np.percentile(intervals, 95) * 1000.0),
        "interval_p99_ms": float(np.percentile(intervals, 99) * 1000.0),
        "interval_max_ms": float(np.max(intervals) * 1000.0),
        "interval_over_15ms_count": int(np.count_nonzero(intervals > 0.015)),
        "interval_under_5ms_count": int(np.count_nonzero(intervals < 0.005)),
    }


def _measure(
    workload: Callable[[], None],
    *,
    request_count: int,
) -> dict[str, float | int]:
    send_times: list[float] = []

    def sender(_target: np.ndarray) -> bool:
        send_times.append(time.monotonic())
        return True

    servo = ServoStreamer(
        sender,
        np.zeros(ACTION_DIM, dtype=np.float64),
        servo_hz=100.0,
        max_tick_lateness_seconds=1.0,
    )
    servo.start()
    started_at = time.monotonic()
    try:
        for _ in range(request_count):
            workload()
    finally:
        elapsed = time.monotonic() - started_at
        servo.stop()
    result = _interval_summary(send_times)
    result["workload_elapsed_seconds"] = elapsed
    result["request_count"] = request_count
    result["servo_max_tick_lateness_ms"] = (
        float(servo.stats()["max_tick_lateness_seconds"]) * 1000.0
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--image-height", type=int, default=720)
    parser.add_argument("--image-width", type=int, default=1280)
    parser.add_argument("--server-delay-seconds", type=float, default=0.03)
    parser.add_argument("--output-json", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.requests <= 0:
        raise ValueError("requests must be positive")
    _PolicyHandler.delay_seconds = max(0.0, args.server_delay_seconds)
    _PolicyHandler.response_payload = json.dumps(
        {
            "response": np.zeros((CHUNK_STEPS, ACTION_DIM)).tolist(),
            "timing_seconds": {"server_model_seconds": 0.0},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PolicyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/process_frame"
    pipeline: ProcessInferencePipeline | None = None
    backend: DM05HTTPBackend | None = None
    try:
        same_state = _StateSource()
        observations = ObservationSource(
            MockCameraRig(args.image_height, args.image_width),
            same_state,
        )
        backend = DM05HTTPBackend(endpoint, timeout_seconds=5.0)
        backend.infer(observations.capture())
        same_process = _measure(
            lambda: backend.infer(observations.capture()),
            request_count=args.requests,
        )

        isolated_state = _StateSource()
        pipeline = ProcessInferencePipeline(
            PolicyWorkerConfig(
                endpoint=endpoint,
                request_timeout_seconds=5.0,
                camera_timeout_ms=50.0,
                gdk_stabilization_seconds=0.0,
                mock=True,
                mock_image_height=args.image_height,
                mock_image_width=args.image_width,
            ),
            state_source=isolated_state,
            startup_timeout_seconds=10.0,
        )
        pipeline.infer()
        isolated = _measure(
            pipeline.infer,
            request_count=args.requests,
        )
        state_stats = pipeline.control_state_stats()
        result = {
            "scope": "offline_no_gdk",
            "image_shape": [args.image_height, args.image_width, 3],
            "server_delay_seconds": args.server_delay_seconds,
            "same_process": same_process,
            "isolated_process": isolated,
            "isolated_control_state": state_stats,
            "delta": {
                "interval_p95_ms": float(isolated["interval_p95_ms"])
                - float(same_process["interval_p95_ms"]),
                "interval_max_ms": float(isolated["interval_max_ms"])
                - float(same_process["interval_max_ms"]),
                "over_15ms_count": int(isolated["interval_over_15ms_count"])
                - int(same_process["interval_over_15ms_count"]),
            },
            "claim_boundary": (
                "This isolates Python camera/JPEG/HTTP interference only; "
                "G2 GDK and real 100 Hz dispatch still require robot telemetry."
            ),
        }
    finally:
        if pipeline is not None:
            pipeline.close()
        if backend is not None:
            backend.close()
        server.shutdown()
        server_thread.join(timeout=1.0)
        server.server_close()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
