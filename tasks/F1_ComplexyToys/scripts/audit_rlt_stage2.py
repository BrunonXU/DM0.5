#!/usr/bin/env python3
"""Zero-update offline evaluation/export of the five-record RLT pilot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
import torch


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-root',type=Path,required=True)
    p.add_argument('--core',type=Path,required=True)
    p.add_argument('--run-root',type=Path,required=True)
    args=p.parse_args()
    sys.path.insert(0,str(args.task_root/'src'))
    from f1_toys.rlt_stage2 import make_policy
    from f1_toys.eef import physical_actions,decode_prefix,solver
    root=args.run_root;out=root/'audit'
    out.mkdir(exist_ok=False)
    cfg=json.loads((root/'recipe.json').read_text())
    records=[json.loads(s) for s in (root/'cache/transitions.jsonl').read_text().splitlines()]
    data=torch.load(root/'cache/replay.pt',weights_only=True,map_location='cpu')
    torch.set_num_threads(4)
    import wandb
    run=wandb.init(project=cfg['wandb']['project'],entity=cfg['wandb']['entity'],
        group=cfg['wandb']['group'],job_type='offline-evaluation',id='f1rlt2offline20261026r1audit',
        name='B1250_RLT_FiveRollouts_Offline_Evaluation',resume='never',dir=str(out),
        mode=os.environ.get('WANDB_MODE','online'),tags=['F1','RLT_Stage2','offline','zero_update'],
        config={'recipe_sha256':sha(root/'recipe.json'),'replay_manifest_sha256':sha(root/'cache/manifest.json'),
                'audit_source_sha256':sha(__file__)})
    model=make_policy(args.core,cfg['model']).eval()
    obs={k:data[k][data['current']] for k in ('z_rl','proprio','ref_chunk')}
    reports=[];predictions={}
    for step in (300,800):
        ckpt=root/f'train/step-{step:06d}.pt'
        state=torch.load(ckpt,map_location='cpu',weights_only=False)
        model.load_state_dict(state['model'],strict=True)
        with torch.no_grad():
            action=model.sac_forward(obs,deterministic=True)[0].reshape(-1,16,10)
            q=model.sac_q_forward(obs,data['actions'])
        predictions[str(step)]=action.numpy()
        terminal=[{'index':i,'segment':r['segment_id'],'split':r['split'],
            'outcome':r['segment_outcome'],'reward_target':float(sum(r['rewards']))*cfg['gamma_per_step']**15,
            'q':q[i].tolist()} for i,r in enumerate(records) if r['terminated']]
        reports.append({'step':step,'checkpoint_sha256':sha(ckpt),'terminals':terminal,
            'q_outside_0_1_count':int(((q<0)|(q>1)).sum()),'q_values_count':q.numel(),
            'q_max':float(q.max()),'q_min':float(q.min()),
            'reference_mse':float((action-obs['ref_chunk'][:,:16]).square().mean()),
            'actor_optimizer_steps':sorted({int(v['step']) for v in state['actor_optimizer']['state'].values()}),
            'critic_optimizer_steps':sorted({int(v['step']) for v in state['critic_optimizer']['state'].values()})})
        run.log({'evaluation/checkpoint_step':step,'evaluation/q_max':float(q.max()),
            'evaluation/q_outside_0_1_count':reports[-1]['q_outside_0_1_count']},step=step)
    # Same saved states for reference, BC warmup and final actor; no robot I/O.
    chosen=[]
    for segment in sorted({r['segment_id'] for r in records}):
        ix=[i for i,r in enumerate(records) if r['segment_id']==segment]
        chosen.extend([ix[len(ix)//2],ix[-1]])
    stats=json.loads((Path(cfg['parent_bundle'])/'norm_stats.json').read_text())['action']
    ik=solver();probes=[]
    for i in chosen:
        record=records[i]
        for name,action in [('reference',obs['ref_chunk'][i,:16].numpy()),
                            ('warmup300',predictions['300'][i]),('actor800',predictions['800'][i])]:
            padded=np.zeros((16,32),np.float32);padded[:,:10]=action
            try:
                decoded=decode_prefix(ik,physical_actions(padded,stats),np.array(record['state16']),
                    np.array(record['submitted16'][0]),execute_steps=16)
                result={'success':decoded['success'],'gripper_clipped_count':decoded.get('gripper_clipped_count')}
            except ValueError as error:
                result={'success':False,'error':str(error)}
            probes.append({'index':i,'segment':record['segment_id'],'kind':name,**result})
    # Portable head weights are bound to this F1 output parameterization.
    export=root/'export';export.mkdir(exist_ok=False)
    from safetensors.torch import save_file
    save_file({k:v.detach().contiguous() for k,v in model.state_dict().items()},str(export/'head.safetensors'))
    export_manifest={'schema':'f1_rlt_stage2_head_v1','model_type':'f1_rlt_mlp_eef10_v1',
        'checkpoint_step':800,'model_config':cfg['model'],'head_sha256':sha(export/'head.safetensors'),
        'parent_bundle_sha256':json.loads((root/'cache/manifest.json').read_text())['parent_bundle_sha256'],
        'encoder_manifest_sha256':sha(Path(cfg['encoder'])/'manifest.json'),
        'output_parameterization':cfg['output_parameterization'],'reference_mode':cfg['reference_mode'],
        'adapter_sha256':sha(args.task_root/'src/f1_toys/rlt_stage2.py'),
        'deployment_ready':False,'rtc_prefix_routing_implemented':False,'asynchronous_runner':False,
        'init':'random RLinf actor/twin Q; B1250 and trained encoder frozen'}
    (export/'manifest.json').write_text(json.dumps(export_manifest,indent=2)+'\n')
    from safetensors.torch import load_file
    with torch.no_grad():
        before=model.sac_forward(obs,deterministic=True)[0]
        model.load_state_dict(load_file(str(export/'head.safetensors')),strict=True)
        assert torch.equal(before,model.sac_forward(obs,deterministic=True)[0])
    pre=[]
    for step in (2,3):
        state=torch.load(root/f'preflight/step-{step:06d}.pt',map_location='cpu',weights_only=False)
        pre.append({'step':step,'actor_optimizer_steps':sorted({int(s['step']) for s in state['actor_optimizer']['state'].values()}),
            'critic_optimizer_steps':sorted({int(s['step']) for s in state['critic_optimizer']['state'].values()})})
    summary={'checkpoints':reports,'ik_probes':probes,
        'ik_success_counts':{name:sum(p['success'] for p in probes if p['kind']==name) for name in ('reference','warmup300','actor800')},
        'ik_probes_per_model':len(chosen),'resume_optimizer_steps':pre,'export_reload_exact':True,
        'validation_scope':'One held-out failure recording; no independent successful rollout. TD targets use learned values and do not measure task success.',
        'robot_commands':0,'parameter_updates':0,'deployment_ready':False,'wandb_url':run.url}
    (out/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    run.summary.update(summary['ik_success_counts'])
    run.summary.update({'export_reload_exact':True,'deployment_ready':False,'parameter_updates':0})
    run.finish()
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
