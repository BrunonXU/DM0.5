#!/usr/bin/env python3
"""Derive a new source selection from a frozen parent, without altering raw data or masks."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def freeze(config_path, output):
    config = json.loads(config_path.read_text())
    parent = TASK / config['parent_directory']
    parent_selection = json.loads((parent / 'selection.json').read_text())
    for name in ('candidates', 'excluded'):
        if sha(parent / (name + '.jsonl')) != parent_selection[name + '_sha256']:
            raise ValueError('Parent manifest checksum differs: ' + name)
    if parent_selection['candidates_sha256'] != config['parent_candidates_sha256']:
        raise ValueError('Unexpected parent version')
    rows = read_rows(parent / 'candidates.jsonl')
    excluded = read_rows(parent / 'excluded.jsonl')
    by_id = {row['episode']: row for row in rows}
    added = config['additional_exclusions']
    if len(rows) != len(by_id) or len(rows) != config['expected_parent_count']:
        raise ValueError('Parent identities/count differ')
    if not set(added) <= set(by_id):
        raise ValueError('New exclusions must belong to the frozen parent')
    if len(rows) - len(added) != config['expected_candidates']:
        raise ValueError('Candidate count differs')
    if any((output / name).exists() for name in ('candidates.jsonl', 'excluded.jsonl', 'selection.json')):
        raise ValueError('Refusing to overwrite an existing frozen selection')
    for relative, expected in config['evidence_sha256'].items():
        if sha(TASK / relative) != expected:
            raise ValueError('Evidence checksum differs: ' + relative)

    candidates, totals = [], Counter()
    for episode, row in sorted(by_id.items()):
        if episode in added:
            excluded.append({**row, 'exclusion_reason': added[episode],
                             'excluded_in_version': config['version'], 'raw_deleted': False})
            continue
        source_stat = Path(row['raw_path']).stat()
        if {'size_bytes': source_stat.st_size, 'mtime_ns': source_stat.st_mtime_ns} != row['source_fingerprint']:
            raise ValueError('Raw source identity differs: ' + episode)
        for kind in ('aligned', 'signal', 'camera'):
            if sha(Path(row[kind + '_npz_path'])) != row[kind + '_npz_sha256']:
                raise ValueError('Derived pack checksum differs: ' + episode + ' ' + kind)
        if sha(Path(row['aligned_npz_path']).with_suffix('.json')) != row['aligned_metadata_sha256']:
            raise ValueError('Aligned metadata checksum differs: ' + episode)
        candidates.append({**row, 'selection_version': config['version'],
                           'parent_selection_version': row['selection_version'],
                           'terminal_arm_supervision_status': 'unresolved_held_targets_during_feedback_return',
                           'training_approved': False})
        totals.update(frames=row['frames'], technical_joint_frames=row['technical_joint_frames'],
                      chunk_starts_before_new_tail_handling=row['v1_chunk_candidates'],
                      source_bytes=source_stat.st_size, source_duration_ns=row['source_duration_ns'])
    excluded.sort(key=lambda row: row['episode'])
    ids = {row['episode'] for row in candidates}
    rejected_ids = {row['episode'] for row in excluded}
    if ids & rejected_ids or len(excluded) != len(rejected_ids):
        raise ValueError('Duplicate or intersecting source identities')
    if len(ids | rejected_ids) != parent_selection['source_count']:
        raise ValueError('Original source coverage differs')
    output.mkdir(parents=True, exist_ok=True)
    for name, records in (('candidates', candidates), ('excluded', excluded)):
        with (output / (name + '.jsonl')).open('x') as stream:
            for row in records:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    report = {
        'schema': 'f1_demo_candidate_selection_v2', 'version': config['version'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_count': parent_selection['source_count'], 'candidate_count': len(candidates),
        'excluded_count': len(excluded), 'additional_excluded_count': len(added),
        'split_counts': dict(Counter(row['parent_split'] for row in candidates)),
        'batch_counts': dict(Counter(row['episode'].split('/')[0] for row in candidates)),
        'totals_before_new_tail_handling': dict(totals),
        'parent_directory': config['parent_directory'],
        'parent_selection_sha256': sha(parent / 'selection.json'),
        'parent_candidates_sha256': sha(parent / 'candidates.jsonl'),
        'config_sha256': sha(config_path), 'script_sha256': sha(Path(__file__)),
        'candidates_sha256': sha(output / 'candidates.jsonl'),
        'excluded_sha256': sha(output / 'excluded.jsonl'),
        'raw_identity_check': 'size_and_mtime_not_full_raw_sha256',
        'derived_packs_hash_verified': True, 'training_approved': False,
        'raw_files_modified': 0, 'v1_files_modified': 0, 'annotations_modified': 0,
        'new_tail_masks_written': False,
        'scope': 'source_exclusions_only_terminal_arm_supervision_requires_separate_handling',
    }
    with (output / 'selection.json').open('x') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=TASK / 'configs/demo_778_selection.json')
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_778_tail')
    args = parser.parse_args()
    print(json.dumps(freeze(args.config, args.output), ensure_ascii=False, indent=2))
