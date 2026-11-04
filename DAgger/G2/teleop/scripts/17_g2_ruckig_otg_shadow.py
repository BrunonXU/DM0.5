#!/usr/bin/env python3
"""100 Hz PoweredArmTarget + Ruckig Shadow; reads G2 and has zero output API."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import socket
import threading
import time

import numpy as np

from g2teleop.control_telemetry import ControlTelemetry
from g2teleop.g2_live_state import parse_body_kinematics
from g2teleop.joint_otg import ruckig_version
from g2teleop.model_audit import sha256_file
from g2teleop.powered_teleop import PoweredArmTarget
from g2teleop.protocol import AckPacket, ProtocolError, decode_intent, encode_packet


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="100 Hz read-only Ruckig OTG Shadow")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7904)
    parser.add_argument("--packet-watchdog-ms", type=float, default=200.0)
    parser.add_argument(
        "--contract", type=Path, default=ROOT / "configs/g2_kinematics_model.json"
    )
    parser.add_argument("--otg-max-acceleration-rad-s2", type=float, default=2.0)
    parser.add_argument("--otg-max-jerk-rad-s3", type=float, default=20.0)
    parser.add_argument(
        "--otg-synchronization",
        choices=("phase", "time", "time_if_necessary", "none"),
        default="phase",
    )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/user/xza/DM0.5/logs/telemetry/shadow/"
            f"g2_shadow_f710_ruckig_{stamp}.jsonl"
        ),
    )
    return parser.parse_args()


class LatestIntent:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.packet = None
        self.received_at = 0.0
        self.fault: str | None = None
        self.stop = threading.Event()

    def update(self, packet) -> None:
        with self.lock:
            self.packet = packet
            self.received_at = time.monotonic()

    def snapshot(self):
        with self.lock:
            return self.packet, self.received_at, self.fault


def network_loop(shared: LatestIntent, bind: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((bind, port))
            server.listen(1)
            server.settimeout(0.5)
            print(f"RUCKIG SHADOW listening only on {bind}:{port}", flush=True)
            while not shared.stop.is_set():
                try:
                    conn, peer = server.accept()
                except socket.timeout:
                    continue
                print(f"Windows tunnel connected: {peer}", flush=True)
                try:
                    with conn:
                        conn.settimeout(0.2)
                        buffer = bytearray()
                        while not shared.stop.is_set():
                            try:
                                chunk = conn.recv(4096)
                            except socket.timeout:
                                continue
                            if not chunk:
                                break
                            buffer.extend(chunk)
                            while b"\n" in buffer:
                                raw, _, rest = buffer.partition(b"\n")
                                buffer = bytearray(rest)
                                packet = decode_intent(bytes(raw))
                                shared.update(packet)
                                ack = AckPacket(
                                    session_id=packet.session_id,
                                    sequence=packet.sequence,
                                    accepted=True,
                                    receiver_state=(
                                        "SHADOW_ACTIVE" if packet.deadman else "HOLD"
                                    ),
                                    reason=None,
                                )
                                conn.sendall(encode_packet(ack))
                except (OSError, ProtocolError, ValueError) as exc:
                    print(f"CLIENT ERROR -> SHADOW HOLD: {type(exc).__name__}: {exc}")
                finally:
                    print("Windows disconnected: SHADOW HOLD", flush=True)
    except Exception as exc:
        with shared.lock:
            shared.fault = f"network fault: {type(exc).__name__}: {exc}"
        shared.stop.set()


def main() -> int:
    cli = parse_args()
    if cli.bind != "127.0.0.1":
        raise RuntimeError("OTG Shadow may bind only to G2 loopback")
    if cli.otg_max_acceleration_rad_s2 <= 0.0 or cli.otg_max_jerk_rad_s3 <= 0.0:
        raise RuntimeError("OTG acceleration and jerk limits must be positive")
    version = ruckig_version()
    contract = json.loads(cli.contract.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    if sha256_file(model_path) != contract["sources"]["full_o10_mjcf"]["sha256"]:
        raise RuntimeError("vendor MJCF hash differs from frozen contract")

    # These imports expose measured state only in this file.  A source-level
    # regression test forbids every G2 motion-output API name here.
    import agibot_gdk as gdk
    import pinocchio as pin

    telemetry = ControlTelemetry(
        cli.telemetry_jsonl,
        run_id=cli.telemetry_jsonl.stem,
        mode="shadow-f710-otg",
    )
    shared = LatestIntent()
    initialized = False
    runtime_status = "fault"
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = gdk.Robot()
        full_model = pin.buildModelFromMJCF(str(model_path))
        arm_target = PoweredArmTarget(
            pin,
            full_model,
            contract["controlled_arm_joints"],
            enable_ruckig_otg=True,
            otg_max_acceleration_rad_s2=cli.otg_max_acceleration_rad_s2,
            otg_max_jerk_rad_s3=cli.otg_max_jerk_rad_s3,
            otg_ik_position_lag_limit_rad=0.03,
            otg_synchronization=cli.otg_synchronization,
        )
        initial = parse_body_kinematics(
            robot.get_joint_states(), sequence=0, sampled_at_monotonic=time.monotonic()
        )
        for side in ("left", "right"):
            arm_target.begin(side, initial)
            arm_target.release()

        telemetry.emit(
            "runtime_start",
            mode="shadow-f710-otg",
            profile="pink_singularity_guard_v1_ruckig_velocity_otg_v1",
            endpoint="windows-ssh-tunnel:7904",
            process_id=os.getpid(),
            control_hz=100.0,
            input_hz=50.0,
            hardware_output=False,
            command_scope="none_read_only_shadow",
            otg_enabled=True,
            otg_backend="ruckig",
            otg_version=version,
            otg_synchronization=cli.otg_synchronization,
            otg_max_velocity_rad_s=0.35,
            otg_max_acceleration_rad_s2=cli.otg_max_acceleration_rad_s2,
            otg_max_jerk_rad_s3=cli.otg_max_jerk_rad_s3,
        )
        print("READ-ONLY 100 Hz RUCKIG SHADOW: G2 state input, ZERO hardware output.")
        print(
            f"Ruckig {version}, sync={cli.otg_synchronization}, "
            f"a={cli.otg_max_acceleration_rad_s2:g}, "
            f"jerk={cli.otg_max_jerk_rad_s3:g}; log={cli.telemetry_jsonl}",
            flush=True,
        )
        thread = threading.Thread(
            target=network_loop, args=(shared, cli.bind, cli.port), daemon=True
        )
        thread.start()

        period = 0.01
        next_tick = time.monotonic()
        previous_enabled = False
        motion_fault_latched: str | None = None
        control_sequence = 0
        virtual_arms = initial.arms.copy()
        control_intervals_ms: deque[float] = deque(maxlen=500)
        last_control_at: float | None = None
        previous_arm_command: np.ndarray | None = None
        previous_arm_delta = np.zeros(14, dtype=np.float64)
        small_reversal_count = 0
        while not shared.stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick = now + period
            if last_control_at is not None:
                control_intervals_ms.append((now - last_control_at) * 1000.0)
            last_control_at = now
            packet, received_at, network_fault = shared.snapshot()
            if network_fault:
                raise RuntimeError(network_fault)
            if packet is None or packet.active_side is None:
                continue
            fresh = now - received_at <= cli.packet_watchdog_ms / 1000.0
            enabled = bool(
                packet.deadman and fresh and motion_fault_latched is None
            )
            if not packet.deadman:
                arm_target.release()
                motion_fault_latched = None
            if enabled and not previous_enabled:
                snapshot = parse_body_kinematics(
                    robot.get_joint_states(),
                    sequence=control_sequence + 1,
                    sampled_at_monotonic=time.monotonic(),
                )
                virtual_arms = snapshot.arms.copy()
                arm_target.begin(packet.active_side, snapshot)
                previous_arm_command = virtual_arms.copy()
                previous_arm_delta[:] = 0.0
                telemetry.emit(
                    "f710_control_event",
                    mode="shadow-f710-otg",
                    kind="takeover",
                    active_side=packet.active_side,
                    measured_arm_q_rad=snapshot.arms.tolist(),
                    hardware_output=False,
                )

            solved_this_tick = False
            saturated = False
            if enabled:
                try:
                    solved, saturated = arm_target.step(
                        mode=packet.mode,
                        axes=packet.axes,
                        angular_axes=packet.angular_axes,
                        speed_scale=packet.speed_scale,
                        dt=period,
                    )
                    if packet.active_side == "left":
                        virtual_arms[:7] = solved
                    else:
                        virtual_arms[7:] = solved
                    solved_this_tick = not saturated
                except Exception as exc:
                    motion_fault_latched = f"{type(exc).__name__}: {exc}"
                    enabled = False
                    telemetry.emit(
                        "f710_control_event",
                        mode="shadow-f710-otg",
                        kind="motion_hold",
                        active_side=packet.active_side,
                        error=motion_fault_latched,
                        hardware_output=False,
                    )
                    print(
                        f"SHADOW MOTION HOLD: {motion_fault_latched}; release RB to clear",
                        flush=True,
                    )
            if previous_arm_command is not None:
                arm_delta = virtual_arms - previous_arm_command
                reversal = (
                    (arm_delta * previous_arm_delta < 0.0)
                    & (np.abs(arm_delta) >= 1e-5)
                    & (np.abs(arm_delta) <= 0.003)
                    & (np.abs(previous_arm_delta) >= 1e-5)
                    & (np.abs(previous_arm_delta) <= 0.003)
                )
                small_reversal_count += int(np.count_nonzero(reversal))
                previous_arm_delta = arm_delta
                previous_arm_command = virtual_arms.copy()
            diagnostics = arm_target.diagnostics()
            telemetry.emit(
                "f710_control_sample",
                mode="shadow-f710-otg",
                control_sequence=control_sequence,
                control_monotonic_ns=int(now * 1e9),
                control_interval_seconds=(
                    None
                    if not control_intervals_ms
                    else control_intervals_ms[-1] / 1000.0
                ),
                packet_sequence=packet.sequence,
                session_id=packet.session_id,
                packet_age_ms=max(0.0, (now - received_at) * 1000.0),
                active_side=packet.active_side,
                deadman=packet.deadman,
                fresh=fresh,
                enabled=enabled,
                operator_mode=packet.mode,
                shaped_linear_axes=list(packet.axes),
                shaped_angular_axes=list(packet.angular_axes),
                speed_scale=packet.speed_scale,
                workspace_saturated=saturated,
                ik_solved_this_tick=solved_this_tick,
                **diagnostics,
                trajectory_stage="ruckig_velocity_otg_shadow",
                arm_command_rad=virtual_arms.tolist(),
                command_q_rad=(virtual_arms.tolist() + [0.0] * 20),
                hardware_output=False,
                clip_count=0,
                reversal_count_total=small_reversal_count,
            )
            control_sequence += 1
            previous_enabled = enabled
        runtime_status = "closed"
    except KeyboardInterrupt:
        telemetry.emit("runtime_stop", mode="shadow-f710-otg", reason="operator_ctrl_c")
        runtime_status = "closed"
        print("Ctrl-C: Ruckig Shadow HOLD", flush=True)
    finally:
        shared.stop.set()
        if initialized:
            gdk.gdk_release()
        telemetry.close(status=runtime_status)
        print(f"Ruckig Shadow telemetry finalized: {cli.telemetry_jsonl}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
