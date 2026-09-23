#!/usr/bin/env python3
"""Direct ROS-to-LeRobot-v3 recorder controlled by PICO button gestures."""

from __future__ import annotations

import importlib.util
import json
import os
from collections import deque
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import numpy as np
import rclpy
from rclpy._rclpy_pybind11 import RCLError
from control_msgs.msg import GripperCommand
from geometry_msgs.msg import TwistStamped
from move.msg import State as MoveState
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import String

from pico_f1.config import load_control_config, project_root
from pico_f1.camera_sync import CameraFrameStamp, CameraSyncGate, align_frame_queues
from pico_f1.alignment_archive import archive_frame, read_extras, validate_coverage
from pico_f1.sample_history import SampleHistory
from pico_f1.lerobot_v3 import (
    CAMERA_KEYS,
    FEATURE_NAMES,
    JOINT_MOTOR_NAMES,
    LeRobotV3Dataset,
    jpeg_dimensions,
)


CAMERA_TOPICS = {
    "observation.images.base_0_rgb": "/camera/head/color/image_raw/compressed",
    "observation.images.left_wrist_0_rgb": "/camera/left_wrist/color/image_raw/compressed",
    "observation.images.right_wrist_0_rgb": "/camera/right_wrist/color/image_raw/compressed",
}


def _message_stamp_ns(message, fallback: int) -> int:
    stamp = message.header.stamp
    value = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
    return value if value > 0 else int(fallback)


