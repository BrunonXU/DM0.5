#!/usr/bin/env python3
"""Keep all source frames/values, invalidate conservative held-target tails in a new pack."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from prepare_demo_revision import sha, read_rows, TASK

VERSION = 'demo_778_conservative_tail_v1'


def derive_arrays(arrays, boundary, horizon):
    """A normal 19D chunk must end before unsupported arm commands begin."""
    result = {key: value.copy() for key, value in arrays.items()}
    n = len(result['time_ns'])
    if not 0 < boundary < n or not 1 <= horizon <= n:
        raise ValueError('Invalid boundary/horizon')
    result['action_candidate_valid'][boundary:, :] = False
    future = result['state_valid'].all(axis=1) & result['action_candidate_valid'].all(axis=1)
    joint = result['observation_candidate_valid'] & result['action_candidate_valid'].all(axis=1)
    chunk = np.zeros(n, dtype=bool)
    bad_prefix = np.r_[0, np.cumsum(~future)]
    chunk[:n - horizon + 1] = result['observation_candidate_valid'][:n - horizon + 1] & (
        bad_prefix[horizon:] - bad_prefix[:-horizon] == 0)
    result['future_signal_action_candidate_valid'] = future
    result['joint_frame_candidate_valid'] = joint
    result['chunk_start_candidate_valid'] = chunk
    result['training_approved'] = np.array(False)
    return result


def build(selection, output):
    if output.exists():
        raise ValueError('Refusing to overwrite derived masks: ' + str(output))
    selection_report = json.loads((selection / 'selection.json').read_text())
    if sha(selection / 'candidates.jsonl') != selection_report['candidates_sha256']:
        raise ValueError('Selection hash differs')
    rows = read_rows(selection / 'candidates.jsonl')
    tail_path = TASK / 'results/demo_788_check/signals/tail_reset_candidates.jsonl'
    tails = {r['episode']: r for r in read_rows(tail_path)}
    totals, derived_rows = Counter(), []
    if len(rows) != len({r['episode'] for r in rows}) or len(rows) != 778:
        raise ValueError('Unexpected source pool')
    output.mkdir(parents=True, exist_ok=False)
    for row in rows:
        episode = row['episode']
        tail = tails[episode]
        source = Path(row['aligned_npz_path'])
        if sha(source) != row['aligned_npz_sha256'] or sha(source.with_suffix('.json')) != row['aligned_metadata_sha256']:
            raise ValueError('Parent pack hash differs: ' + episode)
        with np.load(source, allow_pickle=False) as pack:
            original = {key: pack[key] for key in pack.files}
        start = int(tail['tail_start_frame_candidate'])
        stop = int(tail['tail_stop_frame_exclusive'])
        if not tail['raw_lead_exact_constant'] or not tail['continues_to_last_numeric_arm_frame']:
            raise ValueError('Tail evidence no longer satisfies the diagnostic: ' + episode)
        if not np.all(original['action'][start:stop, :14] == original['action'][start, :14]):
            raise ValueError('Selected arm targets are not constant: ' + episode)
        horizon = row['chunk_horizon']
        derived = derive_arrays(original, start, horizon)
        changed_keys = {'action_candidate_valid', 'future_signal_action_candidate_valid',
                        'joint_frame_candidate_valid', 'chunk_start_candidate_valid', 'training_approved'}
        for key in original.keys() - changed_keys:
            if original[key].tobytes() != derived[key].tobytes():
                raise ValueError('Unrequested array mutation: ' + key)
        for key in changed_keys:
            if np.any(derived[key] & ~original[key]):
                raise ValueError('A previously invalid entry was promoted: ' + key)
        # Independent interval condition against the parent starts, including preceding starts.
        expected = original['chunk_start_candidate_valid'] & (np.arange(len(derived['time_ns'])) + horizon <= start)
        if not np.array_equal(expected, derived['chunk_start_candidate_valid']):
            raise ValueError('Rebuilt chunks do not match the exclusive tail boundary: ' + episode)
        if not derived['chunk_start_candidate_valid'].any():
            raise ValueError('No usable prefix remains: ' + episode)
        target = (output / 'aligned' / episode).with_suffix('.npz')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            np.savez_compressed(stream, **derived)
        meta = json.loads(source.with_suffix('.json').read_text())
        old_counts = {k: meta.get(k) for k in ('joint_frame_candidate_valid_frames', 'chunk_candidate_starts', 'continuous_windows')}
        valid_future = derived['future_signal_action_candidate_valid']
        meta.update(cleaning_version=VERSION,
                    joint_frame_candidate_valid_frames=int(derived['joint_frame_candidate_valid'].sum()),
                    chunk_candidate_starts=int(derived['chunk_start_candidate_valid'].sum()),
                    continuous_windows=int(np.count_nonzero(np.diff(np.r_[False, valid_future, False].astype(int)) == 1)),
                    parent_counts=old_counts,
                    parent_aligned_npz_sha256=row['aligned_npz_sha256'],
                    parent_aligned_metadata_sha256=row['aligned_metadata_sha256'],
                    candidate_selection_sha256=selection_report['candidates_sha256'],
                    tail_evidence_sha256=sha(tail_path),
                    tail_mask_start_index=start,
                    tail_mask_start_s=tail['tail_start_s_candidate'],
                    tail_mask_policy='all_19D_invalid_from_conservative_held_arm_target_start_until_source_end',
                    tail_boundary_is_certified_reset_onset=False,
                    return_phase_may_be_truncated=True,
                    note='All raw aligned values/timestamps/images retained; only validity masks tightened. A valid prefix is not a complete-cycle or robot-execution acceptance.')
        with target.with_suffix('.json').open('x') as stream:
            stream.write(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
        record = {'episode': episode, 'parent_split': row['parent_split'], 'cleaning_version': VERSION,
                  'aligned_npz_path': str(target.resolve()), 'aligned_npz_sha256': sha(target),
                  'aligned_metadata_sha256': sha(target.with_suffix('.json')),
                  'parent_aligned_npz_sha256': row['aligned_npz_sha256'],
                  'tail_mask_start_index': start, 'tail_mask_start_s': tail['tail_start_s_candidate'],
                  'frames_retained': len(derived['time_ns']),
                  'joint_valid_frames_before': int(original['joint_frame_candidate_valid'].sum()),
                  'joint_valid_frames_after': int(derived['joint_frame_candidate_valid'].sum()),
                  'chunk_starts_before': int(original['chunk_start_candidate_valid'].sum()),
                  'chunk_starts_after': int(derived['chunk_start_candidate_valid'].sum()),
                  'training_approved': False, 'complete_cycle_verified': False}
        derived_rows.append(record)
        totals.update({key: record[key] for key in ('frames_retained', 'joint_valid_frames_before', 'joint_valid_frames_after', 'chunk_starts_before', 'chunk_starts_after')})
    with (output / 'manifest.jsonl').open('x') as stream:
        for row in derived_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    summary = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
               'sources': len(derived_rows), 'totals': dict(totals),
               'mask_policy': 'conservative_complete_19D_supervision_prefixes_not_fixed_duration_cuts',
               'boundary_is_certified_reset_onset': False,
               'source_selection_sha256': selection_report['candidates_sha256'],
               'tail_evidence_sha256': sha(tail_path), 'script_sha256': sha(Path(__file__)),
               'manifest_sha256': sha(output / 'manifest.jsonl'),
               'raw_and_v1_modified': False, 'values_and_timestamps_modified': False,
               'training_approved': False, 'robot_replay_verified': False,
               'warning': 'Conservative prefixes truncate part of the weak base return in many sources; do not count them as complete loops or pad unknown future actions.'}
    with (output / 'summary.json').open('x') as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', type=Path, default=TASK / 'results/demo_778_tail')
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_778_tail/conservative_masks')
    args = parser.parse_args()
    print(json.dumps(build(args.selection, args.output), ensure_ascii=False, indent=2))
