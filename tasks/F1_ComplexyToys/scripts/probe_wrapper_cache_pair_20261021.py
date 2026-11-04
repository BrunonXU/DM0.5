"""Same-process actual wrapper/uncached rejection comparison; no server/robot."""
import argparse,hashlib,importlib.util,json,os,time
from pathlib import Path
import numpy as np
from PIL import Image

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser()
 for n in ['inputs','output','bundle','core','compat','wrapper']:p.add_argument('--'+n,type=Path,required=True)
 a=p.parse_args();assert os.environ['CUDA_VISIBLE_DEVICES']=='2';a.output.mkdir(parents=True,exist_ok=False)
 assert sha(a.wrapper)=='41c07b196a2e9c214992396d12eb72e179acc356ab39dd809e9b5df583b3034f'
 exec(compile(a.compat.read_text().split('from f1_toys.server import main')[0],str(a.compat),'exec'),{'__file__':str(a.compat)})
 from f1_toys.inference import Policy
 from f1_toys.rtc_optimization import compile_suffix
 import torch
 policy=Policy(a.bundle,a.core);compile_suffix(policy)
 spec=importlib.util.spec_from_file_location('wrapper_pair',a.wrapper);w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
 src=json.loads((a.inputs/'requests.json').read_text());index={(r['episode'],r['request_id']):r for r in map(json.loads,(a.inputs/'image-index.jsonl').read_text().splitlines())}
 selected=[r for r in src if (r['episode'].split('_')[-1],r['request']['request_id']) in [('3dd2babb',58),('64184400',77)]];assert len(selected)==2
 contexts=[]
 for r in selected:
  raw=r['request'];entry=index[r['episode'],raw['request_id']];images={}
  for k,n in entry['images'].items():
   assert sha(a.inputs/n)==entry['image_sha256'][k];images[k]=Image.open(a.inputs/n).convert('RGB')
  contexts.append((r,(images,raw['prediction_replay']['state16'],raw['prediction_replay']['last_submitted16'],raw['rtc_request'])))
 warmups=[]
 for r,ctx in contexts:
  began=time.perf_counter();policy.predict_rtc(*ctx,seed=2026102100,guided=True);warmups.append(dict(request_id=r['request']['request_id'],ms=(time.perf_counter()-began)*1000))
 print(json.dumps(dict(warmups=warmups)),flush=True)
 records=[]
 for r,ctx in contexts:
  baseline={seed:policy.predict_rtc(*ctx,seed=seed,guided=True) for seed in range(2026102100,2026102104)}
  captured=[];audit=[]
  def tracked(*args,**kwargs):
   value=policy.predict_rtc(*args,**kwargs);captured.append((kwargs['seed'],value));return value
  returned=w.recover_predict(policy,tracked,ctx,dict(seed=2026102100,guided=True),max_attempts=4,budget_ms=400.,seed_factory=lambda:2026102101,audit=audit.append)
  checks=[]
  for seed,value in captured:
   after=policy.predict_rtc(*ctx,seed=seed,guided=True)
   before=baseline[seed]
   compare=lambda x,y:dict(raw_bitwise_equal=np.array_equal(x[1],y[1]),max_raw_abs_delta=float(np.max(abs(x[1]-y[1]))),
      absolute_eef_equal=x[0]['absolute_eef10']==y[0]['absolute_eef10'],strict_success_equal=x[0]['success']==y[0]['success'],
      valid_suffix_count_equal=w.valid_suffix_steps(x[0])==w.valid_suffix_steps(y[0]),
      solved_joints_equal=[d['joints'] for d in x[0]['diagnostics']]==[d['joints'] for d in y[0]['diagnostics']])
   checks.append(dict(seed=seed,wrapper_vs_uncached_before=compare(value,before),wrapper_vs_uncached_after=compare(value,after)))
  all_equal=all(all(v for k,v in c[label].items() if k!='max_raw_abs_delta') for c in checks for label in ['wrapper_vs_uncached_before','wrapper_vs_uncached_after'])
  record=dict(episode=r['episode'],request_id=r['request']['request_id'],bootstrap=ctx[3]['bootstrap'],audit=audit[0],checks=checks,all_equal=all_equal)
  records.append(record);print(json.dumps(record),flush=True)
 result=dict(cases=records,warmups=warmups,all_pairs_equal=all(r['all_equal'] for r in records),parameter_grads_absent=all(p.grad is None for p in policy.model.parameters()),
    physical_gpu=2,model_bundle=policy.bundle_id,robot_commands=0,ports_opened=0,production_changes=0,
    limitation='Same-process actual rejected candidates only. Cross-process differences from earlier guided16 remain documented and are not required to match.')
 (a.output/'results.json').write_text(json.dumps(result,indent=2)+'\n')
 (a.output/'manifest.json').write_text(json.dumps(dict(sources={str(p):sha(p) for p in [Path(__file__),a.wrapper,a.compat,a.inputs/'requests.json',a.inputs/'image-index.jsonl',a.bundle/'bundle.json']}),indent=2)+'\n')
 assert result['all_pairs_equal'] and result['parameter_grads_absent'],result
if __name__=='__main__':main()
