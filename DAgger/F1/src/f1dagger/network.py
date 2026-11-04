"""Loopback-only F710 intent server; no robot calls on the network thread."""
from dataclasses import replace
import socket
import select
import threading
import time
from dagger_core.protocol import decode_intent, encode_packet, MAX_WIRE_BYTES
from dagger_core.shadow import ShadowSession
from .input_recovery import RecoveryChallenges


def _disconnect_priority(reason):
    if reason is None:
        return 0
    if reason == 'operator_disconnected' or reason.startswith('operator_transport_error:'):
        return 1
    return 2


class IntentServer:
    def __init__(self, port, *, execution_mode=None, identity=""):
        if execution_mode not in (None, 'shadow', 'powered'):
            raise ValueError('execution_mode must be shadow or powered')
        self.execution_mode = execution_mode
        self.identity = identity
        self.recovery = RecoveryChallenges()
        self.lock = threading.Lock()
        self.latest = None
        self.disconnected = None
        self.status = 'waiting_for_start'
        self.stop_event = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen(1)
        self.sock.settimeout(.1)
        self.thread = threading.Thread(target=self.run, daemon=True, name='f1-f710')
        self.thread.start()

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                session = ShadowSession()
                connection_session = None
                buffer = b''
                loss_reason = 'operator_disconnected'
                try:
                    with conn:
                        conn.setblocking(False)
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        # SSH can leave a connected channel without any data.
                        # Polling recv alone never releases it for a reconnect.
                        # This cleanup is separate from the 200 ms motion watchdog.
                        packet_deadline = time.monotonic() + 1.
                        outgoing = bytearray()
                        rejected = None
                        while not self.stop_event.is_set():
                            if time.monotonic() >= packet_deadline:
                                raise TimeoutError('intent packet timeout')
                            # ACK backpressure must never block intent reception.
                            # Keep every ACK ordered; the sender already bounds
                            # its outstanding requests to 256 packets.
                            readable, writable, _ = select.select(
                                [] if rejected else [conn], [conn] if outgoing else [], [], .05)
                            if readable:
                                try:
                                    block = conn.recv(4096)
                                except BlockingIOError:
                                    block = None
                                if block == b'':
                                    break
                                if block:
                                    buffer += block
                            if not readable and not outgoing:
                                continue
                            while not rejected and b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                if len(line) > MAX_WIRE_BYTES:
                                    raise ValueError('intent too large')
                                packet = decode_intent(line)
                                if connection_session is None:
                                    connection_session = packet.session_id
                                elif connection_session != packet.session_id:
                                    raise ValueError('sender session changed inside one TCP connection')
                                packet_deadline = time.monotonic() + 1.
                                ack = session.process(packet)
                                now = time.monotonic_ns()
                                with self.lock:
                                    if ack.accepted:
                                        self.latest = (packet, now)
                                    else:
                                        self._pending_disconnect('invalid_operator_packet')
                                    status = self.status
                                reason = 'powered_hil:' + status + self.identity
                                if self.execution_mode is not None:
                                    reason += ';execution_mode=' + self.execution_mode
                                # Automatic policy/startup recovery uses the same
                                # sample-after-challenge proof as manual recovery.
                                # Without a challenge these states can never
                                # recover before their lease expires. A token is
                                # freshness evidence only, never motion authority.
                                if (status.startswith('input_wait') or status in
                                        ('startup_wait_operator', 'policy_wait_operator')):
                                    reason += ';recovery_token=' + self.recovery.issue(packet.session_id, now)
                                outgoing.extend(encode_packet(replace(ack, receiver_state='HOLD',
                                    reason=reason if ack.accepted else ack.reason)))
                                if not ack.accepted:
                                    rejected = 'operator packet rejected: ' + str(ack.reason)
                                if len(outgoing) > 256 * MAX_WIRE_BYTES:
                                    raise ValueError('sender exceeded bounded ACK backlog')
                            if len(buffer) > MAX_WIRE_BYTES:
                                raise ValueError('unterminated intent too large')
                            if outgoing and (writable or readable):
                                try:
                                    sent = conn.send(outgoing)
                                    if sent == 0:
                                        raise ConnectionError('intent ACK socket closed')
                                    del outgoing[:sent]
                                except BlockingIOError:
                                    pass
                            if rejected and not outgoing:
                                raise ValueError(rejected)
                except OSError as exc:
                    loss_reason = 'operator_transport_error:' + str(exc)
                except ValueError as exc:
                    # Malformed/replayed packets cannot acquire the neutral/RB
                    # recovery reserved for a healthy sender's transport loss.
                    loss_reason = 'invalid_operator_packet:' + str(exc)
                finally:
                    with self.lock:
                        self.latest = None
                        self._pending_disconnect(loss_reason)
        except OSError:
            if not self.stop_event.is_set():
                with self.lock:
                    self._pending_disconnect('operator_listener_failed')

    def _pending_disconnect(self, reason):
        # Caller holds lock. A main-loop stall may span multiple connections:
        # a pending EOF must not swallow a later protocol fault, nor may a
        # later EOF downgrade a protocol fault into transport recovery.
        if _disconnect_priority(reason) > _disconnect_priority(self.disconnected):
            self.disconnected = reason

    def snapshot(self, status):
        with self.lock:
            self.status = status
            latest, reason = self.latest, self.disconnected
            self.disconnected = None
            return latest, reason

    def close(self):
        self.stop_event.set()
        self.sock.close()
        self.thread.join(timeout=1)
