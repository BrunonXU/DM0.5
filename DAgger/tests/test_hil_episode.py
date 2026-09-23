from __future__ import annotations

import json

import numpy as np

from g2dagger.hil_episode import (
    CameraFrameRef,
    HILEpisodeRecorder,
    audit_hil_episode,
)


def _refs(timestamp_ns: int):
    return {
        name: CameraFrameRef(f"{name}.mp4", 0, timestamp_ns + index)
        for index, name in enumerate(("head", "left_wrist", "right_wrist"))
    }


def test_episode_is_atomic_auditable_and_explicitly_not_training_ready(tmp_path) -> None:
    final = tmp_path / "episode_1"
    recorder = HILEpisodeRecorder(final, episode_id="episode_1", task_prompt="task")
    assert not final.exists()
    assert recorder.partial_path.exists()
    zero = np.zeros(34)
    one = np.ones(34)
    assert recorder.record_observation(
        monotonic_ns=100,
        request_id=0,
        measured_34d=zero,
        camera_refs=_refs(100),
        hil_state="policy",
        intervention_id=0,
        policy_epoch=0,
    )
    assert recorder.record_policy_chunk(
        policy_epoch=0,
        request_id=0,
        observation_request_id=0,
        observation_monotonic_ns=100,
        response_monotonic_ns=110,
        actions_50x34=np.ones((50, 34)),
    )
    assert recorder.record_control_tick(
        monotonic_ns=120,
        control_sequence=0,
        hil_state="policy",
        control_source="policy",
        intervention_id=0,
        policy_epoch=0,
        policy_request_id=0,
        policy_action_index=0,
        intervention_mask=False,
        human_arm_mask=False,
        human_hand_mask=False,
        hold_34d=zero,
        policy_proposal_34d=one,
        human_candidate_34d=zero,
        selected_command_34d=one,
        executed_command_34d=one,
        measured_34d=zero,
    )
    assert recorder.record_event("policy_to_hil", monotonic_ns=130)
    assert recorder.close()
    assert final.exists()
    assert not recorder.partial_path.exists()

    report = audit_hil_episode(final)
    assert report["valid"] is True
    assert report["training_ready"] is False
    assert report["counts"] == {
        "observations": 1,
        "policy_chunks": 1,
        "control": 1,
        "events": 1,
    }
    assert report["ownership_counts"] == {"policy": 1}


def test_audit_rejects_selected_command_that_disagrees_with_owner(tmp_path) -> None:
    final = tmp_path / "episode_bad"
    recorder = HILEpisodeRecorder(final, episode_id="episode_bad", task_prompt="task")
    zero = np.zeros(34)
    assert recorder.record_observation(
        monotonic_ns=0,
        request_id=0,
        measured_34d=zero,
        camera_refs=_refs(0),
        hil_state="policy",
        intervention_id=0,
        policy_epoch=0,
    )
    assert recorder.record_policy_chunk(
        policy_epoch=0,
        request_id=0,
        observation_request_id=0,
        observation_monotonic_ns=0,
        response_monotonic_ns=1,
        actions_50x34=np.zeros((50, 34)),
    )
    assert recorder.record_control_tick(
        monotonic_ns=1,
        control_sequence=0,
        hil_state="policy",
        control_source="policy",
        intervention_id=0,
        policy_epoch=0,
        policy_request_id=0,
        policy_action_index=0,
        intervention_mask=False,
        human_arm_mask=False,
        human_hand_mask=False,
        hold_34d=zero,
        policy_proposal_34d=zero,
        human_candidate_34d=zero,
        selected_command_34d=zero,
        executed_command_34d=zero,
        measured_34d=zero,
    )
    assert recorder.close()
    control_path = final / "control.jsonl"
    row = json.loads(control_path.read_text())
    row["selected_command_34d"][0] = 1.0
    control_path.write_text(json.dumps(row) + "\n")
    report = audit_hil_episode(final)
    assert report["valid"] is False
    assert any("disagrees with source=policy" in error for error in report["errors"])


def test_noncompleted_episode_is_left_partial(tmp_path) -> None:
    final = tmp_path / "episode_abort"
    recorder = HILEpisodeRecorder(final, episode_id="episode_abort", task_prompt="task")
    assert recorder.close(status="aborted") is False
    assert not final.exists()
    manifest = json.loads((recorder.partial_path / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["status"] == "aborted"
