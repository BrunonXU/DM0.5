"""Build F1 state19 from live feedback and pass original compressed images.

Freshness is measured on the robot's monotonic receipt clock. Camera headers
are retained as evidence only: they are not assumed to share a hardware clock
with HAL or the chassis. This module needs neither ROS nor model dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass

from f1_replay.bundle import ARM_NAMES, BODY_NAMES
from f1_replay.runtime import ReplayError, finite_number, fresh_value


CAMERAS = ("head", "left", "right")


class CameraObservationError(ReplayError):
    """Only camera readiness failures may be retried between stopped chunks."""
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def camera_diagnostics(snapshot, now_ns):
    result = {}
    for name in CAMERAS:
        item = snapshot.get("cameras", {}).get(name, {})
        received = item.get("received_ns") if isinstance(item, dict) else None
        result[name] = {"received_ns": received,
                        "age_s": (now_ns-received)/1e9 if type(received) is int else None,
                        "header_stamp_ns": item.get("header_stamp_ns") if isinstance(item, dict) else None}
    return result


@dataclass(frozen=True)
class Observation:
    state19: tuple
    body5: tuple
    images: dict[str, bytes]
    captured_ns: int
    camera_metadata: dict

    @property
    def anchor_joints14(self):
        """The server takes FK of these exact request joints for the whole chunk."""
        return self.state19[:14]


def positive_seconds(value, label):
    if not finite_number(value) or value <= 0:
        raise ReplayError(f"{label} must be a finite positive number")
    return value * 1e9


def fresh_receipt(item, now_ns, max_age_ns, label):
    received = item.get("received_ns") if isinstance(item, dict) else None
    if type(received) is not int or not 0 <= now_ns - received <= max_age_ns:
        raise ReplayError(f"Missing or stale local receipt timestamp: {label}")
    return received


def capture_observation(snapshot, *, now_ns, feedback_max_age_s,
                        camera_max_age_s, camera_max_spread_s):
    """Capture one causal snapshot; never synthesize missing state or cameras."""
    if type(now_ns) is not int or now_ns < 0 or not isinstance(snapshot, dict):
        raise ReplayError("Invalid observation snapshot or monotonic clock")
    feedback_age = positive_seconds(feedback_max_age_s, "feedback_max_age_s")
    camera_age = positive_seconds(camera_max_age_s, "camera_max_age_s")
    camera_spread = positive_seconds(camera_max_spread_s, "camera_max_spread_s")
    joints = snapshot.get("joints", {})
    arms = tuple(fresh_value(joints.get(name), now_ns, feedback_age, name)
                 for name in ARM_NAMES)
    body = tuple(fresh_value(joints.get(name), now_ns, feedback_age, name)
                 for name in BODY_NAMES)
    grippers = tuple(fresh_value(snapshot.get("grippers", {}).get(side), now_ns,
                                feedback_age, side)
                     for side in ("left", "right"))
    if any(not 0 <= value <= 100 for value in grippers):
        raise ReplayError("Gripper feedback must use the F1 0..100 scale")
    base = snapshot.get("base", {})
    fresh_receipt(base, now_ns, feedback_age, "base")
    speeds = tuple(base.get(name) for name in ("vx", "vy", "wz"))
    if not all(finite_number(value) for value in speeds):
        raise ReplayError("Missing or nonfinite measured base vx/vy/wz")

    images, metadata = {}, {}
    for name in CAMERAS:
        camera = snapshot.get("cameras", {}).get(name)
        try:
            received = fresh_receipt(camera, now_ns, camera_age, f"camera/{name}")
        except ReplayError as exc:
            raise CameraObservationError(str(exc), camera_diagnostics(snapshot, now_ns)) from exc
        data = camera.get("data")
        if not isinstance(data, bytes) or not data:
            raise CameraObservationError(f"Missing original compressed image: {name}",
                                         camera_diagnostics(snapshot, now_ns))
        images[name] = data
        metadata[name] = {
            "received_ns": received,
            "age_s": (now_ns - received) / 1e9,
            "header_stamp_ns": camera.get("header_stamp_ns"),
            "format": camera.get("format", ""),
        }
    receipts = [entry["received_ns"] for entry in metadata.values()]
    if max(receipts) - min(receipts) > camera_spread:
        raise CameraObservationError("Three-camera receipt spread exceeds the configured budget",
                                     camera_diagnostics(snapshot, now_ns))
    return Observation(tuple(float(value) for value in arms + grippers + speeds),
                       tuple(float(value) for value in body), images, now_ns, metadata)
