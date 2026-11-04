#!/usr/bin/env python3
"""Explicitly powered F710 receiver: arms via Servo, O10 via move_ee_pos."""

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

from g2teleop.g2_live_state import parse_body_kinematics
from g2teleop.global_home import GlobalHomePlan
from g2teleop.hand_synergy import (
    ChangedOnlyHandScheduler,
    DualHandCommandState,
    DualHandSynergy,
    HandScheduleDecision,
)
from g2teleop.joint_otg import OTGError, ruckig_version
from g2teleop.model_audit import sha256_file
from g2teleop.powered_teleop import PoweredArmTarget
from g2teleop.protocol import AckPacket, ProtocolError, decode_intent, encode_packet
from g2teleop.control_telemetry import ControlTelemetry, SCHEMA_VERSION
from g2teleop.control_timing import MinimumPeriodPacer


ROOT = Path(__file__).resolve().parents[1]
F710_HAND_HZ = 5.0


def finalize_feedback_sidecar(partial: Path, final: Path) -> bool:
    """Publish feedback only after at least one valid sample and a summary."""
    if not partial.exists():
        return False
    with partial.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        tail_start = max(0, size - 65536)
        handle.seek(tail_start)
        if tail_start:
            # The bounded tail normally starts in the middle of a JSONL row.
            # Discard that fragment instead of feeding it to json.loads().
            handle.readline()
        lines = [line for line in handle.read().splitlines() if line.strip()]
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    if not records:
        return False
    has_valid_sample = any(
        record.get("event") == "joint_feedback_sample"
        and record.get("status") == "ok"
        and isinstance(record.get("joints_rad"), list)
        and len(record["joints_rad"]) == 34
        for record in records[:-1]
    )
    last = records[-1]
    if last.get("event") != "joint_feedback_summary" or not has_valid_sample:
        return False
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    return True


def recover_stopped_main_telemetry(partial: Path, final: Path) -> bool:
    """Recover only a fully parsed run stopped before its final summary.

    This is intentionally stricter than normal finalization: every existing
    JSONL row must parse, the final event must be ``runtime_stop``, and a final
    destination must not already exist.  The synthetic summary is explicitly
    marked so a recovered incident trace cannot be mistaken for an ordinary
    clean shutdown.
    """
    if not partial.exists() or final.exists():
        return False
    last: dict[str, object] | None = None
    with partial.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            if not isinstance(record, dict):
                return False
            last = record
    if (
        last is None
        or last.get("telemetry_family") != "f710_teleop"
        or last.get("event") != "runtime_stop"
        or not isinstance(last.get("sequence"), int)
        or not isinstance(last.get("run_id"), str)
    ):
        return False
    summary = {
        "schema_version": SCHEMA_VERSION,
        "telemetry_family": "f710_teleop",
        "event": "runtime_summary",
        "run_id": last["run_id"],
        "sequence": int(last["sequence"]) + 1,
        "drop_count_before": int(last.get("drop_count_before", 0)),
        "wall_time": time.time(),
        "monotonic_ns": time.monotonic_ns(),
        "mode": "powered-f710",
        "status": "recovered_after_feedback_finalizer_error",
        "drop_count_total": int(last.get("drop_count_before", 0)),
        "writer_error": None,
        "recovered": True,
    }
    with partial.open("ab") as handle:
        handle.write(
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, final)
    return True


def exact_approved_home_anchor(record: dict, approved_home: dict) -> bool:
    """Permit a stale anchor only when it is exactly the reviewed 20D Home."""
    record_positions = np.asarray(record.get("positions"), dtype=np.float64)
    approved_positions = np.asarray(approved_home.get("positions"), dtype=np.float64)
    return bool(
        approved_home.get("approved", False)
        and record_positions.shape == (20,)
        and approved_positions.shape == (20,)
        and np.isfinite(record_positions).all()
        and np.isfinite(approved_positions).all()
        and np.array_equal(record_positions, approved_positions)
    )