class EpisodeCapture:
    def __init__(self, root: Path, config: dict, camera_info: dict) -> None:
        staging_root = (root / ".staging").resolve()
        staging_root.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="episode-", dir=staging_root)).resolve()
        self.fps = float(config["fps"])
        self.max_camera_skew_ns = int(float(config["camera_max_skew_s"]) * 1e9)
        self.max_frame_gap_ns = int(float(config["camera_max_frame_gap_s"]) * 1e9)
        self.max_state_age_ns = int(float(config["state_max_age_s"]) * 1e9)
        self.max_rate_error_ratio = float(config["max_rate_error_ratio"])
        self.camera_info = {key: dict(camera_info[key]) for key in CAMERA_KEYS}
        self.processes = {
            key: self._open_encoder(key, config) for key in CAMERA_KEYS
        }
        self.state: list[np.ndarray] = []
        self.action: list[np.ndarray] = []
        self.timestamp_ns: list[int] = []
        self.camera_timestamp_ns: list[np.ndarray] = []
        self.capture_monotonic_ns: list[int] = []
        self.camera_integrity_timestamp_ns: list[np.ndarray] = []
        self.camera_arrival_timestamp_ns: list[np.ndarray] = []
        self.state_max_age_ns: list[int] = []
        self.action_max_age_ns: list[int] = []
        self.dropped = 0
        self.started_monotonic = time.monotonic()
        self.alignment_extra_count = 0
        self.source_gaps = []
        self._samples_stream = (self.path / "samples.jsonl").open("a", encoding="utf-8")
        (self.path / "capture.json").write_text(json.dumps({"fps": self.fps, "cameras": self.camera_info}))
        self._submitted_frames = 0
        self._writer_error: Exception | None = None
        self._writer_aborted = threading.Event()
        self._frame_queue: queue.Queue[tuple | None] = queue.Queue(
            maxsize=int(config["encoder_queue_frames"])
        )
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="LeRobotVideoWriter",
        )
        self._writer_thread.start()

    def _open_encoder(self, key: str, config: dict) -> subprocess.Popen:
        output = self.path / f"{key}.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-framerate", f"{self.fps:g}",
            "-vcodec", "mjpeg", "-i", "pipe:0", "-an",
            "-c:v", str(config["video_codec"]),
            "-preset", str(config["video_preset"]),
            "-crf", str(int(config["video_crf"])),
            "-pix_fmt", "yuv420p", "-threads", "2", "-g", str(int(self.fps)), "-r", f"{self.fps:g}",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(output),
        ]
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def add(
        self,
        state: np.ndarray,
        action: np.ndarray,
        timestamp_ns: int,
        cameras: dict[str, tuple[bytes, int]],
        timing: dict[str, object],
    ) -> None:
        if self._writer_error is not None:
            raise RuntimeError(f"video writer failed: {self._writer_error}")
        item = (
            np.asarray(state, dtype=np.float32).copy(),
            np.asarray(action, dtype=np.float32).copy(),
            int(timestamp_ns),
            {key: (bytes(cameras[key][0]), int(cameras[key][1])) for key in CAMERA_KEYS},
            {
                "capture_monotonic_ns": int(timing["capture_monotonic_ns"]),
                "camera_integrity_timestamp_ns": np.asarray(
                    timing["camera_integrity_timestamp_ns"], dtype=np.int64
                ).copy(),
                "camera_arrival_timestamp_ns": np.asarray(
                    timing["camera_arrival_timestamp_ns"], dtype=np.int64
                ).copy(),
                "state_max_age_ns": int(timing["state_max_age_ns"]),
                "action_max_age_ns": int(timing["action_max_age_ns"]),
            },
        )
        try:
            self._frame_queue.put_nowait(item)
        except queue.Full as exc:
            raise RuntimeError("video encoder queue overflow") from exc
        self._submitted_frames += 1

    @property
    def frame_count(self) -> int:
        return self._submitted_frames

    def _writer_loop(self) -> None:
        try:
            while not self._writer_aborted.is_set():
                item = self._frame_queue.get()
                try:
                    if item is None:
                        return
                    state, action, timestamp_ns, cameras, timing = item
                    for key in CAMERA_KEYS:
                        process = self.processes[key]
                        if process.stdin is None or process.poll() is not None:
                            raise RuntimeError(
                                f"video encoder stopped unexpectedly: {key}"
                            )
                        process.stdin.write(cameras[key][0])
                    self.state.append(state)
                    self.action.append(action)
                    self.timestamp_ns.append(timestamp_ns)
                    self.camera_timestamp_ns.append(
                        np.asarray(
                            [cameras[key][1] for key in CAMERA_KEYS], dtype=np.int64
                        )
                    )
                    self.capture_monotonic_ns.append(
                        int(timing["capture_monotonic_ns"])
                    )
                    self.camera_integrity_timestamp_ns.append(
                        timing["camera_integrity_timestamp_ns"]
                    )
                    self.camera_arrival_timestamp_ns.append(
                        timing["camera_arrival_timestamp_ns"]
                    )
                    self.state_max_age_ns.append(int(timing["state_max_age_ns"]))
                    self.action_max_age_ns.append(int(timing["action_max_age_ns"]))
                    self._samples_stream.write(json.dumps({
                        "state": state.tolist(), "action": action.tolist(),
                        "timestamp_ns": timestamp_ns,
                        "camera_timestamp_ns": [cameras[key][1] for key in CAMERA_KEYS],
                        "timing": {key: value.tolist() if isinstance(value, np.ndarray) else value
                                   for key, value in timing.items()},
                    }) + "\n")
                    self._samples_stream.flush()
                finally:
                    self._frame_queue.task_done()
        except Exception as exc:
            self._writer_error = exc

    def _finish_writer(self) -> None:
        if self._writer_error is not None:
            raise RuntimeError(f"video writer failed: {self._writer_error}")
        try:
            self._frame_queue.put(None, timeout=5.0)
        except queue.Full as exc:
            raise RuntimeError("video encoder queue did not drain") from exc
        self._writer_thread.join(timeout=30.0)
        if self._writer_thread.is_alive():
            raise RuntimeError("video writer did not stop within 30 seconds")
        if self._writer_error is not None:
            raise RuntimeError(f"video writer failed: {self._writer_error}")

    def _close_media(self) -> None:
        self._finish_writer()
        self._samples_stream.close()
        errors: list[str] = []
        for process in self.processes.values():
            if process.stdin is not None:
                process.stdin.close()
        for key, process in self.processes.items():
            try:
                return_code = process.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return_code = -9
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            if process.stderr is not None:
                process.stderr.close()
            if return_code != 0:
                errors.append(f"{key}: ffmpeg rc={return_code}: {stderr[-500:]}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def stop(self) -> tuple[dict[str, np.ndarray], dict[str, dict], dict[str, float]]:
        self._close_media()
        arrays = {
            "state": np.asarray(self.state, dtype=np.float32),
            "action": np.asarray(self.action, dtype=np.float32),
            "timestamp_ns": np.asarray(self.timestamp_ns, dtype=np.int64),
            "camera_timestamp_ns": np.asarray(self.camera_timestamp_ns, dtype=np.int64),
            "capture_monotonic_ns": np.asarray(self.capture_monotonic_ns, dtype=np.int64),
            "camera_integrity_timestamp_ns": np.asarray(
                self.camera_integrity_timestamp_ns, dtype=np.int64
            ),
            "camera_arrival_timestamp_ns": np.asarray(
                self.camera_arrival_timestamp_ns, dtype=np.int64
            ),
            "state_max_age_ns": np.asarray(self.state_max_age_ns, dtype=np.int64),
            "action_max_age_ns": np.asarray(self.action_max_age_ns, dtype=np.int64),
        }
        if len(arrays["timestamp_ns"]) < 2:
            raise RuntimeError("episode has fewer than two synchronized frames")
        np.savez_compressed(self.path / "episode_arrays.npz", **arrays)
        video_info = {
            key: self._probe_video(self.path / f"{key}.mp4") for key in CAMERA_KEYS
        }
        expected = len(arrays["timestamp_ns"])
        for key, info in video_info.items():
            if int(info["frames"]) != expected:
                raise RuntimeError(
                    f"{key} video/data mismatch: {info['frames']} != {expected}"
                )
            configured = self.camera_info[key]
            if (int(info["width"]), int(info["height"])) != (
                int(configured["width"]), int(configured["height"])
            ):
                raise RuntimeError(f"{key} video resolution changed")
            if abs(float(info["fps"]) - self.fps) > 1e-6:
                raise RuntimeError(
                    f"{key} video fps mismatch: {info['fps']} != {self.fps}"
                )
        timing_info = self._validate_timing(arrays)
        return arrays, video_info, timing_info

    def _validate_timing(self, arrays: dict[str, np.ndarray]) -> dict[str, float]:
        capture = arrays["capture_monotonic_ns"]
        integrity = arrays["camera_integrity_timestamp_ns"]
        arrivals = arrays["camera_arrival_timestamp_ns"]
        sources = arrays["camera_timestamp_ns"]
        if arrivals.shape != (len(capture), len(CAMERA_KEYS)):
            raise RuntimeError("camera arrival timestamp shape mismatch")
        if sources.shape != arrivals.shape:
            raise RuntimeError("camera source timestamp shape mismatch")
        if integrity.shape != arrivals.shape:
            raise RuntimeError("camera integrity timestamp shape mismatch")
        if not np.array_equal(integrity, sources):
            raise RuntimeError("camera source timestamps must not be repaired")
        capture_delta = np.diff(capture)
        if np.any(capture_delta <= 0):
            raise RuntimeError("capture timestamps repeated or reversed")
        camera_delta = np.diff(integrity, axis=0)
        arrival_delta = np.diff(arrivals, axis=0)
        if np.any(camera_delta <= 0):
            raise RuntimeError("camera integrity timestamp repeated or reversed")
        if np.any(arrival_delta <= 0):
            raise RuntimeError("camera arrival timestamp repeated or reversed")
        extras = read_extras(self.path / "alignment")
        training_ready = True
        coverage_fault = ""
        try:
            validate_coverage(sources, extras, self.max_frame_gap_ns)
        except ValueError as exc:
            if "frame gap" not in str(exc):
                raise
            # Preserve the WHOLE observed task, but never present missing
            # physical frames as an intact training episode.
            training_ready = False
            coverage_fault = str(exc)
        skew = np.ptp(sources, axis=1)
        if int(np.max(skew)) > self.max_camera_skew_ns:
            raise RuntimeError(
                f"camera source skew exceeds {self.max_camera_skew_ns / 1e6:.3f}ms"
            )
        if int(np.max(arrays["state_max_age_ns"])) > self.max_state_age_ns:
            raise RuntimeError("state age exceeded strict synchronization limit")
        if int(np.max(arrays["action_max_age_ns"])) > self.max_state_age_ns:
            raise RuntimeError("action age exceeded strict synchronization limit")
        duration_s = (int(capture[-1]) - int(capture[0])) / 1e9
        capture_fps = (len(capture) - 1) / duration_s
        rate_error = abs(capture_fps - self.fps) / self.fps
        if len(capture) >= 10 and rate_error > self.max_rate_error_ratio:
            raise RuntimeError(
                f"capture rate {capture_fps:.3f}Hz outside "
                f"{self.max_rate_error_ratio * 100:.1f}% tolerance of {self.fps:g}Hz"
            )
        return {
            "capture_fps": float(capture_fps),
            "max_camera_skew_ms": float(np.max(skew)) / 1e6,
            "max_camera_integrity_gap_ms": float(np.max(camera_delta)) / 1e6,
            "max_camera_arrival_gap_ms": float(np.max(arrival_delta)) / 1e6,
            "max_state_age_ms": float(np.max(arrays["state_max_age_ns"])) / 1e6,
            "max_action_age_ms": float(np.max(arrays["action_max_age_ns"])) / 1e6,
            "alignment_extra_frames": len(extras),
            "training_ready": training_ready,
            "coverage_fault": coverage_fault,
        }

    def add_alignment_extra(self, key, frame, reason="clock_alignment"):
        archive_frame(self.path / "alignment", key, frame, reason)
        self.alignment_extra_count += 1

    def note_source_gaps(self, gaps):
        if not gaps:
            return
        with (self.path / "quality_gaps.jsonl").open("a", encoding="utf-8") as stream:
            for gap in gaps:
                stream.write(json.dumps(gap) + "\n")
                self.source_gaps.append(gap)

    def preserve_failure(self, reason):
        try:
            if self._writer_thread.is_alive():
                # Recovery is explicitly not training-ready. Flush encoders,
                # but do not decode an entire long episode just to quarantine it.
                self._close_media()
        except Exception:
            self.abort(preserve=True)
        finally:
            if any(process.poll() is None for process in self.processes.values()):
                self.abort(preserve=True)
            if not self._samples_stream.closed:
                self._samples_stream.close()
        (self.path / "recovery.json").write_text(json.dumps({
            "reason": str(reason), "submitted_frames": self.frame_count,
            "completed_rows": len(self.state), "training_ready": False,
        }, indent=2))

    def abort(self, preserve=False) -> None:
        self._writer_aborted.set()
        for process in self.processes.values():
            if process.poll() is None:
                process.kill()
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stderr is not None:
                process.stderr.close()
        try:
            self._frame_queue.put_nowait(None)
        except queue.Full:
            pass
        self._writer_thread.join(timeout=5.0)
        self._samples_stream.close()
        if not preserve:
            shutil.rmtree(self.path, ignore_errors=True)

    @staticmethod
    def _probe_video(path: Path) -> dict:
        command = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries",
            "stream=width,height,codec_name,pix_fmt,r_frame_rate,nb_read_frames",
            "-of", "json", str(path),
        ]
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=30.0
        )
        stream = json.loads(completed.stdout)["streams"][0]
        numerator, denominator = stream["r_frame_rate"].split("/", 1)
        return {
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "codec": stream["codec_name"],
            "pix_fmt": stream["pix_fmt"],
            "fps": float(numerator) / float(denominator),
            "frames": int(stream["nb_read_frames"]),
            "bytes": path.stat().st_size,
        }


