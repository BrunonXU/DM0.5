#!/usr/bin/env python3
"""Publish a read-only comparison of an imported sidecar on the review page.

This samples the actual 30 Hz sidecar causally at the existing video preview
grid. Unknowns remain JSON null; no candidate or review decision is generated.
"""
import argparse
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path

import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-dir', type=Path, required=True)
    parser.add_argument('--web-root', type=Path, required=True)
    args = parser.parse_args()
    imported, web = args.import_dir.resolve(), args.web_root.resolve()
    manifest = json.loads((imported / 'manifest.json').read_text())
    assert manifest['schema'] == 'f1_base_repair_import_manifest_v1'
    assert manifest['status'] == 'complete'
    for name, identity in manifest['artifacts'].items():
        path = (imported / name).resolve()
        assert path.is_relative_to(imported) and sha(path) == identity['sha256'], name
    phase_path = imported / 'review_input.json'
    phase = json.loads(phase_path.read_text())
    entries = json.loads((web / 'manifest.json').read_text())['episodes']
    entry = next(e for e in entries if e['episode'] == phase['episode'])
    data_path = (web / entry['data_url']).resolve()
    data = json.loads(data_path.read_text())
    assert data['episode'] == phase['episode']
    assert data['source_origin_ns'] == phase['source_origin_ns']
    assert entry['source_duration_s'] == phase['source_duration_s']
    origin = int(phase['source_origin_ns'])
    query_ns = np.array([origin + int((Decimal(str(t)) * 10**9).to_integral_value(
        rounding=ROUND_HALF_EVEN)) for t in data['time_s']], dtype=np.int64)
    with np.load(imported / 'candidate/base_relabel.npz', allow_pickle=False) as z:
        times = z['time_ns']
        indices = np.searchsorted(times, query_ns, side='right') - 1
        ages = (query_ns - times[np.maximum(indices, 0)]) / 1e9
        valid = (indices >= 0) & (ages >= 0) & (ages <= .034)
        assert bool(np.all(valid)), 'Preview grid leaves the sidecar coverage'
        values = z['base_candidate_v'][indices]
        present = z['base_candidate_present'][indices]
        reviewed = z['base_timing_reviewed'][indices]
        assert np.array_equal(np.isfinite(values).all(axis=1), present)
        payload = {
            'schema': 'f1_base_relabel_preview_v1', 'episode': phase['episode'],
            'source_origin_ns': phase['source_origin_ns'], 'time_s': data['time_s'],
            'base_v': [[float(v) if np.isfinite(v) else None for v in row] for row in values],
            'candidate_present': present.tolist(), 'timing_reviewed': reviewed.tolist(),
            'sidecar_index': indices.tolist(), 'sidecar_age_s': ages.tolist(),
            'phase': phase, 'phase_sha256': sha(phase_path), 'training_approved': False,
            'review_notice': ('已标出时段的时刻经过复核，速度仍是路线先验弱估计；未知段留空，正式reader与真机尚未验收。'
                              if phase['segments'] and all(s['timing_status']=='reviewed' for s in phase['segments'])
                              else '本页包含尚未复核的时段建议；速度为路线先验弱估计，未知段留空，尚未通过训练验收。'),
            'sampling': 'causal sample of the imported 30 Hz sidecar; no interpolation or zero fill',
            'provenance': {'import_manifest_sha256': sha(imported / 'manifest.json'),
                           'sidecar_sha256': sha(imported / 'candidate/base_relabel.npz'),
                           'original_preview_sha256': sha(data_path),
                           'publisher_sha256': sha(Path(__file__))}}
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    target = web / 'relabel_previews' / (entry['id'] + '_' + hashlib.sha256(encoded).hexdigest()[:12] + '.json')
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        assert target.read_bytes() == encoded, 'Use a new preview version'
    else:
        target.write_bytes(encoded)
    index_path = web / 'relabel_preview_manifest.json'
    index = json.loads(index_path.read_text()) if index_path.exists() else {
        'schema': 'f1_base_relabel_preview_index_v1', 'episodes': []}
    index['episodes'] = [e for e in index['episodes'] if e['episode'] != phase['episode']]
    index['episodes'].append({'episode': phase['episode'], 'url': str(target.relative_to(web)),
                              'sha256': sha(target), 'training_approved': False})
    tmp = index_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(index_path)
    print(json.dumps({'preview': str(target), 'samples': len(query_ns),
                      'known': int(present.sum()), 'reviewed': int(reviewed.sum()),
                      'max_age_s': float(ages.max())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
