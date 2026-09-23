#!/usr/bin/env python3
"""Recover only unknown, visually stationary arm-transition samples.

Additional raw frames use the existing pixel/pose thresholds. Known labels,
accepted moving phases, conflicting evidence and original masks are preserved.
This does not approve training or change the loss contract.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import numpy as np
from compose_base_candidate import compose_arrays
from refine_arm_guided_base_boundaries import make_pairs, TASK, sha, write
from repair_joint_static_gaps import anchored_pairs


def repair_one(job):
    row, m, cache, out = job
    ep = row['episode']; stem = ep.replace('/', '_')
    source = TASK/'results/full_pool_reader_20260917_v1/cases'/stem/'composed/episode.npz'
    if not source.exists():
        source = TASK/'results/partial_phase_pool_20260917_v1/cases'/stem/'composed/episode.npz'
    meta = json.loads(source.with_suffix('.json').read_text())
    assert sha(m['aligned_npz_path']) == m['aligned_npz_sha256']
    with np.load(m['aligned_npz_path'], allow_pickle=False) as z: parent = {k:z[k] for k in z.files}
    with np.load(source, allow_pickle=False) as z: previous = {k:z[k] for k in z.files}
    assert np.array_equal(previous['time_ns'], parent['time_ns'])
    assert np.array_equal(previous['action'][:,:16], parent['action'][:,:16], equal_nan=True)
    assert np.array_equal(previous['state'], parent['state'], equal_nan=True)
    make_pairs(ep, out/'evidence', cache)
    record_path = out/'evidence/pairs'/f'{stem}.json'
    record = json.loads(record_path.read_text())
    pairs, bridges = anchored_pairs(record)
    t = (parent['time_ns']-row['source_start_ns'])/1e9
    static = np.zeros(len(t), bool)
    for p in pairs:
        if p['status'] == 'stationary_support':
            static |= (t >= p['start_s']) & (t < p['end_s'])
    veto = np.zeros(len(t), bool)
    pr = meta.get('partial_phase_report')
    assert pr is not None, 'Existing verified reference must not be patched'
    for stage in pr['motion_stages']:
        veto |= (t >= stage['start_s']) & (t <= stage['end_s'])
    # Existing measured-motion/pose conflicts cannot be overruled by a new zero.
    old_paths = [TASK/'results/arm_guided_full_pool_20260917/refinement/pairs'/f'{stem}.json',
                 TASK/'results/arm_guided_dense_20260917_v3/pairs'/f'{stem}.json']
    old_path = next(p for p in old_paths if p.exists())
    old = json.loads(old_path.read_text())
    for p in old['pairs']:
        if p['status'].startswith('image_') or p['status'] == 'conflict' or not p['pose_ok'] or not p['fresh'] or p['repeated']:
            veto |= (t >= p['start_s']) & (t < p['end_s'])
    fill = static & ~veto & ~previous['base_candidate_present'] & (t < m['tail_mask_start_s'])
    v = previous['action'][:,16:19].copy(); v[fill] = 0
    present = np.isfinite(v).all(1)
    data = compose_arrays(parent, dict(time_ns=parent['time_ns'], base_candidate_v=v,
        base_candidate_present=present, base_timing_reviewed=present), 50)
    assert np.all(data['chunk_start_candidate_valid'][previous['chunk_start_candidate_valid']])
    assert np.array_equal(v[previous['base_candidate_present']], previous['action'][previous['base_candidate_present'],16:19])
    assert not data['action_candidate_valid'][m['tail_mask_start_index']:].any()
    pair = present[1:] & present[:-1]
    acc = np.linalg.norm(np.diff(v,axis=0)[pair],axis=1)/np.diff(t)[pair]
    assert max(acc, default=0) <= .13001
    report = dict(episode=ep, training_approved=False, filled_static_frames=int(fill.sum()),
        previous_starts=int(previous['chunk_start_candidate_valid'].sum()), starts=int(data['chunk_start_candidate_valid'].sum()),
        arms={}, bridges=bridges, inputs={str(source):sha(source), str(old_path):sha(old_path), str(record_path):sha(record_path)},
        policy='existing static thresholds; unknown only; original motion/conflicts/parent masks veto; no HAL targets')
    for name, sl in [('left',slice(0,7)),('right',slice(7,14))]:
        active = np.ptp(np.lib.stride_tricks.sliding_window_view(parent['action'][:,sl],50,axis=0),axis=-1).max(1)>5
        report['arms'][name] = dict(before=int((active & previous['chunk_start_candidate_valid'][:len(active)]).sum()),
            after=int((active & data['chunk_start_candidate_valid'][:len(active)]).sum()))
    dest = out/'cases'/stem/'composed'; dest.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(dest/'episode.npz', **data)
    meta.update(training_approved=False, transition_static_repair=report,
        version='transition_static_coverage_v1', cleaning_version='transition_static_coverage_v1',
        chunk_candidate_starts=report['starts'])
    write(dest/'episode.json',meta)
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for k in ['candidates','cache','output']: ap.add_argument('--'+k,type=Path,required=True)
    ap.add_argument('--workers',type=int,default=4)
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    rows=list(map(json.loads,args.candidates.read_text().splitlines()))
    masks={m['episode']:m for m in map(json.loads,(TASK/'results/demo_772_selection/mask_manifest.jsonl').read_text().splitlines())}
    jobs=[(r,masks[r['episode']],args.cache.resolve(),args.output.resolve())for r in rows]
    with ProcessPoolExecutor(max_workers=args.workers)as pool:
        results=[]
        for r in pool.map(repair_one,jobs):
            results.append(r);write(args.output/'progress.json',dict(complete=len(results),total=len(rows)))
    summary=dict(schema='f1_transition_static_repair_v1',training_approved=False,episodes=results,
        filled_static_frames=sum(r['filled_static_frames']for r in results),
        previous_starts=sum(r['previous_starts']for r in results),starts=sum(r['starts']for r in results),
        script_sha256=sha(__file__))
    write(args.output/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items()if k!='episodes'}))


if __name__=='__main__':main()
