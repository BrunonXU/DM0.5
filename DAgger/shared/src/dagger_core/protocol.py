"""Versioned, robot-agnostic wire protocol for operator intent.

The protocol intentionally has no chassis, joint, GDK, or absolute pose field.
It transports only a bounded local end-effector velocity intent and two
low-dimensional hand controls.  Newline-delimited JSON keeps the first Shadow
deployment inspectable without extra dependencies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any


PROTOCOL_VERSION = 1
MAX_WIRE_BYTES = 4096
VALID_SIDES = {None, "left", "right"}
VALID_MODES = {"translation", "rotation", "combined"}


class ProtocolError(ValueError):
    pass


def _finite_bounded(value: object, label: str, lower: float, upper: float) -> float:
    if isinstance(value, bool):
        raise ProtocolError(f"{label} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{label} must be a number") from exc
    if not math.isfinite(parsed) or not lower <= parsed <= upper:
        raise ProtocolError(f"{label} must be finite and in [{lower}, {upper}]")
    return parsed


@dataclass(frozen=True)
class IntentPacket:
    session_id: str
    sequence: int
    sent_at_unix_ns: int
    deadman: bool
    active_side: str | None
    mode: str
    axes: tuple[float, float, float]
    grip_velocity: float
    thumb_clearance: bool
    warning: str | None = None
    angular_axes: tuple[float, float, float] = (0.0, 0.0, 0.0)
    grip_target: float | None = None
    speed_scale: float = 1.0
    home_reset: bool = False
    hil_toggle_id: int = 0
    hil_button_pressed: bool = False
    version: int = PROTOCOL_VERSION
    kind: str = "intent"
    # Optional negotiated extension. Omitted on legacy wire connections.
    recovery_token: str | None = None
    controls_neutral: bool | None = None
    episode_end_id: int | None = None
    episode_end_pressed: bool | None = None
    policy_switch_id: int | None = None
    policy_switch_pressed: bool | None = None

    operator_protocol: str | None = None
    home_end_id: int | None = None
    local_result: str | None = None

    def __post_init__(self) -> None:
        if self.operator_protocol is not None:
            if self.operator_protocol not in ('f1_rlt_local_v2', 'f1_rlt_local_v3', 'f1_rlt_local_v4') or type(self.home_end_id) is not int or self.home_end_id < 0:
                raise ProtocolError('invalid local RLT operator extension')
            if self.policy_switch_id is None or self.episode_end_id is None:
                raise ProtocolError('local RLT requires complete event counters')
        elif self.home_end_id is not None:
            raise ProtocolError('home_end_id requires operator_protocol')
        if self.operator_protocol == 'f1_rlt_local_v4':
            if self.local_result not in ('success', 'failure'):
                raise ProtocolError('v4 requires explicit local result kind')
        elif self.local_result is not None:
            raise ProtocolError('local_result requires negotiated v4')
        if self.policy_switch_id is not None and (type(self.policy_switch_id) is not int or self.policy_switch_id < 0):
            raise ProtocolError('invalid policy_switch_id')
        if self.policy_switch_pressed is not None and type(self.policy_switch_pressed) is not bool:
            raise ProtocolError('invalid policy_switch_pressed')
        if (self.policy_switch_id is None) != (self.policy_switch_pressed is None):
            raise ProtocolError('policy switch extension must be complete')
        if self.episode_end_id is not None and (type(self.episode_end_id) is not int or self.episode_end_id < 0):
            raise ProtocolError("invalid episode_end_id")
        if self.episode_end_pressed is not None and type(self.episode_end_pressed) is not bool:
            raise ProtocolError("invalid episode_end_pressed")
        if (self.episode_end_id is None) != (self.episode_end_pressed is None):
            raise ProtocolError("episode end extension must be complete")
        if self.recovery_token is not None and (not isinstance(self.recovery_token, str)
                or len(self.recovery_token) != 32
                or any(c not in '0123456789abcdef' for c in self.recovery_token)):
            raise ProtocolError('invalid recovery token')
        if self.controls_neutral is not None and type(self.controls_neutral) is not bool:
            raise ProtocolError('controls_neutral must be boolean')
        if self.version != PROTOCOL_VERSION or self.kind != "intent":
            raise ProtocolError("unsupported protocol version or packet kind")
        if not self.session_id or len(self.session_id) > 128:
            raise ProtocolError("session_id must be 1..128 characters")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ProtocolError("sequence must be a non-negative integer")
        if not isinstance(self.sent_at_unix_ns, int) or isinstance(self.sent_at_unix_ns, bool) or self.sent_at_unix_ns <= 0:
            raise ProtocolError("sent_at_unix_ns must be a positive integer")
        if self.active_side not in VALID_SIDES:
            raise ProtocolError("active_side must be left, right, or null")
        if self.mode not in VALID_MODES:
            raise ProtocolError("invalid mode")
        if len(self.axes) != 3:
            raise ProtocolError("axes must have exactly three values")
        for index, value in enumerate(self.axes):
            _finite_bounded(value, f"axes[{index}]", -1.0, 1.0)
        if len(self.angular_axes) != 3:
            raise ProtocolError("angular_axes must have exactly three values")
        for index, value in enumerate(self.angular_axes):
            _finite_bounded(value, f"angular_axes[{index}]", -1.0, 1.0)
        _finite_bounded(self.grip_velocity, "grip_velocity", -1.0, 1.0)
        if self.grip_target is not None:
            _finite_bounded(self.grip_target, "grip_target", 0.0, 1.0)
        _finite_bounded(self.speed_scale, "speed_scale", 1.0, 2.0)
        if (
            not isinstance(self.deadman, bool)
            or not isinstance(self.thumb_clearance, bool)
            or not isinstance(self.home_reset, bool)
            or not isinstance(self.hil_button_pressed, bool)
        ):
            raise ProtocolError(
                "deadman, thumb_clearance, home_reset and hil_button_pressed "
                "must be booleans"
            )
        if (
            not isinstance(self.hil_toggle_id, int)
            or isinstance(self.hil_toggle_id, bool)
            or self.hil_toggle_id < 0
        ):
            raise ProtocolError("hil_toggle_id must be a non-negative integer")
        if self.home_reset and self.deadman:
            raise ProtocolError("home reset requires arm deadman released")
        if (self.deadman or self.grip_target is not None) and self.active_side is None:
            raise ProtocolError("arm or hand intent requires an active_side")
        if not self.deadman and (
            any(abs(value) > 0.0 for value in self.axes)
            or any(abs(value) > 0.0 for value in self.angular_axes)
            or abs(self.grip_velocity) > 0.0
            or self.thumb_clearance
        ):
            raise ProtocolError("arm-disabled intent must contain zero arm outputs")
        if self.home_reset and self.grip_target is not None:
            raise ProtocolError("home reset cannot carry a hand target")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "IntentPacket":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(payload) - allowed
        if unknown:
            raise ProtocolError(f"unknown intent fields: {sorted(unknown)}")
        try:
            copied = dict(payload)
            copied["axes"] = tuple(copied["axes"])
            if "angular_axes" in copied:
                copied["angular_axes"] = tuple(copied["angular_axes"])
            return cls(**copied)
        except (KeyError, TypeError) as exc:
            raise ProtocolError(f"malformed intent: {exc}") from exc


@dataclass(frozen=True)
class AckPacket:
    session_id: str
    sequence: int
    accepted: bool
    receiver_state: str
    reason: str | None = None
    version: int = PROTOCOL_VERSION
    kind: str = "ack"

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AckPacket":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(payload) - allowed
        if unknown:
            raise ProtocolError(f"unknown ack fields: {sorted(unknown)}")
        try:
            packet = cls(**payload)
        except TypeError as exc:
            raise ProtocolError(f"malformed ack: {exc}") from exc
        if packet.version != PROTOCOL_VERSION or packet.kind != "ack":
            raise ProtocolError("unsupported ack")
        if (
            not packet.session_id
            or not isinstance(packet.sequence, int)
            or isinstance(packet.sequence, bool)
            or packet.sequence < 0
            or not isinstance(packet.accepted, bool)
            or packet.receiver_state not in {"HOLD", "SHADOW_ACTIVE", "POWERED_ACTIVE"}
        ):
            raise ProtocolError("invalid ack fields")
        return packet


def encode_packet(packet: IntentPacket | AckPacket) -> bytes:
    fields = asdict(packet)
    for key in ('recovery_token', 'controls_neutral', 'episode_end_id', 'episode_end_pressed',
                'policy_switch_id', 'policy_switch_pressed', 'operator_protocol', 'home_end_id', 'local_result'):
        if fields.get(key) is None:
            fields.pop(key, None)
    encoded = (json.dumps(fields, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_WIRE_BYTES:
        raise ProtocolError("packet exceeds maximum wire size")
    return encoded


def decode_intent(line: bytes) -> IntentPacket:
    if not line or len(line) > MAX_WIRE_BYTES:
        raise ProtocolError("invalid wire packet size")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("packet is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("packet root must be an object")
    return IntentPacket.from_mapping(payload)


def decode_ack(line: bytes) -> AckPacket:
    if not line or len(line) > MAX_WIRE_BYTES:
        raise ProtocolError("invalid wire packet size")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("ack is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("ack root must be an object")
    return AckPacket.from_mapping(payload)
