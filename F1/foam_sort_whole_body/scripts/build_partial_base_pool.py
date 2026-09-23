#!/usr/bin/env python3
"""Retain independently supported phases instead of rejecting whole episodes.

No robot access, no source changes, no training approval. Unknown base targets
stay NaN. Existing all-19D reader masks and H50 restrictions remain in force.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np
from build_arm_guided_joint_proposals import TASK, ROUTE, sha, write
from compose_base_candidate import compose_arrays
from repair_joint_static_gaps import anchored_pairs

DIRECTION = dict(back_out='image_up', right_out='image_left',
                 forward_out='image_down', back_return='image_up', left_return='image_right')


def build_partial(case, pairs, parent, t, tail, acceleration=.13, speed=.65):
    keys = {(b['role'], b['boundary']): b for b in case['boundaries']}
    v = np.full((len(t), 3), np.nan, np.float32)
    static = np.zeros(len(t), bool)
    blocked = np.zeros(len(t), bool)
    for p in pairs:
        take = (t >= p['start_s']) & (t < p['end_s'])
        if p['status'] == 'stationary_support':
            static |= take
        if p['status'] == 'conflict' or not p.get('pose_ok', True) or not p.get('fresh', True) or p.get('repeated', False):
            blocked |= take
    moves, rejected = [], []
    for role, (axis, distance) in ROUTE.items():
        bounds = [keys.get((role, k)) for k in ('start_s', 'end_s')]
        if any(b is None or b['primary']['status'] != 'bounded_visual_proposal'
               or not b['stable_under_pixel_thresholds'] for b in bounds):
            rejected.append(dict(role=role, reason='unresolved_boundary_local_only'))
            continue
        a, b = [float(x['primary']['estimate_s']) for x in bounds]
        if not 0 <= a < b or a >= tail:
            rejected.append(dict(role=role, reason='invalid_or_outside_parent_tail'))
            continue
        # A valid early part can survive a later parent reset mask. Evaluate
        # the original phase curve, never compress the phase to the crop.
        moves.append(dict(role=role, start_s=a, end_s=b, axis=axis, distance_prior_m=distance))
    overlapping = set()
    for i, a in enumerate(moves):
        for b in moves[i+1:]:
            if min(a['end_s'], b['end_s']) > max(a['start_s'], b['start_s']):
                overlapping.update((a['role'], b['role']))
            if list(ROUTE).index(a['role']) < list(ROUTE).index(b['role']) and a['start_s'] > b['start_s']:
                overlapping.update((a['role'], b['role']))
    accepted = []
    for m in moves:
        if m['role'] in overlapping:
            rejected.append(dict(role=m['role'], reason='overlap_or_reversed_route_order'))
            continue
        a, b = m['start_s'], m['end_s']
        take = (t >= a) & (t <= b)
        interior = (t > a+.5) & (t < b-.5)
        # Isolate only contradicted intervals; do not fabricate a zero stop.
        conflict = interior & static
        for p in pairs:
            if p['status'].startswith('image_') and p['status'] != DIRECTION[m['role']]:
                conflict |= take & (t >= p['start_s']) & (t < p['end_s'])
        blocked |= conflict
        scale = min(1., acceleration*(b-a)**2/(6*abs(m['distance_prior_m'])),
                    speed*(b-a)/(1.5*abs(m['distance_prior_m'])))
        if scale < 1:
            scale *= 1-1e-6
        u = (t[take]-a)/(b-a)
        v[take] = 0
        v[take, m['axis']] = m['distance_prior_m']*scale*6*u*(1-u)/(b-a)
        accepted.append(dict(**m, scale=scale, conflict_frames=int(conflict.sum()),
                             new_prior_distance_m=m['distance_prior_m']*scale))
        for kind, value in [('start_s', a), ('end_s', b)]:
            envelope = keys[m['role'], kind]['primary']['envelope_s']
            if kind == 'start_s':
                neighbours = [p['end_s'] for p in pairs if p['status'] == 'stationary_support' and envelope[0]-.01 <= p['end_s'] <= value]
                if neighbours:
                    idx = (t >= max(neighbours)) & (t < value)
                    v[idx & ~np.isfinite(v).any(1)] = 0
            else:
                neighbours = [p['start_s'] for p in pairs if p['status'] == 'stationary_support' and value <= p['start_s'] <= envelope[1]+.01]
                if neighbours:
                    idx = (t > value) & (t < min(neighbours))
                    v[idx & ~np.isfinite(v).any(1)] = 0
    v[static & ~np.isfinite(v).any(1)] = 0
    # Conservatively isolate gripper-transition/moving-label clashes, locally.
    # This is a task-specific consistency gate, not inferred measured stops.
    gripper_changes = np.r_[False, (np.abs(np.diff(parent['action'][:, 14:16], axis=0)) > 1).any(1)]
    grip_conflict = gripper_changes & np.isfinite(v).all(1) & (np.abs(v) > 1e-7).any(1)
    blocked |= grip_conflict
    v[blocked | (t >= tail)] = np.nan
    present = np.isfinite(v).all(1)
    data = compose_arrays(parent, dict(time_ns=parent['time_ns'], base_candidate_v=v,
                          base_candidate_present=present, base_timing_reviewed=present), 50)
    # Check actual exported samples; no acceleration calculation across unknowns.
    pair = present[1:] & present[:-1]
    dv = np.linalg.norm(np.diff(v, axis=0)[pair], axis=1)/np.diff(t)[pair]
    if len(dv) and float(dv.max()) > acceleration+1e-5:
        raise ValueError('exported known-region acceleration exceeds policy')
    cov = np.zeros(len(t), bool)
    starts = np.flatnonzero(data['chunk_start_candidate_valid'])
    for i in starts:
        cov[i:i+50] = True
    moving = present & np.any(np.nan_to_num(v) != 0, axis=1)
    events = {}
    for j, arm in [(14, 'left'), (15, 'right')]:
        delta = np.r_[0, np.diff(parent['action'][:, j])]
        for sign, name in [(1, 'closing'), (-1, 'opening')]:
            ix = np.flatnonzero((delta*sign > 1) & (t < tail))
            events[arm+'_'+name] = dict(total_change_frames=len(ix), covered_change_frames=int(cov[ix].sum()),
                                      source_indices=ix.tolist(), covered_indices=ix[cov[ix]].tolist())
    report = dict(status='partial_episode_candidate', training_approved=False,
                  motion_stages=accepted, unresolved_stages=rejected,
                  valid_starts=len(starts), covered_frames=int(cov.sum()),
                  transition_starts=sum(bool(moving[i:i+50].any() and (~moving[i:i+50]).any()) for i in starts),
                  stationary_starts=sum(bool(not moving[i:i+50].any()) for i in starts),
                  moving_starts=sum(bool(moving[i:i+50].any()) for i in starts),
                  gripper_events=events, local_conflict_frames=int(blocked.sum()),
                  gripper_motion_conflict_frames=int(grip_conflict.sum()),
                  max_known_pair_acceleration=float(max(dv, default=0)), parent_masks_relaxed=False,
                  limitations='partial windows; not a complete replay or proof of physical task success; base remains posthoc weak label')
    return data, report


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--limit', type=int); ap.add_argument('--bridge-static-holes', action='store_true')
    ap.add_argument('--refinement', type=Path, action='append',
                    help='Explicit derived refinement roots; default retains the original two pools')
    args = ap.parse_args(); out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    masks_path = TASK/'results/demo_772_selection/mask_manifest.jsonl'
    selected_path = TASK/'results/demo_772_selection/candidates.jsonl'
    masks = {r['episode']: r for r in map(json.loads, masks_path.read_text().splitlines())}
    selected = {r['episode']: r for r in map(json.loads, selected_path.read_text().splitlines())}
    roots = args.refinement or [TASK/'results/arm_guided_dense_20260917_v3', TASK/'results/arm_guided_full_pool_20260917/refinement']
    cases = [p for root in roots for p in sorted((root/'cases').glob('*.json'))]
    if args.limit: cases = cases[:args.limit]
    results = []; seen = set()
    for path in cases:
        case = json.loads(path.read_text()); ep = case['episode']; assert ep not in seen; seen.add(ep)
        m = masks[ep]; row = selected[ep]; assert case['parent_split'] == m['parent_split'] == row['parent_split']
        for name, expected in case['inputs'].items(): assert sha(name) == expected
        assert sha(m['aligned_npz_path']) == m['aligned_npz_sha256']
        with np.load(m['aligned_npz_path'], allow_pickle=False) as z: parent = {k: z[k] for k in z.files}
        pp = path.parents[1]/'pairs'/path.name; pr = json.loads(pp.read_text()); assert pr['cache_sha256'] == case['cache_sha256']
        pairs, bridges = anchored_pairs(pr) if args.bridge_static_holes else (pr['pairs'], [])
        t = (parent['time_ns']-row['source_start_ns'])/1e9
        data, report = build_partial(case, pairs, parent, t, m['tail_mask_start_s'])
        report.update(episode=ep, split=m['parent_split'], bridges=bridges,
                      inputs={str(path): sha(path), str(pp): sha(pp), m['aligned_npz_path']: m['aligned_npz_sha256']})
        dest = out/'cases'/ep.replace('/', '_')/'composed'; dest.mkdir(parents=True)
        np.savez_compressed(dest/'episode.npz', **data)
        meta = json.loads(Path(m['aligned_npz_path']).with_suffix('.json').read_text())
        meta.update(version='partial_phase_pool_v1', cleaning_version='partial_phase_pool_v1', training_approved=False,
                    base_label_origin='posthoc_local_visual_phase_and_amplitude_limited_route_prior',
                    chunk_candidate_starts=report['valid_starts'], partial_phase_report=report)
        write(dest/'episode.json', meta); report['npz_sha256'] = sha(dest/'episode.npz')
        results.append(report)
        write(out/'progress.json', dict(processed=len(results), scope=len(cases), with_windows=sum(x['valid_starts']>0 for x in results)))
        if len(results)%25 == 0: print(json.dumps(dict(processed=len(results), scope=len(cases))), flush=True)
    summary = dict(schema='f1_partial_phase_pool_v1', scope=len(results), training_approved=False,
                   with_windows=sum(r['valid_starts']>0 for r in results),
                   with_moving_windows=sum(r['moving_starts']>0 for r in results),
                   with_stationary_windows=sum(r['stationary_starts']>0 for r in results),
                   with_transition_windows=sum(r['transition_starts']>0 for r in results),
                   total_starts=sum(r['valid_starts'] for r in results),
                   by_split=dict(Counter(r['split'] for r in results if r['valid_starts'])),
                   sources_sha256={str(p):sha(p) for p in [Path(__file__), masks_path, selected_path]}, results=results)
    write(out/'summary.json', summary); (out/'method_snapshot.py').write_bytes(Path(__file__).read_bytes())
    print(json.dumps({k:v for k,v in summary.items() if k not in ('results','sources_sha256')}, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
