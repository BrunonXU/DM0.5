#!/usr/bin/env python3
"""Paired offline Q-on/Q-off check with reviewed demos; never publishes weights."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','demo','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--q',choices=('on','off'),required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--warmup',type=int,default=128)
    p.add_argument('--joint',type=int,default=128)
    args=p.parse_args()
    import torch
    import wandb
    from f1_toys.rlt_online import LocalLearner,sha,atomic_json
    from f1_toys.rlt_critic_calibration import concatenate,subset,load_terminals,critic_report
    from f1_toys.rlt_evaluation import action_metrics,actor_digest
    torch.set_num_threads(2)
    cfg=json.loads(args.config.read_text())['f1']
    demo_meta=json.loads((args.demo/'manifest.json').read_text())
    if (demo_meta['objective']!='approach_place_release_v1'
            or demo_meta['capture_mode']!='DAgger'
            or demo_meta['feature_identity']!=cfg['replay']['feature_identity']):
        raise ValueError('Demo objective/source/features mismatch')
    for name,digest in demo_meta['files_sha256'].items():
        if sha(args.demo/name)!=digest:raise ValueError('Demo checksum mismatch')
    demo={s:torch.load(args.demo/(s+'.pt'),weights_only=True) for s in ('train','validation')}
    parts={'train':[], 'validation':[]};sources=[];seen={}
    for root in map(Path,cfg['seed_caches']):
        m=json.loads((root/'manifest.json').read_text())
        if (m['objective']!='approach_place_release_v1' or m.get('synthetic')
                or m['feature_identity']!=demo_meta['feature_identity'] or m['learning_scope']!='local_replay'):
            raise ValueError('Untrusted local replay')
        if m['episode_id'] in seen:raise ValueError('Duplicate or cross-split episode')
        seen[m['episode_id']]=m['split']
        for name,digest in m['files_sha256'].items():
            if sha(root/name)!=digest:raise ValueError('Local replay checksum mismatch')
        parts[m['split']].append(torch.load(root/'replay.pt',weights_only=True))
        sources.append(dict(episode=m['episode_id'],split=m['split'],manifest_sha256=sha(root/'manifest.json')))
    local={s:concatenate(parts[s]) for s in parts}
    anchors,anchor_meta=load_terminals(cfg['critic_terminal_cache'],demo_meta['feature_identity'])
    train_terms=concatenate([subset(demo['train'],demo['train']['terminated']),
                             subset(local['train'],local['train']['terminated']),anchors['train']])
    valid_terms=concatenate([subset(demo['validation'],demo['validation']['terminated']),
                             subset(local['validation'],local['validation']['terminated']),anchors['validation']])
    recipe=copy.deepcopy(cfg['training']);recipe.update(actor_update_interval=4,enable_q_guidance=False)
    identity=dict(recipe=recipe,q_branch=args.q,demo_manifest_sha256=sha(args.demo/'manifest.json'),
                  source_manifests=sources,terminal_manifest_sha256=sha(Path(cfg['critic_terminal_cache'])/'manifest.json'),
                  code_sha256={p.name:sha(p) for p in (Path(__file__),Path(__file__).resolve().parents[1]/'src/f1_toys/rlt_online.py')},
                  initial_head_sha256=sha(Path(cfg['initial_head'])/'manifest.json'),
                  warmup_steps=args.warmup,joint_steps=args.joint,demo_sample_fraction=.5,
                  deterministic_training=True,production_publication=False)
    args.output.mkdir(parents=True,exist_ok=False)
    run=wandb.init(project='f1_complexytoys',entity='robotics-example',
        group='12_RLT_Local_Demo_Joint_20261029',job_type='offline-q-ab',
        tags=['F1','RLT','DAgger_seed','Q_'+args.q,'no_deployment','development_validation'],
        id=args.run_id,name='local-demo-Q-'+args.q,dir=str(args.output),resume='never',
        mode=os.environ.get('WANDB_MODE','online'),config=identity)
    atomic_json(args.output/'wandb.json',dict(id=run.id,url=run.url,mode=run.settings.mode))
    atomic_json(args.output/'identity.json',identity)
    try:
        torch.manual_seed(recipe['seed'])
        learner=LocalLearner(cfg['core_root'],recipe,cfg['initial_head'])
        baseline={s:action_metrics(learner.model,data) for s,data in
                  (('old_demo_validation',demo['validation']),('new_local_validation',local['validation']))}
        initial_digest=actor_digest(learner.model)
        generator=torch.Generator().manual_seed(recipe['seed']+1)
        sample=lambda data,n:subset(data,torch.randint(len(data['actions']),(n,),generator=generator))
        history=[];common=None;started=time.monotonic()
        for step in range(args.warmup+args.joint):
            if step==args.warmup:
                assert actor_digest(learner.model)==initial_digest
                common=dict(actor_sha256=initial_digest,critic=critic_report(learner,valid_terms))
                learner.cfg['enable_q_guidance']=args.q=='on'
            batch=concatenate([sample(demo['train'],32),sample(local['train'],32)])
            metrics=learner.update(batch,terminal_batch=train_terms,update_actor=step>=args.warmup)
            metrics['updates_per_second']=(step+1)/(time.monotonic()-started)
            metrics['phase_joint']=int(step>=args.warmup)
            run.log(metrics,step=step+1);history.append(metrics)
            if (step+1)%32==0:print(args.q,step+1,metrics['critic_loss'],flush=True)
        checkpoint_sha=learner.save(args.output/'checkpoint',identity)
        restored=LocalLearner(cfg['core_root'],recipe,cfg['initial_head'])
        restored.restore(args.output/'checkpoint',identity)
        restored.cfg['enable_q_guidance']=args.q=='on'
        assert restored.step==learner.step and restored.q_updates==learner.q_updates
        for name in ('model','critic','target'):
            assert all(torch.equal(v,getattr(restored,name).state_dict()[k]) for k,v in getattr(learner,name).state_dict().items())
        result=dict(scope='offline_paired_update_check_not_task_success',q_branch=args.q,
            steps=learner.step,q_updates=learner.q_updates,actor_changed=actor_digest(learner.model)!=initial_digest,
            baseline=baseline,common_warmup=common,
            final={s:action_metrics(learner.model,data) for s,data in
                   (('old_demo_validation',demo['validation']),('new_local_validation',local['validation']))},
            critic=critic_report(learner,valid_terms),checkpoint_sha256=checkpoint_sha,
            checkpoint_roundtrip_exact=True,production_published=False,robot_commands=0,
            counts=dict(demo=demo_meta['counts'],local={s:len(local[s]['actions']) for s in local}),
            limitations=['Development validation has model-selection exposure.',
                         'Deterministic actor training retained for this single-variable Q comparison; stochastic collection is not tested.',
                         'No online autonomous success-rate evaluation.'])
        atomic_json(args.output/'result.json',result)
        (args.output/'history.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in history))
        run.summary.update(dict(total_updates=learner.step,q_updates=learner.q_updates,
                                checkpoint_roundtrip_exact=True,production_published=False))
        print(json.dumps(result),flush=True)
    except BaseException:
        run.finish(exit_code=1);raise
    run.finish()


if __name__=='__main__':
    main()
