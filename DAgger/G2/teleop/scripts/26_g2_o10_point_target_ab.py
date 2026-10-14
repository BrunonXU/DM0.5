#!/usr/bin/env python3
"""Compare one-shot O10 point motion with 10 Hz v1_1 intermediate targets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import numpy as np

from g2teleop.control_telemetry import ControlTelemetry
from g2teleop.control_timing import MinimumPeriodPacer
from g2teleop.eef_replay import load_causal_command_artifact


DEFAULT_FEEDBACK_PROBE = Path(
    "/home/agi/xza/DM0.5/logs/diagnostics/joint_feedback_probe_latest.json"
)
EVENT_START_S = 8.4
EVENT_END_S = 9.5
SETUP_AT_S = 1.0
ONE_SHOT_AT_S = 6.0
RESET_AT_S = 12.0
STREAM_AT_S = 18.0
POST_STREAM_OBSERVE_S = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-npz", type=Path, required=True)
    parser.add_argument("--joint-feedback-probe-json", type=Path, default=DEFAULT_FEEDBACK_PROBE)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/agi/xza/DM0.5/logs/telemetry/powered/"
            f"g2_o10_point_target_ab_{stamp}.jsonl"
        ),
    )
    return parser.parse_args()


def _sample_hands(time_s: np.ndarray, hands: np.ndarray, sample_s: float) -> np.ndarray:
    return np.asarray(
        [np.interp(sample_s, time_s, hands[:, joint]) for joint in range(20)],
        dtype=np.float64,
    )


def _v11_stream(
    time_s: np.ndarray,
    hands: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    from g2_sort.deployment.g2_client.trajectory_smoothing import (
        CausalHandCommandTracker,
        HAND_V11_MAX_ACCELERATION_RAD_S2,
        HAND_V11_MAX_DELTA_RAD,
        HAND_V11_MAX_VELOCITY_RAD_S,
    )

    start = _sample_hands(time_s, hands, EVENT_START_S)
    target = _sample_hands(time_s, hands, EVENT_END_S)
    reference_times = np.arange(EVENT_START_S, EVENT_END_S + 0.05, 0.1)
    references = np.stack(
        [_sample_hands(time_s, hands, float(value)) for value in reference_times]
    )
    tracker = CausalHandCommandTracker(
        enabled=True,
        max_velocity_rad_s=HAND_V11_MAX_VELOCITY_RAD_S,
        max_acceleration_rad_s2=HAND_V11_MAX_ACCELERATION_RAD_S2,
        max_delta_rad=HAND_V11_MAX_DELTA_RAD,
        reversal_boundary_only=True,
        nominal_hz=10.0,
    )
    tracker.reset(start)
    commands: list[np.ndarray] = []
    raw_targets: list[np.ndarray] = []
    now = 0.0
    for reference in references:
        decision = tracker.step(reference, now=now)
        commands.append(decision.command.copy())
        raw_targets.append(reference.copy())
        now += 0.1
    while not np.array_equal(commands[-1], target):
        decision = tracker.step(target, now=now)
        commands.append(decision.command.copy())
        raw_targets.append(target.copy())
        now += 0.1
        if len(commands) > 100:
            raise RuntimeError("v1_1 did not reach the frozen event endpoint")
    return start, target, np.stack(commands), np.stack(raw_targets)


def _finalize_feedback_sidecar(partial: Path, final: Path) -> bool:
    if not partial.exists():
        return False
    records = [
        json.loads(line)
        for line in partial.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    has_valid = any(
        item.get("event") == "joint_feedback_sample"
        and item.get("status") == "ok"
        and isinstance(item.get("joints_rad"), list)
        and len(item["joints_rad"]) == 34
        for item in records[:-1]
    )
    if not records or records[-1].get("event") != "joint_feedback_summary" or not has_valid:
        return False
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    return True


def main() -> int:
    cli = parse_args()
    artifact = load_causal_command_artifact(cli.artifact_npz)
    start_hand, target_hand, stream_commands, stream_raw = _v11_stream(
        artifact.time_s, artifact.hands
    )
    stream_duration = 0.1 * (len(stream_commands) - 1)
    duration_s = STREAM_AT_S + stream_duration + POST_STREAM_OBSERVE_S
    print(
        "ZERO-OUTPUT CHECK: "
        f"artifact_sha256={artifact.artifact_sha256} "
        f"event={EVENT_START_S:.1f}-{EVENT_END_S:.1f}s "
        f"stream_commands={len(stream_commands)} stream_duration_s={stream_duration:.1f} "
        f"powered_duration_s={duration_s:.1f}",
        flush=True,
    )
    print(
        "A submits the final target once; B submits v1_1 intermediate targets at 10 Hz. "
        "Both use the same frozen start and endpoint.",
        flush=True,
    )
    if not cli.execute:
        print("ZERO OUTPUT: no GDK import, initialization, or robot command.", flush=True)
        return 0

    import agibot_gdk as gdk
    from g2_client.joint_feedback import JointFeedbackWriter, load_validated_feedback_hz
    from g2_client.robot_control import G2RobotController
    from g2_client.telemetry import DispatchTelemetry
    from g2_client.trajectory_smoothing import (
        CausalHandCommandTracker,
        HAND_V11_MAX_ACCELERATION_RAD_S2,
        HAND_V11_MAX_DELTA_RAD,
        HAND_V11_MAX_VELOCITY_RAD_S,
    )

    run_id = cli.telemetry_jsonl.stem
    telemetry = ControlTelemetry(cli.telemetry_jsonl, run_id=run_id, mode="o10-point-target-ab")
    dispatch = DispatchTelemetry(telemetry)
    feedback_hz = load_validated_feedback_hz(cli.joint_feedback_probe_json)
    feedback_final = cli.telemetry_jsonl.parent.parent / "feedback" / cli.telemetry_jsonl.name
    feedback_partial = Path(f"{feedback_final}.partial")
    feedback_writer = None if feedback_hz <= 0 else JointFeedbackWriter(feedback_partial, run_id=run_id)
    controller = None
    initialized = False
    status = "fault"
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        controller = G2RobotController(
            powered=True,
            robot=gdk.Robot(),
            gdk_module=gdk,
            servo_channel="normal",
            servo_output_mode="arms-only",
            control_period=0.01,
            dispatch_telemetry=dispatch,
            joint_feedback_writer=feedback_writer,
            joint_feedback_hz=feedback_hz,
        )
        measured = np.asarray(controller.read_state(), dtype=np.float64)
        arm_hold = measured[:14].copy()
        action = measured.copy()
        action[:14] = arm_hold
        tracker = CausalHandCommandTracker(
            enabled=True,
            max_velocity_rad_s=HAND_V11_MAX_VELOCITY_RAD_S,
            max_acceleration_rad_s2=HAND_V11_MAX_ACCELERATION_RAD_S2,
            max_delta_rad=HAND_V11_MAX_DELTA_RAD,
            reversal_boundary_only=True,
            nominal_hz=10.0,
        )
        tracker.reset(start_hand)
        dispatch.begin_segment(
            mode="o10-point-target-ab",
            request_id=0,
            blend_seconds=0.0,
            expected_duration_seconds=duration_s,
        )
        telemetry.emit(
            "runtime_start",
            artifact_sha256=artifact.artifact_sha256,
            servo_channel="normal",
            arm_mode="hold-current",
            arm_hz=100.0,
            hand_worker_created=False,
            comparison="one_shot_final_vs_v1_1_10hz_intermediates",
            event_start_s=EVENT_START_S,
            event_end_s=EVENT_END_S,
            setup_at_s=SETUP_AT_S,
            one_shot_at_s=ONE_SHOT_AT_S,
            reset_at_s=RESET_AT_S,
            stream_at_s=STREAM_AT_S,
            stream_command_count=len(stream_commands),
            start_hand_rad=start_hand.tolist(),
            target_hand_rad=target_hand.tolist(),
            joint_feedback_hz=feedback_hz,
        )
        pacer = MinimumPeriodPacer(0.01, spin_guard_seconds=0.0005)
        tick_count = int(round(duration_s / 0.01)) + 1
        stream_index = 0
        for tick in range(tick_count):
            pace = pacer.wait()
            elapsed = tick * 0.01
            send_started = time.monotonic()
            controller.send_action(action)
            send_completed = time.monotonic()
            telemetry.emit(
                "o10_ab_arm_hold_sample",
                tick=tick,
                elapsed_s=elapsed,
                arm_command_rad=arm_hold.tolist(),
                send_started_monotonic_ns=int(send_started * 1e9),
                send_completed_monotonic_ns=int(send_completed * 1e9),
                dispatch_lateness_ms=pace.lateness_seconds * 1000.0,
            )
            phase = None
            command = None
            raw_target = None
            if tick == int(round(SETUP_AT_S / 0.01)):
                phase, command, raw_target = "setup_start_once", start_hand, start_hand
            elif tick == int(round(ONE_SHOT_AT_S / 0.01)):
                phase, command, raw_target = "a_final_once", target_hand, target_hand
            elif tick == int(round(RESET_AT_S / 0.01)):
                phase, command, raw_target = "reset_start_once", start_hand, start_hand
                tracker.reset(start_hand)
            elif (
                tick >= int(round(STREAM_AT_S / 0.01))
                and (tick - int(round(STREAM_AT_S / 0.01))) % 10 == 0
                and stream_index < len(stream_raw)
            ):
                phase = "b_v1_1_10hz"
                raw_target = stream_raw[stream_index]
                decision = tracker.step(raw_target, now=stream_index * 0.1)
                command = decision.command
                stream_index += 1
            if phase is not None and command is not None and raw_target is not None:
                print(f"O10 phase={phase} elapsed={elapsed:.2f}s", flush=True)
                controller.send_hands_once(command, source=f"o10_ab_{phase}")
                telemetry.emit(
                    "o10_ab_hand_command",
                    phase=phase,
                    elapsed_s=elapsed,
                    command_index=stream_index if phase == "b_v1_1_10hz" else 0,
                    command_rad=command.tolist(),
                    raw_target_rad=raw_target.tolist(),
                )
        dispatch.finish_segment(reason="ab_completed")
        telemetry.emit(
            "o10_point_target_ab_summary",
            stream_commands_sent=stream_index,
            controller_stats=controller.control_stats(),
            telemetry_drop_count=telemetry.dropped,
        )
        status = "completed"
        return 0
    except KeyboardInterrupt:
        status = "operator_ctrl_c"
        telemetry.emit("runtime_stop", reason=status)
        return 130
    finally:
        dispatch.finish_segment(reason=status)
        if controller is not None:
            controller.emergency_stop()
            controller.close()
        elif feedback_writer is not None:
            feedback_writer.close()
        if initialized:
            gdk.gdk_release()
        if feedback_writer is not None:
            if _finalize_feedback_sidecar(feedback_partial, feedback_final):
                print(f"Feedback telemetry finalized: {feedback_final}", flush=True)
            else:
                print(f"Feedback telemetry remains partial: {feedback_partial}", flush=True)
        telemetry.close(status=status)
        print(f"Telemetry finalized: {cli.telemetry_jsonl}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
