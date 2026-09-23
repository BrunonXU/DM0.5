"""Dependency-free, read-only Linux joystick event reader."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct
from typing import BinaryIO


JS_EVENT_SIZE = 8
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80
_JS_EVENT = struct.Struct("<IhBB")

EXPECTED_F710_AXES: dict[int, str] = {
    0: "left_x",
    1: "left_y",
    2: "left_trigger",
    3: "right_x",
    4: "right_y",
    5: "right_trigger",
    6: "dpad_x",
    7: "dpad_y",
}

EXPECTED_F710_BUTTONS: dict[int, str] = {
    0: "a",
    1: "b",
    2: "x",
    3: "y",
    4: "lb",
    5: "rb",
    6: "back",
    7: "start",
    8: "guide",
    9: "left_stick",
    10: "right_stick",
}


class JoystickDisconnectedError(RuntimeError):
    pass


@dataclass(frozen=True)
class JoystickEvent:
    device_time_ms: int
    value: int
    event_type: int
    number: int
    is_initial: bool

    @property
    def kind(self) -> str:
        if self.event_type == JS_EVENT_AXIS:
            return "axis"
        if self.event_type == JS_EVENT_BUTTON:
            return "button"
        return "unknown"

    @property
    def expected_name(self) -> str | None:
        if self.kind == "axis":
            return EXPECTED_F710_AXES.get(self.number)
        if self.kind == "button":
            return EXPECTED_F710_BUTTONS.get(self.number)
        return None

    @property
    def normalized_value(self) -> float:
        if self.kind != "axis":
            return float(bool(self.value))
        divisor = 32767.0 if self.value >= 0 else 32768.0
        return max(-1.0, min(1.0, self.value / divisor))


def decode_js_event(payload: bytes) -> JoystickEvent:
    if len(payload) != JS_EVENT_SIZE:
        raise ValueError(f"joystick event must be {JS_EVENT_SIZE} bytes")
    device_time_ms, value, raw_type, number = _JS_EVENT.unpack(payload)
    return JoystickEvent(
        device_time_ms=device_time_ms,
        value=value,
        event_type=raw_type & ~JS_EVENT_INIT,
        number=number,
        is_initial=bool(raw_type & JS_EVENT_INIT),
    )


class LinuxJoystickReader:
    """Open a Linux ``/dev/input/js*`` device read-only."""

    def __init__(self, device: str | Path) -> None:
        self.device = Path(device)
        self._handle: BinaryIO | None = None

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> None:
        if self.is_open:
            return
        try:
            fd = os.open(self.device, os.O_RDONLY)
        except OSError as exc:
            raise JoystickDisconnectedError(
                f"cannot open joystick {self.device}: {exc}"
            ) from exc
        self._handle = os.fdopen(fd, "rb", buffering=0)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def read_event(self) -> JoystickEvent:
        if self._handle is None:
            raise JoystickDisconnectedError("joystick reader is not open")
        payload = self._handle.read(JS_EVENT_SIZE)
        if payload == b"":
            raise JoystickDisconnectedError(
                f"joystick {self.device} disconnected or reached EOF"
            )
        if len(payload) != JS_EVENT_SIZE:
            raise JoystickDisconnectedError(
                f"short joystick read: {len(payload)} of {JS_EVENT_SIZE} bytes"
            )
        return decode_js_event(payload)

    def __enter__(self) -> "LinuxJoystickReader":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

