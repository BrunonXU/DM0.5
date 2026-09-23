"""Pose transport through the audited motor_ctl ROS path, never direct UDP.

The caller owns controller identity readback, ownership, interpolation, operator
confirmation and stopping. Opening remains subscription-only, as in RosBackend.
"""
from __future__ import annotations

from collections.abc import Mapping
import time

from .controller_evidence import AUDITED_SHA256, NEW_F1_SHA256, LIMITS_BY_SHA256
from .ros_backend import ARM_NAMES, RosBackend, _finite


BODY_NAMES = tuple(f"body_j{i}" for i in range(1, 6))
POSE_NAMES = ARM_NAMES + BODY_NAMES
BODY_LIMITS_BY_BINARY = {
    AUDITED_SHA256: dict(zip(BODY_NAMES, (
        (-130.0, 130.0), (-10.0, 10.0), (-70.0, 0.0),
        (-95.0, 0.0), (-40.0, 0.0),
    ))),
}
# Independently decoded from D071's captured installed binary, including immediate
# fmov constants and xzr zero stores; see results/pose_192_4f_20260918/decoded_limits.json.
# This binds motor angle bounds, not cross-robot geometry or collision acceptance.
BODY_LIMITS_BY_BINARY[NEW_F1_SHA256] = dict(zip(BODY_NAMES, (
    (-130.0, 130.0), (-10.0, 10.0), (-70.0, 0.0),
    (-95.0, 0.0), (-40.0, 0.0),
)))


def validate_body_profile(profile: dict) -> dict:
    """Bind body limits to a reviewed binary, never to arbitrary profile values.

This is a configuration check. The caller also checks the live controller SHA
using the existing read-only controller evidence before enabling commands.
"""
    digest = profile.get("controller_binary_sha256")
    if (profile.get("joint_limit_source") != "verified_controller_binary"
            or digest not in BODY_LIMITS_BY_BINARY):
        raise ValueError("身体恢复仅支持已审计motor_ctl二进制；缺少匹配SHA256")
    if profile.get("topics", {}).get("arms_command") != "/motion_ctl/joint_ctl":
        raise ValueError("身体恢复必须使用已核对的/motion_ctl/joint_ctl接口")
    return dict(BODY_LIMITS_BY_BINARY[digest])


