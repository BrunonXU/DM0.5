"""Online observation and all-or-nothing command gates; CPU fixtures, no robot."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
from f1_replay.bundle import ARM_NAMES, BODY_NAMES
# Load before patch.dict(sys.modules): package attributes and sys.modules must
# retain the same identity when the existing ROS fixture later patches evidence.
from f1_replay import controller_evidence
from f1_replay.runtime import ReplayError
from f1_policy_client.execution import prepare_prefix
from f1_policy_client.observation import CAMERAS, capture_observation
from f1_policy_client.ros import RosObservationBackend, dependency_check


NOW = 10_000_000_000
BINDING = {"checkpoint_sha256": "a" * 64, "stats_sha256": "b" * 64}


def profile():
    # Reuse the checked field layout; execution and feedback remain synthetic.
    return json.loads((TASK / "configs/replay_f1.json").read_text())


def snapshot():
    return {
        "joints": {name: {"position": 0.0, "received_ns": NOW}
                   for name in ARM_NAMES + BODY_NAMES},
        "grippers": {side: {"position": value, "received_ns": NOW}
                     for side, value in (("left", 50.0), ("right", 100.0))},
        "base": {"received_ns": NOW, "vx": 0.1, "vy": -0.2, "wz": 0.0,
                 "is_soft_auto": False, "is_hard_auto": True,
                 "is_parking_mode": False, "is_avoid": False},
        "cameras": {name: {"data": b"original-compressed-" + name.encode(),
                           "received_ns": NOW, "format": "jpeg",
                           "header_stamp_ns": 999_999_999_999_999}
                    for name in CAMERAS},
    }


def capture(value=None, **kwargs):
    options = {"now_ns": NOW, "feedback_max_age_s": 0.2,
               "camera_max_age_s": 0.2, "camera_max_spread_s": 0.1}
    options.update(kwargs)
    return capture_observation(snapshot() if value is None else value, **options)


def response():
    return {
        "contract": "f1_foam_inference_v1", "request_id": "request-7",
        "observation_monotonic_ns": NOW, "anchor_joints14_deg": [0.0] * 14,
        "status": "ready", "ik_recipe_version": "f1_ikfk_demo_v1",
        "model_binding": deepcopy(BINDING), "frequency_hz": 30, "execute_steps": 20,
        "joint_targets_deg": [[index / 10] + [0.0] * 13 for index in range(20)],
        "gripper_targets": [[50.0, 100.0] for _ in range(20)],
        "base_velocity_targets": [[0.1, -0.2, 0.0] for _ in range(20)],
    }


def prepare(value=None, **kwargs):
    options = {"now_ns": NOW, "request_id": "request-7",
               "expected_model_binding": BINDING, "max_observation_age_s": 2.0}
    options.update(kwargs)
    return prepare_prefix(response() if value is None else value, capture(),
                          snapshot(), profile(), **options)


class ObservationTests(unittest.TestCase):
    def test_exact_hal_gripper_and_measured_base_order(self):
        value = snapshot()
        for index, name in enumerate(ARM_NAMES):
            value["joints"][name]["position"] = float(index)
        observed = capture(value)
        self.assertEqual(observed.state19, tuple(float(i) for i in range(14)) +
                         (50.0, 100.0, 0.1, -0.2, 0.0))
        self.assertEqual(observed.anchor_joints14, observed.state19[:14])
        self.assertEqual(len(observed.body5), 5)
        self.assertEqual(observed.images["left"], value["cameras"]["left"]["data"])
        # Unrelated hardware stamp cannot make a fresh receipt appear stale/future.
        self.assertEqual(observed.camera_metadata["head"]["age_s"], 0.0)

    def test_reject_missing_stale_future_and_nonfinite_feedback(self):
        for section, key, field, bad in (
            ("joints", ARM_NAMES[3], "received_ns", NOW - 300_000_000),
            ("joints", BODY_NAMES[0], "position", float("nan")),
            ("grippers", "left", "position", -1),
            ("grippers", "right", "received_ns", NOW + 1),
        ):
            with self.subTest(section=section, key=key, field=field):
                value = snapshot()
                value[section][key][field] = bad
                with self.assertRaises(ReplayError):
                    capture(value)
        for base_change in ({"received_ns": None}, {"vx": float("inf")}, {"wz": None}):
            value = snapshot()
            value["base"].update(base_change)
            with self.assertRaises(ReplayError):
                capture(value)

    def test_reject_missing_stale_empty_or_unsynchronized_camera(self):
        for field, bad in (("received_ns", NOW - 300_000_000),
                           ("received_ns", NOW + 1), ("data", b""),
                           ("received_ns", NOW - 150_000_000)):
            with self.subTest(field=field, bad=bad):
                value = snapshot()
                value["cameras"]["right"][field] = bad
                with self.assertRaises(ReplayError):
                    capture(value)
        value = snapshot()
        del value["cameras"]["left"]
        with self.assertRaises(ReplayError):
            capture(value)

    def test_never_silently_reuses_a_different_snapshot(self):
        value = snapshot()
        observed = capture(value)
        value["joints"][ARM_NAMES[0]]["position"] = 99
        value["cameras"]["head"]["data"] = b"next-frame"
        self.assertEqual(observed.anchor_joints14[0], 0)
        self.assertEqual(observed.images["head"], b"original-compressed-head")


class PrefixTests(unittest.TestCase):
    def test_complete_h20_preserves_values_and_30hz_timeline(self):
        source = response()
        prepared = prepare(source)
        self.assertEqual(len(prepared.bundle.frames), 20)
        self.assertEqual(prepared.bundle.duration_ns, 666_666_667)
        for index, frame in enumerate(prepared.bundle.frames):
            self.assertEqual(frame.time_ns, round(index * 1e9 / 30))
            self.assertEqual(frame.action, tuple(source["joint_targets_deg"][index] +
                source["gripper_targets"][index] + source["base_velocity_targets"][index]))
        source["joint_targets_deg"][0][0] = 99
        self.assertEqual(prepared.bundle.frames[0].action[0], 0)
        self.assertEqual(prepared.response["joint_targets_deg"][0][0], 0)

    def test_reject_wrong_request_anchor_model_ik_and_timing(self):
        cases = {
            "request_id": "old-request", "contract": "g2", "observation_monotonic_ns": NOW - 1,
            "anchor_joints14_deg": [0.1] + [0.0] * 13, "model_binding": {"wrong": True},
            "ik_recipe_version": "new-ik", "status": "rejected", "frequency_hz": 25,
            "execute_steps": 19,
        }
        for key, bad in cases.items():
            with self.subTest(key=key):
                value = response()
                value[key] = bad
                with self.assertRaises(ReplayError):
                    prepare(value)

    def test_reject_expired_future_observation_and_unbound_server(self):
        for changes in ({"now_ns": NOW + 2_000_000_001}, {"now_ns": NOW - 1},
                        {"expected_model_binding": {}}, {"max_observation_age_s": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ReplayError):
                prepare(**changes)

    def test_invalid_final_step_rejects_entire_prefix_without_clipping(self):
        for field, column, bad in (("joint_targets_deg", 0, 200),
                                   ("joint_targets_deg", 0, float("nan")),
                                   ("gripper_targets", 0, -0.0001),
                                   ("gripper_targets", 1, 100.0001),
                                   ("base_velocity_targets", 0, 0.65001),
                                   ("base_velocity_targets", 2, 1e-9)):
            with self.subTest(field=field, column=column, bad=bad):
                value = response()
                value[field][-1][column] = bad
                before = deepcopy(value)
                with self.assertRaises(ReplayError):
                    prepare(value)
                if bad == bad:
                    self.assertEqual(value, before)

    def test_sdk_wrist_coupling_is_checked_even_inside_rectangular_limits(self):
        value = response()
        value["joint_targets_deg"][-1][5:7] = [60, 60]
        with self.assertRaisesRegex(ReplayError, "wrist coupling"):
            prepare(value)

    def test_short_prefix_or_bool_command_is_not_executable(self):
        for field in ("joint_targets_deg", "gripper_targets", "base_velocity_targets"):
            value = response()
            value[field].pop()
            with self.assertRaises(ReplayError):
                prepare(value)
        value = response()
        value["joint_targets_deg"][0][0] = True
        with self.assertRaises(ReplayError):
            prepare(value)

    def test_first_target_uses_current_feedback_and_keeps_request_anchor(self):
        value = response()
        value["joint_targets_deg"][0][0] = 3.0
        with self.assertRaisesRegex(ReplayError, "First IK target"):
            prepare(value)
        current = snapshot()
        current["joints"][ARM_NAMES[0]]["position"] = 2.5
        prepared = prepare_prefix(value, capture(), current, profile(), now_ns=NOW,
                                  request_id="request-7", expected_model_binding=BINDING,
                                  max_observation_age_s=2.0)
        self.assertEqual(prepared.response["anchor_joints14_deg"][0], 0.0)

    def test_live_body_mode_and_feedback_guards_are_reused(self):
        for mutate in (
            lambda value: value["joints"][BODY_NAMES[0]].update(position=10),
            lambda value: value["base"].update(is_soft_auto=True),
            lambda value: value["joints"][ARM_NAMES[0]].update(received_ns=1),
        ):
            current = snapshot()
            mutate(current)
            with self.assertRaises(ReplayError):
                prepare_prefix(response(), capture(), current, profile(), now_ns=NOW,
                               request_id="request-7", expected_model_binding=BINDING,
                               max_observation_age_s=2.0)


# Reuse the established ROS transport fixture instead of implementing ROS again.
_spec = importlib.util.spec_from_file_location(
    "_f1_inference_ros_test_fixture", Path(__file__).with_name("test_replay_ros_backend.py"))
_ros_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ros_fixture)


class CameraBackendTests(unittest.TestCase):
    def setUp(self):
        self.ros = _ros_fixture.FakeRos()
        self.ros.modules["sensor_msgs.msg"].CompressedImage = SimpleNamespace
        self.ros.modules["rclpy.qos"].ReliabilityPolicy.BEST_EFFORT = "best_effort"
        self.module_patch = patch.dict(sys.modules, self.ros.modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.backend = RosObservationBackend(profile())
        self.addCleanup(self.backend.close)

    def test_dependency_check_and_open_create_no_commands(self):
        self.assertTrue(dependency_check()["ok"])
        self.assertEqual(self.ros.init_calls, [])
        self.backend.open()
        node = self.ros.nodes[-1]
        self.assertEqual(len(node.subscriptions), 7)
        self.assertEqual(node.publishers, {})
        self.assertFalse(self.backend.commands_enabled)
        cameras = [sub for sub in node.subscriptions if "camera" in sub.topic]
        self.assertEqual(len(cameras), 3)
        self.assertTrue(all(sub.qos.depth == 1 for sub in cameras))
        self.backend.close()
        self.assertEqual(self.backend.snapshot()["cameras"], {})

    def test_original_bytes_receipt_clock_and_bad_frame_invalidation(self):
        self.backend.open()
        callback = self.backend._camera_callback("head")
        message = SimpleNamespace(data=b"jpeg-original", format="jpeg",
                                  header=SimpleNamespace(stamp=SimpleNamespace(sec=87, nanosec=12)))
        with patch("f1_policy_client.ros.time.monotonic_ns", return_value=NOW):
            callback(message)
        camera = self.backend.snapshot()["cameras"]["head"]
        self.assertEqual(camera["data"], b"jpeg-original")
        self.assertEqual(camera["received_ns"], NOW)
        self.assertEqual(camera["header_stamp_ns"], 87_000_000_012)
        message.data = b""
        callback(message)
        self.assertNotIn("head", self.backend.snapshot()["cameras"])

    def test_native_sensor_qos_is_never_deepcopied(self):
        class NativeQos:
            def __deepcopy__(self, memo):
                raise TypeError("cannot pickle rcl_duration_t")
        self.ros.modules["rclpy.qos"].qos_profile_sensor_data = NativeQos()
        self.backend.open()
        cameras = [sub for sub in self.ros.nodes[-1].subscriptions if "camera" in sub.topic]
        self.assertEqual(len(cameras), 3)
        self.assertTrue(all(sub.qos.depth == 1 and sub.qos.reliability == "best_effort"
                            for sub in cameras))

    def test_partial_camera_open_failure_closes_replay_context(self):
        self.ros.modules["sensor_msgs.msg"].__dict__.pop("CompressedImage")
        with self.assertRaises(ImportError):
            self.backend.open()
        self.assertIsNone(self.backend._context)
        self.assertFalse(self.backend.commands_enabled)
        self.assertFalse(any(node.publishers for node in self.ros.nodes))


if __name__ == "__main__":
    unittest.main()
