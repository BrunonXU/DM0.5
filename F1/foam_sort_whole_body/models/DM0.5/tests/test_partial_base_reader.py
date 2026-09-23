import json
import numpy as np
from test_reader import make_episode
from f1_data.reader import Episode, PARTIAL_BASE_CONTRACT, fit_statistics, sha256
from f1_data.state_tokens import state_tokens, make_tokenizer


def promote(ep):
    d = ep.data.copy()
    d['upper_body_chunk_start_candidate_valid'] = d['chunk_start_candidate_valid'].copy()
    np.savez(ep.source, **d)
    m = ep.meta | {'reader_contract': PARTIAL_BASE_CONTRACT}
    ep.source.with_suffix('.json').write_text(json.dumps(m))
    pm = ep.pose_meta | {'source_npz_sha256': sha256(ep.source)}
    ep.pose_cache.with_suffix('.json').write_text(json.dumps(pm))
    return Episode(ep.source, ep.pose_cache, allow_candidates=True)


def test_missing_base_keeps_arms_and_current_observation(tmp_path):
    ep = promote(make_episode(tmp_path, gap=55))
    assert ep.starts.tolist() == list(range(61))
    crossing = ep.sample(30)
    assert crossing['action_mask'][:, :20].all()
    assert not crossing['action_mask'][25, 20:23].any()
    assert np.isfinite(crossing['action']).all()
    current = ep.sample(55)
    assert current['state_mask'][:16].all() and not current['state_mask'][16:].any()
    assert state_tokens(current['state'], current['state_mask'])[-3:] == ['unknown']*3


def test_arm_invalidity_is_not_relaxed_by_partial_base(tmp_path):
    ep = make_episode(tmp_path, gap=55)
    ep.data['action_candidate_valid'][55, 0] = False
    partial = promote(ep)
    assert partial.starts.tolist() == list(range(6)) + list(range(56, 61))


def test_missing_placeholder_does_not_fit_state_statistics(tmp_path):
    ep = make_episode(tmp_path, gap=55)
    ep.data['action'][ep.data['base_candidate_present'], 16] = 2
    partial = promote(ep)
    stats = fit_statistics([partial])
    assert stats['schema'] == PARTIAL_BASE_CONTRACT
    assert stats['state']['observed_min'][16] == stats['state']['observed_max'][16] == 2
    assert stats['state_valid_count'][16] == 60


def test_tokenizer_preserves_nineteen_slots_and_marks_only_missing_base():
    class Base:
        def __init__(self, processor, **kwargs): pass
        def __call__(self, data): return self.action_to_bin_tokens(data['state'])
    tokenizer = make_tokenizer(None, base_class=Base)
    mask = np.ones(19, bool); mask[16:] = False
    actual = tokenizer({'state': np.zeros(19), 'state_mask': mask})
    assert actual == [127]*16 + ['unknown']*3
    assert tokenizer({'state': np.zeros(19)}) == [127]*19


def test_streaming_statistics_match_materialized_masked_moments(tmp_path):
    ep = promote(make_episode(tmp_path, gap=55))
    # Cross multiple 256-window accumulation boundaries.
    ep.starts = np.tile(ep.starts, 10)
    stats = fit_statistics([ep])
    samples = [ep.sample(t) for t in ep.starts]
    action = np.concatenate([s['action'][:, :23] for s in samples]).astype(np.float64)
    mask = np.concatenate([s['action_mask'][:, :23] for s in samples])
    for d in range(23):
        values = action[mask[:, d], d]
        assert stats['action']['valid_count'][d] == len(values)
        np.testing.assert_allclose(stats['action']['raw_std'][d], values.std(), atol=1e-12, rtol=1e-10)
        if d in (0, 1, 2, 9, 10, 11, 20, 21, 22):
            np.testing.assert_allclose(stats['action']['center'][d], values.mean(), atol=1e-12, rtol=1e-10)
