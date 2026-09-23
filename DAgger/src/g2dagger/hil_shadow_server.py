"""Shared loopback input server for synthetic and real-observation Shadow."""
import json
import select
import socket
import time
from .protocol import MAX_WIRE_BYTES, ProtocolError, decode_intent, encode_packet


def serve_shadow(engine, port, announce, *, stop=lambda: False):
    conn = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            announce(f"ZERO OUTPUT HIL listening 127.0.0.1:{port}")
            buffer = bytearray()
            while not stop():
                ready, _, _ = select.select([server] + ([conn] if conn else []), [], [], .01)
                if server in ready:
                    if conn:
                        conn.close()
                        engine.disconnect(time.monotonic_ns(), "connection_replaced")
                    conn, _ = server.accept()
                    conn.settimeout(.05)
                    buffer.clear()
                    # Tick on next pass, after an available first packet anchors the session.
                    continue
                if conn and conn in ready:
                    try:
                        data = conn.recv(4096)
                        if not data:
                            raise ConnectionError("operator_disconnected")
                        buffer.extend(data)
                        while b"\n" in buffer:
                            line, _, buffer = buffer.partition(b"\n")
                            if len(line) > MAX_WIRE_BYTES:
                                raise ProtocolError("oversize packet")
                            payload = json.loads(line)
                            if not isinstance(payload, dict) or not {"hil_toggle_id", "hil_button_pressed"} <= payload.keys():
                                raise ProtocolError("old sender: missing HIL fields; use isolated candidate")
                            ack = engine.receive(decode_intent(bytes(line)), time.monotonic_ns())
                            conn.sendall(encode_packet(ack))
                        if len(buffer) > MAX_WIRE_BYTES:
                            raise ProtocolError("unterminated oversize packet")
                    except (OSError, ValueError) as exc:
                        engine.disconnect(time.monotonic_ns(), str(exc))
                        conn.close()
                        conn = None
                        buffer.clear()
                engine.tick(time.monotonic_ns())
    finally:
        if conn:
            conn.close()
