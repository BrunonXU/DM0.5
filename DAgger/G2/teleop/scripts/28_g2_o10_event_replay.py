#!/usr/bin/env python3
"""Replay frozen O10 action windows once, with static or Q-direct arms."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

import numpy as np

from g2teleop.control_telemetry import ControlTelemetry
from g2teleop.control_timing import MinimumPeriodPacer
from g2teleop.eef_replay import load_causal_command_artifact, load_frozen_replay


DEFAULT_FEEDBACK_PROBE = Path(
    "/home/user/xza/DM0.5/logs/diagnostics/joint_feedback_probe_latest.json"
)
SETUP_AT_S = 1.0
Q_DIRECT_SETUP_LEAD_S = 2.0
# Linear interpolation of an unchanged float can leave sub-ULP differences
# (observed maximum 4.44e-16 rad in this artifact).  This tolerance removes
# only that arithmetic residue; it is not a hand-motion acceptance gate.
INTERPOLATION_RESIDUE_ATOL_RAD = 1e-12
SOURCE_SETTLE_S = 1.0


@dataclass(frozen=True)
class ReplaySignals:
    input_path: Path
    input_sha256: str
    timeline_mode: str
    time_s: np.ndarray
    q_direct: np.ndarray
    hands: np.ndarray


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--artifact-npz", type=Path)
    source.add_argument("--source-replay-json", type=Path)
    parser.add_argument(
        "--arm-mode",
        choices=("hold-current", "q-direct"),
        default="hold-current",
    )
    parser.add_argument(
        "--hand-execution",
        choices=("event-endpoint", "raw-5hz-changes"),
        default="event-endpoint",
    )
    parser.add_argument("--joint-feedback-probe-json", type=Path, default=DEFAULT_FEEDBACK_PROBE)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/user/xza/DM0.5/logs/telemetry/powered/"
            f"g2_o10_event_replay_{stamp}.jsonl"
        ),
    )
    return parser.parse_args()


def load_replay_signals(cli: argparse.Namespace) -> ReplaySignals:
    if cli.artifact_npz is not None:
        artifact = load_causal_command_artifact(cli.artifact_npz)
        return ReplaySignals(
            input_path=cli.artifact_npz,
            input_sha256=artifact.artifact_sha256,
            timeline_mode=str(artifact.metadata.get("timeline_mode", "unknown")),
            time_s=artifact.time_s,
            q_direct=artifact.q_direct,
            hands=artifact.hands,
        )

    replay = load_frozen_replay(cli.source_replay_json)
    source_time_s = np.arange(len(replay.actions), dtype=np.float64) / replay.sample_hz
    source_duration_s = float(source_time_s[-1])
    tick_count = int(round((source_duration_s + SOURCE_SETTLE_S) / 0.01)) + 1
    time_s = np.arange(tick_count, dtype=np.float64) * 0.01
    sample_time_s = np.minimum(time_s, source_duration_s)
    q_direct = np.column_stack(
        [
            np.interp(sample_time_s, source_time_s, replay.states[:, joint])
            for joint in range(14)
        ]
    )
    hands = np.column_stack(
        [
            np.interp(sample_time_s, source_time_s, replay.actions[:, joint])
            for joint in range(14, 34)
        ]
    )
    return ReplaySignals(
        input_path=cli.source_replay_json,
        input_sha256=replay.artifact_sha256,
        timeline_mode="source-30hz-to-100hz",
        time_s=time_s,
        q_direct=q_direct,
        hands=hands,
    )


def coalesce_hand_events(
    time_s: np.ndarray,
    hands: np.ndarray,
) -> tuple[np.ndarray, list[tuple[float, float, np.ndarray]]]:
    """Return setup target and contiguous 10 Hz source-action endpoints."""
    sample_indices = np.arange(0, len(time_s), 10, dtype=np.intp)
    if sample_indices[-1] != len(time_s) - 1:
        sample_indices = np.append(sample_indices, len(time_s) - 1)
    sampled_time = time_s[sample_indices]
    sampled_hands = hands[sample_indices]
    step_max = np.max(np.abs(sampled_hands[1:] - sampled_hands[:-1]), axis=1)
    changing = step_max > INTERPOLATION_RESIDUE_ATOL_RAD
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, active in enumerate(changing):
        if active and start is None:
            start = index
        if start is not None and (not active or index == len(changing) - 1):
            end = index if not active else index + 1
            runs.append((start, end))
            start = None
    if not runs:
        raise RuntimeError("frozen hand trajectory contains no change windows")

    setup_target = sampled_hands[0].copy()
    events: list[tuple[float, float, np.ndarray]] = []
    for start_index, end_index in runs:
        start_s = float(sampled_time[start_index])
        end_s = float(sampled_time[end_index])
        endpoint = sampled_hands[end_index].copy()
        # The artifact begins with a 0.1 s normalization into its open pose.
        # Use that endpoint as setup rather than calling it a task hand event.
        if start_s == 0.0:
            setup_target = endpoint
            continue
        events.append((start_s, end_s, endpoint))
    if not events:
        raise RuntimeError("frozen hand trajectory contains no task hand events")
    return setup_target, events


def raw_5hz_hand_changes(
    time_s: np.ndarray,
    hands: np.ndarray,
) -> tuple[np.ndarray, list[tuple[float, float, np.ndarray]]]:
    """Return the initial target and changed samples on the original 5 Hz cadence."""
    sample_indices = np.arange(0, len(time_s), 20, dtype=np.intp)
    if sample_indices[-1] != len(time_s) - 1:
        sample_indices = np.append(sample_indices, len(time_s) - 1)
    sampled_time = time_s[sample_indices]
    sampled_hands = hands[sample_indices]
    changes: list[tuple[float, float, np.ndarray]] = []
    for index in range(1, len(sample_indices)):
        step_max = float(
            np.max(np.abs(sampled_hands[index] - sampled_hands[index - 1]))
        )
        if step_max > INTERPOLATION_RESIDUE_ATOL_RAD:
            sample_s = float(sampled_time[index])
            changes.append((sample_s, sample_s, sampled_hands[index].copy()))
    if not changes:
        raise RuntimeError("frozen hand trajectory contains no changed 5 Hz samples")
    return sampled_hands[0].copy(), changes


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
    signals = load_replay_signals(cli)
    if cli.hand_execution == "event-endpoint":
        setup_target, events = coalesce_hand_events(signals.time_s, signals.hands)
    else:
        setup_target, events = raw_5hz_hand_changes(
            signals.time_s, signals.hands
        )
    event_table = [
        {
            "event_index": index,
            "window_start_s": start_s,
            "window_end_s": end_s,
            "source_window_duration_s": end_s - start_s,
            "endpoint_rad": endpoint.tolist(),
        }
        for index, (start_s, end_s, endpoint) in enumerate(events)
    ]
    print(
        "ZERO-OUTPUT CHECK: "
        f"input_sha256={signals.input_sha256} timeline_mode={signals.timeline_mode} "
        f"duration_s={signals.time_s[-1]:.2f} "
        f"arm_mode={cli.arm_mode} hand_execution={cli.hand_execution} "
        f"hand_command_count={len(events)} "
        f"O10_calls={1 + len(events)} "
        f"setup_lead_s={Q_DIRECT_SETUP_LEAD_S if cli.arm_mode == 'q-direct' else 0.0} "
        f"arm_max_step_rad={np.max(np.abs(np.diff(signals.q_direct, axis=0))):.9f}",
        flush=True,
    )
    for item in event_table:
        print(
            "HAND COMMAND "
            f"{item['event_index']}: submit once at {item['window_start_s']:.2f}s "
            f"(source endpoint time {item['window_end_s']:.2f}s)",
            flush=True,
        )
    if not cli.execute:
        print("ZERO OUTPUT: no GDK import, initialization, or robot command.", flush=True)
        return 0

    import agibot_gdk as gdk
    from g2_client.joint_feedback import JointFeedbackWriter, load_validated_feedback_hz
    from g2_client.robot_control import G2RobotController
    from g2_client.telemetry import DispatchTelemetry

    artifact_duration_s = float(signals.time_s[-1])
    setup_lead_s = Q_DIRECT_SETUP_LEAD_S if cli.arm_mode == "q-direct" else 0.0
    duration_s = artifact_duration_s + setup_lead_s
    run_id = cli.telemetry_jsonl.stem
    telemetry = ControlTelemetry(cli.telemetry_jsonl, run_id=run_id, mode="o10-event-replay")
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
        if cli.arm_mode == "q-direct":
            start_error = float(np.max(np.abs(arm_hold - signals.q_direct[0])))
            if start_error > 0.05:
                raise RuntimeError(
                    f"arm start mismatch {start_error:.6f} > 0.050000 rad; "
                    "run approved Home first"
                )
        else:
            start_error = 0.0
        action = measured.copy()
        action[:14] = arm_hold
        dispatch.begin_segment(
            mode="o10-event-replay",
            request_id=0,
            blend_seconds=0.0,
            expected_duration_seconds=duration_s,
        )
        telemetry.emit(
            "runtime_start",
            input_path=str(signals.input_path),
            input_sha256=signals.input_sha256,
            timeline_mode=signals.timeline_mode,
            servo_channel="normal",
            arm_mode=cli.arm_mode,
            arm_hz=100.0,
            arm_start_error_rad=start_error,
            setup_lead_s=setup_lead_s,
            hand_worker_created=False,
            hand_execution=cli.hand_execution,
            interpolation_residue_atol_rad=INTERPOLATION_RESIDUE_ATOL_RAD,
            setup_at_s=SETUP_AT_S,
            setup_target_rad=setup_target.tolist(),
            event_table=event_table,
            joint_feedback_hz=feedback_hz,
        )
        pacer = MinimumPeriodPacer(0.01, spin_guard_seconds=0.0005)
        setup_lead_ticks = int(round(setup_lead_s / 0.01))
        tick_count = len(signals.time_s) + setup_lead_ticks
        event_index = 0
        for tick in range(tick_count):
            pace = pacer.wait()
            elapsed = tick * 0.01
            artifact_tick = tick - setup_lead_ticks
            if cli.arm_mode == "q-direct":
                arm_command = (
                    signals.q_direct[artifact_tick]
                    if artifact_tick >= 0
                    else signals.q_direct[0]
                )
                artifact_elapsed = (
                    float(signals.time_s[artifact_tick])
                    if artifact_tick >= 0
                    else None
                )
            else:
                arm_command = arm_hold
                artifact_elapsed = elapsed
            action[:14] = arm_command
            send_started = time.monotonic()
            controller.send_action(action)
            send_completed = time.monotonic()
            telemetry.emit(
                "o10_event_arm_hold_sample",
                tick=tick,
                elapsed_s=elapsed,
                artifact_time_s=artifact_elapsed,
                arm_command_rad=arm_command.tolist(),
                send_started_monotonic_ns=int(send_started * 1e9),
                send_completed_monotonic_ns=int(send_completed * 1e9),
                dispatch_lateness_ms=pace.lateness_seconds * 1000.0,
            )
            phase = None
            endpoint = None
            source_start_s = None
            source_end_s = None
            setup_tick = (
                0
                if cli.arm_mode == "q-direct"
                else int(round(SETUP_AT_S / 0.01))
            )
            if tick == setup_tick:
                phase = "setup_open_once"
                endpoint = setup_target
            elif artifact_tick >= 0 and event_index < len(events):
                source_start_s, source_end_s, candidate = events[event_index]
                if artifact_tick == int(round(source_start_s / 0.01)):
                    phase = (
                        "task_endpoint_once"
                        if cli.hand_execution == "event-endpoint"
                        else "raw_5hz_changed_sample_once"
                    )
                    endpoint = candidate
                    event_index += 1
            if phase is not None and endpoint is not None:
                print(
                    f"O10 phase={phase} event={event_index - 1 if source_start_s is not None else 'setup'} "
                    f"elapsed={elapsed:.2f}s",
                    flush=True,
                )
                controller.send_hands_once(
                    endpoint,
                    source=f"o10_event_{phase}",
                )
                telemetry.emit(
                    "o10_event_hand_command",
                    phase=phase,
                    event_index=(event_index - 1 if source_start_s is not None else None),
                    elapsed_s=elapsed,
                    source_window_start_s=source_start_s,
                    source_window_end_s=source_end_s,
                    endpoint_rad=endpoint.tolist(),
                )
        dispatch.finish_segment(reason="event_replay_completed")
        telemetry.emit(
            "o10_event_replay_summary",
            hand_commands_sent=event_index,
            hand_execution=cli.hand_execution,
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
