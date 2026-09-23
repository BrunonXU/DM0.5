#!/usr/bin/env python3
"""Independently verify the frozen exclusions, mask-only changes and numeric replay."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
from prepare_demo_revision import TASK, sha, read_rows
from export_replay_bundle import export_bundle

sys.path.insert(0, str(TASK / 'robot'))
from f1_replay.bundle import load_bundle


def main():
    root = TASK / 'results/demo_778_tail'
    masks = root / 'conservative_masks'
    summary = json.loads((masks / 'summary.json').read_text())
    rows = read_rows(root / 'candidates.jsonl')
    parents = {row['episode']: row for row in rows}
    excluded = read_rows(root / 'excluded.jsonl')
    derived = read_rows(masks / 'manifest.jsonl')
    assert len(parents) == len(derived) == 778
    assert len({r['episode'] for r in excluded}) == 23
    assert not set(parents) & {r['episode'] for r in excluded}
    assert {r['episode'] for r in derived} == set(parents)
    assert sha(masks / 'manifest.jsonl') == summary['manifest_sha256']
    new_ids = {'all_data_2_derived/episode_000256'} | {f'all_data_1_derived/episode_{i:06d}' for i in range(9)}
    old_ids = {r['episode'] for r in read_rows(TASK / 'results/demo_788_check/excluded.jsonl')}
    assert {r['episode'] for r in excluded} == old_ids | new_ids
    assert Counter(r['parent_split'] for r in rows) == {'train': 697, 'validation': 81}
    changed = {'action_candidate_valid', 'future_signal_action_candidate_valid',
               'joint_frame_candidate_valid', 'chunk_start_candidate_valid', 'training_approved'}
    totals = Counter()
    for row in derived:
        parent = parents[row['episode']]
        a, b = Path(parent['aligned_npz_path']), Path(row['aligned_npz_path'])
        assert sha(a) == parent['aligned_npz_sha256']
        assert sha(a.with_suffix('.json')) == parent['aligned_metadata_sha256']
        assert sha(b) == row['aligned_npz_sha256']
        assert sha(b.with_suffix('.json')) == row['aligned_metadata_sha256']
        assert parent['parent_split'] == row['parent_split']
        with np.load(a, allow_pickle=False) as old, np.load(b, allow_pickle=False) as new:
            assert set(old.files) == set(new.files)
            for key in set(old.files) - changed:
                assert old[key].dtype == new[key].dtype and old[key].shape == new[key].shape
                assert old[key].tobytes() == new[key].tobytes(), (row['episode'], key)
            boundary = row['tail_mask_start_index']
            assert not new['action_candidate_valid'][boundary:].any()
            assert np.array_equal(old['action_candidate_valid'][:boundary], new['action_candidate_valid'][:boundary])
            for key in changed:
                assert not np.any(new[key] & ~old[key])
            starts = np.flatnonzero(new['chunk_start_candidate_valid'])
            assert len(starts) > 0 and (starts + 50 <= boundary).all()
            future = new['future_signal_action_candidate_valid']
            obs = new['observation_candidate_valid']
            # Explicitly examine every admitted chunk, independently of prefix-sum construction.
            assert all(obs[i] and future[i:i + 50].all() for i in starts)
            assert np.array_equal(np.flatnonzero(new['chunk_start_candidate_valid']),
                                  np.flatnonzero(old['chunk_start_candidate_valid'][:boundary - 49]))
            totals.update(sources=1, frames=len(new['time_ns']),
                          joint_frames=int(new['joint_frame_candidate_valid'].sum()), chunks=len(starts))
    assert totals['joint_frames'] == summary['totals']['joint_valid_frames_after'] == 822444
    assert totals['chunks'] == summary['totals']['chunk_starts_after'] == 784454

    by_id = {row['episode']: row for row in derived}
    bad = by_id['all_data_derived/episode_000192']
    with tempfile.TemporaryDirectory(prefix='f1-tail-rejection-') as tmp:
        start = bad['tail_mask_start_index']
        try:
            export_bundle(bad['aligned_npz_path'], Path(tmp) / 'rejected', summary['version'], start, start + 5)
        except ValueError as error:
            assert 'Invalid action' in str(error), str(error)
            rejection = str(error)
            assert not (Path(tmp) / 'rejected/manifest.json').exists()
        else:
            raise AssertionError('A known unsupported tail was exportable')
    sample = 'all_data_3_derived/episode_000083'
    bundle_path = root / 'replay_bundles/all_data_3_derived_episode_000083'
    bundle = load_bundle(bundle_path)
    assert bundle.manifest['source']['cleaning_version'] == summary['version']
    assert bundle.manifest['source']['aligned_npz']['sha256'] == by_id[sample]['aligned_npz_sha256']
    with np.load(by_id[sample]['aligned_npz_path'], allow_pickle=False) as data:
        start, stop = 2, 773
        assert bundle.manifest['source']['start_index'] == start
        assert bundle.manifest['source']['stop_index_exclusive'] == stop
        assert len(bundle.frames) == stop - start == 771
        for i, frame in enumerate(bundle.frames, start):
            assert frame.source_time_ns == int(data['time_ns'][i])
            assert np.array_equal(frame.state, data['state'][i])
            assert np.array_equal(frame.action, data['action'][i])
        assert all(frame.action[-3:] == (0.0, 0.0, 0.0) for frame in bundle.frames[-30:])
    previews = sorted((root / 'replay_preview').glob('*/commands.jsonl'))
    assert len(previews) == 1
    commands = read_rows(previews[0])
    assert len(commands) == len(bundle.frames) + 1
    for command, frame in zip(commands[:-1], bundle.frames):
        assert command['source_time_ns'] == frame.source_time_ns
        assert command['source_state'] == list(frame.state)
        assert command['arm_positions_deg'] + command['gripper_positions'] + command['base_vx_vy_wz'] == list(frame.action)
        assert command['hardware_published'] is False
    assert commands[-1]['event'] == 'planned_end' and commands[-1]['base_vx_vy_wz'] == [0, 0, 0]
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'passed': True, 'totals': dict(totals),
              'additional_exclusions_exact': True, 'split_unchanged': True,
              'all_nonmask_arrays_byte_identical': True, 'parent_pack_hashes_unchanged': True,
              'all_chunks_checked': True, 'known_tail_export_rejected': rejection,
              'numeric_replay': {'episode': sample, 'source_indices_half_open': [2, 773],
                                 'frames': 771, 'duration_s': 25.7, 'all_values_and_times_verified': True,
                                 'manifest_sha256': bundle.sha256, 'commands_sha256': sha(previews[0]),
                                 'scope': 'numeric_only_not_training_reader_or_camera_or_robot_acceptance'},
              'selection_sha256': sha(root / 'selection.json'), 'mask_summary_sha256': sha(masks / 'summary.json'),
              'validator_sha256': sha(Path(__file__)), 'ros_imported': any(n == 'rclpy' or n.startswith('rclpy.') for n in sys.modules),
              'robot_motion_started': False, 'training_started': False}
    assert result['ros_imported'] is False
    with (root / 'validation.json').open('x') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
