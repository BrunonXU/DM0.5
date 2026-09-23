#!/usr/bin/env python3
"""Independently validate candidate packs against extracted source arrays.

This checks the complete output, without importing the assembler or reading MCAP
payloads again. Passing is a technical integrity result, not training approval.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np
import yaml

TASK = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def same(actual, expected, message):
    require(np.array_equal(actual, expected, equal_nan=True), message)


def read_json(path):
    return json.loads(path.read_text())


def rows_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def validate_episode(episode, root, source, cfg, manifest, windows, review, split, known_bad_base):
    pack = root / 'aligned' / episode
    meta = read_json(pack.with_suffix('.json'))
    require(meta == manifest, 'Pack metadata differs from episode manifest')
    require(meta['episode'] == episode, 'Episode identity mismatch')
    require(meta['split'] == split, 'Episode split differs from source split')
    require(meta['training_approved'] is False, 'Metadata training approval must be false')
    require(meta['cycle_boundaries_finalized'] is False, 'Unexpected finalized cycle boundaries')
    source_stat = (source / meta['source_relative_path']).stat()
    require((source_stat.st_size, source_stat.st_mtime_ns) ==
            (meta['source_size_bytes'], meta['source_mtime_ns']), 'Raw source fingerprint changed')
    signal_path = (root / 'signals' / episode).with_suffix('.npz')
    camera_path = (root / 'cameras' / episode).with_suffix('.npz')
    sm, cm = read_json(signal_path.with_suffix('.json')), read_json(camera_path.with_suffix('.json'))
    signal_stat = signal_path.stat()
    require(meta['signal_npz_fingerprint'] == sm['npz_fingerprint'] ==
            {'size': signal_stat.st_size, 'mtime_ns': signal_stat.st_mtime_ns},
            'Signal intermediate fingerprint mismatch')
    require(meta['camera_npz_sha256'] == cm['npz_sha256'] ==
            hashlib.sha256(camera_path.read_bytes()).hexdigest(), 'Camera intermediate checksum mismatch')
    for item in (sm, cm):
        require(item['status'] == 'complete' and item['episode'] == episode,
                'Intermediate is incomplete or belongs to another episode')
        require((item['start_ns'], item['end_ns']) == (meta['start_ns'], meta['end_ns']),
                'Intermediate source time bounds mismatch')
        require(item['source_relative_path'] == meta['source_relative_path'],
                'Intermediate source path mismatch')

    with np.load(pack.with_suffix('.npz'), allow_pickle=False) as data, \
            np.load(signal_path, allow_pickle=False) as signals, \
            np.load(camera_path, allow_pickle=False) as cameras:
        n = meta['frames']
        shapes = {
            'time_ns': ((n,), np.int64), 'state': ((n, 21), np.float32),
            'action': ((n, 19), np.float32), 'state_valid': ((n, 21), np.bool_),
            'action_candidate_valid': ((n, 19), np.bool_),
            'camera_source_indices': ((n, 3), np.int64),
            'camera_log_age_s': ((n, 3), np.float32),
            'camera_timestamp_candidate_valid': ((n, 3), np.bool_),
            'observation_candidate_valid': ((n,), np.bool_),
            'joint_frame_candidate_valid': ((n,), np.bool_),
            'future_signal_action_candidate_valid': ((n,), np.bool_),
            'chunk_start_candidate_valid': ((n,), np.bool_),
            'training_approved': ((), np.bool_),
        }
        require(set(data.files) == set(shapes) | {'source_relative_path'}, 'Unexpected pack fields')
        for key, (shape, dtype) in shapes.items():
            require(data[key].shape == shape and data[key].dtype == dtype,
                    f'{key}: incorrect shape or dtype')
        require(data['source_relative_path'].shape == () and
                data['source_relative_path'].dtype.kind == 'U' and
                str(data['source_relative_path']) == meta['source_relative_path'],
                'NPZ source identity mismatch')
        require(not data['training_approved'].item(), 'NPZ training approval must be false')
        require((meta['state_dim'], meta['action_dim']) == (21, 19), 'Metadata dimensions mismatch')
        hz = cfg['grid']['frequency_hz']
        require(isinstance(hz, int) and hz > 0, 'Validator requires positive integer grid frequency')
        require(meta['grid_hz'] == hz, 'Metadata grid frequency mismatch')
        grid = data['time_ns']
        require(n > 0 and grid[0] == meta['start_ns'] and np.all(np.diff(grid) > 0),
                'Grid must start at record start and strictly increase')
        # Integer arithmetic is independent of the assembler float/rint grid.
        numerator = np.arange(n, dtype=np.int64) * 1_000_000_000
        q, r = np.divmod(numerator, hz)
        rounded = q + ((r * 2 > hz) | ((r * 2 == hz) & (q % 2 == 1)))
        same(grid - meta['start_ns'], rounded, 'Grid differs from rounded regular schedule')
        require(grid[-1] <= meta['end_ns'], 'Grid extends beyond source end')
        next_q, next_r = divmod(n * 1_000_000_000, hz)
        next_offset = next_q + int(next_r * 2 > hz or (next_r * 2 == hz and next_q % 2))
        require(meta['start_ns'] + next_offset > meta['end_ns'], 'Grid omits a final in-range frame')

        expected_values, expected_masks = {}, {}
        max_signal_age = round(cfg['grid']['max_signal_age_s'] * 1e9)
        for name, value_key in [('hal', 'hal_q'), ('lead', 'lead_q'),
                                ('left_cmd', 'left_cmd'), ('right_cmd', 'right_cmd'),
                                ('left_state', 'left_state'), ('right_state', 'right_state'),
                                ('base', 'base_v')]:
            stamps, raw = signals[name + '_t_ns'], signals[value_key]
            require(stamps.dtype == np.int64 and np.all(np.diff(stamps) > 0),
                    f'{name}: source times are not strictly increasing int64')
            require(len(stamps) > 0 and len(raw) == len(stamps), f'{name}: empty/mismatched stream')
            indices = np.searchsorted(stamps, grid, side='right') - 1
            chosen = np.maximum(indices, 0)
            age = grid - stamps[chosen]
            raw = raw.reshape(len(raw), -1)
            valid = ((indices >= 0) & (age >= 0) & (age <= max_signal_age))[:, None] & np.isfinite(raw[chosen])
            values = raw[chosen].astype(np.float32)
            values[~valid] = np.nan
            expected_values[name], expected_masks[name] = values, valid
        state_fields = [('hal', slice(0, 14)), ('left_state', slice(None)),
                        ('right_state', slice(None)), ('hal', slice(14, 19))]
        action_fields = [('lead', slice(None)), ('left_cmd', slice(None)),
                         ('right_cmd', slice(None)), ('base', slice(None))]
        state_expected = np.concatenate([expected_values[k][:, c] for k, c in state_fields], axis=1)
        state_mask = np.concatenate([expected_masks[k][:, c] for k, c in state_fields], axis=1)
        action_expected = np.concatenate([expected_values[k][:, c] for k, c in action_fields], axis=1)
        action_mask = np.concatenate([expected_masks[k][:, c] for k, c in action_fields], axis=1)
        require(meta['confirmed_bad_base_masked'] == (episode in known_bad_base),
                'Confirmed base quarantine differs from known episode')
        if episode in known_bad_base:
            action_mask[:, 16:] = False
        same(data['state'], state_expected, 'State21 column mapping/value mismatch against signals')
        same(data['action'], action_expected, 'Action19 column mapping/value mismatch against signals')
        same(data['state_valid'], state_mask, 'State freshness/finite validity mismatch')
        same(data['action_candidate_valid'], action_mask, 'Action validity/quarantine mismatch')
        require(np.all(np.isfinite(data['state'][state_mask])) and
                np.all(np.isfinite(data['action'][action_mask])), 'Valid state/action contains nonfinite value')

        image_valid = np.zeros((n, 3), dtype=bool)
        max_camera_age = round(cfg['grid']['max_camera_log_age_s'] * 1e9)
        reused = Counter()
        for column, name in enumerate(['head', 'left', 'right']):
            stamps, candidate = cameras[name + '_log_ns'], cameras[name + '_timestamp_candidate_valid']
            require(stamps.dtype == np.int64 and np.all(np.diff(stamps) > 0),
                    f'{name}: camera log times are not strictly increasing int64')
            require(candidate.dtype == np.bool_ and candidate.shape == stamps.shape and candidate.any(),
                    f'{name}: camera candidate mask malformed/empty')
            # Search all original frames, then walk back to the most recent valid
            # source frame. This independently preserves original frame indices.
            previous_valid = np.maximum.accumulate(np.where(candidate, np.arange(len(stamps)), -1))
            latest_raw = np.searchsorted(stamps, grid, side='right') - 1
            expected_indices = np.where(latest_raw >= 0, previous_valid[np.maximum(latest_raw, 0)], -1)
            same(data['camera_source_indices'][:, column], expected_indices,
                 f'{name}: selected camera frame is not last causal candidate')
            first_valid = np.flatnonzero(candidate)[0]
            reference_indices = np.where(expected_indices >= 0, expected_indices, first_valid)
            ages = grid - stamps[reference_indices]
            image_valid[:, column] = (expected_indices >= 0) & (ages >= 0) & (ages <= max_camera_age)
            same(data['camera_log_age_s'][:, column], (ages / 1e9).astype(np.float32),
                 f'{name}: camera ages disagree with source log timestamps')
            reused[name] = int(np.count_nonzero(image_valid[1:, column] & image_valid[:-1, column] &
                                              (expected_indices[1:] == expected_indices[:-1])))
        same(data['camera_timestamp_candidate_valid'], image_valid, 'Camera freshness mask mismatch')
        observation_valid = state_mask.all(axis=1) & image_valid.all(axis=1)
        joint_valid = observation_valid & action_mask.all(axis=1)
        future_valid = state_mask.all(axis=1) & action_mask.all(axis=1)
        same(data['observation_candidate_valid'], observation_valid, 'Observation mask mismatch')
        same(data['joint_frame_candidate_valid'], joint_valid, 'Joint frame mask mismatch')
        same(data['future_signal_action_candidate_valid'], future_valid, 'Future signal/action mask mismatch')
        horizon = cfg['chunks']['horizon']
        require(meta['chunk_horizon'] == horizon, 'Metadata chunk horizon mismatch')
        chunks = np.zeros(n, dtype=bool)
        if n >= horizon:
            chunks[:n - horizon + 1] = np.lib.stride_tricks.sliding_window_view(future_valid, horizon).all(axis=1)
        chunks &= image_valid.all(axis=1)
        same(data['chunk_start_candidate_valid'], chunks, 'Chunk crosses invalid signal/action, lacks current image, or pads future')
        if episode in known_bad_base:
            require(not action_mask[:, 16:].any() and not chunks.any(), 'Known bad base episode has usable base/chunks')
        # Count admitted starts with a later bad camera; a model action chunk
        # only observes images at its first step, so those starts are permitted.
        strict_future_images = np.zeros(n, dtype=bool)
        if n >= horizon:
            strict_future_images[:n - horizon + 1] = np.lib.stride_tricks.sliding_window_view(image_valid.all(axis=1), horizon).all(axis=1)
        current_only_image_starts = int(np.count_nonzero(chunks & ~strict_future_images))

        active = np.flatnonzero(future_valid)
        groups = np.split(active, np.flatnonzero(np.diff(active) > 1) + 1) if len(active) else []
        require(len(windows) == len(groups) == meta['continuous_windows'], 'Continuous window count mismatch')
        for number, (row, indices) in enumerate(zip(windows, groups)):
            a, b = int(indices[0]), int(indices[-1]) + 1
            expected = {'window': number, 'source_start_frame': a, 'source_stop_frame_exclusive': b,
                        'frames': b - a, 'chunk_starts': int(chunks[a:b].sum())}
            for key, value in expected.items():
                require(int(row[key]) == value, f'Window {number} {key} mismatch')
            require(row['split'] == split and row['training_approved'] == 'False', 'Window split/approval mismatch')
            require(row['kind'] == 'continuous_signal_action_window_observation_starts_masked_not_confirmed_cycle',
                    'Window misrepresented as confirmed cycle')
            require(abs(float(row['start_s']) - (grid[a] - meta['start_ns']) / 1e9) < 1e-9 and
                    abs(float(row['last_frame_s']) - (grid[b - 1] - meta['start_ns']) / 1e9) < 1e-9,
                    'Window timestamps mismatch')
        counters = {'frames': n, 'technical_joint_frames': int(joint_valid.sum()),
                    'technical_chunk_starts': int(chunks.sum()), 'technical_continuous_windows': len(groups),
                    'known_bad_base_episodes_masked': int(meta['confirmed_bad_base_masked']),
                    'multiple_gripper_event_review_candidates': int(meta['multiple_event_review_candidate'])}
        for key, value in [('signal_observation_valid_frames', int(state_mask.all(axis=1).sum())),
                           ('observation_candidate_valid_frames', int(observation_valid.sum())),
                           ('joint_frame_candidate_valid_frames', int(joint_valid.sum())),
                           ('chunk_candidate_starts', int(chunks.sum()))]:
            require(meta[key] == value, f'Metadata {key} mismatch')
        for key, text in review.items():
            value = meta[key]
            expected = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else '' if value is None else str(value)
            require(text == expected, f'Review queue {key} differs from episode metadata')
    return counters, reused, current_only_image_starts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TASK / 'results/cleaning_v1')
    parser.add_argument('--overrides', type=Path, default=TASK / 'configs/review_overrides.yaml',
                        help='Human-reviewed label quarantine used for this assembly')
    args = parser.parse_args()
    root = args.output.resolve()
    started = time.monotonic()
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'output': str(root),
              'success': False, 'training_approved': False,
              'scope': 'All aligned values/masks checked against extracted arrays; no MCAP payload reread or robot/visual validation.',
              'checks': ['array schema, exact rational grid and finite valid values',
                         'all state21/action19 values and masks against raw signal intermediates',
                         'causal original camera indices, candidate flags and exact stored ages',
                         'full 50-step signal/action validity, current images only, no end padding',
                         'known base-label quarantine and false training approval',
                         'manifest, source split, windows, review queue and summary reconciliation',
                         'raw source size/mtime and intermediate fingerprints'],
              'errors': []}
    totals, reused = Counter(), Counter()
    completed, current_only = 0, 0
    output_digest = hashlib.sha256()
    try:
        cfg = yaml.safe_load((root / 'config_snapshot.yaml').read_text())
        overrides_bytes = args.overrides.read_bytes()
        overrides = yaml.safe_load(overrides_bytes).get('episodes', {})
        known_bad_base = {episode for episode, override in overrides.items()
                          if override.get('estimated_base_label_status') == 'requires_per_cycle_relabel_before_supervision'}
        report['review_overrides_sha256'] = hashlib.sha256(overrides_bytes).hexdigest()
        summary = read_json(root / 'summary.json')
        require((root / 'review_overrides_snapshot.yaml').read_bytes() == overrides_bytes,
                'Review overrides changed since assembly; rebuild before validation')
        require(summary['review_overrides_sha256'] == report['review_overrides_sha256'],
                'Review overrides snapshot checksum differs from summary')
        require(summary['config_sha256'] == hashlib.sha256((root / 'config_snapshot.yaml').read_bytes()).hexdigest(),
                'Configuration snapshot checksum differs from summary')
        report['config_sha256'] = summary['config_sha256']
        require(cfg['state_dim'] == 21 and cfg['action_dim'] == 19 and cfg['right_side_freeze'] is False,
                'Configuration does not describe confirmed both-arm 21-to-19 design')
        require(cfg['chunks']['horizon'] == 50 and cfg['chunks']['pad_unknown_future_actions'] is False,
                'Unexpected horizon or padding configuration')
        require(cfg['approval']['automatic_training_approval'] is False, 'Automatic training approval enabled')
        manifest_rows = [json.loads(line) for line in (root / 'episode_manifest.jsonl').read_text().splitlines()]
        manifest = {row['episode']: row for row in manifest_rows}
        require(len(manifest) == len(manifest_rows), 'Duplicate manifest episodes')
        require(known_bad_base <= set(manifest), 'A confirmed quarantined episode is missing from full output')
        review_rows = rows_csv(root / 'review_queue.csv')
        review = {row['episode']: row for row in review_rows}
        require(len(review) == len(review_rows) and set(review) == set(manifest), 'Review queue episode coverage mismatch')
        windows = defaultdict(list)
        for row in rows_csv(root / 'technical_windows.csv'):
            windows[row['episode']].append(row)
        require(set(windows) <= set(manifest), 'Windows reference unknown episodes')
        split = read_json(root / 'source_split.json')
        require(split['unit'] == 'original_recording' and split['child_cycles_share_parent_split'] is True,
                'Split unit does not preserve source recording')
        require(set(split['assignments']) == set(manifest), 'Source split episode coverage mismatch')
        originals = [json.loads(line) for line in (TASK / cfg['source_audit'] / 'mcap_audit/episodes.jsonl').read_text().splitlines()]
        require({row['episode'] for row in originals} == set(manifest), 'Output misses or adds source-audit episodes')
        grouped = defaultdict(list)
        for row in originals:
            grouped[row['episode'].split('/')[0]].append((row['start_ns'], row['episode']))
        for group in grouped.values():
            ordered = sorted(group)
            validation_count = max(1, math.ceil(len(ordered) * cfg['split']['validation_fraction']))
            for index, (_, episode) in enumerate(ordered):
                require(split['assignments'][episode] == ('validation' if index >= len(ordered) - validation_count else 'train'),
                        'Split is not last temporal fraction within each source batch')
        actual_npz = {str(path.relative_to(root / 'aligned').with_suffix('')) for path in (root / 'aligned').glob('*/*.npz')}
        actual_json = {str(path.relative_to(root / 'aligned').with_suffix('')) for path in (root / 'aligned').glob('*/*.json')}
        require(actual_npz == actual_json == set(manifest), 'Aligned file coverage differs from manifest')
        for index, episode in enumerate(sorted(manifest)):
            try:
                counts, repeat_counts, extra = validate_episode(episode, root, Path(cfg['source']), cfg,
                                                               manifest[episode], windows[episode], review[episode],
                                                               split['assignments'][episode], known_bad_base)
                totals.update(counts)
                reused.update(repeat_counts)
                current_only += extra
                completed += 1
                output_digest.update(episode.encode())
                output_digest.update(hashlib.sha256((root / 'aligned' / episode).with_suffix('.npz').read_bytes()).digest())
            except Exception as exc:
                report['errors'].append({'episode': episode, 'type': type(exc).__name__, 'error': str(exc)})
            if (index + 1) % 100 == 0 or index + 1 == len(manifest):
                print(f'Validated {index + 1}/{len(manifest)}; errors={len(report["errors"])}', flush=True)
        if not report['errors']:
            for key, value in totals.items():
                require(summary[key] == value, f'Summary {key} mismatch: {summary[key]} vs {value}')
            require(summary['requested_episodes'] == summary['assembled_episodes'] == completed,
                    'Summary episode counts mismatch')
            require(summary['split_counts'] == dict(Counter(split['assignments'].values())), 'Summary split counts mismatch')
            require(summary['errors'] == summary['approved_training_episodes'] == summary['raw_files_modified'] == 0,
                    'Unexpected summary errors/approval/source mutation')
            require(read_json(root / 'errors.json') == [], 'Builder reported errors')
            require(totals['known_bad_base_episodes_masked'] == len(known_bad_base),
                    'Confirmed quarantine count differs from review overrides')
            report['success'] = True
        report.update({'expected_episodes': len(manifest), 'validated_episodes': completed, 'totals': dict(totals),
                       'split_counts': dict(Counter(split['assignments'].values())),
                       'adjacent_valid_grid_pairs_reusing_same_source_camera_frame': dict(reused),
                       'chunk_starts_with_valid_current_images_and_at_least_one_invalid_future_image': current_only,
                       'aligned_npz_collection_sha256': output_digest.hexdigest(),
                       'known_bad_base_episodes': sorted(known_bad_base)})
    except Exception as exc:
        report['errors'].append({'type': type(exc).__name__, 'error': str(exc)})
    report['elapsed_s'] = round(time.monotonic() - started, 3)
    temporary = root / 'validation.json.tmp'
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(root / 'validation.json')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
