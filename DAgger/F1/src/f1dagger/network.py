"""Loopback-only F710 intent server; no robot calls on the network thread."""
from dataclasses import replace
import socket
import threading
import time
from dagger_core.protocol import decode_intent, encode_packet, MAX_WIRE_BYTES
from dagger_core.shadow import ShadowSession


class IntentServer:
    def __init__(self, port, *, execution_mode=None, identity=""):
        if execution_mode not in (None, 'shadow', 'powered'):
            raise ValueError('execution_mode must be shadow or powered')
        self.execution_mode = execution_mode
        self.identity = identity
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
                buffer = b''
                try:
                    with conn:
                        conn.settimeout(.1)
                        # SSH can leave a connected channel without any data.
                        # Polling recv alone never releases it for a reconnect.
                        # This cleanup is separate from the 200 ms motion watchdog.
                        packet_deadline = time.monotonic() + 1.
                        while not self.stop_event.is_set():
                            if time.monotonic() >= packet_deadline:
                                raise TimeoutError('intent packet timeout')
                            try:
                                block = conn.recv(4096)
                            except socket.timeout:
                                continue
                            if not block:
                                break
                            buffer += block
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                if len(line) > MAX_WIRE_BYTES:
                                    raise ValueError('intent too large')
                                packet = decode_intent(line)
                                packet_deadline = time.monotonic() + 1.
                                ack = session.process(packet)
                                now = time.monotonic_ns()
                                with self.lock:
                                    if ack.accepted:
                                        self.latest = (packet, now)
                                    else:
                                        self.disconnected = 'invalid_operator_packet'
                                    status = self.status
                                reason = 'powered_hil:' + status + self.identity
                                if self.execution_mode is not None:
                                    reason += ';execution_mode=' + self.execution_mode
                                conn.sendall(encode_packet(replace(ack, receiver_state='HOLD',
                                    reason=reason if ack.accepted else ack.reason)))
                            if len(buffer) > MAX_WIRE_BYTES:
                                raise ValueError('unterminated intent too large')
                except (OSError, ValueError) as exc:
                    with self.lock:
                        self.disconnected = 'operator_network:' + str(exc)
                finally:
                    with self.lock:
                        self.latest = None
                        self.disconnected = self.disconnected or 'operator_disconnected'
        except OSError:
            if not self.stop_event.is_set():
                with self.lock:
                    self.disconnected = 'operator_listener_failed'

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
