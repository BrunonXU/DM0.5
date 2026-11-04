#!/usr/bin/env python3
"""Matched CPU BC/Q trials across controller action contracts; never deploys."""
import argparse
import copy
import json
from pathlib import Path
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--checkpoint',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--demo-raw',type=Path,required=True)
    p.add_argument('--run-prefix',required=True)
    p.add_argument('--steps',type=int,default=256)
    args=p.parse_args()
    import torch
    import numpy as np
    import wandb
    from f1_toys.rlt_online import LocalLearner, sha, atomic_json
    from f1_toys.rlt_local_replay import read_prefix
    from f1_toys.rlt_action_contract import REFERENCE,SUBMITTED,action_joints
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import concatenate,subset
    from f1_toys.rlt_evaluation import action_metrics
    from f1_toys.eef import solver,encode_actions,normalize_actions
    torch.set_num_threads(2)
    cfg=json.loads(args.config.read_text())['f1'];args.output.mkdir(parents=True,exist_ok=False)
    model=solver().model;stats=cfg['replay']['action_norm_stats']
    groups={s:[] for s in ('train','validation')};reference={s:[] for s in groups};sources=[]
    def convert(data,records,controls,sequence_field):
        actions=torch.zeros_like(data['actions']);out=[]
        for i,record in enumerate(records):
            chosen=[controls[n] for n in record[sequence_field]]
            if [r['submitted'] for r in chosen] != record['submitted16']:
                raise ValueError('Action publication sequence differs from cache')
            if any(r.get('publication_complete') is not True or r.get('stage')!='completed' for r in chosen):
                raise ValueError('Unpublished reference cannot train actor')
            row=dict(record,selected16=[r['selected'] for r in chosen],
                     action_time_mask=data['action_time_mask'][i].tolist())
            values=action_joints(row,REFERENCE)
            a=normalize_actions(encode_actions(model,record['state16'],values),stats)[...,:10]
            actions[i,:len(a)]=torch.from_numpy(a);out.append(row)
        return dict(data,actions=actions),out
    for entry in json.loads((args.checkpoint/'replay-catalog.json').read_text()):
        root=Path(entry['path']);m=json.loads((root/'manifest.json').read_text())
        assert sha(root/'manifest.json')==entry['sha256']
        for n,h in m['files_sha256'].items():assert sha(root/n)==h
        raw=Path(cfg['raw_root'])/m['episode_id'];receipt=json.loads((root/'source-receipt.json').read_text())
        assert sha(raw/'local_segments/placement-1.json')==m['source_receipt_sha256']
        streams=read_prefix(raw,receipt);controls={r['command_sequence']:r for r in streams['control']}
        data=torch.load(root/'replay.pt',weights_only=True)
        rows=[json.loads(s) for s in (root/'transitions.jsonl').read_text().splitlines()]
        revised,newrows=convert(data,rows,controls,'command_sequences')
        split=m['split'];groups[split].append(data);reference[split].append(revised)
        dest=args.output/'derived'/m['episode_id'];dest.mkdir(parents=True)
        torch.save(revised,dest/'replay.pt');(dest/'transitions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in newrows))
        atomic_json(dest/'provenance.json',dict(source_manifest_sha256=entry['sha256'],source_cache=str(root),action_representation=REFERENCE,source_receipt_sha256=m['source_receipt_sha256'],split=split))
        sources.append(dict(episode=m['episode_id'],split=split,transitions=len(rows),source_manifest_sha256=entry['sha256']))
    local={s:concatenate(groups[s]) for s in groups};local_reference={s:concatenate(reference[s]) for s in groups}
    demos,records,dm=load_demo_seed(cfg['demo_seed'],cfg['replay']['feature_identity'])
    receipts=json.loads((Path(cfg['demo_seed']['audit_root'])/'source-receipts.json').read_text())
    by_run={}
    for name,receipt in receipts.items():
        path=args.demo_raw/name/'journal/control.jsonl'
        assert sha(path)==receipt['files_sha256']['journal/control.jsonl']
        by_run[name]={r['stream_sequence']:r for r in map(json.loads,path.read_text().splitlines())}
    demo_reference={}
    for split in demos:
        parts=[]
        for i,row in enumerate(records[split]):
            part,_=convert(subset(demos[split],[i]),[row],by_run[row['source_run']],'action_control_sequences');parts.append(part)
        demo_reference[split]=concatenate(parts)
    validation=concatenate([local_reference['validation'],demo_reference['validation']])
    identity=dict(sources=sources,demo_manifest_sha256=cfg['demo_seed']['manifest_sha256'],
        checkpoint_sha256=sha(args.checkpoint/'learner.pt'),action_representation=REFERENCE,
        local_counts={s:len(local[s]['actions']) for s in local},demo_counts={s:len(demos[s]['actions']) for s in demos},
        scope='fixed existing development split; no new robot success estimate; no promotion',
        source_sha256={n:sha(TASK/'src/f1_toys'/n) for n in ('rlt_online.py','rlt_action_contract.py','rlt_evaluation.py')})
    atomic_json(args.output/'data-audit.json',identity)
    results=[]
    for representation in (SUBMITTED,REFERENCE):
        for enabled in (False,True):
            variant=('submitted' if representation==SUBMITTED else 'reference')+('-q' if enabled else '-bc')
            recipe=dict(cfg['training'],action_representation=representation,enable_q_guidance=enabled,gradient_diagnostics=True)
            torch.manual_seed(recipe['seed']);learner=LocalLearner(cfg['core_root'],recipe,cfg['initial_head'])
            local_data=(local if representation==SUBMITTED else local_reference)['train']
            demo_data=(demos if representation==SUBMITTED else demo_reference)['train']
            local_valid=(local if representation==SUBMITTED else local_reference)['validation']
            run=wandb.init(project='f1_complexytoys',entity='robotics-example',
                id=args.run_prefix+'-'+variant,group='15_RLT_Controller_Contract_V34',job_type='offline-contract-comparison',
                tags=['f1','cpu','frozen-vla','no-deploy',variant],dir=str(args.output),
                config=dict(recipe=recipe,data=identity,steps=args.steps))
            generator=torch.Generator().manual_seed(recipe['seed']);history=[]
            initial=action_metrics(learner.model,validation)
            try:
                for step in range(args.steps):
                    batch=concatenate([subset(part,torch.randint(len(part['actions']),(recipe['batch_size']//2,),generator=generator)) for part in (local_data,demo_data)])
                    metrics=learner.update(batch)
                    if step==0 or (step+1)%32==0:
                        report=action_metrics(learner.model,validation)
                        with torch.no_grad():
                            q=learner.critic(learner.model._critic_state(local_valid['obs']),local_valid['actions'],local_valid['action_time_mask'],local_valid['elapsed_steps']).mean(-1)
                            terminal=local_valid['terminated']
                            report['critic_terminal_mae']=float((q[terminal]-local_valid['reward'][terminal]).abs().mean())
                        metrics.update({'validation/'+k:v for k,v in report.items()})
                        history.append(dict(step=step+1,**report));print(variant,step+1,json.dumps(report),flush=True)
                    run.log(metrics,step=step+1)
                dest=args.output/variant;learner.save(dest,dict(data=identity,recipe=recipe))
                atomic_json(dest/'history.json',history)
                results.append(dict(variant=variant,initial=initial,final=history[-1],run_url=run.url))
            finally:run.finish()
    atomic_json(args.output/'comparison.json',dict(data=identity,results=results,deployed=False))


if __name__=='__main__':main()
