"""ROS-independent replay validation and monotonic scheduling."""
from __future__ import annotations

import math
import time

from .bundle import ARM_NAMES, BODY_NAMES
from .ros_backend import COMMAND_TYPES


class ReplayError(RuntimeError):
    pass


class ReplayInterrupted(ReplayError):
    pass


POSITIVE_FIELDS = (
    "feedback_max_age_s", "start_arm_tolerance_deg", "start_gripper_tolerance",
    "body_tolerance_deg", "base_stopped_linear_m_s", "base_stopped_angular_rad_s",
    "max_command_hz", "max_source_gap_s", "max_lateness_s",
    "stop_timeout_s", "stop_publish_hz",
)


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def profile_issues(profile):
    if not isinstance(profile, dict):
        return ["运行配置必须为JSON对象"]
    issues = []
    if profile.get("arm_limit_policy", "reject") not in ("reject", "clip"):
        issues.append("arm_limit_policy 必须为reject或clip")
    if profile.get("joint_limit_source") == "verified_controller_binary":
        digest = profile.get("controller_binary_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            issues.append("缺少已核对控制器二进制SHA256")
    if profile.get("schema_version") != 1:
        issues.append("schema_version 必须为1")
    if profile.get("status") != "verified":
        issues.append("F1运行配置尚未核定（status不是verified）")
    if not isinstance(profile.get("verification_reference"), str) or not profile["verification_reference"].strip():
        issues.append("缺少运行配置核对记录 verification_reference")
    for key in POSITIVE_FIELDS:
        value = profile.get(key)
        if not finite_number(value) or value <= 0:
            issues.append(f"待核定正数参数：{key}")
    for side in ("left", "right"):
        effort = profile.get("gripper_max_effort")
        value = effort.get(side) if isinstance(effort, dict) else None
        if not finite_number(value) or value < 0:
            issues.append(f"待核定夹爪 max_effort：{side}")
    for key in ("vx", "vy", "wz"):
        speeds = profile.get("base_speed_limits")
        value = speeds.get(key) if isinstance(speeds, dict) else None
        if not finite_number(value) or value < 0:
            issues.append(f"待核定底盘速度绝对值上限：{key}")
    if type(profile.get("expected_hard_auto")) is not bool:
        issues.append("待核定 expected_hard_auto")
    for key, allowed in (("arm_control_mode", (1, 3)), ("arm_impedance_type", (1, 2, 3))):
        if type(profile.get(key)) is not int or profile[key] not in allowed:
            issues.append(f"待核定 {key}")
    if not isinstance(profile.get("base_frame"), str) or not profile["base_frame"]:
        issues.append("缺少已核定的底盘坐标系")
    limits = profile.get("joint_limits_deg")
    if not isinstance(limits, dict) or set(limits) != set(ARM_NAMES):
        issues.append("joint_limits_deg 必须包含完整14个臂关节")
    else:
        for name, bounds in limits.items():
            if (not isinstance(bounds, list) or len(bounds) != 2
                    or not all(finite_number(x) for x in bounds) or bounds[0] >= bounds[1]):
                issues.append(f"无效关节限位：{name}")
    topics = profile.get("topics")
    topics = topics if isinstance(topics, dict) else {}
    for key in ("arms_command", "left_gripper_command", "right_gripper_command", "base_command",
                "joint_state", "left_gripper_state", "right_gripper_state", "base_state"):
        if not isinstance(topics.get(key), str) or not topics[key].startswith("/"):
            issues.append(f"缺少绝对topic：{key}")
    values = [value for value in topics.values() if isinstance(value, str)]
    if len(set(values)) != len(values):
        issues.append("命令与反馈topic不能重名")
    return issues


def prepare_action(action, profile):
    """Project arm targets only under the explicitly selected clipping policy.

    Source data is immutable. Other fields are never changed by this mapping.
    """
    command = list(action)
    changes = []
    if profile.get("arm_limit_policy", "reject") != "clip":
        return tuple(command), changes
    limits = profile.get("joint_limits_deg")
    if not isinstance(limits, dict) or set(limits) != set(ARM_NAMES):
        raise ReplayError("关节截断需要完整14个已核对限位")
    for i, name in enumerate(ARM_NAMES):
        bounds = limits[name]
        if (not isinstance(bounds, list) or len(bounds) != 2
                or not all(finite_number(x) for x in bounds) or bounds[0] >= bounds[1]):
            raise ReplayError(f"无效关节限位：{name}")
        value = command[i]
        if not finite_number(value):
            raise ReplayError(f"非有限关节目标：{name}")
        low, high = bounds
        clipped = min(high, max(low, value))
        if clipped != value:
            command[i] = clipped
            changes.append({"joint": name, "source_deg": value, "command_deg": clipped,
                            "limit_deg": [low, high], "delta_deg": clipped - value})
    return tuple(command), changes


def action_projection_summary(bundle, profile):
    """Describe the explicit mapping without declaring hardware readiness."""
    result = {"policy": profile.get("arm_limit_policy", "reject"), "source_data_modified": False,
              "clipped_frames": 0, "clipped_joint_values": 0, "joints": {}}
    if result["policy"] != "clip":
        return result
    for index, frame in enumerate(bundle.frames):
        _, changes = prepare_action(frame.action, profile)
        result["clipped_frames"] += bool(changes)
        result["clipped_joint_values"] += len(changes)
        for change in changes:
            entry = result["joints"].setdefault(change["joint"],
                {"count": 0, "first_frame": index, "last_frame": index, "max_abs_delta_deg": 0.0})
            entry["count"] += 1
            entry["last_frame"] = index
            entry["max_abs_delta_deg"] = max(entry["max_abs_delta_deg"], abs(change["delta_deg"]))
    return result


def validate_actions(bundle, profile):
    """Validate source time and the explicitly mapped command; never edit the bundle."""
    issues = profile_issues(profile)
    if issues:
        raise ReplayError("；".join(issues))
    if bundle.manifest.get("status") != "replay_ready":
        raise ReplayError("数据包仍是candidate；清洗完成并核对该数据包后才能现场回放")
    if bundle.manifest.get("retiming"):
        limits = bundle.manifest["retiming"]["plan"]["limits"]
        expected = profile.get("base_controller_expected_parameters", {})
        for short, name in [("acceleration", "max_acceleration.linear"),
                            ("deceleration", "max_deceleration.linear"),
                            ("speed", "max_speed.linear")]:
            if name not in expected or expected[name] != limits[short]:
                raise ReplayError("重定时必须绑定相同底盘运行参数，禁止无参数检查执行")
    contract = bundle.manifest.get("execution_contract", {})
    for field in ("arm_limit_policy", "controller_binary_sha256"):
        if field in contract and contract[field] != profile.get(field):
            raise ReplayError(f"数据包核准的{field}与当前执行配置不一致")
    periods = [b.time_ns - a.time_ns for a, b in zip(bundle.frames, bundle.frames[1:])]
    periods.append(bundle.duration_ns - bundle.frames[-1].time_ns)
    if any(x <= 0 or x > profile["max_source_gap_s"] * 1e9 for x in periods):
        raise ReplayError("源时间轴存在超出已核定范围的间隔；不压缩或跨越缺口")
    if any(1e9 / x > profile["max_command_hz"] * (1 + 1e-6) for x in periods):
        raise ReplayError("源指令频率超过已核定的控制频率")
    for i, frame in enumerate(bundle.frames):
        command, _ = prepare_action(frame.action, profile)
        if any(abs(a - b) > profile["body_tolerance_deg"]
               for a, b in zip(frame.state[16:21], bundle.frames[0].state[16:21])):
            raise ReplayError(f"第{i}帧录制身体姿态偏离起点；19D回放不包含身体驱动")
        for name, value in zip(ARM_NAMES, command[:14]):
            low, high = profile["joint_limits_deg"][name]
            if not low <= value <= high:
                raise ReplayError(f"第{i}帧{name}目标{value}超出[{low}, {high}]；当前未启用关节截断")
        if not all(0 <= x <= 100 for x in frame.action[14:16]):
            raise ReplayError(f"第{i}帧夹爪目标不在0..100")
        for key, value in zip(("vx", "vy", "wz"), frame.action[16:19]):
            if abs(value) > profile["base_speed_limits"][key]:
                raise ReplayError(f"第{i}帧底盘{key}超过已核定范围；不自动限速")


def fresh_value(item, now_ns, max_age_ns, label):
    if not isinstance(item, dict) or not finite_number(item.get("position")):
        raise ReplayError(f"缺少有效反馈：{label}")
    received = item.get("received_ns")
    if type(received) is not int or not 0 <= now_ns - received <= max_age_ns:
        raise ReplayError(f"反馈过旧：{label}")
    return item["position"]


def check_feedback(snapshot, frame0, profile, now_ns, initial=False):
    age = profile["feedback_max_age_s"] * 1e9
    joints = snapshot.get("joints", {})
    arms = [fresh_value(joints.get(n), now_ns, age, n) for n in ARM_NAMES]
    body = [fresh_value(joints.get(n), now_ns, age, n) for n in BODY_NAMES]
    grippers = [fresh_value(snapshot.get("grippers", {}).get(side), now_ns, age, side)
                for side in ("left", "right")]
    if any(abs(a - b) > profile["body_tolerance_deg"] for a, b in zip(body, frame0.state[16:21])):
        raise ReplayError("身体姿态与所选示教起点不一致；本程序不控制头部/身体")
    base = snapshot.get("base", {})
    received = base.get("received_ns")
    if type(received) is not int or not 0 <= now_ns - received <= age:
        raise ReplayError("缺少新鲜底盘反馈")
    if not all(finite_number(base.get(k)) for k in ("vx", "vy", "wz")):
        raise ReplayError("底盘反馈含缺失或非有限速度")
    if base.get("is_soft_auto") is not False:
        raise ReplayError("ManualMoveCmd需要软手动模式；本程序不自动切换模式")
    if base.get("is_hard_auto") is not profile["expected_hard_auto"]:
        raise ReplayError("底盘硬控制模式与核定配置不同")
    if base.get("is_parking_mode") is not False or base.get("is_avoid") is not False:
        raise ReplayError("底盘处于驻车、避障介入或状态未知，停止精确回放")
    if initial:
        # Check both the recorded starting observation and the first issued target.
        first_command, _ = prepare_action(frame0.action, profile)
        for reference in (frame0.state[:14], first_command[:14]):
            if any(abs(a - b) > profile["start_arm_tolerance_deg"] for a, b in zip(arms, reference)):
                raise ReplayError("双臂未处于回放起点附近；不自动归位或跨大距离追首帧")
        for reference in (frame0.state[14:16], frame0.action[14:16]):
            if any(abs(a - b) > profile["start_gripper_tolerance"] for a, b in zip(grippers, reference)):
                raise ReplayError("夹爪未处于回放起点附近；不自动开闭准备")
        if not base_stopped(snapshot, profile, now_ns):
            raise ReplayError("回放开始前底盘尚未确认静止")
    return {"arms": arms, "body": body, "grippers": grippers, "base": base}


def base_stopped(snapshot, profile, now_ns):
    base = snapshot.get("base", {})
    received = base.get("received_ns")
    if type(received) is not int or not 0 <= now_ns - received <= profile["feedback_max_age_s"] * 1e9:
        return False
    if not all(finite_number(base.get(k)) for k in ("vx", "vy", "wz")):
        return False
    return (math.hypot(base["vx"], base["vy"]) <= profile["base_stopped_linear_m_s"]
            and abs(base["wz"]) <= profile["base_stopped_angular_rad_s"])


def check_command_activity(backend, profile):
    if profile.get('command_ownership_policy') != 'observed_idle_v1':
        return None
    activity = backend.command_activity()
    if activity['observed_s'] < 1.0:
        raise ReplayError('命令监听不足1秒，尚不能判断是否待机')
    if activity['external_commands']:
        raise ReplayError(f"检测到其他程序实际发送控制命令：{activity['external_commands']}；本次运行中止")
    if backend.commands_enabled and not activity['own_gids_bound']:
        raise ReplayError('尚未绑定自身命令GID')
    return activity


def check_command_peers(backend, profile, enabled=False):
    peers = backend.command_peers()
    observed_idle = profile.get('command_ownership_policy') == 'observed_idle_v1'
    check_command_activity(backend, profile)
    for key in ("arms_command", "left_gripper_command", "right_gripper_command", "base_command"):
        topic = profile["topics"][key]
        info = peers.get(topic, {})
        # A recorder subscriber is not a controller and must not satisfy this check.
        consumers = [p for p in info.get("subscribers", [])
                     if "rosbag" not in p.get("name", "") and p.get("type") == COMMAND_TYPES[key]
                     and not (p.get('name') in (backend.node_name, backend.node_name+'_monitor')
                              and p.get('namespace') == backend.node_namespace)]
        if not consumers:
            raise ReplayError(f"未发现控制器订阅：{topic}")
        others = [p for p in info.get("publishers", [])
                  if not (enabled and p.get("name") == backend.node_name
                          and p.get("namespace") == backend.node_namespace)]
        if others and not observed_idle:
            raise ReplayError(f"存在其他命令发布者：{topic} {others}；请现场结束对应任务")
    return peers


def check_controller_parameters(actual, profile):
    if (profile.get("joint_limit_source") == "verified_controller_binary"
            or profile.get("gripper_effort_policy") == "ignored_by_verified_omnipicker"):
        if actual.get("_controller_binary_sha256") != profile.get("controller_binary_sha256"):
            raise ReplayError("当前运行控制器二进制与已审计SHA256不一致")
    if profile.get("gripper_effort_policy") == "ignored_by_verified_omnipicker":
        for side in ("left", "right"):
            if actual.get(side + "_gripper_type") != "omnipicker":
                raise ReplayError(f"{side}夹爪类型改变，不能沿用忽略max_effort的接口语义")
    for key in ("arm_control_mode", "arm_impedance_type"):
        if actual.get(key) != profile[key]:
            raise ReplayError(f"motor_ctl参数{key}与已核定配置不同：{actual.get(key)!r}")
    for name in ARM_NAMES:
        for suffix, expected in zip(("min", "max"), profile["joint_limits_deg"][name]):
            value = actual.get(name + "_" + suffix)
            if not finite_number(value) or abs(value - expected) > 1e-9:
                raise ReplayError(f"motor_ctl限位参数不匹配：{name}_{suffix}")


def run_schedule(bundle, send, tick, log, max_lateness_s,
                 now=time.monotonic_ns, sleep=time.sleep, stopped=lambda: False,
                 max_command_hz=None, action_transform=None):
    """One source frame per deadline. Never catch up by bursting old commands."""
    start = now()
    # First dispatch still gets a fresh check and the normal lateness budget.
    tick()
    max_late = round(max_lateness_s * 1e9)
    previous_dispatch = None

    def wait_until(deadline):
        while True:
            if stopped():
                raise ReplayInterrupted("操作者停止回放")
            # If sleep reached the deadline, dispatch now. Running another
            # feedback batch here adds its cost to every rate-limited period.
            if deadline - now() <= 0:
                return
            tick()
            if stopped():
                raise ReplayInterrupted("操作者停止回放")
            remaining = deadline - now()
            if remaining <= 0:
                return
            sleep(min(remaining / 1e9, 0.01))

    for index, frame in enumerate(bundle.frames):
        deadline = start + frame.time_ns
        next_time = bundle.frames[index + 1].time_ns if index + 1 < len(bundle.frames) else bundle.duration_ns
        command, changes = (action_transform(frame.action) if action_transform else (frame.action, []))
        # A slightly late previous send must not cause an immediate false alarm
        # or a compressed interval. Wait within the existing lateness budget.
        rate_deadline = (previous_dispatch + math.ceil(1e9 / max_command_hz)
                         if max_command_hz is not None and previous_dispatch is not None else deadline)
        wait_until(max(deadline, rate_deadline))
        dispatch = now()
        if dispatch - deadline > max_late or dispatch >= start + next_time:
            raise ReplayError(f"第{index}帧调度迟到，停止而不突发补发旧命令")
        if (max_command_hz is not None and previous_dispatch is not None
                and dispatch - previous_dispatch < 1e9 / max_command_hz * (1 - 1e-6)):
            raise ReplayError("实际下发间隔小于已核定周期；停止而不集中补发")
        if stopped():
            raise ReplayInterrupted("操作者停止回放")
        result = send(command)
        previous_dispatch = dispatch
        log("command", {"index": index, "source_time_ns": frame.source_time_ns,
                        "source_relative_ns": frame.time_ns, "scheduled_monotonic_ns": deadline,
                        "dispatch_monotonic_ns": dispatch, "lateness_ns": dispatch - deadline,
                        "source_action": list(frame.action), "command_action": list(command),
                        "action_adjustments": changes, "published": result})
        if now() >= start + next_time:
            raise ReplayError("发送或日志处理跨过下一源时间边界，停止而不补发")
    # Preserve the final source interval, then let the caller send explicit zero speed.
    wait_until(start + bundle.duration_ns)
    log("source_timeline_complete", {"duration_ns": bundle.duration_ns, "frames": len(bundle.frames)})


def run_preparation_schedule(bundle, send, tick, log, max_stall_s,
                             now=time.monotonic_ns, sleep=time.sleep,
                             stopped=lambda: False, max_command_hz=None, before_frame=None):
    """Generated pose preparation may stretch in time; recorded replay may not.

    Send every waypoint in order with at least its planned interval since the
    previous dispatch. A short CPU stall lengthens motion, never compresses it
    or jumps to a later waypoint. Freshness checks and bounded stalls remain.
    """
    if not finite_number(max_stall_s) or max_stall_s <= 0:
        raise ReplayError('准备动作卡顿上限必须为正数')
    if max_command_hz is not None and (not finite_number(max_command_hz) or max_command_hz <= 0):
        raise ReplayError('准备动作派发上限必须为正数')
    limit = round(max_stall_s*1e9)
    origin = now()
    previous = None
    for index, frame in enumerate(bundle.frames):
        if stopped():raise ReplayInterrupted('操作者停止姿态调整')
        tick()
        if before_frame is not None and before_frame():
            # An explicit feedback-controlled pause is not a CPU stall. Resume
            # from now, with the normal interval and no skipped/catch-up points.
            previous=now()
        interval = (frame.time_ns-bundle.frames[index-1].time_ns) if index else 0
        if index and (interval <= 0 or interval > limit):
            raise ReplayError('准备轨迹采样间隔无效或超过卡顿上限')
        if max_command_hz is not None and index:
            interval=max(interval,math.ceil(1e9/max_command_hz))
        deadline=origin if previous is None else previous+interval
        while now()<deadline:
            if stopped():raise ReplayInterrupted('操作者停止姿态调整')
            tick()
            remaining=deadline-now()
            if remaining>0:sleep(min(remaining/1e9,.01))
        dispatch=now()
        if dispatch-(origin if previous is None else previous)>limit:
            raise ReplayError(f'准备动作连续卡顿超过{max_stall_s:.3f}秒，停止')
        if stopped():raise ReplayInterrupted('操作者停止姿态调整')
        published=send(frame.action)
        previous=dispatch
        log('command',{'index':index,'source_time_ns':frame.source_time_ns,
            'source_relative_ns':frame.time_ns,'scheduled_monotonic_ns':deadline,
            'dispatch_monotonic_ns':dispatch,'lateness_ns':max(0,dispatch-deadline),
            'schedule_stretch_ns':dispatch-origin-frame.time_ns,
            'timing_policy':'generated_pose_stretch_no_catchup',
            'source_action':list(frame.action),'command_action':list(frame.action),
            'action_adjustments':[],'published':published})
        if now()-dispatch>limit:
            raise ReplayError(f'准备动作发送或日志卡顿超过{max_stall_s:.3f}秒，停止')
    log('source_timeline_complete',{'duration_ns':bundle.duration_ns,
        'actual_duration_ns':now()-origin,'frames':len(bundle.frames),
        'timing_policy':'generated_pose_stretch_no_catchup'})


def stop_motion(backend, profile, log, now=time.monotonic_ns, sleep=time.sleep):
    """Best-effort zero base command and fresh arm hold; never open grippers."""
    started = now()
    deadline = started + round(profile["stop_timeout_s"] * 1e9)
    base_confirmed = False
    zero_published = False
    held = False
    errors = []
    while now() < deadline:
        base_confirmed = False
        try:
            sent = backend.stop_base()
            zero_published = zero_published or sent is not None
            log("stop_base", sent or {"published": False})
        except Exception as exc:
            errors.append(str(exc))
        try:
            backend.poll(0.0)
            snapshot = backend.snapshot()
            # Require feedback received after stopping began, not an earlier zero sample.
            base_confirmed = (zero_published and snapshot.get("base", {}).get("received_ns", -1) > started
                              and base_stopped(snapshot, profile, now()))
            if not held:
                for name in ARM_NAMES:
                    value = fresh_value(snapshot.get("joints", {}).get(name), now(),
                                        profile["feedback_max_age_s"] * 1e9, name)
                    low, high = profile["joint_limits_deg"][name]
                    if not low <= value <= high:
                        raise ReplayError(f"停止时{name}反馈超限，未发送臂保持目标")
                sent = backend.hold_arms()
                log("hold_arms", sent or {"published": False})
                held = sent is not None
        except Exception as exc:
            if not errors or errors[-1] != str(exc):
                errors.append(str(exc))
        if base_confirmed and held:
            break
        sleep(min(1.0 / profile["stop_publish_hz"], max(0, (deadline - now()) / 1e9)))
    result = {"zero_velocity_feedback_confirmed": base_confirmed, "fresh_arm_hold_published": held,
              "arm_hold_is_not_measured_arrival": True, "errors": errors[-10:]}
    log("stop_result", result)
    return result
