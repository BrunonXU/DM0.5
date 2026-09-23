"""Pose messages and stop semantics in fake ROS; never connect to hardware."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot"))
from f1_replay.controller_evidence import AUDITED_SHA256, NEW_F1_SHA256
from f1_replay.pose_backend import BODY_NAMES, POSE_NAMES, PoseBackend, validate_body_profile
from test_replay_ros_backend import FakeRos, profile as transport_profile


class PoseBackendTests(unittest.TestCase):
    def setUp(self):
        self.ros = FakeRos()
        self.patcher = patch.dict(sys.modules, self.ros.modules)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.profile = transport_profile()
        self.profile.update(joint_limit_source="verified_controller_binary",
                            controller_binary_sha256=AUDITED_SHA256, feedback_max_age_s=0.5)
        self.backend = PoseBackend(self.profile)
        self.addCleanup(self.backend.close)
        self.joints = {name: 0.0 for name in POSE_NAMES}
        self.joints.update(body_j3=-30.0, body_j4=-50.0, body_j5=-20.0)
        self.grippers = {"left": 25.0, "right": 100.0}

    def enable(self):
        self.backend.open()
        self.backend.enable_commands()
        return self.ros.nodes[-1]

    def sent(self):
        return sum(len(pub.messages) for pub in self.ros.nodes[-1].publishers.values())

    def test_open_remains_subscription_only_and_pose_requires_enable(self):
        self.backend.open()
        self.assertFalse(self.ros.nodes[-1].publishers)
        with self.assertRaisesRegex(RuntimeError, "enable_commands"):
            self.backend.publish_pose(self.joints, self.grippers)
        self.assertFalse(self.ros.nodes[-1].publishers)

    def test_unknown_binary_rejected_before_creating_publishers(self):
        self.backend.open()
        self.backend.profile["controller_binary_sha256"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.backend.enable_commands()
        self.assertFalse(self.ros.nodes[-1].publishers)

    def test_d071_body_limits_use_independently_decoded_binary_bounds(self):
        import json
        evidence = json.loads((Path(__file__).resolve().parents[1] /
            'results/pose_192_4f_20260918/decoded_limits.json').read_text())
        self.profile['controller_binary_sha256'] = NEW_F1_SHA256
        limits = validate_body_profile(self.profile)
        self.assertEqual({n:list(v) for n,v in limits.items()},
                         {n:evidence['compiled_limits_deg'][n] for n in BODY_NAMES})
        backend = PoseBackend(self.profile)
        backend.validate_pose_target(self.joints, self.grippers)
        bad = dict(self.joints, body_j4=-95.01)
        with self.assertRaises(ValueError): backend.validate_pose_target(bad, self.grippers)

    def test_ros_path_cannot_be_changed_to_unreviewed_endpoint(self):
        self.profile["topics"]["arms_command"] = "/unreviewed"
        with self.assertRaises(ValueError):
            validate_body_profile(self.profile)

    def test_body_parameters_require_actual_binary_and_enabled_local_udp(self):
        params = {"_controller_binary_sha256": AUDITED_SHA256, "enable_udp_motors": True,
                  "udp_motor_host": "127.0.0.1", "udp_motor_send_port": 50001}
        self.backend.check_body_parameters(params)
        for key in params:
            missing = dict(params); del missing[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.backend.check_body_parameters(missing)
        for key, value in (("_controller_binary_sha256", "different"),
                           ("enable_udp_motors", 1), ("enable_udp_motors", False),
                           ("udp_motor_host", "localhost"), ("udp_motor_host", "192.168.1.1"),
                           ("udp_motor_send_port", 50002), ("udp_motor_send_port", 50001.0)):
            wrong = dict(params); wrong[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.backend.check_body_parameters(wrong)
        self.assertFalse(self.ros.nodes)

    def test_pure_target_validation_has_no_ros_side_effects(self):
        values, grippers = self.backend.validate_pose_target(self.joints, self.grippers)
        self.assertEqual(values, self.joints)
        self.assertEqual(grippers, self.grippers)
        for key, value in (("body_j4", -96), ("arm_l_j4", -146)):
            joints = dict(self.joints); joints[key] = value
            with self.assertRaises(ValueError):
                self.backend.validate_pose_target(joints, self.grippers)
        self.assertFalse(self.ros.nodes)

    def test_pose_batch_contains_all_19_degrees_and_zero_base(self):
        node = self.enable()
        record = self.backend.publish_pose(self.joints, self.grippers)
        self.assertEqual(len(record["commands"]), 4)
        msg = node.publishers["/motion_ctl/joint_ctl"].messages[0]
        self.assertEqual(msg.name, list(POSE_NAMES))
        self.assertEqual(msg.position, [self.joints[name] for name in POSE_NAMES])
        self.assertFalse(any(name.startswith("head_") for name in msg.name))
        base = node.publishers["/move/ManualMoveCmd"].messages[0]
        self.assertEqual((base.twist.linear.x, base.twist.linear.y, base.twist.angular.z), (0, 0, 0))

    def test_arms_only_restore_and_stop_never_publish_body_or_grippers(self):
        from f1_replay.pose_backend import ArmsOnlyBackend, ARM_NAMES
        self.backend = ArmsOnlyBackend(self.profile)
        self.addCleanup(self.backend.close)
        node = self.enable()
        self.backend.publish_pose(self.joints, self.grippers)
        self.backend._joints = {n: {'position':v, 'received_ns':time.monotonic_ns()}
                                for n,v in self.joints.items()}
        self.backend.hold_arms()
        for message in node.publishers['/motion_ctl/joint_ctl'].messages:
            self.assertEqual(message.name, list(ARM_NAMES))
        for side in ('left','right'):
            self.assertEqual(node.publishers[f'/motion_ctl/gripper/{side}'].messages, [])

    def test_body_samples_do_not_raise_arm_gripper_or_base_cadence(self):
        node = self.enable()
        for index in range(201):
            self.backend.publish_body_sample(self.joints, self.grippers, include_auxiliary=index % 8 == 0)
        messages = node.publishers['/motion_ctl/joint_ctl'].messages
        self.assertEqual(len(messages), 201)
        self.assertEqual(sum(len(msg.name) == 19 for msg in messages), 26)
        self.assertEqual(messages[1].name, list(BODY_NAMES))
        self.assertEqual(len(node.publishers['/move/ManualMoveCmd'].messages), 26)
        for side in ('left', 'right'):
            self.assertEqual(len(node.publishers[f'/motion_ctl/gripper/{side}'].messages), 26)
        before = self.sent()
        bad = dict(self.joints, body_j4=-96)
        with self.assertRaises(ValueError):
            self.backend.publish_body_sample(bad, self.grippers)
        self.assertEqual(before, self.sent())

    def test_bad_body_missing_extra_nonfinite_or_outside_bounds_publish_nothing(self):
        self.enable()
        candidates = []
        for key, value in (("body_j4", -96), ("body_j3", float("nan")), ("head_j1", 0)):
            candidate = dict(self.joints); candidate[key] = value; candidates.append(candidate)
        candidate = dict(self.joints); del candidate["body_j5"]; candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.backend.publish_pose(candidate, self.grippers)
        self.assertEqual(self.sent(), 0)

    def test_profile_cannot_expand_actual_arm_or_body_limits(self):
        self.enable()
        self.backend.profile["joint_limits_deg"] = {name: [-1000, 1000] for name in POSE_NAMES}
        joints = dict(self.joints); joints["arm_r_j2"] = -94.1
        with self.assertRaises(ValueError):
            self.backend.publish_pose(joints, self.grippers)
        self.assertEqual(self.sent(), 0)

    def test_invalid_gripper_does_not_send_joint_batch(self):
        self.enable()
        with self.assertRaises(ValueError):
            self.backend.publish_pose(self.joints, {"left": 101, "right": 0})
        self.assertEqual(self.sent(), 0)

    def test_hold_sends_fresh19_only_without_opening_grippers(self):
        node = self.enable()
        self.backend._joints = {name: {"position": value, "received_ns": time.monotonic_ns()}
                                for name, value in self.joints.items()}
        record = self.backend.hold_arms()
        self.assertEqual(record["kind"], "hold_pose")
        self.assertEqual(self.sent(), 1)
        self.assertEqual(node.publishers["/motion_ctl/joint_ctl"].messages[0].name, list(POSE_NAMES))

    def test_stale_missing_future_or_outside_body_feedback_blocks_hold(self):
        self.enable()
        original = {name: {"position": value, "received_ns": time.monotonic_ns()}
                    for name, value in self.joints.items()}
        for mutation in ("stale", "missing", "future", "outside"):
            self.backend._joints = deepcopy(original)
            item = self.backend._joints["body_j4"]
            if mutation == "stale": item["received_ns"] -= 2_000_000_000
            if mutation == "missing": del self.backend._joints["body_j4"]
            if mutation == "future": item["received_ns"] += 2_000_000_000
            if mutation == "outside": item["position"] = -96
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, RuntimeError)):
                self.backend.hold_arms()
        self.assertEqual(self.sent(), 0)

    def test_arm_only_action_cannot_accidentally_move_base(self):
        self.enable()
        with self.assertRaises(RuntimeError):
            self.backend.publish([0.0] * 19)
        self.assertEqual(self.sent(), 0)


if __name__ == "__main__":
    unittest.main()
