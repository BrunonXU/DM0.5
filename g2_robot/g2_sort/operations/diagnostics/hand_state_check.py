"""G2 O10双手原始状态只读检查。

用途：确认GDK状态接口返回的双手名称、位置和健康字段是否连续可信。
输入：``get_end_state``、``get_joint_states``、whole-body及motion只读结果。
输出：中文摘要和包含全部原始采样的JSON报告。
方法：连续采样左右手各10个关节，检查名称、finite、enable、status、err_code和
静止波动；本模块不构造任何控制请求，也绝不发送Servo。
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from g2_sort.deployment.g2_client.joint_layout import (
    LEFT_HAND_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)

EXPECTED_NAMES = {
    "left": LEFT_HAND_JOINT_NAMES,
    "right": RIGHT_HAND_JOINT_NAMES,
}
SIDE_LABELS = {"left": "左手", "right": "右手"}


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


def _analyse_side(
    snapshots: Sequence[Mapping[str, Any]],
    side: str,
) -> dict[str, object]:
    expected = EXPECTED_NAMES[side]
    issues: list[str] = []
    controlled_values: list[bool | None] = []
    rows: dict[str, dict[str, list[object]]] = {
        name: {
            "positions": [],
            "enable": [],
            "status": [],
            "err_code": [],
        }
        for name in expected
    }
    observed_name_lists: list[list[str]] = []

    for sample_index, snapshot in enumerate(snapshots):
        payload = snapshot.get(f"{side}_end_state")
        if not isinstance(payload, Mapping):
            issues.append(f"sample {sample_index}: missing {side}_end_state")
            continue
        controlled = payload.get("controlled")
        controlled_values.append(
            bool(controlled) if isinstance(controlled, bool) else None
        )
        if not isinstance(controlled, bool):
            issues.append(f"sample {sample_index}: controlled is missing/invalid")
        names = payload.get("names")
        states = payload.get("end_states")
        if (
            not isinstance(names, Sequence)
            or isinstance(names, (str, bytes))
            or not isinstance(states, Sequence)
            or isinstance(states, (str, bytes))
        ):
            issues.append(f"sample {sample_index}: names/end_states is invalid")
            continue
        observed_names = [str(name).strip() for name in names]
        observed_name_lists.append(observed_names)
        if tuple(observed_names) != expected:
            issues.append(
                f"sample {sample_index}: joint names/order differs from O10 contract"
            )
        if len(observed_names) != len(states):
            issues.append(
                f"sample {sample_index}: names/state length "
                f"{len(observed_names)} != {len(states)}"
            )
            continue
        if len(set(observed_names)) != len(observed_names):
            issues.append(f"sample {sample_index}: duplicate joint names")
            continue

        state_by_name = dict(zip(observed_names, states, strict=True))
        for name in expected:
            entry = state_by_name.get(name)
            if not isinstance(entry, Mapping):
                issues.append(f"sample {sample_index}: missing {name}")
                continue
            try:
                position = float(entry["position"])
            except (KeyError, TypeError, ValueError):
                issues.append(f"sample {sample_index}: invalid position for {name}")
            else:
                if np.isfinite(position):
                    rows[name]["positions"].append(position)
                else:
                    issues.append(
                        f"sample {sample_index}: non-finite position for {name}"
                    )
            for field in ("enable", "status", "err_code"):
                if field not in entry:
                    issues.append(
                        f"sample {sample_index}: {name} missing {field}"
                    )
                else:
                    rows[name][field].append(_jsonable(entry[field]))

    joints: list[dict[str, object]] = []
    maximum_span = 0.0
    for name in expected:
        positions = [float(value) for value in rows[name]["positions"]]
        span = (
            float(max(positions) - min(positions))
            if positions
            else float("nan")
        )
        if np.isfinite(span):
            maximum_span = max(maximum_span, span)
        enable_values = rows[name]["enable"]
        error_values = rows[name]["err_code"]
        if enable_values and not all(value is True for value in enable_values):
            issues.append(f"{name}: enable is not always True")
        if error_values:
            try:
                if any(int(value) != 0 for value in error_values):
                    issues.append(f"{name}: non-zero err_code")
            except (TypeError, ValueError):
                issues.append(f"{name}: invalid err_code")
        joints.append(
            {
                "name": name,
                "first_position": positions[0] if positions else None,
                "last_position": positions[-1] if positions else None,
                "minimum_position": min(positions) if positions else None,
                "maximum_position": max(positions) if positions else None,
                "span_radians": span if np.isfinite(span) else None,
                "enable_values": enable_values,
                "status_values": rows[name]["status"],
                "err_code_values": error_values,
            }
        )

    return {
        "controlled_values": controlled_values,
        "observed_name_lists": observed_name_lists,
        "expected_names": list(expected),
        "maximum_stationary_span_radians": maximum_span,
        "joints": joints,
        "issues": list(dict.fromkeys(issues)),
    }


def analyse_end_state_samples(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Analyse raw samples without importing GDK or issuing commands."""
    if not snapshots:
        raise ValueError("at least one end-state snapshot is required")
    sides = {
        side: _analyse_side(snapshots, side)
        for side in ("left", "right")
    }
    issues = [
        f"{SIDE_LABELS[side]}: {issue}"
        for side, report in sides.items()
        for issue in report["issues"]
    ]
    return {
        "sample_count": len(snapshots),
        "sides": sides,
        "issues": issues,
        "structural_result": (
            "DATA_FIELDS_OK" if not issues else "REVIEW_REQUIRED"
        ),
    }