class PoseBackend(RosBackend):
    """Send arm14 + body5 named positions, two grippers and zero base velocity."""

    def enable_commands(self) -> None:
        validate_body_profile(self.profile)
        super().enable_commands()

    def check_body_parameters(self, actual_parameters: Mapping) -> None:
        """Check the caller's live readback before it enables command output."""
        validate_body_profile(self.profile)
        if not isinstance(actual_parameters, Mapping):
            raise ValueError("身体控制缺少当前参数读回")
        if actual_parameters.get("_controller_binary_sha256") != self.profile["controller_binary_sha256"]:
            raise ValueError("身体控制器当前二进制SHA与已审计版本不符")
        if actual_parameters.get("enable_udp_motors") is not True:
            raise ValueError("身体控制要求当前enable_udp_motors=True；缺失或未启用")
        if actual_parameters.get("udp_motor_host") != "127.0.0.1":
            raise ValueError("身体控制要求当前udp_motor_host=127.0.0.1；缺失或不匹配")
        port = actual_parameters.get("udp_motor_send_port")
        if isinstance(port, bool) or not isinstance(port, int) or port != 50001:
            raise ValueError("身体控制要求当前udp_motor_send_port=50001；缺失或不匹配")

    def validate_pose_target(self, joints: Mapping, grippers: Mapping) -> tuple[dict, dict]:
        """Pure validation; creates no ROS object and publishes no messages."""
        values = self._validated_joints(joints)
        if not isinstance(grippers, Mapping) or set(grippers) != {"left", "right"}:
            raise ValueError("姿态必须包含left/right双夹爪位置")
        grips = {side: _finite(grippers[side], f"gripper.{side}") for side in ("left", "right")}
        if any(not 0 <= value <= 100 for value in grips.values()):
            raise ValueError("夹爪姿态必须在0..100内")
        return values, grips

    def _validated_joints(self, joints: Mapping) -> dict[str, float]:
        body_limits = validate_body_profile(self.profile)
        if not isinstance(joints, Mapping) or set(joints) != set(POSE_NAMES):
            raise ValueError("姿态必须完整包含双臂14个和身体5个具名关节，禁止额外字段")
        arm_limits = LIMITS_BY_SHA256[self.profile["controller_binary_sha256"]]
        values = {}
        for name in POSE_NAMES:
            value = _finite(joints[name], name)
            low, high = (body_limits[name] if name in body_limits else
                         (arm_limits[f"{name}_min"], arm_limits[f"{name}_max"]))
            if not low <= value <= high:
                raise ValueError(f"姿态目标{name}={value}°超出已审计限位[{low}, {high}]，请先生成截断计划")
            values[name] = value
        return values

    def _pose_message(self, joints: Mapping, stamp, names=POSE_NAMES) -> tuple:
        msg = self._message_types["arms_command"]()
        msg.header.stamp = stamp
        msg.header.frame_id = self.profile["base_frame"]
        msg.name = list(names)
        msg.position = [joints[name] for name in names]
        msg.velocity = []
        msg.effort = []
        fields = {
            "header": {"stamp": self._stamp_fields(stamp), "frame_id": msg.header.frame_id},
            "name": list(msg.name), "position": list(msg.position),
            "velocity": [], "effort": [],
        }
        return "arms_command", msg, fields, stamp

    def publish_pose(self, joints: Mapping, grippers: Mapping) -> dict:
        self._require_open()
        if not self.commands_enabled:
            raise RuntimeError("尚未enable_commands()；拒绝发布姿态动作")
        values, grips = self.validate_pose_target(joints, grippers)
        stamp = self._stamp()
        # Validate and construct the entire batch before sending anything.
        messages = [
            self._pose_message(values, stamp),
            self._gripper_message("left", grips["left"], stamp),
            self._gripper_message("right", grips["right"], stamp),
            self._base_message([0.0, 0.0, 0.0], stamp),
        ]
        return self._publish_batch("pose", messages)

    def publish_body_sample(self, joints: Mapping, grippers: Mapping, *, include_auxiliary=False) -> dict:
        """Candidate high-rate body-only samples; other outputs retain their own cadence.

        Full targets are validated before constructing any message. This method
        does not declare the candidate rate or physical vibration validated.
        """
        if type(include_auxiliary) is not bool:
            raise ValueError('include_auxiliary必须为bool')
        if include_auxiliary:
            return self.publish_pose(joints, grippers)
        self._require_open()
        if not self.commands_enabled:
            raise RuntimeError('尚未enable_commands()；拒绝发布姿态动作')
        values, _ = self.validate_pose_target(joints, grippers)
        return self._publish_batch('body_pose', [self._pose_message(values, self._stamp(), BODY_NAMES)])

    def publish(self, action19) -> dict:
        raise RuntimeError("姿态后端仅使用publish_pose；不接受底盘运动回放动作")

    def hold_arms(self) -> dict | None:
        """Compatibility hook for stop_motion; hold all 19 measured joints.

No gripper command is sent. Stale or out-of-range body feedback prevents the
hold rather than silently retaining the last interpolation target.
"""
        if not self.commands_enabled:
            return None
        self._require_open()
        max_age = _finite(self.profile.get("feedback_max_age_s"), "feedback_max_age_s")
        if max_age <= 0:
            raise ValueError("feedback_max_age_s必须大于0")
        now_ns = time.monotonic_ns()
        joints = {}
        for name in POSE_NAMES:
            item = self._joints.get(name, {})
            stamp_ns = item.get("received_ns")
            if (isinstance(stamp_ns, bool) or not isinstance(stamp_ns, int)
                    or not 0 <= now_ns - stamp_ns <= max_age * 1e9):
                raise RuntimeError(f"停止时{name}反馈缺失或过期，未发送身体/双臂保持目标")
            joints[name] = item.get("position")
        values = self._validated_joints(joints)
        return self._publish_batch("hold_pose", [self._pose_message(values, self._stamp())])


class ArmsOnlyBackend(PoseBackend):
    """Reuse the pose scheduler while publishing only arms and a stationary base."""
    def publish_pose(self, joints, grippers):
        self._require_open()
        if not self.commands_enabled:
            raise RuntimeError('Call enable_commands before restoring arms')
        values, _ = self.validate_pose_target(joints, grippers)
        stamp = self._stamp()
        return self._publish_batch('restore_arms_only', [
            self._pose_message(values, stamp, ARM_NAMES),
            self._base_message([0., 0., 0.], stamp)])

    def hold_arms(self):
        return RosBackend.hold_arms(self)
