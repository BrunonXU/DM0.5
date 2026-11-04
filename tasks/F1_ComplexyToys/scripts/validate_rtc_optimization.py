"""Same-seed eager/compiled RTC on paired historical real observations."""
import argparse, hashlib, json, time
from pathlib import Path
import numpy as np
from PIL import Image
import torch
from f1_toys.inference import Policy
from f1_toys.rtc_optimization import compile_suffix
from f1_toys.eef import physical_actions, relative_pose
p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args()
root=Path('/workspace/tasks/F1_ComplexyToys');inputs=root/'artifacts/20261019-rtc/inputs';out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
policy=Policy(root/'artifacts/20261016-v1-formal-training/bundle-5000',root/'artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf')
eager=policy.model.run_suffix;compile_suffix(policy);compiled=policy.model.run_suffix
observations=json.loads((inputs/'observations.json').read_text());pairs=[p for p in json.loads((inputs/'pairs.json').read_text()) if p['shift'] in (20,25)]
def load(oid):
 row=observations[str(oid)];images={}
 for camera,name in row['images'].items():
  f=inputs/name;assert hashlib.sha256(f.read_bytes()).hexdigest()==row['image_sha256'][camera];images[camera]=Image.open(f).convert('RGB')
 return images,row['state16']
def compare(images,state,held,ctx,seed):
 values=[]
 for fn in (eager,compiled):
  policy.model.run_suffix=fn
  values.append(policy.predict_rtc(images,state,held,ctx,seed=seed))
 (base,raw0,_),(cand,raw1,_)=values
 phys0=physical_actions(raw0,policy.stats['action']);phys1=physical_actions(raw1,policy.stats['action'])
 angles=[]
 for x,y in zip(phys0,phys1):
  rx=relative_pose(x)[:3,:3];ry=relative_pose(y)[:3,:3]
  angles.append(np.degrees(np.arccos(np.clip((np.trace(rx.T@ry)-1)/2,-1,1))))
 r=dict(bootstrap=ctx['bootstrap'],s=ctx['shift_steps'],seed=seed,base_success=base['success'],candidate_success=cand['success'],
  max_raw_error=float(np.abs(raw0-raw1).max()),max_position_mm=float(np.linalg.norm(phys0[:,:3]-phys1[:,:3],axis=1).max()*1000),max_rotation_deg=float(max(angles)),base_ms=base['timing_ms'],candidate_ms=cand['timing_ms'])
 if base['success'] and cand['success']:
  ac0=np.asarray(base['actions']);ac1=np.asarray(cand['actions']);d=0 if ctx['bootstrap'] else ctx['delay_steps']
  if d:
   assert np.array_equal(ac0[:d],np.asarray(ctx['committed_actions16'])) and np.array_equal(ac1[:d],np.asarray(ctx['committed_actions16']))
  r.update(max_joint_difference_deg=float(abs(ac0[:,7:14]-ac1[:,7:14]).max()),max_gripper_difference=float(abs(ac0[:,15]-ac1[:,15]).max()))
 return base,r
records=[]
for index,pair in enumerate(pairs):
 s=pair['shift'];im0,st0=load(pair['a']);im1,st1=load(pair['b'])
 for seed in (20261019,20261020):
  ctx=dict(bootstrap=True,delay_steps=20,shift_steps=s,coverage_steps=min(50,s+22),plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
  old,r=compare(im0,st0,pair['held'],ctx,seed);records.append(dict(pair=index,**r))
  if not old['success']:continue
  ctx.update(bootstrap=False,base_plan_id=1,previous_absolute_eef10=old['absolute_eef10'][s:],committed_actions16=old['actions'][s:s+20])
  _,r=compare(im1,st1,pair['held'],ctx,seed);records.append(dict(pair=index,**r))
 (out/'results.json').write_text(json.dumps(records,indent=2))
 print(json.dumps({'pair':index+1,'total':len(pairs)}),flush=True)
summary=dict(comparisons=len(records),guided=sum(not r['bootstrap'] for r in records),base_ik_pass=sum(r['base_success'] for r in records),candidate_ik_pass=sum(r['candidate_success'] for r in records),robot_commands=0)
for key in ('max_raw_error','max_position_mm','max_rotation_deg','max_joint_difference_deg','max_gripper_difference'):
 vals=[r[key] for r in records if key in r];summary[key]=dict(zip(('p50','p95','max'),np.percentile(vals,[50,95,100]).tolist()))
for key in ('base_ms','candidate_ms'):
 vals=[r[key]['model_h2d_d2h'] for r in records[4:] if not r['bootstrap']];summary[key]=dict(zip(('p50','p95','max'),np.percentile(vals,[50,95,100]).tolist()))
(out/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