def _print_side(side: str, report: Mapping[str, Any]) -> None:
    label = SIDE_LABELS[side]
    controlled = report["controlled_values"]
    print(
        f"\n【{label}】controlled采样={controlled} "
        f"最大静止波动={float(report['maximum_stationary_span_radians']):.6f}rad"
    )
    print(
        f"{'关节':38s} {'末帧位置':>10s} {'波动':>10s} "
        f"{'enable':>10s} {'status':>10s} {'err':>8s}"
    )
    print("-" * 96)
    for joint in report["joints"]:
        last = joint["last_position"]
        span = joint["span_radians"]
        last_text = f"{float(last):.6f}" if last is not None else "None"
        span_text = f"{float(span):.6f}" if span is not None else "None"
        enable_text = str(joint["enable_values"][-1:])
        status_text = str(joint["status_values"][-1:])
        error_text = str(joint["err_code_values"][-1:])
        print(
            f"{joint['name']:38s} {last_text:>10s} {span_text:>10s} "
            f"{enable_text:>10s} {status_text:>10s} {error_text:>8s}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument("--stabilization-seconds", type=float, default=10.0)
    args = parser.parse_args()
    if not 3 <= args.samples <= 100:
        parser.error("--samples must be between 3 and 100")
    if not 0.05 <= args.interval_seconds <= 2.0:
        parser.error("--interval-seconds must be between 0.05 and 2")
    if args.stabilization_seconds < 0:
        parser.error("--stabilization-seconds cannot be negative")

    import agibot_gdk as gdk

    args.output_dir.mkdir(parents=True, exist_ok=True)
    initialized = False
    try:
        print("【只读 1/4】初始化GDK并等待DDS稳定……", flush=True)
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = gdk.Robot()
        time.sleep(args.stabilization_seconds)

        print("【只读 2/4】读取全身、运动和body关节状态……", flush=True)
        whole = robot.get_whole_body_status()
        motion = robot.get_motion_control_status()
        body = robot.get_joint_states()
        if not isinstance(whole, Mapping):
            raise RuntimeError(
                "get_whole_body_status returned a non-mapping payload"
            )

        print(
            f"【只读 3/4】连续读取双手{args.samples}次；"
            "期间不会发送任何动作……",
            flush=True,
        )
        snapshots: list[Mapping[str, Any]] = []
        for sample_index in range(args.samples):
            payload = robot.get_end_state()
            if not isinstance(payload, Mapping):
                raise RuntimeError("get_end_state returned a non-mapping payload")
            snapshots.append(payload)
            if sample_index + 1 < args.samples:
                time.sleep(args.interval_seconds)

        analysis = analyse_end_state_samples(snapshots)
        body_states = (
            body.get("states", [])
            if isinstance(body, Mapping)
            else []
        )
        body_names = [
            str(entry.get("name", "")).strip()
            for entry in body_states
            if isinstance(entry, Mapping)
        ]
        hand_names_in_body = [
            name
            for name in LEFT_HAND_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
            if name in body_names
        ]
        report = {
            "generated_at_unix": time.time(),
            "read_only": True,
            "servo_commands_sent": 0,
            "motion": {
                "mode": _jsonable(getattr(motion, "mode", None)),
                "error_code": _jsonable(
                    getattr(motion, "error_code", None)
                ),
                "error_msg": _jsonable(
                    getattr(motion, "error_msg", None)
                ),
                "collision_pairs_1": _jsonable(
                    list(getattr(motion, "collision_pairs_1", []) or [])
                ),
                "collision_pairs_2": _jsonable(
                    list(getattr(motion, "collision_pairs_2", []) or [])
                ),
            },
            "whole_body": _jsonable(whole),
            "get_joint_states": {
                "reported_count": (
                    body.get("nums") if isinstance(body, Mapping) else None
                ),
                "names": body_names,
                "o10_hand_names_present": hand_names_in_body,
            },
            "analysis": analysis,
            "raw_end_state_samples": _jsonable(snapshots),
        }
        report_path = args.output_dir / "hand_state.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        print("【只读 4/4】状态采样完成。")
        print(
            "whole-body末端："
            f"left={whole.get('left_end_model')!r}/"
            f"error={whole.get('left_end_error')!r} "
            f"right={whole.get('right_end_model')!r}/"
            f"error={whole.get('right_end_error')!r}"
        )
        print(
            "get_joint_states中出现的O10手指数="
            f"{len(hand_names_in_body)}/20；"
            "手指正常应主要来自get_end_state。"
        )
        for side in ("left", "right"):
            _print_side(side, analysis["sides"][side])
        if analysis["issues"]:
            print("\n【需要复核】检测到以下状态字段问题：")
            for issue in analysis["issues"]:
                print(f"  - {issue}")
        else:
            print("\n【数据字段通过】名称、位置和健康字段结构未发现异常。")
        print("【重要】这只是只读结果，不代表Servo或Powered已经通过。")
        print(f"【报告位置】{report_path}")
        return 0 if not analysis["issues"] else 2
    finally:
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    raise SystemExit(main())