def assess_zero_feedback_hand_anchor(
    record: dict,
    approved_home: dict | None,
    *,
    now_wall: float,
    max_age_s: float,
) -> dict[str, object]:
    """Classify an already schema-validated O10 fallback without hardware.

    A fresh acknowledged command may anchor zero feedback.  A stale command is
    accepted only when its entire 20D vector exactly equals the separately
    reviewed Home record.  This helper is shared by file preflight and powered
    startup so their safety decisions cannot drift apart.
    """
    if max_age_s <= 0.0:
        raise ValueError("max hand-command age must be positive")
    positions = np.asarray(record.get("positions"), dtype=np.float64)
    if positions.shape != (20,) or not np.isfinite(positions).all():
        raise ValueError("validated O10 fallback must contain finite 20D positions")
    record_wall = float(record["wall_time"])
    wall_clock_trustworthy = (
        now_wall >= 1_700_000_000.0 and record_wall <= now_wall + 300.0
    )
    if not wall_clock_trustworthy:
        return {
            "positions": positions.copy(),
            "kind": "validated_clock_untrusted",
            "age_label": "unavailable (G2 wall clock/PTP not trustworthy)",
        }
    age_s = max(0.0, now_wall - record_wall)
    if age_s <= max_age_s:
        return {
            "positions": positions.copy(),
            "kind": "fresh_last_success",
            "age_label": f"{age_s:.1f}s",
        }
    if approved_home is None or not exact_approved_home_anchor(record, approved_home):
        raise RuntimeError(
            "last successful hand command is stale and is not exactly the approved "
            f"Home: age={age_s:.1f}s limit={max_age_s:.1f}s"
        )
    return {
        "positions": positions.copy(),
        "kind": "stale_exact_approved_home",
        "age_label": (
            f"{age_s:.1f}s; accepted only because 20D exactly matches approved Home"
        ),
    }


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7904)
    parser.add_argument("--packet-watchdog-ms", type=float, default=200.0)
    parser.add_argument("--max-last-hand-command-age-s", type=float, default=21600.0)
    parser.add_argument("--confirm-powered-arms-and-hands", action="store_true")
    parser.add_argument(
        "--preflight-files-only",
        action="store_true",
        help="validate local files/dependencies only; never initialize GDK",
    )
    parser.add_argument("--accept-dm05-hand-fit", action="store_true")
    parser.add_argument("--contract", type=Path, default=ROOT / "configs/g2_kinematics_model.json")
    parser.add_argument("--hand-fit", type=Path, default=ROOT / "configs/hand_synergy_dm05_current_aid.json")
    parser.add_argument(
        "--last-hand-command",
        type=Path,
        default=Path("/home/user/xza/DM0.5/logs/runtime/last_hand_command.json"),
    )
    parser.add_argument(
        "--approved-hand-home",
        type=Path,
        default=Path("/home/user/xza/DM0.5/g2_sort/config/hand_home_approved.json"),
    )
    parser.add_argument(
        "--reset-pose-config",
        type=Path,
        default=Path("/home/user/xza/DM0.5/g2_sort/config/reset_poses.json"),
    )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/user/xza/DM0.5/logs/telemetry/powered/"
            f"g2_powered_f710_{stamp}.jsonl"
        ),
    )
    parser.add_argument(
        "--joint-feedback-probe-json",
        type=Path,
        default=Path(
            "/home/user/xza/DM0.5/logs/diagnostics/"
            "joint_feedback_probe_latest.json"
        ),
    )
    parser.add_argument("--disable-joint-feedback", action="store_true")
    parser.add_argument(
        "--diagnostic-profile",
        choices=("full", "timing-ab"),
        default="full",
        help=(
            "full keeps 100 Hz layered telemetry and validated feedback; "
            "timing-ab keeps compact 100 Hz dispatch records, samples full "
            "layers at 20 Hz, and disables synchronous joint feedback"
        ),
    )
    parser.add_argument("--enable-ruckig-otg", action="store_true")
    parser.add_argument("--otg-max-acceleration-rad-s2", type=float, default=2.0)
    parser.add_argument("--otg-max-jerk-rad-s3", type=float, default=20.0)
    parser.add_argument(
        "--otg-synchronization",
        choices=("phase", "time", "time_if_necessary", "none"),
        default="phase",
    )
    return parser.parse_args()


class LatestIntent:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.packet = None
        self.received_at = 0.0
        self.fault: str | None = None
        self.stop = threading.Event()
        self.packet_intervals_ms: deque[float] = deque(maxlen=500)
        self.packet_count = 0

    def update(self, packet) -> None:
        with self.lock:
            now = time.monotonic()
            if self.received_at > 0.0:
                self.packet_intervals_ms.append((now - self.received_at) * 1000.0)
            self.packet = packet
            self.received_at = now
            self.packet_count += 1

    def snapshot(self):
        with self.lock:
            return self.packet, self.received_at, self.fault

    def timing_snapshot(self):
        with self.lock:
            return self.packet_count, tuple(self.packet_intervals_ms)


