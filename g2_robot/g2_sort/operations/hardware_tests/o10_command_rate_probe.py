"""Measure accepted O10 ``move_ee_pos`` rates with a fixed, verified target.

The operator must run this powered diagnostic locally on G2.  It repeats the
last successful dual-hand target, so it measures command acceptance without
intentionally changing pose.  Any feedback mismatch or non-zero result aborts.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_HAND_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.operations.hardware_tests.o10_move_ee_smoke import (
    build_move_ee_request,
    read_o10_positions,
)
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return float(ordered[index])


def _load_target(path: Path) -> np.ndarray:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "last_success":
        raise RuntimeError("target file is not a recorded last_success")
    if payload.get("group") != "dual_tool" or payload.get("target_type") != "o10_t2":
        raise RuntimeError("target file is not an O10 dual_tool command")
    target = np.asarray(payload.get("positions"), dtype=np.float64)
    if target.shape != (20,) or not np.all(np.isfinite(target)):
        raise RuntimeError("target file must contain 20 finite positions")
    return target


def _stage(robot: Any, request: Any, rate_hz: float, seconds: float) -> dict[str, object]:
    period = 1.0 / rate_hz
    deadline = time.monotonic() + seconds
    next_send = time.monotonic()
    starts: list[float] = []
    durations: list[float] = []
    while time.monotonic() < deadline:
        delay = next_send - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        started = time.monotonic()
        result = robot.move_ee_pos(request)
        finished = time.monotonic()
        if result != 0:
            raise RuntimeError(f"{rate_hz:g}Hz returned status {result}")
        starts.append(started)
        durations.append(finished - started)
        next_send += period
    intervals = [b - a for a, b in zip(starts, starts[1:])]
    elapsed = starts[-1] - starts[0] if len(starts) > 1 else 0.0
    actual_hz = (len(starts) - 1) / elapsed if elapsed > 0 else 0.0
    return {
        "requested_hz": rate_hz,
        "commands": len(starts),
        "actual_hz": actual_hz,
        "call_seconds_p50": statistics.median(durations),
        "call_seconds_p95": _percentile(durations, 0.95),
        "call_seconds_max": max(durations),
        "interval_seconds_p95": _percentile(intervals, 0.95) if intervals else None,
        "deadline_miss_count": sum(value > period * 1.10 for value in intervals),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("logs/runtime/last_hand_command.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds-per-rate", type=float, default=2.0)
    parser.add_argument("--rates", type=float, nargs="+", default=[5, 10, 20, 50, 100, 200])
    parser.add_argument("--max-anchor-error-rad", type=float, default=0.08)
    args = parser.parse_args(argv)
    if not 1.0 <= args.seconds_per_rate <= 10.0:
        parser.error("--seconds-per-rate must be between 1 and 10")
    if any(rate <= 0 or rate > 200 for rate in args.rates):
        parser.error("rates must be in (0, 200]")

    import agibot_gdk as gdk

    target = _load_target(args.target)
    report: dict[str, object] = {
        "target": str(args.target),
        "rates": args.rates,
        "stages": [],
        "commands_sent": 0,
    }
    initialized = False
    try:
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError("gdk_init failed")
        initialized = True
        robot = gdk.Robot()
        time.sleep(3.0)
        payload = robot.get_end_state()
        if not isinstance(payload, Mapping):
            raise RuntimeError("get_end_state returned invalid payload")
        feedback = np.concatenate(
            (read_o10_positions(payload, "left"), read_o10_positions(payload, "right"))
        )
        anchor_deltas = np.abs(feedback - target)
        anchor_index = int(np.argmax(anchor_deltas))
        anchor_error = float(anchor_deltas[anchor_index])
        report["anchor_error_rad"] = anchor_error
        report["anchor_error_joint"] = (
            LEFT_HAND_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
        )[anchor_index]
        report["feedback_positions"] = feedback.tolist()
        report["anchor_absolute_deltas"] = anchor_deltas.tolist()
        if anchor_error > args.max_anchor_error_rad:
            raise RuntimeError(
                f"saved target differs from feedback by {anchor_error:.6f}rad; abort"
            )
        request, _, _ = build_move_ee_request(
            gdk,
            side="dual",
            left_positions=target[:10],
            right_positions=target[10:],
        )
        confirm_ok_and_countdown(
            "O10固定姿态5/10/20/50/100/200Hz接受率测试",
            supplied=args.confirm,
        )
        for rate in args.rates:
            print(f"测试 {rate:g} Hz……", flush=True)
            stage = _stage(robot, request, rate, args.seconds_per_rate)
            report["stages"].append(stage)
            report["commands_sent"] += int(stage["commands"])
            time.sleep(0.5)
        report["result"] = "PASS"
        return_code = 0
    except Exception as exc:
        report["result"] = "ABORTED"
        report["error"] = repr(exc)
        return_code = 1
    finally:
        report["finished_at_unix"] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if initialized:
            gdk.gdk_release()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
