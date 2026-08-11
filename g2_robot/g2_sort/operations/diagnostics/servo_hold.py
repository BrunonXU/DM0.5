"""G2当前位置Servo hold只读诊断。

用途：预览历史current-hold输入，不执行真实动作。
输入：通道、持续时间、execute开关和可选ok确认。
输出：当前34D与裁剪信息；``--execute``由安全锁直接拒绝。
方法：只读状态。重启O10零反馈不能作为物理保持命令回发。
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import clip_action_positions
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

EXECUTION_ENABLED = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--servo-channel",
        choices=("normal", "low-latency"),
        default="normal",
    )
    parser.add_argument("--duration-seconds", type=float, default=2.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not 0.5 <= args.duration_seconds <= 5.0:
        parser.error("--duration-seconds must be between 0.5 and 5")
    if args.execute and not EXECUTION_ENABLED:
        parser.error(
            "当前位置Servo hold执行已软件锁定：G2重启后的O10零反馈是"
            "官方开手命令，曾在模型动作下发前使双拇指伸向相机"
        )
    import agibot_gdk as gdk

    initialized = False
    robot = None
    servo = None
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = G2RobotController(
            powered=args.execute,
            servo_channel=args.servo_channel,
            stabilization_seconds=10.0,
        )
        measured = np.asarray(robot.read_state(), dtype=np.float64)
        target, clip_count, clip_delta = clip_action_positions(measured)
        print(
            f"channel={args.servo_channel} clip_count={clip_count} "
            f"max_clip_delta={clip_delta:.6f}"
        )
        if clip_delta > 0.02:
            raise RuntimeError("measured target exceeds limits by more than 0.02rad")
        if not args.execute:
            print("PREVIEW ONLY: no servo command was sent.")
            return
        if args.servo_channel == "low-latency":
            print("WARNING: LOW-LATENCY HAS NO COLLISION PROTECTION.")
        confirm_ok_and_countdown(
            f"{args.servo_channel}当前位置Servo hold",
            supplied=args.confirm,
        )
        robot.send_action(target)
        servo = ServoStreamer(robot.send_action, target)
        servo.start()
        deadline = time.monotonic() + args.duration_seconds
        while time.monotonic() < deadline:
            if servo.has_fault():
                raise RuntimeError(servo.fault_message() or "servo fault")
            time.sleep(0.01)
        feedback = np.asarray(robot.read_state(), dtype=np.float64)
        drift = float(np.max(np.abs(feedback - target)))
        print(
            f"PASS: hold complete drift={drift:.6f} "
            f"servo={servo.stats()} control={robot.control_stats()}"
        )
    finally:
        if servo is not None:
            servo.close()
        if robot is not None:
            if args.execute:
                robot.emergency_stop()
            robot.close()
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    main()
