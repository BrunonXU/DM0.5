#!/usr/bin/env python3
"""Read-only single-Robot-owner process-isolation preflight.

The parent is the sole Robot state owner.  The spawned worker owns Camera and
HTTP only; after image capture it requests one timestamped 34D snapshot through
the shared state ring.  No ServoStreamer or robot command is created.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ActionChunk
from g2_sort.deployment.g2_client.process_pipeline import (
    PolicyWorkerConfig,
    ProcessInferencePipeline,
)
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader


class _StrictStateSource:
    def __init__(self, reader: StrictRobotStateReader) -> None:
        self._reader = reader

    def read_state(self) -> np.ndarray:
        return self._reader.read()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-endpoint", required=True)
    parser.add_argument(
        "--policy-transport",
        choices=("http-multipart-json", "websocket-binary"),
        default="http-multipart-json",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--camera-timeout-ms", type=float, default=200.0)
    parser.add_argument("--gdk-stabilization-seconds", type=float, default=3.0)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = args.output_json
    output.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "ok": False,
        "wall_time": time.time(),
        "parent_pid": os.getpid(),
        "servo_created": False,
        "robot_commands_sent": 0,
        "robot_owner": "control_process",
        "policy_robot_reader": False,
    }
    pipeline: ProcessInferencePipeline | None = None
    gdk = None
    gdk_initialized = False
    try:
        import agibot_gdk as gdk

        init_result = gdk.gdk_init()
        if init_result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"preflight parent gdk_init failed: {init_result}")
        gdk_initialized = True
        parent_reader = StrictRobotStateReader(gdk.Robot())
        native_log_dir = Path(
            os.environ.get(
                "GLOG_log_dir",
                Path(__file__).resolve().parents[3] / "logs" / "gdk_native",
            )
        )
        pipeline = ProcessInferencePipeline(
            PolicyWorkerConfig(
                endpoint=args.policy_endpoint,
                request_timeout_seconds=args.request_timeout_seconds,
                camera_timeout_ms=args.camera_timeout_ms,
                gdk_stabilization_seconds=args.gdk_stabilization_seconds,
                transport=args.policy_transport,
                native_log_dir=str(native_log_dir),
            ),
            state_source=_StrictStateSource(parent_reader),
        )
        started_at = time.monotonic()
        chunk: ActionChunk = pipeline.infer()
        state_stats = pipeline.control_state_stats()
        if int(state_stats["read_count"]) != 1:
            raise RuntimeError("preflight expected exactly one Control state read")
        result.update(
            {
                "ok": True,
                "worker_pid": pipeline.worker_pid,
                "control_state_reads_during_inference": state_stats["read_count"],
                "control_state_read_max_seconds": state_stats["read_max_seconds"],
                "state_ring_latest_sequence": state_stats[
                    "latest_ring_sequence"
                ],
                "state_ring_capacity": state_stats["ring_capacity"],
                "request_id": chunk.request_id,
                "inference_latency_seconds": chunk.latency_seconds,
                "observation_to_response_seconds": (
                    chunk.response_received_at - chunk.observation_at
                ),
                "elapsed_seconds": time.monotonic() - started_at,
            }
        )
    except BaseException as exc:
        result.update(
            {
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        )
    finally:
        if pipeline is not None:
            pipeline.close()
        if gdk_initialized and gdk is not None:
            try:
                gdk.gdk_release()
            except Exception as exc:
                result["gdk_release_error"] = str(exc)
                result["ok"] = False
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
