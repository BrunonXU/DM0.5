"""Non-blocking latest-state TCP transport for Windows operator intent.

Gamepad sampling must never wait for a network ACK.  This worker owns the
socket, sends only the newest published packet, and reads ACKs as telemetry.
Intermediate packets are deliberately overwritten instead of replayed late.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import select
import socket
import threading
import time
from typing import Callable

from .protocol import AckPacket, IntentPacket, decode_ack, encode_packet


@dataclass(frozen=True)
class TransportSnapshot:
    connected: bool
    generation: int
    last_ack: AckPacket | None
    sent_packets: int
    acked_packets: int
    dropped_latest_packets: int
    reconnects: int
    ack_rtt_ms: tuple[float, ...]
    last_error: str | None


class AsyncLatestIntentTransport:
    """A single-slot, reconnecting TCP publisher with asynchronous ACKs."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ack_timeout_s: float = 0.5,
        reconnect_delay_s: float = 1.0,
        connect_timeout_s: float = 1.0,
        status: Callable[[str], None] = print,
    ) -> None:
        if ack_timeout_s <= 0.0 or reconnect_delay_s < 0.0 or connect_timeout_s <= 0.0:
            raise ValueError("transport timeouts must be positive")
        self.host = host
        self.port = int(port)
        self.ack_timeout_s = float(ack_timeout_s)
        self.reconnect_delay_s = float(reconnect_delay_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self._status = status
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._generation = 0
        self._latest: tuple[int, IntentPacket] | None = None
        self._last_ack: AckPacket | None = None
        self._last_error: str | None = None
        self._sent_packets = 0
        self._acked_packets = 0
        self._dropped_latest_packets = 0
        self._reconnects = 0
        self._rtt_ms: deque[float] = deque(maxlen=500)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("transport already started")
        self._thread = threading.Thread(
            target=self._run, name="g2-latest-intent-transport", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def publish(self, generation: int, packet: IntentPacket) -> bool:
        """Publish for the current connection; stale generations are rejected."""
        with self._lock:
            if not self._connected or generation != self._generation:
                return False
            if self._latest is not None:
                self._dropped_latest_packets += 1
            self._latest = (generation, packet)
        self._wake.set()
        return True

    def snapshot(self) -> TransportSnapshot:
        with self._lock:
            return TransportSnapshot(
                connected=self._connected,
                generation=self._generation,
                last_ack=self._last_ack,
                sent_packets=self._sent_packets,
                acked_packets=self._acked_packets,
                dropped_latest_packets=self._dropped_latest_packets,
                reconnects=self._reconnects,
                ack_rtt_ms=tuple(self._rtt_ms),
                last_error=self._last_error,
            )

    def _mark_connected(self) -> int:
        with self._lock:
            self._generation += 1
            self._connected = True
            self._latest = None
            self._last_ack = None
            self._last_error = None
            return self._generation

    def _mark_disconnected(self, error: BaseException) -> None:
        with self._lock:
            self._connected = False
            self._latest = None
            self._last_ack = None
            self._last_error = f"{type(error).__name__}: {error}"

    def _take_latest(self, generation: int) -> IntentPacket | None:
        with self._lock:
            item = self._latest
            if item is None or item[0] != generation:
                return None
            self._latest = None
            return item[1]

    def _record_sent(self) -> None:
        with self._lock:
            self._sent_packets += 1

    def _record_ack(self, ack: AckPacket, rtt_s: float) -> None:
        with self._lock:
            self._last_ack = ack
            self._acked_packets += 1
            self._rtt_ms.append(rtt_s * 1000.0)

    @staticmethod
    def _send_wire(sock: socket.socket, wire: bytes, timeout_s: float = 0.05) -> None:
        view = memoryview(wire)
        deadline = time.monotonic() + timeout_s
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError(f"socket was not writable for {timeout_s * 1000.0:.0f} ms")
            _, writable, exceptional = select.select([], [sock], [sock], remaining)
            if exceptional:
                raise ConnectionError("socket exception while sending intent")
            if not writable:
                continue
            sent = sock.send(view)
            if sent <= 0:
                raise ConnectionError("socket closed while sending intent")
            view = view[sent:]

    def _run_connection(self, sock: socket.socket, generation: int) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        buffer = bytearray()
        sent_at: dict[tuple[str, int], float] = {}
        while not self._stop.is_set():
            packet = self._take_latest(generation)
            if packet is not None:
                wire = encode_packet(packet)
                # Payloads are below 4 KiB.  Wait briefly for writability, but
                # never couple this wait to the gamepad sampling thread.
                self._send_wire(sock, wire)
                now = time.monotonic()
                sent_at[(packet.session_id, packet.sequence)] = now
                self._record_sent()

            readable, _, exceptional = select.select([sock], [], [sock], 0.005)
            if exceptional:
                raise ConnectionError("socket exception while waiting for ACK")
            if readable:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("G2 receiver closed the connection")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    ack = decode_ack(bytes(raw))
                    key = (ack.session_id, ack.sequence)
                    started = sent_at.pop(key, None)
                    if started is None:
                        raise ConnectionError("ACK does not match a pending packet")
                    acked_at = time.monotonic()
                    self._record_ack(ack, acked_at - started)
                # Keep the pending timestamp map bounded even if an invalid
                # peer drops selected ACKs; the timeout below still disconnects.
                if len(sent_at) > 256:
                    oldest = sorted(sent_at.items(), key=lambda item: item[1])[:-128]
                    for key, _ in oldest:
                        sent_at.pop(key, None)

            now = time.monotonic()
            oldest_pending = min(sent_at.values()) if sent_at else None
            if oldest_pending is not None and now - oldest_pending > self.ack_timeout_s:
                raise TimeoutError(f"no ACK received for {self.ack_timeout_s * 1000.0:.0f} ms")
            if packet is None:
                self._wake.wait(0.005)
                self._wake.clear()

    def _run(self) -> None:
        first_connection = True
        while not self._stop.is_set():
            try:
                with socket.create_connection(
                    (self.host, self.port), timeout=self.connect_timeout_s
                ) as sock:
                    generation = self._mark_connected()
                    if not first_connection:
                        with self._lock:
                            self._reconnects += 1
                    first_connection = False
                    self._status(
                        f"Connected generation={generation}. Release controls, "
                        "select side, then press RB."
                    )
                    self._run_connection(sock, generation)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._mark_disconnected(exc)
                self._status(
                    f"HOLD / transport unavailable: {exc}; retrying in "
                    f"{self.reconnect_delay_s:g} s"
                )
                self._stop.wait(self.reconnect_delay_s)
        with self._lock:
            self._connected = False
            self._latest = None

    def __enter__(self) -> "AsyncLatestIntentTransport":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
