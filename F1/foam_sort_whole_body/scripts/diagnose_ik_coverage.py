#!/usr/bin/env python3
"""Diagnose frozen-run failures; candidate branch probes do not alter the solver."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

from audit_ik_coverage import TASK, CONFIG, load_arm_data, sha, write_json, stats
sys.path.insert(0,str(TASK/'robot'))
sys.path.insert(0,str(TASK/'models/DM0.5'))
from kinematics import ArmModel, ContinuousIK, pose_error
from f1_data.reader import decode_rotation6d


def targets_for_block(model,state,q,arm,start):
    anchor=model.fk(arm,state[start,arm]);inverse=np.linalg.inv(anchor);targets=[]
    for k in range(50):
        delta=inverse@model.fk(arm,q[start+k,arm])
        a=np.r_[delta[:3,3],delta[:2,:3].ravel()].astype(np.float32)
        pose=np.eye(4);pose[:3,3]=a[:3];pose[:3,:3]=decode_rotation6d(a[3:])
        targets.append(anchor@pose)
    return targets


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True)
    args=p.parse_args();run=args.run
    manifest=json.loads((run/'manifest.json').read_text());model=ArmModel(CONFIG);solver=ContinuousIK(model)
    failures=[json.loads(l) for l in (run/'failures.jsonl').open()]
    block_keys=sorted({(r['episode'],r['arm'],r['start']) for r in failures})
    diagnosis=[];witness_errors=[];probes=[]
    for episode,arm,start in block_keys:
        src=next(r for r in manifest['sources'] if r['episode']==episode)
        state,raw,q,sv,av,time=load_arm_data(src['source_path'])
        targets=targets_for_block(model,state,q,arm,start)
        observed=[json.loads(l) for l in (run/'cases'/episode.replace('/','_')/'responses.jsonl').open()]
        observed=[r for r in observed if r['arm']==arm and r['start']==start]
        bad=[r for r in observed if not r['ik']['success']]
        for r in bad:
            # Known feasible source q is a diagnosis witness ONLY, never an input
            # to either the frozen evaluation or the candidate branch probes.
            witness_errors.append(pose_error(targets[r['step']],model.fk(arm,q[r['frame'],arm])))
        diagnosis.append({'episode':episode,'arm':arm,'start':start,
            'failed_steps':[r['step'] for r in bad],
            'initial_feedback':state[start,arm].tolist(),
            'source_j4_deg':q[start:start+50,arm,3].tolist(),
            'returned_j4_deg':[r['ik']['candidate_joints'][3] for r in observed],
            'first_failure':bad[0],
            'min_witness_rect_margin_deg':float(np.min(np.minimum(q[start:start+50,arm]-np.rad2deg(model.low),
                                                                        np.rad2deg(model.high)-q[start:start+50,arm]))),
            'interpretation':'Feasible targets; local branch followed toward J4/J6 limits. Greedy pose acceptance does not certify future continuation.'})
        for offset in [-40.,-20.,0.,20.,40.]:
            initial=state[start,arm].copy();seed=initial.copy();seed[3]+=offset
            seed=np.clip(seed,np.rad2deg(model.low),np.rad2deg(model.high))
            reference=seed;steps=[];joints=[];first20_failed=0;total_failed=0
            for k,target in enumerate(targets):
                result=solver.solve(arm,target,reference)
                if result['success']:
                    qq=np.array(result['joints'])
                    steps.append(float(np.max(abs(qq-(initial if k==0 else reference)))))
                    reference=qq;joints.append(qq.tolist())
                else:
                    total_failed+=1;first20_failed+=k<20;joints.append(None)
            probes.append({'episode':episode,'arm':arm,'start':start,'initial_j4_offset_deg':offset,
                'failed':total_failed,'first20_failed':first20_failed,
                'accepted_reference_step_deg':stats(steps),'joints':joints,
                'scope':'diagnostic fixed seed offset; not a selected runtime algorithm or deployment result'})
    # Distinguish actual consecutive accepted outputs from a retained reference
    # after failure. Also report big initial feedback-to-target gaps separately.
    continuity={};sampled=[]
    for src in manifest['sources']:
        prev=None
        for index,line in enumerate((run/'cases'/src['episode'].replace('/','_')/'responses.jsonl').open()):
            r=json.loads(line);ik=r['ik'];arm=r['arm'];g=continuity.setdefault(str(arm),{'actual_adjacent_deg':[],'first_step_deg':[],
                'adjacent_excess_over_source_deg':[],'after_rejection_accepted':0})
            if ik['success']:
                if r['step']==0:g['first_step_deg'].append(ik['reference_step_max_deg'])
                elif prev and prev['arm']==arm and prev['start']==r['start']:
                    if prev['ik']['success']:
                        jump=float(max(abs(np.array(ik['joints'])-prev['ik']['joints'])))
                        g['actual_adjacent_deg'].append(jump)
                        g['adjacent_excess_over_source_deg'].append(jump-r['source_step_deg'])
                    else:g['after_rejection_accepted']+=1
            if index%500==0:
                sampled.append({'episode':r['episode'],'arm':arm,'frame':r['frame'],
                                'joints':ik['joints'],'source_path':src['source_path']})
            prev=r
    for g in continuity.values():
        for key,value in list(g.items()):
            if isinstance(value,list):g[key]=stats(value)
    # Exact repeat of representative accepted cases, with original references.
    repeat=[]
    for src in manifest['sources']:
        state,raw,q,sv,av,time=load_arm_data(src['source_path'])
        rows=[json.loads(l) for l in (run/'cases'/src['episode'].replace('/','_')/'responses.jsonl').open()]
        for r in [rows[0],rows[len(rows)//2]]:
            target=targets_for_block(model,state,q,r['arm'],r['start'])[r['step']]
            result=solver.solve(r['arm'],target,r['reference'])
            repeat.append(float(max(abs(np.array(result['joints'])-r['ik']['joints']))))
    write_json(run/'diagnosis.json',{'manifest_sha256':sha(run/'manifest.json'),
        'diagnostic_code_sha256':sha(__file__),'solver_sha256':sha(TASK/'robot/kinematics.py'),
        'failed_blocks':diagnosis,'failure_count':len(failures),
        'witness_count':len(witness_errors),'witness_max_error_mm_deg':np.max(witness_errors,axis=0).tolist(),
        'seed_probes':probes,'continuity':continuity,
        'repeat_count':len(repeat),'repeat_max_difference_deg':max(repeat),
        'runtime_solver_changed':False,'new_native_sdk_parity':False})
    print(json.dumps({'failures':len(failures),'witness_max_error':np.max(witness_errors,axis=0).tolist(),
                     'probes':[{k:v for k,v in r.items() if k not in ['joints','scope']} for r in probes]},indent=2))


if __name__=='__main__':main()
