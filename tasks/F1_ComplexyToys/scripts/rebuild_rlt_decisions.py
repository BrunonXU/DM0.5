#!/usr/bin/env python3
"""Rebuild an isolated decision-aligned dataset from approved raw sources.

No weight updates, live-buffer mutation or robot commands. Original replies
are copied into a bounded provenance index so an append-only log cannot change
the identity of a prepared dataset.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--requests',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--audit-only',action='store_true')
    p.add_argument('--demo-raw',type=Path)
    p.add_argument('--legacy-raw',type=Path)
    a=p.parse_args()
    from f1_toys.rlt_decision_replay import build_decision_transitions,prediction_key
    from f1_toys.rlt_local_replay import read_prefix
    from f1_toys.rlt_priority import effective_result
    def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    config=json.loads((a.source/'candidate-data-config.json').read_text())
    migration=json.loads((a.source/'migration.json').read_text())
    f=config['f1'];raw_root=Path(f['raw_root'])
    a.output.mkdir(parents=True,exist_ok=True)
    episodes={};wanted=set()
    for entry in migration['sources']:
        raw=raw_root/entry['episode'];rp=raw/'local_segments/placement-1.json'
        receipt=json.loads(rp.read_text());streams=read_prefix(raw,receipt)
        review_path=Path(f['replay']['reviews_root'])/(entry['episode']+'.json')
        review=json.loads(review_path.read_text()) if review_path.exists() else None
        result=effective_result(receipt,digest(rp),review)
        episodes[entry['episode']]=(entry,streams,result)
        wanted.update(prediction_key(e['plan']) for e in streams['events']
                      if e['event']=='rlt_plan_adopted' and e['plan']['phase']=='actor')
    predictions={};index=a.output/'server-predictions.jsonl'
    if index.exists():
        predictions={prediction_key(r):r for r in map(json.loads,index.read_text().splitlines())}
    else:
        # Bounded at the opening size. A live trailing partial line is ignored.
        limit=a.requests.stat().st_size
        with a.requests.open('rb') as handle:
            while handle.tell()<limit:
                line=handle.readline()
                if handle.tell()>limit or not line.endswith(b'\n'):break
                r=json.loads(line)
                if not r.get('success') or 'prediction_replay' not in r:continue
                if prediction_key(r) not in wanted:continue
                r['_source_line_sha256']=hashlib.sha256(line).hexdigest()
                key=prediction_key(r)
                if key in predictions and predictions[key]!=r:raise ValueError('Ambiguous server prediction')
                predictions[key]=r
        index.write_text(''.join(json.dumps(v)+'\n' for _,v in sorted(predictions.items())))
    report=dict(schema='f1_decision_rebuild_v1',source_migration_sha256=digest(a.source/'migration.json'),
                server_prediction_index_sha256=digest(index),episodes=[],raw_mutations=0,
                parameter_updates=0,production_changed=False)
    for name,(entry,streams,result) in episodes.items():
        try:
            records, audit=build_decision_transitions(streams,result,predictions)
            item=dict(episode=name,split=entry['split'],count=len(records),**audit)
        except ValueError as exc:
            item=dict(episode=name,split=entry['split'],error=str(exc))
        report['episodes'].append(item)
        print(json.dumps({k:v for k,v in item.items() if k!='excluded'}),flush=True)
    (a.output/'alignment-audit.json').write_text(json.dumps(report,indent=2)+'\n')
    if a.audit_only:return
    if a.demo_raw is None or a.legacy_raw is None:raise ValueError('Explicit legacy raw roots required')
    if any('error' in e for e in report['episodes']):raise ValueError('Resolve episode alignment errors before materialization')
    import numpy as np
    import torch
    from f1_toys.rlt_action_contract import DECISION
    from f1_toys.rlt_local_pipeline import prepare_local_cache,FrozenFeatureClient
    from f1_toys.rlt_online import atomic_json,sha
    torch.set_num_threads(2)
    client=FrozenFeatureClient(f['feature_endpoint'],f['replay']['feature_identity'])
    replay=dict(f['replay'],action_representation=DECISION,
                server_prediction_index_sha256=sha(index))
    sources=[]
    for name,(entry,streams,result) in episodes.items():
        old=Path(entry['cache']);meta=json.loads((old/'manifest.json').read_text())
        for file,value in meta['files_sha256'].items():
            if sha(old/file)!=value:raise ValueError('Source cache digest changed')
        data=torch.load(old/'replay.pt',map_location='cpu',weights_only=True)
        records=[json.loads(s) for s in (old/'transitions.jsonl').read_text().splitlines()]
        features={}
        for i,r in enumerate(records):
            for key,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
                features[r[key]]={k:v[i].tolist() for k,v in data[part].items()}
        def provider(obs,payload,root):
            if obs['observation_id'] in features:return features[obs['observation_id']]
            return client(obs,payload,root)
        cache=prepare_local_cache(raw_root/name,a.output/'local',replay,provider,predictions=predictions)
        m=json.loads((cache/'manifest.json').read_text())
        if m['split']!=entry['split']:raise ValueError('Frozen split changed')
        sources.append(dict(episode=name,split=m['split'],cache=str(cache),count=m['transition_count']))
        print('materialized',name,m['transition_count'],flush=True)
    # Legacy demo and failure-terminal conversion is kept explicit. Reuse only
    # source-bound observations and reconstruct future targets from raw journals.
    from f1_toys.rlt_decision_dataset import rebuild_legacy_data
    demo,terminal=rebuild_legacy_data(a.source,a.output,config,client,demo_raw=a.demo_raw,legacy_raw=a.legacy_raw)
    candidate=copy.deepcopy(config);nf=candidate['f1']
    nf.update(replay=replay,cache_root=str(a.output/'local'),seed_caches=[s['cache'] for s in sources],
              prediction_log=str(a.requests),
              priority_root=str(Path(f['raw_root']).parent/'priority-V40'),
              wandb_group='17_RLT_Decision_V40',
              demo_seed=demo,critic_terminal_cache=terminal,publish_updates=False,
              bootstrap_checkpoint=None,health_root=str(a.output/'health'),
              snapshot_root=str(a.output/'snapshots'),promoted_checkpoint_root=str(a.output/'promoted'))
    nf['training']['action_representation']=DECISION
    candidate['runner'].update(resume_dir=None)
    candidate['runner']['logger']['log_path']=str(a.output/'online')
    candidate['runner']['logger']['experiment_name']='f1-rlt-decision-V40'
    atomic_json(a.output/'candidate-data-config.json',candidate)
    atomic_json(a.output/'migration.json',dict(report,sources=sources,action_representation=DECISION,
                                              decision_anchor_migration=True))


if __name__=='__main__':main()
