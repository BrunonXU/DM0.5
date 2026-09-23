#!/usr/bin/env python3
"""Independent result checks, repeats, and float32 action23 prefix round trips."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'robot'))
from kinematics import ArmModel, ContinuousIK, pose_error


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def stats(values):
    return dict(zip(['p50','p95','p99','max'], np.percentile(values,[50,95,99,100]).tolist()))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    args=p.parse_args()
    dst=args.run/'independent_verification.json'
    if dst.exists(): raise FileExistsError(dst)
    baseline=TASK/'results/kinematics_validation_20260916'
    model=ArmModel(TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg')
    solver=ContinuousIK(model)
    reqs={}
    for line in (baseline/'requests.jsonl').open():
        r=json.loads(line)
        if ':chunk:' in r['id']: reqs[(r['id'],r['arm'])]=r
    native={}
    with (baseline/'responses.jsonl').open() as f:
        next(f)
        for line in f:
            r=json.loads(line)
            if ':chunk:' in r['id']: native[(r['id'],r['arm'])]=r
    rows=[json.loads(line) for line in (args.run/'responses.jsonl').open()]
    bykey={(r['id'],r['arm']):r for r in rows}
    assert len(rows)==len(reqs)==len(bykey)==19900
    errors=[];violations=0;wrist_limit_errors=[];source_steps=[];repeat=[]
    for i,row in enumerate(rows):
        key=row['id'],row['arm'];r=row['ik'];q=np.array(r['joints'])
        assert r['success'] and q.shape==(7,)
        # Literal independent inequality checks, not solver.admissible_rad().
        violations+=int(np.any(q<np.array([-178,-94,-178,-145,-178,-60,-90])-1e-7)
                        or np.any(q>np.array([178,120,178,60,178,60,90])+1e-7)
                        or abs(q[6])+1.025*abs(q[5])>110.5+1e-7)
        errors.append(pose_error(np.array(native[key]['pose']),model.fk(row['arm'],q)))
        sdk=native[key]['ik'];sq=sdk['joints']
        wrist_limit_errors.append(abs(sdk['limit_pos'][6]-min(90,110.5-1.025*abs(sq[5]))))
        step=int(row['id'].split(':')[-1])
        if step:
            prev_key=(row['id'].rsplit(':',1)[0]+':'+str(step-1),row['arm'])
            source_change=float(np.max(np.abs(np.array(reqs[key]['joints'])-reqs[prev_key]['joints'])))
            source_steps.append({'id':row['id'],'arm':row['arm'],
                                 'source_max_deg':source_change,'ik_max_deg':r['reference_step_max_deg']})
        if i%199==0 or (row['id']=='7:chunk:301:45' and row['arm']==0):
            again=solver.solve(row['arm'],native[key]['pose'],row['reference'])
            repeat.append(float(np.max(np.abs(np.array(again['joints'])-q))))
    # Exercise every paired H50 block through the actual public prefix API.
    starts=[r for r in rows if r['arm']==0 and r['id'].endswith(':0')]
    prefix_errors=[];prefix_latency=[];prefix_rows=[]
    for start in starts:
        stem=start['id'].rsplit(':',1)[0]
        reference=np.concatenate([reqs[(start['id'],a)]['reference'] for a in (0,1)])
        anchors=[model.fk(a,reference[a*7:a*7+7]) for a in (0,1)]
        actions=np.zeros((50,23),dtype=np.float32)
        for k in range(50):
            for a in (0,1):
                target=np.array(native[(stem+':'+str(k),a)]['pose'])
                delta=np.linalg.inv(anchors[a])@target
                actions[k,a*9:a*9+9]=np.r_[delta[:3,3],delta[:2,:3].ravel()]
        t=time.perf_counter()
        result=solver.solve_eef_prefix(actions,reference,reference,execute_steps=20)
        prefix_latency.append((time.perf_counter()-t)*1000)
        prefix_rows.append({'id':stem,'success':result['success'],
                            'joint_targets_deg':result['joint_targets_deg'],
                            'latency_ms':prefix_latency[-1]})
        assert result['success'], (stem,result)
        for k,q in enumerate(result['joint_targets_deg']):
            for a in (0,1):
                prefix_errors.append(pose_error(np.array(native[(stem+':'+str(k),a)]['pose']),
                                               model.fk(a,q[a*7:a*7+7])))
    (args.run/'prefix_roundtrips.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in prefix_rows))
    errors=np.array(errors);pe=np.array(prefix_errors)
    report={'run_report_sha256':sha(args.run/'report.json'),'verifier_sha256':sha(__file__),
            'solver_sha256':sha(TASK/'robot/kinematics.py'),
            'count':len(rows),'independent_limit_violations':violations,
            'native_target_closure_max_mm_deg':errors.max(axis=0).tolist(),
            'wrist_positive_limit_formula_max_error_deg':max(wrist_limit_errors),
            'repeat_count':len(repeat),'repeat_max_difference_deg':max(repeat),
            'fallback_count':sum(r['ik']['fallback_used'] for r in rows),
            'continuity':{},'prefix_count':len(starts),'prefix_single_arm_targets':len(prefix_errors),
            'float32_prefix_closure_max_mm_deg':pe.max(axis=0).tolist(),
            'float32_prefix_latency_ms':stats(prefix_latency),
            'prefix_output_sha256':sha(args.run/'prefix_roundtrips.jsonl')}
    for a in (0,1):
        arm=[s for s in source_steps if s['arm']==a]
        initial=[r for r in rows if r['arm']==a and r['id'].endswith(':0')]
        report['continuity'][str(a)]={
            'first_step_deg':stats([r['ik']['reference_step_max_deg'] for r in initial]),
            'adjacent_step_deg':stats([r['ik']['reference_step_max_deg'] for r in rows if r['arm']==a and not r['id'].endswith(':0')]),
            'largest_ik_adjacent':max(arm,key=lambda r:r['ik_max_deg']),
            'largest_ik_excess_over_source':max(arm,key=lambda r:r['ik_max_deg']-r['source_max_deg'])}
    report['previous_native_jump_case']=bykey[('7:chunk:301:45',0)]
    dst.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
