#!/usr/bin/env python3
"""Replay one frozen causal arm stage on G2; never recompute FK, Pink, or OTG."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-npz", type=Path, required=True)
    parser.add_argument("--stage", choices=("direct", "pink", "otg"), required=True)
    parser.add_argument(
        "--arm-mode",
        choices=("stage", "hold-start", "hold-current"),
        default="stage",
    )
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument(
        "--servo-channel",
        choices=("normal", "low-latency"),
        default="normal",
        help="keep identical across a paired A/B; normal is collision-protected",
    )
    parser.add_argument("--joint-feedback-probe-json", type=Path, default=DEFAULT_FEEDBACK_PROBE)
    parser.add_argument("--disable-joint-feedback", action="store_true")
    parser.add_argument(
        "--hand-mode",
        choices=("disabled", "hold-start", "hold-current", "recorded-actions"),
        default="disabled",
    )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/agi/xza/DM0.5/logs/telemetry/powered/"
            f"g2_frozen_arm_stage_{stamp}.jsonl"
        ),
    )
    return parser.parse_args()


def _stage_stats(time_s: np.ndarray, commands: np.ndarray) -> dict[str, float | int]:
    intervals = np.diff(time_s)
    velocity = np.diff(commands, axis=0) / intervals[:, None]
    terminal_count = max(2, int(round(0.25 / float(np.median(intervals)))))
    terminal_velocity = velocity[-terminal_count:]
    return {
        "ticks": int(len(commands)),
        "duration_s": float(time_s[-1]),
        "period_p50_s": float(np.percentile(intervals, 50)),
        "period_max_s": float(np.max(intervals)),
        "max_step_rad": float(np.max(np.abs(np.diff(commands, axis=0)))),
        "max_velocity_rad_s": float(np.max(np.abs(velocity))),
        "terminal_250ms_max_velocity_rad_s": float(np.max(np.abs(terminal_velocity))),
    }


def _validate_powered_gate(artifact, stats: dict[str, float | int]) -> None:
    if float(artifact.metadata.get("settle_seconds", 0.0)) < 0.5:
        raise RuntimeError("artifact has less than 0.5 s settle tail")
    if abs(float(stats["period_p50_s"]) - 0.01) > 1e-6:
        raise RuntimeError("artifact is not on the required 100 Hz common clock")
    if float(stats["period_max_s"]) > 0.010001:
        raise RuntimeError("artifact contains a non-100 Hz command interval")
    if float(stats["terminal_250ms_max_velocity_rad_s"]) > 0.005:
        raise RuntimeError("selected stage is still moving in its terminal settle window")


def _finalize_feedback_sidecar(partial: Path, final: Path) -> bool:
    """Publish feedback only when it ends with a summary and has a valid sample."""
    if not partial.exists():
        return False
    records = []
    for line in partial.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    has_valid = any(
        record.get("event") == "joint_feedback_sample"
        and record.get("status") == "ok"
        and isinstance(record.get("joints_rad"), list)
        and len(record["joints_rad"]) == 34
        for record in records[:-1]
    )
    if not records or records[-1].get("event") != "joint_feedback_summary" or not has_valid:
        return False
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    return True


def main() -> int:
    cli = parse_args()
    artifact = load_causal_command_artifact(cli.artifact_npz)
    if cli.hand_mode != "disabled" and (
        artifact.metadata.get("version") != "g2-causal-command-replay-v2"
        or artifact.metadata.get("hand_command_source") != "actions"
    ):
        raise RuntimeError("O10 output requires a v2 artifact with action commands")
    commands = artifact.stage(cli.stage)
    time_s = artifact.time_s.copy()
    hands = artifact.hands.copy()
    if cli.duration_seconds is not None:
        if cli.duration_seconds <= 0.0 or cli.duration_seconds > float(time_s[-1]):
            raise RuntimeError("duration seconds must be within the artifact duration")
        count = int(np.searchsorted(time_s, cli.duration_seconds, side="right"))
        if count < 2:
            raise RuntimeError("duration selects fewer than two control ticks")
        time_s = time_s[:count]
        commands = commands[:count]
        hands = hands[:count]
    if cli.arm_mode in {"hold-start", "hold-current"}:
        commands = np.repeat(commands[0][None, :], len(commands), axis=0)
    if cli.hand_mode in {"hold-start", "hold-current"}:
        hands = np.repeat(hands[0][None, :], len(hands), axis=0)
    stats = _stage_stats(time_s, commands)
    current_pose_hold = (
        cli.arm_mode == "hold-current" and cli.hand_mode == "hold-current"
    )
    if not current_pose_hold:
        _validate_powered_gate(artifact, stats)
    print(
        "ZERO-OUTPUT CHECK: "
        f"artifact_sha256={artifact.artifact_sha256} stage={cli.stage} "
        f"arm_mode={cli.arm_mode} hand_mode={cli.hand_mode} "
        f"channel={cli.servo_channel} stats={json.dumps(stats, sort_keys=True)}",
        flush=True,
    )
    if not cli.execute:
        print("ZERO OUTPUT: no GDK import, initialization, or robot command.", flush=True)
        return 0

    print(
        "POWERED SCOPE: 14 arm joints"
        + (" + O10 " + cli.hand_mode if cli.hand_mode != "disabled" else " only")
        + "; chassis/waist output disabled. "
        + f"Servo={cli.servo_channel}.",
        flush=True,
    )
    import agibot_gdk as gdk
    from g2_client.joint_feedback import JointFeedbackWriter, load_validated_feedback_hz
    from g2_client.robot_control import G2RobotController
    from g2_client.telemetry import DispatchTelemetry

    run_id = cli.telemetry_jsonl.stem
    telemetry = ControlTelemetry(
        cli.telemetry_jsonl,
        run_id=run_id,
        mode="powered-frozen-arm-stage",
    )
    dispatch = DispatchTelemetry(telemetry)
    feedback_hz = (
        0.0
        if cli.disable_joint_feedback
        else load_validated_feedback_hz(cli.joint_feedback_probe_json)
    )
    feedback_final_path = (
        cli.telemetry_jsonl.parent.parent
        / "feedback"
        / f"{cli.telemetry_jsonl.stem}.jsonl"
    )
    feedback_path = Path(f"{feedback_final_path}.partial")
    feedback_writer = (
        None if feedback_hz <= 0.0 else JointFeedbackWriter(feedback_path, run_id=run_id)
    )
    initialized = False
    controller = None
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
            servo_channel=cli.servo_channel,
            servo_output_mode=(
                "split" if cli.hand_mode != "disabled" else "arms-only"
            ),
            control_period=0.01,
            hand_stream_hz=10.0,
            hand_smoothing_profile="v1_1",
            dispatch_telemetry=dispatch,
            joint_feedback_writer=feedback_writer,
            joint_feedback_hz=feedback_hz,
        )
        measured = np.asarray(controller.read_state(), dtype=np.float64)
        if cli.arm_mode == "hold-current":
            commands = np.repeat(measured[:14][None, :], len(commands), axis=0)
        if cli.hand_mode == "hold-current":
            if np.max(np.abs(measured[14:])) < 0.05:
                raise RuntimeError(
                    "current O10 feedback is all-zero/near-zero and unsafe to echo"
                )
            hands = np.repeat(measured[14:][None, :], len(hands), axis=0)
        start_error = float(np.max(np.abs(measured[:14] - commands[0])))
        if cli.arm_mode != "hold-current" and start_error > 0.05:
            raise RuntimeError(
                f"arm start mismatch {start_error:.6f} > 0.050000 rad; run approved Home first"
            )
        hand_feedback_usable = bool(np.max(np.abs(measured[14:])) >= 0.05)
        hand_start_error = float(np.max(np.abs(measured[14:] - artifact.hands[0])))
        if cli.hand_mode != "disabled":
            controller.begin_hand_segment(started_at=time.monotonic())
        dispatch.begin_segment(
            mode=f"frozen-{cli.stage}",
            request_id=0,
            blend_seconds=0.0,
            expected_duration_seconds=float(time_s[-1]),
        )
        telemetry.emit(
            "runtime_start",
            mode="powered-frozen-arm-stage",
            process_id=os.getpid(),
            artifact_path=str(cli.artifact_npz),
            artifact_sha256=artifact.artifact_sha256,
            artifact_metadata=artifact.metadata,
            command_stage=cli.stage,
            arm_mode=cli.arm_mode,
            servo_channel=cli.servo_channel,
            collision_protection=cli.servo_channel == "normal",
            command_scope=(
                "arms14_normal_hands20_move_ee_pos"
                if cli.hand_mode != "disabled"
                else "arms14_only"
            ),
            control_hz=100.0,
            hand_stream_hz=10.0 if cli.hand_mode != "disabled" else 0.0,
            hand_mode=cli.hand_mode,
            hand_start_feedback_usable=hand_feedback_usable,
            hand_start_error_rad=hand_start_error,
            current_pose_anchored=current_pose_hold,
            joint_feedback_hz=feedback_hz,
            start_error_rad=start_error,
            stage_stats=stats,
        )
        pacer = MinimumPeriodPacer(0.01, spin_guard_seconds=0.0005)
        action = measured.copy()
        previous_send_started = None
        for tick, arm_command in enumerate(commands):
            pace = pacer.wait()
            send_started = time.monotonic()
            action[:14] = arm_command
            if cli.hand_mode != "disabled":
                action[14:] = hands[tick]
            controller.send_action(action)
            send_completed = time.monotonic()
            telemetry.emit(
                "frozen_arm_dispatch_sample",
                tick=tick,
                artifact_time_s=float(time_s[tick]),
                command_stage=cli.stage,
                arm_mode=cli.arm_mode,
                arm_command_rad=arm_command.tolist(),
                dispatch_deadline_monotonic_ns=int(pace.deadline * 1e9),
                send_started_monotonic_ns=int(send_started * 1e9),
                send_completed_monotonic_ns=int(send_completed * 1e9),
                send_interval_seconds=(
                    None if previous_send_started is None else send_started - previous_send_started
                ),
                dispatch_lateness_ms=pace.lateness_seconds * 1000.0,
                controller_call_duration_ms=(send_completed - send_started) * 1000.0,
                telemetry_drop_count=telemetry.dropped,
            )
            previous_send_started = send_started
        dispatch.finish_segment(reason="artifact_completed_after_settle")
        telemetry.emit(
            "frozen_arm_stage_summary",
            command_stage=cli.stage,
            artifact_sha256=artifact.artifact_sha256,
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
            if _finalize_feedback_sidecar(feedback_path, feedback_final_path):
                print(f"Feedback telemetry finalized: {feedback_final_path}", flush=True)
            else:
                print(f"Feedback telemetry remains partial: {feedback_path}", flush=True)
        telemetry.close(status=status)
        print(f"Telemetry finalized: {cli.telemetry_jsonl}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