class LeRobotRecorderNode(Node):
    def __init__(self, enable_commands=True, publish_status=True) -> None:
        super().__init__("pico_lerobot_v3_recorder" if enable_commands else "pico_lerobot_v3_recorder_validation")
        control = load_control_config()
        self.config = control["recording"]
        from pico_f1.collection import effective_settings
        # The read-only stationary verifier substitutes an isolated temp root.
        # Never redirect its samples into the real scene dataset.
        if not enable_commands and not (project_root() / "config/collection.yaml").exists():
            collection = dict(source=str(project_root() / "mocap_data"), task=self.config["task"])
        else:
            collection = effective_settings(project_root())
        root = Path(collection["source"])
        self.config = dict(self.config, task=collection["task"])
        self.dataset = LeRobotV3Dataset(root, self.config["fps"], self.config["task"])
        self._cache_lock = threading.Lock()
        self._mode_lock = threading.RLock()
        self._joint_state: dict[str, float] = {}
        self._joint_state_time: dict[str, float] = {}
        self._gripper_state = [0.0, 0.0]
        self._gripper_state_time = [0.0, 0.0]
        self._base_state = [0.0, 0.0, 0.0]
        self._base_state_time = 0.0
        self._joint_action: dict[str, float] = {}
        self._joint_action_time: dict[str, float] = {}
        self._gripper_action: dict[int, float] = {}
        self._gripper_action_time = [0.0, 0.0]
        self._base_action = [0.0, 0.0, 0.0]
        self._base_action_time = 0.0
        self._state_history = SampleHistory(FEATURE_NAMES)
        self._action_history = SampleHistory(FEATURE_NAMES)
        self._cameras: dict[str, tuple[bytes, int, int, int, int, int, int]] = {}
        self._camera_queues = {key: deque() for key in CAMERA_KEYS}
        self._camera_queue_limit = int(self.config.get("camera_queue_frames", 90))
        self._alignment_pending = {key: [] for key in CAMERA_KEYS}
        self._record_start_ros_ns = 0
        self._camera_sequence = {key: 0 for key in CAMERA_KEYS}
        self._camera_sync = CameraSyncGate(
            CAMERA_KEYS,
            max_sync_skew_s=float(self.config["camera_max_skew_s"]),
            max_source_gap_s=float(self.config["camera_max_frame_gap_s"]),
            allow_source_gaps=True,
        )
        self._mode = "idle"
        self._capture: EpisodeCapture | None = None
        self._review_arrays: dict[str, np.ndarray] | None = None
        self._review_video_info: dict | None = None
        self._review_timing_info: dict | None = None
        self._last_drop_log = 0.0
        self._last_status_log = 0.0
        self._last_sync_monotonic = 0.0
        self._last_sync_fault = "waiting for first camera set"
        self._udp_stop = threading.Event()

        status_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._status_pub = self.create_publisher(
            String, "/pico/recording_status", status_qos
        ) if publish_status else None

        sensor_qos = rclpy.qos.qos_profile_sensor_data
        camera_qos = QoSProfile(
            depth=self._camera_queue_limit,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.create_subscription(JointState, "/hal/joint_states", self._joint_cb, sensor_qos)
        self.create_subscription(JointState, "/motion_ctl/gripper/left/state", lambda m: self._gripper_state_cb(0, m), sensor_qos)
        self.create_subscription(JointState, "/motion_ctl/gripper/right/state", lambda m: self._gripper_state_cb(1, m), sensor_qos)
        self.create_subscription(MoveState, "/move/State", self._move_state_cb, 10)
        self.create_subscription(JointState, "/lead/joint_states", self._joint_action_cb, 10)
        self.create_subscription(JointState, "/motion_ctl/joint_ctl", self._joint_action_cb, 10)
        self.create_subscription(GripperCommand, "/motion_ctl/gripper/left", lambda m: self._gripper_action_cb(0, m), 10)
        self.create_subscription(GripperCommand, "/motion_ctl/gripper/right", lambda m: self._gripper_action_cb(1, m), 10)
        self.create_subscription(TwistStamped, "/move/ManualMoveCmd", self._move_action_cb, 10)
        for key, topic in CAMERA_TOPICS.items():
            self.create_subscription(
                CompressedImage,
                topic,
                lambda m, k=key: self._camera_cb(k, m),
                camera_qos,
            )
        self.create_timer(0.05, self._recording_watchdog)

        self._udp_thread = None
        if enable_commands:
            self._udp_thread = threading.Thread(target=self._udp_loop, daemon=True, name="RecorderUDP")
            self._udp_thread.start()
        self.get_logger().info(
            f"LEROBOT_V3_RECORDER_READY root={root} fps={float(self.config['fps']):g} "
            f"features={len(FEATURE_NAMES)} cameras=3"
        )
        self.get_logger().info("CONTROL Xx2=start Yx2=stop Bx1=save Bx2=discard; X+Y reset does not stop recording")
        self._publish_status("idle")

    def _publish_status(self, mode: str) -> None:
        if self._status_pub is None or not rclpy.ok():
            return
        message = String()
        message.data = str(mode)
        self._status_pub.publish(message)

    def _joint_cb(self, msg: JointState) -> None:
        if len(msg.name) != len(msg.position):
            return
        now = time.monotonic()
        stamp = self.get_clock().now().nanoseconds
        with self._cache_lock:
            for name, value in zip(msg.name, msg.position):
                if name in JOINT_MOTOR_NAMES and np.isfinite(value):
                    self._joint_state[name] = float(value)
                    self._joint_state_time[name] = now
                    self._state_history.add(name, stamp, value)

    def _gripper_state_cb(self, index: int, msg: JointState) -> None:
        if not msg.position or not np.isfinite(msg.position[0]):
            return
        with self._cache_lock:
            self._gripper_state[index] = float(msg.position[0])
            self._gripper_state_time[index] = time.monotonic()
            self._state_history.add(FEATURE_NAMES[22 + index], self.get_clock().now().nanoseconds, msg.position[0])

    def _move_state_cb(self, msg: MoveState) -> None:
        values = [float(msg.speed.linear.x), float(msg.speed.linear.y), float(msg.speed.angular.z)]
        if not np.all(np.isfinite(values)):
            return
        with self._cache_lock:
            self._base_state = values
            self._base_state_time = time.monotonic()
            stamp = self.get_clock().now().nanoseconds
            for name, value in zip(FEATURE_NAMES[24:], values):
                self._state_history.add(name, stamp, value)

    def _joint_action_cb(self, msg: JointState) -> None:
        if len(msg.name) != len(msg.position):
            return
        now = time.monotonic()
        stamp = self.get_clock().now().nanoseconds
        with self._cache_lock:
            for name, value in zip(msg.name, msg.position):
                if name in JOINT_MOTOR_NAMES and np.isfinite(value):
                    self._joint_action[name] = float(value)
                    self._joint_action_time[name] = now
                    self._action_history.add(name, stamp, value)

    def _gripper_action_cb(self, index: int, msg: GripperCommand) -> None:
        if np.isfinite(msg.position):
            with self._cache_lock:
                self._gripper_action[index] = float(msg.position)
                self._gripper_action_time[index] = time.monotonic()
                self._action_history.add(FEATURE_NAMES[22 + index], self.get_clock().now().nanoseconds, msg.position)

    def _move_action_cb(self, msg: TwistStamped) -> None:
        values = [float(msg.twist.linear.x), float(msg.twist.linear.y), float(msg.twist.angular.z)]
        if np.all(np.isfinite(values)):
            with self._cache_lock:
                self._base_action = values
                self._base_action_time = time.monotonic()
                stamp = self.get_clock().now().nanoseconds
                for name, value in zip(FEATURE_NAMES[24:], values):
                    self._action_history.add(name, stamp, value)

    def _camera_cb(self, key: str, msg: CompressedImage) -> None:
        arrival_ns = time.monotonic_ns()
        data = bytes(msg.data)
        try:
            width, height = jpeg_dimensions(data)
        except ValueError as exc:
            self.get_logger().error(f"CAMERA_REJECTED {key}: {exc}")
            return
        stamp = _message_stamp_ns(msg, self.get_clock().now().nanoseconds)
        overflow = False
        with self._mode_lock:
            with self._cache_lock:
                self._camera_sequence[key] += 1
                frame = (
                    data,
                    stamp,
                    arrival_ns,
                    width,
                    height,
                    self._camera_sequence[key],
                    stamp,
                )
                self._cameras[key] = frame
                if self._mode == "recording" and stamp >= self._record_start_ros_ns:
                    self._camera_queues[key].append(frame)
            while self._sample():
                pass
            with self._cache_lock:
                overflow = len(self._camera_queues[key]) > self._camera_queue_limit
            if overflow:
                self._reject_active_episode(f"{key} synchronization queue overflow")
                return
        # A row is emitted only when this callback completes a fresh, aligned
        # three-camera set. No timer can reuse an old image.
        while self._sample():
            pass

    def _camera_stamps_locked(self) -> dict[str, CameraFrameStamp]:
        return {
            key: CameraFrameStamp(
                sequence=self._cameras[key][5],
                integrity_timestamp_ns=self._cameras[key][6],
                sync_timestamp_ns=self._cameras[key][1],
            )
            for key in CAMERA_KEYS
            if key in self._cameras
        }

    def _ready_snapshot(self, require_fresh_camera_set: bool = False):
        now = time.monotonic()
        now_ns = time.monotonic_ns()
        ros_now_ns = self.get_clock().now().nanoseconds
        max_age = float(self.config["state_max_age_s"])
        camera_age = float(self.config["camera_max_age_s"])
        with self._cache_lock:
            if any(name not in self._joint_state for name in JOINT_MOTOR_NAMES):
                return None, "joint state incomplete", False
            if not require_fresh_camera_set and any(
                now - self._joint_state_time.get(name, 0.0) > max_age
                for name in JOINT_MOTOR_NAMES
            ):
                return None, "joint state stale", False
            if not require_fresh_camera_set and any(now - value > max_age for value in self._gripper_state_time):
                return None, "OmniPicker state stale", False
            if not require_fresh_camera_set and now - self._base_state_time > max_age:
                return None, "base state stale", False
            if require_fresh_camera_set:
                selected, extras = align_frame_queues(
                    self._camera_queues, self._camera_sync.max_sync_skew_ns
                )
                try:
                    for key, frame in extras:
                        self._capture.add_alignment_extra(key, frame)
                        self._alignment_pending[key].append(
                            CameraFrameStamp(frame[5], frame[6], frame[1])
                        )
                except Exception as exc:
                    return None, f"alignment archive failed: {exc}", True
                if selected is None:
                    return None, "waiting for one fresh frame per camera", False
            else:
                if any(key not in self._cameras for key in CAMERA_KEYS):
                    return None, "camera set incomplete", False
                selected = {key: self._cameras[key] for key in CAMERA_KEYS}
            arrivals = [selected[key][2] for key in CAMERA_KEYS]
            if any(now_ns - value > int(camera_age * 1e9) for value in arrivals):
                return None, "camera frame stale", False
            state = np.asarray(
                [self._joint_state[name] for name in JOINT_MOTOR_NAMES]
                + self._gripper_state + self._base_state,
                dtype=np.float32,
            )
            state_times = [self._joint_state_time[name] for name in JOINT_MOTOR_NAMES]
            state_times += list(self._gripper_state_time)
            state_times += [self._base_state_time] * 3
            joint_action = []
            action_times = []
            for name in JOINT_MOTOR_NAMES:
                action_time = self._joint_action_time.get(name, 0.0)
                if name in self._joint_action and now - action_time <= max_age:
                    joint_action.append(self._joint_action[name])
                    action_times.append(action_time)
                else:
                    joint_action.append(self._joint_state[name])
                    action_times.append(self._joint_state_time[name])
            gripper_action = []
            for index in range(2):
                action_time = self._gripper_action_time[index]
                if index in self._gripper_action and now - action_time <= max_age:
                    gripper_action.append(self._gripper_action[index])
                    action_times.append(action_time)
                else:
                    gripper_action.append(self._gripper_state[index])
                    action_times.append(self._gripper_state_time[index])
            if now - self._base_action_time <= max_age:
                base_action = self._base_action
                action_times += [self._base_action_time] * 3
            else:
                base_action = self._base_state
                action_times += [self._base_state_time] * 3
            action = np.asarray(joint_action + gripper_action + list(base_action), dtype=np.float32)
            cameras = {key: (selected[key][0], selected[key][1]) for key in CAMERA_KEYS}
            camera_info = {
                key: {"width": selected[key][3], "height": selected[key][4]}
                for key in CAMERA_KEYS
            }
            timing = {
                "capture_monotonic_ns": now_ns,
                "camera_integrity_timestamp_ns": [
                    selected[key][6] for key in CAMERA_KEYS
                ],
                "camera_arrival_timestamp_ns": arrivals,
                "state_max_age_ns": int(max(now - value for value in state_times) * 1e9),
                "action_max_age_ns": int(max(now - value for value in action_times) * 1e9),
            }
            # All three F1 headers are in the ROS system-time domain. Use the
            # newest acquisition time as the row reference, not callback time.
            reference_ns = max(selected[key][1] for key in CAMERA_KEYS)
            delay_ns = ros_now_ns - reference_ns
            if require_fresh_camera_set:
                bound_ns = int(max_age * 1e9)
                try:
                    state_samples = [self._state_history.at(name, reference_ns, bound_ns) for name in FEATURE_NAMES]
                except ValueError as exc:
                    return None, str(exc), False
                action_samples = []
                for name, fallback in zip(FEATURE_NAMES, state_samples):
                    try:
                        action_samples.append(self._action_history.at(name, reference_ns, bound_ns))
                    except ValueError:
                        action_samples.append(fallback)
                state = np.asarray([sample[0] for sample in state_samples], dtype=np.float32)
                action = np.asarray([sample[0] for sample in action_samples], dtype=np.float32)
                timing["state_max_age_ns"] = max(sample[1] for sample in state_samples)
                timing["action_max_age_ns"] = max(sample[1] for sample in action_samples)
                decision = self._camera_sync.evaluate(
                    {key: CameraFrameStamp(selected[key][5], selected[key][6], selected[key][1])
                     for key in CAMERA_KEYS}, self._alignment_pending,
                )
                if not decision.ready:
                    return None, decision.reason, decision.fatal
                if decision.source_gaps:
                    try:
                        self._capture.note_source_gaps(decision.source_gaps)
                    except OSError as exc:
                        return None, f"quality journal failed: {exc}", True
                    self.get_logger().warn(
                        f"SOURCE_GAP_RECORDING_CONTINUES training_ready=false gaps={decision.source_gaps}"
                    )
                for key in CAMERA_KEYS:
                    self._camera_queues[key].popleft()
                    self._alignment_pending[key].clear()
            timing["capture_monotonic_ns"] = now_ns - delay_ns
            timing["capture_ros_timestamp_ns"] = reference_ns
        return (state, action, cameras, camera_info, timing), None, False

    def _sample(self) -> bool:
        with self._mode_lock:
            if self._mode != "recording" or self._capture is None:
                return False
            snapshot, fault, fatal = self._ready_snapshot(require_fresh_camera_set=True)
            if fault is not None:
                self._last_sync_fault = str(fault)
                if fatal:
                    self._reject_active_episode(fault)
                return False
            state, action, cameras, _, timing = snapshot
            try:
                self._capture.add(
                    state,
                    action,
                    timing["capture_ros_timestamp_ns"],
                    cameras,
                    timing,
                )
            except Exception as exc:
                self._reject_active_episode(f"encoder error: {exc}")
                return False
            now = time.monotonic()
            self._last_sync_monotonic = now
            if now - self._last_status_log >= 1.0:
                elapsed = now - self._capture.started_monotonic
                self.get_logger().info(
                    f"RECORDING elapsed={elapsed:.1f}s frames={self._capture.frame_count} "
                    f"effective_fps={self._capture.frame_count / max(elapsed, 1e-6):.1f} "
                    f"queued={self._capture.frame_count - len(self._capture.state)} "
                    f"dropped={self._capture.dropped} alignment_archived={self._capture.alignment_extra_count} "
                    f"source_gap_events={len(self._capture.source_gaps)}"
                )
                self._last_status_log = now
            return True

    def _recording_watchdog(self) -> None:
        while self._sample():
            pass
        with self._mode_lock:
            if self._mode != "recording" or self._capture is None:
                return
            if time.monotonic() - self._last_sync_monotonic > float(
                self.config["camera_max_age_s"]
            ):
                with self._cache_lock:
                    queues = {key: len(value) for key, value in self._camera_queues.items()}
                self._reject_active_episode(
                    f"synchronized camera stream stalled: {self._last_sync_fault}; queues={queues}"
                )

    def _udp_loop(self) -> None:
        config_path = os.environ["PICO_F1_CONFIG"]
        spec = importlib.util.spec_from_file_location("ip_config", config_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.2)
        sock.bind((module.local_ip, int(module.port2)))
        self.get_logger().info(f"RECORDER_COMMAND_LISTENING {module.local_ip}:{module.port2}")
        while not self._udp_stop.is_set():
            try:
                data, _ = sock.recvfrom(128)
            except TimeoutError:
                continue
            except OSError:
                break
            values = np.frombuffer(data, dtype=np.float64)
            if len(values) != 1 or not np.isfinite(values[0]):
                continue
            command = int(round(float(values[0])))
            if command == 1:
                self.start_recording()
            elif command == 0:
                self.stop_recording()
            elif command == 2:
                self.save_review()
            elif command == 3:
                self.discard_review()
        sock.close()

    def start_recording(self) -> None:
        with self._mode_lock:
            if self._mode != "idle":
                return
            snapshot, fault, _ = self._ready_snapshot()
            if fault is not None:
                self.get_logger().error(f"RECORD_START_REJECTED reason={fault}")
                self._publish_status("idle")
                return
            _, _, _, camera_info, _ = snapshot
            with self._cache_lock:
                for queue_ in self._camera_queues.values():
                    queue_.clear()
                for pending in self._alignment_pending.values():
                    pending.clear()
                self._camera_sync.arm(self._camera_stamps_locked())
            self._capture = EpisodeCapture(self.dataset.root, self.config, camera_info)
            self._record_start_ros_ns = self.get_clock().now().nanoseconds
            self._mode = "recording"
            self._publish_status("recording")
            self._last_status_log = 0.0
            self._last_sync_monotonic = time.monotonic()
            details = " ".join(
                f"{key}={camera_info[key]['width']}x{camera_info[key]['height']}"
                for key in CAMERA_KEYS
            )
            self.get_logger().info(
                f"RECORDING_STARTED format=LeRobot-v3.0 fps={float(self.config['fps']):g} {details}"
            )

    def stop_recording(self) -> None:
        with self._mode_lock:
            if self._mode != "recording" or self._capture is None:
                return
            capture = self._capture
            while self._sample():
                pass
            if self._mode != "recording":
                return
            self._mode = "finalizing"
            self._publish_status("finalizing")
            try:
                with self._cache_lock:
                    for key, queue_ in self._camera_queues.items():
                        while queue_:
                            capture.add_alignment_extra(key, queue_.popleft(), "stop_boundary")
                arrays, video_info, timing_info = capture.stop()
            except Exception as exc:
                self._reject_active_episode(f"final timing/media validation failed: {exc}")
                return
            self._review_arrays = arrays
            self._review_video_info = video_info
            self._review_timing_info = timing_info
            self._mode = "review"
            self._publish_status("review")
            if not timing_info["training_ready"]:
                self.get_logger().warn(
                    "REVIEW_RECOVERY_ONLY: source frames are missing; B single preserves the whole "
                    "observed task in recovery/, NOT the training dataset; B double discards."
                )
            frames = len(arrays["timestamp_ns"])
            duration = frames / float(self.config["fps"])
            self.get_logger().info(
                f"RECORDING_COMPLETED duration={duration:.2f}s frames={frames} "
                f"fps={float(self.config['fps']):g} dropped={capture.dropped} "
                f"alignment_archived={capture.alignment_extra_count} source_gap_events={len(capture.source_gaps)} "
                f"training_ready={timing_info['training_ready']}"
            )
            self.get_logger().info(
                f"TIMING capture_fps={timing_info['capture_fps']:.3f} "
                f"max_camera_skew_ms={timing_info['max_camera_skew_ms']:.3f} "
                f"max_camera_integrity_gap_ms={timing_info['max_camera_integrity_gap_ms']:.3f} "
                f"max_camera_arrival_gap_ms={timing_info['max_camera_arrival_gap_ms']:.3f} "
                f"max_state_age_ms={timing_info['max_state_age_ms']:.3f} "
                f"max_action_age_ms={timing_info['max_action_age_ms']:.3f}"
            )
            for key in CAMERA_KEYS:
                info = video_info[key]
                self.get_logger().info(
                    f"VIDEO {key} {info['width']}x{info['height']} "
                    f"fps={info['fps']:.1f} frames={info['frames']} codec={info['codec']}"
                )
            self.get_logger().info("+------------------------------------------------------+")
            self.get_logger().info("| 数据待确认：PICO B 单击保存，B 双击丢弃并录下一条 |")
            self.get_logger().info("+------------------------------------------------------+")

    def save_review(self) -> None:
        with self._mode_lock:
            if self._mode != "review" or self._capture is None or self._review_arrays is None:
                return
            if not self._review_timing_info.get("training_ready", True):
                self._reject_active_episode(
                    "operator saved complete observed task with source gaps: "
                    + self._review_timing_info.get("coverage_fault", "")
                )
                return
            try:
                episode, path = self.dataset.commit(
                    self._capture.path, self._review_arrays, self._review_video_info
                )
            except Exception as exc:
                self.get_logger().error(f"EPISODE_SAVE_FAILED error={exc}")
                self._publish_status("review")
                return
            self.get_logger().info(
                f"EPISODE_SAVED episode={episode:06d} data={path} root={self.dataset.root}"
            )
            self._clear_review("idle")

    def discard_review(self) -> None:
        with self._mode_lock:
            if self._mode != "review" or self._capture is None:
                return
            staging = self._capture.path.resolve()
            if (self.dataset.root / ".staging").resolve() not in staging.parents:
                raise RuntimeError("refusing to discard an out-of-tree path")
            shutil.rmtree(staging)
            self.get_logger().info("EPISODE_DISCARDED ready_for_next=true")
            self._clear_review("idle")

    def _clear_review(self, mode: str) -> None:
        self._capture = None
        self._review_arrays = None
        self._review_video_info = None
        self._review_timing_info = None
        with self._cache_lock:
            for queue_ in self._camera_queues.values():
                queue_.clear()
        self._mode = mode
        self._publish_status(mode)

    def _reject_active_episode(self, reason: str) -> None:
        """Quarantine failures without deleting the long recording prefix."""
        capture = self._capture
        recovery_path = None
        if capture is not None:
            self._mode = "finalizing"
            try:
                with self._cache_lock:
                    for key, queue_ in self._camera_queues.items():
                        while queue_:
                            capture.add_alignment_extra(key, queue_.popleft(), "failure_boundary")
                capture.preserve_failure(reason)
                recovery = self.dataset.root / "recovery"
                recovery.mkdir(parents=True, exist_ok=True)
                recovery_path = recovery / capture.path.name
                capture.path.rename(recovery_path)
            except Exception as exc:
                capture.abort(preserve=True)
                recovery_path = capture.path
                self.get_logger().error(f"RECOVERY_FINALIZE_FAILED error={exc}")
        self.get_logger().error(
            f"EPISODE_RECOVERY_PRESERVED training_ready=false reason={reason} "
            f"path={recovery_path} ready_for_next=true"
        )
        self._clear_review("idle")

    def destroy_node(self):
        self._udp_stop.set()
        with self._mode_lock:
            if self._capture is not None:
                self._reject_active_episode("recorder stopped before save confirmation")
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LeRobotRecorderNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException, RCLError):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
