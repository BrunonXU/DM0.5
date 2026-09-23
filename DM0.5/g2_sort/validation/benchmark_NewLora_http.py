#!/usr/bin/env python3
"""Benchmark a non-RTC NewLora HTTP endpoint using archived G2 inputs."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np
import requests

from g2_sort.validation.smoke_NewLora_rtc_http import PROMPT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
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
    args = parse_args()
    if args.requests < 1 or args.warmup < 1:
        raise ValueError("requests and warmup must be positive")
    state = json.loads((args.archive / "preflight.json").read_text())["state"][
        "values"
    ]
    payloads = [
        (args.archive / f"{name}.jpg").read_bytes()
        for name in ("head", "left_wrist", "right_wrist")
    ]
    session = requests.Session()

    def send(timeout: float) -> tuple[float, dict[str, float]]:
        files = [
            ("image", (f"camera{index}.jpg", payload, "image/jpeg"))
            for index, payload in enumerate(payloads)
        ]
        started = time.perf_counter()
        response = session.post(
            args.endpoint,
            files=files,
            data={"states": json.dumps(state, separators=(",", ":")), "text": PROMPT},
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        body = response.json()
        actions = np.asarray(body["response"], dtype=np.float64)
        if actions.shape != (50, 34) or not np.isfinite(actions).all():
            raise RuntimeError(f"invalid response action shape/values: {actions.shape}")
        timing = {
            str(key): float(value)
            for key, value in body.get("timing_seconds", {}).items()
        }
        try:
            handler_seconds = float(
                response.headers.get("X-DM05-Handler-Seconds", "nan")
            )
        except (TypeError, ValueError):
            handler_seconds = float("nan")
        if np.isfinite(handler_seconds) and handler_seconds >= 0:
            timing["server_handler_seconds"] = handler_seconds
        return elapsed, timing

    warmups = [send(60.0)[0] for _ in range(args.warmup)]
    records = []
    for index in range(args.requests):
        elapsed, timing = send(10.0)
        model = timing.get("server_model_and_device_to_cpu_seconds")
        handler = timing.get("server_handler_seconds")
        ledger: dict[str, float | bool] = {}
        if model is not None:
            ledger["client_non_model_seconds"] = max(0.0, elapsed - model)
        if handler is not None:
            ledger["transport_and_client_response_seconds"] = max(
                0.0, elapsed - handler
            )
        if model is not None and handler is not None:
            server_non_model = max(0.0, handler - model)
            transport = max(0.0, elapsed - handler)
            ledger.update(
                {
                    "server_non_model_seconds": server_non_model,
                    "reconciled_non_model_seconds": server_non_model + transport,
                    "non_model_reconciliation_residual_seconds": max(
                        0.0,
                        elapsed - model - server_non_model - transport,
                    ),
                    "complete": True,
                }
            )
        else:
            ledger["complete"] = False
        records.append(
            {
                "request": index,
                "elapsed_seconds": elapsed,
                "server": timing,
                "ledger": ledger,
            }
        )
        print(f"request={index + 1} elapsed={elapsed:.3f}s")

    timing_keys = sorted(
        {key for record in records for key in record["server"]}
    )
    ledger_keys = sorted(
        {
            key
            for record in records
            for key, value in record["ledger"].items()
            if key != "complete" and isinstance(value, float)
        }
    )
    result = {
        "schema": "NewLora_http_benchmark_v2",
        "endpoint": args.endpoint,
        "archive": str(args.archive),
        "warmup_elapsed_seconds": warmups,
        "requests": args.requests,
        "client_elapsed_seconds": _summary(
            [record["elapsed_seconds"] for record in records]
        ),
        "server_timing_seconds": {
            key: _summary(
                [record["server"][key] for record in records if key in record["server"]]
            )
            for key in timing_keys
        },
        "non_model_ledger_seconds": {
            key: _summary(
                [
                    float(record["ledger"][key])
                    for record in records
                    if key in record["ledger"]
                ]
            )
            for key in ledger_keys
        },
        "complete_ledger_requests": sum(
            bool(record["ledger"].get("complete")) for record in records
        ),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
