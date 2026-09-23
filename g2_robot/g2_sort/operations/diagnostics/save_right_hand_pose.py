"""Extract a measured right-hand reference from a read-only hand-state report.

This file does not import GDK or send commands. A measured reference is not an
approved command target, especially after reboot when feedback may be stale.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    side = report["analysis"]["sides"]["right"]
    joints = side["joints"]
    record = {
        "schema_version": 1,
        "kind": "measured_right_hand_reference",
        "source_report": str(args.report.resolve()),
        "captured_at_unix": report["generated_at_unix"],
        "operator_context": "Operator reported the current boot-time thumb configuration usable; preserve for review.",
        "approved_for_command": False,
        "joint_names": [j["name"] for j in joints],
        "positions_rad": [j["last_position"] for j in joints],
        "joint_diagnostics": joints,
        "issues": side["issues"],
        "motion": report["motion"],
        "limitations": "Measured SDK values only; not proof of fresh feedback, calibration, or safe command replay. Does not replace Home.",
    }
    output = args.report.with_name("right_hand_reference.json")
    with output.open("x") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print("【右手参考姿态】单位rad；前三项为拇指，idx84为小指横向")
    for name, value in zip(record["joint_names"], record["positions_rad"]):
        print(f"  {name}: {value}")
    print(f"【已保存】{output}")
    print("仅记录反馈参考，未发送动作、未覆盖Home。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
