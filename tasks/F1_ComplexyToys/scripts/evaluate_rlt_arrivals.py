#!/usr/bin/env python3
"""First-look evaluation of held-out arrivals; no optimizer or deployment."""
import argparse
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--comparison',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    a=p.parse_args()
    import torch
    import wandb
    torch.set_num_threads(2)
    from f1_toys.rlt_online import LocalLearner,sha,atomic_json
    from f1_toys.rlt_evaluation import action_metrics,grouped_action_metrics
    from f1_toys.rlt_critic_calibration import concatenate,critic_report
    cfg=json.loads((a.data/'candidate-data-config.json').read_text())['f1']
    arrivals=json.loads((a.data/'arrival-audit.json').read_text())['episodes']
    identities=json.loads((a.comparison/'data-identity.json').read_text())
    parts=[];ids=[]
    for entry in arrivals:
        if not entry['local_replay']:continue
        assert entry['split']=='validation' and entry['episode'] not in identities['train_episodes']
        assert entry['episode'] not in identities['validation_episodes'], 'Not a first-look holdout'
        root=Path(entry['cache']);meta=json.loads((root/'manifest.json').read_text())
        for name,digest in meta['files_sha256'].items():assert sha(root/name)==digest
        parts.append(torch.load(root/'replay.pt',weights_only=True))
        ids += [entry['episode']]*entry['count']
    data=concatenate(parts)
    a.output.mkdir(parents=True,exist_ok=False)
    identity=dict(arrival_audit_sha256=sha(a.data/'arrival-audit.json'),
                  trained_identity_sha256=sha(a.comparison/'data-identity.json'),episodes=sorted(set(ids)),
                  parameter_updates=0,robot_commands=0,scope='new episode; same scene, not independent-layout proof')
    run=wandb.init(project='f1_complexytoys',entity='robotics-example',id=a.run_id,
                   group='17_RLT_Decision_V40',job_type='new-arrival-validation',
                   tags=['f1','validation','zero-update','no-deploy'],dir=str(a.output),config=identity)
    reports=[]
    try:
        paths=[('bc400',None)]
        for variant in ('decision-bc','decision-q'):
            paths += [(variant+'-'+str(step),a.comparison/variant/('step-'+str(step))) for step in (128,256,512)]
            paths += [(variant+'-1024',a.comparison/variant)]
        for index,(name,path) in enumerate(paths):
            learner=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head'])
            if path:
                saved=torch.load(path/'learner.pt',weights_only=True)
                assert not set(ids)&set(saved['identity']['data']['train_episodes'])
                learner.restore(path,saved['identity'])
            metrics=action_metrics(learner.model,data,allow_incomplete=True)
            report=dict(name=name,actions=metrics,groups=grouped_action_metrics(learner.model,data,ids),
                        checkpoint_sha256=sha(path/'learner.pt') if path else sha(Path(cfg['initial_head'])/'manifest.json'))
            if path:report['critic']=critic_report(learner,data)
            reports.append(report)
            run.log({name+'/'+key:value for key,value in metrics.items() if value is not None},step=index)
            print(json.dumps(dict(name=name,actions=metrics)),flush=True)
        atomic_json(a.output/'report.json',dict(identity=identity,results=reports,run_url=run.url,deployed=False))
    finally:run.finish()


if __name__=='__main__':main()
