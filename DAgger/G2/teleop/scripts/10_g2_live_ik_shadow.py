#!/usr/bin/env python3
"""G2-local measured-state + F710 Live IK Shadow with zero hardware output."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import socket
import time
from typing import Any

from g2teleop.g2_live_state import parse_body_kinematics
from g2teleop.live_ik_shadow import LiveIKShadowEngine
from g2teleop.model_audit import sha256_file
from g2teleop.protocol import (
    AckPacket,
    MAX_WIRE_BYTES,
    ProtocolError,
    decode_intent,
    encode_packet,
)
from g2teleop.shadow import ShadowSession


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = (ROOT.parents[1] / "artifacts/logs/g2_teleop"
           if ROOT.parent.name == "G2" else ROOT / "logs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="G2 read-only Live Pink IK Shadow")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7904)
    parser.add_argument("--watchdog-ms", type=float, default=100.0)
    parser.add_argument("--max-state-read-ms", type=float, default=30.0)
    parser.add_argument("--stabilization-seconds", type=float, default=2.0)
    parser.add_argument(
        "--contract", type=Path, default=ROOT / "configs/g2_kinematics_model.json"
    )
    parser.add_argument("--log", type=Path, default=LOG_DIR / "live_ik_shadow.jsonl")
    parser.add_argument("--pid-file", type=Path, default=(LOG_DIR if ROOT.parent.name == "G2" else ROOT) / "live_ik_shadow.pid")
    return parser.parse_args()


def emit(handle: Any, event: str, **fields: object) -> None:
    record = {
        "event": event,
        "unix_ns": time.time_ns(),
        "monotonic": time.monotonic(),
        **fields,
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    print(line, flush=True)
    handle.write(line + "\n")
    handle.flush()


def acquire_pid_file(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another Live IK Shadow owns {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


class StateReader:
    def __init__(self, robot: Any, max_read_s: float) -> None:
        self.robot = robot
        self.max_read_s = max_read_s
        self.sequence = 0

    def read(self):
        started = time.monotonic()
        payload = self.robot.get_joint_states()
        finished = time.monotonic()
        elapsed = finished - started
        if elapsed > self.max_read_s:
            raise RuntimeError(
                f"get_joint_states exceeded {self.max_read_s * 1000:.1f}ms: "
                f"{elapsed * 1000:.3f}ms"
            )
        snapshot = parse_body_kinematics(
            payload,
            sequence=self.sequence,
            sampled_at_monotonic=finished,
        )
        self.sequence += 1
        return snapshot, elapsed


def serve_client(
    conn: socket.socket,
    peer: object,
    *,
    watchdog_s: float,
    state_reader: StateReader,
    engine: LiveIKShadowEngine,
    log_handle: Any,
) -> None:
    session = ShadowSession()
    engine.reset("client_connected")
    buffer = bytearray()
    conn.settimeout(watchdog_s)
    emit(log_handle, "client_connected", peer=str(peer), state="HOLD")
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            session.watchdog_timeout()
            engine.reset("network_watchdog")
            emit(log_handle, "watchdog_timeout", state="HOLD")
            continue
        if not chunk:
            session.watchdog_timeout()
            engine.reset("client_disconnected")
            emit(log_handle, "client_disconnected", state="HOLD")
            return
        buffer.extend(chunk)
        if len(buffer) > MAX_WIRE_BYTES and b"\n" not in buffer:
            raise ProtocolError("unterminated oversize packet")
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            packet = decode_intent(bytes(raw_line))
            transport_ack = session.process(packet)
            result = None
            state_read_s = None
            if not transport_ack.accepted:
                engine.reset(transport_ack.reason or "transport_rejected")
                ack = transport_ack
            elif not packet.deadman:
                engine.reset("deadman_released")
                ack = transport_ack
            else:
                try:
                    snapshot, state_read_s = state_reader.read()
                    result = engine.process(packet, snapshot, now=time.monotonic())
                    accepted = result.state == "SHADOW_ACTIVE"
                    ack = AckPacket(
                        session_id=packet.session_id,
                        sequence=packet.sequence,
                        accepted=accepted,
                        receiver_state=result.state,
                        reason=result.reason,
                    )
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    engine.reset(reason)
                    ack = AckPacket(
                        session_id=packet.session_id,
                        sequence=packet.sequence,
                        accepted=False,
                        receiver_state="HOLD",
                        reason=reason,
                    )
            emit(
                log_handle,
                "intent",
                packet=asdict(packet),
                accepted=ack.accepted,
                receiver_state=ack.receiver_state,
                reason=ack.reason,
                state_read_ms=(None if state_read_s is None else state_read_s * 1000.0),
                candidate_arms14=(
                    None
                    if result is None or result.candidate_arms14 is None
                    else result.candidate_arms14.tolist()
                ),
                position_error_m=(None if result is None else result.position_error_m),
                orientation_error_deg=(
                    None if result is None else result.orientation_error_deg
                ),
            )
            conn.sendall(encode_packet(ack))


def main() -> int:
    args = parse_args()
    if args.bind != "127.0.0.1":
        raise ValueError("Live Shadow must bind only to 127.0.0.1")
    if not 20.0 <= args.watchdog_ms <= 1000.0:
        raise ValueError("watchdog must be in [20,1000] ms")
    if not 1.0 <= args.max_state_read_ms <= 1000.0:
        raise ValueError("max state read must be in [1,1000] ms")

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    expected_hash = contract["sources"]["full_o10_mjcf"]["sha256"]
    if sha256_file(model_path) != expected_hash:
        raise RuntimeError("vendor MJCF hash differs from frozen contract")

    import agibot_gdk as gdk
    import pinocchio as pin

    pid_handle = acquire_pid_file(args.pid_file)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    gdk_initialized = False
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        gdk_initialized = True
        robot = gdk.Robot()
        if args.stabilization_seconds > 0:
            time.sleep(args.stabilization_seconds)
        state_reader = StateReader(robot, args.max_state_read_ms / 1000.0)
        preflight = []
        for _ in range(3):
            snapshot, elapsed = state_reader.read()
            preflight.append(
                {
                    "sequence": snapshot.sequence,
                    "state_read_ms": elapsed * 1000.0,
                    "waist": snapshot.waist.tolist(),
                    "arms": snapshot.arms.tolist(),
                }
            )
            time.sleep(0.05)
        full_model = pin.buildModelFromMJCF(str(model_path))
        engine = LiveIKShadowEngine(
            pin,
            full_model,
            contract["controlled_arm_joints"],
        )

        print("READ-ONLY LIVE IK SHADOW: real G2 state, virtual IK, ZERO hardware output.")
        print("This file contains no arm/hand command path. Ctrl-C returns HOLD and releases GDK.")
        with args.log.open("a", encoding="utf-8", buffering=1) as log_handle:
            emit(
                log_handle,
                "startup_preflight_pass",
                pid=os.getpid(),
                model_sha256=expected_hash,
                preflight=preflight,
                powered_allowed=False,
                hardware_output=False,
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((args.bind, args.port))
                server.listen(1)
                print(f"Listening on {args.bind}:{args.port}; log={args.log}")
                while True:
                    conn, peer = server.accept()
                    with conn:
                        try:
                            serve_client(
                                conn,
                                peer,
                                watchdog_s=args.watchdog_ms / 1000.0,
                                state_reader=state_reader,
                                engine=engine,
                                log_handle=log_handle,
                            )
                        except (ConnectionError, OSError, ProtocolError) as exc:
                            engine.reset(f"connection_error: {exc}")
                            emit(
                                log_handle,
                                "connection_error",
                                reason=str(exc),
                                state="HOLD",
                            )
    except KeyboardInterrupt:
        print("Stopped by operator; Live IK Shadow is HOLD.")
    finally:
        if gdk_initialized:
            gdk.gdk_release()
        pid_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
