#!/usr/bin/env python3
"""Offline-only BC->EEF->Pink->Ruckig causal command artifact generator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import numpy as np

from g2teleop.control_telemetry import ControlTelemetry
from g2teleop.eef_replay import (
    ReplayTimeline,
    dual_eef_fk,
    ideal_timed_twist,
    load_frozen_replay,
    normalized_twist_axes,
    sample_timed_vector,
    velocity_limited_timeline,
)
from g2teleop.g2_live_state import KinematicSnapshot
from g2teleop.joint_otg import ruckig_version
from g2teleop.model_audit import sha256_file
from g2teleop.powered_teleop import PoweredArmTarget
from g2teleop.pink_ik import IKSafety


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY = Path("/home/agi/xza/DM0.5/g2_sort/config/training_replay_v1.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-files-only", action="store_true")
    parser.add_argument("--replay-json", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument(
        "--path-source",
        choices=("states", "actions"),
        default="states",
        help="states replays the measured demonstration path; actions replays the recorded command path",
    )
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
    parser.add_argument(
        "--timeline",
        choices=("source", "velocity-limited"),
        default="source",
        help="source preserves 30 Hz timing; velocity-limited preserves the path without input clipping",
    )
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--offline-artifact-npz",
        type=Path,
        default=Path(
            "/home/agi/xza/DM0.5/logs/diagnostics/"
            f"g2_bc_eef_causal_commands_{stamp}.npz"
        ),
        help="offline-only frozen Q-direct/Q-pink/Q-otg artifact",
    )
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=Path(
            "/home/agi/xza/DM0.5/logs/telemetry/shadow/"
            f"g2_bc_eef_causal_commands_{stamp}.jsonl"
        ),
    )
    return parser.parse_args()


def _load_contract(path: Path) -> tuple[dict[str, object], Path]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    model_path = Path(contract["sources"]["full_o10_mjcf"]["path_on_g2"])
    expected_hash = str(contract["sources"]["full_o10_mjcf"]["sha256"])
    if sha256_file(model_path) != expected_hash:
        raise RuntimeError("vendor MJCF hash differs from frozen G2 contract")
    return contract, model_path


def _target(
    pin,
    model,
    arm_joints,
    *,
    acceleration: float,
    jerk: float,
    synchronization: str,
    safety: IKSafety,
    enable_otg: bool,
) -> PoweredArmTarget:
    return PoweredArmTarget(
        pin,
        model,
        arm_joints,
        enable_ruckig_otg=enable_otg,
        otg_max_acceleration_rad_s2=acceleration,
        otg_max_jerk_rad_s3=jerk,
        otg_synchronization=synchronization,
        safety=safety,
    )


def _run_pipeline(
    *,
    pin,
    model,
    contract: dict[str, object],
    replay,
    snapshot: KinematicSnapshot,
    telemetry: ControlTelemetry,
    acceleration: float,
    jerk: float,
    synchronization: str,
    timeline_mode: str,
    path_source: str,
    settle_seconds: float,
    artifact_path: Path | None,
) -> dict[str, object]:
    period = 0.01
    trajectory = replay.trajectory(path_source)
    left_poses, right_poses = dual_eef_fk(
        pin, model, snapshot.waist, trajectory[:, :14]
    )
    if timeline_mode == "velocity-limited":
        timeline = velocity_limited_timeline(
            pin,
            left_poses,
            right_poses,
            trajectory[:, :14],
            source_hz=replay.sample_hz,
            max_linear_m_s=0.04,
            max_angular_rad_s=IKSafety().max_angular_speed_rad_s,
            max_joint_rad_s=IKSafety().max_joint_speed_rad_s,
        )
        max_translation = max(
            float(np.linalg.norm(pose.translation - poses[0].translation))
            for poses in (left_poses, right_poses)
            for pose in poses
        )
        max_rotation = max(
            float(np.linalg.norm(pin.log3(pose.rotation @ poses[0].rotation.T)))
            for poses in (left_poses, right_poses)
            for pose in poses
        )
        safety = IKSafety(
            max_linear_speed_m_s=0.04,
            max_target_translation_m=max_translation + 0.02,
            max_target_rotation_rad=max_rotation + 0.05,
        )
    else:
        timeline = ReplayTimeline(
            np.arange(len(replay.actions), dtype=np.float64) / replay.sample_hz
        )
        safety = IKSafety(max_linear_speed_m_s=0.04, max_target_translation_m=0.20)
    left_pink = _target(
        pin,
        model,
        contract["controlled_arm_joints"],
        acceleration=acceleration,
        jerk=jerk,
        synchronization=synchronization,
        safety=safety,
        enable_otg=False,
    )
    right_pink = _target(
        pin,
        model,
        contract["controlled_arm_joints"],
        acceleration=acceleration,
        jerk=jerk,
        synchronization=synchronization,
        safety=safety,
        enable_otg=False,
    )
    left_otg = _target(
        pin,
        model,
        contract["controlled_arm_joints"],
        acceleration=acceleration,
        jerk=jerk,
        synchronization=synchronization,
        safety=safety,
        enable_otg=True,
    )
    right_otg = _target(
        pin,
        model,
        contract["controlled_arm_joints"],
        acceleration=acceleration,
        jerk=jerk,
        synchronization=synchronization,
        safety=safety,
        enable_otg=True,
    )
    for target, side in (
        (left_pink, "left"),
        (right_pink, "right"),
        (left_otg, "left"),
        (right_otg, "right"),
    ):
        target.begin(side, snapshot)
    command = trajectory[0].copy()
    command[:14] = snapshot.arms
    telemetry.emit(
        "runtime_start",
        mode="offline-bc-eef-chain",
        process_id=os.getpid(),
        episode_id=replay.episode_id,
        source_jsonl_sha256=replay.source_sha256,
        replay_artifact_sha256=replay.artifact_sha256,
        path_source=path_source,
        source_hz=replay.sample_hz,
        timeline_mode=timeline_mode,
        replay_duration_s=timeline.duration_s,
        source_duration_s=replay.duration_s,
        settle_seconds=settle_seconds,
        control_hz=100.0,
        arm_hz=100.0,
        hand_stream_hz=0.0,
        conversion="BC_joint14_to_dual_hand_base_EEF_FK_to_ideal_twist",
        arm_pipeline="input_shaping_to_Pink_to_constraints_to_Ruckig_to_joint_reference",
        servo_channel="none",
        collision_protection=None,
        command_scope="none",
        otg_enabled=True,
        otg_backend="ruckig",
        otg_version=ruckig_version(),
        otg_synchronization=synchronization,
        otg_max_acceleration_rad_s2=acceleration,
        otg_max_jerk_rad_s3=jerk,
        target_translation_envelope_m=safety.max_target_translation_m,
        target_rotation_envelope_rad=safety.max_target_rotation_rad,
    )

    tick_count = int(round((timeline.duration_s + settle_seconds) / period)) + 1
    ideal_saturation_ticks = 0
    workspace_saturation_ticks = 0
    max_pink_reference_disagreement_rad = 0.0
    max_source_vs_command_rad = 0.0
    artifact_times: list[float] = []
    artifact_direct: list[np.ndarray] = []
    artifact_pink: list[np.ndarray] = []
    artifact_otg: list[np.ndarray] = []
    artifact_hands: list[np.ndarray] = []
    for tick in range(tick_count):
        trajectory_time = min(tick * period, timeline.duration_s)
        left_linear, left_angular = ideal_timed_twist(
            pin, left_poses, timeline, trajectory_time, period
        )
        right_linear, right_angular = ideal_timed_twist(
            pin, right_poses, timeline, trajectory_time, period
        )
        left_axes, left_angular_axes, left_saturated = normalized_twist_axes(
            left_linear,
            left_angular,
            max_linear_m_s=left_otg.safety.max_linear_speed_m_s,
            max_angular_rad_s=left_otg.safety.max_angular_speed_rad_s,
        )
        right_axes, right_angular_axes, right_saturated = normalized_twist_axes(
            right_linear,
            right_angular,
            max_linear_m_s=right_otg.safety.max_linear_speed_m_s,
            max_angular_rad_s=right_otg.safety.max_angular_speed_rad_s,
        )
        ideal_saturation_ticks += int(left_saturated or right_saturated)
        left_pink_q, left_pink_workspace = left_pink.step(
            mode="combined",
            axes=left_axes,
            angular_axes=left_angular_axes,
            dt=period,
        )
        right_pink_q, right_pink_workspace = right_pink.step(
            mode="combined",
            axes=right_axes,
            angular_axes=right_angular_axes,
            dt=period,
        )
        left_q, left_workspace = left_otg.step(
            mode="combined",
            axes=left_axes,
            angular_axes=left_angular_axes,
            dt=period,
        )
        right_q, right_workspace = right_otg.step(
            mode="combined",
            axes=right_axes,
            angular_axes=right_angular_axes,
            dt=period,
        )
        pink_q = np.concatenate((left_pink_q, right_pink_q))
        if (left_pink_workspace, right_pink_workspace) != (
            left_workspace,
            right_workspace,
        ):
            raise RuntimeError("Pink-only and OTG branches disagree on workspace state")
        if left_otg.last_candidate is not None and right_otg.last_candidate is not None:
            otg_pink_q = np.concatenate(
                (left_otg.last_candidate.q, right_otg.last_candidate.q)
            )
            disagreement = float(np.max(np.abs(pink_q - otg_pink_q)))
            max_pink_reference_disagreement_rad = max(
                max_pink_reference_disagreement_rad,
                disagreement,
            )
            if disagreement > 1e-9:
                raise RuntimeError(
                    "Pink-only and OTG branches no longer share the same Pink reference"
                )
        workspace_saturation_ticks += int(left_workspace or right_workspace)
        command[:7] = left_q
        command[7:14] = right_q
        # Arm path source and O10 command source are intentionally separate.
        # Dataset states are measured hand feedback and differ from the actual
        # O10 commands by up to 0.921 rad in this episode; only actions are a
        # valid replay command stream for the hands.
        command[14:] = sample_timed_vector(
            replay.actions[:, 14:], timeline, trajectory_time
        )
        source_q = sample_timed_vector(
            trajectory[:, :14], timeline, trajectory_time
        )
        artifact_times.append(tick * period)
        artifact_direct.append(source_q.copy())
        artifact_pink.append(pink_q)
        artifact_otg.append(command[:14].copy())
        artifact_hands.append(command[14:].copy())
        source_error = float(np.max(np.abs(source_q - command[:14])))
        max_source_vs_command_rad = max(max_source_vs_command_rad, source_error)
        telemetry.emit(
            "bc_eef_chain_sample",
            tick=tick,
            trajectory_time_s=trajectory_time,
            source_frame_position=float(
                np.interp(
                    trajectory_time,
                    timeline.frame_times_s,
                    np.arange(len(timeline.frame_times_s), dtype=np.float64),
                )
            ),
            ideal_left_linear_m_s=left_linear.tolist(),
            ideal_left_angular_rad_s=left_angular.tolist(),
            ideal_right_linear_m_s=right_linear.tolist(),
            ideal_right_angular_rad_s=right_angular.tolist(),
            ideal_input_saturated=left_saturated or right_saturated,
            left_workspace_saturated=left_workspace,
            right_workspace_saturated=right_workspace,
            source_arm_q_rad=source_q.tolist(),
            arm_command_rad=command[:14].tolist(),
            source_vs_command_max_rad=source_error,
            hand_target_rad=command[14:].tolist(),
            pink_left=left_pink.diagnostics(),
            pink_right=right_pink.diagnostics(),
            otg_left=left_otg.diagnostics(),
            otg_right=right_otg.diagnostics(),
            hardware_output=False,
        )
    artifact_sha256 = None
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = Path(f"{artifact_path}.partial")
        metadata = {
            "version": "g2-causal-command-replay-v2",
            "episode_id": replay.episode_id,
            "path_source": path_source,
            "arm_path_source": path_source,
            "hand_command_source": "actions",
            "timeline_mode": timeline_mode,
            "source_jsonl_sha256": replay.source_sha256,
            "replay_artifact_sha256": replay.artifact_sha256,
            "source_hz": replay.sample_hz,
            "control_hz": 1.0 / period,
            "motion_duration_s": timeline.duration_s,
            "settle_seconds": settle_seconds,
            "otg_max_acceleration_rad_s2": acceleration,
            "otg_max_jerk_rad_s3": jerk,
            "otg_synchronization": synchronization,
        }
        with partial_path.open("xb") as handle:
            np.savez_compressed(
                handle,
                metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)),
                time_s=np.asarray(artifact_times),
                q_direct=np.stack(artifact_direct),
                q_pink=np.stack(artifact_pink),
                q_otg=np.stack(artifact_otg),
                hands=np.stack(artifact_hands),
                source_frame_times_s=timeline.frame_times_s,
            )
        os.replace(partial_path, artifact_path)
        artifact_sha256 = sha256_file(artifact_path)
        telemetry.emit(
            "causal_command_artifact",
            path=str(artifact_path),
            sha256=artifact_sha256,
            metadata=metadata,
        )
    return {
        "ticks": tick_count,
        "duration_s": timeline.duration_s,
        "total_duration_s": (tick_count - 1) * period,
        "settle_seconds": settle_seconds,
        "path_source": path_source,
        "source_duration_s": replay.duration_s,
        "timeline_mode": timeline_mode,
        "ideal_saturation_ticks": ideal_saturation_ticks,
        "workspace_saturation_ticks": workspace_saturation_ticks,
        "max_pink_reference_disagreement_rad": max_pink_reference_disagreement_rad,
        "max_source_vs_command_rad": max_source_vs_command_rad,
        "max_send_ms": None,
        "artifact_path": None if artifact_path is None else str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "final_command": command.tolist(),
    }


def main() -> int:
    cli = parse_args()
    if cli.otg_max_acceleration_rad_s2 <= 0.0 or cli.otg_max_jerk_rad_s3 <= 0.0:
        raise RuntimeError("OTG acceleration and jerk limits must be positive")
    if cli.settle_seconds < 0.0:
        raise RuntimeError("settle seconds must be non-negative")
    replay = load_frozen_replay(cli.replay_json)
    contract, model_path = _load_contract(cli.contract)
    import pinocchio as pin

    model = pin.buildModelFromMJCF(str(model_path))
    if cli.preflight_files_only:
        print(
            f"PREFLIGHT PASS: episode={replay.episode_id} frames={len(replay.actions)} "
            f"duration={replay.duration_s:.3f}s model={model_path} Ruckig={ruckig_version()}"
        )
        print(
            f"Selected path={cli.path_source}; offline-only; no command sent."
        )
        return 0

    telemetry = ControlTelemetry(
        cli.telemetry_jsonl,
        run_id=cli.telemetry_jsonl.stem,
        mode="offline-bc-eef-chain",
        capacity=32768,
    )
    status = "fault"
    try:
        snapshot = KinematicSnapshot(
            sequence=0,
            sampled_at_monotonic=time.monotonic(),
            waist=np.zeros(5),
            arms=replay.trajectory(cli.path_source)[0, :14],
        )
        print("OFFLINE: full current arm chain; zero GDK and zero hardware output.")
        summary = _run_pipeline(
            pin=pin,
            model=model,
            contract=contract,
            replay=replay,
            snapshot=snapshot,
            telemetry=telemetry,
            acceleration=cli.otg_max_acceleration_rad_s2,
            jerk=cli.otg_max_jerk_rad_s3,
            synchronization=cli.otg_synchronization,
            timeline_mode=cli.timeline,
            path_source=cli.path_source,
            settle_seconds=cli.settle_seconds,
            artifact_path=cli.offline_artifact_npz,
        )
        telemetry.emit("bc_eef_chain_summary", **summary)
        print("CHAIN RESULT: " + json.dumps(summary, ensure_ascii=False), flush=True)
        status = "completed"
        return 0
    except KeyboardInterrupt:
        telemetry.emit("runtime_stop", reason="operator_ctrl_c")
        status = "operator_ctrl_c"
        print("Ctrl-C: command output stopped and software E-stop latched.", flush=True)
        return 130
    finally:
        telemetry.close(status=status)
        print(f"Telemetry finalized: {cli.telemetry_jsonl}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
