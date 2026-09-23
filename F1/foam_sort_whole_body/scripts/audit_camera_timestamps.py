#!/usr/bin/env python3
"""Indexed camera timestamp sampling, without JPEG decompression or source edits.

Sample seven windows per episode: a first-frame probe, 0.3 s, 3 s, 25%,
50%, 75%, and the final 0.3 s. Only the first message per camera/window
is examined. Offsets are header minus MCAP log time, NOT sensor latency.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import datetime as dt
import json
import math
from pathlib import Path
import struct
import time

import numpy as np
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

SOURCE = Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw')
OUTPUT = Path(__file__).resolve().parents[1] / 'results/20260914/camera_timestamps'
TOPICS = ['/camera/head/color/image_raw/compressed',
          '/camera/left_wrist/color/image_raw/compressed',
          '/camera/right_wrist/color/image_raw/compressed']
SCHEMA_NAME = 'sensor_msgs/msg/CompressedImage'


def distribution(values):
    a = np.asarray(values, dtype=float)
    a = a[np.isfinite(a)]
    if not len(a):
        return {'count': 0}
    return dict(count=int(len(a)), min=float(a.min()), median=float(np.median(a)),
                p05=float(np.quantile(a, .05)), p95=float(np.quantile(a, .95)),
                max=float(a.max()), mean=float(a.mean()))


def fields(text):
    return [s.split('#', 1)[0].strip() for s in text.splitlines()
            if s.split('#', 1)[0].strip()]


def schema_supported(schema, channel):
    if schema is None or schema.name != SCHEMA_NAME or schema.encoding != 'ros2msg' or channel.message_encoding != 'cdr':
        return False
    blocks = schema.data.decode('utf-8').split('=' * 80)
    top = fields(blocks[0])
    if not top or top[0].split() != ['std_msgs/Header', 'header']:
        return False
    nested = {}
    for block in blocks[1:]:
        lines = fields(block)
        if lines and lines[0].startswith('MSG: '):
            nested[lines[0][5:]] = lines[1:]
    header = nested.get('std_msgs/Header', [])
    stamp = nested.get('builtin_interfaces/Time', [])
    return (bool(header) and header[0].split() == ['builtin_interfaces/Time', 'stamp']
            and len(stamp) >= 2 and stamp[0].split() == ['int32', 'sec']
            and stamp[1].split() == ['uint32', 'nanosec'])


def header_ns(data):
    if len(data) < 12:
        raise ValueError(f'CDR shorter than timestamp prefix: {len(data)} bytes')
    if data[:4] != b'\x00\x01\x00\x00':
        raise ValueError(f'Unsupported CDR encapsulation {data[:4].hex()}')
    sec, nanos = struct.unpack_from('<iI', data, 4)
    if not 0 <= nanos < 1_000_000_000:
        raise ValueError(f'Invalid timestamp nanosecond field: {nanos}')
    return sec * 1_000_000_000 + nanos


def verify_full_decoder(path):
    with path.open('rb') as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        if summary is None or summary.statistics is None or not summary.chunk_indexes:
            raise ValueError('Indexed MCAP summary is required; refusing unbounded fallback scan')
        start = summary.statistics.message_start_time
        for schema, channel, message in reader.iter_messages(topics=TOPICS, start_time=start, end_time=start+1_000_000_000):
            if not schema_supported(schema, channel):
                raise ValueError('Unexpected image schema in decoder cross-check')
            prefix_stamp = header_ns(message.data)
            decoder = DecoderFactory().decoder_for(channel.message_encoding, schema)
            if decoder is None:
                raise ValueError('No ROS2 decoder for image schema')
            decoded = decoder(message.data)
            full_stamp = decoded.header.stamp.sec * 1_000_000_000 + decoded.header.stamp.nanosec
            if prefix_stamp != full_stamp:
                raise ValueError(f'Prefix/full-decoder stamp mismatch {prefix_stamp} != {full_stamp}')
            return dict(path=str(path), topic=channel.topic, schema=schema.name,
                        encapsulation=message.data[:4].hex(), log_time_ns=message.log_time,
                        header_stamp_ns=prefix_stamp, full_decoder_stamp_ns=full_stamp,
                        match=True, image_format=decoded.format, jpeg_decompressed=False)
    raise ValueError('No camera frame in first second for decoder cross-check')


def inspect_file(task):
    path_string, source_string, window_s = task
    path, source = Path(path_string), Path(source_string)
    episode = str(path.parent.relative_to(source))
    rows, errors = [], []
    try:
        with path.open('rb') as stream:
            reader = make_reader(stream)
            summary = reader.get_summary()
            if summary is None or summary.statistics is None or not summary.chunk_indexes:
                raise ValueError('No indexed summary; skipped instead of scanning full payload')
            start, end = summary.statistics.message_start_time, summary.statistics.message_end_time
            duration_s = (end-start)/1e9
            schema_ok = {}
            for channel_id, channel in summary.channels.items():
                if channel.topic in TOPICS:
                    schema_ok[channel_id] = schema_supported(summary.schemas.get(channel.schema_id), channel)
            samples = [('first_frame_probe', 0.0), ('start_0p3s', .3), ('t3s', 3.0),
                       ('quarter', .25*duration_s), ('midpoint', .5*duration_s),
                       ('three_quarters', .75*duration_s), ('last_0p3s', max(0.0, duration_s-.3))]
            for label, target in samples:
                begin = start+int(target*1e9)
                stop = min(end+1, begin+int(window_s*1e9))
                found = {}
                if begin < stop:
                    for schema, channel, message in reader.iter_messages(topics=TOPICS, start_time=begin, end_time=stop):
                        if channel.topic in found:
                            continue
                        row = dict(episode=episode, batch=path.parent.parent.name, topic=channel.topic,
                                   sample_label=label, target_s=target, window_start_ns=begin,
                                   window_end_ns=stop, log_time_ns=message.log_time,
                                   publish_time_ns=message.publish_time,
                                   log_time_from_episode_start_s=(message.log_time-start)/1e9,
                                   payload_size_bytes=len(message.data), schema_name=schema.name if schema else None,
                                   encapsulation=message.data[:4].hex(), status='ok')
                        try:
                            if not schema_ok.get(channel.id, False):
                                raise ValueError('Unverified CompressedImage/Header/Time schema or message encoding')
                            stamp = header_ns(message.data)
                            row.update(header_time_ns=stamp, header_minus_log_s=(stamp-message.log_time)/1e9,
                                       header_minus_publish_s=(stamp-message.publish_time)/1e9)
                        except (ValueError, UnicodeError) as exc:
                            row.update(status='unsupported_or_invalid_timestamp', error=str(exc))
                        found[channel.topic] = row
                        if len(found) == len(TOPICS):
                            break
                for topic in TOPICS:
                    rows.append(found.get(topic, dict(episode=episode, batch=path.parent.parent.name, topic=topic,
                                sample_label=label, target_s=target, window_start_ns=begin,
                                window_end_ns=stop, status='no_message_in_window')))
    except Exception as exc:
        errors.append(dict(episode=episode, error_type=type(exc).__name__, error=str(exc)))
    return dict(episode=episode, samples=rows, errors=errors)


def csv_write(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def summarize(samples, errors, args, crosscheck, elapsed):
    grouped = collections.defaultdict(list)
    for row in samples:
        grouped[(row['episode'], row['topic'])].append(row)
    camera_rows, flags = [], []
    for (episode, topic), rows in sorted(grouped.items()):
        valid = [r for r in rows if r['status'] == 'ok']
        unique = sorted({r['log_time_ns']: r for r in valid}.values(), key=lambda r:r['log_time_ns'])
        steady = [r for r in valid if r['sample_label'] not in ['first_frame_probe', 'start_0p3s']]
        offsets = [r['header_minus_log_s'] for r in valid]
        steady_offsets = [r['header_minus_log_s'] for r in steady]
        median = float(np.median(steady_offsets)) if steady_offsets else None
        span = max(steady_offsets)-min(steady_offsets) if steady_offsets else None
        startup = [r for r in valid if r['sample_label'] in ['first_frame_probe', 'start_0p3s']]
        startup_dev = max((abs(r['header_minus_log_s']-median) for r in startup), default=None) if median is not None else None
        backward = [dict(previous_log_time_ns=a['log_time_ns'], next_log_time_ns=b['log_time_ns'],
                         header_decrease_s=(a['header_time_ns']-b['header_time_ns'])/1e9)
                    for a,b in zip(unique,unique[1:]) if b['header_time_ns'] < a['header_time_ns']]
        frozen = [dict(previous_log_time_ns=a['log_time_ns'], next_log_time_ns=b['log_time_ns'])
                  for a,b in zip(unique,unique[1:]) if b['header_time_ns'] == a['header_time_ns']]
        codes = []
        if median is not None and abs(median) > args.clock_offset_review_s:
            codes.append('clock_domain_or_offset_review')
        if span is not None and span > args.offset_span_review_s:
            codes.append('offset_variation_review')
        if startup_dev is not None and startup_dev > args.startup_outlier_review_s:
            codes.append('startup_timestamp_outlier_review_before_trimming')
        # Some cameras first appear around 3 s. The early probes are empty,
        # so a stale first timestamp must not silently escape startup review.
        late_first_dev = abs(unique[0]['header_minus_log_s']-median) if not startup and unique and median is not None else None
        if late_first_dev is not None and late_first_dev > args.startup_outlier_review_s:
            codes.append('late_first_sample_timestamp_outlier')
        if backward:
            codes.append('sampled_header_time_reversal')
        if frozen:
            codes.append('sampled_repeated_header_stamp')
        if any(r['status']=='no_message_in_window' for r in rows):
            codes.append('sample_window_without_frame')
        if any(r['status']=='unsupported_or_invalid_timestamp' for r in rows):
            codes.append('unsupported_or_invalid_timestamp')
        camera_rows.append(dict(episode=episode, batch=episode.split('/')[0], topic=topic,
                                planned_samples=len(rows), valid_samples=len(valid), unique_frames=len(unique),
                                missing_window_samples=sum(r['status']=='no_message_in_window' for r in rows),
                                offset_min_s=min(offsets) if offsets else None,
                                offset_max_s=max(offsets) if offsets else None,
                                steady_median_header_minus_log_s=median, steady_offset_span_s=span,
                                max_startup_deviation_from_steady_median_s=startup_dev,
                                late_first_sample_deviation_s=late_first_dev,
                                sampled_backwards_count=len(backward), sampled_frozen_count=len(frozen),
                                flags=codes))
        for code in codes:
            flags.append(dict(episode=episode, topic=topic, flag=code,
                              steady_median_header_minus_log_s=median, steady_offset_span_s=span,
                              max_startup_deviation_from_steady_median_s=startup_dev,
                              header_reversals=backward, repeated_headers=frozen,
                              recommendation='Review clock mapping/startup frames; no automatic trimming or timestamp rewriting performed'))
    by_batch_camera = []
    for batch in sorted({r['batch'] for r in camera_rows}):
        for topic in TOPICS:
            rows = [r for r in camera_rows if r['batch']==batch and r['topic']==topic]
            by_batch_camera.append(dict(batch=batch, topic=topic, episodes=len(rows),
                                       steady_median_header_minus_log_s=distribution([r['steady_median_header_minus_log_s'] for r in rows if r['steady_median_header_minus_log_s'] is not None]),
                                       steady_offset_span_s=distribution([r['steady_offset_span_s'] for r in rows if r['steady_offset_span_s'] is not None]),
                                       flag_episode_counts=dict(collections.Counter(c for r in rows for c in r['flags']))))
    flag_codes=sorted({r['flag'] for r in flags})
    summary = dict(generated_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
                   source=str(args.source.resolve()), elapsed_s=elapsed,
                   episodes_sampled=len({r['episode'] for r in samples}), episode_camera_pairs=len(camera_rows),
                   planned_samples=len(samples), sample_status_counts=dict(collections.Counter(r['status'] for r in samples)),
                   missing_samples_by_window=dict(collections.Counter(r['sample_label'] for r in samples if r['status']=='no_message_in_window')),
                   unique_sampled_frames=sum(r['unique_frames'] for r in camera_rows),
                   sample_schedule=['first_frame_probe: 0 s', '0.3 s', '3 s', '25%', '50%', '75%', 'last 0.3 s'],
                   sample_window_s=args.window_s, workers=args.workers,
                   thresholds=dict(clock_offset_review_s=args.clock_offset_review_s,
                                   offset_span_review_s=args.offset_span_review_s,
                                   startup_outlier_review_s=args.startup_outlier_review_s),
                   offset_sign='header timestamp minus MCAP log timestamp; positive means header is later',
                   interpretation='Offsets and their variation compare clock fields, not measured physical acquisition/transmission latency. Steady excludes first-frame and 0.3s probes.',
                   limitations=['Only selected frames were sampled; no full camera header audit, payload CRC verification or JPEG decoding.',
                                'A large startup offset can be stale frame/header, clock epoch/mapping, or capture pipeline behavior; imagery and driver semantics must distinguish them.',
                                'No automatic frame deletion, startup crop length, header correction or training conversion was applied.',
                                'Review thresholds are heuristics, not accepted sensor synchronization specifications.'],
                   prefix_decoder_crosscheck=crosscheck, file_errors=errors,
                   flag_camera_pair_counts=dict(collections.Counter(r['flag'] for r in flags)),
                   flag_distinct_episode_counts={code:len({r['episode'] for r in flags if r['flag']==code}) for code in flag_codes},
                   batch_camera_statistics=by_batch_camera,
                   largest_startup_outliers=sorted([r for r in camera_rows if r['max_startup_deviation_from_steady_median_s'] is not None],key=lambda r:r['max_startup_deviation_from_steady_median_s'],reverse=True)[:25],
                   largest_steady_offset_spans=sorted([r for r in camera_rows if r['steady_offset_span_s'] is not None],key=lambda r:r['steady_offset_span_s'],reverse=True)[:25])
    output=args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv_write(output/'samples.csv', samples)
    csv_write(output/'episode_cameras.csv', camera_rows)
    (output/'flags.json').write_text(json.dumps(flags,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    manifest=[]
    for episode in sorted({r['episode'] for r in samples}):
        relevant=[r for r in flags if r['episode']==episode]
        manifest.append(dict(episode=episode, flags=[dict(topic=r['topic'],flag=r['flag']) for r in relevant],
                             requires_clock_mapping_review=any(r['flag'] in ['clock_domain_or_offset_review','offset_variation_review','sampled_header_time_reversal','sampled_repeated_header_stamp'] for r in relevant),
                             requires_startup_frame_review=any(r['flag'] in ['startup_timestamp_outlier_review_before_trimming','late_first_sample_timestamp_outlier'] for r in relevant),
                             source_modified=False, sampling_only=True))
    (output/'clock_review_manifest.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in manifest),encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ['episodes_sampled','planned_samples','sample_status_counts','unique_sampled_frames','file_errors','flag_distinct_episode_counts','elapsed_s']},ensure_ascii=False,indent=2),flush=True)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE)
    p.add_argument('--output',type=Path,default=OUTPUT)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--window-s',type=float,default=.5)
    p.add_argument('--clock-offset-review-s',type=float,default=.1)
    p.add_argument('--offset-span-review-s',type=float,default=.05)
    p.add_argument('--startup-outlier-review-s',type=float,default=.5)
    p.add_argument('--limit',type=int,help='Optional bounded smoke run; omit for all episodes')
    args=p.parse_args()
    if args.source.resolve()==args.output.resolve() or args.source.resolve() in args.output.resolve().parents:
        p.error('output must not be within source data')
    if args.workers<1 or args.workers>8 or not math.isfinite(args.window_s) or args.window_s<=0:
        p.error('workers must be 1..8 and window must be finite and positive')
    if any(not math.isfinite(value) or value<=0 for value in [args.clock_offset_review_s,args.offset_span_review_s,args.startup_outlier_review_s]):
        p.error('review thresholds must be finite and positive')
    paths=sorted(args.source.glob('*/episode_*/*.mcap'))
    if args.limit is not None:
        paths=paths[:args.limit]
    if not paths:
        p.error('No MCAP files found')
    started=time.monotonic()
    crosscheck=verify_full_decoder(paths[0])
    samples,errors=[],[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i,result in enumerate(pool.map(inspect_file,[(str(path),str(args.source.resolve()),args.window_s) for path in paths]),1):
            samples.extend(result['samples']);errors.extend(result['errors'])
            if i%50==0 or i==len(paths):
                print(f'Sampled {i}/{len(paths)} episodes in {time.monotonic()-started:.1f}s; file errors {len(errors)}',flush=True)
    summarize(samples,errors,args,crosscheck,time.monotonic()-started)


if __name__=='__main__':
    main()
