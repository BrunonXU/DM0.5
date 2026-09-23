from __future__ import annotations

import socket
import threading
import time

from g2teleop.async_transport import AsyncLatestIntentTransport
from g2teleop.protocol import AckPacket, IntentPacket, decode_intent, encode_packet


def _packet(sequence: int) -> IntentPacket:
    return IntentPacket(
        session_id="async-test",
        sequence=sequence,
        sent_at_unix_ns=time.time_ns(),
        deadman=False,
        active_side="left",
        mode="combined",
        axes=(0.0, 0.0, 0.0),
        angular_axes=(0.0, 0.0, 0.0),
        grip_velocity=0.0,
        grip_target=None,
        thumb_clearance=False,
    )


def test_publish_does_not_wait_for_delayed_ack_and_keeps_latest_state() -> None:
    ready = threading.Event()
    done = threading.Event()
    received: list[int] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    def serve() -> None:
        ready.set()
        conn, _ = server.accept()
        with conn:
            buffer = bytearray()
            while len(received) < 2:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\n" in buffer and len(received) < 2:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    packet = decode_intent(bytes(raw))
                    received.append(packet.sequence)
                    time.sleep(0.08)
                    conn.sendall(encode_packet(AckPacket(
                        session_id=packet.session_id,
                        sequence=packet.sequence,
                        accepted=True,
                        receiver_state="HOLD",
                    )))
        done.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(1.0)
    with AsyncLatestIntentTransport(
        "127.0.0.1", port, ack_timeout_s=0.5, reconnect_delay_s=0.01,
        status=lambda _message: None,
    ) as transport:
        deadline = time.monotonic() + 1.0
        while not transport.snapshot().connected and time.monotonic() < deadline:
            time.sleep(0.005)
        generation = transport.snapshot().generation
        assert transport.publish(generation, _packet(0))
        first_deadline = time.monotonic() + 0.5
        while not received and time.monotonic() < first_deadline:
            time.sleep(0.002)
        assert received == [0]
        started = time.monotonic()
        for sequence in range(1, 20):
            assert transport.publish(generation, _packet(sequence))
        publish_elapsed = time.monotonic() - started
        assert publish_elapsed < 0.05
        assert done.wait(1.0)
        ack_deadline = time.monotonic() + 0.5
        while transport.snapshot().acked_packets < 2 and time.monotonic() < ack_deadline:
            time.sleep(0.002)
        snapshot = transport.snapshot()
        assert snapshot.dropped_latest_packets >= 17
        assert snapshot.acked_packets >= 2
    server.close()
    assert received[0] <= received[1]
    assert received[-1] == 19


def test_stale_connection_generation_cannot_publish() -> None:
    transport = AsyncLatestIntentTransport("127.0.0.1", 1, status=lambda _message: None)
    assert not transport.publish(0, _packet(0))
