#!/usr/bin/env python3
"""Audit named arrivals and append eligible local replay with frozen splits."""
import argparse
import copy
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--episodes',nargs='+',required=True)
    a=p.parse_args()
    import torch
    torch.set_num_threads(2)
    from f1_toys.rlt_online import sha,atomic_json
    from f1_toys.rlt_local_replay import read_prefix
    from f1_toys.rlt_local_pipeline import prepare_local_cache,FrozenFeatureClient
    from f1_toys.rlt_prediction_index import source_predictions
    cfg=json.loads((a.data/'candidate-data-config.json').read_text());f=cfg['f1']
    migration=json.loads((a.data/'migration.json').read_text())
    a.output.mkdir(parents=True,exist_ok=False)
    client=FrozenFeatureClient(f['feature_endpoint'],f['replay']['feature_identity'])
    audit=[];sources=[]
    for name in a.episodes:
        if Path(name).name!=name:raise ValueError('Episode must be a directory name')
        raw=Path(f['raw_root'])/name;rp=raw/'local_segments/placement-1.json'
        manifest=json.loads((raw/'journal/manifest.json').read_text())
        assert manifest['complete'] and manifest['drop_count']==0 and manifest['writer_error'] is None
        for stream,digest in manifest['stream_sha256'].items():assert sha(raw/'journal'/(stream+'.jsonl'))==digest
        events=list(map(json.loads,(raw/'journal/events.jsonl').read_text().splitlines()))
        item=dict(episode=name,source_manifest_sha256=sha(raw/'journal/manifest.json'),
            complete=True,counts=manifest['counts'],local_opened=sum(e['event']=='rlt_local_opened' for e in events))
        if not rp.exists():
            assert item['local_opened']==0, 'Missing receipt after actual RLT entry'
            item.update(local_replay=False,reason='no_local_rlt_task',human_review_required=True,
                switch_rejections=[e for e in events if e['event']=='rlt_switch_rejected'])
        else:
            receipt=json.loads(rp.read_text());streams=read_prefix(raw,receipt)
            pred=source_predictions(f['prediction_log'],[e['plan'] for e in streams['events']
                if e['event']=='rlt_plan_adopted' and e['plan']['phase']=='actor'])
            cache=prepare_local_cache(raw,a.output/'local',f['replay'],client,predictions=pred)
            meta=json.loads((cache/'manifest.json').read_text())
            source=dict(episode=name,split=meta['split'],cache=str(cache),count=meta['transition_count'])
            sources.append(source)
            item.update(local_replay=True,**{k:v for k,v in source.items() if k!='episode'},
                        result=meta['local_result'],alignment=meta['decision_alignment'])
        audit.append(item);print(json.dumps(item),flush=True)
    combined=copy.deepcopy(migration)
    oldnames={e['episode'] for e in combined['sources']}
    if any(e['episode'] in oldnames for e in sources):raise ValueError('Arrival already included')
    combined['sources']+=sources
    combined.update(parent_migration_sha256=sha(a.data/'migration.json'),arrival_episodes=a.episodes)
    nf=cfg['f1'];nf['seed_caches']=[s['cache'] for s in combined['sources']]
    for k,sub in [('cache_root','local'),('health_root','health'),('snapshot_root','snapshots'),('promoted_checkpoint_root','promoted')]:nf[k]=str(a.output/sub)
    nf['publish_updates']=False;cfg['runner']['logger']['log_path']=str(a.output/'online')
    atomic_json(a.output/'candidate-data-config.json',cfg)
    atomic_json(a.output/'migration.json',combined)
    atomic_json(a.output/'arrival-audit.json',dict(episodes=audit,parameter_updates=0,production_changed=False))


if __name__=='__main__':main()
