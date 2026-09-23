#!/usr/bin/env python3
"""Join a frozen source subset to an existing verified mask manifest without copying arrays."""
import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from prepare_demo_revision import sha, read_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--parent-mask', type=Path, required=True)
    args = parser.parse_args()
    output = args.selection
    manifest_path = output / 'mask_manifest.jsonl'
    validation_path = output / 'mask_reuse_validation.json'
    if manifest_path.exists() or validation_path.exists():
        raise ValueError('Refusing to overwrite mask selection')
    selection = json.loads((output/'selection.json').read_text())
    assert sha(output/'candidates.jsonl') == selection['candidates_sha256']
    candidates = read_rows(output/'candidates.jsonl')
    parent = read_rows(args.parent_mask)
    masks = {r['episode']: r for r in parent}
    assert len(masks) == len(parent)
    assert len(candidates) == selection['candidate_count']
    assert {r['episode'] for r in candidates} <= masks.keys()
    result = []; totals = Counter()
    for source in candidates:
        row = masks[source['episode']]
        assert row['parent_split'] == source['parent_split']
        path = Path(row['aligned_npz_path'])
        assert sha(path) == row['aligned_npz_sha256']
        assert sha(path.with_suffix('.json')) == row['aligned_metadata_sha256']
        with np.load(path, allow_pickle=False) as pack:
            n, horizon = len(pack['time_ns']), source['chunk_horizon']
            future = pack['state_valid'].all(axis=1) & pack['action_candidate_valid'].all(axis=1)
            expected = np.zeros(n, dtype=bool)
            expected[:n-horizon+1] = pack['observation_candidate_valid'][:n-horizon+1] & np.lib.stride_tricks.sliding_window_view(future, horizon).all(axis=1)
            assert np.array_equal(expected, pack['chunk_start_candidate_valid'])
            assert np.array_equal(future, pack['future_signal_action_candidate_valid'])
            assert np.array_equal(pack['joint_frame_candidate_valid'], pack['observation_candidate_valid'] & pack['action_candidate_valid'].all(axis=1))
            assert int(expected.sum()) == row['chunk_starts_after']
            assert int(pack['joint_frame_candidate_valid'].sum()) == row['joint_valid_frames_after']
            assert not pack['training_approved'].item()
            totals.update(frames_retained=n, joint_valid_frames=row['joint_valid_frames_after'], chunk_starts=row['chunk_starts_after'])
        result.append(row)
    manifest_path.write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in result))
    report = {'selection_version': selection['version'], 'candidate_count': len(result),
              'parent_mask_manifest_sha256': sha(args.parent_mask), 'candidates_sha256': selection['candidates_sha256'],
              'mask_manifest_sha256': sha(manifest_path), 'script_sha256': sha(Path(__file__)),
              'split_counts': dict(Counter(r['parent_split'] for r in result)), 'totals': dict(totals),
              'all_pack_and_metadata_hashes_verified': True, 'all_chunk_windows_verified': True,
              'all_masks_reused_unchanged': True, 'source_and_mask_identity_join_verified': True,
              'training_approved': False, 'complete_task_cycles_verified': False,
              'note': 'Numeric validity counts only; semantic review queue must be resolved separately.'}
    validation_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
