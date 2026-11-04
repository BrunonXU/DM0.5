#!/usr/bin/env python3
"""Bounded terminal/TD diagnosis on reviewed real data; no production promotion."""
import argparse, copy, json, os, sys
from pathlib import Path
TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--legacy-cache',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--steps',type=int,default=400)
    p.add_argument('--terminal-suffixes',action='store_true')
    p.add_argument('--balance-terminal-episodes',action='store_true')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    import torch, wandb
    from f1_toys.rlt_online import LocalLearner, sha, atomic_json
    from f1_toys.rlt_local_pipeline import prepare_local_cache,FrozenFeatureClient
    from f1_toys.rlt_critic_calibration import (migrate_failure_terminals,load_terminals,
        critic_report,concatenate,subset,combine_training_suffixes)
    from f1_toys.rlt_evaluation import action_metrics,actor_digest
    cfg=json.loads(args.config.read_text())['f1'];torch.set_num_threads(2)
    recipe=copy.deepcopy(cfg['training']);recipe['enable_q_guidance']=False
    torch.manual_seed(recipe['seed'])
    # Isolated review, never mutate the raw interrupted event or the live reader.
    reviews=args.output/'reviews';reviews.mkdir()
    for source in Path(cfg['replay']['reviews_root']).glob('*.json'):
        (reviews/source.name).write_bytes(source.read_bytes())
    episode='f1_hil_20261028T051834Z_ad2e55bc'
    raw=Path(cfg['raw_root'])/episode
    receipt=raw/'local_segments/placement-1.json'
    original=json.loads(receipt.read_text())
    result=original['local_result']
    if ('Arm-body collision risk' not in result['termination_reason']
            or result['status']!='interrupted' or result['assisted']):
        raise ValueError('Unexpected source for the user-confirmed contact review')
    review=dict(objective='approach_place_release_v1',source_receipt_sha256=sha(receipt),
        approved=True,reviewer='Codex: user confirmed arm close to or contacting torso after this exact protective stop',
        status='failure',failure_type='policy_safety_termination',
        reason='Confirmed arm-body contact; no successful placement; retain executed prefix only',
        terminal_observation=result['terminal_observation'])
    atomic_json(reviews/(episode+'.json'),review)
    replay_recipe=copy.deepcopy(cfg['replay'])
    replay_recipe.update(reviews_root=str(reviews), data_policy=cfg['data_policy'],
                         data_policy_sha256=sha(cfg['data_policy']))
    failure_cache=prepare_local_cache(raw,args.output/'failure-cache',replay_recipe,
        FrozenFeatureClient(cfg['feature_endpoint'],replay_recipe['feature_identity']))
    migrate_failure_terminals(args.legacy_cache,args.output/'critic-anchors',
        cfg['replay']['feature_identity'],cfg['replay']['action_norm_stats'])
    anchors, anchor_manifest=load_terminals(args.output/'critic-anchors',cfg['replay']['feature_identity'])
    data={'train':[], 'validation':[]}; identities=[]
    for cache in [*Path(cfg['cache_root']).glob('*/manifest.json'),failure_cache/'manifest.json']:
        meta=json.loads(cache.read_text())
        if meta.get('synthetic') or meta['split'] not in data:raise ValueError('Unexpected real cache')
        for name,digest in meta['files_sha256'].items():
            if sha(cache.parent/name)!=digest:raise ValueError('Cache checksum mismatch')
        data[meta['split']].append(torch.load(cache.parent/'replay.pt',weights_only=True))
        identities.append(dict(episode=meta['episode_id'],split=meta['split'],manifest_sha256=sha(cache)))
    assert not ({i['episode'] for i in identities if i['split']=='train'} &
                {i['episode'] for i in identities if i['split']=='validation'})
    train,valid=concatenate(data['train']),concatenate(data['validation'])
    train_term=concatenate([subset(train,train['terminated']),anchors['train']])
    valid_term=concatenate([subset(valid,valid['terminated']),anchors['validation']])
    terminal_pools=None
    if args.terminal_suffixes:
        supplemental=[];suffix_roots=[]
        suffix_recipe=dict(replay_recipe,terminal_suffixes_only=True)
        for entry in identities:
            if entry['split']!='train': continue
            cache=prepare_local_cache(Path(cfg['raw_root'])/entry['episode'],
                args.output/'terminal-suffixes',suffix_recipe,
                FrozenFeatureClient(cfg['feature_endpoint'],suffix_recipe['feature_identity']))
            supplemental.append(torch.load(cache/'replay.pt',weights_only=True))
            suffix_roots.append(cache)
        combine_training_suffixes(args.output/'critic-anchors',suffix_roots,
            args.output/'combined-critic-anchors',cfg['replay']['feature_identity'])
        train_term=concatenate([anchors['train'],*supplemental])
        pools={}
        for i,row in enumerate(anchor_manifest['provenance']['train']):
            pools.setdefault(row['episode'],[]).append(i)
        offset=len(anchors['train']['actions'])
        for i,part in enumerate(supplemental):
            pools['local-'+str(i)]=list(range(offset,offset+len(part['actions'])))
            offset+=len(part['actions'])
        terminal_pools=[torch.tensor(pool,dtype=torch.long) for pool in pools.values()]
    if args.balance_terminal_episodes and terminal_pools is None:
        raise ValueError('Balanced terminal episodes require explicit suffix provenance')
    identity=dict(recipe=recipe,episodes=identities,terminal_suffixes=args.terminal_suffixes,
                  balance_terminal_episodes=args.balance_terminal_episodes,
                  critic_anchors_sha256=sha(args.output/'critic-anchors/manifest.json'))
    atomic_json(args.output/'data.json',identity)
    for name,value in [('train',train),('validation',valid),('train-terminals',train_term),('validation-terminals',valid_term)]:
        torch.save(value,args.output/(name+'.pt'))
    history=[]
    # One variable: terminal fitting, then add the actual temporal TD objective.
    learner=LocalLearner(cfg['core_root'],recipe,cfg['initial_head'])
    original_actor=actor_digest(learner.model)
    run=wandb.init(project='f1_complexytoys',entity='robotics-example',id=args.run_id,
        group='12_RLT_Q_Calibration_B1250',job_type='critic-terminal-td-diagnosis',
        tags=['f1','real-data','frozen-vla','critic-only','no-deploy'],dir=str(args.output),
        config=dict(identity=identity,steps_per_phase=args.steps,
                    source={n:sha(TASK/'src/f1_toys'/n) for n in ('rlt_online.py','rlt_critic_calibration.py')}))
    print('WANDB',run.url,flush=True)
    try:
        for phase in ('terminal_only','temporal_td'):
            for index in range(args.steps):
                if args.balance_terminal_episodes:
                    choices=torch.randint(len(terminal_pools),(64,))
                    indices=torch.tensor([int(terminal_pools[int(i)][torch.randint(len(terminal_pools[int(i)]),(1,))])
                                          for i in choices])
                else:
                    indices=torch.randint(len(train_term['actions']),(64,))
                terminals=subset(train_term,indices)
                batch=terminals if phase=='terminal_only' else subset(train,torch.randint(len(train['actions']),(64,)))
                metrics=learner.update(batch,update_actor=False,bootstrap=phase!='terminal_only',
                                       terminal_batch=None if phase=='terminal_only' else terminals)
                if index==0 or (index+1)%50==0:
                    report=critic_report(learner,valid_term)
                    report.update(phase=phase,step=learner.step,training=critic_report(learner,train_term))
                    history.append(report)
                    metrics.update({'validation/'+k:v for k,v in report['metrics'].items() if v is not None})
                    print(json.dumps(report),flush=True)
                    learner.save(args.output/'checkpoints'/f'step-{learner.step}',identity)
                run.log(metrics,step=learner.step)
            assert actor_digest(learner.model)==original_actor
        atomic_json(args.output/'history.json',history)
        atomic_json(args.output/'result.json',dict(final=history[-1],actor_unchanged=True,
            failure_cache=str(failure_cache),published=False,q_guidance_updates=learner.q_updates,
            wandb_url=run.url))
    finally:run.finish()


if __name__=='__main__':main()
