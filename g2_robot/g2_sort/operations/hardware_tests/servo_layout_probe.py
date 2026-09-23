"""Compare canonical single-34D and dual-17D normal Servo, with one Robot owner."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES, POLICY_JOINT_LIMITS
from g2_sort.operations.hardware_tests.single_side_17d_servo_probe import (
    build_dynamic_side_target, build_motion_targets, MAX_SEND_SECONDS,
    MAX_PREFLIGHT_DRIFT_RAD,
)
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

PERIOD = 0.01


def read_target(robot):
    # Both sides come from the same body/end snapshots.
    body, end = robot.get_joint_states(), robot.get_end_state()
    left_names, left = build_dynamic_side_target(body, end, "left")
    right_names, right = build_dynamic_side_target(body, end, "right")
    raw = left_names + right_names
    names = tuple(name.strip() for name in raw)
    values = np.concatenate((left, right))
    validate_target(names, values)
    return raw, names, values


def validate_target(names, values):
    values = np.asarray(values)
    if len(names) != 34 or len(set(names)) != 34 or set(names) != set(POLICY_JOINT_NAMES):
        raise ValueError("expected exactly the known canonical 34 joints")
    if values.shape != (34,) or not np.all(np.isfinite(values)):
        raise ValueError("expected finite 34D feedback")
    limits = np.asarray([POLICY_JOINT_LIMITS[POLICY_JOINT_NAMES.index(n)] for n in names])
    if np.any(values < limits[:, 0]) or np.any(values > limits[:, 1]):
        raise ValueError("position exceeds official limits; no automatic clipping")
    for prefix in ("hand_l_", "hand_r_"):
        if np.all(values[[i for i, n in enumerate(names) if prefix in n]] == 0):
            raise ValueError("all-zero hand feedback is not a verified hold baseline")


def plan_targets(names, baseline, motion):
    validate_target(names, baseline)
    if motion == "static":
        return np.tile(baseline, (100, 1))
    joints = {"left-index": "idx37_hand_l_index_pip_joint",
              "right-index": "idx77_hand_r_index_pip_joint"}
    if motion not in joints:
        raise ValueError("unsupported motion")
    curve = build_motion_targets("left-index-pip-visible", np.zeros(17))[:, 11]
    targets = np.tile(baseline, (len(curve), 1))
    targets[:, names.index(joints[motion])] += curve
    validate_target(names, targets[np.argmax(curve)])
    return targets


def request_groups(names, command, layout):
    if layout not in ("unified-34", "dual-side-17d"):
        raise ValueError("unsupported layout")
    if len(names) != 34 or np.asarray(command).shape != (34,):
        raise ValueError("layout requires 34 paired names/values")
    # read_target orders left arm+hand, then right arm+hand, for both layouts.
    slices = (slice(None),) if layout == "unified-34" else (slice(0, 17), slice(17, 34))
    return [(names[s], command[s]) for s in slices]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=("unified-34", "dual-side-17d"), default="unified-34")
    parser.add_argument("--motion", choices=("static", "left-index", "right-index"), default="static")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    import agibot_gdk as gdk
    report = dict(schema_version=1, layout=args.layout, motion=args.motion,
                  execute=args.execute, channel="normal", period_seconds=PERIOD,
                  commands_sent=0, dispatch=[], feedback=[], result="NOT_STARTED")
    initialized = False
    try:
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError("gdk_init failed")
        initialized = True
        robot = gdk.Robot()
        time.sleep(5)

        def check_health():
            whole, motion = robot.get_whole_body_status(), robot.get_motion_control_status()
            if any(whole.get(f"{s}_end_model") != "o10_t2" or whole.get(f"{s}_end_error") != 0 for s in ("left", "right")):
                raise RuntimeError("unhealthy O10 state")
            if getattr(motion, "mode", -1) != 5 or getattr(motion, "error_code", -1) != 0:
                raise RuntimeError("unhealthy motion control state")

        check_health()
        samples = []
        original_raw = None
        for _ in range(5):
            raw, names, baseline = read_target(robot)
            if original_raw is not None and raw != original_raw:
                raise RuntimeError("feedback names changed")
            original_raw = raw
            samples.append(baseline)
            time.sleep(0.05)
        drift = float(np.max(np.ptp(samples, axis=0)))
        if drift > MAX_PREFLIGHT_DRIFT_RAD:
            raise RuntimeError("pose is not static")
        targets = plan_targets(names, baseline, args.motion)
        report.update(raw_names=raw, send_names=names, baseline_rad=baseline.tolist(),
                      preflight_drift_rad=drift, planned_ticks=len(targets))
        print(f"{args.layout}, normal, {args.motion}, ticks={len(targets)}")
        print(f"send_names={names!r}")
        if not args.execute:
            report["result"] = "PREVIEW_ONLY"
            return 0
        description = "current-position hold for 1 second" if args.motion == "static" else f"{args.motion} +0.20rad out-and-back for 4 seconds; other axes held"
        confirm_ok_and_countdown(f"{args.layout}: {description}")
        check_health()
        fresh_raw, fresh_names, fresh = read_target(robot)
        if fresh_raw != raw or fresh_names != names or np.max(np.abs(fresh - baseline)) > MAX_PREFLIGHT_DRIFT_RAD:
            raise RuntimeError("pose/names changed during confirmation; rerun")
        origin = time.monotonic()
        report["started_at_unix"] = time.time()
        for tick, command in enumerate(targets):
            scheduled = origin + tick * PERIOD
            time.sleep(max(0, scheduled - time.monotonic()))
            if time.monotonic() - scheduled > 0.02:
                raise RuntimeError("schedule late by more than 20ms; stop without catch-up")
            tick_record = dict(tick=tick, calls=[])
            report["dispatch"].append(tick_record)
            for group_names, positions in request_groups(names, command, args.layout):
                req = gdk.JointServoControlReq()
                req.control_period = PERIOD
                req.joint_names = list(group_names)
                req.joint_positions = positions.tolist()
                req.joint_velocities = []
                started = time.monotonic()
                result = robot.joint_servo_control(req, enable_low_latency=False)
                elapsed = time.monotonic() - started
                report["commands_sent"] += 1
                tick_record["calls"].append(dict(offset_seconds=started-origin, duration_seconds=elapsed, result=int(result)))
                if result != 0 or elapsed > MAX_SEND_SECONDS:
                    raise RuntimeError(f"Servo result={result}, duration={elapsed:.6f}s")
            if tick % 10 == 0 or tick == len(targets)-1:
                started = time.monotonic()
                feedback_raw, feedback_names, feedback = read_target(robot)
                if feedback_raw != raw or feedback_names != names:
                    raise RuntimeError("feedback names changed during execution")
                report["feedback"].append(dict(tick=tick, at_unix=time.time(),
                    read_seconds=time.monotonic()-started, command_rad=command.tolist(), positions_rad=feedback.tolist()))
            if time.monotonic() > scheduled + PERIOD + 0.02:
                raise RuntimeError("tick exceeded schedule budget")
        report["result"] = "COMPLETED_PENDING_MAPPING_REVIEW"
        print(report["result"])
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report.update(result="STOPPED", error=f"{type(exc).__name__}: {exc}")
        print(f"STOP: {report['error']}; no automatic return/reset")
        return 2
    finally:
        report["finished_at_unix"] = time.time()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        print(f"report={args.report}")
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    raise SystemExit(main())
