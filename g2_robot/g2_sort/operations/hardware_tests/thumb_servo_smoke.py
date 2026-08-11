"""G2拇指方向微动实验。

用途：保留历史白名单拇指关节方向实验的只读预览。
输入：指定拇指关节、最大0.015rad偏移和可选ok确认。
输出：dry-run目标；``--execute``由安全锁直接拒绝。
方法：只计算候选偏移，不发送手指或统一34D Servo命令。
"""

from __future__ import annotations

import argparse
import math
import time

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import (
    POLICY_JOINT_NAMES,
    POLICY_JOINT_LIMITS,
)
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

JOINTS = {
    "left-thumb-roll": ("idx31_hand_l_thumb_roll_joint", -0.010),
    "left-thumb-abad": ("idx32_hand_l_thumb_abad_joint", 0.010),
    "left-thumb-mcp": ("idx33_hand_l_thumb_mcp_joint", -0.015),
    "right-thumb-roll": ("idx71_hand_r_thumb_roll_joint", 0.010),
    "right-thumb-abad": ("idx72_hand_r_thumb_abad_joint", -0.010),
    "right-thumb-mcp": ("idx73_hand_r_thumb_mcp_joint", 0.015),
}
EXECUTION_ENABLED = False


def _move(
    robot: G2RobotController,
    start: np.ndarray,
    target: np.ndarray,
    duration: float,
) -> None:
    count = max(2, round(duration * 100))
    started = time.monotonic()
    for index in range(count):
        phase = index / (count - 1)
        blend = 0.5 - 0.5 * math.cos(math.pi * phase)
        robot.send_action(start + blend * (target - start))
        delay = started + (index + 1) * 0.01 - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joint", choices=tuple(JOINTS), required=True)
    parser.add_argument(
        "--servo-channel",
        choices=("normal", "low-latency"),
        default="normal",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--duration-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if not 1.0 <= args.duration_seconds <= 5.0:
        parser.error("--duration-seconds must be between 1 and 5")
    if args.execute and not EXECUTION_ENABLED:
        parser.error(
            "拇指微动execute已软件锁定：当前只允许通过正式split控制链路"
            "进行真机动作"
        )
    if args.execute and args.servo_channel == "low-latency":
        parser.error(
            "thumb smoke defaults to collision-protected normal mode; "
            "low-latency hardware smoke is intentionally disabled"
        )

    import agibot_gdk as gdk

    initialized = False
    robot = None
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = G2RobotController(
            powered=args.execute,
            servo_channel=args.servo_channel,
            stabilization_seconds=3.0,
        )
        current = np.asarray(robot.read_state(), dtype=np.float64)
        joint_name, delta = JOINTS[args.joint]
        joint_index = POLICY_JOINT_NAMES.index(joint_name)
        target = current.copy()
        target[joint_index] += delta
        lower, upper = POLICY_JOINT_LIMITS[joint_index]
        if not lower <= target[joint_index] <= upper:
            raise RuntimeError("smoke target would exceed the joint limit")
        print(
            f"joint={joint_name} current={current[joint_index]:.6f} "
            f"target={target[joint_index]:.6f} delta={delta:.6f}"
        )
        if not args.execute:
            print("DRY RUN: no servo command was sent.")
            return
        confirm_ok_and_countdown(
            f"拇指微动{args.joint}",
            supplied=args.confirm,
        )
        _move(robot, current, target, args.duration_seconds)
        time.sleep(0.25)
        _move(robot, target, current, args.duration_seconds)
        print("PASS: full-control-group thumb round trip completed.")
    finally:
        if robot is not None:
            if args.execute:
                robot.emergency_stop()
            robot.close()
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    main()
