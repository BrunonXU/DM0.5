"""Source-bound reconstruction of legacy demonstrations and terminal anchors."""
from pathlib import Path
import json

import numpy as np
import torch

from .rlt_online import sha,atomic_json
from .rlt_decision_replay import _human_record
from .rlt_action_contract import DECISION
from .rlt_demo_seed import load_demo_seed
from .rlt_critic_calibration import concatenate,subset
from .eef import solver,encode_actions,normalize_actions


def rebuild_legacy_data(source,output,config,client,*,demo_raw,legacy_raw):
    f=config['f1'];identity=f['replay']['feature_identity'];stats=f['replay']['action_norm_stats']
    data,rows,manifest=load_demo_seed(f['demo_seed'],identity)
    audit=Path(f['demo_seed']['audit_root']);receipts=json.loads((audit/'source-receipts.json').read_text())
    kin=solver().model;raw={};features={};newrows=[];parts={s:[] for s in data}
    def load_raw(root,name,receipt=None):
        key=(str(root),name)
        if key not in raw:
            path=Path(root)/name
            if receipt:
                for file,digest in receipt['files_sha256'].items():
                    if sha(path/file)!=digest:raise ValueError('Legacy raw source changed: '+file)
            else:
                meta=json.loads((path/'journal/manifest.json').read_text())
                for stream,digest in meta['stream_sha256'].items():
                    if sha(path/'journal'/(stream+'.jsonl'))!=digest:raise ValueError('Legacy raw journal changed')
            obs={r['observation_id']:r for r in map(json.loads,(path/'journal/observations.jsonl').read_text().splitlines())}
            controls=list(map(json.loads,(path/'journal/control.jsonl').read_text().splitlines()))
            payload={r['observation_id']:r for r in map(json.loads,(path/'payload/frames.jsonl').read_text().splitlines())}
            raw[key]=(path,obs,controls,payload)
        return raw[key]
    def make_row(old,root,name,human):
        path,obs,controls,payload=load_raw(root,name,receipts.get(name))
        # Prove the source commands still match the reviewed original cache.
        cmap={c['stream_sequence']:c for c in controls}
        if [cmap[i]['submitted'] for i in old['action_control_sequences']]!=old['submitted16']:
            raise ValueError('Reviewed legacy publication evidence changed')
        row=_human_record(dict(old,reward=sum(old['rewards'])),obs,controls,
                          [c['monotonic_ns'] for c in controls],gamma=f['replay']['gamma_per_step'],fps=30,human=human)
        for field in ('observation_id','next_observation_id'):
            oid=row[field];key=(name,oid)
            if key not in features:
                result=client(obs[oid],payload[oid],path/'payload')
                features[key]={k:torch.tensor(v,dtype=torch.float32) for k,v in result.items()}
        n=sum(row['action_time_mask'])
        actions=torch.zeros(1,16,10)
        actions[0,:n]=torch.from_numpy(normalize_actions(encode_actions(kin,row['state16'],row['selected16']),stats)[...,:10])
        part=dict(obs={k:v[None] for k,v in features[(name,row['observation_id'])].items()},
                  next_obs={k:v[None] for k,v in features[(name,row['next_observation_id'])].items()},actions=actions)
        for key,dtype in [('action_time_mask',torch.bool),('intervene_flags',torch.bool),('terminated',torch.bool),
                          ('truncated',torch.bool),('reward',torch.float32),('discount',torch.float32),
                          ('elapsed_steps',torch.float32),('execution_fraction',torch.float32)]:
            part[key]=torch.tensor([row[key]],dtype=dtype)
        row['rewards']=[0.]*15+[row['reward']]
        return row,part
    for split,records in rows.items():
        for i,r in enumerate(records):
            for field,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
                features[(r['source_run'],r[field])]={k:v[i] for k,v in data[split][part].items()}
    for split,records in rows.items():
        for r in records:
            row,part=make_row(r,demo_raw,r['source_run'],True)
            newrows.append(row);parts[split].append(part)
    dest=Path(output)/'demo';dest.mkdir(exist_ok=True)
    newaudit=dest/'audit';newaudit.mkdir(exist_ok=True)
    (newaudit/'transitions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in newrows))
    atomic_json(newaudit/'source-receipts.json',receipts)
    atomic_json(newaudit/'audit.json',dict(schema='f1_rlt_decision_demo_audit_v1',
        objective=manifest['objective'],source_audit_sha256=sha(audit/'audit.json'),
        files_sha256={n:sha(newaudit/n) for n in ('transitions.jsonl','source-receipts.json')}))
    demos={s:concatenate(v) for s,v in parts.items()}
    for s,v in demos.items():torch.save(v,dest/(s+'.pt'))
    atomic_json(dest/'manifest.json',dict(manifest,action_representation=DECISION,
        source_manifest_sha256=sha(Path(f['demo_seed']['root'])/'manifest.json'),
        audit_sha256=sha(newaudit/'audit.json'),counts={s:len(v['actions']) for s,v in demos.items()},
        files_sha256={s+'.pt':sha(dest/(s+'.pt')) for s in demos}))
    demo=dict(root=str(dest),audit_root=str(newaudit),manifest_sha256=sha(dest/'manifest.json'))
    load_demo_seed(demo,identity)

    # Find the original approved failed placements through the immutable chain.
    oldroot=Path(f['critic_terminal_cache'])
    while True:
        m=json.loads((oldroot/'manifest.json').read_text())
        if m['schema']=='f1_rlt_dagger_replay_v1':break
        nxt=Path(m['source'])
        if sha(nxt/'manifest.json')!=m['source_manifest_sha256']:raise ValueError('Terminal source chain changed')
        oldroot=nxt
    for file,digest in m['files_sha256'].items():
        if sha(oldroot/file)!=digest:raise ValueError('Original terminal cache changed')
    original=torch.load(oldroot/'replay.pt',map_location='cpu',weights_only=True)
    records=list(map(json.loads,(oldroot/'transitions.jsonl').read_text().splitlines()))
    terms={s:[] for s in parts};provenance={s:[] for s in parts}
    for i,r in enumerate(records):
        if not r.get('terminated') or r.get('segment_outcome')!='failure':continue
        name=r['source_run']
        for field,indices in [('observation_id','current'),('next_observation_id','next')]:
            features[(name,r[field])]={k:original[k][int(original[indices][i])] for k in ('z_rl','proprio','ref_chunk')}
        row,part=make_row(r,legacy_raw,name,False)
        terms[r['split']].append(part)
        provenance[r['split']].append(dict(episode=name,segment=r['segment_id'],
            decision_ns=row['decision_ns'],next_decision_ns=row['next_decision_ns']))
    # One actual successful terminal per local training episode; repeated
    # suffixes are not independent outcomes and do not dominate this rebuild.
    for cache in (Path(output)/'local').glob('*/manifest.json'):
        m=json.loads(cache.read_text())
        if m['split']!='train':continue
        v=torch.load(cache.parent/'replay.pt',map_location='cpu',weights_only=True)
        rr=list(map(json.loads,(cache.parent/'transitions.jsonl').read_text().splitlines()))
        for i,r in enumerate(rr):
            if r['terminated']:
                terms['train'].append(subset(v,torch.tensor([i])))
                provenance['train'].append(dict(episode=m['episode_id'],segment=r['segment_id'],
                    decision_ns=r['decision_ns'],next_decision_ns=r['next_decision_ns']))
    terminal=Path(output)/'terminals';terminal.mkdir(exist_ok=True)
    for s,v in terms.items():torch.save(concatenate(v),terminal/(s+'.pt'))
    atomic_json(terminal/'manifest.json',dict(schema='f1_rlt_critic_terminals_v1',
        objective='approach_place_release_v1',feature_identity=identity,action_representation=DECISION,
        source=str(oldroot),source_manifest_sha256=sha(oldroot/'manifest.json'),provenance=provenance,
        scope='reviewed_placement_terminals_one_per_episode',
        validation_scope='previous_development_holdout_not_new_blind_test',
        files_sha256={s+'.pt':sha(terminal/(s+'.pt')) for s in terms}))
    return demo,str(terminal)
