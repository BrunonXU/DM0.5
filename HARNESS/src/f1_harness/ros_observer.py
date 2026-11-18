"""Optional ROS2 subscriber, loaded only by the read-only robot-side gateway."""
from __future__ import annotations

import json
import time

from .contracts import Frame
from .observation import ObservationBuffer


class RosObserver:
    def __init__(self, profile, store):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from sensor_msgs.msg import CameraInfo, CompressedImage, Image, JointState
        from rclpy.qos import qos_profile_sensor_data
        self.rclpy, self.store = rclpy, store
        self.buffer = ObservationBuffer(source_clock_verified=profile.get("source_clock_verified", False),
            common_camera_window_ns=profile.get('common_camera_window_ns'))
        # Inspection-only devices have an independent source epoch and diagnostics.
        # Their absence or restart cannot invalidate calibrated RGB-D measurements.
        self.inspection_buffer = ObservationBuffer()
        self.profile = profile
        self.info = {}
        self.sequences = {}
        self.publishers = {}
        self.retired_publishers = {}
        self.context=Context()
        rclpy.init(args=None,context=self.context)
        self.node = rclpy.create_node("f1_harness_observer",context=self.context)
        self.executor=SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.closed=False
        self.measured_gripper = None
        self.measured_gripper_error = None
        self.subscriptions = []
        inspections = profile.get("inspection_cameras", [])
        for camera in [*profile["cameras"], *inspections]:
            inspection = camera in inspections
            for stream in (("rgb",) if inspection else ("rgb", "depth")):
                specification = camera[stream]
                message_type = CompressedImage if specification["type"] == "compressed" else Image
                self.subscriptions.append(self.node.create_subscription(message_type, specification["topic"],
                    self._image_callback(camera, stream, specification, inspection=inspection), qos_profile_sensor_data))
                self.subscriptions.append(self.node.create_subscription(CameraInfo, specification["camera_info_topic"],
                    self._info_callback(camera["name"], stream), qos_profile_sensor_data))
        joints = profile.get("joint_state")
        if joints:
            names = joints["names16"]
            if len(names) != 16 or len(joints["scale16"]) != 16:
                raise ValueError("explicit 16D joint mapping and scales required")
            def state_callback(message):
                lookup = dict(zip(message.name, message.position))
                if any(name is not None and name not in lookup for name in names):
                    self.buffer.faults.add("joint_mapping_missing")
                    return
                values = [lookup[name] * scale if name is not None else 0.
                          for name, scale in zip(names, joints["scale16"])]
                self.buffer.add_state(values, [name is not None for name in names], time.monotonic_ns())
            self.subscriptions.append(self.node.create_subscription(JointState, joints["topic"],
                                                                     state_callback, qos_profile_sensor_data))
        measured = profile.get('measured_gripper')
        if measured is not None:
            from .gripper_feedback import PassiveCanGripper
            try:
                if not isinstance(measured, dict):
                    raise ValueError('measured_gripper_configuration_must_be_object')
                if measured.get('enabled', True):
                    self.measured_gripper = PassiveCanGripper(
                        interface=measured['interface'], response_id=measured['response_id'],
                        side=measured.get('side', 'right'), window_samples=measured.get('window_samples', 64))
                    self.measured_gripper.start()
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                self.measured_gripper_error = f'{type(exc).__name__}:{exc}'
                if self.measured_gripper is not None:
                    self.measured_gripper.close()
                self.measured_gripper = None
            if self.measured_gripper_error:
                self.buffer._diagnostic('measured_gripper_unavailable', error=self.measured_gripper_error)

    def _info_callback(self, camera, stream):
        def callback(message):
            record = dict(k=list(message.k), d=list(message.d), r=list(message.r), p=list(message.p),
                          width=message.width, height=message.height, frame_id=message.header.frame_id,
                          distortion_model=message.distortion_model)
            self.info[(camera, stream)] = self.store.put_blob(json.dumps(record, sort_keys=True).encode())
        return callback

    def _image_callback(self, camera, stream, specification, *, inspection=False):
        buffer = self.inspection_buffer if inspection else self.buffer
        # rclpy chooses MessageOnly if a callback can bind ONE argument. An
        # optional second argument silently disables publisher identity.
        def callback(message, message_info):
            key = (camera["name"], stream)
            gid=(message_info.get('publisher_gid') if isinstance(message_info,dict)
                 else getattr(message_info,'publisher_gid',None))
            publisher=bytes(gid).hex() if gid is not None else None
            old_publisher=self.publishers.get(key)
            if publisher and old_publisher and publisher!=old_publisher:
                retired=self.retired_publishers.setdefault(key,set())
                if publisher in retired:
                    buffer.faults.add('multiple_source_publishers:'+camera['name'])
                    return
                retired.add(old_publisher)
                buffer.rebuild_source(reason='publisher_changed',evidence=dict(
                    camera=key[0],stream=key[1],previous_publisher=old_publisher,publisher=publisher))
            if publisher:self.publishers[key]=publisher
            self.sequences[key] = self.sequences.get(key, 0) + 1
            stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
            if specification["type"] == "compressed":
                width, height = specification["width"], specification["height"]
                encoding = message.format
            else:
                width, height, encoding = message.width, message.height, message.encoding
            if (width, height) != (specification["width"], specification["height"]):
                buffer.faults.add("resolution_changed:" + camera["name"])
                return
            frame = Frame(camera=camera["name"], stream=stream, frame_id=message.header.frame_id,
                sequence=self.sequences[key], source_ns=stamp, received_ns=time.monotonic_ns(),
                clock_domain=camera["source_clock_domain"], width=width, height=height, encoding=encoding,
                row_step_bytes=getattr(message, "step", None), is_bigendian=bool(getattr(message, "is_bigendian", False)),
                blob_sha256=self.store.put_blob(bytes(message.data)),
                depth_scale_m=specification.get("depth_scale_m"), camera_info_sha256=self.info.get(key),
                publisher_id=publisher)
            from .images import decode_cached as decode
            try:
                decode(frame, bytes(message.data))
            except ValueError:
                buffer.faults.add("invalid_image_contract:" + camera["name"])
                return
            buffer.add(frame)
        return callback

    def inspection_frame(self, *, target_ns=None):
        buffer = getattr(self, 'inspection_buffer', None)
        if buffer is None:
            return None
        return buffer.inspection_frame('left', target_ns=target_ns,
            max_skew_ns=self.buffer.common_camera_window_ns or self.buffer.max_skew_ns,
            max_age_ns=500_000_000)

    def observe(self):
        from .contracts import MeasuredGripperFeedback
        observation = self.buffer.snapshot()
        # Select the recorded left view nearest the same head exposure, even when
        # a later left frame arrived while the main RGB-D pair was being formed.
        head = next((f for f in observation.frames if f.camera == 'head' and f.stream == 'rgb'), None)
        if head is not None:
            frame = self.inspection_frame(target_ns=head.received_ns)
            if frame is not None:
                observation = observation.model_copy(update={'frames': observation.frames + (frame,)})
        reader = getattr(self, 'measured_gripper', None)
        if reader is not None:
            try:
                feedback = MeasuredGripperFeedback.model_validate(reader.snapshot())
                observation = observation.model_copy(update={'measured_gripper': feedback})
                self.measured_gripper_error = feedback.error
            except (OSError, ValueError, RuntimeError) as exc:
                self.measured_gripper_error = f'{type(exc).__name__}:{exc}'
        if getattr(self, 'measured_gripper_error', None):
            observation = observation.model_copy(update={'quality_warnings':
                observation.quality_warnings + ('measured_gripper_unavailable',)})
        return observation

    def spin(self):
        from rclpy.executors import ExternalShutdownException
        try:self.executor.spin()
        except ExternalShutdownException:
            # ROS signal handling can close the context before the service's
            # normal finally block. This is not a new camera acquisition fault.
            pass

    def close(self):
        if self.closed:return
        self.closed=True
        if getattr(self, 'measured_gripper', None) is not None:
            try:self.measured_gripper.close()
            except OSError as exc:self.measured_gripper_error=f'{type(exc).__name__}:{exc}'
        self.executor.shutdown(timeout_sec=2.)
        self.node.destroy_node()
        if self.context.ok():self.rclpy.shutdown(context=self.context)
