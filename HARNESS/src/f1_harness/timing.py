"""Inspect retained camera timestamps without certifying clocks or rewriting data."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics


def _stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return dict(min=min(values), median=statistics.median(values),
                p95=ordered[min(len(ordered)-1, int(.95*len(ordered)))], max=max(values))


def inspect_timing(directory):
    path = Path(directory) / 'capture.json'
    raw = path.read_bytes()
    capture = json.loads(raw)
    groups = defaultdict(list)
    for row in capture['records']:
        if row.get('kind') == 'image':
            groups[row['topic']].append(row)
    streams = []
    for topic, rows in sorted(groups.items()):
        # Preserve recorded callback order: sorting by source time would hide reversals.
        valid = [r for r in rows if isinstance(r.get('source_ns'), int) and r['source_ns'] > 0]
        deltas = [(b['source_ns']-a['source_ns'])/1e6 for a,b in zip(valid, valid[1:])]
        receipt = [(b['received_ns']-a['received_ns'])/1e6 for a,b in zip(rows, rows[1:])]
        stamps = defaultdict(set)
        for r in valid:
            stamps[r['source_ns']].add(r.get('blob_sha256'))
        offsets = [r['received_ns']-r['source_ns'] for r in valid]
        streams.append(dict(topic=topic, retained_frames=len(rows),
            missing_or_zero_source_stamp=len(rows)-len(valid),
            repeated_source_intervals=sum(v == 0 for v in deltas),
            reversed_source_intervals=sum(v < 0 for v in deltas),
            repeated_stamp_with_changed_content=sum(len(v)>1 for v in stamps.values()),
            receipt_not_advancing=sum(v <= 0 for v in receipt),
            retained_source_interval_ms=_stats(deltas), retained_receipt_interval_ms=_stats(receipt),
            source_receipt_offset_range_ms=(max(offsets)-min(offsets))/1e6 if offsets else None))
    # Pairing below measures retained samples, not original stream synchronization.
    pairs = []
    cameras = defaultdict(dict)
    for topic, rows in groups.items():
        parts = topic.split('/')
        if len(parts) >= 4 and parts[1] == 'camera':
            cameras[parts[2]]['depth' if '/depth/' in topic else 'rgb'] = rows
    for camera, streams_by_kind in sorted(cameras.items()):
        if set(streams_by_kind) != {'rgb','depth'}:
            pairs.append(dict(camera=camera, missing_stream=True))
            continue
        rgb, depth = streams_by_kind['rgb'], streams_by_kind['depth']
        skews = []
        for r in rgb:
            if not r.get('source_ns'):
                continue
            distances = [abs(r['source_ns']-d['source_ns'])/1e6 for d in depth if d.get('source_ns')]
            if distances:
                skews.append(min(distances))
        pairs.append(dict(camera=camera, missing_stream=False,
                          retained_nearest_source_skew_ms=_stats(skews)))
    sampling = dict(image_interval_s=capture.get('image_interval_s'),
                    received_counts=capture.get('received_counts'),
                    retained_counts=capture.get('retained_counts'),
                    complete_window=capture.get('complete_window'))
    return dict(schema='f1-harness-timing-audit-v1', capture=str(path.resolve()),
                capture_sha256=hashlib.sha256(raw).hexdigest(), streams=streams, pairings=pairs,
                retention=sampling, source_clock_verified=False, geometry_admitted=False,
                motion_authorized=False,
                limitations=['Retained/downsampled frames cannot establish absence of dropped or repeated live timestamps.',
                             'Source/receipt offset variation is not absolute capture latency or a clock calibration.',
                             'Nearest source stamps do not prove synchronized RGB-D exposure.'])
