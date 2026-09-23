#!/usr/bin/env python3
"""Read-only inventory and weak-label audit; does not decode MCAP payloads.

The only writes are new audit artifacts under --output. Source MCAP hashes are
not recomputed: transfer completion records and current file sizes are compared.
Speeds are conditional on piecewise-linear interpretation of JSON segments.
They are review heuristics, not measured speeds or hardware validity limits.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import yaml

DEFAULT_SOURCE = Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw')
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'results/20260914/metadata_audit'
TZ = dt.timezone(dt.timedelta(hours=8))
EPS = 1e-7


def json_read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def iso(ns):
    return dt.datetime.fromtimestamp(ns / 1e9, TZ).isoformat(timespec='milliseconds')


def distribution(values):
    a = np.asarray(values, dtype=float)
    a = a[np.isfinite(a)]
    if not len(a):
        return {'count': 0}
    return dict(count=int(len(a)), min=float(a.min()), p05=float(np.quantile(a, .05)),
                median=float(np.median(a)), p95=float(np.quantile(a, .95)),
                max=float(a.max()), mean=float(a.mean()))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def inspect_segments(segments, duration_s, windows, assumptions, threshold):
    """Return serializable checks, issues and rows for JSON segment geometry."""
    issues, rows = [], []

    def issue(code, detail, category='known_error'):
        issues.append(dict(code=code, category=category, detail=detail))

    if len(segments) != 6:
        issue('unexpected_segment_count', f'Expected six task-template segments; got {len(segments)}')
    valid = []
    for i, segment in enumerate(segments):
        if not isinstance(segment, list) or len(segment) != 7:
            issue('invalid_segment_format', f'segment {i}: {segment!r}')
            continue
        try:
            t0, t1, x0, y0, x1, y1 = map(float, segment[:6])
        except (TypeError, ValueError):
            issue('invalid_segment_number', f'segment {i}: {segment!r}')
            continue
        if not all(math.isfinite(v) for v in [t0, t1, x0, y0, x1, y1]):
            issue('nonfinite_segment_number', f'segment {i}')
            continue
        elapsed = t1 - t0
        if elapsed <= 0:
            issue('nonpositive_segment_duration', f'segment {i}: {elapsed} s')
        # Window times are stored to milliseconds while bag times retain ns.
        # A <1 ms overhang is recording precision, not a damaged episode.
        if t0 < -0.001 or t1 > duration_s + 0.001:
            issue('segment_outside_episode', f'segment {i}: [{t0}, {t1}] vs duration {duration_s}')
        elif t0 < -EPS or t1 > duration_s + EPS:
            issue('millisecond_rounding_at_episode_boundary', f'segment {i}: [{t0}, {t1}] vs duration {duration_s}; within 1 ms serialization tolerance', 'info')
        vx = (x1 - x0) / elapsed if elapsed > 0 else None
        vy = (y1 - y0) / elapsed if elapsed > 0 else None
        speed = math.hypot(vx, vy) if elapsed > 0 else None
        row = dict(segment_index=i, label=segment[6], start_s=t0, end_s=t1,
                   duration_s=elapsed, x0_m=x0, y0_m=y0, x1_m=x1, y1_m=y1,
                   distance_m=math.hypot(x1-x0, y1-y0), mean_vx_mps=vx, mean_vy_mps=vy,
                   segment_mean_speed_mps=speed)
        rows.append(row)
        valid.append((i, t0, t1, x0, y0, x1, y1, vx, vy))
    gaps, overlaps, discontinuities, jumps = [], [], [], []
    for a, b in zip(valid, valid[1:]):
        gap = b[1] - a[2]
        if gap < -EPS:
            overlaps.append(dict(after_segment=a[0], seconds=-gap))
        elif gap > EPS:
            gaps.append(dict(after_segment=a[0], seconds=gap,
                             expected_task_dwell=(a[0] == 2 and b[0] == 3)))
            if not (a[0] == 2 and b[0] == 3):
                issue('unexpected_internal_time_gap', f'after segment {a[0]}: {gap} s', 'manual_review')
        jump_position = math.hypot(a[5] - b[3], a[6] - b[4])
        if jump_position > EPS:
            discontinuities.append(dict(after_segment=a[0], distance_m=jump_position))
        if all(v is not None for v in [a[7], a[8], b[7], b[8]]):
            if abs(gap) <= EPS:
                jumps.append(math.hypot(b[7]-a[7], b[8]-a[8]))
            else:
                jumps.extend([math.hypot(a[7], a[8]), math.hypot(b[7], b[8])])
    if valid and valid[0][7] is not None:
        jumps.append(math.hypot(valid[0][7], valid[0][8]))
    if valid and valid[-1][7] is not None:
        jumps.append(math.hypot(valid[-1][7], valid[-1][8]))
    if overlaps:
        issue('segment_time_overlap', canonical(overlaps))
    if discontinuities:
        issue('segment_position_discontinuity', canonical(discontinuities))
    closure = (math.hypot(valid[-1][5]-valid[0][3], valid[-1][6]-valid[0][4])
               if valid else None)
    if closure is not None and closure > EPS:
        issue('template_path_not_closed', f'closure error {closure} m')
    if len(valid) == 6:
        expected_window_edges = [valid[0][1], valid[2][2], valid[3][1], valid[5][2]]
        for key, expected in zip(['outbound_start_s', 'outbound_end_s', 'return_start_s', 'return_end_s'], expected_window_edges):
            actual = windows.get(key)
            if actual is None or abs(float(actual) - expected) > EPS:
                issue('window_segment_edge_mismatch', f'{key}: window {actual}, segment {expected}')
        back, right = assumptions.get('backward_distance_m'), assumptions.get('right_distance_m')
        if back is not None and right is not None:
            expected_points = [(0, 0), (-back, 0), (-back, -right), (0, -right),
                               (-back, -right), (-back, 0), (0, 0)]
            actual_points = [(valid[0][3], valid[0][4])] + [(s[5], s[6]) for s in valid]
            if any(math.hypot(x-a, y-b) > EPS for (x, y), (a, b) in zip(actual_points, expected_points)):
                issue('geometry_assumption_mismatch', f'actual {actual_points}; expected {expected_points}')
    speeds = [r['segment_mean_speed_mps'] for r in rows if r['segment_mean_speed_mps'] is not None]
    max_speed = max(speeds, default=None)
    if max_speed is not None and max_speed > threshold:
        issue('template_speed_review', f'Largest segment MEAN speed {max_speed:.6f} m/s > review threshold {threshold}; not instantaneous MCAP speed; hardware limit unknown', 'manual_review')
    if any(v > EPS for v in jumps):
        issue('segment_means_do_not_define_acceleration', 'Adjacent segment mean velocity vectors differ; endpoint JSON alone cannot establish instantaneous speed, acceleration ramps, or actual discontinuities. MCAP may interpolate smoothly and have higher peak speed.', 'weak_label_risk')
    return dict(segment_count=len(segments), max_segment_mean_speed_mps=max_speed,
                max_segment_mean_velocity_change_mps=max(jumps, default=None),
                template_path_length_m=sum(r['distance_m'] for r in rows),
                template_closure_error_m=closure, internal_gaps=gaps, overlaps=overlaps,
                position_discontinuities=discontinuities), issues, rows


def write_csv(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: canonical(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def audit(args):
    source, output = args.source.resolve(), args.output.resolve()
    if output == source or source in output.parents:
        raise ValueError('--output must not be inside the immutable source dataset')
    transfer = args.transfer.resolve() if args.transfer else source.parent / 'transfer'
    manifest_path, completed_path = transfer / 'manifest.json', transfer / 'completed.json'
    manifest_doc = json_read(manifest_path) if manifest_path.exists() else {}
    manifest, completed = manifest_doc.get('files', {}), json_read(completed_path) if completed_path.exists() else {}
    all_files = {str(f.relative_to(source)): f.stat().st_size for f in source.rglob('*') if f.is_file()}
    manifest_missing = sorted(set(manifest) - set(all_files))
    local_extras = sorted(set(all_files) - set(manifest))
    size_mismatch = [dict(path=k, local_bytes=all_files[k], remote_bytes=v['size'])
                     for k, v in manifest.items() if k in all_files and all_files[k] != v['size']]
    completion_missing = sorted(set(manifest) - set(completed))
    completion_stale = [k for k, v in manifest.items() if k in completed and
                        (completed[k].get('source') != v or completed[k].get('bytes') != v['size'])]
    completion_hash_invalid = [k for k in manifest if k in completed and
                               (len(completed[k].get('sha256', '')) != 64 or
                                any(c not in '0123456789abcdef' for c in completed[k].get('sha256', '')))]
    problem_files = collections.defaultdict(list)
    for code, entries in [('remote_file_missing', manifest_missing), ('unlisted_local_file', local_extras),
                          ('file_size_mismatch', [r['path'] for r in size_mismatch]),
                          ('completion_record_missing', completion_missing),
                          ('completion_record_stale', completion_stale),
                          ('completion_sha_record_invalid', completion_hash_invalid)]:
        for name in entries:
            problem_files[str(Path(name).parent)].append((code, name))

    episodes, findings, segment_rows, topic_rows = [], [], [], []
    metadata_records = {}
    per_topic = collections.defaultdict(lambda: dict(message_count=0, episode_count=0, rates=[]))
    signatures, assumption_variants, encoding_variants = collections.Counter(), collections.Counter(), collections.Counter()
    metadata_loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
    for directory in sorted(p for p in source.glob('*/episode_*') if p.is_dir()):
        rel = str(directory.relative_to(source))
        local = {k: v for k, v in all_files.items() if str(Path(k).parent) == rel}
        episode_issues = []

        def issue(code, detail, category='known_error'):
            episode_issues.append(dict(code=code, category=category, detail=detail))

        row = dict(episode=rel, batch=directory.parent.name, episode_id=directory.name,
                   local_file_count=len(local), local_bytes=sum(local.values()))
        for code, filename in problem_files[rel]:
            issue(code, filename, 'manual_review' if code == 'unlisted_local_file' else 'known_error')
        for name in ['metadata.yaml', 'estimated_move_state.json', f'{directory.name}_0.mcap']:
            if not (directory / name).is_file():
                issue('required_file_missing', name)
        metadata, weak = None, None
        try:
            metadata = yaml.load((directory / 'metadata.yaml').read_text(), Loader=metadata_loader)['rosbag2_bagfile_information']
        except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
            issue('metadata_read_error', str(exc))
        try:
            weak = json_read(directory / 'estimated_move_state.json')
        except (OSError, ValueError) as exc:
            issue('weak_label_read_error', str(exc))
        if metadata:
            metadata_records[rel] = metadata
            duration_ns = metadata['duration']['nanoseconds']
            start_ns = metadata['starting_time']['nanoseconds_since_epoch']
            duration_s = duration_ns / 1e9
            row.update(duration_s=duration_s, start_ns=start_ns, end_ns=start_ns+duration_ns,
                       start_time_beijing=iso(start_ns), end_time_beijing=iso(start_ns+duration_ns),
                       message_count=metadata['message_count'], storage_identifier=metadata.get('storage_identifier'),
                       ros_distro=metadata.get('ros_distro'))
            if duration_ns <= 0:
                issue('nonpositive_episode_duration', str(duration_ns))
            topic_metadata = metadata['topics_with_message_count']
            topics = {x['topic_metadata']['name']: x for x in topic_metadata}
            if len(topics) != len(topic_metadata):
                issue('duplicate_topic_name', 'metadata contains repeated topic names')
            signature = canonical(sorted((name, entry['topic_metadata']['type'], entry['topic_metadata']['serialization_format']) for name, entry in topics.items()))
            signatures[signature] += 1
            row.update(topic_count=len(topics), topic_signature=signature)
            topic_sum = sum(x['message_count'] for x in topics.values())
            if topic_sum != metadata['message_count']:
                issue('topic_message_sum_mismatch', f'topics={topic_sum}; bag={metadata["message_count"]}')
            bag_files = metadata.get('files', [])
            listed_files = set(metadata.get('relative_file_paths', []))
            actual_mcaps = {Path(k).name for k in local if k.endswith('.mcap')}
            if listed_files != actual_mcaps or {f['path'] for f in bag_files} != actual_mcaps:
                issue('metadata_file_list_mismatch', f'metadata={sorted(listed_files)}, files={sorted(actual_mcaps)}')
            if sum(f['message_count'] for f in bag_files) != metadata['message_count']:
                issue('file_message_sum_mismatch', 'files message counts differ from bag count')
            if len(bag_files) == 1:
                bf = bag_files[0]
                if bf['starting_time']['nanoseconds_since_epoch'] != start_ns or bf['duration']['nanoseconds'] != duration_ns:
                    issue('file_time_metadata_mismatch', 'single MCAP time metadata differs from bag time metadata')
            image_counts = {}
            for name, entry in topics.items():
                n, typ = entry['message_count'], entry['topic_metadata']['type']
                rate = n / duration_s if duration_s > 0 else 0
                per_topic[name]['message_count'] += n
                per_topic[name]['episode_count'] += 1
                per_topic[name]['rates'].append(rate)
                topic_rows.append(dict(episode=rel, topic=name, type=typ, message_count=n, mean_rate_hz=rate))
                if n <= 0:
                    issue('empty_topic', name)
                if typ == 'sensor_msgs/msg/CompressedImage':
                    image_counts[name] = n
            row['image_message_count'] = sum(image_counts.values())
            row['camera_count_ratio_min_max'] = min(image_counts.values()) / max(image_counts.values()) if image_counts and max(image_counts.values()) else None
            if row['camera_count_ratio_min_max'] is not None and row['camera_count_ratio_min_max'] < .95:
                issue('camera_count_imbalance', canonical(image_counts), 'manual_review')
            if weak:
                source_topic = weak.get('source_timestamps_topic')
                row.update(weak_label_samples=weak.get('samples'), source_timestamps_topic=source_topic,
                           source_topic_message_count=topics.get(source_topic, {}).get('message_count'),
                           move_state_message_count=topics.get('/move/State', {}).get('message_count'))
                if source_topic not in topics:
                    issue('weak_source_topic_missing', str(source_topic))
                elif topics[source_topic]['message_count'] != weak.get('samples'):
                    issue('weak_samples_source_count_mismatch', f'samples={weak.get("samples")}, {source_topic}={topics[source_topic]["message_count"]}')
                if row['move_state_message_count'] != weak.get('samples'):
                    issue('weak_samples_move_count_mismatch', f'samples={weak.get("samples")}, /move/State={row["move_state_message_count"]}')
        if weak:
            assumptions = weak.get('assumptions', {})
            encoding = weak.get('orientation_encoding')
            windows = weak.get('detected_windows', {})
            assumption_variants[canonical(assumptions)] += 1
            encoding_variants[canonical(encoding)] += 1
            row.update(label_status=weak.get('status'), label_method=windows.get('method'),
                       label_confidence=windows.get('confidence'), assumptions=assumptions,
                       coordinate_convention=weak.get('coordinate_convention'), orientation_encoding=encoding)
            for k in ['outbound_start_s', 'outbound_end_s', 'return_start_s', 'return_end_s']:
                row[k] = windows.get(k)
            issue('estimated_base_not_measured', 'Base positions are explicitly declared estimated weak labels, not measured odometry', 'weak_label_risk')
            issue('fixed_motion_template', canonical(assumptions), 'weak_label_risk')
            issue('confidence_not_calibrated', f'Declared confidence {windows.get("confidence")}; no accuracy calibration available in final files', 'weak_label_risk')
            if encoding == 'yaw radians directly in odometry.orientation.w':
                issue('nonstandard_yaw_in_orientation_w', 'Producer declares scalar yaw in orientation.w; consumer must explicitly decode convention. Metadata audit alone does not inspect actual MCAP quaternion fields.', 'weak_label_risk')
            if 'fallback' in str(windows.get('method', '')):
                issue('fallback_movement_window', str(windows.get('method')), 'manual_review')
            if metadata:
                checks, segment_issues, segments = inspect_segments(weak.get('motion_segments', []), row['duration_s'], windows, assumptions, args.speed_review_threshold_mps)
                row.update(checks)
                episode_issues.extend(segment_issues)
                segment_rows.extend(dict(episode=rel, **s) for s in segments)
                row['pre_outbound_s'] = row.get('outbound_start_s')
                if row.get('return_start_s') is not None and row.get('outbound_end_s') is not None:
                    row['outbound_return_dwell_s'] = row['return_start_s'] - row['outbound_end_s']
                if row.get('return_end_s') is not None:
                    row['post_return_s'] = row['duration_s'] - row['return_end_s']
        row['known_error_flags'] = sorted({f['code'] for f in episode_issues if f['category'] == 'known_error'})
        row['manual_review_flags'] = sorted({f['code'] for f in episode_issues if f['category'] == 'manual_review'})
        row['weak_label_risk_flags'] = sorted({f['code'] for f in episode_issues if f['category'] == 'weak_label_risk'})
        row['info_flags'] = sorted({f['code'] for f in episode_issues if f['category'] == 'info'})
        row['metadata_structure_pass'] = not row['known_error_flags']
        row['review_priority'] = ('exclude_pending_repair' if row['known_error_flags'] else
                                  'priority_video_and_label_review' if row['manual_review_flags'] else
                                  'sample_video_and_label_review')
        row['full_body_supervision_ready'] = False
        episodes.append(row)
        findings.extend(dict(episode=rel, **f) for f in episode_issues)

    if signatures:
        canonical_signature = signatures.most_common(1)[0][0]
        for row in episodes:
            if row.get('topic_signature') != canonical_signature:
                row['known_error_flags'].append('topic_signature_differs_from_majority')
                row['metadata_structure_pass'] = False
                row['review_priority'] = 'exclude_pending_repair'
                findings.append(dict(episode=row['episode'], code='topic_signature_differs_from_majority', category='known_error', detail='Topic name/type/encoding set differs from majority'))

    batches = {}
    for batch in sorted({r['batch'] for r in episodes}):
        rows = [r for r in episodes if r['batch'] == batch]
        ids = {int(r['episode_id'].split('_')[-1]) for r in rows}
        start, end = min(r['start_ns'] for r in rows if 'start_ns' in r), max(r['end_ns'] for r in rows if 'end_ns' in r)
        batches[batch] = dict(episodes=len(rows), files=sum(r['local_file_count'] for r in rows),
                             bytes=sum(r['local_bytes'] for r in rows), message_count=sum(r.get('message_count', 0) for r in rows),
                             duration_sum_s=sum(r.get('duration_s', 0) for r in rows),
                             duration_distribution_s=distribution([r['duration_s'] for r in rows if 'duration_s' in r]),
                             start_beijing=iso(start), end_beijing=iso(end),
                             missing_ids_within_number_range=sorted(set(range(min(ids), max(ids)+1)) - ids),
                             methods=dict(collections.Counter(r.get('label_method') for r in rows)),
                             confidence_counts=dict(collections.Counter(str(r.get('label_confidence')) for r in rows)))
    intervals = sorted((r['start_ns'], r['end_ns'], r['episode']) for r in episodes if 'start_ns' in r)
    inter_episode_overlaps = [dict(first=a[2], second=b[2], overlap_s=(a[1]-b[0])/1e9)
                             for a, b in zip(intervals, intervals[1:]) if b[0] < a[1]]
    flag_counts = {category: dict(collections.Counter(f['code'] for f in findings if f['category'] == category))
                   for category in ['known_error', 'weak_label_risk', 'manual_review', 'info']}
    summary = dict(
        generated_at=dt.datetime.now(TZ).isoformat(), source=str(source),
        scope='All local metadata.yaml and estimated_move_state.json; file stat versus transfer manifest/completion records. No MCAP payload decoding or SHA recomputation.',
        limitations=['Metadata counts are declarations until compared with decoded MCAP.',
                     'Matching source size and recorded SHA provenance is not independent byte-for-byte source verification.',
                     'Recorded confidence is not a measured probability of label correctness.',
                     'A closed synthetic trajectory does not establish real return accuracy.',
                     'Speed and velocity jumps assume piecewise-linear interpolation; hardware limits are unknown.',
                     'No episode is certified full-body-training-ready by this metadata-only audit.'],
        episodes=len(episodes), local_files=len(all_files), local_bytes=sum(all_files.values()),
        recording_duration_sum_s=sum(r.get('duration_s', 0) for r in episodes),
        start_beijing=iso(intervals[0][0]) if intervals else None,
        end_beijing=iso(max(r[1] for r in intervals)) if intervals else None,
        message_count=sum(r.get('message_count', 0) for r in episodes),
        image_message_count=sum(r.get('image_message_count', 0) for r in episodes),
        batches=batches,
        transfer=dict(manifest_scanned_at=manifest_doc.get('scanned_at'), manifest_files=len(manifest),
                      manifest_bytes=sum(v['size'] for v in manifest.values()), manifest_missing=manifest_missing,
                      local_extras=local_extras, size_mismatch=size_mismatch,
                      completion_record_missing=completion_missing, completion_record_stale=completion_stale,
                      completion_sha_record_invalid=completion_hash_invalid,
                      sha256_recomputed=False, last_recorded_completion=max((v.get('completed_at', '') for v in completed.values()), default=None)),
        topic_signature_variants=[dict(signature=json.loads(k), episodes=v) for k, v in signatures.items()],
        topics={name: dict(message_count=d['message_count'], episode_count=d['episode_count'],
                           mean_rate_hz=distribution(d['rates'])) for name, d in sorted(per_topic.items())},
        assumption_variants=[dict(assumptions=json.loads(k), episodes=v) for k, v in assumption_variants.items()],
        orientation_encoding_variants=[dict(encoding=json.loads(k), episodes=v) for k, v in encoding_variants.items()],
        method_counts=dict(collections.Counter(r.get('label_method') for r in episodes)),
        confidence_counts=dict(collections.Counter(str(r.get('label_confidence')) for r in episodes)),
        duration_distribution_s=distribution([r['duration_s'] for r in episodes if 'duration_s' in r]),
        max_segment_mean_speed_distribution_mps=distribution([r['max_segment_mean_speed_mps'] for r in episodes if r.get('max_segment_mean_speed_mps') is not None]),
        speed_review_threshold_mps=args.speed_review_threshold_mps,
        speed_definition='Template segment displacement divided by segment duration: segment MEAN velocity under straight-segment motion. Not actual MCAP peak speed. Smooth interpolation can have higher peak speed.',
        episode_boundary_rounding_tolerance_s=0.001,
        episodes_above_segment_mean_speed_mps={str(t): sum(r.get('max_segment_mean_speed_mps', 0) > t for r in episodes)
                                 for t in [.5, 1, 1.5, 2, 3]},
        max_segment_mean_velocity_change_distribution_mps=distribution([r['max_segment_mean_velocity_change_mps'] for r in episodes if r.get('max_segment_mean_velocity_change_mps') is not None]),
        pre_outbound_distribution_s=distribution([r['pre_outbound_s'] for r in episodes if r.get('pre_outbound_s') is not None]),
        placement_dwell_distribution_s=distribution([r['outbound_return_dwell_s'] for r in episodes if r.get('outbound_return_dwell_s') is not None]),
        post_return_distribution_s=distribution([r['post_return_s'] for r in episodes if r.get('post_return_s') is not None]),
        inter_episode_time_overlaps=inter_episode_overlaps,
        flag_counts=flag_counts,
        review_priority_counts=dict(collections.Counter(r['review_priority'] for r in episodes)),
        metadata_structure_pass_episodes=sum(r['metadata_structure_pass'] for r in episodes),
        full_body_supervision_ready_episodes=0,
        full_body_supervision_ready_reason='Not assessed: needs actual message values, time alignment, kinematics/action contract and visual outcome review.',
        fastest_template_episodes=[dict(episode=r['episode'], max_segment_mean_speed_mps=r['max_segment_mean_speed_mps'],
                                       method=r['label_method']) for r in sorted(episodes, key=lambda r: r.get('max_segment_mean_speed_mps') or -1, reverse=True)[:25]],
    )
    # Write only after every input has been read, so the report is a self-contained snapshot.
    output.mkdir(parents=True, exist_ok=True)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    write_csv(output / 'episodes.csv', [{k: v for k, v in r.items() if k != 'topic_signature'} for r in episodes])
    write_csv(output / 'segments.csv', segment_rows)
    write_csv(output / 'topics.csv', topic_rows)
    (output / 'findings.json').write_text(json.dumps(findings, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    manifest_rows = [dict(episode=r['episode'], local_path=str(source/r['episode']),
                          review_priority=r['review_priority'], metadata_structure_pass=r['metadata_structure_pass'],
                          full_body_supervision_ready=False, known_errors=r['known_error_flags'],
                          manual_review=r['manual_review_flags'], weak_label_risks=r['weak_label_risk_flags'],
                          informational_flags=r['info_flags'],
                          deleted=False) for r in episodes]
    (output / 'screening_manifest.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in manifest_rows), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['episodes', 'local_files', 'local_bytes', 'recording_duration_sum_s', 'metadata_structure_pass_episodes', 'review_priority_counts', 'flag_counts']}, ensure_ascii=False, indent=2))
    print(f'Artifacts: {output}')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--transfer', type=Path, help='Transfer manifest directory; default sibling transfer/')
    parser.add_argument('--speed-review-threshold-mps', type=float, default=1.0,
                        help='Manual-review heuristic only, not a hardware validity limit (default: 1.0)')
    args = parser.parse_args()
    if not math.isfinite(args.speed_review_threshold_mps) or args.speed_review_threshold_mps <= 0:
        parser.error('speed review threshold must be finite and positive')
    audit(args)


if __name__ == '__main__':
    main()
