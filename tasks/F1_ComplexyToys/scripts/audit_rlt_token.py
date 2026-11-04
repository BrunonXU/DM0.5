#!/usr/bin/env python3
"""Post-training token sensitivity audit, with no optimization or robot I/O."""
import argparse
import json
import os
from pathlib import Path
import sys

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))
from f1_toys.rlt import PrefixCache, token_class


@torch.no_grad()
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('core','recipe','cache','checkpoint','output'):
        parser.add_argument('--'+key,required=True,type=Path)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    recipe=json.loads(args.recipe.read_text())
    cache=PrefixCache(args.cache,'validation')
    import wandb
    run=wandb.init(project=recipe['wandb']['project'],entity=recipe['wandb']['entity'],
        group=recipe['wandb']['group'],job_type='offline-evaluation',id=args.run_id,
        name='B1250_RLT_Token_Sensitivity',dir=str(args.output),
        tags=['F1','RLT','frozen_B1250','token_ablation','offline_evaluation'],
        config={'checkpoint_step':300,'validation_observations':len(cache),'optimizer_updates':0})
    try:
        saved=torch.load(args.checkpoint,map_location='cpu',weights_only=True,mmap=True)
        with torch.device('meta'): module=token_class(args.core)(**recipe['token_config'])
        module.load_state_dict(saved['module'],strict=True,assign=True)
        module=module.cuda().eval().requires_grad_(False)
        all_z=[]
        for i in range(0,len(cache),2):
            x,mask=cache.batch(list(range(i,min(i+2,len(cache)))),'cuda')
            with torch.autocast('cuda',dtype=torch.bfloat16): all_z.append(module.encode(x,mask).float().cpu())
        all_z=torch.cat(all_z)
        mean_z=all_z.mean(0,keepdim=True).cuda()
        variants={k:{group:[] for group in ('all','first_valid','right_wrist_first','visual','prompt_state')}
                  for k in ('matched','zero','swapped','mean')}
        for i in range(0,len(cache),2):
            indices=list(range(i,min(i+2,len(cache))))
            x,mask=cache.batch(indices,'cuda')
            z=all_z[indices].cuda()
            other=all_z[[(j+len(cache)//2)%len(cache) for j in indices]].cuda()
            masks={'all':mask,'visual':mask.clone(),'prompt_state':mask.clone(),
                   'first_valid':torch.zeros_like(mask),'right_wrist_first':torch.zeros_like(mask)}
            masks['visual'][:,768:]=False
            masks['prompt_state'][:,:768]=False
            masks['first_valid'].scatter_(1,mask.int().argmax(1,keepdim=True),True)
            assert mask[:,512].all()  # F1 image order: head, masked left, right.
            masks['right_wrist_first'][:,512]=True
            for name,value in (('matched',z),('zero',torch.zeros_like(z)),('swapped',other),('mean',mean_z.expand_as(z))):
                with torch.autocast('cuda',dtype=torch.bfloat16): reconstructed=module.decode(value,x,mask)
                error=(reconstructed.float()-x.float()).square().mean(-1)
                for group,selected in masks.items():
                    metric=(error*selected).sum(1)/selected.sum(1).clamp_min(1)
                    variants[name][group].extend(metric.cpu().tolist())
        metrics={k:{g:float(np.mean(v)) for g,v in groups.items()} for k,groups in variants.items()}
        centered=all_z-all_z.mean(0,keepdim=True)
        other=all_z.roll(len(cache)//2,0)
        cosine=torch.nn.functional.cosine_similarity(all_z.flatten(1),other.flatten(1),dim=1)
        report={'metrics':metrics,'observations':len(cache),'independent_episodes':4,
                'token_std_mean':float(all_z.std(0).mean()),
                'centered_norm_mean':float(centered.flatten(1).norm(dim=1).mean()),
                'swapped_cosine_mean':float(cosine.mean()),
                'optimizer_updates':0,'robot_commands':0,
                'interpretation':'Teacher-forced reconstruction sensitivity only, not RL utility or success-rate evidence'}
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        run.summary.update(report)
        print(json.dumps(report),flush=True)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__=='__main__': main()
