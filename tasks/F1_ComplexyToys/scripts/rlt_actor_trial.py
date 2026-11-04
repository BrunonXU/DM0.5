"""Bounded clean actor/critic trials with episode-held-out development checks."""
import argparse, copy, json, os, sys, time
from pathlib import Path
TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--train-cache',type=Path,required=True);p.add_argument('--validation-cache',type=Path,required=True)
    p.add_argument('--steps',type=int,default=400);p.add_argument('--run-prefix',required=True)
    args=p.parse_args();args.output.mkdir(exist_ok=False,parents=True)
    import torch, wandb
    from f1_toys.rlt_online import LocalLearner,sha,atomic_json
    from f1_toys.rlt_evaluation import action_metrics,evaluate_candidate
    from f1_toys.rlt_conditioning import fit_conditioning,export_actor_state
    from f1_toys.rlt_snapshots import publish_actor
    cfg=json.loads(args.config.read_text())['f1'];torch.set_num_threads(2)
    train_manifest=json.loads((args.train_cache/'manifest.json').read_text())
    valid_manifest=json.loads((args.validation_cache/'manifest.json').read_text())
    assert train_manifest['episode_id']!=valid_manifest['episode_id']
    assert not train_manifest['synthetic'] and not valid_manifest['synthetic']
    for root,manifest in ((args.train_cache,train_manifest),(args.validation_cache,valid_manifest)):
        for name,digest in manifest['files_sha256'].items():assert sha(root/name)==digest
    train=torch.load(args.train_cache/'replay.pt',weights_only=True)
    valid=torch.load(args.validation_cache/'replay.pt',weights_only=True)
    identity=dict(train_episode=train_manifest['episode_id'],validation_episode=valid_manifest['episode_id'],
        train_cache_sha256=sha(args.train_cache/'manifest.json'),validation_cache_sha256=sha(args.validation_cache/'manifest.json'),
        caveat='development_holdout; both episodes used only in discarded engineering preflight; prospective robot evaluation required')
    assert not cfg['training']['enable_q_guidance']
    torch.manual_seed(cfg['training']['seed'])
    baseline=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head'])
    baseline_metrics=action_metrics(baseline.model,valid)
    print('BASELINE',json.dumps(baseline_metrics),flush=True)
    initial_manifest=json.loads((Path(cfg['initial_head'])/'manifest.json').read_text())
    stats=fit_conditioning(baseline.model,train['obs'])
    results=[]
    # uniform -> phase-balanced changes sampling only; conditioned changes input
    # parameterization only. All share seed, lr, steps and original BC400 weights.
    for variant in ('uniform','balanced','balanced_conditioned'):
        torch.manual_seed(cfg['training']['seed']);recipe=copy.deepcopy(cfg['training'])
        recipe.update(actor_update_interval=1,learning_rate=1e-4)
        if variant=='balanced_conditioned':recipe['input_conditioning']=stats
        learner=LocalLearner(cfg['core_root'],recipe,cfg['initial_head'])
        output=args.output/variant;output.mkdir()
        run=wandb.init(project='f1_complexytoys',entity='robotics-example',
            id=args.run_prefix+'-'+variant,group='11_RLT_Local_Actor_Repair_B1250',job_type='bounded-actor-comparison',
            tags=['f1','cpu','frozen-vla','no-deploy',variant],config={'recipe':recipe,'data':identity,'steps':args.steps,
                'source':{n:sha(TASK/'src/f1_toys'/n) for n in ('rlt_online.py','rlt_conditioning.py','rlt_evaluation.py')}},dir=str(output))
        mask=train['action_time_mask'] & train['intervene_flags']
        opens=((train['actions'][...,9]<-.5)&mask).any(1)
        holds=((train['actions'][...,9]>.5)&mask).any(1)&~opens
        pools=[torch.nonzero(m).flatten() for m in (opens,holds,~(opens|holds))]
        best=None;history=[]
        try:
            for step in range(args.steps):
                if variant=='uniform':idx=torch.randint(len(opens),(64,))
                else:
                    nonempty=[pool for pool in pools if len(pool)]
                    idx=torch.cat([pool[torch.randint(len(pool),(64//len(nonempty)+1,))] for pool in nonempty])[:64]
                batch={k:{n:t[idx] for n,t in v.items()} if isinstance(v,dict) else v[idx] for k,v in train.items()}
                metrics=learner.update(batch)
                if (step+1)%50==0 or step==0:
                    report=evaluate_candidate(learner.model,baseline.model,valid,identity)
                    report['step']=step+1;history.append(report)
                    metrics.update({'validation/'+k:v for k,v in report['candidate'].items()})
                    score=report['candidate']['human_mse']
                    if best is None or (report['passed'] and not best['passed']) or (report['passed']==best['passed'] and score<best['candidate']['human_mse']):
                        best=report;learner.save(output,dict(data=identity,recipe=recipe))
                    print(variant,step+1,json.dumps(report['candidate']),report['passed'],flush=True)
                run.log(metrics,step=step+1)
            atomic_json(output/'history.json',history)
            learner.restore(output,dict(data=identity,recipe=recipe))
            exported=export_actor_state(learner.model)
            fresh=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head'])
            state=fresh.model.state_dict();state.update(exported);fresh.model.load_state_dict(state)
            with torch.no_grad():
                a=learner.model.sac_forward(valid['obs'],deterministic=True)[0]
                b=fresh.model.sac_forward(valid['obs'],deterministic=True)[0]
            torch.testing.assert_close(a,b,atol=3e-5,rtol=3e-5)
            best['folded_export_max_error']=float((a-b).abs().max())
            # Candidates stay unapproved; a separate integration check promotes.
            path=publish_actor(learner,initial_manifest,output/'candidates',evaluation=best)
            best.update(candidate_path=str(path),checkpoint_path=str(output),variant=variant)
            atomic_json(output/'best.json',best);atomic_json(output/'recipe.json',recipe)
            results.append(best)
        finally:run.finish()
    atomic_json(args.output/'comparison.json',dict(data=identity,baseline=baseline_metrics,results=results,published=False))


if __name__=='__main__':main()
