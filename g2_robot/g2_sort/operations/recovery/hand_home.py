"""Preview or restore the approved G2 hands-and-arms training Home pose.

Purpose
-------
Provide a recovery path that never treats reboot-time O10 feedback as a hand
command. It is intended for a clean workflow: fully exit VLA, restore the arms,
restore the hands, reset body from the tablet if needed, then start a new VLA run.

Inputs / outputs
----------------
Input is the approved 20D Home JSON and, for execution, a recent last-successful
Split hand command. Preview prints all joints and sends nothing. Execute reuses
the production Split dispatcher: measured arms first move to the H5 frame-0 14D
median at 100 Hz while the hand output gate remains closed; only after arm
completion do O10 hands move to Home in at most eight 5 Hz steps.

Method
------
Validate the frozen order and GDK limits, compare Home with official open-zero
and the H5 frame-0 median, then fail closed unless the record is approved, the
last-command anchor is valid, O10/motion health is good, and the operator gives
explicit authorization. Linearly restore arms through the proven
``joint_servo_control`` path with Split hand output explicitly disabled, then
open the hand gate, re-anchor at the last successful command, and restore hands.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.hand_command_store import (
    HAND_JOINT_NAMES,
    build_hand_command_record,
    load_hand_command_record,
    save_hand_command_record,
)
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES
from g2_sort.deployment.g2_client.robot_control import G2RobotController
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.operations.operator_confirmation import confirm_enter_and_countdown

LOGGER = logging.getLogger(__name__)
DEFAULT_APPROVED_HOME = Path("g2_sort/config/hand_home_approved.json")
DEFAULT_LAST_COMMAND = Path("logs/runtime/last_hand_command.json")
RESET_POSE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "reset_poses.json"


def build_bounded_hand_trajectory(
    start: np.ndarray,
    target: np.ndarray,
    *,
    max_step_radians: float,
    max_intervals: int | None = None,
) -> np.ndarray:
    """Create a convex 20D sequence, optionally capped to fewer intervals."""
    start_values = np.asarray(start, dtype=np.float64)
    target_values = np.asarray(target, dtype=np.float64)
    if (
        start_values.shape != (20,)
        or target_values.shape != (20,)
        or not np.all(np.isfinite(start_values))
        or not np.all(np.isfinite(target_values))
    ):
        raise ValueError("hand trajectory endpoints must be finite 20D")
    if max_step_radians <= 0:
        raise ValueError("max_step_radians must be positive")
    if max_intervals is not None and max_intervals < 1:
        raise ValueError("max_intervals must be positive when provided")
    # Still submit the anchor once, but never wait for an identical second
    # target: the production changed-only worker intentionally skips it.
    if np.array_equal(start_values, target_values):
        return start_values[None, :].copy()
    maximum = float(np.max(np.abs(target_values - start_values)))
    intervals = max(1, int(math.ceil(maximum / max_step_radians)))
    if max_intervals is not None:
        intervals = min(intervals, int(max_intervals))
    alpha = np.linspace(0.0, 1.0, intervals + 1)[:, None]
    return start_values[None, :] + alpha * (
        target_values - start_values
    )[None, :]


def _h5_hand_median() -> np.ndarray:
    payload = json.loads(RESET_POSE_CONFIG.read_text(encoding="utf-8"))
    values = np.asarray(payload["action_median"], dtype=np.float64)[14:]
    if values.shape != (20,) or not np.all(np.isfinite(values)):
        raise ValueError("reset_poses action_median hand section is invalid")
    return values


def _h5_arm_median() -> np.ndarray:
    payload = json.loads(RESET_POSE_CONFIG.read_text(encoding="utf-8"))
    values = np.asarray(payload["action_median"], dtype=np.float64)[:14]
    if values.shape != (14,) or not np.all(np.isfinite(values)):
        raise ValueError("reset_poses action_median arm section is invalid")
    return values


def build_arm_recovery_trajectory(
    start_arms: np.ndarray,
    target_arms: np.ndarray,
    hand_home: np.ndarray,
    *,
    duration_seconds: float,
    source_hz: float = 30.0,
) -> np.ndarray:
    """Build a 34D arm transition with the approved hands held constant."""
    start = np.asarray(start_arms, dtype=np.float64)
    target = np.asarray(target_arms, dtype=np.float64)
    hands = np.asarray(hand_home, dtype=np.float64)
    if (
        start.shape != (14,)
        or target.shape != (14,)
        or hands.shape != (20,)
        or not np.all(np.isfinite(start))
        or not np.all(np.isfinite(target))
        or not np.all(np.isfinite(hands))
    ):
        raise ValueError("arm recovery endpoints must be finite 14D/20D")
    if duration_seconds <= 0 or source_hz <= 0:
        raise ValueError("arm recovery duration and source_hz must be positive")
    intervals = max(1, int(math.ceil(duration_seconds * source_hz)))
    alpha = np.linspace(0.0, 1.0, intervals + 1)[:, None]
    arms = start[None, :] + alpha * (target - start)[None, :]
    return np.concatenate(
        (arms, np.repeat(hands[None, :], intervals + 1, axis=0)),
        axis=1,
    )


def print_home(record: dict[str, object]) -> None:
    """Print the approved hand Home and frozen H5 arm Home without hardware."""
    positions = np.asarray(record["positions"], dtype=np.float64)
    h5_values = _h5_hand_median()
    arm_values = _h5_arm_median()
    print("\n【记录来源】成功返回0且人工确认的Split move_ee_pos命令，不是手指反馈值")
    print(f"run_id={record.get('run_id')}")
    print(f"command_index={record.get('command_index')}")
    print(f"wall_time_utc={record.get('wall_time_utc')}")
    print(f"approved={bool(record.get('approved', False))}")
    print()
    print(f"{'joint':40s} {'home':>11s} {'open_zero':>11s} {'h5_frame0':>11s}")
    print("-" * 78)
    for name, value, h5_value in zip(
        HAND_JOINT_NAMES,
        positions,
        h5_values,
        strict=True,
    ):
        print(f"{name:40s} {value:11.6f} {0.0:11.6f} {h5_value:11.6f}")
    print()
    print(f"max_abs_from_official_open_zero={np.max(np.abs(positions)):.6f}rad")
    print(f"max_delta_from_h5_frame0={np.max(np.abs(positions-h5_values)):.6f}rad")
    print("【拇指重点】")
    for index in (0, 1, 2, 10, 11, 12):
        print(f"  {HAND_JOINT_NAMES[index]}={positions[index]:.6f}rad")
    if np.max(np.abs(positions)) < 0.02:
        print("【Home无效】记录接近O10官方全零开手命令，拒绝将其用于复位。")
    else:
        print("【Home通过】20D、顺序、finite、GDK限位和人工批准标记均有效。")
    print("\n【双臂H5第0帧14D目标】")
    for name, value in zip(POLICY_JOINT_NAMES[:14], arm_values, strict=True):
        print(f"  {name:40s} {value:11.6f}rad")
    print("【只读预览】本次没有初始化GDK，也没有发送动作。")


def _wait_for_split_hand_target(
    controller: G2RobotController,
    servo: ServoStreamer,
    command_path: Path,
    target: np.ndarray,
    *,
    previous_send_count: int,
    timeout_seconds: float = 2.0,
) -> None:
    """Wait until the production 5 Hz worker acknowledges this exact target."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if servo.has_fault():
            raise RuntimeError(
                f"Split Servo故障：{servo.fault_message() or 'unknown'}"
            )
        stats = controller.control_stats()
        if int(stats["hand_send_count"]) > previous_send_count:
            try:
                saved = load_hand_command_record(command_path)
            except (FileNotFoundError, ValueError, json.JSONDecodeError):
                saved = None
            if saved is not None and np.allclose(
                np.asarray(saved["positions"], dtype=np.float64),
                target,
                rtol=0.0,
                atol=1e-9,
            ):
                return
        time.sleep(0.01)
    raise RuntimeError("等待Split双手5Hz确认目标超时")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home-json",
        "--candidate-json",
        dest="home_json",
        type=Path,
        default=DEFAULT_APPROVED_HOME,
        help="已批准Home文件；--candidate-json仅作为旧命令兼容别名",
    )
    parser.add_argument(
        "--approved-home-json",
        type=Path,
        default=DEFAULT_APPROVED_HOME,
    )
    parser.add_argument(
        "--last-command-json",
        type=Path,
        default=DEFAULT_LAST_COMMAND,
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="", help="旧参数仅兼容解析；执行仍必须现场按ENTER")
    parser.add_argument("--max-step-radians", type=float, default=0.03)
    parser.add_argument("--hand-recovery-steps", type=int, default=8)
    parser.add_argument("--arm-recovery-seconds", type=float, default=5.0)
    parser.add_argument("--max-arm-travel-radians", type=float, default=1.5)
    parser.add_argument("--gdk-stabilization-seconds", type=float, default=10.0)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_step_radians <= 0 or args.max_step_radians > 0.05:
        parser.error("--max-step-radians must be in (0, 0.05]")
    if args.hand_recovery_steps < 1 or args.hand_recovery_steps > 20:
        parser.error("--hand-recovery-steps must be between 1 and 20")
    if args.arm_recovery_seconds < 2.0 or args.arm_recovery_seconds > 15.0:
        parser.error("--arm-recovery-seconds must be between 2 and 15")
    if args.max_arm_travel_radians <= 0:
        parser.error("--max-arm-travel-radians must be positive")
    if args.gdk_stabilization_seconds < 0:
        parser.error("--gdk-stabilization-seconds cannot be negative")
    if not args.execute:
        if not args.home_json.exists():
            parser.error(
                f"已批准Home文件不存在：{args.home_json}"
            )
        home = load_hand_command_record(args.home_json)
        if not bool(home.get("approved", False)):
            parser.error("Home记录未标记approved=true，仅允许预览已批准Home")
        print_home(home)
        return 0
    if not args.approved_home_json.exists():
        parser.error(f"已批准Home文件不存在：{args.approved_home_json}")
    if not args.last_command_json.exists():
        parser.error(
            f"最后成功命令不存在：{args.last_command_json}；先完整运行并退出一次VLA"
        )

    home = load_hand_command_record(args.approved_home_json)
    if not bool(home.get("approved", False)):
        parser.error("approved Home记录未标记approved=true")
    latest = load_hand_command_record(args.last_command_json)
    start = np.asarray(latest["positions"], dtype=np.float64)
    target = np.asarray(home["positions"], dtype=np.float64)
    trajectory = build_bounded_hand_trajectory(
        start,
        target,
        max_step_radians=args.max_step_radians,
        max_intervals=args.hand_recovery_steps,
    )
    maximum_travel = float(np.max(np.abs(target - start)))
    print(
        "【复位计划】先重发最后成功命令作为锚点，再执行"
        f"{len(trajectory) - 1}步（上限{args.hand_recovery_steps}步）；"
        f"最大总行程={maximum_travel:.6f}rad",
        flush=True,
    )
    import agibot_gdk as gdk

    initialized = False
    controller: G2RobotController | None = None
    servo: ServoStreamer | None = None
    try:
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError("gdk_init failed")
        initialized = True
        controller = G2RobotController(
            powered=True,
            control_period=0.01,
            servo_channel="low-latency",
            servo_output_mode="split",
            stabilization_seconds=args.gdk_stabilization_seconds,
            max_send_seconds=0.2,
            enforce_low_latency_step_limit=False,
            last_hand_command_path=args.last_command_json,
            hand_command_run_id=f"hand-home-{int(time.time())}",
            hand_startup_delay_seconds=0.08,
            split_hand_output_enabled=False,
            gdk_module=gdk,
        )
        measured = np.asarray(controller.read_state(), dtype=np.float64)
        arm_hold = measured[:14].copy()
        arm_home = _h5_arm_median()
        arm_delta = arm_home - arm_hold
        max_arm_travel = float(np.max(np.abs(arm_delta)))
        max_arm_index = int(np.argmax(np.abs(arm_delta)))
        print(
            "【双臂计划】"
            f"最大行程={max_arm_travel:.6f}rad，"
            f"关节={POLICY_JOINT_NAMES[max_arm_index]}，"
            f"持续={args.arm_recovery_seconds:.1f}秒",
            flush=True,
        )
        if max_arm_travel > args.max_arm_travel_radians:
            raise RuntimeError(
                f"双臂复位行程{max_arm_travel:.6f}rad超过门限"
                f"{args.max_arm_travel_radians:.6f}rad，未发送动作"
            )
        confirm_enter_and_countdown(
            "Split先将双臂恢复到H5初始姿态，再复位双手",
        )
        print(
            "【Split已就绪】手部输出闸门保持关闭；先让双臂以100Hz回H5"
            "初始姿态，确认完成后再打开闸门复位双手。",
            flush=True,
        )
        servo = ServoStreamer(
            controller.send_action,
            None,
            max_tick_lateness_seconds=0.2,
            smoothstep_blend=False,
        )
        servo.start()
        arm_trajectory = build_arm_recovery_trajectory(
            arm_hold,
            arm_home,
            start,
            duration_seconds=args.arm_recovery_seconds,
        )
        print("【双臂复位开始】手部输出闸门关闭，不发送O10命令。", flush=True)
        playback_id = servo.play(
            arm_trajectory,
            start_index=0,
            interval_count=len(arm_trajectory) - 1,
            blend_seconds=0.0,
            anchor=np.concatenate((arm_hold, start)),
        )
        while servo.is_playing(playback_id):
            if servo.has_fault():
                raise RuntimeError(
                    f"双臂复位Servo故障：{servo.fault_message() or 'unknown'}"
                )
            time.sleep(0.01)
        servo.hold(np.concatenate((arm_home, start)))
        if servo.has_fault():
            raise RuntimeError(
                f"双臂最终Home保持故障：{servo.fault_message() or 'unknown'}"
            )
        print("【双臂复位完成】现在打开手部输出闸门。", flush=True)
        controller.enable_split_hand_output()
        for step, positions in enumerate(trajectory):
            phase = "最后成功命令锚点" if step == 0 else f"复位第{step}步"
            before = int(controller.control_stats()["hand_send_count"])
            servo.hold(np.concatenate((arm_home, positions)))
            _wait_for_split_hand_target(
                controller,
                servo,
                args.last_command_json,
                positions,
                previous_send_count=before,
            )
            print(
                f"【{phase}已确认】"
                f"max_from_start={np.max(np.abs(positions-start)):.6f}rad",
                flush=True,
            )
        final_target = np.concatenate((arm_home, target))
        servo.hold(final_target)
        time.sleep(0.25)
        if servo.has_fault():
            raise RuntimeError(
                f"最终Home保持故障：{servo.fault_message() or 'unknown'}"
            )
        servo.stop(timeout=5.0)
        servo = None
        reported = np.asarray(controller.read_state(), dtype=np.float64)[14:]
        print("【复位后原始反馈对照】以下为reported位置，不是接触力或认证到位反馈：", flush=True)
        for side, offset in (("left", 0), ("right", 10)):
            values = reported[offset:offset + 10]
            expected = target[offset:offset + 10]
            print(f"{side} target={expected.tolist()} reported={values.tolist()} "
                  f"max_error_rad={float(np.max(np.abs(values - expected))):.6f}", flush=True)
        controller.close()
        controller = None
        saved = build_hand_command_record(
            target,
            kind="last_success",
            run_id=f"hand-home-{int(time.time())}",
            command_index=len(trajectory),
            source="hand_home_recovery_successful_move_ee_pos",
        )
        save_hand_command_record(
            args.last_command_json,
            saved,
            overwrite=True,
        )
        print("【复位命令完成】SDK已确认批准Home目标；请核对双手实际打开及上方反馈。SDK确认不等于物理到位。")
        return 0
    except Exception:
        LOGGER.exception("灵巧手Home复位失败；必要时使用物理急停")
        return 1
    finally:
        if servo is not None:
            servo.stop(timeout=5.0)
        if controller is not None:
            controller.close()
        if initialized:
            gdk.gdk_release()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(run())


if __name__ == "__main__":
    main()
