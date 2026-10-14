"""Immutable shared acquisition contract. No Robot owner or inference scheduling."""
from dataclasses import dataclass, asdict
import math

CAMERAS = ("head", "left_wrist", "right_wrist")


@dataclass(frozen=True)
class DeviceTimestamp:
    value: int
    unit: str
    clock_domain: str

    def __post_init__(self):
        if type(self.value) is not int or self.value < 0 or not self.unit or not self.clock_domain or len(self.unit) > 32 or len(self.clock_domain) > 128:
            raise ValueError("timestamp requires raw integer, unit and clock domain")


@dataclass(frozen=True)
class TimestampedFrame:
    camera: str
    frame_id: int
    timestamp: DeviceTimestamp
    host_received_monotonic_ns: int
    payload: bytes
    media_type: str
    width: int
    height: int

    def __post_init__(self):
        if self.camera not in CAMERAS or self.frame_id < 0 or self.host_received_monotonic_ns < 0:
            raise ValueError("invalid camera/frame/host timestamp")
        if type(self.payload) is not bytes or not self.payload or min(self.width, self.height) <= 0:
            raise ValueError("immutable nonempty payload and dimensions required")
        if self.media_type not in ("image/jpeg", "image/png"):
            raise ValueError("native JPEG/PNG required; raw frames need explicit encoding upstream")

    def metadata(self):
        return {k: v for k, v in asdict(self).items() if k != "payload"}


