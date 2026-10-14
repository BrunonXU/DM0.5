#!/usr/bin/env python3
"""Loopback-only network receiver. Deliberately contains no GDK imports/calls."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import socket
import time

from g2teleop.protocol import MAX_WIRE_BYTES, ProtocolError, decode_intent, encode_packet
from g2teleop.shadow import ShadowSession


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = (ROOT.parents[1] / "artifacts/logs/g2_teleop"
           if ROOT.parent.name == "G2" else ROOT / "logs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="G2 loopback-only, no-GDK Shadow receiver")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7904)
    parser.add_argument("--watchdog-ms", type=float, default=100.0)
    parser.add_argument("--log", type=Path, default=LOG_DIR / "g2_shadow_intents.jsonl")
    return parser.parse_args()


def emit(log_handle: object, event: str, **fields: object) -> None:
    record = {"receiver_unix_ns": time.time_ns(), "event": event, **fields}
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    print(line, flush=True)
    log_handle.write(line + "\n")
    log_handle.flush()


def serve_client(conn: socket.socket, peer: object, watchdog_s: float, log_handle: object) -> None:
    session = ShadowSession()
    buffer = bytearray()
    conn.settimeout(watchdog_s)
    emit(log_handle, "client_connected", peer=str(peer), state="HOLD")
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            if session.state != "HOLD" or not session.require_disabled_packet:
                session.watchdog_timeout()
                emit(log_handle, "watchdog_timeout", state="HOLD")
            continue
        if not chunk:
            session.watchdog_timeout()
            emit(log_handle, "client_disconnected", state="HOLD")
            return
        buffer.extend(chunk)
        if len(buffer) > MAX_WIRE_BYTES and b"\n" not in buffer:
            emit(log_handle, "protocol_error", reason="unterminated_oversize_packet", state="HOLD")
            return
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            try:
                packet = decode_intent(bytes(raw_line))
                ack = session.process(packet)
                emit(
                    log_handle, "intent", packet=asdict(packet), accepted=ack.accepted,
                    receiver_state=ack.receiver_state, reason=ack.reason,
                )
                conn.sendall(encode_packet(ack))
            except ProtocolError as exc:
                session.watchdog_timeout()
                emit(log_handle, "protocol_error", reason=str(exc), state="HOLD")
                return


def main() -> int:
    args = parse_args()
    if args.bind != "127.0.0.1":
        raise ValueError("Shadow safety policy requires --bind 127.0.0.1")
    if not 20.0 <= args.watchdog_ms <= 1000.0:
        raise ValueError("--watchdog-ms must be in [20, 1000]")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    print("NO-GDK SHADOW: validates/logs intent only; cannot command G2 hardware.")
    try:
        with open(args.log, "a", encoding="utf-8", buffering=1) as log_handle:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((args.bind, args.port))
                server.listen(1)
                print(f"Listening only on {args.bind}:{args.port}; log={args.log}")
                while True:
                    conn, peer = server.accept()
                    with conn:
                        try:
                            serve_client(conn, peer, args.watchdog_ms / 1000.0, log_handle)
                        except (ConnectionError, OSError) as exc:
                            emit(log_handle, "connection_error", reason=str(exc), state="HOLD")
    except KeyboardInterrupt:
        print("Stopped by operator; receiver is no longer listening.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
