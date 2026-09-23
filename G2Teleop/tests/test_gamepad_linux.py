from __future__ import annotations

import struct

import pytest

from g2teleop.gamepad_linux import (
    JS_EVENT_AXIS,
    JS_EVENT_BUTTON,
    JS_EVENT_INIT,
    decode_js_event,
)


def test_decode_axis_event_and_normalize() -> None:
    payload = struct.pack("<IhBB", 1234, -32768, JS_EVENT_AXIS, 1)
    event = decode_js_event(payload)
    assert event.kind == "axis"
    assert event.number == 1
    assert event.expected_name == "left_y"
    assert event.normalized_value == -1.0
    assert not event.is_initial


def test_decode_initial_button_event() -> None:
    payload = struct.pack(
        "<IhBB", 8, 1, JS_EVENT_BUTTON | JS_EVENT_INIT, 5
    )
    event = decode_js_event(payload)
    assert event.kind == "button"
    assert event.expected_name == "rb"
    assert event.normalized_value == 1.0
    assert event.is_initial


def test_reject_short_event() -> None:
    with pytest.raises(ValueError, match="8 bytes"):
        decode_js_event(b"short")

