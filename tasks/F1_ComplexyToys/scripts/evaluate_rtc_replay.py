"""Paired 5K guided/unguided action tests on historical sync rollout observations."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from f1_toys.inference import Policy


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--delays',type=int,nargs='+',default=[12,15,18])
    a=p.parse_args();a.output.mkdir(exist_ok=False,parents=True)
    root=Path('/workspace/tasks/F1_ComplexyToys')
    policy=Policy(root/'artifacts/20261016-v1-formal-training/bundle-5000',
        root/'artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf')
    observations=json.loads((a.inputs/'observations.json').read_text())
    pairs=json.loads((a.inputs/'pairs.json').read_text())
    def load(oid):
        row=observations[str(oid)];images={}
        for camera,name in row['images'].items():
            path=a.inputs/name
            assert hashlib.sha256(path.read_bytes()).hexdigest()==row['image_sha256'][camera]
            images[camera]=Image.open(path).convert('RGB')
        return images,row['state16']
    records=[];boot={}
    stream=(a.output/'results.jsonl').open('x')
    for pair in pairs:
        s=pair['shift'];image_a,state_a=load(pair['a']);image_b,state_b=load(pair['b'])
        for seed in (20261019,20261020):
            for d in a.delays:
                if d>s:continue
                k=min(50,s+d+2)
                key=(pair['a'],seed,k)
                ctx=dict(bootstrap=True,delay_steps=d,shift_steps=s,coverage_steps=k,
                    plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
                if key not in boot:
                    boot[key]=policy.predict_rtc(image_a,state_a,pair['held'],ctx,seed=seed)[0]
                old=boot[key]
                if not old['success']:
                    record=dict(request_id=pair['request_id'],s=s,d=d,seed=seed,phase='bootstrap',
                                success=False,diagnostics=old['diagnostics'])
                    records.append(record);stream.write(json.dumps(record)+'\n');continue
                ctx.update(bootstrap=False,base_plan_id=1,
                    committed_actions16=old['actions'][s:s+d],previous_absolute_eef10=old['absolute_eef10'][s:])
                for guided in (False,True):
                    result,raw,_=policy.predict_rtc(image_b,state_b,pair['held'],ctx,seed=seed,guided=guided)
                    record=dict(request_id=pair['request_id'],s=s,d=d,seed=seed,phase='guided' if guided else 'unguided',
                        success=result['success'],timing_ms=result['timing_ms'],
                        observation_age_at_replan_ms=pair['observation_age_at_replan_ms'],
                        diagnostics=result['diagnostics'])
                    if result['success']:
                        actions=np.asarray(result['actions']);committed=np.asarray(ctx['committed_actions16'])
                        assert np.array_equal(actions[:d],committed)
                        record.update(join_max_joint_step_deg=float(abs(actions[d,7:14]-actions[d-1,7:14]).max()),
                            join_gripper_step=float(abs(actions[d,15]-actions[d-1,15])),actions=actions.tolist())
                    records.append(record);stream.write(json.dumps(record)+'\n');stream.flush()
        print(json.dumps(dict(completed_pair=pairs.index(pair)+1,total=len(pairs))),flush=True)
    groups={}
    for s in (15,20,25):
        for d in a.delays:
            for phase in ('bootstrap','unguided','guided'):
                rows=[r for r in records if (r['s'],r['d'],r['phase'])==(s,d,phase)]
                if not rows:continue
                item=dict(n=len(rows),success=sum(r['success'] for r in rows))
                for key in ('join_max_joint_step_deg','join_gripper_step'):
                    vals=[r[key] for r in rows if key in r]
                    if vals:item[key]=dict(zip(('p50','p95','max'),np.percentile(vals,[50,95,100]).tolist()))
                vals=[r['timing_ms']['policy'] for r in rows if 'timing_ms' in r]
                if vals:item['policy_ms']=dict(zip(('p50','p95','max'),np.percentile(vals,[50,95,100]).tolist()))
                groups[f's{s}-d{d}-{phase}']=item
    (a.output/'summary.json').write_text(json.dumps(dict(model=policy.bundle_id,groups=groups,
        pairs=len(pairs),seeds=[20261019,20261020],robot_commands=0,
        limitation='Same recorded sync observations, not RTC closed-loop success; bootstrap IK failures counted separately.'),indent=2))


if __name__=='__main__':main()
