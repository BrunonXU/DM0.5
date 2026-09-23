#!/usr/bin/env python3
"""Independently validate sampled head-frame cache provenance, clocks and duplicates."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

TASK = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, default=TASK / 'results/demo_775_selection/candidates.jsonl')
    parser.add_argument('--cache', type=Path, default=TASK / 'results/demo_775_phase_review/frame_cache')
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_775_phase_review/cache_validation')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Refusing to overwrite validation results')
    rows = [json.loads(line) for line in args.candidates.read_text().splitlines() if line.strip()]
    identity = json.loads((args.cache / 'run_config.json').read_text())
    assert sha(args.candidates) == identity['candidate_sha256']
    assert len(rows) == len({r['episode'] for r in rows})
    incomplete = [r['episode'] for r in rows if not (args.cache / 'sources' / (r['episode'] + '.json')).exists()]
    if incomplete:
        raise ValueError(f'Cache incomplete: {len(incomplete)} sources missing')
    totals = Counter()
    results, duplicate_runs, errors = [], [], []
    sha_sources = defaultdict(set)
    for row in rows:
        ep = row['episode']
        record = {'episode': ep, 'checks_passed': False}
        try:
            meta_path = args.cache / 'sources' / (ep + '.json')
            meta = json.loads(meta_path.read_text())
            assert meta['status'] == 'complete' and meta['identity'] == identity
            assert meta['source_fingerprint'] == row['source_fingerprint']
            raw_stat = Path(row['raw_path']).stat()
            assert row['source_fingerprint'] == {'size_bytes': raw_stat.st_size, 'mtime_ns': raw_stat.st_mtime_ns}
            pack_path = meta_path.with_suffix('.npz')
            assert sha(pack_path) == meta['npz_sha256']
            assert sha(row['camera_npz_path']) == row['camera_npz_sha256']
            assert sha(row['signal_npz_path']) == row['signal_npz_sha256']
            with np.load(pack_path, allow_pickle=False) as pack, np.load(row['camera_npz_path'], allow_pickle=False) as camera, np.load(row['signal_npz_path'], allow_pickle=False) as signal:
                q, logs, ids = pack['query_ns'], pack['head_log_ns'], pack['head_source_indices']
                headers, gray, hashes = pack['head_header_ns'], pack['head_gray'], pack['head_jpeg_sha256']
                assert len(q) == len(logs) == len(ids) == len(headers) == len(gray) == len(hashes) == meta['sampled_frames']
                assert gray.dtype == np.uint8 and gray.ndim == 3 and gray.shape[2] == identity['width']
                assert np.array_equal(camera['head_log_ns'][ids], logs)
                assert np.array_equal(camera['head_header_ns'][ids], headers)
                assert camera['head_timestamp_candidate_valid'][ids].all()
                assert np.all(logs <= q) and np.all(np.diff(q) > 0) and np.all(np.diff(logs) >= 0)
                assert np.array_equal(pack['head_age_s'], (q - logs) / 1e9)
                # Independent forward walk verifies every causal choice and every query slot.
                all_logs = camera['head_log_ns']
                valid_ids = np.flatnonzero(camera['head_timestamp_candidate_valid'])
                expected_ids, expected_grid = [], []
                pointer = -1
                for t in range(row['source_start_ns'], row['source_start_ns'] + row['source_duration_ns'], round(identity['step_s'] * 1e9)):
                    while pointer + 1 < len(valid_ids) and all_logs[valid_ids[pointer + 1]] <= t:
                        pointer += 1
                    if pointer >= 0:
                        expected_grid.append(t)
                        expected_ids.append(valid_ids[pointer])
                assert np.array_equal(q, expected_grid) and np.array_equal(ids, expected_ids)
                assert np.array_equal(pack['hal_log_ns'], signal['hal_t_ns'])
                assert np.array_equal(pack['head_body_q'][:, 3:], signal['hal_q'][:, -5:])
                assert np.isfinite(pack['head_body_q']).all()
                assert np.all(np.diff(pack['hal_log_ns']) > 0)
                count = len(logs)
                run_start = 0
                for end in range(1, count + 1):
                    if end < count and hashes[end] == hashes[run_start]:
                        continue
                    duration = (int(q[end - 1]) - int(q[run_start])) / 1e9
                    if end - run_start > 1 and duration >= 1:
                        duplicate_runs.append({'episode': ep, 'start_s': (int(logs[run_start]) - row['source_start_ns']) / 1e9,
                                               'duration_s': duration, 'sampled_frames': end - run_start,
                                               'distinct_log_times': len(set(map(int, logs[run_start:end]))),
                                               'jpeg_sha256': str(hashes[run_start])})
                    run_start = end
                for value in set(map(str, hashes)):
                    sha_sources[value].add(ep)
                stale = int((pack['head_age_s'] > .1).sum())
                totals.update(sources=1, sampled_frames=count, stale_queries=stale,
                              repeated_selected_frame_pairs=int((np.diff(logs) == 0).sum()),
                              identical_jpeg_adjacent_pairs=int((hashes[1:] == hashes[:-1]).sum()),
                              full_hal_samples=len(pack['hal_log_ns']))
                record.update(checks_passed=True, sampled_frames=count, stale_queries=stale,
                              cache_npz_sha256=meta['npz_sha256'], max_head_age_s=float(pack['head_age_s'].max()))
        except Exception as error:
            record['error'] = repr(error)
            errors.append({'episode': ep, 'error': repr(error)})
        results.append(record)
    cross_source = [{'jpeg_sha256': key, 'episodes': sorted(value)} for key, value in sha_sources.items() if len(value) > 1]
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'episodes.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in results))
    summary = {'created_at': datetime.now(timezone.utc).isoformat(), 'script_sha256': sha(__file__),
               'cache_identity': identity, 'expected_sources': len(rows), 'totals': dict(totals),
               'errors': errors, 'all_checks_passed': not errors and totals['sources'] == len(rows),
               'identical_jpeg_runs_at_least_1s': duplicate_runs, 'cross_source_identical_jpegs': cross_source,
               'scope': 'selected grayscale cache and original camera/numeric indexes; does not re-decode MCAP JPEG bytes or certify exposure synchrony/task quality',
               'duplicate_interpretation': 'identical JPEG or selected frame reuse needs review; absence does not rule out near-frozen scene/video',
               'training_approved': False}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k not in ('cross_source_identical_jpegs', 'identical_jpeg_runs_at_least_1s')}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
