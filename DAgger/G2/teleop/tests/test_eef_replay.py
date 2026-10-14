from __future__ import annotations

import json

import numpy as np
import pytest

from g2teleop.eef_replay import (
    ReplayTimeline,
    load_causal_command_artifact,
    load_frozen_replay,
    normalized_twist_axes,
    sample_timed_vector,
    sample_vector,
)


def test_sample_vector_interpolates_and_clamps() -> None:
    samples = np.asarray([[0.0, 2.0], [2.0, 4.0], [4.0, 6.0]])
    np.testing.assert_allclose(sample_vector(samples, 2.0, 0.25), [1.0, 3.0])
    np.testing.assert_allclose(sample_vector(samples, 2.0, -1.0), samples[0])
    np.testing.assert_allclose(sample_vector(samples, 2.0, 99.0), samples[-1])


def test_normalized_axes_preserve_direction_and_report_saturation() -> None:
    linear, angular, saturated = normalized_twist_axes(
        [0.06, 0.0, 0.0],
        [0.0, 0.1, 0.0],
        max_linear_m_s=0.04,
        max_angular_rad_s=0.2,
    )
    np.testing.assert_allclose(linear, [1.5, 0.0, 0.0])
    np.testing.assert_allclose(angular, [0.0, 0.5, 0.0])
    assert saturated


def test_timed_vector_uses_nonuniform_intervals() -> None:
    samples = np.asarray([[0.0], [2.0], [6.0]])
    timeline = ReplayTimeline(np.asarray([0.0, 1.0, 3.0]))
    np.testing.assert_allclose(sample_timed_vector(samples, timeline, 2.0), [4.0])


def test_load_frozen_replay_validates_shape(tmp_path) -> None:
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "version": "g2-training-replay-v1",
                "episode_id": "episode",
                "sample_hz": 30.0,
                "source_jsonl_sha256": "abc",
                "frame_count": 2,
                "states": [[0.2] * 34, [0.3] * 34],
                "actions": [[0.0] * 34, [0.1] * 34],
            }
        ),
        encoding="utf-8",
    )
    replay = load_frozen_replay(path)
    assert replay.states.shape == (2, 34)
    assert replay.actions.shape == (2, 34)
    np.testing.assert_allclose(replay.trajectory("states"), replay.states)
    np.testing.assert_allclose(replay.trajectory("actions"), replay.actions)
    assert len(replay.artifact_sha256) == 64
    assert replay.duration_s == pytest.approx(1.0 / 30.0)


def test_load_frozen_replay_rejects_missing_or_misaligned_states(tmp_path) -> None:
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "version": "g2-training-replay-v1",
                "sample_hz": 30.0,
                "actions": [[0.0] * 34, [0.1] * 34],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="states"):
        load_frozen_replay(path)


def test_load_causal_command_artifact_keeps_three_common_clock_stages(tmp_path) -> None:
    path = tmp_path / "commands.npz"
    times = np.asarray([0.0, 0.01, 0.02])
    metadata = {"version": "g2-causal-command-replay-v1", "path_source": "states"}
    np.savez_compressed(
        path,
        metadata_json=np.asarray(json.dumps(metadata)),
        time_s=times,
        q_direct=np.zeros((3, 14)),
        q_pink=np.ones((3, 14)),
        q_otg=np.full((3, 14), 2.0),
        hands=np.zeros((3, 20)),
        source_frame_times_s=np.asarray([0.0, 0.02]),
    )
    artifact = load_causal_command_artifact(path)
    np.testing.assert_allclose(artifact.stage("direct"), 0.0)
    np.testing.assert_allclose(artifact.stage("pink"), 1.0)
    np.testing.assert_allclose(artifact.stage("otg"), 2.0)
    assert artifact.metadata["path_source"] == "states"
    assert len(artifact.artifact_sha256) == 64
