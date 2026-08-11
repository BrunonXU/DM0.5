"""Preview selected G2 arms/hands against the training start pose.

Purpose
-------
This is an explicit operator recovery utility, not a VLA client. It loads the
versioned 34D frame-0 action median and preserves every unselected joint.
All real execution is currently locked pending a camera-clear O10 reset target.

Inputs / outputs
----------------
Input is one strict measured 34D state plus ``--part`` and ``--servo-channel``.
Preview prints every delta and sends nothing. ``--execute`` currently fails
closed for every part through ``reset_poses.json``.

Safety
------
Normal mode is the default and keeps GDK collision protection. Low-latency has
no collision protection. The official O10 actuator API accepts explicit joint
angles, but neither reboot-time zero feedback nor the H5 frame-0 thumb pose is
approved as a universal physical reset target.

Method
------
Keep unselected joints at their measured values and build a convex transition
for preview. If execution is later re-enabled, it must use the production Split
dispatch (14D arms at 100 Hz plus 20D O10 hands at 5 Hz), never unified 34D.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from g2_sort.deployment.g2_client.contracts import ACTION_DIM
from g2_sort.deployment.g2_client.joint_layout import (
    POLICY_JOINT_NAMES,
    clip_action_positions,
)
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

LOGGER = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "reset_poses.json"

PART_INDICES = {
    "left-hand": tuple(range(14, 24)),
    "right-hand": tuple(range(24, 34)),
    "hands": tuple(range(14, 34)),
    "left-arm": tuple(range(0, 7)),
    "right-arm": tuple(range(7, 14)),
    "arms": tuple(range(0, 14)),
    "all": tuple(range(0, 34)),
}
HAND_PARTS = {"left-hand", "right-hand", "hands", "all"}


def load_reset_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("dimension", -1)) != ACTION_DIM:
        raise ValueError("reset pose dimension does not match the 34D contract")
    for key in ("state_median", "action_median"):
        array = np.asarray(payload.get(key), dtype=np.float64)
        if array.shape != (ACTION_DIM,) or not np.all(np.isfinite(array)):
            raise ValueError(f"{key} must be a finite 34D array")
    return payload


def part_execution_enabled(config: dict[str, object], part: str) -> bool:
    execution = config.get("execution")
    if not isinstance(execution, dict):
        return False
    flags = {
        "left-hand": ("left_hand_enabled",),
        "right-hand": ("right_hand_enabled",),
        "hands": ("left_hand_enabled", "right_hand_enabled"),
        "left-arm": ("left_arm_enabled",),
        "right-arm": ("right_arm_enabled",),
        "arms": ("left_arm_enabled", "right_arm_enabled"),
        "all": ("all_enabled",),
    }[part]
    return all(bool(execution.get(flag, False)) for flag in flags)


def build_reset_target(
    current_state: np.ndarray,
    *,
    part: str,
    config: dict[str, object],
) -> np.ndarray:
    current = np.asarray(current_state, dtype=np.float64)
    if current.shape != (ACTION_DIM,) or not np.all(np.isfinite(current)):
        raise ValueError("current_state must be finite 34D")
    if part not in PART_INDICES:
        raise ValueError(f"unsupported reset part {part!r}")
    canonical = np.asarray(config["action_median"], dtype=np.float64)
    target = current.copy()
    indices = np.asarray(PART_INDICES[part], dtype=np.intp)
    target[indices] = canonical[indices]
    clipped, _, max_delta = clip_action_positions(target)
    if max_delta > 1e-9:
        raise ValueError(
            "configured reset target violates physical limits "
            f"(max_delta={max_delta:.6f})"
        )
    return clipped


def build_linear_trajectory(
    start: np.ndarray,
    target: np.ndarray,
    *,
    duration_seconds: float,
    source_hz: float = 30.0,
) -> np.ndarray:
    if duration_seconds <= 0 or source_hz <= 0:
        raise ValueError("duration and source_hz must be positive")
    start_array = np.asarray(start, dtype=np.float64)
    target_array = np.asarray(target, dtype=np.float64)
    if (
        start_array.shape != (ACTION_DIM,)
        or target_array.shape != (ACTION_DIM,)
        or not np.all(np.isfinite(start_array))
        or not np.all(np.isfinite(target_array))
    ):
        raise ValueError("reset trajectory endpoints must be finite 34D")
    intervals = max(1, int(math.ceil(duration_seconds * source_hz)))
    alpha = np.linspace(0.0, 1.0, intervals + 1)[:, None]
    return start_array[None, :] + alpha * (
        target_array - start_array
    )[None, :]


def build_reset_stages(
    current: np.ndarray,
    target: np.ndarray,
    *,
    part: str,
    duration_seconds: float,
) -> list[tuple[str, np.ndarray]]:
    if part != "all":
        return [
            (
                part,
                build_linear_trajectory(
                    current,
                    target,
                    duration_seconds=duration_seconds,
                ),
            )
        ]
    hands_target = np.asarray(current, dtype=np.float64).copy()
    hands_target[14:] = target[14:]
    return [
        (
            "hands",
            build_linear_trajectory(
                current,
                hands_target,
                duration_seconds=duration_seconds,
            ),
        ),
        (
            "arms",
            build_linear_trajectory(
                hands_target,
                target,
                duration_seconds=duration_seconds,
            ),
        ),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--part",
        choices=tuple(PART_INDICES),
        default="left-hand",
    )
    parser.add_argument(
        "--servo-channel",
        choices=("normal", "low-latency"),
        default="normal",
    )
    parser.add_argument("--duration-seconds", type=float, default=8.0)
    parser.add_argument("--max-joint-travel-radians", type=float, default=1.5)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--object-supported", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--gdk-stabilization-seconds", type=float, default=3.0)
    return parser


def _validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    config: dict[str, object],
) -> None:
    if not 2.0 <= args.duration_seconds <= 30.0:
        parser.error("--duration-seconds must be between 2 and 30")
    if args.max_joint_travel_radians <= 0:
        parser.error("--max-joint-travel-radians must be positive")
    if not args.execute:
        return
    if not part_execution_enabled(config, args.part):
        parser.error(
            f"{args.part} execution is locked in reset_poses.json pending "
            "staged G2 validation"
        )
    if args.part in HAND_PARTS and not args.object_supported:
        parser.error(
            "--object-supported is required because hand recovery may "
            "release a held object"
        )


def _preview(current: np.ndarray, target: np.ndarray, part: str) -> float:
    print(f"\nReset part={part}")
    print(f"{'joint':32s} {'current':>10s} {'target':>10s} {'delta':>10s}")
    print("-" * 68)
    maximum = 0.0
    for index in PART_INDICES[part]:
        delta = float(target[index] - current[index])
        maximum = max(maximum, abs(delta))
        print(
            f"{POLICY_JOINT_NAMES[index]:32s} "
            f"{current[index]:10.6f} {target[index]:10.6f} {delta:10.6f}"
        )
    print(f"max_joint_travel={maximum:.6f} rad")
    return maximum


def _play_stage(
    servo: ServoStreamer,
    trajectory: np.ndarray,
) -> None:
    playback_id = servo.play(
        trajectory,
        start_index=0,
        interval_count=len(trajectory) - 1,
        blend_seconds=0.0,
    )
    while servo.is_playing(playback_id):
        if servo.has_fault():
            raise RuntimeError(servo.fault_message() or "servo fault")
        time.sleep(0.01)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_reset_config()
    _validate_args(parser, args, config)
    gdk = None
    gdk_initialized = False
    robot: G2RobotController | None = None
    servo: ServoStreamer | None = None
    try:
        import agibot_gdk as gdk

        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        gdk_initialized = True
        robot = G2RobotController(
            powered=args.execute,
            servo_channel=args.servo_channel,
            control_period=0.01,
            stabilization_seconds=args.gdk_stabilization_seconds,
        )
        current = np.asarray(robot.read_state(), dtype=np.float64)
        clipped_current, clip_count, clip_delta = clip_action_positions(current)
        if clip_delta > 0.02:
            raise RuntimeError(
                "measured state exceeds configured limits by more than 0.02rad"
            )
        if clip_count:
            print(
                f"NOTICE: current hold target clipped {clip_count} values "
                f"(max_delta={clip_delta:.6f})"
            )
        target = build_reset_target(
            clipped_current,
            part=args.part,
            config=config,
        )
        max_travel = _preview(clipped_current, target, args.part)
        if max_travel > args.max_joint_travel_radians:
            raise RuntimeError(
                f"reset travel {max_travel:.6f} exceeds "
                f"{args.max_joint_travel_radians:.6f}rad"
            )
        if not args.execute:
            print("\nPREVIEW ONLY: no servo command was sent.")
            return 0
        if args.servo_channel == "low-latency":
            print(
                "\nWARNING: LOW-LATENCY MODE HAS NO GDK COLLISION PROTECTION."
            )
        confirm_ok_and_countdown(
            f"{args.servo_channel}通道复位{args.part}",
            supplied=args.confirm,
        )
        print("Preflight: exact-current 100 Hz hold")
        robot.send_action(clipped_current)
        servo = ServoStreamer(robot.send_action, clipped_current)
        servo.start()
        time.sleep(0.25)
        if servo.has_fault():
            raise RuntimeError(servo.fault_message() or "hold preflight failed")
        for label, trajectory in build_reset_stages(
            clipped_current,
            target,
            part=args.part,
            duration_seconds=args.duration_seconds,
        ):
            print(f"Executing reset stage: {label}")
            _play_stage(servo, trajectory)
        servo.hold(target)
        time.sleep(0.25)
        print("DONE: reset target reached and held.")
        return 0
    except KeyboardInterrupt:
        print("\nINTERRUPTED: stopping reset; use hardware E-stop if unsafe.")
        return 130
    except Exception:
        LOGGER.exception(
            "Reset failed; do not restart powered VLA until posture is checked"
        )
        return 1
    finally:
        if servo is not None:
            try:
                servo.hold()
                time.sleep(0.05)
            except Exception:
                pass
            servo.close()
        if robot is not None:
            if args.execute:
                robot.emergency_stop()
            robot.close()
        if gdk_initialized and gdk is not None:
            gdk.gdk_release()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(run())


if __name__ == "__main__":
    main()
