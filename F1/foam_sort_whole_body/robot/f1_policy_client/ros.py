"""Camera subscriptions layered on the existing F1 replay ROS transport.

Opening subscribes only. All control messages, ownership monitoring, controller
parameter reads and stopping remain implemented by f1_replay.RosBackend.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
import os
import time

from f1_replay.ros_backend import RosBackend, dependency_check as replay_dependency_check

from .observation import CAMERAS


DEFAULT_CAMERA_TOPICS = {
    "head": "/camera/head/color/image_raw/compressed",
    "left": "/camera/left_wrist/color/image_raw/compressed",
    "right": "/camera/right_wrist/color/image_raw/compressed",
}


def dependency_check():
    """Import/type-support only; creates no ROS context or publishers."""
    report = replay_dependency_check()
    try:
        message = importlib.import_module("sensor_msgs.msg").CompressedImage
        importlib.import_module("rclpy.type_support").check_for_type_support(message)
        report["modules"]["sensor_msgs.msg"]["symbols"].append("CompressedImage")
    except Exception as exc:
        report["ok"] = False
        report["errors"].append(f"sensor_msgs.msg.CompressedImage: {type(exc).__name__}: {exc}")
    return report


class RosObservationBackend(RosBackend):
    def __init__(self, profile, camera_topics=None):
        super().__init__(profile)
        self.node_name = f"f1_foam_inference_{os.getpid()}"
        self.camera_topics = deepcopy(DEFAULT_CAMERA_TOPICS if camera_topics is None else camera_topics)
        if (not isinstance(self.camera_topics, dict) or set(self.camera_topics) != set(CAMERAS)
                or any(not isinstance(topic, str) or not topic.startswith("/")
                       for topic in self.camera_topics.values())
                or len(set(self.camera_topics.values())) != len(CAMERAS)
                or set(self.camera_topics.values()).intersection(profile.get("topics", {}).values())):
            raise ValueError("Three distinct absolute compressed camera topics are required")
        self._cameras = {}

    def open(self):
        super().open()
        self._cameras.clear()
        try:
            from sensor_msgs.msg import CompressedImage
            from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

            # Keep only the latest queued frame; byte decoding stays on the server.
            # Jazzy QoS includes native duration handles which cannot be deep-copied.
            qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1,
                             reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE)
            for name in CAMERAS:
                self._subscriptions.append(self._node.create_subscription(
                    CompressedImage, self.camera_topics[name], self._camera_callback(name), qos,
                ))
        except BaseException:
            self.close()
            raise

    def _camera_callback(self, name):
        def receive(message):
            received_ns = time.monotonic_ns()
            try:
                data = bytes(message.data)
                if not data:
                    raise ValueError("empty compressed image")
                stamp = message.header.stamp
                header_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
                image_format = str(message.format)
            except (AttributeError, TypeError, ValueError, OverflowError):
                self._cameras.pop(name, None)
                return
            self._cameras[name] = {"data": data, "received_ns": received_ns,
                                   "header_stamp_ns": header_ns, "format": image_format}
        return receive

    def snapshot(self):
        result = super().snapshot()
        result["cameras"] = deepcopy(self._cameras)
        return result

    def close(self):
        try:
            super().close()
        finally:
            self._cameras.clear()
