#!/usr/bin/env python3
"""Derive source-bound controller-reference caches; never edit originals or train.

This migration preserves observation anchors, labels, split and masks. It does
not certify that legacy arbitrary windows are decision-aligned RTC trajectories.
The output is a candidate data contract, not an automatic deployment approval.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--config',type=Path,required=True)
    a.add_argument('--output',type=Path,required=True)
    a.add_argument('--demo-raw',type=Path,required=True)
    a.add_argument('--legacy-raw',type=Path,required=True)
    args=a.parse_args()
    import numpy as np
    import torch
    from f1_toys.rlt_online import sha,atomic_json
    from f1_toys.rlt_local_pipeline import prepare_local_cache
    from f1_toys.rlt_local_replay import read_prefix,terminal_suffixes
    from f1_toys.rlt_priority import effective_result
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import load_terminals,subset
    from f1_toys.rlt_action_contract import REFERENCE,action_joints
    from f1_toys.eef import solver,encode_actions,normalize_actions
    c=json.loads(args.config.read_text());cfg=c['f1'];out=args.output
    out.mkdir(parents=True,exist_ok=False);torch.set_num_threads(2)
    replay=dict(cfg['replay'],action_representation=REFERENCE)
    model=solver().model;stats=replay['action_norm_stats']
    sources=[];caches=[];terminal_rows={};control_maps={}
    def verify_cache(path):
        m=json.loads((path/'manifest.json').read_text())
        for name,digest in m['files_sha256'].items():assert sha(path/name)==digest,(path,name)
        return m
    def convert(data,rows,lookup):
        result={k:v for k,v in data.items()};result['actions']=torch.zeros_like(data['actions'])
        for i,row in enumerate(rows):
            controls,field=lookup(row)
            ticks=[controls[n] for n in row[field]]
            assert [r['submitted'] for r in ticks]==row['submitted16'],'Publication source changed'
            assert all(r.get('publication_complete') is True and r.get('stage')=='completed' for r in ticks)
            amended=dict(row,selected16=[r['selected'] for r in ticks],action_time_mask=data['action_time_mask'][i].tolist())
            values=action_joints(amended,REFERENCE)
            encoded=normalize_actions(encode_actions(model,row['state16'],values),stats)[...,:10]
            result['actions'][i,:len(values)]=torch.from_numpy(encoded)
        return result
    admissions=sorted(Path(cfg['health_root'],'admission').glob('*/actor.json'))
    for admission in admissions:
        accepted=json.loads(admission.read_text())
        if accepted.get('status')!='buffered':continue
        old=Path(accepted['cache']); assert sha(old/'manifest.json')==accepted['manifest_sha256']
        m=verify_cache(old);ep=m['episode_id'];raw=Path(cfg['raw_root'])/ep
        receipt=json.loads((old/'source-receipt.json').read_text());streams=read_prefix(raw,receipt)
        old_data=torch.load(old/'replay.pt',weights_only=True,map_location='cpu')
        rows=[json.loads(s) for s in (old/'transitions.jsonl').read_text().splitlines()]
        fmap={}
        for i,row in enumerate(rows):
            for key,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
                v={k:t[i].tolist() for k,t in old_data[part].items()}
                if row[key] in fmap:assert fmap[row[key]]==v
                fmap[row[key]]=v
        def provider(observation,payload,payload_root):return fmap[observation['observation_id']]
        dest=prepare_local_cache(raw,out/'local',replay,provider);new=torch.load(dest/'replay.pt',weights_only=True)
        for k,v in old_data.items():
            if k=='actions':continue
            if isinstance(v,dict):assert all(torch.equal(t,new[k][n]) for n,t in v.items()),k
            else:assert torch.equal(v,new[k]),k
        caches.append(str(dest));sources.append(dict(episode=ep,source_cache=str(old),source_manifest_sha256=accepted['manifest_sha256'],cache=str(dest),split=m['split'],count=len(rows)))
        control_maps[ep]={r['command_sequence']:r for r in streams['control']}
        review_path=Path(replay['reviews_root'])/(ep+'.json')
        review=json.loads(review_path.read_text()) if review_path.exists() else None
        result=effective_result(receipt,m['source_receipt_sha256'],review)
        for row in terminal_suffixes(streams,result):terminal_rows[(ep,row['decision_ns'],row['next_decision_ns'])]=(row,'command_sequences')
    demos,drows,dm=load_demo_seed(cfg['demo_seed'],replay['feature_identity'])
    receipts=json.loads(Path(cfg['demo_seed']['audit_root'],'source-receipts.json').read_text());demo_controls={}
    for name,receipt in receipts.items():
        path=args.demo_raw/name/'journal/control.jsonl'
        assert sha(path)==receipt['files_sha256']['journal/control.jsonl']
        demo_controls[name]={r['stream_sequence']:r for r in map(json.loads,path.read_text().splitlines())}
    demo_out=out/'demo';demo_out.mkdir()
    for split,data in demos.items():
        new=convert(data,drows[split],lambda row:(demo_controls[row['source_run']],'action_control_sequences'))
        torch.save(new,demo_out/(split+'.pt'))
    dmanifest=dict(dm,action_representation=REFERENCE,source_manifest_sha256=cfg['demo_seed']['manifest_sha256'],
        files_sha256={s+'.pt':sha(demo_out/(s+'.pt')) for s in demos})
    atomic_json(demo_out/'manifest.json',dmanifest)
    terminal_cfg=cfg['critic_terminal_cache'];terms,tm=load_terminals(terminal_cfg,replay['feature_identity'])
    # Traverse the already-bound legacy manifest chain, not an arbitrary dataset.
    source=Path(terminal_cfg)
    while True:
        mm=json.loads((source/'manifest.json').read_text())
        if mm.get('schema')=='f1_rlt_dagger_replay_v1':break
        nxt=Path(mm['source']);assert sha(nxt/'manifest.json')==mm['source_manifest_sha256'];source=nxt
    verify_cache(source)
    for row in map(json.loads,(source/'transitions.jsonl').read_text().splitlines()):
        if row.get('terminated') and row.get('segment_outcome')=='failure':
            terminal_rows[(row['source_run'],row['decision_ns'],row['next_decision_ns'])]=(row,'action_control_sequences')
    legacy_controls={};terminal_out=out/'terminals';terminal_out.mkdir();terminal_proofs=[]
    terminal_exclusions=[];new_provenance={}
    for split,data in terms.items():
        rows=[];fields={};indices=[];new_provenance[split]=[]
        for index,provenance in enumerate(tm['provenance'][split]):
            key=(provenance['episode'],provenance['decision_ns'],provenance['next_decision_ns'])
            if key not in terminal_rows:
                assert key[0] in control_maps, 'Unknown terminal source cannot be silently removed'
                assert any(k[0]==key[0] and k[2]==key[2] for k in terminal_rows), 'Terminal outcome changed'
                terminal_exclusions.append(dict(split=split,**provenance,reason='old_suffix_not_valid_under_current_causal_reader'))
                continue
            indices.append(index);new_provenance[split].append(provenance)
            row,field=terminal_rows[key];row=dict(row,source_run=key[0]);rows.append(row);fields[key]=field
            if field=='action_control_sequences' and key[0] not in legacy_controls:
                path=args.legacy_raw/key[0]/'journal/control.jsonl'
                legacy_controls[key[0]]={r['stream_sequence']:r for r in map(json.loads,path.read_text().splitlines())}
                terminal_proofs.append(dict(path=str(path),sha256=sha(path)))
        def lookup(row):
            field=fields[(row['source_run'],row['decision_ns'],row['next_decision_ns'])]
            return ((legacy_controls if field=='action_control_sequences' else control_maps)[row['source_run']],field)
        assert indices, 'No verified terminal anchors remain in split'
        torch.save(convert(subset(data,indices),rows,lookup),terminal_out/(split+'.pt'))
    atomic_json(terminal_out/'manifest.json',dict(tm,action_representation=REFERENCE,
        source_manifest_sha256=sha(Path(terminal_cfg)/'manifest.json'),source=terminal_cfg,
        migration_control_proofs=terminal_proofs,provenance=new_provenance,excluded_legacy_suffixes=terminal_exclusions,files_sha256={s+'.pt':sha(terminal_out/(s+'.pt')) for s in terms}))
    candidate=copy.deepcopy(c);f=candidate['f1'];f.update(replay=replay,cache_root=str(out/'local'),
        demo_seed=dict(cfg['demo_seed'],root=str(demo_out),manifest_sha256=sha(demo_out/'manifest.json')),
        critic_terminal_cache=str(terminal_out),seed_caches=caches,bootstrap_checkpoint=None,publish_updates=False)
    f['training']['action_representation']=REFERENCE
    atomic_json(out/'candidate-data-config.json',candidate)
    atomic_json(out/'migration.json',dict(schema='f1_reference_migration_v1',sources=sources,
        demo_counts={s:len(v['actions']) for s,v in demos.items()},terminal_counts={s:len(v) for s,v in new_provenance.items()},excluded_legacy_suffixes=terminal_exclusions,
        action_representation=REFERENCE,source_config_sha256=sha(args.config),raw_mutations=0,parameter_updates=0,
        production_changed=False,decision_anchor_migration=False))
    print(json.dumps(dict(output=str(out),local_episodes=len(sources),counts={s:sum(x['count'] for x in sources if x['split']==s) for s in ['train','validation']},production_changed=False)))

if __name__=='__main__':main()
