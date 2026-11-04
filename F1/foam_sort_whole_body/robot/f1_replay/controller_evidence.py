"""Read limits embedded in one explicitly audited controller binary.

This is a version-bound compatibility path for motor_ctl builds that do not
declare joint limits as ROS parameters. Values never come from the profile.
No process, parameter, mode or command is modified here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


AUDITED_SHA256 = "898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8"
LIMITS_BY_SHA256 = {
    AUDITED_SHA256: {
        f"arm_{side}_j{index}_{bound}": value
        for side in ("l", "r")
        for index, pair in enumerate(((-178, 178), (-94, 120), (-178, 178),
                                      (-145, 60), (-178, 178), (-60, 60), (-90, 90)), 1)
        for bound, value in zip(("min", "max"), pair)
    }
}
# New F1 at 192.0.2.25: independently decoded the installed AND running
# executable's 14 arm assignments; values match the previous audited build.
NEW_F1_SHA256 = "1ec598c860672824d46fe93f8f7c784002ed7612f666d6660c1f925206086ddc"
LIMITS_BY_SHA256[NEW_F1_SHA256] = dict(LIMITS_BY_SHA256[AUDITED_SHA256])
EVIDENCE_REFERENCE = (
    "results/replay_runtime_fix_20260915/hardware_evidence/"
    "motor_binary_evidence.json + motor_binary_extra.json; "
    "motor_ctl::MotorCtlCore::loadJointLimits(), arm_l_j1..7 and arm_r_j1..7, degrees"
)
EVIDENCE_BY_SHA256 = {
    AUDITED_SHA256: EVIDENCE_REFERENCE,
    NEW_F1_SHA256: "results/deployments/20260918-new-f1-29-v1/decoded_controller_contract.json",
}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _start_time(path):
    return path.read_text().rsplit(") ", 1)[1].split()[19]


def read_verified_binary_limits(profile, proc_root=Path("/proc")):
    """Require a unique local same-user controller and hash its live /proc exe."""
    if profile.get("joint_limit_source") != "verified_controller_binary":
        raise RuntimeError("未显式选择verified_controller_binary限位来源")
    expected = profile.get("controller_binary_sha256")
    if expected not in LIMITS_BY_SHA256:
        raise RuntimeError("controller_binary_sha256不在已审计的控制器版本中")
    candidates = []
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            comm = (entry / "comm").read_text().strip()
        except FileNotFoundError:
            continue  # An unrelated process exited during enumeration.
        if comm == "motor_ctl_node":
            candidates.append(entry)
    if len(candidates) != 1:
        raise RuntimeError(f"需要同用户唯一motor_ctl_node进程，实际{len(candidates)}个")
    process = candidates[0]
    started = _start_time(process / "stat")
    command = (process / "cmdline").read_bytes().split(b"\0")
    node_remaps = [part.decode("utf-8", "strict") for part in command
                   if part.startswith((b"__node:=", b"__name:="))]
    if node_remaps not in (["__node:=motor_ctl"], ["__name:=motor_ctl"]):
        raise RuntimeError(f"motor_ctl_node进程ROS节点名无法唯一绑定motor_ctl：{node_remaps}")
    namespaces = [part for part in command if part.startswith(b"__ns:=")]
    if namespaces not in ([], [b"__ns:=/"]):
        raise RuntimeError("motor_ctl_node进程不在/motor_ctl参数服务的根命名空间")
    exe = process / "exe"
    actual_path = exe.resolve(strict=True)
    if actual_path.name != "motor_ctl_node":
        raise RuntimeError("motor_ctl_node进程实际exe名称不匹配")
    before = exe.stat()
    actual = _sha256(exe)  # Never hash a separate path supplied by the profile.
    after = exe.stat()
    if (_start_time(process / "stat") != started
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (process / "cmdline").read_bytes().split(b"\0") != command):
        raise RuntimeError("读取控制器证据期间进程或exe发生变化")
    if actual != expected or actual not in LIMITS_BY_SHA256:
        raise RuntimeError(f"运行控制器SHA256与已审计版本不符：actual={actual} expected={expected}")
    return {"parameters": dict(LIMITS_BY_SHA256[actual]),
            "sha256": actual, "pid": int(process.name), "uid": os.getuid(),
            "process_start_ticks": started, "ros_node": "motor_ctl", "exe": str(actual_path),
            "source": "audited_loadJointLimits_in_current_process_executable",
            "reference": EVIDENCE_BY_SHA256[actual],
            "scope": "compiled_joint_limits_not_joint_or_mode_feedback"}
