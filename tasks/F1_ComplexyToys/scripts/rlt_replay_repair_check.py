"""Bounded real-replay engineering check; never publishes an actor or controls F1."""
import argparse
import copy
import json
from pathlib import Path
import sys
import time

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--episodes',nargs='+',required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    import torch,wandb
    from f1_toys.rlt_local_pipeline import prepare_local_cache,FrozenFeatureClient,replay_adapter_identity
    from f1_toys.rlt_online import LocalLearner,atomic_json,sha
    cfg=json.loads(args.config.read_text())['f1'];torch.set_num_threads(2)
    torch.manual_seed(cfg['training']['seed'])
    assert cfg['publish_updates'] is False and cfg['training']['enable_q_guidance'] is False
    run=wandb.init(project='f1_complexytoys',entity='robotics-example',
        group='10_RLT_Local_Async_B1250',job_type='real-replay-repair-preflight',
        tags=['f1','real-data','replay-repair','cpu-small-head','no-deploy'],id=args.run_id,
        config={'recipe':cfg,'adapter':replay_adapter_identity(),'episodes':args.episodes,
                'script_sha256':sha(Path(__file__))},dir=str(args.output))
    caches=[]
    try:
        client=FrozenFeatureClient(cfg['feature_endpoint'],cfg['replay']['feature_identity'])
        for name in args.episodes:
            assert Path(name).name==name
            deadline=time.monotonic()+900
            while True:
                try:
                    cache=prepare_local_cache(Path(cfg['raw_root'])/name,args.output/'replay',cfg['replay'],client)
                    break
                except Exception as exc:
                    if time.monotonic()>deadline or not any(x in str(exc).lower() for x in ['busy','timed out','timeout']):raise
                    print('Feature service busy; retrying without blocking control',flush=True);time.sleep(2)
            caches.append(cache);print('CACHE_READY',name,str(cache),flush=True)
        # Exercise both sources, partial prefixes and terminal rewards. This is
        # an engineering preflight, not a validation-based model selection run.
        batches=[torch.load(c/'replay.pt',weights_only=False) for c in caches]
        learner=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head'])
        initial=sha(Path(cfg['initial_head'])/'head.safetensors')
        baseline=copy.deepcopy(learner.model.actor_mean.state_dict())
        for step in range(8):
            data=batches[step%len(batches)]
            metrics=learner.update(data);run.log(metrics,step=step);print('STEP',step+1,metrics['critic_loss'],flush=True)
        identity={'initial_head_sha256':initial,'adapter':replay_adapter_identity(),'episodes':args.episodes}
        learner.save(args.output/'checkpoint',identity)
        restored=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head']);restored.restore(args.output/'checkpoint',identity)
        with torch.no_grad():
            a=learner.model.sac_forward(batches[-1]['obs'],deterministic=True)[0]
            b=restored.model.sac_forward(batches[-1]['obs'],deterministic=True)[0]
        torch.testing.assert_close(a,b,atol=0,rtol=0)
        assert initial==sha(Path(cfg['initial_head'])/'head.safetensors')
        assert any(not torch.equal(v,baseline[k]) for k,v in learner.model.actor_mean.state_dict().items())
        atomic_json(args.output/'acceptance.json',dict(passed=True,real_replay=True,
            gradient_steps=learner.step,actor_updates=2,q_guidance_updates=learner.q_updates,
            actor_restore_exact=True,base_export_unchanged=True,actor_published=False,
            caches=[str(c) for c in caches],cache_manifests=[json.loads((c/'manifest.json').read_text()) for c in caches],
            outcome='engineering_check_only_not_model_improvement',wandb_run_id=args.run_id))
    finally:run.finish()


if __name__=='__main__':main()
