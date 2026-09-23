#!/usr/bin/env python3
"""Expand the frozen IK evaluation to unseen episodes; never change source masks.

The extended run is model-consistency validation, not new native-SDK parity.
New arm targets do not depend on repaired chassis labels or image acceptance.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

import numpy as np
import scipy

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'robot'))
sys.path.insert(0, str(TASK/'models/DM0.5'))
from kinematics import ArmModel, ContinuousIK, LOW_DEG, HIGH_DEG, pose_error
from f1_data.reader import decode_rotation6d

CONFIG = TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.write('\n')


def load_arm_data(path):
    with np.load(path, allow_pickle=False) as z:
        state=z['state'][:,:14].astype(float).reshape(-1,2,7)
        raw=z['action'][:,:14].astype(float).reshape(-1,2,7)
        sv=z['state_valid'][:,:14].reshape(-1,2,7).all(-1)&np.isfinite(state).all(-1)
        av=z['action_candidate_valid'][:,:14].reshape(-1,2,7).all(-1)&np.isfinite(raw).all(-1)
        time=z['time_ns'].copy()
    mapped=np.clip(raw,LOW_DEG,HIGH_DEG)
    return state, raw, mapped, sv, av, time


def select_starts(sv, av, horizon=50):
    valid=[i for i in range(len(sv)-horizon+1) if sv[i] and av[i:i+horizon].all()]
    selected=[]
    for i in valid:
        if not selected or i>=selected[-1]+horizon: selected.append(i)
    if valid and valid[-1] not in selected: selected.append(valid[-1])
    return selected


def prepare(output):
    output.mkdir(parents=True,exist_ok=False)
    pool_path=TASK/'results/demo_772_selection/mask_manifest.jsonl'
    old=json.loads((TASK/'results/kinematics_validation_20260916/request_manifest.json').read_text())
    excluded={x['case_name'] for x in old['sources']}
    inventory=[]
    for line in pool_path.open():
        item=json.loads(line); path=Path(item['aligned_npz_path'])
        if sha(path)!=item['aligned_npz_sha256']: raise ValueError(f'Source changed: {path}')
        state,raw,q,sv,av,time=load_arm_data(path)
        if not np.allclose(np.diff(time),1e9/30,atol=1,rtol=0):
            raise ValueError('Unexpected sample interval')
        values=q[av]
        if not len(values): continue
        starts=[select_starts(sv[:,a],av[:,a]) for a in (0,1)]
        both=sv&av
        diff_valid=av[1:]&av[:-1]
        delta=np.abs(np.diff(q,axis=0))[diff_valid]
        span=np.max(values,axis=0)-np.min(values,axis=0)
        row={'episode':item['episode'],'batch':item['episode'].split('/')[0],
             'source_path':str(path),'source_sha256':sha(path),'split':item['parent_split'],
             'frames':len(state),'duration_s':len(state)/30,
             'valid_feedback_per_arm':sv.sum(0).tolist(),'valid_targets_per_arm':av.sum(0).tolist(),
             'chunk_counts_per_arm':[len(s) for s in starts],
             'max_source_step_deg':float(delta.max()) if len(delta) else 0.,
             'max_feedback_gap_deg':float(np.max(abs(q[both]-state[both]))) if both.any() else 0.,
             'clipped_fraction':float(np.any(raw[av]!=values,axis=-1).mean()),
             'wrist_violation_fraction':float((1.025*abs(values[:,5])+abs(values[:,6])>110.500001).mean()),
             'max_arm_span_deg':float(max(span)),
             'previously_tested':item['episode'].replace('/','_') in excluded,
             'features':np.r_[values.min(0),values.max(0),values.mean(0),values.std(0),
                              state[sv].min(0),state[sv].max(0)].tolist()}
        inventory.append(row)
    selected=[]
    for batch in sorted({r['batch'] for r in inventory}):
        pool=[r for r in inventory if r['batch']==batch and not r['previously_tested']
              and min(r['chunk_counts_per_arm'])>0]
        chosen=[]; reasons={}
        def add(row,reason):
            if row not in chosen: chosen.append(row)
            reasons.setdefault(row['episode'],[]).append(reason)
        # Include held-out source groups; this is an IK check, not model fitting.
        validation=sorted([r for r in pool if r['split']=='validation'],
                          key=lambda r:(-r['max_feedback_gap_deg'],r['episode']))
        for row in validation[:2]: add(row,'validation_high_feedback_gap')
        for score in ['max_source_step_deg','max_feedback_gap_deg','clipped_fraction',
                      'wrist_violation_fraction','max_arm_span_deg','duration_s']:
            add(sorted(pool,key=lambda r:(-r[score],r['episode']))[0],score)
        features=np.array([r['features'] for r in pool]);scale=np.ptp(features,axis=0)
        features=(features-features.mean(0))/np.where(scale>1e-6,scale,1)
        while len(chosen)<16:
            indices=[pool.index(r) for r in chosen]
            distances=((features[:,None]-features[indices])**2).sum(-1).min(-1)
            distances[indices]=-1
            add(pool[int(np.argmax(distances))],'farthest_joint_summary')
        for row in chosen:
            selected.append({**row,'selection_reasons':reasons[row['episode']]})
    assert len(selected)==64 and not any(r['previously_tested'] for r in selected)
    write_json(output/'pool_inventory.json',inventory)
    code={}
    for src in [Path(__file__),TASK/'robot/kinematics.py',TASK/'models/DM0.5/f1_data/reader.py']:
        rel=src.relative_to(TASK);dst=output/'code_snapshot'/rel
        dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
        code[str(rel)]=sha(src)
    write_json(output/'manifest.json',{'scope':'additional64_arm_only_frozen_solver',
        'pool_count':len(inventory),'pool_manifest_sha256':sha(pool_path),
        'previous_sources':sorted(excluded),'sources':selected,'source_count':len(selected),
        'config_sha256':sha(CONFIG),'code_sha256':code,'training_approved':False,
        'selection':'16 per batch: 2 validation high-gap; metric extremes; farthest joint-summary fill',
        'native_sdk_new_parity':False})
    print(json.dumps({'scanned':len(inventory),'selected':len(selected),
                      'chunks':sum(sum(r['chunk_counts_per_arm']) for r in selected)}),flush=True)


def run_episode(row, output):
    path=Path(row['source_path'])
    if sha(path)!=row['source_sha256']: raise ValueError('Changed source')
    state,raw,q,sv,av,time=load_arm_data(path)
    model=ArmModel(CONFIG);solver=ContinuousIK(model)
    case=row['episode'].replace('/','_');folder=Path(output)/'cases'/case
    folder.mkdir(parents=True,exist_ok=False)
    poses=np.full((len(q),2,4,4),np.nan)
    for a in (0,1):
        for i in np.flatnonzero(av[:,a]): poses[i,a]=model.fk(a,q[i,a])
    counts={'episode':row['episode'],'calls':0,'failed':0,'chunks':0,'failed_chunks':0,
            'first20_calls':0,'first20_failed':0}
    with (folder/'responses.jsonl').open('x') as f:
        for arm in (0,1):
            for start in select_starts(sv[:,arm],av[:,arm]):
                counts['chunks']+=1;failed_chunk=False
                reference=state[start,arm].copy();anchor=model.fk(arm,reference)
                inverse=np.linalg.inv(anchor)
                for k in range(50):
                    target=poses[start+k,arm]
                    delta=inverse@target
                    action9=np.r_[delta[:3,3],delta[:2,:3].ravel()].astype(np.float32)
                    restored=np.eye(4);restored[:3,3]=action9[:3]
                    restored[:3,:3]=decode_rotation6d(action9[3:])
                    restored=anchor@restored
                    ik=solver.solve(arm,restored,reference)
                    counts['calls']+=1;counts['failed']+=not ik['success']
                    if k<20:
                        counts['first20_calls']+=1;counts['first20_failed']+=not ik['success']
                    source_step=float(np.max(abs(q[start+k,arm]-q[start+k-1,arm]))) if k else None
                    r={'episode':row['episode'],'arm':arm,'start':start,'step':k,
                       'frame':start+k,'source_step_deg':source_step,
                       'source_target_admissible':model.admissible_rad(np.deg2rad(q[start+k,arm])),
                       'reference':reference.tolist(),'ik':ik}
                    if ik['success']:
                        r['source_pose_error_mm_deg']=pose_error(target,model.fk(arm,ik['joints']))
                        reference=np.array(ik['joints'])
                    else: failed_chunk=True
                    f.write(json.dumps(r,allow_nan=False)+'\n')
                counts['failed_chunks']+=failed_chunk
    write_json(folder/'summary.json',counts)
    return counts


def run(output,workers):
    manifest=json.loads((output/'manifest.json').read_text())
    for rel,expected in manifest['code_sha256'].items():
        if sha(TASK/rel)!=expected: raise ValueError(f'Frozen code changed: {rel}')
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs=[pool.submit(run_episode,r,str(output)) for r in manifest['sources']]
        for future in as_completed(jobs):print(json.dumps(future.result()),flush=True)


def stats(values):
    return dict(zip(['p50','p95','p99','max'],np.percentile(values,[50,95,99,100]).tolist())) if values else {}


def collect(output):
    manifest=json.loads((output/'manifest.json').read_text())
    groups={};summaries=[];failures=[];jumps=[];output_hashes={}
    for src in manifest['sources']:
        folder=output/'cases'/src['episode'].replace('/','_')
        summaries.append(json.loads((folder/'summary.json').read_text()))
        output_hashes[src['episode']]=sha(folder/'responses.jsonl')
        for line in (folder/'responses.jsonl').open():
            r=json.loads(line);ik=r['ik']
            for scope in ['all']+(['first20'] if r['step']<20 else []):
                g=groups.setdefault(f'{scope}_arm{r["arm"]}',{'calls':0,'failed':0,
                    'position_mm':[],'rotation_deg':[],'reference_step_deg':[],
                    'first_step_deg':[],'adjacent_step_deg':[],'latency_ms':[],
                    'joint_limit_violations':0,'source_target_inadmissible':0})
                g['calls']+=1;g['failed']+=not ik['success'];g['latency_ms'].append(ik['latency_ms'])
                g['source_target_inadmissible']+=not r['source_target_admissible']
                if ik['success']:
                    q=np.array(ik['joints'])
                    g['joint_limit_violations']+=int(np.any(q<LOW_DEG-1e-7) or np.any(q>HIGH_DEG+1e-7)
                        or abs(q[6])+1.025*abs(q[5])>110.5+1e-7)
                    g['position_mm'].append(r['source_pose_error_mm_deg'][0])
                    g['rotation_deg'].append(r['source_pose_error_mm_deg'][1])
                    g['reference_step_deg'].append(ik['reference_step_max_deg'])
                    g['first_step_deg' if r['step']==0 else 'adjacent_step_deg'].append(ik['reference_step_max_deg'])
            if not ik['success']: failures.append(r)
            if ik['success'] and r['step'] and ik['reference_step_max_deg']-r['source_step_deg']>1:
                jumps.append(r)
    for g in groups.values():
        for k,v in list(g.items()):
            if isinstance(v,list):g[k]=stats(v)
    with (output/'failures.jsonl').open('x') as f:
        for r in failures:f.write(json.dumps(r)+'\n')
    with (output/'jump_review.jsonl').open('x') as f:
        for r in jumps:f.write(json.dumps(r)+'\n')
    write_json(output/'report.json',{'sources':len(summaries),'previous_sources':8,
        'new_source_overlap_with_previous':0,'summaries':summaries,'groups':groups,
        'failure_records':len(failures),'jump_review_records':len(jumps),
        'jump_review_definition':'accepted reference step minus adjacent source command step > 1 degree; review only',
        'manifest_sha256':sha(output/'manifest.json'),'result_hashes':output_hashes,
        'options':asdict(ContinuousIK(ArmModel(CONFIG)).options),
        'environment':{'python':platform.python_version(),'numpy':np.__version__,'scipy':scipy.__version__},
        'target_backend':'locally verified modified DH; new native-SDK parity not yet measured',
        'robot_motion':False,'training_approved':False})
    print(json.dumps({'sources':len(summaries),'failures':len(failures),'jump_review':len(jumps),'groups':groups},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['prepare','run','collect'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=4)
    a=p.parse_args()
    if a.mode=='run':run(a.output,a.workers)
    else:globals()[a.mode](a.output)
