#!/usr/bin/env python3
"""Read selected MCAPs once; cache causal head frames and raw head/body feedback."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

TASK = Path(__file__).resolve().parents[1]
HEAD = '/camera/head/color/image_raw/compressed'
HAL = '/hal/joint_states'
AXES = ['head_j1', 'head_j2', 'head_j3', *[f'body_j{i}' for i in range(1, 6)]]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def process(row, output, identity, width, step_s, intervals=None):
    ep = row['episode']
    target = (output / 'sources' / ep).with_suffix('.json')
    npz = target.with_suffix('.npz')
    if target.exists():
        old = json.loads(target.read_text())
        if old.get('identity') != identity:
            raise ValueError('Cache belongs to another method/input: ' + ep)
        if old.get('status') == 'complete' and npz.exists() and sha(npz) == old['npz_sha256']:
            return old
    result = {'episode': ep, 'parent_split': row['parent_split'], 'identity': identity,
              'created_at': stamp(), 'raw_path': row['raw_path'], 'source_start_ns': row['source_start_ns'],
              'source_duration_ns': row['source_duration_ns'], 'source_fingerprint': row['source_fingerprint'],
              'camera_npz_sha256': row['camera_npz_sha256'], 'signal_npz_sha256': row['signal_npz_sha256'],
              'head_body_columns': AXES, 'sampling_step_s': step_s, 'width': width}
    start = time.monotonic()
    try:
        raw = Path(row['raw_path'])
        before = raw.stat()
        assert {'size_bytes': before.st_size, 'mtime_ns': before.st_mtime_ns} == row['source_fingerprint']
        assert sha(row['camera_npz_path']) == row['camera_npz_sha256']
        assert sha(row['signal_npz_path']) == row['signal_npz_sha256']
        with np.load(row['camera_npz_path'], allow_pickle=False) as cameras:
            all_logs = cameras['head_log_ns'].copy()
            all_headers = cameras['head_header_ns'].copy()
            valid_ids = np.flatnonzero(cameras['head_timestamp_candidate_valid'])
        assert len(valid_ids) > 0 and np.all(np.diff(all_logs) > 0)
        grid = row['source_start_ns'] + np.arange(0, row['source_duration_ns'], round(step_s * 1e9), dtype=np.int64)
        if intervals is not None:
            # Keep the original source-origin grid. Only decode requested
            # boundary neighbourhoods; never rebase each clip to zero.
            relative = (grid - row['source_start_ns']) / 1e9
            keep = np.zeros(len(grid), dtype=bool)
            for begin, end in intervals:
                assert np.isfinite([begin, end]).all() and 0 <= begin < end <= row['source_duration_ns']/1e9
                keep |= (relative >= begin) & (relative <= end)
            grid = grid[keep]
            assert len(grid), 'No samples in requested boundary intervals'
            result['requested_intervals_s'] = intervals
        positions = np.searchsorted(all_logs[valid_ids], grid, side='right') - 1
        good = positions >= 0
        grid = grid[good]
        source_ids = valid_ids[positions[good]]
        logs, headers = all_logs[source_ids], all_headers[source_ids]
        ages = (grid - logs) / 1e9
        wanted = set(map(int, logs))
        images, hashes, dimensions = {}, {}, set()
        hal_times, hal_values = [], []
        factory = DecoderFactory()
        head_seen = hal_seen = 0
        with raw.open('rb') as stream:
            reader = make_reader(stream, validate_crcs=True)
            summary = reader.get_summary()
            assert summary and summary.chunk_indexes
            result['mcap_chunk_count'] = len(summary.chunk_indexes)
            result['mcap_chunk_compressions'] = sorted({x.compression for x in summary.chunk_indexes})
            for schema, channel, message in reader.iter_messages(topics=[HEAD, HAL]):
                if channel.topic == HEAD:
                    head_seen += 1
                    if message.log_time not in wanted:
                        continue
                else:
                    hal_seen += 1
                decoder = factory.decoder_for(channel.message_encoding, schema)
                assert decoder is not None
                data = decoder(message.data)
                if channel.topic == HEAD:
                    index = int(np.searchsorted(all_logs, message.log_time))
                    actual_header = int(data.header.stamp.sec) * 10**9 + int(data.header.stamp.nanosec)
                    assert int(all_headers[index]) == actual_header
                    jpeg = bytes(data.data)
                    with Image.open(io.BytesIO(jpeg)) as image:
                        dimensions.add(image.size)
                        desired_height = round(image.height * width / image.width)
                        image.draft('L', (width, desired_height))
                        gray = image.convert('L').resize((width, desired_height), Image.Resampling.BILINEAR)
                        images[int(message.log_time)] = np.asarray(gray).copy()
                    hashes[int(message.log_time)] = hashlib.sha256(jpeg).hexdigest()
                else:
                    names = dict(zip(data.name, data.position))
                    hal_times.append(int(message.log_time))
                    hal_values.append([float(names.get(name, float('nan'))) for name in AXES])
        assert set(images) == wanted, (len(images), len(wanted))
        q = np.asarray(hal_values, dtype=np.float64)
        ht = np.asarray(hal_times, dtype=np.int64)
        with np.load(row['signal_npz_path'], allow_pickle=False) as signal:
            assert np.array_equal(ht, signal['hal_t_ns'])
            assert np.array_equal(q[:, 3:], signal['hal_q'][:, -5:])
        after = raw.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
        target.parent.mkdir(parents=True, exist_ok=True)
        with npz.with_suffix('.npz.tmp').open('wb') as stream:
            np.savez_compressed(stream, head_gray=np.stack([images[int(t)] for t in logs]),
                                head_log_ns=logs, head_header_ns=headers, query_ns=grid,
                                head_source_indices=source_ids, head_age_s=ages,
                                hal_log_ns=ht, head_body_q=q,
                                head_jpeg_sha256=np.asarray([hashes[int(t)] for t in logs]))
        npz.with_suffix('.npz.tmp').replace(npz)
        result.update(status='complete', sampled_frames=len(logs), unique_decoded_frames=len(images),
                      head_source_frames_seen=head_seen, hal_source_frames_seen=hal_seen,
                      head_body_missing_values=int((~np.isfinite(q)).sum()),
                      stale_head_queries_over_100ms=int((ages > .1).sum()),
                      future_head_queries=int((ages < 0).sum()), raw_dimensions=sorted(dimensions),
                      head_body_matches_existing_body_cache=True, source_stat_unchanged=True,
                      npz_sha256=sha(npz), npz_bytes=npz.stat().st_size)
    except Exception as error:
        result.update(status='error', error=repr(error))
    result['elapsed_s'] = time.monotonic() - start
    write_json(target, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, default=TASK / 'results/demo_775_selection/candidates.jsonl')
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_775_phase_review/frame_cache')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--width', type=int, default=480)
    parser.add_argument('--step-s', type=float, default=.5)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--interval-manifest', type=Path,
                        help='Optional JSONL: episode and intervals_s; retain original source time grid')
    args = parser.parse_args()
    assert args.width > 0 and args.step_s > 0 and 1 <= args.workers <= 4
    rows = [json.loads(line) for line in args.candidates.read_text().splitlines() if line.strip()]
    assert len(rows) == len({r['episode'] for r in rows})
    identity = {'schema': 'f1_base_phase_frames_v1', 'candidate_sha256': sha(args.candidates),
                'script_sha256': sha(Path(__file__)), 'step_s': args.step_s, 'width': args.width}
    intervals = None
    if args.interval_manifest:
        requests = [json.loads(line) for line in args.interval_manifest.read_text().splitlines() if line.strip()]
        assert len(requests) == len({r['episode'] for r in requests}), 'Duplicate boundary request'
        intervals = {r['episode']: r['intervals_s'] for r in requests}
        assert set(intervals) == {r['episode'] for r in rows}, 'Boundary request scope differs from candidate scope'
        identity['interval_manifest_sha256'] = sha(args.interval_manifest)
    run_config = args.output / 'run_config.json'
    if run_config.exists():
        assert json.loads(run_config.read_text()) == identity, 'Use a new cache for another method/input'
    else:
        write_json(run_config, identity)
    selected = rows[:args.limit] if args.limit else rows
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process, row, args.output, identity, args.width, args.step_s,
                                   intervals[row['episode']] if intervals is not None else None): row['episode'] for row in selected}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            progress = {'updated_at': stamp(), 'complete': sum(r['status'] == 'complete' for r in results),
                        'errors': sum(r['status'] == 'error' for r in results), 'requested': len(selected),
                        'total_candidate_scope': len(rows), 'sampled_frames': sum(r.get('sampled_frames', 0) for r in results),
                        'elapsed_s': time.monotonic() - started}
            write_json(args.output / 'progress.json', progress)
            if len(results) % 25 == 0 or result['status'] == 'error' or len(results) == len(selected):
                print(json.dumps({**progress, 'last_episode': result['episode'], 'last_status': result['status'],
                                  'last_error': result.get('error')}, ensure_ascii=False), flush=True)
    summary = {**progress, 'identity': identity,
               'source_results': [{'episode': r['episode'], 'status': r['status'], 'npz_sha256': r.get('npz_sha256'),
                                   'sampled_frames': r.get('sampled_frames'), 'error': r.get('error')} for r in sorted(results, key=lambda r:r['episode'])],
               'future_head_queries': sum(r.get('future_head_queries', 0) for r in results),
               'head_body_missing_values': sum(r.get('head_body_missing_values', 0) for r in results),
               'disk_bytes': sum(r.get('npz_bytes', 0) for r in results),
               'scope': 'sampled_head_images_and_full_raw_head_body_feedback_not_visual_motion_acceptance'}
    write_json(args.output / 'summary.json', summary)
    if progress['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
