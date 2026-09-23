#!/usr/bin/env python3
"""Freeze the user-selected 788-source demo pool; never rewrite v1 or raw data."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
RAW = Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def freeze(output):
    config_path = TASK / 'configs/demo_788_selection.json'
    catalog_path = TASK / 'results/cleaning_v2_review/source_catalog.jsonl'
    v1 = TASK / 'results/cleaning_v1'
    for name in ('candidates.jsonl', 'excluded.jsonl', 'selection.json'):
        if (output / name).exists():
            raise ValueError('Selection already exists; preserve the frozen version: ' + str(output / name))
    config = json.loads(config_path.read_text())
    rows = [json.loads(line) for line in catalog_path.read_text().splitlines() if line.strip()]
    by_id = {r['episode']: r for r in rows}
    assert len(rows) == len(by_id) == config['expected_original_recordings']
    excludes = config['exclusions']
    assert len(excludes) == 13 and set(excludes) <= set(by_id)
    findings = json.loads((v1 / 'cycle_review/findings.json').read_text())
    cycle_sources = {x['episode'] for x in findings['records']}
    ranges = json.loads((TASK / 'results/interface_review_20260914/documented_range_audit.json').read_text())
    j6 = next(x for x in ranges['streams']['lead_q']['fields'] if x['field'] == 'arm_L_joint6')
    assert len(cycle_sources) == 8 and len(j6['episodes_out_of_document_range']) == 5
    assert set(excludes) == cycle_sources | set(j6['episodes_out_of_document_range'])
    assignments = json.loads((v1 / 'source_split.json').read_text())['assignments']
    candidates, excluded = [], []
    totals = Counter()
    print('Verifying the selected source identities and derived pack hashes...', flush=True)
    for episode, row in sorted(by_id.items()):
        assert assignments[episode] == row['parent_split']
        if episode in excludes:
            excluded.append({**row, 'exclusion_reason': excludes[episode], 'raw_deleted': False})
            continue
        assert not row['confirmed_bad_base_masked']
        source = RAW / row['source_relative_path']
        before = source.stat()
        assert {'size_bytes': before.st_size, 'mtime_ns': before.st_mtime_ns} == row['source_fingerprint'], episode
        aligned = (v1 / 'aligned' / episode).with_suffix('.npz')
        metadata = aligned.with_suffix('.json')
        assert sha(aligned) == row['aligned_npz_sha256'], episode
        assert sha(metadata) == row['aligned_metadata_sha256'], episode
        meta = json.loads(metadata.read_text())
        signal = (v1 / 'signals' / episode).with_suffix('.npz')
        signal_stat = signal.stat()
        assert meta['signal_npz_fingerprint'] == {'size': signal_stat.st_size, 'mtime_ns': signal_stat.st_mtime_ns}, episode
        camera = (v1 / 'cameras' / episode).with_suffix('.npz')
        camera_sha = sha(camera)
        assert camera_sha == meta['camera_npz_sha256'], episode
        candidates.append({**row, 'selection_version': config['version'],
                           'raw_path': str(source), 'aligned_npz_path': str(aligned),
                           'signal_npz_path': str(signal), 'signal_npz_sha256': sha(signal),
                           'camera_npz_path': str(camera), 'camera_npz_sha256': camera_sha,
                           'technical_joint_frames': meta['joint_frame_candidate_valid_frames'],
                           'manual_cycle_trimming': False, 'training_approved': False})
        totals.update(frames=row['frames'], technical_joint_frames=meta['joint_frame_candidate_valid_frames'],
                      chunk_starts=row['v1_chunk_candidates'], source_bytes=before.st_size,
                      source_duration_ns=row['source_duration_ns'])
    assert len(candidates) == config['expected_candidates'] == 788
    split = dict(Counter(r['parent_split'] for r in candidates))
    assert split == {'train': 707, 'validation': 81}
    assert totals['technical_joint_frames'] == 931253 and totals['chunk_starts'] == 892784
    output.mkdir(parents=True, exist_ok=True)
    for name, records in (('candidates.jsonl', candidates), ('excluded.jsonl', excluded)):
        with (output / name).open('x') as stream:
            stream.write(''.join(encode(r) + '\n' for r in records))
    report = {'schema': 'f1_demo_candidate_selection_v1', 'version': config['version'],
              'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'complete_recordings_with_existing_v1_masks_not_cycle_reviewed_or_training_approved',
              'source_count': len(rows), 'candidate_count': len(candidates), 'excluded_count': len(excluded),
              'split_counts': split, 'batch_counts': dict(Counter(r['episode'].split('/')[0] for r in candidates)),
              'totals': dict(totals), 'raw_identity_check': 'size_and_mtime_not_full_raw_sha256',
              'derived_packs_hash_verified': True, 'training_approved': False,
              'raw_files_modified': 0, 'v1_files_modified': 0, 'annotations_modified': 0,
              'config_sha256': sha(config_path), 'source_catalog_sha256': sha(catalog_path),
              'candidates_sha256': sha(output / 'candidates.jsonl'), 'excluded_sha256': sha(output / 'excluded.jsonl')}
    with (output / 'selection.json').open('x') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_788_check')
    print(json.dumps(freeze(parser.parse_args().output), ensure_ascii=False, indent=2))
