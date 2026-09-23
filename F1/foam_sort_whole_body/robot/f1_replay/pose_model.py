"""Saved HAL poses and bounded setup plans; no ROS, publishers or file writes.

These are joint-space plans, not collision-checked trajectories. Body angles
remain named motor angles; a separately verified body adapter must execute them.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math

from .bundle import ARM_NAMES, BODY_NAMES
from .runtime import ReplayError, finite_number, fresh_value, prepare_action


POSE_SCHEMA = "f1_pose_snapshot_v1"
TARGET_SCHEMA = "f1_pose_target_v1"
JOINT_NAMES = ARM_NAMES + BODY_NAMES
SIDES = ("left", "right")


def _positive(value, label):
    if not finite_number(value) or value <= 0:
        raise ReplayError(f"{label} 必须为正有限数")
    return value


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"姿态证据不能保存为有限JSON：{exc}") from exc


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _values(values, names, label):
    if not isinstance(values, dict):
        raise ReplayError(f"{label} 必须为具名数值对象")
    for name in names:
        if not finite_number(values.get(name)):
            raise ReplayError(f"{label} 缺少有限数值：{name}")
    for name, value in values.items():
        if not isinstance(name, str) or not name or not finite_number(value):
            raise ReplayError(f"{label} 含无效名称或数值：{name}")
    return values


def capture_pose(snapshot, profile, *, now_ns, host, source_bundle=None,
                 utc=None, extra_feedback=None, profile_sha256=None):
    """Freeze fresh feedback, including every fresh optional HAL joint.

    ``extra_feedback`` stores separately read body/waist service feedback without
    assigning it motor-angle semantics. The caller supplies its provenance.
    ``profile_sha256`` may identify exact file bytes; otherwise canonical JSON.
    """
    if not isinstance(snapshot, dict) or type(now_ns) is not int or now_ns < 0:
        raise ReplayError("姿态采集需要反馈对象和单调时钟纳秒")
    if not isinstance(host, str) or not host.strip():
        raise ReplayError("姿态采集缺少主机来源")
    age_ns = _positive(profile.get("feedback_max_age_s"), "feedback_max_age_s") * 1e9
    joints = snapshot.get("joints", {})
    joint_positions = {name: fresh_value(joints.get(name), now_ns, age_ns, name)
                       for name in JOINT_NAMES}
    omitted = []
    for name, item in joints.items():
        if name in JOINT_NAMES:
            continue
        if not isinstance(name, str) or not name:
            raise ReplayError("HAL反馈含无效关节名")
        try:
            joint_positions[name] = fresh_value(item, now_ns, age_ns, name)
        except ReplayError:
            omitted.append(name)
    grippers = {side: fresh_value(snapshot.get("grippers", {}).get(side), now_ns,
                                  age_ns, f"{side} gripper") for side in SIDES}
    if any(not 0 <= value <= 100 for value in grippers.values()):
        raise ReplayError("夹爪反馈不在0..100")
    base = snapshot.get("base", {})
    received = base.get("received_ns")
    if type(received) is not int or not 0 <= now_ns - received <= age_ns:
        raise ReplayError("姿态备份缺少新鲜底盘反馈")
    for name in ("vx", "vy", "wz"):
        if not finite_number(base.get(name)):
            raise ReplayError(f"姿态备份缺少有限底盘反馈：{name}")
    result = {
        "schema": POSE_SCHEMA, "captured_utc": utc or datetime.now(timezone.utc).isoformat(),
        "host": host, "captured_monotonic_ns": now_ns,
        "feedback_max_age_s": profile["feedback_max_age_s"],
        "profile_sha256": profile_sha256 or _hash(profile),
        "profile_hash_basis": "file_bytes" if profile_sha256 else "canonical_json",
        "source_bundle": _json_copy(source_bundle),
        "joint_positions_deg": joint_positions, "grippers": grippers,
        "base": _json_copy(base), "raw_snapshot": _json_copy(snapshot),
        "extra_feedback": _json_copy(extra_feedback),
        "omitted_optional_joints": omitted,
        "readback_evidence": {"required_joints": list(JOINT_NAMES),
                              "positions_are_hal_feedback": True,
                              "base_pose_available": False,
                              "base_note": "速度与模式反馈；不是可恢复的底盘位置/朝向"},
    }
    validate_pose(result)
    return result


def validate_pose(pose):
    """Validate saved completeness; old capture times remain valid for restore.

    A saved pose is a target only. Execution must separately obtain new feedback.
    Missing values are never filled with zero.
    """
    if not isinstance(pose, dict) or pose.get("schema") != POSE_SCHEMA:
        raise ReplayError("不支持的姿态备份schema")
    for key in ("captured_utc", "host"):
        if not isinstance(pose.get(key), str) or not pose[key].strip():
            raise ReplayError(f"姿态备份缺少{key}")
    digest = pose.get("profile_sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)):
        raise ReplayError("姿态备份缺少profile SHA256")
    if type(pose.get("captured_monotonic_ns")) is not int or pose["captured_monotonic_ns"] < 0:
        raise ReplayError("姿态备份缺少采集时间")
    _values(pose.get("joint_positions_deg"), JOINT_NAMES, "joint_positions_deg")
    grippers = _values(pose.get("grippers"), SIDES, "grippers")
    if set(grippers) != set(SIDES) or any(not 0 <= value <= 100 for value in grippers.values()):
        raise ReplayError("姿态备份夹爪必须为left/right且在0..100")
    base = pose.get("base")
    if not isinstance(base, dict) or any(not finite_number(base.get(k)) for k in ("vx", "vy", "wz")):
        raise ReplayError("姿态备份缺少底盘速度证据")
    raw = pose.get("raw_snapshot")
    if not isinstance(raw, dict):
        raise ReplayError("姿态备份缺少原始反馈证据")
    for name in JOINT_NAMES:
        value = raw.get("joints", {}).get(name, {}).get("position")
        if not finite_number(value) or value != pose["joint_positions_deg"][name]:
            raise ReplayError(f"姿态备份与原始HAL证据不一致：{name}")
    for side in SIDES:
        value = raw.get("grippers", {}).get(side, {}).get("position")
        if not finite_number(value) or value != grippers[side]:
            raise ReplayError(f"姿态备份与原始夹爪证据不一致：{side}")
    _json_copy(pose)
    return pose


def _frame_values(frame0):
    if (len(frame0.state) != 21 or len(frame0.action) != 19
            or not all(finite_number(v) for v in tuple(frame0.state) + tuple(frame0.action))):
        raise ReplayError("起点必须包含有限state21和action19")
    return frame0.state, frame0.action


def compare_to_start(pose, frame0, profile):
    validate_pose(pose)
    state, action = _frame_values(frame0)
    command, projection = prepare_action(action, profile)
    rows = []
    for index, name in enumerate(ARM_NAMES):
        current = pose["joint_positions_deg"][name]
        tolerance = _positive(profile.get("start_arm_tolerance_deg"), "start_arm_tolerance_deg")
        rows.append({"name": name, "kind": "arm", "current": current,
                     "hal_start": state[index], "first_command": command[index],
                     "delta_to_hal": state[index] - current,
                     "delta_to_first_command": command[index] - current,
                     "tolerance": tolerance,
                     "within_tolerance": max(abs(state[index] - current), abs(command[index] - current)) <= tolerance})
    for index, side in enumerate(SIDES):
        current = pose["grippers"][side]
        tolerance = _positive(profile.get("start_gripper_tolerance"), "start_gripper_tolerance")
        rows.append({"name": side, "kind": "gripper", "current": current,
                     "hal_start": state[index + 14], "first_command": command[index + 14],
                     "delta_to_hal": state[index + 14] - current,
                     "delta_to_first_command": command[index + 14] - current,
                     "tolerance": tolerance,
                     "within_tolerance": max(abs(state[index + 14] - current), abs(command[index + 14] - current)) <= tolerance})
    for index, name in enumerate(BODY_NAMES):
        current = pose["joint_positions_deg"][name]
        tolerance = _positive(profile.get("body_tolerance_deg"), "body_tolerance_deg")
        rows.append({"name": name, "kind": "body", "current": current,
                     "hal_start": state[index + 16], "first_command": None,
                     "delta_to_hal": state[index + 16] - current,
                     "delta_to_first_command": None, "tolerance": tolerance,
                     "within_tolerance": abs(state[index + 16] - current) <= tolerance})
    return {"all_within_tolerance": all(row["within_tolerance"] for row in rows),
            "fields": rows, "action_projection": projection,
            "body_semantics": "5 motor-angle feedback channels, not base xyz"}


def _target(joints, grippers, profile, source):
    _values(joints, JOINT_NAMES, "目标关节")
    _values(grippers, SIDES, "目标夹爪")
    if any(not 0 <= value <= 100 for value in grippers.values()):
        raise ReplayError("目标夹爪不在0..100")
    values = [joints[n] for n in ARM_NAMES] + [grippers[s] for s in SIDES] + [0., 0., 0.]
    command, changes = prepare_action(values, profile)
    limits = profile.get("joint_limits_deg", {})
    for index, name in enumerate(ARM_NAMES):
        if name not in limits or not limits[name][0] <= command[index] <= limits[name][1]:
            raise ReplayError(f"准备动作目标超出限位：{name}")
    result = {"schema": TARGET_SCHEMA, "source": deepcopy(source),
              "joint_positions_deg": {name: joints[name] for name in JOINT_NAMES},
              "grippers": {side: grippers[side] for side in SIDES},
              "arm_projection": changes, "exact_requested_pose": not bool(changes),
              "body_limits_verified": False}
    result["joint_positions_deg"].update(dict(zip(ARM_NAMES, command[:14])))
    return result


def start_target(frame0, profile, *, policy="recorded_hal"):
    if policy not in ("recorded_hal", "feasible_intersection_v1"):
        raise ReplayError(f"未知起姿选择策略：{policy}")
    state, action = _frame_values(frame0)
    joints = dict(zip(JOINT_NAMES, tuple(state[:14]) + tuple(state[16:21])))
    target = _target(joints, dict(zip(SIDES, state[14:16])), profile,
                     {"kind": "recorded_hal_start", "source_time_ns": frame0.source_time_ns})
    command, _ = prepare_action(action, profile)
    arm_tolerance = _positive(profile.get("start_arm_tolerance_deg"), "start_arm_tolerance_deg")
    grip_tolerance = _positive(profile.get("start_gripper_tolerance"), "start_gripper_tolerance")
    adjustments = []
    for i, name in enumerate(ARM_NAMES):
        actual = target["joint_positions_deg"][name]
        if max(abs(actual - state[i]), abs(actual - command[i])) > arm_tolerance:
            if policy == "recorded_hal":
                raise ReplayError(f"按HAL原值起姿时与首帧命令超过容差：{name}；不自动选择折中起姿")
            limit_low, limit_high = profile["joint_limits_deg"][name]
            low = max(state[i]-arm_tolerance, command[i]-arm_tolerance, limit_low)
            high = min(state[i]+arm_tolerance, command[i]+arm_tolerance, limit_high)
            if low > high:
                raise ReplayError(f"HAL/首帧目标容差与硬限位没有交集：{name}；不能准备该起点")
            actual = (low+high)/2
            target["joint_positions_deg"][name] = actual
            adjustments.append({"name": name, "recorded_hal": state[i],
                                "first_command_after_limit_projection": command[i],
                                "feasible_interval_deg": [low, high], "target_deg": actual,
                                "delta_to_hal_deg": actual-state[i],
                                "delta_to_first_command_deg": actual-command[i]})
    for i, side in enumerate(SIDES):
        if abs(state[i + 14] - command[i + 14]) > grip_tolerance:
            raise ReplayError(f"HAL起姿与首帧夹爪命令超过容差：{side}")
    target["start_pose_selection"] = {"policy": policy, "adjustments": adjustments,
                                      "tolerance_changed": False, "source_data_changed": False}
    return target


def restore_target(pose, profile):
    validate_pose(pose)
    return _target(pose["joint_positions_deg"], pose["grippers"], profile,
                   {"kind": "saved_hal_pose", "pose_sha256": _hash(pose),
                    "captured_utc": pose["captured_utc"], "host": pose["host"]})


def plan_transition(current_pose, target, profile, *, rate_hz=30.0,
                    arm_rate_deg_s=3.0, body_rate_deg_s=1.0, gripper_rate_s=10.0,
                    body_acceleration_deg_s2=None, body_jerk_deg_s3=None, sample_stride=1):
    """Quintic named motor-angle plan, including endpoints and bounded peak rate.

    Current arm feedback outside verified limits is explicitly projected under
    the profile policy before interpolation. Such initial projection is reported;
    its correction is not represented as a smooth motion from invalid feedback.
    No command is generated for optional head joints or base velocities/position.
    """
    validate_pose(current_pose)
    if not isinstance(target, dict) or target.get("schema") != TARGET_SCHEMA:
        raise ReplayError("不支持的姿态目标schema")
    for value, name in ((rate_hz, "rate_hz"), (arm_rate_deg_s, "arm_rate_deg_s"),
                        (body_rate_deg_s, "body_rate_deg_s"), (gripper_rate_s, "gripper_rate_s")):
        _positive(value, name)
    if rate_hz > profile["max_command_hz"]:
        raise ReplayError("准备动作采样率超过profile.max_command_hz")
    if type(sample_stride) is not int or not 1 <= sample_stride <= 200:
        raise ReplayError('准备动作sample_stride必须为1..200整数')
    start = restore_target(current_pose, profile)
    end = _target(target["joint_positions_deg"], target["grippers"], profile, target.get("source"))
    duration = 1.0 / rate_hz
    for value, name in ((body_acceleration_deg_s2, 'body_acceleration_deg_s2'),
                        (body_jerk_deg_s3, 'body_jerk_deg_s3')):
        if value is not None:
            _positive(value, name)
    for name in JOINT_NAMES:
        speed = arm_rate_deg_s if name in ARM_NAMES else body_rate_deg_s
        displacement = abs(end["joint_positions_deg"][name] - start["joint_positions_deg"][name])
        duration = max(duration, 1.875 * displacement / speed)
        if name in BODY_NAMES:
            if body_acceleration_deg_s2 is not None:
                duration = max(duration, math.sqrt((10 / math.sqrt(3)) * displacement / body_acceleration_deg_s2))
            if body_jerk_deg_s3 is not None:
                duration = max(duration, (60 * displacement / body_jerk_deg_s3) ** (1 / 3))
    for side in SIDES:
        duration = max(duration, 1.875 * abs(end["grippers"][side] - start["grippers"][side]) / gripper_rate_s)
    steps = math.ceil(duration * rate_hz / sample_stride) * sample_stride
    if steps > 100_000:
        raise ReplayError("准备动作计划超过100000帧；请检查单位或目标")
    duration = steps / rate_hz
    frames = []
    for index in range(steps + 1):
        u = index / steps
        blend = u * u * u * (10.0 + u * (-15.0 + u * 6.0))
        joints = {name: start["joint_positions_deg"][name] + blend *
                  (end["joint_positions_deg"][name] - start["joint_positions_deg"][name]) for name in JOINT_NAMES}
        grippers = {side: start["grippers"][side] + blend *
                    (end["grippers"][side] - start["grippers"][side]) for side in SIDES}
        frames.append({"time_ns": round(index / rate_hz * 1e9),
                       "joint_positions_deg": joints, "grippers": grippers})
    frames[0]["joint_positions_deg"] = deepcopy(start["joint_positions_deg"])
    frames[0]["grippers"] = deepcopy(start["grippers"])
    frames[-1]["joint_positions_deg"] = deepcopy(end["joint_positions_deg"])
    frames[-1]["grippers"] = deepcopy(end["grippers"])
    return {"schema": "f1_pose_plan_v1", "duration_s": duration, "rate_hz": rate_hz,
            "sample_stride": sample_stride,
            "profile_sha256": _hash(profile), "start_pose_sha256": _hash(current_pose),
            "interpolation": "quintic_10u3_minus_15u4_plus_6u5",
            "software_policy": {"arm_rate_deg_s": arm_rate_deg_s,
                                "body_rate_deg_s": body_rate_deg_s,
                                "body_acceleration_deg_s2": body_acceleration_deg_s2,
                                "body_jerk_deg_s3": body_jerk_deg_s3,
                                "gripper_rate_s": gripper_rate_s,
                                "policy_is_hardware_rating": False},
            "initial_arm_projection": start["arm_projection"],
            "target_arm_projection": deepcopy(target.get("arm_projection", [])) + end["arm_projection"],
            "source_snapshot_modified": False, "base_commands_included": False,
            "head_commands_included": False, "collision_checked": False,
            "body_adapter_required": True, "body_limits_verified": False,
            "note": "仅关节空间慢速数值计划；执行需独立核对身体接口、限位、控制权、反馈和现场路径",
            "target_source": deepcopy(target.get("source")), "frames": frames}
