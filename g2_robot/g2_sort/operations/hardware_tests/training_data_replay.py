"""Replay one complete frozen training episode on G2 arms, without DM0.5."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM, MODEL_HZ
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES, clip_action_positions
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.operations.operator_confirmation import confirm_enter_and_countdown

LOGGER = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "training_replay_v1.json"
ARM_DIM = 14
MAX_START_ARM_ERROR_RAD = 0.05
MAX_START_HAND_ERROR_RAD = 0.08
MAX_ZERO_HAND_FEEDBACK_RAD = 0.02
MAX_SOURCE_ARM_FRAME_STEP_RAD = 0.08
MAX_ARM_TRACKING_ERROR_RAD = 0.25
MAX_CONSECUTIVE_TRACKING_FAILURES = 3


def load_replay(path: Path = CONFIG_PATH) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "g2-training-replay-v1":
        raise ValueError("unsupported replay artifact version")
    if int(payload.get("dimension", -1)) != ACTION_DIM:
        raise ValueError("replay dimension does not match the 34D contract")
    if float(payload.get("sample_hz", -1)) != MODEL_HZ:
        raise ValueError("replay sample rate must be 30 Hz")
    states = np.asarray(payload.get("states"), dtype=np.float64)
    actions = np.asarray(payload.get("actions"), dtype=np.float64)
    if states.shape != (948, ACTION_DIM) or actions.shape != states.shape:
        raise ValueError("frozen replay must contain matching 948x34 arrays")
    if not np.all(np.isfinite(states)) or not np.all(np.isfinite(actions)):
        raise ValueError("replay artifact contains non-finite values")
    if not np.array_equal(actions[0], states[0]):
        raise ValueError("frame-0 action must exactly equal frame-0 state")
    clipped, count, max_delta = clip_action_positions(actions)
    if count or max_delta > 0 or not np.array_equal(clipped, actions):
        raise ValueError("replay artifact violates configured physical limits")
    arm_step = float(np.max(np.abs(np.diff(actions[:, :ARM_DIM], axis=0))))
    if arm_step > MAX_SOURCE_ARM_FRAME_STEP_RAD:
        raise ValueError(
            f"source arm step {arm_step:.6f} exceeds "
            f"{MAX_SOURCE_ARM_FRAME_STEP_RAD:.6f}rad"
        )
    payload["states"] = states
    payload["actions"] = actions
    return payload


def build_arms_only_actions(source_actions: np.ndarray, measured: np.ndarray) -> np.ndarray:
    actions = np.asarray(source_actions, dtype=np.float64).copy()
    current = np.asarray(measured, dtype=np.float64)
    if actions.ndim != 2 or actions.shape[1] != ACTION_DIM:
        raise ValueError("source actions must be Nx34")
    if current.shape != (ACTION_DIM,) or not np.all(np.isfinite(current)):
        raise ValueError("measured state must be finite 34D")
    actions[:, ARM_DIM:] = current[ARM_DIM:]
    return actions


def arm_start_error(measured: np.ndarray, expected: np.ndarray) -> float:
    current = np.asarray(measured, dtype=np.float64)
    reference = np.asarray(expected, dtype=np.float64)
    if current.shape != (ACTION_DIM,) or reference.shape != (ACTION_DIM,):
        raise ValueError("start states must both be 34D")
    return float(np.max(np.abs(current[:ARM_DIM] - reference[:ARM_DIM])))


def hand_start_error(measured: np.ndarray, expected: np.ndarray) -> float:
    current = np.asarray(measured, dtype=np.float64)
    reference = np.asarray(expected, dtype=np.float64)
    if current.shape != (ACTION_DIM,) or reference.shape != (ACTION_DIM,):
        raise ValueError("start states must both be 34D")
    return float(np.max(np.abs(current[ARM_DIM:] - reference[ARM_DIM:])))


def hand_feedback_is_usable(measured: np.ndarray) -> bool:
    """Reject reboot-time O10 all-zero feedback as a position measurement."""
    current = np.asarray(measured, dtype=np.float64)
    if current.shape != (ACTION_DIM,) or not np.all(np.isfinite(current)):
        raise ValueError("measured state must be finite 34D")
    return bool(np.max(np.abs(current[ARM_DIM:])) >= MAX_ZERO_HAND_FEEDBACK_RAD)


def build_hand_start_preparation(measured: np.ndarray, first_action: np.ndarray) -> np.ndarray:
    """Five seconds at source 30Hz; hold arms, interpolate hands, no new limit."""
    measured = np.asarray(measured, dtype=np.float64)
    first_action = np.asarray(first_action, dtype=np.float64)
    if not hand_feedback_is_usable(measured) or first_action.shape != (34,) or not np.isfinite(first_action).all():
        raise ValueError("hand preparation requires finite usable reported start and 34D target")
    alpha = np.linspace(0., 1., 151)[:, None]
    result = np.tile(measured, (151, 1))
    result[:, 14:] = measured[14:] + alpha * (first_action[14:] - measured[14:])
    return result


def prepare_hand_start(robot, servo, measured, first_action, report) -> None:
    trajectory = build_hand_start_preparation(measured, first_action)
    report["hand_preparation"] = {"duration_seconds": 5., "start_reported": measured.tolist(),
        "target": trajectory[-1].tolist(), "verified": False}
    print("HAND PREP: same-owner unified-34 normal Servo; arms hold, hands approach frame 0 over 5s.", flush=True)
    playback_id = servo.play(trajectory, start_index=0, interval_count=150, blend_seconds=0.)
    deadline = time.monotonic() + 7.
    while servo.is_playing(playback_id):
        if servo.has_fault():
            raise RuntimeError(servo.fault_message() or "hand preparation Servo fault")
        if time.monotonic() > deadline:
            raise RuntimeError("hand preparation playback timed out")
        time.sleep(.01)
    servo.hold(trajectory[-1])
    # Read while holding the submitted target. This is position agreement only,
    # not a grip-force or general hand-feedback certification.
    deadline = time.monotonic() + 2.
    while True:
        if servo.has_fault():
            raise RuntimeError(servo.fault_message() or "hand preparation hold fault")
        feedback = np.asarray(robot.read_state(), dtype=np.float64)
        error = hand_start_error(feedback, first_action)
        report["hand_preparation"].update(final_reported=feedback.tolist(), max_error_rad=error)
        if (hand_feedback_is_usable(feedback) and error <= MAX_START_HAND_ERROR_RAD
                and arm_start_error(feedback, first_action) <= MAX_START_ARM_ERROR_RAD):
            report["hand_preparation"]["verified"] = True
            print(f"HAND PREP position check passed: max_hand_error={error:.6f}rad", flush=True)
            return
        if time.monotonic() >= deadline:
            for index in np.argsort(np.abs(feedback[14:] - first_action[14:]))[-5:][::-1] + 14:
                print(f"HAND PREP mismatch {POLICY_JOINT_NAMES[index]} target={first_action[index]:.6f} reported={feedback[index]:.6f}", flush=True)
            raise RuntimeError(f"hand preparation feedback did not reach frame 0: error={error:.6f}; dataset playback NOT started")
        time.sleep(.1)


def _preview(
    payload: dict[str, object], measured: np.ndarray, actions: np.ndarray, *, with_hands: bool, output_mode: str | None = None
) -> tuple[float, float]:
    start_error = arm_start_error(measured, actions[0])
    start_hand_error = hand_start_error(measured, actions[0])
    arm_step = float(np.max(np.abs(np.diff(actions[:, :ARM_DIM], axis=0))))
    hand_5hz_step = float(
        np.max(np.abs(np.diff(actions[::6, ARM_DIM:], axis=0)))
    )
    joint_range = float(
        np.max(np.max(actions[:, :ARM_DIM], axis=0) - np.min(actions[:, :ARM_DIM], axis=0))
    )
    print(
        f"episode={payload['episode_id']} split=train frames=0..947 "
        f"duration={(len(actions) - 1) / MODEL_HZ:.3f}s"
    )
    print(
        f"mode={output_mode or ('split' if with_hands else 'arms-only')} "
        "servo_channel=normal hand_v1=off hand_smoothing=off"
    )
    print(
        f"start_arm_error={start_error:.6f}rad "
        f"start_hand_error={start_hand_error:.6f}rad "
        f"max_source_frame_step={arm_step:.6f}rad "
        f"max_raw_hand_5hz_step={hand_5hz_step:.6f}rad "
        f"max_episode_joint_range={joint_range:.6f}rad"
    )
    deltas = actions[30, :ARM_DIM] - actions[0, :ARM_DIM]
    print("Largest initial-to-first-second arm deltas:")
    for index in np.argsort(np.abs(deltas))[::-1][:5]:
        print(f"  {POLICY_JOINT_NAMES[int(index)]}: {deltas[int(index)]:+.6f}rad")
    return start_error, start_hand_error


def _write_report(path: Path | None, report: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report={path}")


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--with-hands", action="store_true")
    parser.add_argument("--unified-34", action="store_true", help="single canonical 34D normal Servo; includes hands")
    parser.add_argument("--prepare-hands", action="store_true", help="after ENTER, prepare hands using unified34 before replay")
    parser.add_argument("--files-only", action="store_true", help="validate frozen artifact without importing GDK")
    parser.add_argument("--confirm", default="", help="legacy parsing only; execution requires ENTER")
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args()
    if args.files_only and args.execute:
        parser.error("--files-only cannot be combined with --execute")
    if args.prepare_hands and not args.unified_34:
        parser.error("--prepare-hands requires --unified-34")
    args.with_hands = args.with_hands or args.unified_34
    output_mode = "unified-34" if args.unified_34 else ("split" if args.with_hands else "arms-only")
    payload = load_replay()
    source_actions = np.asarray(payload["actions"], dtype=np.float64)
    if args.files_only:
        print(f"FILES ONLY: episode={payload['episode_id']} frames={len(source_actions)} source=actions_34d source_hz=30 servo_hz=100 layout={output_mode} channel=normal; no GDK")
        return 0
    from g2_sort.deployment.g2_client.app import _acquire_pid_file, _release_pid_file
    import os
    pid_file = Path(os.environ.get("G2_RUNTIME_PID_FILE", "/tmp/g2_dm05_runtime.pid"))
    acquired = False
    gdk = None
    initialized = False
    robot = None
    servo = None
    output_started = False
    report: dict[str, object] = {
        "version": "g2-training-replay-result-v1",
        "episode_id": payload["episode_id"],
        "source_jsonl_sha256": payload["source_jsonl_sha256"],
        "frame_count": len(source_actions),
        "output_mode": output_mode,
        "source": "actions_34d", "source_hz": MODEL_HZ, "servo_hz": 100,
        "joint_names": list(POLICY_JOINT_NAMES),
        "servo_channel": "normal",
        "hand_output": "unified_servo_100hz" if args.unified_34 else ("raw_5hz_no_smoothing" if args.with_hands else "disabled"),
        "hand_smoothing_profile": "off",
        "execute": bool(args.execute),
        "completed": False,
    }
    try:
        _acquire_pid_file(pid_file)
        acquired = True
        import agibot_gdk as gdk

        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = G2RobotController(
            powered=args.execute,
            servo_channel="normal",
            servo_output_mode=output_mode,
            stabilization_seconds=3.0,
            **({"max_send_seconds": 0.2, "timing_warning_callback":
                lambda event, actual, limit: LOGGER.warning("%s actual=%.6f limit=%.6f", event, actual, limit)}
               if args.unified_34 else {}),
            hand_smoothing_profile="off",
        )
        measured = np.asarray(robot.read_state(), dtype=np.float64)
        actions = (
            source_actions.copy()
            if args.with_hands
            else build_arms_only_actions(source_actions, measured)
        )
        start_error, start_hand_error = _preview(
            payload, measured, actions, with_hands=args.with_hands, output_mode=output_mode
        )
        hand_feedback_usable = hand_feedback_is_usable(measured)
        report["initial_measured"] = measured.tolist()
        report["start_arm_error_rad"] = start_error
        report["start_hand_error_rad"] = start_hand_error
        report["hand_start_feedback_usable"] = hand_feedback_usable
        report["hand_start_source"] = (
            "measured_feedback"
            if hand_feedback_usable
            else "training_frame0_zero_feedback_unavailable"
        )
        if start_error > MAX_START_ARM_ERROR_RAD:
            raise RuntimeError(
                f"arm start mismatch {start_error:.6f} > "
                f"{MAX_START_ARM_ERROR_RAD:.6f}rad; run approved Home first"
            )
        needs_preparation = args.prepare_hands and start_hand_error > MAX_START_HAND_ERROR_RAD
        if args.unified_34 and not hand_feedback_usable:
            raise RuntimeError("unified34 replay needs usable hand start feedback; zero feedback is not a physical pose")
        if args.with_hands and not hand_feedback_usable:
            print(
                "O10 feedback is all-zero/near-zero and cannot represent the "
                "physical hand pose; hand start is anchored directly to the "
                "recorded training frame-0 command."
            )
        elif args.with_hands and start_hand_error > MAX_START_HAND_ERROR_RAD and not needs_preparation:
            raise RuntimeError(
                f"hand start mismatch {start_hand_error:.6f} > "
                f"{MAX_START_HAND_ERROR_RAD:.6f}rad; do not replay hands"
            )
        if not args.execute:
            print("PREVIEW ONLY: GDK state was read; no command was sent.")
            return 0
        confirm_enter_and_countdown(
            ("先保持双臂、5秒渐进开手并核对反馈，再单34D回放（31.6秒）" if needs_preparation else "完整训练episode双臂+双手原始回放（31.6秒）")
            if args.with_hands
            else "完整训练episode双臂回放（31.6秒）",
        )
        anchor = measured.copy() if needs_preparation else (actions[0].copy() if args.with_hands else measured.copy())
        if args.with_hands and not args.unified_34:
            robot.begin_hand_segment(started_at=time.monotonic())
        output_started = True
        robot.send_action(anchor)
        servo = ServoStreamer(robot.send_action, anchor,
            **({"max_tick_lateness_seconds": 0.2, "timing_warning_callback":
                lambda event, actual, limit: LOGGER.warning("%s actual=%.6f limit=%.6f", event, actual, limit)}
               if args.unified_34 else {}))
        servo.start()
        time.sleep(0.20)
        if needs_preparation:
            prepare_hand_start(robot, servo, measured, actions[0], report)
        playback_id = servo.play(
            actions,
            start_index=0,
            interval_count=len(actions) - 1,
            blend_seconds=0.0,
        )
        deadline = time.monotonic() + (len(actions) - 1) / MODEL_HZ + 2.0
        next_tracking_check = time.monotonic() + 0.2
        consecutive_tracking_failures = 0
        max_arm_tracking_error = 0.0
        while servo.is_playing(playback_id):
            if servo.has_fault():
                raise RuntimeError(servo.fault_message() or "servo fault")
            if time.monotonic() > deadline:
                raise RuntimeError("training replay exceeded playback deadline")
            if time.monotonic() >= next_tracking_check:
                feedback = np.asarray(robot.read_state(), dtype=np.float64)
                tracking_error = arm_start_error(feedback, servo.current_target())
                max_arm_tracking_error = max(max_arm_tracking_error, tracking_error)
                if tracking_error > MAX_ARM_TRACKING_ERROR_RAD:
                    consecutive_tracking_failures += 1
                else:
                    consecutive_tracking_failures = 0
                if consecutive_tracking_failures >= MAX_CONSECUTIVE_TRACKING_FAILURES:
                    report["max_arm_tracking_error_rad"] = max_arm_tracking_error
                    report["tracking_gate_triggered"] = True
                    servo.stop()
                    robot.emergency_stop()
                    raise RuntimeError(
                        "arm tracking gate stopped replay: "
                        f"{tracking_error:.6f}rad > "
                        f"{MAX_ARM_TRACKING_ERROR_RAD:.6f}rad for "
                        f"{consecutive_tracking_failures} checks"
                    )
                next_tracking_check = time.monotonic() + 0.2
            time.sleep(0.01)
        servo.hold(actions[-1])
        time.sleep(0.25)
        final_measured = np.asarray(robot.read_state(), dtype=np.float64)
        final_error = arm_start_error(final_measured, actions[-1])
        final_hand_error = hand_start_error(final_measured, actions[-1])
        report["final_target"] = actions[-1].tolist()
        report["final_measured"] = final_measured.tolist()
        report["final_arm_tracking_error_rad"] = final_error
        report["final_hand_tracking_error_rad"] = final_hand_error
        report["max_arm_tracking_error_rad"] = max_arm_tracking_error
        report["tracking_gate_triggered"] = False
        report["control_stats"] = robot.control_stats()
        report["completed"] = True
        print(
            f"DONE: final_arm_tracking_error={final_error:.6f}rad "
            f"final_hand_tracking_error={final_hand_error:.6f}rad"
        )
        print(
            "完整双臂+双手原始示教已结束；手部V1和平滑均关闭。"
            if args.with_hands
            else "完整示教已结束并停止发送；双手从始至终未被命令。"
        )
        return 0
    except KeyboardInterrupt:
        print("\nINTERRUPTED: output stopping; use physical E-stop if unsafe.")
        return 130
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        LOGGER.exception("Training replay failed closed")
        return 1
    finally:
        # Release the owner lock only after closing Servo/controller/GDK.
        try:
            _write_report(args.report_json, report)
        finally:
            try:
                if servo is not None:
                    servo.close()
                if robot is not None:
                    if args.execute and output_started:
                        robot.emergency_stop()
                    robot.close()
                if initialized and gdk is not None:
                    gdk.gdk_release()
            finally:
                if acquired:
                    _release_pid_file(pid_file)



def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
