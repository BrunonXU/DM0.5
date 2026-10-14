#!/usr/bin/env python3
"""Exercise START-latched HIL, arbitration, recording and audit; no GDK."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from g2dagger.hil_arbiter import HILActionArbiter
from g2dagger.hil_episode import CameraFrameRef, HILEpisodeRecorder, audit_hil_episode
from g2dagger.hil_supervisor import HILState, HILSupervisor
from g2dagger.recording_observation import DeviceTimestamp, TimestampedFrame, TimestampedObservation
from g2dagger.recording_sinks import make_recording_sink
from g2dagger.recording_audit import audit_payload_archive, audit_journal_payload_links


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--recording-mode", choices=("inference-only", "hil-record", "prebuffer-record"), default="inference-only")
    args = parser.parse_args()
    payload_path = args.output.with_name(args.output.name + "_payload")
    sink = make_recording_sink(args.recording_mode, payload_path)
    synthetic_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEUlEQVR4nGP4z8DA8B+MgBgAHfAD/dPQfSYAAAAASUVORK5CYII=")

    supervisor = HILSupervisor()
    arbiter = HILActionArbiter()
    recorder = HILEpisodeRecorder(
        args.output,
        episode_id=args.output.name,
        task_prompt="mock policy to START-latched human correction and fresh-policy handoff",
    )
    base_ns = time.monotonic_ns()
    hold = np.zeros(34)
    policy_chunk = np.stack([np.full(34, index / 1000.0) for index in range(50)])

    toggle_id = 0
    update = supervisor.observe_operator(
        session_id="mock-session", hil_toggle_id=toggle_id,
        rb_deadman=False, hand_motion_pending=False,
    )
    request_id = 0
    observation_id = 0
    latest_observation_ns = base_ns
    chunk_start_ns = base_ns + 1_000_000
    for tick in range(60):
        now_ns = base_ns + 2_000_000 + tick * 10_000_000
        # Rational 30 Hz acquisition clock, independent of inference and HIL.
        while base_ns + observation_id * 1_000_000_000 // 30 <= now_ns:
            latest_observation_ns = base_ns + observation_id * 1_000_000_000 // 30
            acquisition = TimestampedObservation(
                observation_id, latest_observation_ns, latest_observation_ns + 2,
                tuple(TimestampedFrame(name, observation_id,
                    DeviceTimestamp(latest_observation_ns + j, "ns", "synthetic"),
                    latest_observation_ns + j, synthetic_png, "image/png", 2, 2)
                    for j, name in enumerate(("head", "left_wrist", "right_wrist"))),
                latest_observation_ns, tuple(hold), (False,) * 34, "synthetic_state_no_hardware")
            sink.submit(acquisition)
            recorder.record_timestamped_observation(acquisition, hil_state=update.state,
                intervention_id=update.intervention_id, policy_epoch=update.policy_epoch)
            observation_id += 1
        rb = 11 <= tick < 30
        if tick == 10:
            toggle_id += 1
        if tick == 35:
            toggle_id += 1
        update = supervisor.observe_operator(
            session_id="mock-session",
            hil_toggle_id=toggle_id,
            rb_deadman=rb,
            hand_motion_pending=False, monotonic_ns=now_ns,
        )
        if update.transition:
            recorder.record_event(
                update.transition,
                monotonic_ns=now_ns,
                reason=update.reason,
                hil_state=update.state,
                intervention_id=update.intervention_id,
                policy_epoch=update.policy_epoch,
            )
        if tick == 10:
            sink.trigger()
        if tick == 0:
            recorder.record_policy_chunk(
                policy_epoch=0, request_id=0, observation_request_id=0,
                observation_monotonic_ns=base_ns, request_monotonic_ns=base_ns + 100_000,
                response_monotonic_ns=chunk_start_ns, actions_50x34=policy_chunk)
        if tick == 38 and update.state == HILState.HANDOFF_HOLD.value:
            request_id = 1
            fresh = np.stack(
                [np.full(34, 0.2 + index / 1000.0) for index in range(50)]
            )
            recorder.record_policy_chunk(
                policy_epoch=update.policy_epoch,
                request_id=request_id,
                observation_request_id=observation_id - 1,
                observation_monotonic_ns=latest_observation_ns,
                request_monotonic_ns=latest_observation_ns + 100_000,
                response_monotonic_ns=now_ns,
                actions_50x34=fresh,
            )
            assert supervisor.register_fresh_policy_request(
                policy_epoch=update.policy_epoch, request_id=request_id,
                observation_id=observation_id - 1,
                observation_monotonic_ns=latest_observation_ns,
                request_monotonic_ns=latest_observation_ns + 100_000)
            chunk_start_ns = now_ns
            policy_chunk = fresh
            update = supervisor.accept_fresh_policy_chunk(
                policy_epoch=update.policy_epoch, request_id=request_id, response_monotonic_ns=now_ns
            )
            recorder.record_event(
                update.transition or "fresh_policy_accepted",
                monotonic_ns=now_ns,
                reason=update.reason,
                hil_state=update.state,
                intervention_id=update.intervention_id,
                policy_epoch=update.policy_epoch,
                policy_request_id=request_id,
            )

        policy_index = min(max(0, (now_ns - chunk_start_ns) * 30 // 1_000_000_000), 49)
        policy = policy_chunk[policy_index]
        human = np.full(34, 0.5 + tick / 1000.0)
        selected = arbiter.select(
            update,
            hold_action=hold,
            policy_action=policy,
            human_action=human,
            policy_request_id=request_id,
            policy_action_index=policy_index,
        )
        recorder.record_control_tick(
            monotonic_ns=now_ns,
            control_sequence=tick,
            hil_state=update.state,
            control_source=selected.control_source,
            intervention_id=update.intervention_id,
            policy_epoch=update.policy_epoch,
            policy_request_id=request_id,
            policy_action_index=policy_index,
            intervention_mask=selected.intervention_mask,
            human_arm_mask=selected.human_arm_mask,
            human_hand_mask=selected.human_hand_mask,
            hold_34d=hold,
            policy_proposal_34d=policy,
            human_candidate_34d=human,
            selected_command_34d=selected.command,
            executed_command_34d=None,
            measured_34d=hold,
            evidence_kind="synthetic_no_hardware",
        )
        hold = selected.command.copy()

    payload_closed = sink.close()
    if not recorder.close() or not payload_closed:
        print(f"MOCK HIL FAILED TO FINALIZE: {recorder.partial_path}")
        return 2
    report = audit_hil_episode(args.output)
    if args.recording_mode != "inference-only":
        report["payload_audit"] = audit_payload_archive(payload_path)
        report["errors"].extend(audit_journal_payload_links(args.output, payload_path))
        report["valid"] = report["valid"] and report["payload_audit"]["valid"] and not report["errors"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("NO GDK: structural HIL evidence only; training_ready remains false.")
    return 0 if report["valid"] and not report["training_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