def network_loop(shared: LatestIntent, bind: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((bind, port))
            server.listen(1)
            server.settimeout(0.5)
            print(f"POWERED receiver listening only on {bind}:{port}", flush=True)
            while not shared.stop.is_set():
                try:
                    conn, peer = server.accept()
                except socket.timeout:
                    continue
                print(
                    f"[{time.strftime('%H:%M:%S')}] Windows tunnel connected: {peer}",
                    flush=True,
                )
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
                                _, _, fault = shared.snapshot()
                                active = packet.deadman and fault is None
                                ack = AckPacket(
                                    session_id=packet.session_id,
                                    sequence=packet.sequence,
                                    accepted=fault is None,
                                    receiver_state="POWERED_ACTIVE" if active else "HOLD",
                                    reason=fault,
                                )
                                conn.sendall(encode_packet(ack))
                except (OSError, ProtocolError, ValueError) as exc:
                    # A client/tunnel failure is not a receiver failure. The
                    # control loop observes packet staleness and holds, while
                    # this server remains available for a fresh safe session.
                    print(
                        f"[{time.strftime('%H:%M:%S')}] CLIENT ERROR -> HOLD; "
                        f"receiver remains listening: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                finally:
                    print(
                        f"[{time.strftime('%H:%M:%S')}] Windows disconnected: "
                        "command watchdog is HOLD",
                        flush=True,
                    )
    except Exception as exc:
        with shared.lock:
            shared.fault = f"network fault: {type(exc).__name__}: {exc}"
        shared.stop.set()


def main() -> int:
    cli = args()
    if cli.bind != "127.0.0.1":
        raise RuntimeError("powered receiver may bind only to G2 loopback")
    if not cli.preflight_files_only and not cli.confirm_powered_arms_and_hands:
        raise RuntimeError("missing --confirm-powered-arms-and-hands")
    if cli.otg_max_acceleration_rad_s2 <= 0.0 or cli.otg_max_jerk_rad_s3 <= 0.0:
        raise RuntimeError("OTG acceleration and jerk limits must be positive")
    otg_version = None
    if cli.enable_ruckig_otg:
        try:
            otg_version = ruckig_version()
        except OTGError as exc:
            raise RuntimeError(
                f"OTG startup check failed before GDK initialization: {exc}"
            ) from exc
    synergy = DualHandSynergy(cli.hand_fit)
    if not synergy.powered_allowed and not cli.accept_dm05_hand_fit:
        raise RuntimeError("DM0.5 hand fit still requires explicit --accept-dm05-hand-fit")

    # Fail before GDK initialization if the old Shadow receiver still owns the port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.bind, cli.port))

    contract = json.loads(cli.contract.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    if sha256_file(model_path) != contract["sources"]["full_o10_mjcf"]["sha256"]:
        raise RuntimeError("vendor MJCF hash differs from frozen contract")

    # This module is pure file validation.  Importing it neither initializes
    # GDK nor communicates with the robot.
    from g2_client.hand_command_store import load_hand_command_record

    approved_home_record = load_hand_command_record(cli.approved_hand_home)
    if not bool(approved_home_record.get("approved", False)):
        raise RuntimeError("approved hand Home record is not marked approved")
    approved_home_hands = np.asarray(
        approved_home_record["positions"], dtype=np.float64
    )
    reset_pose_payload = json.loads(cli.reset_pose_config.read_text(encoding="utf-8"))
    approved_home_arms = np.asarray(
        reset_pose_payload["action_median"], dtype=np.float64
    )[:14]
    if (
        approved_home_hands.shape != (20,)
        or approved_home_arms.shape != (14,)
        or not np.all(np.isfinite(approved_home_hands))
        or not np.all(np.isfinite(approved_home_arms))
    ):
        raise RuntimeError("approved global Home targets must be finite 14D+20D")

    fallback_record: dict | None = None
    fallback_assessment: dict[str, object] | None = None
    fallback_error: str | None = None
    try:
        fallback_record = load_hand_command_record(cli.last_hand_command)
        approved_home: dict | None = None
        now_wall = time.time()
        record_age = max(0.0, now_wall - float(fallback_record["wall_time"]))
        if now_wall >= 1_700_000_000.0 and record_age > cli.max_last_hand_command_age_s:
            approved_home = approved_home_record
        fallback_assessment = assess_zero_feedback_hand_anchor(
            fallback_record,
            approved_home,
            now_wall=now_wall,
            max_age_s=cli.max_last_hand_command_age_s,
        )
    except Exception as exc:
        fallback_error = f"{type(exc).__name__}: {exc}"

    if fallback_assessment is not None:
        print(
            "O10 ZERO-FEEDBACK FALLBACK READY: "
            f"{fallback_assessment['kind']} "
            f"(age={fallback_assessment['age_label']})",
            flush=True,
        )
    else:
        print(
            "O10 ZERO-FEEDBACK FALLBACK UNAVAILABLE: "
            f"{fallback_error}. Powered startup may proceed only if live O10 "
            "feedback is finite and non-zero. Otherwise run: "
            "cd /home/user/xza/DM0.5 && bash scripts/03_reset_home.sh --execute",
            flush=True,
        )

    if cli.preflight_files_only:
        print("FILES-ONLY PREFLIGHT COMPLETE: GDK was not imported or initialized.")
        return 0

    print("POWERED SCOPE: left/right arm + left/right O10 only; NO chassis; waist read-only.")
    print(
        "Normal collision-protected Servo; O10 uses changed-only dual_tool "
        "move_ee_pos samples at 5 Hz."
    )
    print("Loading GDK now; missing-PTP messages are warnings, not motion commands.", flush=True)

    import agibot_gdk as gdk
    import pinocchio as pin
    from g2_client.joint_feedback import (
        JointFeedbackWriter,
        load_validated_feedback_hz,
    )
    from g2_client.joint_layout import POLICY_JOINT_NAMES, clip_action_positions
    from g2_client.robot_control import G2RobotController

    initialized = False
    controller = None
    shared = LatestIntent()
    run_id = cli.telemetry_jsonl.stem
    telemetry = ControlTelemetry(cli.telemetry_jsonl, run_id=run_id)
    feedback_writer = None
    runtime_status = "fault"
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        robot = gdk.Robot()
        timing_ab = cli.diagnostic_profile == "timing-ab"
        feedback_hz = (
            0.0
            if cli.disable_joint_feedback or timing_ab
            else load_validated_feedback_hz(cli.joint_feedback_probe_json)
        )
        telemetry_detail_hz = 20.0 if timing_ab else 100.0
        telemetry_detail_stride = int(round(100.0 / telemetry_detail_hz))
        feedback_path = (
            cli.telemetry_jsonl.parent.parent
            / "feedback"
            / f"{cli.telemetry_jsonl.stem}.jsonl"
        )
        feedback_partial_path = Path(f"{feedback_path}.partial")
        if feedback_hz > 0.0:
            feedback_writer = JointFeedbackWriter(
                feedback_partial_path,
                run_id=run_id,
            )
        controller = G2RobotController(
            powered=True,
            robot=robot,
            gdk_module=gdk,
            servo_channel="normal",
            # The F710 owner explicitly submits changed O10 targets after the
            # 100 Hz arm dispatch.  Do not create the old periodic Split hand
            # worker, which repeatedly refreshed move_ee_pos point targets.
            servo_output_mode="arms-only",
            control_period=0.01,
            last_hand_command_path=cli.last_hand_command,
            hand_command_run_id=run_id,
            joint_feedback_writer=feedback_writer,
            joint_feedback_hz=feedback_hz,
        )
        telemetry.emit(
            "runtime_start",
            mode="powered-f710",
            profile=(
                "pink_singularity_guard_v2_rolling_envelope_ruckig_velocity_otg_v1"
                if cli.enable_ruckig_otg
                else "pink_singularity_guard_v2_rolling_envelope_no_otg"
            ),
            endpoint="windows-ssh-tunnel:7904",
            process_id=os.getpid(),
            control_hz=100.0,
            input_hz=50.0,
            arm_hz=100.0,
            hand_stream_hz=F710_HAND_HZ,
            hand_execution="changed_5hz_move_ee_pos_once",
            hand_unchanged_resend=False,
            joint_feedback_hz=feedback_hz,
            diagnostic_profile=cli.diagnostic_profile,
            telemetry_dispatch_hz=100.0,
            telemetry_detail_hz=telemetry_detail_hz,
            pacer_phase="pre_dispatch",
            joint_layout=list(POLICY_JOINT_NAMES),
            command_scope="arms14_normal_servo_hands20_move_ee_pos",
            otg_enabled=cli.enable_ruckig_otg,
            otg_backend="ruckig" if cli.enable_ruckig_otg else None,
            otg_version=otg_version,
            otg_synchronization=(
                cli.otg_synchronization if cli.enable_ruckig_otg else None
            ),
            otg_max_velocity_rad_s=0.35 if cli.enable_ruckig_otg else None,
            otg_max_acceleration_rad_s2=(
                cli.otg_max_acceleration_rad_s2 if cli.enable_ruckig_otg else None
            ),
            otg_max_jerk_rad_s3=(
                cli.otg_max_jerk_rad_s3 if cli.enable_ruckig_otg else None
            ),
            otg_ik_position_lag_limit_rad=(0.03 if cli.enable_ruckig_otg else None),
        )
        print(
            f"Structured telemetry: {cli.telemetry_jsonl}; "
            f"profile={cli.diagnostic_profile}; full layers={telemetry_detail_hz:g} Hz; "
            f"dispatch timing=100 Hz; joint feedback={feedback_hz:g} Hz",
            flush=True,
        )
        measured = np.asarray(controller.read_state(), dtype=np.float64)
        initial_hands = measured[14:].copy()
        if np.max(np.abs(initial_hands)) < 0.05:
            if fallback_record is None or fallback_assessment is None:
                raise RuntimeError(
                    "O10 feedback is zero and no safe file fallback is available: "
                    f"{fallback_error}. Run: cd /home/user/xza/DM0.5 && "
                    "bash scripts/03_reset_home.sh --execute"
                )
            initial_hands = np.asarray(
                fallback_assessment["positions"], dtype=np.float64
            )
            print(
                "O10 feedback is zero; HOLD initialized from strictly validated "
                "last successful command "
                f"({fallback_record['source']}, "
                f"age={fallback_assessment['age_label']})",
                flush=True,
            )
        else:
            print("O10 HOLD initialized from finite non-zero feedback", flush=True)
        hands = DualHandCommandState(synergy, initial_hands)
        hand_scheduler = ChangedOnlyHandScheduler(
            hands,
            nominal_hz=F710_HAND_HZ,
        )
        body = parse_body_kinematics(
            robot.get_joint_states(), sequence=0, sampled_at_monotonic=time.monotonic()
        )
        action = measured.copy()
        action[:14] = body.arms
        action[14:] = initial_hands
        full_model = pin.buildModelFromMJCF(str(model_path))
        arm_target = PoweredArmTarget(
            pin,
            full_model,
            contract["controlled_arm_joints"],
            enable_ruckig_otg=cli.enable_ruckig_otg,
            otg_max_acceleration_rad_s2=cli.otg_max_acceleration_rad_s2,
            otg_max_jerk_rad_s3=cli.otg_max_jerk_rad_s3,
            otg_synchronization=cli.otg_synchronization,
        )
        if cli.enable_ruckig_otg:
            print(
                "OTG ENABLED: Ruckig "
                f"{otg_version}, velocity interface, sync={cli.otg_synchronization}, "
                f"a_max={cli.otg_max_acceleration_rad_s2:g} rad/s^2, "
                f"jerk_max={cli.otg_max_jerk_rad_s3:g} rad/s^3",
                flush=True,
            )
        else:
            print("OTG DISABLED: direct Pink reference baseline", flush=True)

        # The first reduced-model/Pink construction held the Python control
        # thread for 467 ms in the field.  Warm both sides before opening the
        # network listener; this performs computation only and sends nothing.
        for warm_side in ("left", "right"):
            warm_started = time.monotonic()
            arm_target.begin(warm_side, body)
            arm_target.release()
            print(
                f"IK WARMUP {warm_side}: "
                f"{(time.monotonic() - warm_started) * 1000.0:.1f} ms; no command sent",
                flush=True,
            )

        thread = threading.Thread(
            target=network_loop, args=(shared, cli.bind, cli.port), daemon=True
        )
        thread.start()
        period = 0.01
        pacer = MinimumPeriodPacer(period, spin_guard_seconds=0.0005)
        output_started = False
        previous_deadman = False
        previous_fresh = False
        motion_fault_latched: str | None = None
        workspace_saturated = False
        control_intervals_ms: deque[float] = deque(maxlen=500)
        ik_durations_ms: deque[float] = deque(maxlen=500)
        takeover_durations_ms: deque[float] = deque(maxlen=100)
        send_durations_ms: deque[float] = deque(maxlen=500)
        send_intervals_ms: deque[float] = deque(maxlen=500)
        pacer_lateness_ms: deque[float] = deque(maxlen=500)
        last_control_at: float | None = None
        last_send_started_at: float | None = None
        next_report = time.monotonic() + 5.0
        watchdog_count = 0
        arm_steps_rad: deque[float] = deque(maxlen=500)
        previous_arm_command: np.ndarray | None = None
        previous_arm_delta = np.zeros(14, dtype=np.float64)
        small_reversal_count = 0
        control_sequence = 0
        home_plan: GlobalHomePlan | None = None
        home_request_latched = False
        home_phase: str | None = None
        while not shared.stop.is_set():
            now = time.monotonic()
            if last_control_at is not None:
                control_intervals_ms.append((now - last_control_at) * 1000.0)
            last_control_at = now
            packet, received_at, network_fault = shared.snapshot()
            if network_fault:
                raise RuntimeError(network_fault)
            if packet is None:
                # No dispatch clock exists until an active side is selected.
                # Avoid spinning while the Windows sender is absent/unarmed.
                shared.stop.wait(0.002)
                continue
            fresh = now - received_at <= cli.packet_watchdog_ms / 1000.0
            if not packet.home_reset:
                home_request_latched = False
            if (
                packet.home_reset
                and fresh
                and not home_request_latched
                and home_plan is None
            ):
                home_request_latched = True
                home_body = parse_body_kinematics(
                    robot.get_joint_states(),
                    sequence=control_sequence,
                    sampled_at_monotonic=time.monotonic(),
                )
                action[:14] = home_body.arms
                home_plan = GlobalHomePlan(
                    home_body.arms,
                    approved_home_arms,
                    action[14:],
                    approved_home_hands,
                    started_at=now,
                )
                arm_target.release()
                motion_fault_latched = None
                workspace_saturated = False
                previous_deadman = False
                previous_arm_command = home_body.arms.copy()
                previous_arm_delta[:] = 0.0
                telemetry.emit(
                    "f710_control_event",
                    mode="powered-f710",
                    kind="global_home_requested",
                    packet_sequence=packet.sequence,
                    session_id=packet.session_id,
                )
                print(
                    "Y GLOBAL HOME: same owner; arms move to H5 Home over 5 s, "
                    "then hands move to approved Home at 5 Hz.",
                    flush=True,
                )
            if packet.active_side is None and home_plan is None:
                shared.stop.wait(0.002)
                continue
            enabled = bool(
                home_plan is None
                and packet.deadman
                and fresh
                and motion_fault_latched is None
            )
            if not packet.deadman:
                arm_target.release()
                motion_fault_latched = None
            if enabled and not previous_deadman and home_plan is None:
                takeover_started = time.monotonic()
                # One GDK joint-state read is sufficient.  The previous code
                # called controller.read_state() and robot.get_joint_states()
                # back-to-back, creating a measured ~316 ms takeover pause.
                body = parse_body_kinematics(
                    robot.get_joint_states(), sequence=1, sampled_at_monotonic=time.monotonic()
                )
                action[:14] = body.arms
                arm_target.begin(packet.active_side, body)
                previous_arm_command = action[:14].copy()
                previous_arm_delta[:] = 0.0
                takeover_ms = (time.monotonic() - takeover_started) * 1000.0
                takeover_durations_ms.append(takeover_ms)
                print(
                    f"TAKEOVER {packet.active_side}: powered target locked to measured state "
                    f"in {takeover_ms:.1f} ms",
                    flush=True,
                )
                telemetry.emit(
                    "f710_control_event",
                    mode="powered-f710",
                    kind="takeover",
                    active_side=packet.active_side,
                    packet_sequence=packet.sequence,
                    session_id=packet.session_id,
                    takeover_duration_ms=takeover_ms,
                    measured_arm_q_rad=body.arms.tolist(),
                )
            ik_solved = False
            ik_duration_ms: float | None = None
            arm_action_before_tick = action[:14].copy()
            if enabled:
                try:
                    ik_started = time.monotonic()
                    solved, saturated = arm_target.step(
                        mode=packet.mode,
                        axes=packet.axes,
                        angular_axes=packet.angular_axes,
                        speed_scale=packet.speed_scale,
                        dt=period,
                    )
                    if arm_target.target_envelope_recentered:
                        print(
                            "TARGET ENVELOPE RECENTERED: continuous RB motion; "
                            f"count={arm_target.target_envelope_recenter_count} "
                            f"reason={arm_target.target_envelope_recenter_reason}",
                            flush=True,
                        )
                        telemetry.emit(
                            "f710_control_event",
                            mode="powered-f710",
                            kind="target_envelope_recentered",
                            active_side=packet.active_side,
                            packet_sequence=packet.sequence,
                            session_id=packet.session_id,
                            reason=arm_target.target_envelope_recenter_reason,
                            recenter_count=arm_target.target_envelope_recenter_count,
                        )
                    ik_duration_ms = (time.monotonic() - ik_started) * 1000.0
                    ik_durations_ms.append(ik_duration_ms)
                    if saturated and not workspace_saturated:
                        print(
                            "WORKSPACE SATURATED: reverse the stick to move inward; "
                            "RB remains active",
                            flush=True,
                        )
                    workspace_saturated = saturated
                    ik_solved = not saturated
                    if packet.active_side == "left":
                        action[:7] = solved
                    else:
                        action[7:14] = solved
                except Exception as exc:
                    workspace_saturated = False
                    fault_stage = "OTG" if isinstance(exc, OTGError) else "IK"
                    motion_fault_latched = (
                        f"{fault_stage} HOLD: {type(exc).__name__}: {exc}"
                    )
                    enabled = False
                    print(motion_fault_latched + "; release RB to clear", flush=True)
                    telemetry.emit(
                        "f710_control_event",
                        mode="powered-f710",
                        kind=f"{fault_stage.lower()}_hold",
                        active_side=packet.active_side,
                        packet_sequence=packet.sequence,
                        session_id=packet.session_id,
                        error=motion_fault_latched,
                    )
            # Pace the actual GDK dispatch boundary, not the loop/IK start.
            # Historical timing-ab data showed corr(send interval, delta IK
            # duration)=0.902 when the pacer ran before IK.  Computing first
            # lets normal sub-period IK variation consume slack rather than
            # directly phase-modulating the outgoing 100 Hz command stream.
            pacer_sample = pacer.wait()
            send_started = pacer_sample.started_at
            pacer_lateness_ms.append(pacer_sample.lateness_seconds * 1000.0)

            # Freshness is a send-time property.  If the packet crossed the
            # watchdog while this tick was computed/waiting, discard the newly
            # solved arm step and force a measured-state takeover after input
            # recovers.  Never send one last stale motion step.
            dispatch_fresh = (
                send_started - received_at <= cli.packet_watchdog_ms / 1000.0
            )
            if previous_fresh and not dispatch_fresh and (
                packet.deadman or packet.grip_target is not None
            ):
                watchdog_count += 1
                telemetry.emit(
                    "f710_control_event",
                    mode="powered-f710",
                    kind="watchdog_hold",
                    packet_sequence=packet.sequence,
                    session_id=packet.session_id,
                    packet_age_ms=(send_started - received_at) * 1000.0,
                )
                print(
                    "WATCHDOG HOLD: packet age="
                    f"{(send_started - received_at) * 1000.0:.1f} ms; "
                    "automatic measured-state takeover when fresh input recovers; "
                    "release RB if the sender remains unavailable",
                    flush=True,
                )
            if enabled and not dispatch_fresh:
                enabled = False
                ik_solved = False
                workspace_saturated = False
                action[:14] = arm_action_before_tick
                arm_target.release()

            home_done_this_tick = False
            if home_plan is not None:
                home_step = home_plan.step(send_started)
                action[:14] = home_step.arms
                action[14:] = home_step.hands
                home_phase = home_step.phase
                hand_enabled = True
                hand_decision = HandScheduleDecision(
                    command=home_step.hands,
                    should_send=home_step.send_hands,
                    reason=f"global_home_{home_step.phase}",
                    intent_id=hand_scheduler.intent_id + 1,
                    intent_changed=home_step.phase == "hand_anchor",
                    sampled=home_step.send_hands,
                )
                home_done_this_tick = home_step.done
            else:
                home_phase = None
                hand_enabled = bool(
                    dispatch_fresh
                    and packet.active_side in {"left", "right"}
                    and (
                        packet.grip_target is not None
                        or (
                            enabled
                            and (
                                abs(packet.grip_velocity) > 0.0
                                or packet.thumb_clearance
                            )
                        )
                    )
                )
                hand_decision = hand_scheduler.step(
                    now=send_started,
                    side=packet.active_side,
                    enabled=hand_enabled,
                    grip_velocity=packet.grip_velocity if enabled else 0.0,
                    grip_target=packet.grip_target if hand_enabled else None,
                    thumb_clearance=packet.thumb_clearance if enabled else False,
                )
                action[14:] = hand_decision.command
            if previous_arm_command is not None:
                arm_delta = action[:14] - previous_arm_command
                arm_steps_rad.append(float(np.max(np.abs(arm_delta))))
                reversal = (
                    (arm_delta * previous_arm_delta < 0.0)
                    & (np.abs(arm_delta) >= 1e-5)
                    & (np.abs(arm_delta) <= 0.003)
                    & (np.abs(previous_arm_delta) >= 1e-5)
                    & (np.abs(previous_arm_delta) <= 0.003)
                )
                small_reversal_count += int(np.count_nonzero(reversal))
                previous_arm_delta = arm_delta
                previous_arm_command = action[:14].copy()
            dispatched, clip_count, max_clip_delta = clip_action_positions(action)
            clip_indices = np.flatnonzero(np.abs(dispatched - action) > 0.0)
            send_interval_seconds = (
                None
                if last_send_started_at is None
                else send_started - last_send_started_at
            )
            if send_interval_seconds is not None:
                send_intervals_ms.append(send_interval_seconds * 1000.0)
            last_send_started_at = send_started
            # Send the exact position vector recorded as command_q_rad.  The
            # controller retains its own safety validation as defense in depth.
            controller.send_action(dispatched)
            send_completed = time.monotonic()
            send_duration_ms = (send_completed - send_started) * 1000.0
            send_durations_ms.append(send_duration_ms)
            hand_send_duration_ms = None
            if hand_decision.should_send:
                hand_send_started = time.monotonic()
                controller.send_hands_once(
                    dispatched[14:],
                    source=(
                        "f710_global_home_changed_5hz_move_ee_pos_once"
                        if home_plan is not None
                        else "f710_intent_changed_5hz_move_ee_pos_once"
                    ),
                )
                hand_send_completed = time.monotonic()
                hand_send_duration_ms = (
                    hand_send_completed - hand_send_started
                ) * 1000.0
                telemetry.emit(
                    "f710_hand_command",
                    mode="powered-f710",
                    control_sequence=control_sequence,
                    hand_intent_id=hand_decision.intent_id,
                    hand_intent_changed=hand_decision.intent_changed,
                    hand_schedule_reason=hand_decision.reason,
                    active_side=packet.active_side,
                    grip_target=packet.grip_target,
                    thumb_clearance=packet.thumb_clearance,
                    command_rad=dispatched[14:].tolist(),
                    send_started_monotonic_ns=int(hand_send_started * 1e9),
                    send_completed_monotonic_ns=int(hand_send_completed * 1e9),
                    send_duration_ms=hand_send_duration_ms,
                    source=(
                        "f710_global_home_changed_5hz_move_ee_pos_once"
                        if home_plan is not None
                        else "f710_intent_changed_5hz_move_ee_pos_once"
                    ),
                )
            if home_done_this_tick:
                telemetry.emit(
                    "f710_control_event",
                    mode="powered-f710",
                    kind="global_home_completed",
                    packet_sequence=packet.sequence,
                    session_id=packet.session_id,
                    arm_home_rad=approved_home_arms.tolist(),
                    hand_home_rad=approved_home_hands.tolist(),
                )
                print(
                    "Y GLOBAL HOME complete: select a side again, then press RB; "
                    "speed is LOW and old RT targets are cleared.",
                    flush=True,
                )
                hands = DualHandCommandState(synergy, approved_home_hands)
                hand_scheduler = ChangedOnlyHandScheduler(
                    hands, nominal_hz=F710_HAND_HZ
                )
                home_plan = None
            detailed_sample = control_sequence % telemetry_detail_stride == 0
            common_telemetry = dict(
                mode="powered-f710",
                control_sequence=control_sequence,
                control_monotonic_ns=int(now * 1e9),
                control_interval_seconds=(
                    None
                    if not control_intervals_ms
                    else control_intervals_ms[-1] / 1000.0
                ),
                control_deadline_monotonic_ns=int(pacer_sample.deadline * 1e9),
                control_lateness_ms=pacer_sample.lateness_seconds * 1000.0,
                dispatch_deadline_monotonic_ns=int(pacer_sample.deadline * 1e9),
                dispatch_lateness_ms=pacer_sample.lateness_seconds * 1000.0,
                pacer_phase="pre_dispatch",
                pacer_sleep_ms=pacer_sample.slept_seconds * 1000.0,
                pacer_spin_ms=pacer_sample.spun_seconds * 1000.0,
                ik_duration_ms=ik_duration_ms,
                send_started_monotonic_ns=int(send_started * 1e9),
                send_completed_monotonic_ns=int(send_completed * 1e9),
                send_interval_seconds=send_interval_seconds,
                packet_sequence=packet.sequence,
                session_id=packet.session_id,
                packet_sent_at_unix_ns=packet.sent_at_unix_ns,
                packet_age_ms=max(0.0, (send_started - received_at) * 1000.0),
                active_side=packet.active_side,
                deadman=packet.deadman,
                fresh=dispatch_fresh,
                enabled=enabled,
                hand_enabled=hand_enabled,
                operator_mode=packet.mode,
                shaped_linear_axes=list(packet.axes),
                shaped_angular_axes=list(packet.angular_axes),
                grip_velocity=packet.grip_velocity,
                grip_target=packet.grip_target,
                thumb_clearance=packet.thumb_clearance,
                speed_scale=packet.speed_scale,
                home_reset=packet.home_reset,
                hil_toggle_id=packet.hil_toggle_id,
                hil_button_pressed=packet.hil_button_pressed,
                hil_state="standalone_human",
                control_source=(
                    "home"
                    if home_plan is not None
                    else "human_arm"
                    if enabled
                    else "human_hand"
                    if hand_enabled
                    else "hold"
                ),
                hil_latched=False,
                intervention_id=None,
                policy_epoch=None,
                policy_request_id=None,
                policy_chunk_valid=None,
                policy_action_index=None,
                discarded_policy_suffix_steps=None,
                human_arm_active=enabled,
                human_hand_active=hand_enabled,
                intervention_mask=False,
                human_arm_mask=enabled,
                human_hand_mask=hand_enabled,
                home_phase=home_phase,
                hand_intent_id=hand_decision.intent_id,
                hand_intent_changed=hand_decision.intent_changed,
                hand_scheduler_sampled=hand_decision.sampled,
                hand_should_send=hand_decision.should_send,
                hand_schedule_reason=hand_decision.reason,
                hand_send_duration_ms=hand_send_duration_ms,
                workspace_saturated=workspace_saturated,
                ik_solved_this_tick=ik_solved,
                diagnostic_profile=cli.diagnostic_profile,
                trajectory_stage=(
                    "ruckig_velocity_otg" if cli.enable_ruckig_otg else "direct_ik_reference"
                ),
                arm_command_rad=dispatched[:14].tolist(),
                clip_count=int(clip_count),
                max_clip_delta_rad=float(max_clip_delta),
                clip_joint_indices=clip_indices.tolist(),
                clip_joint_names=[POLICY_JOINT_NAMES[index] for index in clip_indices],
                reversal_count_total=small_reversal_count,
                gdk_send_duration_ms=send_duration_ms,
                telemetry_drop_count=telemetry.dropped,
            )
            if detailed_sample:
                telemetry.emit(
                    "f710_control_sample",
                    **common_telemetry,
                    **arm_target.diagnostics(),
                    hand_command_rad=dispatched[14:].tolist(),
                    command_q_rad=dispatched.tolist(),
                )
            else:
                telemetry.emit("f710_dispatch_sample", **common_telemetry)
            control_sequence += 1
            if not output_started:
                output_started = True
                print(
                    "HOLD active: arms 100 Hz; O10 changed-only at 5 Hz "
                    "while a hand intent is progressing",
                    flush=True,
                )
            previous_deadman = enabled
            previous_fresh = dispatch_fresh
            if now >= next_report:
                packet_count, packet_intervals = shared.timing_snapshot()
                packet_ms = np.asarray(packet_intervals, dtype=np.float64)
                control_ms = np.asarray(control_intervals_ms, dtype=np.float64)
                ik_ms = np.asarray(ik_durations_ms, dtype=np.float64)
                takeover_ms = np.asarray(takeover_durations_ms, dtype=np.float64)
                arm_step = np.asarray(arm_steps_rad, dtype=np.float64)
                send_ms = np.asarray(send_durations_ms, dtype=np.float64)
                send_interval_ms = np.asarray(send_intervals_ms, dtype=np.float64)
                lateness_ms = np.asarray(pacer_lateness_ms, dtype=np.float64)

                def summary(values: np.ndarray) -> str:
                    if not len(values):
                        return "n/a"
                    return (
                        f"p50={np.percentile(values, 50):.1f} "
                        f"p95={np.percentile(values, 95):.1f} "
                        f"p99={np.percentile(values, 99):.1f} "
                        f"max={np.max(values):.1f}"
                    )

                packet_age_ms = max(0.0, (now - received_at) * 1000.0)
                print(
                    f"TIMING packet_interval_ms[{summary(packet_ms)}] "
                    f"control_interval_ms[{summary(control_ms)}] "
                    f"takeover_ms[{summary(takeover_ms)}] "
                    f"ik_ms[{summary(ik_ms)}] "
                    f"arm_step_rad[{summary(arm_step)}] "
                    f"small_reversals={small_reversal_count} "
                    f"gdk_send_ms[{summary(send_ms)}] "
                    f"dispatch_interval_ms[{summary(send_interval_ms)}] "
                    f"pacer_lateness_ms[{summary(lateness_ms)}] "
                    f"packet_age={packet_age_ms:.1f} "
                    f"packets={packet_count} watchdogs={watchdog_count}",
                    flush=True,
                )
                next_report = now + 5.0
        _, _, terminal_network_fault = shared.snapshot()
        if terminal_network_fault:
            raise RuntimeError(terminal_network_fault)
        runtime_status = "closed"
    except KeyboardInterrupt:
        telemetry.emit("runtime_stop", mode="powered-f710", reason="operator_ctrl_c")
        runtime_status = "closed"
        print("Ctrl-C: closing powered command owner", flush=True)
    except Exception as exc:
        telemetry.emit(
            "runtime_fault",
            mode="powered-f710",
            error=f"{type(exc).__name__}: {exc}",
        )
        if controller is not None:
            controller.emergency_stop()
        raise
    finally:
        shared.stop.set()
        if controller is not None:
            controller.close()
        elif feedback_writer is not None:
            feedback_writer.close()
        feedback_message: str | None = None
        if feedback_writer is not None:
            try:
                if finalize_feedback_sidecar(feedback_partial_path, feedback_path):
                    feedback_message = f"Feedback telemetry finalized: {feedback_path}"
                elif feedback_partial_path.exists() and feedback_partial_path.stat().st_size < 4096:
                    feedback_partial_path.unlink()
                    feedback_message = (
                        "Feedback telemetry had no valid sample; empty sidecar discarded."
                    )
                else:
                    feedback_message = (
                        f"Feedback telemetry remains partial and will not upload: "
                        f"{feedback_partial_path}"
                    )
            except Exception as exc:
                # Feedback observability must never prevent the independently
                # valid main control telemetry from being atomically published.
                feedback_message = (
                    "Feedback telemetry finalization failed; main telemetry "
                    f"will still finalize: {type(exc).__name__}: {exc}"
                )
        if initialized:
            gdk.gdk_release()
        telemetry_finalized = telemetry.close(status=runtime_status)
        if feedback_message is not None:
            print(feedback_message, flush=True)
        if telemetry_finalized:
            print(f"Structured telemetry finalized: {cli.telemetry_jsonl}", flush=True)
        else:
            print(
                f"Structured telemetry remains partial and will not upload: "
                f"{telemetry.partial_path} ({telemetry.writer_error})",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
