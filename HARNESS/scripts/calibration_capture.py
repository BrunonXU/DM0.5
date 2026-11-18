#!/usr/bin/env python3
"""Bounded, read-only native ROS capture for calibration; no HARNESS dependencies.

Run only after the control owner has stopped the robot. All received HAL samples
are retained; image throttling does not shorten the joint observation window.
This script records evidence, without deciding that the robot is stationary.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time


IMAGE_TOPICS = (
    "/camera/head/color/image_raw",
    "/camera/head/depth/image_raw",
    "/camera/right_wrist/color/image_raw",
    "/camera/right_wrist/depth/image_rect_raw",
)
INFO_TOPICS = tuple(topic.rsplit("/", 1)[0] + "/camera_info" for topic in IMAGE_TOPICS)
JOINT_TOPIC = "/hal/joint_states"
IMAGE_INTERVAL_NS = 500_000_000
MAX_IMAGES_PER_STREAM = 4


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--seconds", type=float, default=3.0, help="capture window, 2–10 seconds")
    args = parser.parse_args(argv)
    if not 2 <= args.seconds <= 10:
        parser.error("--seconds must be finite and between 2 and 10 inclusive")
    if args.output.exists() or args.output.is_symlink():
        parser.error("--output already exists; capture evidence is never overwritten")
    return args


class Capture:
    def __init__(self, output, start_ns, end_ns):
        self.output = output
        self.start_ns, self.end_ns = start_ns, end_ns
        self.records = []
        self.received_counts = {}
        self.image_counts = {}
        self.last_image_ns = {}
        self.infos = {}

    def receive(self, topic, kind, message, received_ns=None):
        received_ns = time.monotonic_ns() if received_ns is None else received_ns
        if not self.start_ns <= received_ns < self.end_ns:
            return
        sequence = self.received_counts.get(topic, 0) + 1
        self.received_counts[topic] = sequence
        if kind == "image":
            if self.image_counts.get(topic, 0) >= MAX_IMAGES_PER_STREAM:
                return
            if received_ns - self.last_image_ns.get(topic, -IMAGE_INTERVAL_NS) < IMAGE_INTERVAL_NS:
                return
        row = dict(topic=topic, kind=kind, sequence=sequence, received_ns=received_ns,
                   source_ns=message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec,
                   frame_id=message.header.frame_id)
        if kind == "image":
            data = bytes(message.data)
            sha = hashlib.sha256(data).hexdigest()
            path = self.output / "blobs" / sha
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(data)
            row.update(width=message.width, height=message.height, encoding=message.encoding,
                       row_step_bytes=message.step, is_bigendian=bool(message.is_bigendian),
                       blob_sha256=sha)
            self.image_counts[topic] = self.image_counts.get(topic, 0) + 1
            self.last_image_ns[topic] = received_ns
        elif kind == "camera_info":
            row.update(k=list(message.k), d=list(message.d), r=list(message.r), p=list(message.p),
                       width=message.width, height=message.height,
                       distortion_model=message.distortion_model)
            first = self.infos.get(topic, (row, row))[0]
            self.infos[topic] = first, row
            return
        elif kind == "joint_state":
            row.update(names=list(message.name), position=list(message.position),
                       velocity=list(message.velocity), effort=list(message.effort))
        else:
            raise ValueError("unsupported capture kind")
        self.records.append(row)

    def collected_records(self):
        result = list(self.records)
        for first, last in self.infos.values():
            result.append(first)
            if last["sequence"] != first["sequence"]:
                result.append(last)
        return sorted(result, key=lambda row: row["received_ns"])

    def joint_window_summary(self):
        rows = [row for row in self.records if row["kind"] == "joint_state"]
        values = {}
        for row in rows:
            for name, position in zip(row["names"], row["position"]):
                if math.isfinite(position):
                    item = values.setdefault(name, dict(count=0, minimum=position, maximum=position))
                    item["count"] += 1
                    item["minimum"] = min(item["minimum"], position)
                    item["maximum"] = max(item["maximum"], position)
        for item in values.values():
            item["range"] = item["maximum"] - item["minimum"]
        receipts = [row["received_ns"] for row in rows]
        return dict(sample_count=len(rows), first_received_ns=receipts[0] if receipts else None,
                    last_received_ns=receipts[-1] if receipts else None,
                    receipt_span_s=(receipts[-1] - receipts[0]) / 1e9 if receipts else 0.0,
                    leading_gap_s=(receipts[0] - self.start_ns) / 1e9 if receipts else None,
                    trailing_gap_s=(self.end_ns - receipts[-1]) / 1e9 if receipts else None,
                    max_receipt_gap_s=max((b - a for a, b in zip(receipts, receipts[1:])),
                                          default=0) / 1e9,
                    finite_position_ranges=values, arm_position_units="HAL degrees",
                    stationary=None,
                    note="Full-window received feedback only; no stationarity or hardware-clock verdict.")


def main(argv=None):
    args = parse_args(argv)
    # Parse/help/argument errors work on 6000D without importing or starting ROS.
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, Image, JointState
    except ImportError:
        raise SystemExit("Native ROS Python is required; source the existing robot ROS environment.") from None

    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "blobs").mkdir()
    rclpy.init(args=None)
    node = None
    try:
        node = rclpy.create_node("f1_harness_calibration_capture", enable_rosout=False,
                                 start_parameter_services=False)
        # Fixed native topics avoid capturing left-camera or derived/compressed images.
        # No command publishers, control clients, locks, or control-owner calls exist.
        capture = Capture(args.output, 0, 0)
        subscriptions = []
        specifications = ([(topic, "image", Image) for topic in IMAGE_TOPICS]
                          + [(topic, "camera_info", CameraInfo) for topic in INFO_TOPICS]
                          + [(JOINT_TOPIC, "joint_state", JointState)])
        for topic, kind, message_type in specifications:
            subscriptions.append(node.create_subscription(
                message_type, topic,
                lambda message, topic=topic, kind=kind: capture.receive(topic, kind, message),
                qos_profile_sensor_data))
        capture.start_ns = time.monotonic_ns()
        capture.end_ns = capture.start_ns + int(args.seconds * 1_000_000_000)
        started_unix_ns = time.time_ns()
        interrupted = False
        try:
            while rclpy.ok():
                remaining_s = (capture.end_ns - time.monotonic_ns()) / 1e9
                if remaining_s <= 0:
                    break
                rclpy.spin_once(node, timeout_sec=min(0.1, remaining_s))
        except (KeyboardInterrupt, ExternalShutdownException):
            interrupted = True
        finished_ns = time.monotonic_ns()
        complete_window = not interrupted and finished_ns >= capture.end_ns
        records = capture.collected_records()
        retained_counts = {}
        for row in records:
            retained_counts[row["topic"]] = retained_counts.get(row["topic"], 0) + 1
        missing = [topic for topic, _, _ in specifications if not retained_counts.get(topic)]
        topics = node.get_topic_names_and_types() if rclpy.ok() else []
        report = dict(schema="f1-native-ros-capture-v1", acquisition_kind="calibration",
                      topics=topics, subscribed_topics=[s[0] for s in specifications],
                      records=records, source_clock_verified=False, command_publishers=0,
                      started_unix_ns=started_unix_ns, window_start_received_ns=capture.start_ns,
                      window_end_received_ns=capture.end_ns, finished_received_ns=finished_ns,
                      requested_seconds=args.seconds, complete_window=complete_window,
                      interrupted=interrupted, received_counts=capture.received_counts,
                      retained_counts=retained_counts, missing_topics=missing,
                      image_interval_s=0.5, max_images_per_stream=MAX_IMAGES_PER_STREAM,
                      camera_info_retention="first_and_last", joint_retention="all_received_in_window",
                      joint_window=capture.joint_window_summary())
        with (args.output / "capture.json").open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(json.dumps(dict(output=str(args.output), counts=retained_counts, missing_topics=missing,
                              complete_window=complete_window, joint_window=report["joint_window"],
                              command_publishers=0)))
        return 0 if complete_window and not missing else 2
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
