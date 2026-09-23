#!/usr/bin/env python3
"""Explain arm-transition attrition without changing labels or validity rules."""
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np

TASK = Path(__file__).resolve().parents[1]


def intervals(mask, times):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    return [[float(times[a]), float(times[b - 1])] for a, b in
            zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    masks = list(map(json.loads, (TASK/'results/demo_772_selection/mask_manifest.jsonl').read_text().splitlines()))
    selected = {r['episode']: r for r in map(json.loads, (TASK/'results/demo_772_selection/candidates.jsonl').read_text().splitlines())}
    rows = []
    for m in masks:
        ep = m['episode']; stem = ep.replace('/', '_')
        with np.load(m['aligned_npz_path'], allow_pickle=False) as z:
            parent = {k: z[k] for k in z.files}
        path = TASK/'results/full_pool_reader_20260917_v1/cases'/stem/'composed/episode.npz'
        included = path.exists()
        if not included:
            path = TASK/'results/partial_phase_pool_20260917_v1/cases'/stem/'composed/episode.npz'
        with np.load(path, allow_pickle=False) as z:
            current = {k: z[k] for k in z.files}
        meta = json.loads(path.with_suffix('.json').read_text())
        t = (parent['time_ns'] - selected[ep]['source_start_ns']) / 1e9
        count = len(t) - 49
        pstart = parent['chunk_start_candidate_valid'][:count]
        cstart = current['chunk_start_candidate_valid'][:count]
        # Separate missing current base labels from pre-existing parent restrictions.
        base_bad = ~current['base_candidate_present']
        missing = np.lib.stride_tricks.sliding_window_view(base_bad, 50).any(1)
        row = dict(episode=ep, reader_included=included, split=m['parent_split'], arms={})
        for arm, sl in [('left', slice(0, 7)), ('right', slice(7, 14))]:
            # Descriptive 5 degree/50 frame statistic, NOT a new acceptance rule.
            a = np.ptp(np.lib.stride_tricks.sliding_window_view(parent['action'][:, sl], 50, axis=0), axis=-1).max(1)
            h = np.ptp(np.lib.stride_tricks.sliding_window_view(parent['state'][:, sl], 50, axis=0), axis=-1).max(1)
            active = np.isfinite(a) & (a > 5)
            eligible = active & pstart
            lost = eligible & ~cstart
            row['arms'][arm] = dict(raw_transition_starts=int(active.sum()),
                parent_valid_transition_starts=int(eligible.sum()), retained=int((active & cstart).sum()),
                lost_with_missing_base=int((lost & missing).sum()), lost_other=int((lost & ~missing).sum()),
                parent_valid_command_and_feedback_transition_starts=int((eligible & (h > 5)).sum()),
                missing_start_intervals_s=intervals(lost, t),
                retained_start_intervals_s=intervals(active & cstart, t))
        pr = meta.get('partial_phase_report', {})
        row['unresolved_stages'] = pr.get('unresolved_stages', [])
        rows.append(row)
    summary = dict(schema='f1_transition_retention_diagnosis_v1', training_approved=False,
        sources=len(rows), threshold_scope='descriptive >5 degrees inside H50, not a mask or loss decision', arms={})
    for arm in ('left', 'right'):
        v = [r['arms'][arm] for r in rows]
        keys = ['raw_transition_starts', 'parent_valid_transition_starts', 'retained', 'lost_with_missing_base',
                'lost_other', 'parent_valid_command_and_feedback_transition_starts']
        summary['arms'][arm] = {k: sum(x[k] for x in v) for k in keys}
        summary['arms'][arm].update(parent_valid_sources=sum(x['parent_valid_transition_starts'] > 0 for x in v),
            retained_sources=sum(x['retained'] > 0 for x in v),
            sources_losing_all_transitions=sum(x['parent_valid_transition_starts'] > 0 and x['retained'] == 0 for x in v))
    for name, value in [('summary.json', summary), ('episodes.json', rows)]:
        (args.output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
