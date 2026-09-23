"""ROS transport contract tests using isolated fake modules; no ROS or robot."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


BACKEND_PATH = Path(__file__).resolve().parents[1] / "robot/f1_replay/ros_backend.py"
SPEC = importlib.util.spec_from_file_location("replay_ros_backend_under_test", BACKEND_PATH)
backend_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backend_module)


def profile():
    """Transport fixture values; not verified F1 operating specifications."""
    return {
        "topics": {
            "arms_command": "/motion_ctl/joint_ctl",
            "left_gripper_command": "/motion_ctl/gripper/left",
            "right_gripper_command": "/motion_ctl/gripper/right",
            "base_command": "/move/ManualMoveCmd",
            "joint_state": "/hal/joint_states",
            "left_gripper_state": "/motion_ctl/gripper/left/state",
            "right_gripper_state": "/motion_ctl/gripper/right/state",
            "base_state": "/move/State",
        },
        "base_frame": "base_link",
        "gripper_max_effort": {"left": 1.2, "right": 3.4},
    }


class FakeContext:
    def __init__(self):
        self.active = False

    def ok(self):
        return self.active

    def try_shutdown(self):
        self.active = False


class FakeExecutor:
    def __init__(self, context):
        self.context = context
        self.shutdown_calls = 0

    def shutdown(self, timeout_sec):
        self.shutdown_calls += 1


class FakeJointState:
    def __init__(self):
        self.header = NS(stamp=NS(sec=0, nanosec=0), frame_id="")
        self.name, self.position, self.velocity, self.effort = [], [], [], []


class FakeGripperCommand:
    def __init__(self):
        self.position, self.max_effort = 0.0, 0.0


class FakeTwistStamped:
    def __init__(self):
        self.header = NS(stamp=NS(sec=0, nanosec=0), frame_id="")
        self.twist = NS(linear=NS(x=0.0, y=0.0, z=0.0),
                        angular=NS(x=0.0, y=0.0, z=0.0))


class FakePublisher:
    def __init__(self, message_type, topic, qos):
        self.message_type, self.topic, self.qos = message_type, topic, qos
        self.messages = []
        self.fail = False

    def publish(self, message):
        if self.fail:
            raise RuntimeError("synthetic publish failure")
        self.messages.append(deepcopy(message))


class FakeParameterClient:
    def __init__(self):
        self.requests = []

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        values = [NS(type=4, string_value="omnipicker") if name in ("left_gripper_type", "right_gripper_type")
                  else NS(type=2, integer_value=1) if name in ("arm_control_mode", "arm_impedance_type")
                  else NS(type=3, double_value=(-178.0 if name.endswith("_min") else 178.0))
                  for name in request.names]
        return NS(done=lambda: True, result=lambda: NS(values=values))


class FakeListParameterClient(FakeParameterClient):
    def call_async(self, request):
        self.requests.append(request)
        names = ["arm_control_mode", "arm_impedance_type", "left_gripper_type", "right_gripper_type"] + [
            f"{name}_{bound}" for name in backend_module.ARM_NAMES for bound in ("min", "max")]
        return NS(done=lambda: True, result=lambda: NS(result=NS(names=names)))


class FakeNode:
    def __init__(self, ros, name, options):
        self.ros, self.name, self.options = ros, name, options
        self.publishers, self.subscriptions, self.clients = {}, [], []
        self.destroyed_clients = []
        self.destroy_calls = 0
        self.fail_publisher_topic = None

    def get_name(self):
        return self.name

    def get_namespace(self):
        return self.options["namespace"]

    def create_subscription(self, message_type, topic, callback, qos):
        if self.ros.fail_subscription:
            raise RuntimeError("synthetic subscription creation failure")
        subscription = NS(message_type=message_type, topic=topic, callback=callback, qos=qos)
        self.subscriptions.append(subscription)
        return subscription

    def create_publisher(self, message_type, topic, qos):
        if topic == self.fail_publisher_topic:
            raise RuntimeError("synthetic publisher creation failure")
        publisher = FakePublisher(message_type, topic, qos)
        self.publishers[topic] = publisher
        return publisher

    def get_clock(self):
        def now():
            self.ros.clock_ns += 1_000_000
            stamp = NS(sec=self.ros.clock_ns // 1_000_000_000,
                       nanosec=self.ros.clock_ns % 1_000_000_000)
            return NS(to_msg=lambda: stamp)
        return NS(now=now)

    def get_publishers_info_by_topic(self, topic):
        return []

    def get_subscriptions_info_by_topic(self, topic):
        message_types = {
            "/motion_ctl/joint_ctl": "sensor_msgs/msg/JointState",
            "/motion_ctl/gripper/left": "control_msgs/msg/GripperCommand",
            "/motion_ctl/gripper/right": "control_msgs/msg/GripperCommand",
            "/move/ManualMoveCmd": "geometry_msgs/msg/TwistStamped",
        }
        return [NS(node_name="controller", node_namespace="/", topic_type=message_types[topic])]

    def create_client(self, service_type, service):
        client = FakeListParameterClient() if service.endswith("/list_parameters") else FakeParameterClient()
        self.clients.append((service_type, service, client))
        return client

    def destroy_client(self, client):
        self.destroyed_clients.append(client)

    def destroy_node(self):
        self.destroy_calls += 1


class FakeRos:
    """Modules are installed with patch.dict and restored after each test."""

    def __init__(self):
        self.nodes, self.init_calls, self.poll_calls, self.checked_types = [], [], [], []
        self.clock_ns = 123_000_000_000
        self.fail_subscription = False
        self.modules = {}
        self.add("rclpy", init=self.init, create_node=self.create_node, spin_once=self.spin_once)
        self.add("rclpy.context", Context=FakeContext)
        self.add("rclpy.executors", SingleThreadedExecutor=FakeExecutor)
        self.add("rclpy.qos", DurabilityPolicy=NS(VOLATILE="volatile"),
                 HistoryPolicy=NS(KEEP_LAST="keep_last"), QoSProfile=lambda **kw: NS(**kw),
                 ReliabilityPolicy=NS(RELIABLE="reliable"),
                 qos_profile_sensor_data=NS(depth=5, reliability="best_effort"))
        self.add("rclpy.signals", SignalHandlerOptions=NS(NO="no_signal_handlers"))
        self.add("rclpy.type_support", check_for_type_support=self.checked_types.append)
        for parent in ("sensor_msgs", "control_msgs", "geometry_msgs", "move", "rcl_interfaces"):
            self.add(parent)
        self.add("sensor_msgs.msg", JointState=FakeJointState)
        self.add("control_msgs.msg", GripperCommand=FakeGripperCommand)
        self.add("geometry_msgs.msg", TwistStamped=FakeTwistStamped)
        self.add("move.msg", State=NS)
        self.add("rcl_interfaces.srv", GetParameters=NS(Request=NS), ListParameters=NS(Request=NS))
        self.add("rcl_interfaces.msg", ParameterType=NS(PARAMETER_BOOL=1, PARAMETER_INTEGER=2, PARAMETER_DOUBLE=3, PARAMETER_STRING=4))

    def add(self, name, **symbols):
        module = ModuleType(name)
        module.__dict__.update(symbols)
        self.modules[name] = module
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(self.modules[parent], child, module)

    def init(self, **options):
        self.init_calls.append(options)
        options["context"].active = True

    def create_node(self, name, **options):
        node = FakeNode(self, name, options)
        self.nodes.append(node)
        return node

    def spin_once(self, node, **options):
        self.poll_calls.append((node, options))


class ReplayRosBackendTests(unittest.TestCase):
    def setUp(self):
        self.ros = FakeRos()
        self.modules_patch = patch.dict(sys.modules, self.ros.modules)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        self.profile = profile()
        self.backend = backend_module.RosBackend(self.profile)
        self.addCleanup(self.backend.close)

    def open(self):
        self.backend.open()
        return self.ros.nodes[-1]

    def enable(self):
        node = self.open()
        self.backend.enable_commands()
        return node

    def test_constructor_and_dependency_check_do_not_initialize_ros(self):
        self.assertFalse(self.ros.init_calls)
        self.assertIsNone(self.backend.stop_base())
        self.assertIsNone(self.backend.hold_arms())
        report = backend_module.dependency_check()
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(self.ros.checked_types)
        self.assertFalse(self.ros.init_calls)
        self.assertFalse(self.ros.nodes)

    def test_open_and_peer_check_create_only_feedback_subscriptions(self):
        node = self.open()
        self.assertEqual(len(node.subscriptions), 4)
        self.assertEqual({s.topic for s in node.subscriptions}, {
            "/hal/joint_states", "/motion_ctl/gripper/left/state",
            "/motion_ctl/gripper/right/state", "/move/State",
        })
        self.assertTrue(all(s.qos.reliability == "best_effort" for s in node.subscriptions))
        peers = self.backend.command_peers()
        self.assertEqual(len(peers), 4)
        self.assertEqual(peers["/move/ManualMoveCmd"]["subscribers"][0]["name"], "controller")
        self.assertEqual(peers["/move/ManualMoveCmd"]["subscribers"][0]["type"],
                         "geometry_msgs/msg/TwistStamped")
        self.backend.poll(0.0)
        self.assertGreaterEqual(len(self.ros.poll_calls), 1)
        self.assertFalse(node.publishers)
        self.assertFalse(node.clients)
        self.assertFalse(self.backend.commands_enabled)
        self.assertEqual(self.ros.init_calls[0]["signal_handler_options"], "no_signal_handlers")

    def test_poll_consumes_pending_hal_after_other_feedback_callback(self):
        self.open()
        self.backend._joints['arm_l_j1']={'position':0.0,'received_ns':1}
        pending=[lambda:None, lambda:None,
                 lambda:self.backend._joints['arm_l_j1'].update(position=2.0,received_ns=999)]
        def spin(node, **kwargs):
            if pending:pending.pop(0)()
        with patch.object(self.backend._rclpy,'spin_once',side_effect=spin):
            self.backend.poll(0.0)
        self.assertEqual(self.backend.snapshot()['joints']['arm_l_j1']['received_ns'],999)
        self.assertFalse(self.backend.commands_enabled)

    def test_callback_drain_does_not_fabricate_missing_hal(self):
        self.open()
        self.backend._joints['arm_l_j1']={'position':0.0,'received_ns':1}
        self.backend.poll(0.0)
        self.assertEqual(self.backend.snapshot()['joints']['arm_l_j1']['received_ns'],1)

    def test_enable_creates_four_publishers_and_sends_nothing(self):
        node = self.enable()
        self.assertTrue(self.backend.commands_enabled)
        self.assertEqual(len(node.publishers), 4)
        self.assertFalse(any(p.messages for p in node.publishers.values()))
        self.assertFalse(node.clients)
        for publisher in node.publishers.values():
            self.assertEqual((publisher.qos.depth, publisher.qos.history,
                              publisher.qos.reliability, publisher.qos.durability),
                             (1, "keep_last", "reliable", "volatile"))
        previous_publishers = dict(node.publishers)
        self.backend.enable_commands()
        self.assertEqual(node.publishers, previous_publishers)

    def test_action19_preserves_values_names_and_uses_new_ros_stamps(self):
        node = self.enable()
        action = [-35.0 + i * 2.5 for i in range(14)] + [12.5, 99.0, 0.21, -0.32, 0.43]
        original = list(action)
        first = self.backend.publish(action)
        second = self.backend.publish(action)
        arms = node.publishers["/motion_ctl/joint_ctl"].messages
        self.assertEqual(arms[0].name,
                         [f"arm_l_j{i}" for i in range(1, 8)] +
                         [f"arm_r_j{i}" for i in range(1, 8)])
        self.assertEqual(arms[0].position, action[:14])
        self.assertEqual((arms[0].velocity, arms[0].effort), ([], []))
        left = node.publishers["/motion_ctl/gripper/left"].messages[0]
        right = node.publishers["/motion_ctl/gripper/right"].messages[0]
        self.assertEqual((left.position, left.max_effort), (12.5, 1.2))
        self.assertEqual((right.position, right.max_effort), (99.0, 3.4))
        base = node.publishers["/move/ManualMoveCmd"].messages[0]
        self.assertEqual((base.twist.linear.x, base.twist.linear.y, base.twist.angular.z),
                         (0.21, -0.32, 0.43))
        self.assertEqual((base.twist.linear.z, base.twist.angular.x, base.twist.angular.y),
                         (0.0, 0.0, 0.0))
        self.assertEqual(base.header.frame_id, "base_link")
        self.assertEqual(base.header.stamp, arms[0].header.stamp)
        self.assertGreater(arms[1].header.stamp.nanosec, arms[0].header.stamp.nanosec)
        self.assertNotEqual(first["commands"][0]["ros_stamp"], second["commands"][0]["ros_stamp"])
        self.assertEqual(first["commands"][0]["fields"]["position"], action[:14])
        self.assertEqual(first["commands"][0]["ros_stamp"], vars(arms[0].header.stamp))
        self.assertFalse(first["commands"][1]["stamp_in_message"])
        self.assertNotIn("header", first["commands"][1]["fields"])
        self.assertTrue(all(c["status"] == "sent" and c["returned_ns"] >= c["sent_ns"]
                            for c in first["commands"]))
        self.assertEqual(action, original)

    def test_invalid_action_sends_nothing(self):
        node = self.enable()
        for action in ([0.0] * 18, [0.0] * 18 + [float("nan")], [0.0] * 18 + [True]):
            with self.assertRaises(ValueError):
                self.backend.publish(action)
        self.assertFalse(any(p.messages for p in node.publishers.values()))

    def test_partial_publish_failure_records_sent_and_uncertain_topics(self):
        node = self.enable()
        node.publishers["/motion_ctl/gripper/right"].fail = True
        with self.assertRaises(backend_module.PublishError) as caught:
            self.backend.publish([0.0] * 19)
        error = caught.exception
        self.assertEqual([c["topic"] for c in error.sent_records],
                         ["/motion_ctl/joint_ctl", "/motion_ctl/gripper/left"])
        self.assertEqual(error.record["commands"][-1]["topic"], "/motion_ctl/gripper/right")
        self.assertEqual(error.record["commands"][-1]["status"], "error_delivery_unknown")
        self.assertEqual(self.backend.last_publication, error.record)
        self.assertFalse(node.publishers["/move/ManualMoveCmd"].messages)
        self.backend.stop_base()
        self.assertEqual(len(node.publishers["/move/ManualMoveCmd"].messages), 1)

    def test_partial_enable_preserves_base_zero_stop_path(self):
        node = self.open()
        node.fail_publisher_topic = "/motion_ctl/joint_ctl"
        with self.assertRaisesRegex(RuntimeError, "publisher creation failure"):
            self.backend.enable_commands()
        self.assertFalse(self.backend.commands_enabled)
        self.assertEqual(list(node.publishers), ["/move/ManualMoveCmd"])
        base_pub = node.publishers["/move/ManualMoveCmd"]
        self.assertFalse(base_pub.messages)
        with self.assertRaises(RuntimeError):
            self.backend.publish([0.0] * 19)
        self.backend.stop_base()
        self.assertEqual(len(base_pub.messages), 1)
        msg = base_pub.messages[0]
        self.assertEqual((msg.twist.linear.x, msg.twist.linear.y, msg.twist.angular.z),
                         (0.0, 0.0, 0.0))
        self.backend.close()
        self.backend.close()
        self.assertEqual(node.destroy_calls, 1)
        self.assertEqual(len(base_pub.messages), 1)

    def test_close_is_idempotent_and_never_sends_motion(self):
        node = self.enable()
        context = self.backend._context
        executor = self.backend._executor
        self.backend.close()
        self.backend.close()
        self.assertEqual(node.destroy_calls, 1)
        self.assertEqual(executor.shutdown_calls, 1)
        self.assertFalse(context.ok())
        self.assertFalse(any(p.messages for p in node.publishers.values()))
        self.assertIsNone(self.backend.stop_base())

    def test_failed_open_cleans_up_without_publishers(self):
        self.ros.fail_subscription = True
        with self.assertRaisesRegex(RuntimeError, "subscription creation failure"):
            self.backend.open()
        node = self.ros.nodes[-1]
        self.assertEqual(node.destroy_calls, 1)
        self.assertFalse(node.publishers)
        self.assertFalse(self.backend.commands_enabled)
        self.backend.close()
        self.assertEqual(node.destroy_calls, 1)

    def test_controller_parameter_read_uses_only_list_and_get_parameters(self):
        node = self.open()
        report = self.backend.read_controller_parameters()
        self.assertEqual(report["service"], "/motor_ctl/get_parameters")
        self.assertEqual(len(report["parameters"]), 32)
        self.assertEqual(report["parameters"]["arm_control_mode"], 1)
        self.assertEqual(report["parameters"]["arm_r_j7_max"], 178.0)
        self.assertEqual(report["requested_count"], 32)
        self.assertEqual(report["returned_count"], 32)
        self.assertEqual(report["stage"], "complete")
        self.assertEqual([service for _, service, _ in node.clients],
                         ["/motor_ctl/list_parameters", "/motor_ctl/get_parameters"])
        self.assertEqual(node.destroyed_clients, [node.clients[1][2], node.clients[0][2]])
        self.assertFalse(node.publishers)

    def test_parameter_short_response_keeps_counts_and_destroys_client(self):
        node = self.open()
        response = NS(values=[])
        with patch.object(FakeParameterClient, "call_async",
                          return_value=NS(done=lambda: True, result=lambda: response)):
            with self.assertRaises(backend_module.ParameterReadError) as raised:
                self.backend.read_controller_parameters()
        self.assertEqual(raised.exception.diagnostics["requested_count"], 32)
        self.assertEqual(raised.exception.diagnostics["returned_count"], 0)
        self.assertIn("requested_count=32", str(raised.exception))
        self.assertIn("returned_count=0", str(raised.exception))
        self.assertEqual(len(node.destroyed_clients), 2)
        self.assertFalse(node.publishers)

    def test_pose_udp_parameters_decode_bool_string_and_integer_without_publishers(self):
        node = self.open()
        self.backend.profile["require_body_control"] = True
        names = ["enable_udp_motors", "udp_motor_host", "udp_motor_send_port"]
        listed = NS(done=lambda: True, result=lambda: NS(result=NS(names=names)))
        values = [NS(type=1, bool_value=True), NS(type=4, string_value="127.0.0.1"),
                  NS(type=2, integer_value=50001)]
        with patch.object(FakeListParameterClient, "call_async", return_value=listed), \
                patch.object(FakeParameterClient, "call_async",
                             return_value=NS(done=lambda: True, result=lambda: NS(values=values))):
            report = self.backend.read_controller_parameters()
        self.assertEqual(report["parameters"], dict(zip(names, (True, "127.0.0.1", 50001))))
        self.assertIs(report["parameters"]["enable_udp_motors"], True)
        self.assertFalse(node.publishers)

    def test_pose_enable_udp_parameter_requires_bool_type(self):
        node = self.open()
        self.backend.profile["require_body_control"] = True
        listed = NS(done=lambda: True, result=lambda: NS(result=NS(names=["enable_udp_motors"])))
        response = NS(values=[NS(type=2, integer_value=1)])
        with patch.object(FakeListParameterClient, "call_async", return_value=listed), \
                patch.object(FakeParameterClient, "call_async",
                             return_value=NS(done=lambda: True, result=lambda: response)):
            with self.assertRaisesRegex(backend_module.ParameterReadError, "enable_udp_motors"):
                self.backend.read_controller_parameters()
        self.assertFalse(node.publishers)

    def test_pose_missing_udp_parameters_remain_explicit(self):
        node = self.open()
        self.backend.profile["require_body_control"] = True
        report = self.backend.read_controller_parameters()
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing_names"],
                         ["enable_udp_motors", "udp_motor_host", "udp_motor_send_port"])
        self.assertFalse(node.publishers)

    def test_parameter_future_exception_keeps_original_detail(self):
        node = self.open()
        def fail():
            raise ValueError("synthetic server exception")
        with patch.object(FakeParameterClient, "call_async",
                          return_value=NS(done=lambda: True, result=fail)):
            with self.assertRaises(backend_module.ParameterReadError) as raised:
                self.backend.read_controller_parameters()
        diagnostic = raised.exception.diagnostics
        self.assertEqual(diagnostic["requested_count"], 32)
        self.assertIsNone(diagnostic["returned_count"])
        self.assertEqual(diagnostic["stage"], "reading_response")
        self.assertIn("ValueError: synthetic server exception", diagnostic["error"])
        self.assertEqual(len(node.destroyed_clients), 2)
        self.assertFalse(node.publishers)

    def test_only_declared_modes_are_requested_missing_limits_remain_explicit(self):
        node = self.open()
        names = ["arm_control_mode", "arm_impedance_type", "unrelated_parameter"]
        listed = NS(done=lambda: True, result=lambda: NS(result=NS(names=names)))
        with patch.object(FakeListParameterClient, "call_async", return_value=listed):
            report = self.backend.read_controller_parameters()
        self.assertEqual(node.clients[1][2].requests[0].names, names[:2])
        self.assertEqual(report["parameters"], {"arm_control_mode": 1, "arm_impedance_type": 1})
        self.assertEqual(report["requested_count"], 2)
        self.assertEqual(report["returned_count"], 2)
        self.assertEqual(report["expected_count"], 32)
        self.assertEqual(len(report["missing_names"]), 30)
        self.assertFalse(report["complete"])
        self.assertFalse(node.publishers)

    def test_no_declared_replay_parameters_does_not_call_empty_get(self):
        node = self.open()
        listed = NS(done=lambda: True, result=lambda: NS(result=NS(names=[])))
        with patch.object(FakeListParameterClient, "call_async", return_value=listed):
            report = self.backend.read_controller_parameters()
        self.assertEqual([service for _, service, _ in node.clients], ["/motor_ctl/list_parameters"])
        self.assertEqual(report["requested_count"], 0)
        self.assertEqual(len(report["missing_names"]), 32)
        self.assertFalse(report["complete"])
        self.assertEqual(len(node.destroyed_clients), 1)
        self.assertFalse(node.publishers)

    def test_binary_fills_only_ros_missing_limits_preserving_ros_values(self):
        sys.path.insert(0, str(BACKEND_PATH.parents[1]))
        from f1_replay import controller_evidence
        self.backend.profile.update(joint_limit_source="verified_controller_binary",
                                    controller_binary_sha256=controller_evidence.AUDITED_SHA256)
        node = self.open()
        names = ["arm_control_mode", "arm_impedance_type", "left_gripper_type", "right_gripper_type", "arm_l_j1_min"]
        listed = NS(done=lambda: True, result=lambda: NS(result=NS(names=names)))
        values = [NS(type=2, integer_value=3), NS(type=2, integer_value=1),
                  NS(type=4, string_value="omnipicker"), NS(type=4, string_value="omnipicker"),
                  NS(type=3, double_value=-170.0)]
        readback = NS(done=lambda: True, result=lambda: NS(values=values))
        binary = {"sha256": controller_evidence.AUDITED_SHA256,
                  "parameters": controller_evidence.LIMITS_BY_SHA256[controller_evidence.AUDITED_SHA256]}
        with patch.object(FakeListParameterClient, "call_async", return_value=listed), \
                patch.object(FakeParameterClient, "call_async", return_value=readback), \
                patch.object(backend_module, "__package__", "f1_replay"), \
                patch.object(backend_module, "__spec__", NS(parent="f1_replay")), \
                patch.object(controller_evidence, "read_verified_binary_limits", return_value=binary):
            report = self.backend.read_controller_parameters()
        self.assertEqual(report["parameters"]["arm_l_j1_min"], -170.0)
        self.assertEqual(report["parameters"]["arm_l_j1_max"], 178)
        self.assertEqual(report["parameters"]["left_gripper_type"], "omnipicker")
        self.assertEqual(len(report["ros_missing_names"]), 27)
        self.assertEqual(report["missing_names"], [])
        self.assertEqual(report["parameters"]["_controller_binary_sha256"], controller_evidence.AUDITED_SHA256)
        self.assertTrue(report["complete"])
        self.assertFalse(node.publishers)

    def test_binary_is_verified_even_when_all_limits_are_ros_parameters(self):
        sys.path.insert(0, str(BACKEND_PATH.parents[1]))
        from f1_replay import controller_evidence
        self.backend.profile.update(joint_limit_source="verified_controller_binary",
                                    controller_binary_sha256=controller_evidence.AUDITED_SHA256)
        node = self.open()
        binary = {"sha256": controller_evidence.AUDITED_SHA256,
                  "parameters": controller_evidence.LIMITS_BY_SHA256[controller_evidence.AUDITED_SHA256]}
        with patch.object(backend_module, "__package__", "f1_replay"), \
                patch.object(backend_module, "__spec__", NS(parent="f1_replay")), \
                patch.object(controller_evidence, "read_verified_binary_limits", return_value=binary) as verify:
            report = self.backend.read_controller_parameters()
        verify.assert_called_once_with(self.backend.profile)
        self.assertEqual(report["parameters"]["_controller_binary_sha256"], controller_evidence.AUDITED_SHA256)
        # Fake ROS reports -178 for j2; binary has -94. ROS must retain priority.
        self.assertEqual(report["parameters"]["arm_l_j2_min"], -178.0)
        self.assertEqual(report["ros_missing_names"], [])
        self.assertTrue(report["complete"])
        self.assertFalse(node.publishers)

    def test_parameter_rmw_helper_success_keeps_parent_environment_and_no_publishers(self):
        self.backend.profile["controller_parameter_rmw"] = "rmw_fastrtps_cpp"
        node = self.open()
        response = {"parameters": {"arm_control_mode": 1}, "complete": True,
                    "transport": {"kind": "current_process", "rmw": "rmw_fastrtps_cpp"}}
        process = NS(returncode=0, stdout=json.dumps({"ok": True, "result": response}), stderr="")
        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}), \
                patch.object(backend_module.subprocess, "run", return_value=process) as run:
            result = self.backend.read_controller_parameters(timeout_sec=2.0)
            self.assertEqual(os.environ["RMW_IMPLEMENTATION"], "rmw_cyclonedds_cpp")
        args, options = run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "f1_replay.parameter_probe"])
        self.assertEqual(options["env"]["RMW_IMPLEMENTATION"], "rmw_fastrtps_cpp")
        self.assertEqual(options["timeout"], 5.0)
        self.assertEqual(json.loads(options["input"])["profile"], self.backend.profile)
        self.assertEqual(result["parameters"], response["parameters"])
        self.assertEqual(result["transport"]["parent_rmw"], "rmw_cyclonedds_cpp")
        self.assertFalse(node.clients)
        self.assertFalse(node.publishers)

    def test_parameter_rmw_helper_timeout_is_reported_without_fallback_or_commands(self):
        self.backend.profile["controller_parameter_rmw"] = "rmw_fastrtps_cpp"
        node = self.open()
        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}), \
                patch.object(backend_module.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("parameter_probe", 5)):
            with self.assertRaises(backend_module.ParameterReadError) as error:
                self.backend.read_controller_parameters(timeout_sec=2.0)
        self.assertIn("TimeoutExpired", error.exception.diagnostics["error"])
        self.assertFalse(node.clients)
        self.assertFalse(node.publishers)

    def test_parameter_helper_wait_keeps_parent_feedback_polling(self):
        self.backend.profile["controller_parameter_rmw"] = "rmw_fastrtps_cpp"
        self.open()
        polled = threading.Event()

        def helper_waits_for_parent(*args, **kwargs):
            self.assertTrue(polled.wait(timeout=1.0))
            return NS(returncode=0, stdout=json.dumps({"ok": True, "result": {"parameters": {}}}), stderr="")

        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}), \
                patch.object(backend_module.subprocess, "run", side_effect=helper_waits_for_parent), \
                patch.object(self.backend, "poll", side_effect=lambda timeout: polled.set()) as poll:
            self.backend.read_controller_parameters()
        poll.assert_called_with(0.05)

    def test_matching_fast_parameter_rmw_uses_local_services_without_recursion(self):
        self.backend.profile["controller_parameter_rmw"] = "rmw_fastrtps_cpp"
        node = self.open()
        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_fastrtps_cpp"}), \
                patch.object(backend_module.subprocess, "run") as run:
            result = self.backend.read_controller_parameters()
        run.assert_not_called()
        self.assertTrue(result["complete"])
        self.assertEqual(result["transport"], {"kind": "current_process", "rmw": "rmw_fastrtps_cpp"})
        self.assertEqual(len(node.clients), 2)

    def test_parameter_rmw_helper_nonzero_exit_preserves_child_diagnostics(self):
        self.backend.profile["controller_parameter_rmw"] = "rmw_fastrtps_cpp"
        node = self.open()
        process = NS(returncode=2, stdout=json.dumps({"ok": False, "error": "service timeout",
                                                    "diagnostics": {"stage": "waiting_for_service"}}),
                     stderr="diagnostic stderr")
        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}), \
                patch.object(backend_module.subprocess, "run", return_value=process):
            with self.assertRaises(backend_module.ParameterReadError) as error:
                self.backend.read_controller_parameters()
        diagnostic = error.exception.diagnostics
        self.assertEqual(diagnostic["returncode"], 2)
        self.assertEqual(diagnostic["child_result"]["error"], "service timeout")
        self.assertEqual(diagnostic["stderr"], "diagnostic stderr")
        self.assertFalse(node.publishers)

    def test_parameter_probe_rejects_mismatched_transport_and_motion_methods(self):
        sys.path.insert(0, str(BACKEND_PATH.parents[1]))
        from f1_replay.parameter_probe import ReadOnlyParameterBackend, probe
        with patch.dict(os.environ, {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}):
            with self.assertRaisesRegex(ValueError, "匹配"):
                probe({"profile": {"controller_parameter_rmw": "rmw_fastrtps_cpp"}, "timeout_sec": 5})
        reader = ReadOnlyParameterBackend(self.profile)
        for name, args in (("enable_commands", ()), ("publish", ([0] * 19,)),
                           ("stop_base", ()), ("hold_arms", ())):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "禁止"):
                getattr(reader, name)(*args)
        self.assertFalse(self.ros.nodes)


if __name__ == "__main__":
    unittest.main()
