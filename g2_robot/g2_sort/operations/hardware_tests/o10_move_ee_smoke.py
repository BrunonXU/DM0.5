"""G2 O10官方move_ee_pos单次当前位置验证。

用途：隔离验证O10灵巧手专用控制接口，不经过正式VLA或双臂Servo路径。
输入：left/right/dual侧别和当前位置反馈。
输出：请求摘要和JSON报告；``--execute``由安全锁直接拒绝。
方法：严格校验O10名称与健康字段并构造请求预览，不调用``move_ee_pos``。
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_HAND_JOINT_NAMES,
    POLICY_JOINT_LIMITS,
    RIGHT_HAND_JOINT_NAMES,
)
from g2_sort.operations.operator_confirmation import confirm_ok_and_countdown

LOGGER = logging.getLogger(__name__)
EXECUTION_ENABLED = False
SIDE_NAMES = {
    "left": LEFT_HAND_JOINT_NAMES,
    "right": RIGHT_HAND_JOINT_NAMES,
}
SIDE_LABELS = {"left": "左手", "right": "右手", "dual": "双手"}
SIDE_GROUPS = {
    "left": "left_tool",
    "right": "right_tool",
    "dual": "dual_tool",
}
SIDE_LIMITS = {
    "left": POLICY_JOINT_LIMITS[14:24],
    "right": POLICY_JOINT_LIMITS[24:34],
}


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def read_o10_positions(
    payload: Mapping[str, Any],
    side: str,
) -> np.ndarray:
    """Return one healthy O10 side in the frozen official joint order."""
    expected_names = SIDE_NAMES[side]
    side_payload = payload.get(f"{side}_end_state")
    if not isinstance(side_payload, Mapping):
        raise RuntimeError(f"{SIDE_LABELS[side]}状态缺失")
    if side_payload.get("controlled") is not True:
        raise RuntimeError(f"{SIDE_LABELS[side]}controlled不是True")
    names = side_payload.get("names")
    states = side_payload.get("end_states")
    if (
        not isinstance(names, Sequence)
        or isinstance(names, (str, bytes))
        or not isinstance(states, Sequence)
        or isinstance(states, (str, bytes))
    ):
        raise RuntimeError(f"{SIDE_LABELS[side]}names/end_states结构无效")
    observed_names = tuple(str(name).strip() for name in names)
    if observed_names != expected_names:
        raise RuntimeError(
            f"{SIDE_LABELS[side]}关节名称或顺序不符合O10合同"
        )
    if len(states) != len(expected_names):
        raise RuntimeError(
            f"{SIDE_LABELS[side]}需要10个状态，实际{len(states)}个"
        )
    positions: list[float] = []
    for name, state in zip(expected_names, states, strict=True):
        if not isinstance(state, Mapping):
            raise RuntimeError(f"{name}状态不是mapping")
        if state.get("enable") is not True:
            raise RuntimeError(f"{name} enable不是True")
        try:
            error_code = int(state["err_code"])
            position = float(state["position"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{name}位置或错误码无效") from exc
        if error_code != 0:
            raise RuntimeError(f"{name} err_code={error_code}")
        if not np.isfinite(position):
            raise RuntimeError(f"{name}位置不是finite")
        positions.append(position)
    array = np.asarray(positions, dtype=np.float64)
    limits = SIDE_LIMITS[side]
    if np.any(array < limits[:, 0]) or np.any(array > limits[:, 1]):
        raise RuntimeError(f"{SIDE_LABELS[side]}当前位置超出GDK 2.6.3限位")
    return array


def build_move_ee_request(
    gdk: Any,
    *,
    side: str,
    left_positions: np.ndarray,
    right_positions: np.ndarray,
) -> tuple[Any, np.ndarray, tuple[str, ...]]:
    """Build the documented O10 request without sending it."""
    if side == "left":
        positions = np.asarray(left_positions, dtype=np.float64)
        names = LEFT_HAND_JOINT_NAMES
    elif side == "right":
        positions = np.asarray(right_positions, dtype=np.float64)
        names = RIGHT_HAND_JOINT_NAMES
    elif side == "dual":
        positions = np.concatenate((left_positions, right_positions))
        names = LEFT_HAND_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
    else:
        raise ValueError(f"unsupported side {side!r}")
    expected_size = 20 if side == "dual" else 10
    if positions.shape != (expected_size,) or not np.all(np.isfinite(positions)):
        raise RuntimeError(
            f"{side} move_ee_pos target must be finite {expected_size}D"
        )
    request = gdk.JointStates()
    request.group = SIDE_GROUPS[side]
    request.target_type = "o10_t2"
    states = []
    for value in positions.tolist():
        state = gdk.JointState()
        state.position = float(value)
        states.append(state)
    request.states = states
    request.nums = len(states)
    return request, positions, names


def _selected_feedback(
    payload: Mapping[str, Any],
    side: str,
) -> tuple[np.ndarray, tuple[str, ...]]:
    left = read_o10_positions(payload, "left")
    right = read_o10_positions(payload, "right")
    if side == "left":
        return left, LEFT_HAND_JOINT_NAMES
    if side == "right":
        return right, RIGHT_HAND_JOINT_NAMES
    return (
        np.concatenate((left, right)),
        LEFT_HAND_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES,
    )


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--side",
        choices=("left", "right", "dual"),
        default="left",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--stabilization-seconds", type=float, default=10.0)
    parser.add_argument("--feedback-delay-seconds", type=float, default=0.5)
    parser.add_argument("--max-drift-radians", type=float, default=0.05)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute and not EXECUTION_ENABLED:
        parser.error(
            "O10 move_ee_pos execute已软件锁定：重启后的零反馈回发会执行"
            "官方开手命令并让拇指靠近相机；当前只允许预览"
        )
    if args.stabilization_seconds < 0:
        parser.error("--stabilization-seconds cannot be negative")
    if not 0.2 <= args.feedback_delay_seconds <= 2.0:
        parser.error("--feedback-delay-seconds must be between 0.2 and 2")
    if not 0.01 <= args.max_drift_radians <= 0.20:
        parser.error("--max-drift-radians must be between 0.01 and 0.20")

    import agibot_gdk as gdk

    initialized = False
    report: dict[str, object] = {
        "read_only": not args.execute,
        "side": args.side,
        "commands_sent": 0,
    }
    try:
        print("【O10验证 1/4】初始化GDK并等待DDS稳定……", flush=True)
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = gdk.Robot()
        time.sleep(args.stabilization_seconds)

        print("【O10验证 2/4】严格读取双手当前位置和健康状态……", flush=True)
        whole = robot.get_whole_body_status()
        motion = robot.get_motion_control_status()
        if not isinstance(whole, Mapping):
            raise RuntimeError("get_whole_body_status返回值无效")
        if (
            whole.get("left_end_model") != "o10_t2"
            or whole.get("right_end_model") != "o10_t2"
            or int(whole.get("left_end_error", -1)) != 0
            or int(whole.get("right_end_error", -1)) != 0
        ):
            raise RuntimeError(f"O10 whole-body状态异常: {whole}")
        if (
            int(getattr(motion, "mode", -1)) != 5
            or int(getattr(motion, "error_code", -1)) != 0
        ):
            raise RuntimeError(
                "G2运动状态异常: "
                f"mode={getattr(motion, 'mode', None)} "
                f"error={getattr(motion, 'error_code', None)}"
            )
        before_payload = robot.get_end_state()
        if not isinstance(before_payload, Mapping):
            raise RuntimeError("get_end_state返回值无效")
        left = read_o10_positions(before_payload, "left")
        right = read_o10_positions(before_payload, "right")
        request, target, names = build_move_ee_request(
            gdk,
            side=args.side,
            left_positions=left,
            right_positions=right,
        )
        report.update(
            {
                "group": request.group,
                "target_type": request.target_type,
                "nums": request.nums,
                "joint_names": list(names),
                "target_positions": target.tolist(),
                "before_end_state": _jsonable(before_payload),
            }
        )

        print(
            f"【请求预览】侧别={SIDE_LABELS[args.side]} "
            f"group={request.group} target_type={request.target_type} "
            f"nums={request.nums}"
        )
        for name, value in zip(names, target, strict=True):
            print(f"  {name}: {float(value): .6f}")
        if not args.execute:
            report["result"] = "PREVIEW_ONLY"
            _write_report(args.report_path, report)
            print("【仅预览】没有调用move_ee_pos，没有发送任何动作。")
            print(f"【报告位置】{args.report_path}")
            return 0

        print(
            "【警告】下一步只调用一次官方move_ee_pos，目标等于当前反馈；"
            "专人握住物理急停。"
        )
        confirm_ok_and_countdown(
            f"O10 {SIDE_LABELS[args.side]}单次当前位置验证",
            supplied=args.confirm,
        )
        print("【O10验证 3/4】发送一次当前位置请求……", flush=True)
        started_at = time.monotonic()
        result = robot.move_ee_pos(request)
        call_seconds = time.monotonic() - started_at
        report["commands_sent"] = 1
        report["call_seconds"] = call_seconds
        report["return_code"] = result
        if result != 0:
            raise RuntimeError(f"move_ee_pos returned non-zero status {result}")

        time.sleep(args.feedback_delay_seconds)
        after_payload = robot.get_end_state()
        if not isinstance(after_payload, Mapping):
            raise RuntimeError("发送后get_end_state返回值无效")
        feedback, feedback_names = _selected_feedback(after_payload, args.side)
        delta = np.abs(feedback - target)
        maximum_index = int(np.argmax(delta))
        maximum_drift = float(delta[maximum_index])
        maximum_joint = feedback_names[maximum_index]
        report.update(
            {
                "after_end_state": _jsonable(after_payload),
                "feedback_positions": feedback.tolist(),
                "absolute_deltas": delta.tolist(),
                "max_drift_radians": maximum_drift,
                "max_drift_joint": maximum_joint,
            }
        )
        print(
            "【O10验证 4/4】"
            f"调用耗时={call_seconds:.6f}s "
            f"最大漂移={maximum_drift:.6f}rad "
            f"关节={maximum_joint}"
        )
        if maximum_drift > args.max_drift_radians:
            report["result"] = "FAIL_DRIFT"
            _write_report(args.report_path, report)
            print(
                f"【失败】漂移超过{args.max_drift_radians:.3f}rad；"
                "不要测试另一侧、dual、复位或Powered。"
            )
            return 2
        report["result"] = "PASS_SINGLE_MOVE_EE"
        _write_report(args.report_path, report)
        print("【通过】单次move_ee_pos没有检测到超限漂移。")
        print("【仍然禁止】这不代表100Hz或正式Powered已经通过。")
        print(f"【报告位置】{args.report_path}")
        return 0
    except KeyboardInterrupt:
        report["result"] = "INTERRUPTED"
        _write_report(args.report_path, report)
        print("\n【已中断】不要继续后续动作测试。")
        return 130
    except Exception as exc:
        report["result"] = "ERROR"
        report["error"] = repr(exc)
        _write_report(args.report_path, report)
        LOGGER.exception("O10 move_ee_pos验证失败")
        print("【失败】不要继续另一侧、dual、复位或Powered。")
        print(f"【报告位置】{args.report_path}")
        return 1
    finally:
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
