"""F1 ROS transport; importing or opening it never publishes motion commands.

The caller owns data validation, control ownership checks, timing, recording,
operator confirmation and the stop sequence. There are no mode-changing
services or SDK calls here. ROS imports are delayed until explicitly requested.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import math
from numbers import Real
import os
import subprocess
import sys
import time
from typing import Any


ARM_NAMES = tuple(
    f"arm_{side}_j{joint}" for side in ("l", "r") for joint in range(1, 8)
)
COMMAND_TYPES = {
    "arms_command": "sensor_msgs/msg/JointState",
    "left_gripper_command": "control_msgs/msg/GripperCommand",
    "right_gripper_command": "control_msgs/msg/GripperCommand",
    "base_command": "geometry_msgs/msg/TwistStamped",
}
STATE_KEYS = (
    "joint_state", "left_gripper_state", "right_gripper_state", "base_state",
)
BASE_FLAGS = ("is_soft_auto", "is_hard_auto", "is_parking_mode", "is_avoid")
DEPENDENCIES = {
    "rclpy": (),
    "rclpy.context": ("Context",),
    "rclpy.executors": ("SingleThreadedExecutor",),
    "rclpy.qos": ("QoSProfile", "qos_profile_sensor_data"),
    "rclpy.signals": ("SignalHandlerOptions",),
    "sensor_msgs.msg": ("JointState",),
    "control_msgs.msg": ("GripperCommand",),
    "geometry_msgs.msg": ("TwistStamped",),
    "move.msg": ("State",),
    "rcl_interfaces.srv": ("GetParameters", "ListParameters"),
    "rcl_interfaces.msg": ("ParameterType",),
}


def dependency_check() -> dict:
    """Import/type-support check only; never initialize ROS or create a node."""
    result = {
        "ok": True, "python": sys.version.split()[0], "executable": sys.executable,
        "ros_distro": os.environ.get("ROS_DISTRO"), "modules": {}, "errors": [],
    }
    for module_name, names in DEPENDENCIES.items():
        try:
            module = importlib.import_module(module_name)
            for name in names:
                interface = getattr(module, name)
                if module_name.endswith((".msg", ".srv")) and name != "ParameterType":
                    checker = importlib.import_module("rclpy.type_support")
                    checker.check_for_type_support(interface)
            result["modules"][module_name] = {
                "file": getattr(module, "__file__", None), "symbols": list(names),
            }
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"{module_name}: {type(exc).__name__}: {exc}")
    return result


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} 必须是有限数值，收到 {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} 不是有限数值")
    return result


class PublishError(RuntimeError):
    """Some topics may already have been sent; inspect ``record`` for details."""

    def __init__(self, message: str, record: dict):
        super().__init__(message)
        self.record = deepcopy(record)
        self.sent_records = [
            item for item in self.record["commands"] if item["status"] == "sent"
        ]


class ParameterReadError(RuntimeError):
    """Preserve the exact read-only request/response failure for diagnostics."""

    def __init__(self, message: str, diagnostics: dict):
        self.diagnostics = deepcopy(diagnostics)
        super().__init__(
            f"{message} (requested_count={diagnostics['requested_count']}, "
            f"returned_count={diagnostics['returned_count']}, stage={diagnostics['stage']})"
        )


class RosBackend:
    """Single-threaded ROS adapter. ``poll`` services a bounded callback batch.

    Freshness timestamps are local ``time.monotonic_ns()`` receipt times, not
    hardware timestamps. Missing/invalid feedback remains absent. HAL arm and
    body messages may arrive separately, so joint freshness is per joint.
    """

    def __init__(self, profile: dict):
        self.profile = deepcopy(profile)
        self.node_name = f"f1_dataset_replay_{os.getpid()}"
        self.node_namespace = "/"
        self.last_publication: dict | None = None
        self._joints: dict[str, dict] = {}
        self._grippers: dict[str, dict] = {}
        self._base: dict = {}
        self._rclpy = None
        self._context = None
        self._executor = None
        self._node = None
        self._subscriptions: list = []
        self._publishers: dict = {}
        self._message_types: dict = {}
        self._get_parameters_type = None
        self._list_parameters_type = None
        self._parameter_type = None
        self._command_qos = None
        self._commands_enabled = False
        self._opened = False
        self._monitor_started_ns = None
        self._own_command_gids = {}
        self._external_commands = {}
        self._native_monitor = None

    def command_activity(self):
        """Latched external traffic since open; idle endpoints are not traffic."""
        if self._native_monitor is not None:
            self._native_monitor.poll()
        if self._monitor_started_ns is None:
            raise RuntimeError('命令活动监听尚未建立')
        return {'observed_s': (time.monotonic_ns()-self._monitor_started_ns)/1e9,
                'external_commands': deepcopy(self._external_commands),
                'own_gids_bound': len(self._own_command_gids) == 4}

    def _command_observer(self, key):
        def observe(message, info):
            # Jazzy provides MessageInfo as a dict; some test/ROS adapters use attributes.
            raw_gid = info.get('publisher_gid', b'') if isinstance(info, dict) else info.publisher_gid
            gid = bytes(raw_gid).hex()
            self._record_command(key, gid)
        return observe

    def _record_command(self, key, gid):
        if gid and gid == self._own_command_gids.get(key):
            return
        entry = self._external_commands.setdefault(key+':'+gid,
            {'topic': self.profile['topics'][key], 'publisher_gid': gid, 'count': 0})
        entry['count'] += 1
        entry['received_ns'] = time.monotonic_ns()

    @property
    def commands_enabled(self) -> bool:
        return self._commands_enabled

    def open(self) -> None:
        """Join the ROS graph and subscribe only; create no control publishers."""
        if self._opened or self._context is not None:
            raise RuntimeError("ROS 后端已经打开；需要先 close() 才能重开")
        topics = self.profile.get("topics", {})
        for key in (*COMMAND_TYPES, *STATE_KEYS):
            topic = topics.get(key)
            if not isinstance(topic, str) or not topic.startswith("/"):
                raise ValueError(f"profile.topics.{key} 必须是明确的绝对 Topic 名")
        if len(set(topics[key] for key in (*COMMAND_TYPES, *STATE_KEYS))) != 8:
            raise ValueError("命令与反馈 Topic 必须分别使用不同名称")
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import (
                DurabilityPolicy, HistoryPolicy, QoSProfile,
                ReliabilityPolicy, qos_profile_sensor_data,
            )
            from rclpy.signals import SignalHandlerOptions
            from sensor_msgs.msg import JointState
            from control_msgs.msg import GripperCommand
            from geometry_msgs.msg import TwistStamped
            from move.msg import State
            from rcl_interfaces.srv import GetParameters, ListParameters
            from rcl_interfaces.msg import ParameterType
        except ImportError as exc:
            raise RuntimeError(
                "缺少 F1 ROS 依赖：请在 F1 使用系统 Python 3，先 source ROS Jazzy "
                "及现有 move 消息工作区；需要 rclpy、sensor_msgs、control_msgs、"
                "geometry_msgs、move。不要在 6000D 安装机器人整套环境。"
            ) from exc
        self._rclpy = rclpy
        self._joints.clear()
        self._grippers.clear()
        self._base.clear()
        self._context = Context()
        try:
            # Keep the context alive after Ctrl+C so the caller can still stop.
            rclpy.init(args=[], context=self._context,
                       signal_handler_options=SignalHandlerOptions.NO)
            self._node = rclpy.create_node(
                self.node_name, namespace=self.node_namespace,
                context=self._context, use_global_arguments=False,
                enable_rosout=False, start_parameter_services=False,
            )
            self.node_name = self._node.get_name()
            self.node_namespace = self._node.get_namespace()
            self._executor = SingleThreadedExecutor(context=self._context)
            self._command_qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST, depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
            self._message_types = {
                "arms_command": JointState,
                "left_gripper_command": GripperCommand,
                "right_gripper_command": GripperCommand,
                "base_command": TwistStamped,
            }
            self._get_parameters_type = GetParameters
            self._list_parameters_type = ListParameters
            self._parameter_type = ParameterType
            subscriptions = (
                (JointState, "joint_state", self._on_joint_state),
                (JointState, "left_gripper_state", self._on_left_gripper),
                (JointState, "right_gripper_state", self._on_right_gripper),
                (State, "base_state", self._on_base_state),
            )
            for message_type, key, callback in subscriptions:
                self._subscriptions.append(self._node.create_subscription(
                    message_type, topics[key], callback, qos_profile_sensor_data,
                ))
            if self.profile.get('command_ownership_policy') == 'observed_idle_v1':
                self._external_commands.clear()
                self._own_command_gids.clear()
                from .command_monitor import CommandMonitor
                self._native_monitor = CommandMonitor(self.profile, self.node_name+'_monitor', self._record_command)
                deadline = time.monotonic()+5
                while self._native_monitor.started_ns is None and time.monotonic()<deadline:
                    self._native_monitor.poll()
                    time.sleep(.01)
                if self._native_monitor.started_ns is None:
                    raise RuntimeError('命令监听器未就绪')
                self._monitor_started_ns = self._native_monitor.started_ns
            self._opened = True
        except BaseException:
            # There cannot be motion publishers yet, including on partial open.
            self.close()
            raise

    def _require_open(self) -> None:
        if not self._opened or self._node is None:
            raise RuntimeError("ROS 后端尚未 open()")
        if not self._context.ok():
            raise RuntimeError("ROS context 已关闭，无法继续读取或下发")

    def poll(self, timeout_sec: float = 0.0) -> None:
        self._require_open()
        timeout = _finite(timeout_sec, "poll timeout_sec")
        if timeout < 0:
            raise ValueError("poll timeout_sec 不能为负数")
        self._rclpy.spin_once(
            self._node, executor=self._executor, timeout_sec=timeout,
        )
        # One ready callback may be base/gripper while a fresh HAL sample is
        # already queued. Service all feedback streams before reading snapshot.
        # Bound both count and work time so continuous feedback cannot starve dispatch.
        until = time.monotonic_ns()+3_000_000
        for _ in range(16):
            if time.monotonic_ns() >= until:
                break
            self._rclpy.spin_once(self._node, executor=self._executor, timeout_sec=0.0)
        if self._native_monitor is not None:
            self._native_monitor.poll()

    def snapshot(self) -> dict:
        """A detached snapshot; empty mappings mean no valid feedback yet."""
        return deepcopy({
            "joints": self._joints, "grippers": self._grippers, "base": self._base,
        })

    def _on_joint_state(self, msg: Any) -> None:
        received_ns = time.monotonic_ns()
        names = list(msg.name)
        positions = list(msg.position)
        counts = Counter(names)
        for index, name in enumerate(names):
            if not isinstance(name, str) or not name:
                continue
            if counts[name] != 1 or index >= len(positions):
                self._joints.pop(name, None)
                continue
            try:
                value = _finite(positions[index], f"HAL {name}")
            except (ValueError, OverflowError):
                self._joints.pop(name, None)
                continue
            self._joints[name] = {"position": value, "received_ns": received_ns}

    def _on_left_gripper(self, msg: Any) -> None:
        self._on_gripper("left", msg)

    def _on_right_gripper(self, msg: Any) -> None:
        self._on_gripper("right", msg)

    def _on_gripper(self, side: str, msg: Any) -> None:
        received_ns = time.monotonic_ns()
        expected_name = f"{side}_gripper_position"
        names = list(msg.name)
        positions = list(msg.position)
        if names.count(expected_name) != 1:
            self._grippers.pop(side, None)
            return
        index = names.index(expected_name)
        try:
            value = _finite(positions[index], f"{side} gripper")
        except (IndexError, ValueError, OverflowError):
            self._grippers.pop(side, None)
            return
        self._grippers[side] = {"position": value, "received_ns": received_ns}

    def _on_base_state(self, msg: Any) -> None:
        received_ns = time.monotonic_ns()
        try:
            state = {
                "vx": _finite(msg.speed.linear.x, "base vx"),
                "vy": _finite(msg.speed.linear.y, "base vy"),
                "wz": _finite(msg.speed.angular.z, "base wz"),
            }
            for key in BASE_FLAGS:
                value = getattr(msg, key)
                if type(value) is not bool:
                    raise ValueError(f"base {key} 必须是 bool")
                state[key] = value
        except (AttributeError, ValueError, OverflowError):
            self._base = {}
            return
        self._base = {"received_ns": received_ns, **state}

    def command_peers(self) -> dict:
        """Describe command endpoints without creating any command publisher.

        The graph includes our own publishers after ``enable_commands``. The
        caller can identify them using ``node_name`` and ``node_namespace``.
        Graph discovery is asynchronous; an endpoint is not execution ACK.
        """
        return self.topic_peers([self.profile["topics"][key] for key in COMMAND_TYPES])

    def topic_peers(self, topics) -> dict:
        """Read graph endpoints for recording/preflight; create no publishers."""
        self._require_open()

        def describe(endpoints: list) -> list[dict]:
            return [
                {"name": info.node_name, "namespace": info.node_namespace,
                 "type": info.topic_type}
                for info in endpoints
            ]

        result = {}
        for topic in topics:
            result[topic] = {
                "publishers": describe(self._node.get_publishers_info_by_topic(topic)),
                "subscribers": describe(self._node.get_subscriptions_info_by_topic(topic)),
            }
        return result

    def enable_commands(self) -> None:
        """Explicitly create the four publishers, without publishing a message."""
        self._require_open()
        if self._commands_enabled:
            return
        if self._publishers:
            raise RuntimeError("此前创建命令发布者未完成，请先停止并 close()，不要重试叠加发布者")
        frame = self.profile.get("base_frame")
        if not isinstance(frame, str) or not frame:
            raise ValueError("profile.base_frame 必须明确填写")
        effort = self.profile.get("gripper_max_effort", {})
        for side in ("left", "right"):
            _finite(effort.get(side), f"profile.gripper_max_effort.{side}")
        # Keep a successfully created base publisher on partial failure so the
        # caller's finally block can send zero; close() subsequently destroys it.
        for key in ("base_command", "arms_command", "left_gripper_command", "right_gripper_command"):
            self._publishers[key] = self._node.create_publisher(
                self._message_types[key], self.profile["topics"][key],
                self._command_qos,
            )
        self._commands_enabled = True
        if self._monitor_started_ns is not None:
            from .command_monitor import publisher_gid
            # Public RMW identity matches MessageInfo; graph GUID does not on Jazzy Cyclone.
            self._own_command_gids = {key:publisher_gid(pub) for key,pub in self._publishers.items()}

    def verify_base_parameters(self, timeout_sec=5.0):
        """Only read expected chassis parameters; never set controller parameters."""
        expected = self.profile.get("base_controller_expected_parameters")
        if expected is None:
            return None
        if not isinstance(expected, dict) or not expected:
            raise ValueError("底盘参数绑定不能为空")
        if any(not isinstance(k, str) or type(v) not in (float, int)
               or not math.isfinite(v) for k, v in expected.items()):
            raise ValueError("底盘参数绑定必须为有限数值")
        self._require_open()
        client = self._node.create_client(self._get_parameters_type, "/controller_node/get_parameters")
        future = None
        deadline = time.monotonic() + timeout_sec
        try:
            while not client.service_is_ready():
                if time.monotonic() >= deadline:
                    raise RuntimeError("底盘只读参数服务超时")
                self.poll(0.05)
            request = self._get_parameters_type.Request()
            request.names = list(expected)
            future = client.call_async(request)
            while not future.done():
                if time.monotonic() >= deadline:
                    raise RuntimeError("底盘只读参数响应超时")
                self.poll(0.05)
            response = future.result()
            if response is None or len(response.values) != len(expected):
                raise RuntimeError("底盘参数响应不完整")
            actual = {}
            for name, value in zip(expected, response.values):
                if value.type not in (2, 3):
                    raise RuntimeError("底盘参数缺失或类型变化：" + name)
                number = value.integer_value if value.type == 2 else value.double_value
                if not math.isfinite(number) or abs(number - expected[name]) > 1e-9:
                    raise RuntimeError(f"底盘参数变化：{name}={number}，预期{expected[name]}；重新规划，不改参数")
                actual[name] = number
            return {"service": "/controller_node/get_parameters", "parameters": actual,
                    "read_only": True}
        finally:
            if future is not None and not future.done():
                future.cancel()
            self._node.destroy_client(client)

    def read_controller_parameters(self, timeout_sec: float = 5.0) -> dict:
        """Read current ROS parameters, which are not SDK mode readback/ACK.

        Only ListParameters/GetParameters are called. No mode-changing service.
        The main program compares this result with its reviewed robot profile.
        """
        self._require_open()
        timeout = _finite(timeout_sec, "参数读取超时")
        if timeout <= 0:
            raise ValueError("参数读取超时必须大于 0")
        parameter_rmw = self.profile.get("controller_parameter_rmw")
        if parameter_rmw is not None and parameter_rmw != "rmw_fastrtps_cpp":
            raise ValueError("controller_parameter_rmw仅允许已验证的rmw_fastrtps_cpp")
        if parameter_rmw and os.environ.get("RMW_IMPLEMENTATION") != parameter_rmw:
            return self._read_parameters_isolated(timeout, parameter_rmw)
        service = "/motor_ctl/get_parameters"
        expected_names = ["arm_control_mode", "arm_impedance_type", "left_gripper_type", "right_gripper_type"] + [
            f"{name}_{bound}" for name in ARM_NAMES for bound in ("min", "max")
        ]
        if self.profile.get("require_body_control") is True:
            expected_names += ["enable_udp_motors", "udp_motor_host", "udp_motor_send_port"]
        clients = []
        deadline = time.monotonic() + timeout
        future = None
        diagnostic = {
            "service": service, "expected_names": expected_names, "expected_count": len(expected_names),
            "requested_names": [], "requested_count": 0,
            "returned_count": None, "stage": "creating_client", "parameters": {},
            "returned_types": [], "evidence": "current_ros_parameters_not_sdk_mode_readback",
            "transport": {"kind": "current_process", "rmw": os.environ.get("RMW_IMPLEMENTATION")},
        }

        def call_read_only(service_type, service_name, request, prefix=""):
            nonlocal future
            diagnostic["stage"] = prefix + "creating_client"
            client = self._node.create_client(service_type, service_name)
            clients.append(client)
            diagnostic["stage"] = prefix + "waiting_for_service"
            while not client.service_is_ready():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"等待只读参数服务 {service_name} 超时")
                self.poll(min(0.05, remaining))
            diagnostic["stage"] = prefix + "calling_service"
            future = client.call_async(request)
            diagnostic["stage"] = prefix + "waiting_for_response"
            while not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"读取 {service_name} 超时；未更改任何控制参数")
                self.poll(min(0.05, remaining))
            diagnostic["stage"] = prefix + "reading_response"
            response = future.result()
            if response is None:
                raise RuntimeError(f"{service_name} 返回空 response（None）")
            return response

        try:
            list_service = "/motor_ctl/list_parameters"
            diagnostic["discovery_service"] = list_service
            list_request = self._list_parameters_type.Request()
            list_request.prefixes = []
            list_request.depth = 0  # ROS DEPTH_RECURSIVE, all declared names.
            declared = call_read_only(self._list_parameters_type, list_service,
                                      list_request, prefix="discovery_").result.names
            if any(not isinstance(name, str) for name in declared) or len(set(declared)) != len(declared):
                raise RuntimeError("ListParameters 返回无效或重复的参数名")
            diagnostic["declared_names"] = list(declared)
            diagnostic["declared_count"] = len(declared)
            names = [name for name in expected_names if name in declared]
            diagnostic["missing_names"] = [name for name in expected_names if name not in declared]
            diagnostic["requested_names"] = names
            diagnostic["requested_count"] = len(names)
            if not names:
                return {**diagnostic, "returned_count": 0, "stage": "complete_with_missing_parameters",
                        "complete": False, "received_ns": time.monotonic_ns()}
            request = self._get_parameters_type.Request()
            request.names = names
            response = call_read_only(self._get_parameters_type, service, request)
            diagnostic["returned_count"] = len(response.values)
            diagnostic["returned_types"] = [getattr(value, "type", None) for value in response.values]
            if len(response.values) != len(names):
                raise RuntimeError(f"{service} 返回参数数量异常")
            parameters = {}
            diagnostic["parameters"] = parameters
            diagnostic["stage"] = "decoding_parameters"
            for name, value in zip(names, response.values):
                if name == "enable_udp_motors":
                    if value.type != self._parameter_type.PARAMETER_BOOL:
                        raise RuntimeError(f"控制器参数 {name} 缺失或类型错误：{value.type}")
                    number = bool(value.bool_value)
                elif name in ("left_gripper_type", "right_gripper_type", "udp_motor_host"):
                    if value.type != self._parameter_type.PARAMETER_STRING:
                        raise RuntimeError(f"控制器参数 {name} 缺失或类型错误：{value.type}")
                    number = str(value.string_value)
                elif value.type == self._parameter_type.PARAMETER_INTEGER:
                    number = int(value.integer_value)
                elif (value.type == self._parameter_type.PARAMETER_DOUBLE
                      and name not in ("arm_control_mode", "arm_impedance_type", "udp_motor_send_port")):
                    number = _finite(value.double_value, f"控制器参数 {name}")
                else:
                    raise RuntimeError(f"控制器参数 {name} 缺失或类型错误：{value.type}")
                parameters[name] = number
            diagnostic["ros_missing_names"] = list(diagnostic["missing_names"])
            limit_names = {f"{name}_{bound}" for name in ARM_NAMES for bound in ("min", "max")}
            missing_limits = limit_names.intersection(diagnostic["missing_names"])
            if self.profile.get("joint_limit_source") == "verified_controller_binary":
                from .controller_evidence import read_verified_binary_limits
                diagnostic["stage"] = "checking_current_controller_binary"
                binary = read_verified_binary_limits(self.profile)
                for name in missing_limits:
                    parameters[name] = binary["parameters"][name]
                parameters["_controller_binary_sha256"] = binary["sha256"]
                diagnostic["binary_evidence"] = binary
                diagnostic["missing_names"] = [name for name in diagnostic["missing_names"]
                                               if name not in parameters]
            return {
                **diagnostic, "stage": ("complete_with_missing_parameters" if diagnostic["missing_names"] else "complete"),
                "complete": not diagnostic["missing_names"], "received_ns": time.monotonic_ns(),
            }
        except Exception as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
            raise ParameterReadError(diagnostic["error"], diagnostic) from exc
        finally:
            if future is not None and not future.done():
                future.cancel()
            for client in reversed(clients):
                self._node.destroy_client(client)

    def _read_parameters_isolated(self, timeout: float, parameter_rmw: str) -> dict:
        """Use an isolated read-only RMW without changing this control context."""
        environment = dict(os.environ, RMW_IMPLEMENTATION=parameter_rmw,
                           PYTHONDONTWRITEBYTECODE="1")
        diagnostic = {
            "stage": "isolated_parameter_process", "parameters": {},
            "requested_count": None, "returned_count": None,
            "transport": {"kind": "isolated_read_only_parameter_process",
                          "parent_rmw": os.environ.get("RMW_IMPLEMENTATION"),
                          "parameter_rmw": parameter_rmw, "executable": sys.executable},
            "control_publishers_created": False,
        }
        try:
            # Keep this process's feedback subscriptions serviced while waiting;
            # subprocess.run kills and reaps its own helper on timeout.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    subprocess.run, [sys.executable, "-m", "f1_replay.parameter_probe"],
                    input=json.dumps({"profile": self.profile, "timeout_sec": timeout}),
                    text=True, capture_output=True, env=environment, timeout=timeout + 3.0,
                    check=False,
                )
                while not future.done():
                    self.poll(0.05)
                process = future.result()
            diagnostic["returncode"] = process.returncode
            diagnostic["stderr"] = process.stderr[-8192:]
            try:
                payload = json.loads(process.stdout)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError("只读参数子进程未返回有效JSON") from exc
            if process.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
                diagnostic["child_result"] = payload
                raise RuntimeError(f"只读参数子进程失败，exit={process.returncode}")
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("parameters"), dict):
                raise RuntimeError("只读参数子进程缺少参数结果")
            diagnostic["transport"]["child_transport"] = result.get("transport")
            return {**result, "transport": diagnostic["transport"],
                    "helper_stderr": diagnostic["stderr"]}
        except Exception as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
            raise ParameterReadError(diagnostic["error"], diagnostic) from exc

    def _stamp(self) -> Any:
        return self._node.get_clock().now().to_msg()

    @staticmethod
    def _stamp_fields(stamp: Any) -> dict:
        return {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)}

    def _arms_message(self, positions: list[float], stamp: Any) -> tuple:
        msg = self._message_types["arms_command"]()
        msg.header.stamp = stamp
        msg.header.frame_id = self.profile["base_frame"]
        msg.name = list(ARM_NAMES)
        msg.position = positions
        msg.velocity = []
        msg.effort = []
        fields = {
            "header": {"stamp": self._stamp_fields(stamp),
                       "frame_id": msg.header.frame_id},
            "name": list(msg.name), "position": list(msg.position),
            "velocity": [], "effort": [],
        }
        return "arms_command", msg, fields, stamp

    def _gripper_message(self, side: str, position: float, stamp: Any) -> tuple:
        key = f"{side}_gripper_command"
        msg = self._message_types[key]()
        msg.position = position
        msg.max_effort = float(self.profile["gripper_max_effort"][side])
        return key, msg, {"position": msg.position, "max_effort": msg.max_effort}, stamp

    def _base_message(self, velocity: list[float], stamp: Any) -> tuple:
        msg = self._message_types["base_command"]()
        msg.header.stamp = stamp
        msg.header.frame_id = self.profile["base_frame"]
        msg.twist.linear.x, msg.twist.linear.y, msg.twist.angular.z = velocity
        msg.twist.linear.z = 0.0
        msg.twist.angular.x = 0.0
        msg.twist.angular.y = 0.0
        fields = {
            "header": {"stamp": self._stamp_fields(stamp),
                       "frame_id": msg.header.frame_id},
            "twist": {
                "linear": {"x": msg.twist.linear.x, "y": msg.twist.linear.y, "z": 0.0},
                "angular": {"x": 0.0, "y": 0.0, "z": msg.twist.angular.z},
            },
        }
        return "base_command", msg, fields, stamp

    def _publish_batch(self, kind: str, messages: list[tuple]) -> dict:
        record = {"kind": kind, "started_ns": time.monotonic_ns(), "commands": []}
        self.last_publication = record
        for key, msg, fields, stamp in messages:
            command = {
                "topic": self.profile["topics"][key], "type": COMMAND_TYPES[key],
                "fields": fields, "ros_stamp": self._stamp_fields(stamp),
                # GripperCommand has no header; ros_stamp is log context only.
                "stamp_in_message": key in ("arms_command", "base_command"),
                "sent_ns": time.monotonic_ns(), "status": "sending",
            }
            record["commands"].append(command)
            try:
                self._publishers[key].publish(msg)
            except Exception as exc:
                command["status"] = "error_delivery_unknown"
                command["error"] = str(exc)
                command["returned_ns"] = time.monotonic_ns()
                raise PublishError(
                    f"发布 {command['topic']} 失败；其他 Topic 可能已发送，请执行停止流程",
                    record,
                ) from exc
            command["returned_ns"] = time.monotonic_ns()
            command["status"] = "sent"
        record["finished_ns"] = time.monotonic_ns()
        return deepcopy(record)

    def publish(self, action19: Any) -> dict:
        """Publish L7 + R7 + two grippers + vx/vy/wz with no remapping of values.

        Arm values remain degrees; grippers remain 0-open/100-closed; base is
        m/s, m/s, rad/s. No clipping, interpolation, deduplication or Placo map.
        Four publications are sequential and are not an atomic robot action.
        """
        self._require_open()
        if not self._commands_enabled:
            raise RuntimeError("尚未 enable_commands()；拒绝发布回放动作")
        values = list(action19)
        if len(values) != 19:
            raise ValueError(f"回放动作必须是 19 维，实际 {len(values)} 维")
        values = [_finite(value, f"action[{i}]") for i, value in enumerate(values)]
        stamp = self._stamp()
        # Construct and validate all messages before sending the first one.
        messages = [
            self._arms_message(values[:14], stamp),
            self._gripper_message("left", values[14], stamp),
            self._gripper_message("right", values[15], stamp),
            self._base_message(values[16:19], stamp),
        ]
        return self._publish_batch("action", messages)

    def stop_base(self) -> dict | None:
        """Send zero velocity only when explicitly enabled; never create a pub."""
        if "base_command" not in self._publishers:
            return None
        self._require_open()
        return self._publish_batch(
            "stop_base", [self._base_message([0.0, 0.0, 0.0], self._stamp())],
        )

    def hold_arms(self) -> dict | None:
        """Use all 14 latest measured angles; caller must check freshness first."""
        if not self._commands_enabled:
            return None
        self._require_open()
        try:
            positions = [_finite(self._joints[name]["position"], name) for name in ARM_NAMES]
        except (KeyError, ValueError, OverflowError) as exc:
            raise RuntimeError("双臂 14 维反馈不完整或无效，拒绝发送保持目标") from exc
        return self._publish_batch(
            "hold_arms", [self._arms_message(positions, self._stamp())],
        )

    def close(self) -> None:
        """Release ROS resources only. Caller must stop before closing."""
        if self._native_monitor is not None:
            self._native_monitor.close()
            self._native_monitor = None
        self._opened = False
        self._commands_enabled = False
        try:
            if self._executor is not None:
                self._executor.shutdown(timeout_sec=0.0)
        finally:
            self._executor = None
            try:
                if self._node is not None:
                    self._node.destroy_node()
            finally:
                self._node = None
                self._subscriptions.clear()
                self._publishers.clear()
                try:
                    if self._context is not None:
                        self._context.try_shutdown()
                finally:
                    self._context = None
