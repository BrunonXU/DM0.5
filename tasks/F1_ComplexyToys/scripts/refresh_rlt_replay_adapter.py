#!/usr/bin/env python3
"""Rebuild reviewed replay with a new adapter and prove tensor equivalence.

The source split, reviews, raw records, and checkpoints remain immutable.
No model calls or gradients are permitted by this migration.
"""
import argparse
import copy
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bootstrap',type=Path,required=True)
    a=p.parse_args()
    import torch
    from f1_toys.rlt_online import sha,atomic_json
    from f1_toys.rlt_local_pipeline import prepare_local_cache
    from f1_toys.rlt_decision_replay import prediction_key
    torch.set_num_threads(2)
    config=json.loads((a.source/'candidate-data-config.json').read_text())
    original=json.loads((a.source/'migration.json').read_text())
    a.output.mkdir(parents=True,exist_ok=False)
    f=config['f1']; sources=[]
    def identical(x,y):
        if set(x)!=set(y):raise ValueError('Tensor schema changed')
        for key in x:
            if isinstance(x[key],dict):identical(x[key],y[key])
            elif not torch.equal(x[key],y[key]):raise ValueError('Historical tensor changed: '+key)
    for entry in original['sources']:
        old=Path(entry['cache']); meta=json.loads((old/'manifest.json').read_text())
        for name,digest in meta['files_sha256'].items():
            if Path(name).name!=name or sha(old/name)!=digest:raise ValueError('Source changed')
        data=torch.load(old/'replay.pt',weights_only=True)
        rows=list(map(json.loads,(old/'transitions.jsonl').read_text().splitlines()))
        features={}
        for i,row in enumerate(rows):
            for field,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
                features[row[field]]={k:v[i].tolist() for k,v in data[part].items()}
        predictions={prediction_key(r):r for r in map(json.loads,(old/'predictions.jsonl').read_text().splitlines())}
        def provider(obs,*_):return features[obs['observation_id']]
        cache=prepare_local_cache(Path(f['raw_root'])/entry['episode'],a.output/'local',
                                  f['replay'],provider,predictions=predictions)
        identical(data,torch.load(cache/'replay.pt',weights_only=True))
        m=json.loads((cache/'manifest.json').read_text())
        if m['split']!=entry['split'] or m['transition_count']!=entry['count']:
            raise ValueError('Review or split changed')
        sources.append(dict(entry,cache=str(cache),parent_cache=str(old),tensors_identical=True))
        print('verified',entry['episode'],entry['count'],flush=True)
    migration=dict(original,sources=sources,parent_migration_sha256=sha(a.source/'migration.json'),
        adapter_refresh=True,parameter_updates=0,production_changed=False)
    atomic_json(a.output/'migration.json',migration)
    if (a.source/'server-predictions.jsonl').exists():
        (a.output/'server-predictions.jsonl').write_bytes((a.source/'server-predictions.jsonl').read_bytes())
    saved=torch.load(a.bootstrap/'learner.pt',weights_only=True)
    if saved['identity']['data']['migration_sha256']!=sha(a.source/'migration.json'):
        raise ValueError('Bootstrap used another dataset')
    f.update(seed_caches=[s['cache'] for s in sources],cache_root=str(a.output/'local'),
        snapshot_root=str(a.output/'snapshots'),health_root=str(a.output/'health'),
        promoted_checkpoint_root=str(a.output/'promoted'),publish_updates=False,
        training=saved['identity']['recipe'],initial_update_budget=saved['step'],
        wandb_group='18_RLT_Objective_RTC_V41',
        prediction_logs=[f['prediction_log'],str(a.output.parent/'service/logs/requests.jsonl')],
        bootstrap_checkpoint=dict(schema='f1_rlt_offline_bootstrap_v1',path=str(a.bootstrap),
            parent_step=saved['step'],parent_action_representation=saved['identity']['recipe']['action_representation'],
            files_sha256={'learner.pt':sha(a.bootstrap/'learner.pt')},
            migration_sha256=saved['identity']['data']['migration_sha256']))
    config['runner']['logger'].update(log_path=str(a.output/'online'),experiment_name='f1-rlt-rtc-V41')
    config['runner']['resume_dir']=None
    atomic_json(a.output/'candidate-data-config.json',config)
    atomic_json(a.output/'refresh-report.json',dict(passed=True,episodes=len(sources),
        tensors_identical=True,counts={s:sum(v['count'] for v in sources if v['split']==s) for s in ('train','validation')},
        bootstrap_step=saved['step'],q_updates=saved['q_updates'],robot_commands=0,parameter_updates=0))


if __name__=='__main__':main()
