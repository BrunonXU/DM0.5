import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_data.reader import (Episode, F1Dataset, Normalizer, decode_eef,
                            decode_rotation6d, fit_statistics, sha256)


def make_episode(tmp_path, name="ep", split="train", n=110, gap=None):
    root = tmp_path / name
    root.mkdir()
    source, cache = root / "episode.npz", root / "pose_cache.npz"
    state = np.zeros((n, 21), dtype=np.float32)
    state[:, 14:16] = 100
    state[:, 16:] = 999  # Must never become model base velocity.
    action = np.zeros((n, 19), dtype=np.float32)
    action[:, 14:16] = 100
    action[:, 16] = np.arange(n) / n
    masks = np.ones((n, 19), dtype=bool)
    reviewed = np.ones(n, dtype=bool)
    if gap is not None:
        action[gap, 16:] = np.nan
        masks[gap, 16:] = False
        reviewed[gap] = False
    arrays = dict(time_ns=np.arange(n, dtype=np.int64) * 33333333, state=state, action=action,
                  state_valid=np.ones((n, 21), dtype=bool), action_candidate_valid=masks,
                  camera_timestamp_candidate_valid=np.ones((n, 3), dtype=bool),
                  camera_source_indices=np.tile(np.arange(n)[:, None], (1, 3)),
                  observation_candidate_valid=np.ones(n, dtype=bool),
                  chunk_start_candidate_valid=np.arange(n) <= n-50,
                  base_candidate_present=reviewed, base_timing_reviewed=reviewed,
                  training_approved=np.array(False))
    np.savez(source, **arrays)
    source.with_suffix(".json").write_text(json.dumps(dict(episode=name, split=split, training_approved=False)))
    feedback = np.tile(np.eye(4), (n, 2, 1, 1))
    feedback[:, :, :3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    feedback[:, :, :3, 3] = [1, 2, 3]
    command = np.tile(np.eye(4), (n, 2, 1, 1))
    command[:, :, :3, 3] = [1.1, 2.2, 3.3]
    command[:, 0, 0, 3] += np.arange(n) * .001
    np.savez(cache, time_ns=arrays["time_ns"], feedback_pose=feedback, command_pose=command,
             feedback_valid=np.ones((n, 2), dtype=bool), command_valid=np.ones((n, 2), dtype=bool))
    cache.with_suffix(".json").write_text(json.dumps(dict(source_npz_sha256=sha256(source), pose_cache_sha256=sha256(cache), pose_unit="m",
                                                         arm_order=["left", "right"], execution_mapping="test", kinematics_contract="test")))
    return Episode(source, cache, allow_candidates=True)


def test_anchored_pose_roundtrip_and_layout(tmp_path):
    ep = make_episode(tmp_path)
    sample = ep.sample(4)
    np.testing.assert_allclose(sample["state"][16:], ep.data["action"][4, 16:])
    np.testing.assert_allclose(sample["action"][0, :3], [.2, -.104, .3], atol=1e-7)
    decoded = decode_eef(sample["action"], sample["anchor"])
    np.testing.assert_allclose(decoded, ep.poses["command_pose"][4:54], atol=1e-7)
    assert sample["action"].shape == (50, 32)
    assert sample["action_mask"].dtype == bool
    assert not sample["action_mask"][:, 23:].any()
    assert sample["action_mask"][:, 22].all()  # Valid constant yaw is supervised.


def test_unknown_base_drops_current_and_crossing_windows(tmp_path):
    ep = make_episode(tmp_path, gap=55)
    assert set(ep.starts) == set(range(6)) | set(range(56, 61))
    with pytest.raises(ValueError, match="invalid sample"):
        ep.sample(55)
    for t in ep.starts:
        assert np.isfinite(ep.sample(t)["action"]).all()


def test_candidate_gate_and_source_hash(tmp_path):
    ep = make_episode(tmp_path)
    with pytest.raises(ValueError, match="candidate"):
        Episode(ep.source, ep.pose_cache)
    ep.source.write_bytes(ep.source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA mismatch"):
        Episode(ep.source, ep.pose_cache, allow_candidates=True)


def test_statistics_only_train_and_affine_roundtrip(tmp_path):
    ep = make_episode(tmp_path)
    val = make_episode(tmp_path, "val", "validation")
    with pytest.raises(ValueError, match="only training"):
        fit_statistics([ep, val])
    stats = fit_statistics([ep])
    normalizer = Normalizer(stats)
    sample = ep.sample(10)
    transformed = normalizer(sample)
    np.testing.assert_allclose(normalizer.denormalize_action(transformed["action"]), sample["action"][:, :23], atol=1e-6)
    assert transformed["state"][15] == 1
    assert (transformed["action"][:, 19] == 1).all()


def test_rare_valid_arm_pose_is_retained_by_train_minmax(tmp_path):
    ep = make_episode(tmp_path, n=260)
    # Nondegenerate quantile example: a tiny regular motion plus a rare excursion.
    ep.data['state'][:, 0] = np.linspace(0, 1, len(ep.data['state']))
    ep.data['state'][100, 0] = 45
    stats = fit_statistics([ep]);old = fit_statistics([ep], state_arm_range='q01_q99')
    assert stats['state']['range_policy']['arms'] == 'train_minmax'
    for t in ep.starts:
        sample=ep.sample(t);normalized=Normalizer(stats)(sample)['state']
        restored=normalized[0]*stats['state']['scale'][0]+stats['state']['center'][0]
        assert abs(restored-sample['state'][0]) < 2e-6
    sample=ep.sample(100);old_value=Normalizer(old)(sample)['state'][0]
    assert abs(old_value*old['state']['scale'][0]+old['state']['center'][0]-45)>40
    # The requested change leaves base and action statistics untouched.
    np.testing.assert_array_equal(stats['state']['center'][14:],old['state']['center'][14:])
    assert stats['action']==old['action']
    assert stats["action"]["scale"][22] == 1
    assert stats["action"]["valid_count"][22] == len(ep.starts) * 50


def test_sparse_state_quantile_fallback_keeps_rare_motion(tmp_path):
    ep = make_episode(tmp_path, n=300)
    ep.data["action"][:, 16] = 0
    ep.data["action"][50, 16] = .7
    stats = fit_statistics([ep])
    assert stats["state"]["minmax_fallback"][16]
    assert not stats["state"]["constant"][16]
    sample = Normalizer(stats)(ep.sample(50))
    assert sample["state"][16] == pytest.approx(1)


def test_degenerate_rotation_is_rejected():
    for rows in ([0] * 6, [1, 0, 0, 2, 0, 0], [np.nan, 0, 0, 0, 1, 0]):
        with pytest.raises(ValueError):
            decode_rotation6d(rows)


def test_balanced_sampler_and_split_isolation(tmp_path):
    short = make_episode(tmp_path, "short", n=60)
    long = make_episode(tmp_path, "long", n=300)
    dataset = F1Dataset([short, long])
    first = list(dataset.balanced_indices(4000))
    assert first == list(dataset.balanced_indices(4000))
    count = sum(dataset.index[i][0] == 0 for i in first)
    assert 1850 < count < 2150
    left = list(dataset.balanced_indices(50, world_size=2, rank=0))
    right = list(dataset.balanced_indices(50, world_size=2, rank=1))
    assert [x for pair in zip(left, right) for x in pair] == list(dataset.balanced_indices(100))
    val = make_episode(tmp_path, "val", "validation")
    with pytest.raises(ValueError, match="mix"):
        F1Dataset([short, val])


def test_source_restrictions_never_relaxed(tmp_path):
    ep = make_episode(tmp_path)
    ep.data["state_valid"][:, 18] = False
    ep.data["chunk_start_candidate_valid"][:] = False
    np.savez(ep.source, **ep.data)
    meta = ep.pose_meta
    meta["source_npz_sha256"] = sha256(ep.source)
    ep.pose_cache.with_suffix(".json").write_text(json.dumps(meta))
    reloaded = Episode(ep.source, ep.pose_cache, allow_candidates=True)
    assert len(reloaded.starts) == 0
