"""Offline same-input/seed RTC optimization candidates; no robot connection."""
import argparse, functools, json, time, os
from pathlib import Path
import numpy as np
from PIL import Image
import torch
from f1_toys.inference import Policy
p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args()
root=Path('/workspace/tasks/F1_ComplexyToys');out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
inputs=root/'artifacts/20261019-prerejection-ab'
row=json.loads((inputs/'inputs.jsonl').read_text().splitlines()[0])
policy=Policy(root/'artifacts/20261016-v1-formal-training/bundle-5000',root/'artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf')
state=row['request']['prediction_replay']['state16'];held=row['request']['prediction_replay']['last_submitted16']
images={key:Image.open(inputs/name).convert('RGB') for key,name in row['images'].items()}
ctx=dict(bootstrap=True,delay_steps=20,shift_steps=20,coverage_steps=42,plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
first,raw,_=policy.predict_rtc(images,state,held,ctx,seed=42)
ctx.update(bootstrap=False,base_plan_id=1,previous_absolute_eef10=first['absolute_eef10'][20:],committed_actions16=first['actions'][20:40] if first['success'] else [held]*20)
base=None;report={}
def run(name,n=10):
 global base
 rows=[];started=time.perf_counter()
 try:
  for i in range(n+3):
   result,raw,_=policy.predict_rtc(images,state,held,ctx,seed=42)
   if i>=3:rows.append(result['timing_ms'])
   if i==3:
    if base is None:base=raw.copy();np.save(out/'baseline.npy',raw)
    diff=np.abs(raw-base)
    report[name]=dict(raw_max_error=float(diff.max()),raw_mean_error=float(diff.mean()),ik_success=result['success'])
  report[name].update(model_ms={k:float(np.percentile([r['model_h2d_d2h'] for r in rows],v)) for k,v in [('p50',50),('p95',95),('max',100)]},elapsed_s=time.perf_counter()-started)
 except Exception as e:
  report[name]=dict(error=repr(e))
 (out/'candidates.json').write_text(json.dumps(report,indent=2))
 print(name,json.dumps(report[name]),flush=True)
run('eager')
# Disable unused attention-weight outputs in SigLIP only; frozen files untouched.
originals=[]
for module in policy.model.img.modules():
 if isinstance(module,torch.nn.MultiheadAttention):
  originals.append((module,module.forward));module.forward=functools.partial(module.forward,need_weights=False)
run('vision_sdpa')
for module,forward in originals:module.forward=forward
os.environ['TORCH_COMPILE_DISABLE']='0'
torch._dynamo.config.disable=False
prefix=policy.model.build_prefix_cache
policy.model.build_prefix_cache=torch.compile(prefix,mode='default',fullgraph=True,dynamic=False)
run('compiled_prefix')
policy.model.build_prefix_cache=prefix
suffix=policy.model.run_suffix
policy.model.run_suffix=torch.compile(suffix,mode='default',fullgraph=True,dynamic=False)
run('compiled_suffix')
