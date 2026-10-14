"""Receiver-side validation state for the network-only G2 Shadow stage."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import AckPacket, IntentPacket


@dataclass
class ShadowSession:
    session_id: str | None = None
    last_sequence: int = -1
    require_disabled_packet: bool = True
    state: str = "HOLD"

    def watchdog_timeout(self) -> None:
        self.state = "HOLD"
        self.require_disabled_packet = True

    def process(self, packet: IntentPacket) -> AckPacket:
        if packet.session_id != self.session_id:
            self.session_id = packet.session_id
            self.last_sequence = -1
            self.require_disabled_packet = True
            self.state = "HOLD"

        if packet.sequence <= self.last_sequence:
            self.watchdog_timeout()
            return self._ack(packet, False, "non_increasing_sequence")
        self.last_sequence = packet.sequence

        if not packet.deadman:
            self.require_disabled_packet = False
            self.state = "HOLD"
            return self._ack(packet, True, None)

        if self.require_disabled_packet:
            self.state = "HOLD"
            return self._ack(packet, False, "disabled_packet_required_before_enable")

        self.state = "SHADOW_ACTIVE"
        return self._ack(packet, True, None)

    def _ack(self, packet: IntentPacket, accepted: bool, reason: str | None) -> AckPacket:
        return AckPacket(
            session_id=packet.session_id,
            sequence=packet.sequence,
            accepted=accepted,
            receiver_state=self.state,
            reason=reason,
        )
