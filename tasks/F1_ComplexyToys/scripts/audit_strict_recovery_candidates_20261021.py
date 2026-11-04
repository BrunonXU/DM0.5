"""Rebuild strict candidate prefixes; expanded diagnostics are never reused."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from f1_toys.eef import relative_pose
from f1_toys.native.kinematics import ArmModel, ContinuousIK, pose_error
from f1_toys.native.fast_kinematics import FastArmModel


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    task=Path(__file__).resolve().parents[1]
    joined=task/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    samples=task/'Eval/artifacts/20261021-ik-recovery-next/resample-guided16/candidates.jsonl'
    config=task/'src/f1_toys/native/ccs_m6_40.MvKDCfg'
    source={(r['episode'],r['server']['request_id']):r['server'] for r in map(json.loads,joined.read_text().splitlines()) if not r['server']['success']}
    model=FastArmModel(config);ik=ContinuousIK(model);fk=ArmModel(config)
    cases={key:[] for key in source}
    records=[]
    def decode(key,absolute,trial):
        src=source[key];ctx=src['rtc_request'];prefix=ctx['committed_actions16'];held=src['prediction_replay']['last_submitted16']
        ref=np.array(prefix[-1][7:14] if prefix else src['prediction_replay']['state16'][7:14])
        initial=fk.fk(1,ref);actions=[list(r) for r in prefix];diagnostics=[];errors=[];steps=[]
        for i in range(len(prefix),ctx['coverage_steps']):
            result=ik.solve(1,relative_pose(np.array(absolute[i])),ref)
            diagnostics.append(result)
            if not result['success']: break
            q=np.array(result['joints']); assert model.admissible_rad(np.deg2rad(q))
            err=pose_error(relative_pose(np.array(absolute[i])),fk.fk(1,q))
            assert err[0]<=.01 and err[1]<=.001
            errors.append(err);steps.append(float(abs(q-ref).max()));ref=q
            row=list(held);row[7:14]=q.tolist();row[15]=float(np.clip(absolute[i][9]*50+50,0,100));actions.append(row)
        n=len(actions)-len(prefix)
        result=dict(episode=key[0],request_id=key[1],trial=trial,complete=len(actions)==ctx['coverage_steps'],
                    valid_new_points=n,actions=actions,diagnostics=diagnostics,
                    absolute_eef10=absolute,committed_points=len(prefix),
                    endpoint_translation_mm=float(np.linalg.norm(fk.fk(1,ref)[:3,3]-initial[:3,3])),
                    max_joint_step_deg=max(steps,default=0.),max_pose_errors_mm_deg=np.max(errors,axis=0).tolist() if errors else [0,0])
        records.append(result);cases[key].append(result)
    for key,src in source.items(): decode(key,src['absolute_eef10'],-1)
    for line in samples.open():
        row=json.loads(line)
        decode((row['episode'],row['request_id']),row['absolute_eef10'],row['trial'])
    def select(options):
        complete=next((r for r in options if r['complete']),None)
        return complete or max(options,key=lambda r:r['valid_new_points'])
    summaries={}; selections={}
    for count in (1,2,3,4,8,16):
        for original_first in (False,True):
            name=('original_plus_' if original_first else 'fresh_')+str(count)
            chosen=[select(([rs[0]] if original_first else [])+[r for r in rs if 0<=r['trial']<count]) for rs in cases.values()]
            summaries[name]=dict(complete=sum(r['complete'] for r in chosen),
                partial_nonzero=sum(not r['complete'] and r['valid_new_points']>0 for r in chosen),
                zero=sum(r['valid_new_points']==0 for r in chosen),
                cases_with_any_valid_next_segment=sum(r['valid_new_points']>0 for r in chosen))
            selections[name]=[dict(episode=r['episode'],request_id=r['request_id'],trial=r['trial'],complete=r['complete'],valid_new_points=r['valid_new_points'],endpoint_translation_mm=r['endpoint_translation_mm']) for r in chosen]
    a.output.mkdir(parents=True,exist_ok=False)
    (a.output/'results.json').write_text(json.dumps(dict(records=records,selections=selections)))
    summary=dict(cases=len(source),strict_redecoded_candidates=len(records),strategies=summaries,
        reference_fk_checked_points=sum(len(r['actions'])-r['committed_points'] for r in records),
        robot_commands=0,model_calls=0,limits_changed=False,
        limitations=['Original strict bounds and exact pose gates. All generated candidates preserve their own EEF/gripper values.',
                     'Partial nonzero is not a full-window or task success; controller stopping, contact and subsequent prediction remain to validate.',
                     'Candidate count excludes timing/selection budget; fresh_N is a new replay, original_plus_N retains the recorded failed proposal first.',
                     'Repeated seed candidates are correlated; no probability or closed-loop claim.'])
    (a.output/'summary.json').write_text(json.dumps(summary,indent=2))
    (a.output/'manifest.json').write_text(json.dumps(dict(sources_sha256={str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in (Path(__file__),joined,samples,config)}),indent=2))
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
