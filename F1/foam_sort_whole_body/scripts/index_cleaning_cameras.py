#!/usr/bin/env python3
"""Index every camera timestamp into reversible, log-reference candidates.

Reads CompressedImage CDR timestamp prefixes, never JPEG pixels. Originals are
opened read-only. Candidate validity is a timing heuristic, not exposure-time
synchronization, image quality, or task-success certification.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time

import numpy as np
from mcap.reader import make_reader

from audit_camera_timestamps import header_ns, schema_supported

SOURCE = Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw')
OUTPUT = Path(__file__).resolve().parents[1] / 'results/cleaning_v1/cameras'
CAMERAS = {
    'head': '/camera/head/color/image_raw/compressed',
    'left': '/camera/left_wrist/color/image_raw/compressed',
    'right': '/camera/right_wrist/color/image_raw/compressed',
}
SETTINGS = {
    'schema_version': 1,
    'steady_after_start_ns': 5_000_000_000,
    'steady_min_frames': 15,
    'steady_min_span_ns': 500_000_000,
    'offset_deviation_limit_ns': 250_000_000,
    'gap_limit_ns': 100_000_000,
    'continuous_candidate_min_span_ns': 500_000_000,
}
UNAVAILABLE_HEADER = np.iinfo(np.int64).min


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(path):
    st = path.stat()
    return {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_npz(path, arrays):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def distribution_ns(values):
    values = np.asarray(values, dtype=np.float64) / 1e9
    if not len(values):
        return {'count': 0}
    return {'count': int(len(values)), 'min_s': float(values.min()),
            'p01_s': float(np.quantile(values, .01)), 'p05_s': float(np.quantile(values, .05)),
            'median_s': float(np.median(values)), 'p95_s': float(np.quantile(values, .95)),
            'p99_s': float(np.quantile(values, .99)), 'max_s': float(values.max()),
            'mean_s': float(values.mean())}


def intervals(mask, logs, headers):
    indices = np.flatnonzero(mask)
    if not len(indices):
        return []
    cuts = np.flatnonzero(np.diff(indices) != 1) + 1
    result = []
    for group in np.split(indices, cuts):
        a, b = int(group[0]), int(group[-1])
        result.append({'start_index': a, 'end_index_inclusive': b,
                       'count': b - a + 1, 'start_log_ns': int(logs[a]),
                       'end_log_ns': int(logs[b]), 'start_header_ns': int(headers[a]),
                       'end_header_ns': int(headers[b])})
    return result


def gap_events(values, logs, headers, legal_pair=None):
    delta = np.diff(values)
    mask = delta > SETTINGS['gap_limit_ns']
    if legal_pair is not None:
        mask &= legal_pair
    return [{'previous_index': int(i), 'next_index': int(i + 1),
             'previous_log_ns': int(logs[i]), 'next_log_ns': int(logs[i + 1]),
             'previous_header_ns': int(headers[i]), 'next_header_ns': int(headers[i + 1]),
             'gap_ns': int(delta[i]), 'gap_s': float(delta[i] / 1e9)}
            for i in np.flatnonzero(mask)]


def analyze_camera(logs, headers, legal, start_ns, decode_errors):
    count = len(logs)
    offsets = np.zeros(count, dtype=np.int64)
    offsets[legal] = headers[legal] - logs[legal]
    steady = legal & (logs >= start_ns + SETTINGS['steady_after_start_ns'])
    steady_logs = logs[steady]
    sufficient = (len(steady_logs) >= SETTINGS['steady_min_frames'] and
                  int(steady_logs[-1] - steady_logs[0]) >= SETTINGS['steady_min_span_ns'])
    baseline_mask = steady if sufficient else legal
    median_ns = float(np.median(offsets[baseline_mask])) if np.any(baseline_mask) else None
    offset_outlier = np.zeros(count, dtype=bool)
    if median_ns is not None:
        offset_outlier[legal] = np.abs(offsets[legal].astype(np.float64) - median_ns) > SETTINGS['offset_deviation_limit_ns']
    # Reject the latter member of a duplicate/reversal and keep rejecting until
    # headers advance beyond the previously observed high-water timestamp.
    nonincreasing = np.zeros(count, dtype=bool)
    duplicate = np.zeros(count, dtype=bool)
    backward = np.zeros(count, dtype=bool)
    legal_indices = np.flatnonzero(legal)
    if len(legal_indices) > 1:
        legal_headers = headers[legal_indices]
        current = legal_indices[1:]
        delta = np.diff(legal_headers)
        duplicate[current] = delta == 0
        backward[current] = delta < 0
        nonincreasing[current] = legal_headers[1:] <= np.maximum.accumulate(legal_headers[:-1])
    candidates = legal & ~offset_outlier & ~nonincreasing
    if median_ns is None:
        candidates[:] = False
    first_continuous = None
    run_start = None
    for i in range(count):
        if not candidates[i]:
            run_start = None
            continue
        if run_start is None or (i and logs[i] - logs[i - 1] > SETTINGS['gap_limit_ns']):
            run_start = i
        if logs[i] - logs[run_start] >= SETTINGS['continuous_candidate_min_span_ns']:
            first_continuous = int(logs[run_start])
            break
    legal_pairs = legal[:-1] & legal[1:]
    # Avoid int64 subtraction overflow for unavailable-header sentinels.
    safe_headers = headers.copy()
    safe_headers[~legal] = 0
    info = {
        'count': count,
        'header_legal_count': int(legal.sum()),
        'candidate_valid_count': int(candidates.sum()),
        'candidate_invalid_count': int((~candidates).sum()),
        'first_log_ns': int(logs[0]) if count else None,
        'last_log_ns': int(logs[-1]) if count else None,
        'steady_sample_count': int(steady.sum()),
        'baseline_sample_count': int(baseline_mask.sum()),
        'baseline_method': 'after_episode_start_5s_median' if sufficient else 'fallback_all_legal_frames_median',
        'steady_insufficient_fallback': not sufficient,
        'fallback_reason': None if sufficient else 'Fewer than 15 legal frames or less than 0.5 s log span after episode start + 5 s; use all legal frames, or no candidates when none are legal.',
        'steady_median_header_minus_log_ns': median_ns,
        'steady_median_header_minus_log_s': median_ns / 1e9 if median_ns is not None else None,
        'header_minus_log_distribution': distribution_ns(offsets[legal]),
        'after_start_5s_header_minus_log_distribution': distribution_ns(offsets[steady]),
        'candidate_header_minus_log_distribution': distribution_ns(offsets[candidates]),
        'anomaly_counts': {'invalid_header': int((~legal).sum()),
                           'offset_outlier': int(offset_outlier.sum()),
                           'duplicate_header_step': int(duplicate.sum()),
                           'backward_header_step': int(backward.sum()),
                           'header_not_above_previous_max': int(nonincreasing.sum())},
        'anomaly_intervals': {'invalid_header': intervals(~legal, logs, headers),
                              'offset_outlier': intervals(offset_outlier, logs, headers),
                              'duplicate_header_step': intervals(duplicate, logs, headers),
                              'backward_header_step': intervals(backward, logs, headers),
                              'header_not_above_previous_max': intervals(nonincreasing, logs, headers),
                              'candidate_invalid': intervals(~candidates, logs, headers)},
        'log_frame_interval_distribution': distribution_ns(np.diff(logs)),
        'header_frame_interval_distribution': distribution_ns(np.diff(safe_headers)[legal_pairs]),
        'log_gap_events': gap_events(logs, logs, headers),
        'header_gap_events': gap_events(safe_headers, logs, headers, legal_pairs),
        'first_continuous_candidate_valid_start_log_ns': first_continuous,
        'first_continuous_candidate_valid_start_from_episode_s': (first_continuous - start_ns) / 1e9 if first_continuous is not None else None,
        'decode_errors': decode_errors,
    }
    return candidates, info


def compact_result(meta, resumed, elapsed):
    return {'episode': meta['episode'], 'status': 'ok', 'resumed': resumed,
            'elapsed_s': elapsed, 'source_size_bytes': meta['source_size_bytes'],
            'cameras': {name: {key: camera[key] for key in (
                'count', 'candidate_valid_count', 'candidate_invalid_count',
                'steady_insufficient_fallback', 'anomaly_counts')}
                | {'log_gap_count': len(camera['log_gap_events']),
                   'header_gap_count': len(camera['header_gap_events'])}
                for name, camera in meta['cameras'].items()}}


def process_episode(task):
    path_text, source_text, output_text, run_config = task
    path, source, output = Path(path_text), Path(source_text), Path(output_text)
    episode = str(path.parent.relative_to(source))
    target = output / path.parent.parent.name / path.parent.name
    npz_path, json_path = target.with_suffix('.npz'), target.with_suffix('.json')
    started = time.monotonic()
    try:
        fingerprint = source_fingerprint(path)
        if json_path.is_file() and npz_path.is_file():
            try:
                meta = json.loads(json_path.read_text(encoding='utf-8'))
                if (meta.get('source_fingerprint') == fingerprint and
                    meta.get('source_relative_path') == str(path.relative_to(source)) and
                    meta.get('run_config') == run_config and
                    meta.get('npz_sha256') == file_sha256(npz_path) and meta.get('status') == 'complete'):
                    return compact_result(meta, True, time.monotonic() - started)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        collected = {name: {'log': [], 'header': [], 'legal': [], 'errors': []} for name in CAMERAS}
        by_topic = {topic: name for name, topic in CAMERAS.items()}
        with path.open('rb') as stream:
            reader = make_reader(stream)
            summary = reader.get_summary()
            if summary is None or summary.statistics is None or not summary.chunk_indexes:
                raise ValueError('Indexed MCAP summary and whole-record statistics are required')
            start_ns, end_ns = summary.statistics.message_start_time, summary.statistics.message_end_time
            expected_counts = {name: sum(summary.statistics.channel_message_counts.get(channel.id, 0)
                                        for channel in summary.channels.values() if channel.topic == topic)
                               for name, topic in CAMERAS.items()}
            schema_cache = {}
            for schema, channel, message in reader.iter_messages(topics=list(CAMERAS.values()), log_time_order=True):
                name = by_topic[channel.topic]
                dest = collected[name]
                stamp, legal = UNAVAILABLE_HEADER, False
                try:
                    if channel.id not in schema_cache:
                        schema_cache[channel.id] = schema_supported(schema, channel)
                    if not schema_cache[channel.id]:
                        raise ValueError('Unsupported CompressedImage schema/encoding')
                    stamp = header_ns(message.data)
                    if stamp <= 0:
                        raise ValueError('Nonpositive header stamp is not a usable dataset timestamp')
                    legal = True
                except (ValueError, UnicodeError, struct.error) as exc:
                    # Preserve a readable raw value even for an invalid nanosec
                    # field. Unreadable prefixes are retained verbatim in JSON.
                    if len(message.data) >= 12 and message.data[:4] == b'\x00\x01\x00\x00':
                        sec, nanos = struct.unpack_from('<iI', message.data, 4)
                        stamp = sec * 1_000_000_000 + nanos
                    dest['errors'].append({'index': len(dest['log']), 'error': str(exc),
                                           'raw_cdr_timestamp_prefix_hex': message.data[:12].hex(),
                                           'log_ns': message.log_time})
                dest['log'].append(message.log_time)
                dest['header'].append(stamp)
                dest['legal'].append(legal)
        if source_fingerprint(path) != fingerprint:
            raise RuntimeError('Source fingerprint changed during reading; outputs not committed')
        arrays, camera_meta = {}, {}
        for name, values in collected.items():
            logs = np.asarray(values['log'], dtype=np.int64)
            headers = np.asarray(values['header'], dtype=np.int64)
            legal = np.asarray(values['legal'], dtype=bool)
            if len(logs) != expected_counts[name]:
                raise ValueError(f'{name}: scanned count {len(logs)} != summary count {expected_counts[name]}')
            candidates, info = analyze_camera(logs, headers, legal, start_ns, values['errors'])
            arrays.update({f'{name}_log_ns': logs, f'{name}_header_ns': headers,
                           f'{name}_timestamp_candidate_valid': candidates})
            camera_meta[name] = {'topic': CAMERAS[name], **info}
        atomic_npz(npz_path, arrays)
        meta = {'status': 'complete', 'generated_at_utc': utc_now(), 'episode': episode,
                'source_relative_path': str(path.relative_to(source)),
                'source_fingerprint': fingerprint, 'source_size_bytes': fingerprint['size_bytes'],
                'source_mtime_ns': fingerprint['mtime_ns'], 'start_ns': start_ns, 'end_ns': end_ns,
                'start_end_basis': 'MCAP summary statistics over all recorded messages, inclusive endpoints',
                'record_message_count': summary.statistics.message_count,
                'run_config': run_config, 'npz_relative_path': str(npz_path.relative_to(output)),
                'npz_sha256': file_sha256(npz_path), 'elapsed_s': time.monotonic() - started,
                'timestamp_order': 'Ascending MCAP log time; all original frame timestamps retained',
                'unavailable_header_sentinel': int(UNAVAILABLE_HEADER),
                'offset_sign': 'header_ns - log_ns; not physical sensor latency',
                'candidate_semantics': 'Legal positive header; absolute header-log deviation from baseline median <= 0.25 s; header exceeds all previous legal header stamps. This is a conservative candidate for use on the log reference axis, not exposure synchronization or success certification.',
                'continuous_candidate_definition': 'All frames candidate-valid, each adjacent log gap <= 0.1 s, first-to-last log span >= 0.5 s.',
                'source_modified': False, 'jpeg_decoded': False, 'cameras': camera_meta}
        # JSON is the commit marker; resume also validates the NPZ checksum.
        atomic_json(json_path, meta)
        return compact_result(meta, False, time.monotonic() - started)
    except Exception as exc:
        return {'episode': episode, 'status': 'error', 'error_type': type(exc).__name__,
                'error': str(exc), 'elapsed_s': time.monotonic() - started}


def select_paths(source, selectors):
    paths = sorted(source.glob('*/episode_*/*.mcap'))
    parent_counts = collections.Counter(path.parent for path in paths)
    if any(count != 1 for count in parent_counts.values()):
        raise ValueError('Expected exactly one MCAP per episode; split recordings require explicit merging')
    if not selectors:
        return paths
    aliases = {'all_data': 'all_data_derived', 'all_data1': 'all_data_1_derived',
               'all_data2': 'all_data_2_derived', 'all_data3': 'all_data_3_derived'}
    selected = set()
    for selector in selectors:
        parts = selector.strip('/').split('/')
        if len(parts) == 2:
            parts[0] = aliases.get(parts[0], parts[0])
            if parts[1].isdigit():
                parts[1] = f'episode_{int(parts[1]):06d}'
            selector = '/'.join(parts)
        matches = [path for path in paths if str(path.parent.relative_to(source)) == selector
                   or str(path.relative_to(source)) == selector]
        if len(matches) != 1:
            raise ValueError(f'--episode {selector!r}: expected exactly one matching MCAP, got {len(matches)}')
        selected.add(matches[0])
    return sorted(selected)


def write_summary(args, config, paths, results, started, status):
    successful = [row for row in results if row['status'] == 'ok']
    fresh = [row for row in successful if not row['resumed']]
    elapsed = time.monotonic() - started
    cameras = {}
    for name in CAMERAS:
        rows = [row['cameras'][name] for row in successful]
        cameras[name] = {key: sum(row[key] for row in rows) for key in (
            'count', 'candidate_valid_count', 'candidate_invalid_count', 'log_gap_count', 'header_gap_count')}
        cameras[name]['steady_insufficient_fallback_episodes'] = sum(row['steady_insufficient_fallback'] for row in rows)
        cameras[name]['anomaly_counts'] = dict(sum((collections.Counter(row['anomaly_counts']) for row in rows), collections.Counter()))
    report = {'status': status, 'generated_at_utc': utc_now(), 'source': str(args.source),
              'output': str(args.output), 'run_config': config, 'workers': args.workers,
              'selected_episode_count': len(paths), 'processed_episode_count': len(results),
              'success_count': len(successful), 'error_count': len(results) - len(successful),
              'resumed_episode_count': len(successful) - len(fresh),
              'fresh_episode_count': len(fresh), 'elapsed_s': elapsed,
              'covered_source_bytes': sum(row['source_size_bytes'] for row in successful),
              'fresh_source_bytes': sum(row['source_size_bytes'] for row in fresh),
              'effective_fresh_source_MB_per_s': sum(row['source_size_bytes'] for row in fresh) / elapsed / 1e6,
              'all_source_paths': [str(path.relative_to(args.source)) for path in paths],
              'cameras': cameras, 'errors': [row for row in results if row['status'] != 'ok'],
              'limitations': ['Candidate masks are a log-reference timing heuristic, not exposure synchronization, JPEG integrity, or success certification.',
                             'MCAP camera chunks are read/decompressed as required; JPEG images are never decoded.',
                             'Source fingerprint is size and mtime_ns, not a cryptographic hash of 639 GB raw data.',
                             'Timestamp duplicates/reversals are assessed in ascending MCAP log order. Original physical record order is not retained.',
                             'Per-episode NPZ contains all camera frames; invalid candidates are masked, never removed.']}
    atomic_json(args.output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--episode', action='append', help='Repeatable batch/episode selector; aliases such as all_data1/000059 accepted')
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    if args.source == args.output or args.source in args.output.parents:
        parser.error('Output must be outside the raw source tree')
    if not 1 <= args.workers <= 2:
        parser.error('Workers must be 1 or 2 to limit raw-data I/O')
    try:
        paths = select_paths(args.source, args.episode)
    except ValueError as exc:
        parser.error(str(exc))
    if not paths:
        parser.error('No MCAP files found')
    args.output.mkdir(parents=True, exist_ok=True)
    config = {**SETTINGS, 'script_sha256': file_sha256(Path(__file__)),
              'timestamp_parser_sha256': file_sha256(Path(__file__).with_name('audit_camera_timestamps.py'))}
    started = time.monotonic()
    print(json.dumps({'event': 'start', 'episodes': len(paths), 'workers': args.workers,
                      'source_bytes': sum(path.stat().st_size for path in paths)}), flush=True)
    results = []
    write_summary(args, config, paths, results, started, 'running')
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(process_episode, (str(path), str(args.source), str(args.output), config)): path for path in paths}
        last_report = started
        for future in concurrent.futures.as_completed(pending):
            try:
                result = future.result()
            except Exception as exc:
                result = {'episode': str(pending[future].parent.relative_to(args.source)),
                          'status': 'error', 'error_type': type(exc).__name__, 'error': str(exc)}
            results.append(result)
            now = time.monotonic()
            if len(results) <= 2 or len(results) % 20 == 0 or now - last_report >= 30 or len(results) == len(paths):
                report = write_summary(args, config, paths, results, started, 'running')
                print(json.dumps({'event': 'progress', 'completed': len(results), 'total': len(paths),
                                  'success': report['success_count'], 'errors': report['error_count'],
                                  'resumed': report['resumed_episode_count'], 'elapsed_s': round(now - started, 2),
                                  'source_MB_per_s': round(report['effective_fresh_source_MB_per_s'], 2),
                                  'last_episode': result['episode']}), flush=True)
                last_report = now
    report = write_summary(args, config, paths, results, started,
                           'complete' if all(row['status'] == 'ok' for row in results) else 'complete_with_errors')
    print(json.dumps({'event': 'finished', **{key: report[key] for key in (
        'status', 'selected_episode_count', 'success_count', 'error_count', 'resumed_episode_count',
        'elapsed_s', 'effective_fresh_source_MB_per_s', 'cameras')}}), flush=True)
    return 0 if report['error_count'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
