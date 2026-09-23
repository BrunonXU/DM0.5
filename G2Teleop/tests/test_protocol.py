from __future__ import annotations

from dataclasses import replace

import pytest

from g2teleop.protocol import IntentPacket, ProtocolError, decode_intent, encode_packet
from g2teleop.shadow import ShadowSession


def packet(sequence: int = 0, deadman: bool = False) -> IntentPacket:
    return IntentPacket(
        session_id="session-a", sequence=sequence, sent_at_unix_ns=1,
        deadman=deadman, active_side="left",
        mode="translation", axes=(0.1, 0.0, 0.0) if deadman else (0.0, 0.0, 0.0),
        grip_velocity=0.0, thumb_clearance=False,
    )


def test_intent_round_trip() -> None:
    original = packet()
    assert decode_intent(encode_packet(original).rstrip(b"\n")) == original


def test_disabled_packet_cannot_carry_motion() -> None:
    with pytest.raises(ProtocolError, match="zero arm outputs"):
        replace(packet(), axes=(0.1, 0.0, 0.0))
    with pytest.raises(ProtocolError, match="zero arm outputs"):
        replace(packet(), angular_axes=(0.0, 0.1, 0.0))
    # A speed selection is state, not motion, and may be carried while arms hold.
    assert replace(packet(), speed_scale=2.0).speed_scale == 2.0


def test_discrete_hand_target_is_allowed_without_arm_deadman() -> None:
    hand_only = replace(packet(), grip_target=1.0)
    assert decode_intent(encode_packet(hand_only).rstrip(b"\n")) == hand_only


def test_global_home_request_is_arm_disabled_and_cannot_mix_with_hand_target() -> None:
    request = replace(packet(), active_side=None, home_reset=True)
    assert decode_intent(encode_packet(request).rstrip(b"\n")) == request
    with pytest.raises(ProtocolError, match="deadman released"):
        replace(packet(deadman=True), home_reset=True)
    with pytest.raises(ProtocolError, match="cannot carry a hand target"):
        replace(packet(), home_reset=True, grip_target=1.0)


def test_speed_scale_is_bounded_and_round_trips() -> None:
    boosted = replace(packet(deadman=True), speed_scale=2.0)
    assert decode_intent(encode_packet(boosted).rstrip(b"\n")) == boosted
    with pytest.raises(ProtocolError, match="speed_scale"):
        replace(packet(deadman=True), speed_scale=2.01)


def test_hil_toggle_state_is_backward_compatible_and_validated() -> None:
    toggled = replace(packet(), hil_toggle_id=3, hil_button_pressed=True)
    assert decode_intent(encode_packet(toggled).rstrip(b"\n")) == toggled
    with pytest.raises(ProtocolError, match="hil_toggle_id"):
        replace(packet(), hil_toggle_id=-1)
    with pytest.raises(ProtocolError, match="hil_button_pressed"):
        replace(packet(), hil_button_pressed=1)


def test_sequence_must_be_an_integer() -> None:
    with pytest.raises(ProtocolError, match="non-negative integer"):
        replace(packet(), sequence=1.5)


def test_receiver_requires_disabled_packet_before_enable() -> None:
    session = ShadowSession()
    rejected = session.process(packet(deadman=True))
    assert not rejected.accepted
    assert rejected.receiver_state == "HOLD"
    assert session.process(packet(sequence=1)).accepted
    enabled = session.process(packet(sequence=2, deadman=True))
    assert enabled.accepted
    assert enabled.receiver_state == "SHADOW_ACTIVE"


def test_watchdog_requires_disabled_before_reenable() -> None:
    session = ShadowSession()
    session.process(packet())
    session.process(packet(sequence=1, deadman=True))
    session.watchdog_timeout()
    rejected = session.process(packet(sequence=2, deadman=True))
    assert not rejected.accepted
    assert rejected.reason == "disabled_packet_required_before_enable"


def test_reject_replayed_sequence() -> None:
    session = ShadowSession()
    session.process(packet())
    session.process(packet(sequence=1, deadman=True))
    replay = session.process(packet(sequence=1, deadman=True))
    assert not replay.accepted
    assert replay.reason == "non_increasing_sequence"
    assert replay.receiver_state == "HOLD"
