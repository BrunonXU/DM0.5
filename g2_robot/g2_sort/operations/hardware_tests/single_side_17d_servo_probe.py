"""GDK 2.6.3单侧7D手臂+动态10D末端Servo最小探针。

默认只读预览。执行模式仅以normal通道把新鲜反馈的当前位置保持1秒；
不使用冻结O10名称、不发送模型轨迹，也不控制另一侧。
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    POLICY_JOINT_LIMITS,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

CONTROL_PERIOD_SECONDS = 0.01
HOLD_DURATION_SECONDS = 1.0
MICRO_MOTION_DURATION_SECONDS = 2.0
MICRO_MOTION_AMPLITUDE_RAD = 0.05
VISIBLE_MOTION_DURATION_SECONDS = 4.0
VISIBLE_MOTION_AMPLITUDE_RAD = 0.20
LEFT_INDEX_PIP_17D_INDEX = 11
STABILITY_SAMPLES = 5
STABILITY_INTERVAL_SECONDS = 0.05
MAX_PREFLIGHT_DRIFT_RAD = 0.01
MAX_SEND_SECONDS = 0.03


def _finite_position(entry: Mapping[str, Any], name: str, *, arm: bool) -> float:
    keys = ("motor_position", "position") if arm else ("position",)
    for key in keys:
        if entry.get(key) is not None:
            value = float(entry[key])
            if np.isfinite(value):
                return value
    raise ValueError(f"joint {name!r} has no finite measured position")


def build_dynamic_side_target(
    body_payload: Mapping[str, Any],
    end_payload: Mapping[str, Any],
    side: str,
) -> tuple[tuple[str, ...], np.ndarray]:
    """Build 17D target while preserving exact GDK-returned end names."""
    if side not in {"left", "right"}:
        raise ValueError(f"unsupported side {side!r}")
    arm_names = LEFT_ARM_JOINT_NAMES if side == "left" else RIGHT_ARM_JOINT_NAMES
    expected_end_names = (
        LEFT_HAND_JOINT_NAMES if side == "left" else RIGHT_HAND_JOINT_NAMES
    )
    body_states = body_payload.get("states")
    if not isinstance(body_states, Sequence) or isinstance(body_states, (str, bytes)):
        raise ValueError("get_joint_states payload has no states sequence")
    body_map: dict[str, Mapping[str, Any]] = {}
    for entry in body_states:
        if not isinstance(entry, Mapping):
            raise ValueError("body state contains a non-mapping entry")
        name = str(entry.get("name", "")).strip()
        if not name or name in body_map:
            raise ValueError(f"invalid or duplicate body joint name {name!r}")
        body_map[name] = entry
    missing_arms = [name for name in arm_names if name not in body_map]
    if missing_arms:
        raise ValueError(f"body state missing arm joints: {missing_arms}")

    side_payload = end_payload.get(f"{side}_end_state")
    if not isinstance(side_payload, Mapping):
        raise ValueError(f"{side} end state is missing")
    raw_names = side_payload.get("names")
    end_states = side_payload.get("end_states")
    if (
        not isinstance(raw_names, Sequence)
        or isinstance(raw_names, (str, bytes))
        or not isinstance(end_states, Sequence)
        or isinstance(end_states, (str, bytes))
        or len(raw_names) != 10
        or len(end_states) != 10
    ):
        raise ValueError(f"{side} end state must contain parallel 10D names/states")
    exact_end_names = tuple(str(name) for name in raw_names)
    normalized_end_names = tuple(name.strip() for name in exact_end_names)
    if len(set(normalized_end_names)) != 10:
        raise ValueError(f"{side} end state contains empty or duplicate names")
    if set(normalized_end_names) != set(expected_end_names):
        raise ValueError(
            f"{side} dynamic end names do not match expected O10 set: "
            f"{normalized_end_names}"
        )
    for name, state in zip(exact_end_names, end_states, strict=True):
        if not isinstance(state, Mapping):
            raise ValueError(f"end state for {name!r} is not a mapping")
        if state.get("enable") is not True:
            raise ValueError(f"end joint {name!r} is not enabled")
        if int(state.get("status", -1)) != 0 or int(state.get("err_code", -1)) != 0:
            raise ValueError(f"end joint {name!r} is unhealthy: {state}")

    arm_positions = [
        _finite_position(body_map[name], name, arm=True) for name in arm_names
    ]
    end_positions = [
        _finite_position(state, name, arm=False)
        for name, state in zip(exact_end_names, end_states, strict=True)
    ]
    positions = np.asarray(arm_positions + end_positions, dtype=np.float64)
    if positions.shape != (17,) or not np.all(np.isfinite(positions)):
        raise ValueError("assembled single-side Servo target is not finite 17D")

    policy_names = (
        LEFT_ARM_JOINT_NAMES + LEFT_HAND_JOINT_NAMES
        if side == "left"
        else RIGHT_ARM_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
    )
    policy_indices = (
        tuple(range(0, 7)) + tuple(range(14, 24))
        if side == "left"
        else tuple(range(7, 14)) + tuple(range(24, 34))
    )
    by_normalized_name = {
        name: value
        for name, value in zip(arm_names + normalized_end_names, positions, strict=True)
    }
    ordered_for_limits = np.asarray(
        [by_normalized_name[name] for name in policy_names], dtype=np.float64
    )
    limits = POLICY_JOINT_LIMITS[np.asarray(policy_indices, dtype=np.intp)]
    if np.any(ordered_for_limits < limits[:, 0] - 0.02) or np.any(
        ordered_for_limits > limits[:, 1] + 0.02
    ):
        raise ValueError("fresh single-side feedback lies outside known limits")
    return arm_names + exact_end_names, positions


def build_servo_request(gdk: Any, names: Sequence[str], positions: np.ndarray) -> Any:
    if len(names) != 17 or np.asarray(positions).shape != (17,):
        raise ValueError("single-side Servo request must be exactly 17D")
    request = gdk.JointServoControlReq()
    request.control_period = CONTROL_PERIOD_SECONDS
    request.joint_names = list(names)
    request.joint_positions = np.asarray(positions, dtype=np.float64).tolist()
    request.joint_velocities = []
    return request


def build_motion_targets(profile: str, baseline: np.ndarray) -> np.ndarray:
    """Return a static hold or one tiny minimum-jerk out-and-back trajectory."""
    values = np.asarray(baseline, dtype=np.float64)
    if values.shape != (17,) or not np.all(np.isfinite(values)):
        raise ValueError("17D motion baseline must be finite")
    if profile == "static":
        count = round(HOLD_DURATION_SECONDS / CONTROL_PERIOD_SECONDS)
        return np.repeat(values[None, :], count, axis=0)
    if profile not in {"left-index-pip-minjerk", "left-index-pip-visible"}:
        raise ValueError(f"unsupported motion profile {profile!r}")
    if profile == "left-index-pip-minjerk":
        count = round(MICRO_MOTION_DURATION_SECONDS / CONTROL_PERIOD_SECONDS) + 1
        times = np.arange(count, dtype=np.float64) * CONTROL_PERIOD_SECONDS
        phase = np.where(times <= 1.0, times, 2.0 - times)
        phase = np.clip(phase, 0.0, 1.0)
        amplitude = MICRO_MOTION_AMPLITUDE_RAD
    else:
        count = round(VISIBLE_MOTION_DURATION_SECONDS / CONTROL_PERIOD_SECONDS) + 1
        times = np.arange(count, dtype=np.float64) * CONTROL_PERIOD_SECONDS
        phase = np.zeros_like(times)
        outbound = times <= 1.0
        peak_hold = (times > 1.0) & (times <= 2.0)
        returning = (times > 2.0) & (times <= 3.0)
        phase[outbound] = times[outbound]
        phase[peak_hold] = 1.0
        phase[returning] = 3.0 - times[returning]
        amplitude = VISIBLE_MOTION_AMPLITUDE_RAD
    phase = np.clip(phase, 0.0, 1.0)
    smooth = 10.0 * phase**3 - 15.0 * phase**4 + 6.0 * phase**5
    targets = np.repeat(values[None, :], count, axis=0)
    targets[:, LEFT_INDEX_PIP_17D_INDEX] += amplitude * smooth
    return targets


RIGHT_PROBE_JOINTS = {
    "index": "idx77_hand_r_index_pip_joint",
    "middle": "idx79_hand_r_middle_pip_joint",
    "ring": "idx82_hand_r_ring_pip_joint",
    "pinky": "idx85_hand_r_pinky_pip_joint",
}


def build_right_finger_candidate(names: Sequence[str], baseline: np.ndarray, finger: str):
    """Canonical send names, retaining the paired feedback/value order."""
    if finger not in RIGHT_PROBE_JOINTS:
        raise ValueError(f"unsupported right finger {finger!r}")
    send_names = tuple(name.strip() for name in names)
    expected = RIGHT_ARM_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
    if len(send_names) != 17 or len(set(send_names)) != 17 or set(send_names) != set(expected):
        raise ValueError("right candidate names must match the known 17D set")
    values = np.asarray(baseline, dtype=np.float64)
    if values.shape != (17,) or not np.all(np.isfinite(values)):
        raise ValueError("right candidate requires finite 17D feedback")
    hand_indices = [send_names.index(name) for name in RIGHT_HAND_JOINT_NAMES]
    if np.all(values[hand_indices] == 0.0):
        raise ValueError("all-zero right hand feedback is not a verified hold baseline")
    joint_index = send_names.index(RIGHT_PROBE_JOINTS[finger])
    curve = build_motion_targets("left-index-pip-visible", np.zeros(17))[:, 11]
    targets = np.repeat(values[None, :], len(curve), axis=0)
    targets[:, joint_index] += curve
    policy_indices = tuple(range(7, 14)) + tuple(range(24, 34))
    limits_by_name = dict(zip(expected, POLICY_JOINT_LIMITS[list(policy_indices)], strict=True))
    limits = np.asarray([limits_by_name[name] for name in send_names])
    if np.any(targets < limits[:, 0]) or np.any(targets > limits[:, 1]):
        raise ValueError("right candidate planned positions exceed official limits")
    return send_names, targets, joint_index


def build_right_index_candidate(names: Sequence[str], baseline: np.ndarray):
    return build_right_finger_candidate(names, baseline, "index")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", choices=("left", "right"), default="left")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--motion-profile",
        choices=("static", "left-index-pip-minjerk", "left-index-pip-visible", "right-index-pip-canonical-visible", "right-finger-pip-canonical-visible"),
        default="static",
    )
    parser.add_argument("--confirm", default="")
    parser.add_argument("--finger", choices=tuple(RIGHT_PROBE_JOINTS), default="index")
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    candidate = args.motion_profile in {"right-index-pip-canonical-visible", "right-finger-pip-canonical-visible"}
    if args.finger != "index" and args.motion_profile != "right-finger-pip-canonical-visible":
        raise ValueError("--finger requires right-finger-pip-canonical-visible profile")
    if candidate and args.side != "right":
        raise ValueError("canonical right index profile requires --side right")
    import agibot_gdk as gdk

    report: dict[str, Any] = {
        "schema_version": 1,
        "side": args.side,
        "execute": args.execute,
        "servo_channel": "normal",
        "control_period_seconds": CONTROL_PERIOD_SECONDS,
        "hold_duration_seconds": HOLD_DURATION_SECONDS,
        "motion_profile": args.motion_profile,
        "selected_finger": args.finger if candidate else None,
        "commands_sent": 0,
    }
    initialized = False
    try:
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError("gdk_init failed")
        initialized = True
        robot = gdk.Robot()
        time.sleep(5.0)
        whole = robot.get_whole_body_status()
        motion = robot.get_motion_control_status()
        if not isinstance(whole, Mapping):
            raise RuntimeError("get_whole_body_status returned non-mapping")
        if (
            whole.get("left_end_model") != "o10_t2"
            or whole.get("right_end_model") != "o10_t2"
            or int(whole.get("left_end_error", -1)) != 0
            or int(whole.get("right_end_error", -1)) != 0
        ):
            raise RuntimeError(f"O10 whole-body state is unhealthy: {whole}")
        if int(getattr(motion, "mode", -1)) != 5 or int(
            getattr(motion, "error_code", -1)
        ) != 0:
            raise RuntimeError(
                f"motion state is unhealthy: mode={getattr(motion, 'mode', None)} "
                f"error={getattr(motion, 'error_code', None)}"
            )

        samples: list[np.ndarray] = []
        names: tuple[str, ...] | None = None
        for index in range(STABILITY_SAMPLES):
            sample_names, positions = build_dynamic_side_target(
                robot.get_joint_states(), robot.get_end_state(), args.side
            )
            if names is not None and sample_names != names:
                raise RuntimeError("dynamic 17D joint names changed during preflight")
            names = sample_names
            samples.append(positions)
            if index + 1 < STABILITY_SAMPLES:
                time.sleep(STABILITY_INTERVAL_SECONDS)
        assert names is not None
        target = samples[-1]
        preflight_drift = float(np.max(np.ptp(np.stack(samples), axis=0)))
        report.update(
            {
                "joint_names_exact": list(names),
                "target_positions_rad": target.tolist(),
                "preflight_drift_rad": preflight_drift,
                "dynamic_end_names_preserved_exactly": True,
            }
        )
        print(f"side={args.side} normal Servo 17D dynamic-name preview")
        for name, value in zip(names, target, strict=True):
            print(f"  {name!r}: {value: .7f}")
        print(f"5-sample max drift={preflight_drift:.7f}rad")
        if preflight_drift > MAX_PREFLIGHT_DRIFT_RAD:
            raise RuntimeError("robot is not static enough for 17D hold probe")
        send_names = names
        motion_index = LEFT_INDEX_PIP_17D_INDEX
        if candidate:
            send_names, motion_targets, motion_index = build_right_finger_candidate(names, target, args.finger)
            report["joint_names_sent"] = list(send_names)
            report["dynamic_end_names_preserved_exactly"] = False
            report["feedback_samples"] = []
            print(f"CANONICAL SEND NAME: {send_names[motion_index]!r}")
        else:
            motion_targets = build_motion_targets(args.motion_profile, target)
        if args.motion_profile in {
            "left-index-pip-minjerk",
            "left-index-pip-visible",
        }:
            index_pip_limits = POLICY_JOINT_LIMITS[18]
            commanded = motion_targets[:, LEFT_INDEX_PIP_17D_INDEX]
            if np.any(commanded < index_pip_limits[0]) or np.any(
                commanded > index_pip_limits[1]
            ):
                raise RuntimeError("planned left index PIP motion exceeds limits")
        report.update(
            {
                "planned_command_count": len(motion_targets),
                "planned_duration_seconds": (
                    (len(motion_targets) - 1) * CONTROL_PERIOD_SECONDS
                    if args.motion_profile != "static"
                    else HOLD_DURATION_SECONDS
                ),
                "planned_max_delta_rad": float(
                    np.max(np.abs(motion_targets - target[None, :]))
                ),
                "micro_motion_joint": (
                    send_names[motion_index]
                    if args.motion_profile != "static"
                    else None
                ),
            }
        )
        if args.motion_profile != "static" and args.side != "left" and not candidate:
            raise RuntimeError("micro-motion profile is locked to the left side")
        if not args.execute:
            report["result"] = "PREVIEW_ONLY"
            print("PREVIEW ONLY: no joint_servo_control command was sent.")
            return 0

        if args.motion_profile == "static":
            action_description = "current-position hold at 100Hz for 1 second"
        elif args.motion_profile == "left-index-pip-minjerk":
            action_description = (
                "left index PIP +0.05rad minimum-jerk and return at 100Hz "
                "for 2 seconds"
            )
        else:
            action_description = (
                f"{args.side} {args.finger if candidate else 'index'} PIP +0.20rad minimum-jerk, 1-second peak hold, "
                "return, and 1-second baseline hold at 100Hz"
            )
        print(
            f"WARNING: next step sends {args.side.upper()} 17D "
            f"{action_description} on NORMAL collision-protected Servo."
        )
        confirm_ok_and_countdown(
            f"{args.side} 17D Servo: {action_description}",
            supplied=args.confirm,
        )
        if candidate:
            fresh_names, fresh = build_dynamic_side_target(
                robot.get_joint_states(), robot.get_end_state(), args.side
            )
            build_right_finger_candidate(fresh_names, fresh, args.finger)
            if fresh_names != names or np.max(np.abs(fresh - target)) > MAX_PREFLIGHT_DRIFT_RAD:
                raise RuntimeError("pose changed during confirmation; rerun from fresh baseline")
        send_durations: list[float] = []
        next_tick = time.monotonic()
        report["started_at_unix"] = time.time()
        for tick, command in enumerate(motion_targets):
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            started = time.monotonic()
            request = build_servo_request(gdk, send_names, command)
            result = robot.joint_servo_control(request, enable_low_latency=False)
            elapsed = time.monotonic() - started
            report["commands_sent"] += 1
            send_durations.append(elapsed)
            if result != 0:
                raise RuntimeError(f"joint_servo_control returned {result}")
            if elapsed > MAX_SEND_SECONDS:
                raise RuntimeError(f"Servo call exceeded watchdog: {elapsed:.6f}s")
            if candidate and tick % 10 == 0:
                read_started = time.monotonic()
                feedback_names, feedback = build_dynamic_side_target(
                    robot.get_joint_states(), robot.get_end_state(), args.side
                )
                if feedback_names != names or np.all(feedback[7:] == 0.0):
                    raise RuntimeError("invalid right hand feedback during motion")
                report["feedback_samples"].append({
                    "tick": tick, "at_unix": time.time(),
                    "read_seconds": time.monotonic() - read_started,
                    "command_rad": command.tolist(), "feedback_rad": feedback.tolist(),
                })
            next_tick += CONTROL_PERIOD_SECONDS
            if time.monotonic() > next_tick + 0.02:
                raise RuntimeError("100Hz Servo schedule missed by more than 20ms")
        time.sleep(0.2)
        after_names, after = build_dynamic_side_target(
            robot.get_joint_states(), robot.get_end_state(), args.side
        )
        if after_names != names:
            raise RuntimeError("dynamic 17D joint names changed after execution")
        drift = np.abs(after - target)
        report.update(
            {
                "send_p95_seconds": float(np.percentile(send_durations, 95)),
                "send_max_seconds": float(np.max(send_durations)),
                "feedback_after_rad": after.tolist(),
                "feedback_max_drift_rad": float(np.max(drift)),
                "feedback_max_drift_joint": names[int(np.argmax(drift))],
                "result": (
                    "COMPLETED_PENDING_MAPPING_REVIEW" if candidate else
                    "PASS_STATIC_17D_SERVO"
                    if args.motion_profile == "static"
                    else "PASS_MICRO_MOTION_17D_SERVO"
                ),
            }
        )
        print(
            f"{report['result']}: sent={report['commands_sent']} "
            f"send_max={report['send_max_seconds']:.6f}s "
            f"feedback_max_drift={report['feedback_max_drift_rad']:.6f}rad"
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"STOP: {report['error']}")
        return 2
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report["finished_at_unix"] = time.time()
        args.report.write_text(
            json.dumps(_jsonable(report), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"report={args.report}")
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    raise SystemExit(main())
