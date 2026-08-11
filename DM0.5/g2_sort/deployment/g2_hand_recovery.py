"""Safely return one or both G2 dexterous hands to the training H5 start pose.

Purpose
-------
This is an operator recovery tool, not a VLA inference client. It reads one
strict 34D measured robot state, keeps the current 14 arm joints unchanged,
and replaces only the selected hand joints with the median frame-0 action
from the training H5 episodes. The hand target is approached with a slow
linear trajectory and streamed through the existing 100 Hz servo layer.

Input
-----
- Current 34D measured state from GDK.
- ``--side`` selecting the left hand, right hand, or both hands.
- Explicit ``--execute --object-supported`` authorization for powered motion.

Output
------
- Preview of every selected hand joint's current value, target, and travel.
- In execute mode, a finite 34D absolute-position trajectory whose arm values
  are constant and whose selected hand values end at the H5 start posture.

Safety
------
Preview is the default. Powered execution requires the operator to support
the held object, uses a countdown, rejects excessive joint travel, and never
starts cameras, HTTP inference, or the DM0.5 scheduler.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from typing import Sequence

import numpy as np

from .g2_client.contracts import ACTION_DIM
from .g2_client.joint_layout import POLICY_JOINT_NAMES, clip_action_positions
from .g2_client.robot_control import G2RobotController
from .g2_client.servo import ServoStreamer

LOGGER = logging.getLogger(__name__)

ARM_DIM = 14
HAND_DIM = 10
LEFT_HAND_SLICE = slice(14, 24)
RIGHT_HAND_SLICE = slice(24, 34)

# Median episode frame-0 ACTION across the 800 formal training H5 files.
# These are absolute joint-position targets in the frozen 34D G2 ordering.
H5_LEFT_HAND_START = np.asarray(
    [
        -0.306072,
        0.928682,
        -0.201583,
        0.0,
        0.016224,
        0.016774,
        0.0,
        0.016774,
        0.0,
        0.016224,
    ],
    dtype=np.float32,
)
H5_RIGHT_HAND_START = np.asarray(
    [
        0.300176,
        -0.928269,
        0.207877,
        0.0,
        0.016774,
        0.016774,
        0.003694,
        0.016774,
        0.001299,
        0.016774,
    ],
    dtype=np.float32,
)


def _validate_state(value: np.ndarray, *, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float32)
    if array.shape != (ACTION_DIM,):
        raise ValueError(f"{name} must be shape ({ACTION_DIM},), got {array.shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or Inf")
    return array.copy()


def build_hand_recovery_target(current_state: np.ndarray, side: str) -> np.ndarray:
    """Return a 34D target that changes only the requested hand joint slices."""

    current = _validate_state(current_state, name="current_state")
    if side not in {"left", "right", "both"}:
        raise ValueError(f"unsupported side: {side!r}")

    target = current.copy()
    if side in {"left", "both"}:
        target[LEFT_HAND_SLICE] = H5_LEFT_HAND_START
    if side in {"right", "both"}:
        target[RIGHT_HAND_SLICE] = H5_RIGHT_HAND_START

    clipped, _, _ = clip_action_positions(target)
    for index in _selected_indices(side):
        if clipped[index] != target[index]:
            raise RuntimeError(
                f"H5 recovery target for {POLICY_JOINT_NAMES[index]} exceeds "
                "the configured physical joint limits"
            )
    return target


def build_linear_recovery_trajectory(
    current_state: np.ndarray,
    target_state: np.ndarray,
    *,
    duration_seconds: float,
    model_hz: float = 30.0,
) -> np.ndarray:
    """Build a convex, no-overshoot 30 Hz trajectory including both endpoints."""

    current = _validate_state(current_state, name="current_state")
    target = _validate_state(target_state, name="target_state")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0.0:
        raise ValueError("duration_seconds must be finite and greater than zero")
    if not math.isfinite(model_hz) or model_hz <= 0.0:
        raise ValueError("model_hz must be finite and greater than zero")

    intervals = max(1, int(math.ceil(duration_seconds * model_hz)))
    alpha = np.linspace(0.0, 1.0, intervals + 1, dtype=np.float32)[:, None]
    trajectory = current[None, :] + alpha * (target - current)[None, :]
    return np.asarray(trajectory, dtype=np.float32)


def _selected_indices(side: str) -> range:
    if side == "left":
        return range(LEFT_HAND_SLICE.start, LEFT_HAND_SLICE.stop)
    if side == "right":
        return range(RIGHT_HAND_SLICE.start, RIGHT_HAND_SLICE.stop)
    return range(LEFT_HAND_SLICE.start, RIGHT_HAND_SLICE.stop)


def _print_preview(current: np.ndarray, target: np.ndarray, side: str) -> float:
    print(f"\nRecovery side: {side}")
    print("Arms/body target: HOLD CURRENT MEASURED POSITION")
    print(f"{'joint':28s} {'current':>10s} {'target':>10s} {'delta':>10s}")
    print("-" * 62)
    indices = list(_selected_indices(side))
    for index in indices:
        delta = float(target[index] - current[index])
        print(
            f"{POLICY_JOINT_NAMES[index]:28s} "
            f"{float(current[index]):10.6f} "
            f"{float(target[index]):10.6f} "
            f"{delta:10.6f}"
        )
    max_travel = max(abs(float(target[i] - current[i])) for i in indices)
    print(f"Maximum selected-hand travel: {max_travel:.6f} rad")
    return max_travel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hold current arms and slowly restore selected G2 hand joints to "
            "the median frame-0 H5 action posture."
        )
    )
    parser.add_argument(
        "--side",
        choices=("left", "right", "both"),
        default="left",
        help="hand to restore; defaults to left because the supplied logs show left-thumb closure",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=5.0,
        help="duration of the slow hand recovery trajectory (default: 5)",
    )
    parser.add_argument(
        "--countdown-seconds",
        type=int,
        default=5,
        help="operator countdown before powered motion (default: 5)",
    )
    parser.add_argument(
        "--max-joint-travel-radians",
        type=float,
        default=1.5,
        help="abort if any selected hand joint must travel farther (default: 1.5)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="enable powered motion; without this flag the command is preview-only",
    )
    parser.add_argument(
        "--object-supported",
        action="store_true",
        help="confirm a person/support is ready because opening the hand may drop the object",
    )
    parser.add_argument(
        "--gdk-stabilization-seconds",
        type=float,
        default=3.0,
        help="wait after GDK initialization before reading state (default: 3)",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.duration_seconds < 2.0 or args.duration_seconds > 30.0:
        parser.error("--duration-seconds must be between 2 and 30")
    if args.countdown_seconds < 0 or args.countdown_seconds > 30:
        parser.error("--countdown-seconds must be between 0 and 30")
    if (
        not math.isfinite(args.max_joint_travel_radians)
        or args.max_joint_travel_radians <= 0.0
    ):
        parser.error("--max-joint-travel-radians must be finite and positive")
    if args.execute and not args.object_supported:
        parser.error(
            "--execute also requires --object-supported; opening the hand may drop the object"
        )


def _countdown(seconds: int) -> None:
    if seconds <= 0:
        return
    print("\nSupport the bottle/object now. Keep the hardware emergency stop ready.")
    for remaining in range(seconds, 0, -1):
        print(f"Powered hand recovery starts in {remaining}...", flush=True)
        time.sleep(1.0)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    robot: G2RobotController | None = None
    servo: ServoStreamer | None = None
    gdk_module = None
    gdk_initialized = False
    completed = False

    try:
        # This standalone tool does not pass through g2_client.app, so it must
        # own the GDK lifecycle itself. Robot() alone can receive read-only DDS
        # state, but joint_servo_control's Config Manager requires gdk_init().
        import agibot_gdk as gdk_module

        init_result = gdk_module.gdk_init()
        if init_result != gdk_module.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {init_result}")
        gdk_initialized = True

        robot = G2RobotController(
            powered=args.execute,
            control_period=0.01,
            stabilization_seconds=args.gdk_stabilization_seconds,
            gdk_module=gdk_module,
        )
        current = robot.read_state()
        clipped_current, current_clip_count, current_clip_delta = (
            clip_action_positions(current)
        )
        if current_clip_count:
            raise RuntimeError(
                "measured state is outside configured position limits "
                f"(count={current_clip_count}, max_delta={current_clip_delta:.6f}); "
                "refusing recovery because an unchanged hold cannot be guaranteed"
            )
        del clipped_current
        target = build_hand_recovery_target(current, args.side)
        max_travel = _print_preview(current, target, args.side)

        if max_travel > args.max_joint_travel_radians:
            raise RuntimeError(
                f"recovery needs {max_travel:.6f} rad, exceeding "
                f"--max-joint-travel-radians={args.max_joint_travel_radians:.6f}"
            )

        if not args.execute:
            print(
                "\nPREVIEW ONLY: no servo command was sent. "
                "Add --execute --object-supported after checking these values."
            )
            return 0

        trajectory = build_linear_recovery_trajectory(
            current,
            target,
            duration_seconds=args.duration_seconds,
        )
        intervals = trajectory.shape[0] - 1

        # Warm up and validate joint_servo_control in this exact GDK process
        # before asking the operator to wait through the powered countdown.
        # The target equals the measured state, so this preflight must not
        # intentionally move any joint.
        print("\nPreflight: sending one current-position hold target...")
        robot.send_action(current)
        servo = ServoStreamer(
            robot.send_action,
            initial_hold_target=current,
            servo_hz=100.0,
        )
        servo.start()
        time.sleep(0.25)
        if servo.has_fault():
            raise RuntimeError(
                f"100 Hz hold preflight failed: {servo.fault_message()}"
            )
        print("Preflight: PASS; 100 Hz current-position hold is healthy.")

        # The streamer continues to hold the measured posture throughout the
        # countdown. Only play() below begins the hand-opening trajectory.
        _countdown(args.countdown_seconds)
        servo.play(
            trajectory,
            start_index=0,
            interval_count=intervals,
            blend_seconds=0.0,
        )

        while servo.is_playing():
            if servo.has_fault():
                raise RuntimeError(
                    f"100 Hz servo sender failed: {servo.fault_message()}"
                )
            time.sleep(0.01)

        if servo.has_fault():
            raise RuntimeError(
                f"100 Hz servo sender failed: {servo.fault_message()}"
            )

        servo.hold(target)
        time.sleep(0.25)
        completed = True
        print(
            f"\nDONE: {args.side} hand reached the H5 frame-0 posture in "
            f"{args.duration_seconds:.2f}s; arms were held at their measured start values."
        )
        return 0
    except KeyboardInterrupt:
        print("\nINTERRUPTED: holding the latest commanded position and stopping.")
        return 130
    except Exception:
        LOGGER.exception("Hand recovery failed; use the hardware stop if the robot is unsafe")
        return 1
    finally:
        if servo is not None:
            try:
                servo.hold()
                time.sleep(0.05)
            except Exception:
                LOGGER.exception("Could not send final hold before shutdown")
            servo.close()
        if robot is not None:
            robot.close()
        if gdk_initialized and gdk_module is not None:
            try:
                release_result = gdk_module.gdk_release()
                if release_result not in (None, gdk_module.GDKRes.kSuccess):
                    LOGGER.warning("gdk_release returned %s", release_result)
            except Exception:
                LOGGER.exception("gdk_release failed")
        if not completed and args.execute:
            print(
                "Recovery did not complete. Do not restart VLA powered mode until "
                "the physical posture is checked.",
                file=sys.stderr,
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(run())


if __name__ == "__main__":
    main()
