#!/usr/bin/env python3
"""Verify reconstructed decisions and episode isolation before any update."""
import argparse
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    a=p.parse_args()
    import torch
    torch.set_num_threads(2)
    from f1_toys.rlt_online import sha,atomic_json
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import load_terminals
    from f1_toys.rlt_action_contract import DECISION
    cfg=json.loads((a.data/'candidate-data-config.json').read_text())['f1']
    migration=json.loads((a.data/'migration.json').read_text())
    assert migration['action_representation']==DECISION and migration['decision_anchor_migration']
    index=a.data/'server-predictions.jsonl'
    originals={}
    if index.exists():
        assert sha(index)==migration['server_prediction_index_sha256']
        originals={r['_source_line_sha256']:r for r in map(json.loads,index.read_text().splitlines())}
    groups={s:set() for s in ('train','validation')};reports=[]
    def check(v):
        mask=v['action_time_mask'];f=v['execution_fraction']
        assert mask[:,0].all() and not (mask[:,1:] & ~mask[:,:-1]).any()
        assert not (f[~mask]!=0).any() and ((f[mask]>0)&(f[mask]<=1)).all()
        assert (v['actions'][~mask]==0).all()
        assert (v['reward'][~v['terminated']]==0).all()
        assert ((v['reward']==0)|(v['reward']==1)).all()
        assert (v['elapsed_steps']>0).all()
        assert torch.allclose(v['discount'],cfg['replay']['gamma_per_step']**v['elapsed_steps'],atol=1e-6)
        assert not (v['terminated'] & v['truncated']).any()
        for value in v.values():
            for t in value.values() if isinstance(value,dict) else [value]:assert torch.isfinite(t).all()
    for entry in migration['sources']:
        root=Path(entry['cache']);meta=json.loads((root/'manifest.json').read_text())
        assert meta['action_representation']==DECISION
        for name,digest in meta['files_sha256'].items():assert sha(root/name)==digest
        if (root/'predictions.jsonl').exists():
            assert 'predictions.jsonl' in meta['files_sha256']
            originals.update({r['_source_line_sha256']:r for r in map(json.loads,(root/'predictions.jsonl').read_text().splitlines())})
        v=torch.load(root/'replay.pt',weights_only=True,map_location='cpu');check(v)
        rows=list(map(json.loads,(root/'transitions.jsonl').read_text().splitlines()))
        assert len(rows)==len(v['actions'])==entry['count'] and int(v['terminated'].sum())==1
        assert rows[-1]['terminated'] and all(r['next_decision_ns']>r['decision_ns'] for r in rows)
        for i,r in enumerate(rows):
            if r['decision_kind']!='actor_request':continue
            pred=originals[r['server_prediction_sha256']];rep=pred['prediction_replay']
            count=sum(r['action_time_mask'])
            assert torch.equal(v['actions'][i,:count],torch.tensor(rep['actor_normalized_actions32'],dtype=torch.float32)[:count,:10])
            for key in ('z_rl','proprio'):assert torch.equal(v['obs'][key][i],torch.tensor(rep[key],dtype=torch.float32))
            assert torch.equal(v['obs']['ref_chunk'][i],torch.tensor(rep['reference_normalized_actions32'],dtype=torch.float32)[:,:10])
            assert r['execution_last_ns']<=r['endpoint_capture_ns']<=r['next_decision_ns']
            assert len(r['command_sequences'])==len(set(r['command_sequences']))
        groups[entry['split']].add(entry['episode'])
        reports.append(dict(episode=entry['episode'],split=entry['split'],transitions=len(rows),
            actor_decisions=sum(r['decision_kind']=='actor_request' for r in rows),
            human_windows=sum(r['decision_kind']=='human_future_targets' for r in rows),
            partial_prefixes=int((~v['action_time_mask'][:,-1] | (v['execution_fraction'][:,-1]<1)).sum()),
            success=int(v['reward'].sum()),terminal_count=int(v['terminated'].sum())))
    demo,records,dm=load_demo_seed(cfg['demo_seed'],cfg['replay']['feature_identity'])
    terms,tm=load_terminals(cfg['critic_terminal_cache'],cfg['replay']['feature_identity'])
    for split in groups:
        check(demo[split]);check(terms[split])
        groups[split].update(r['source_run'] for r in records[split])
        groups[split].update(r['episode'] for r in tm['provenance'][split])
    assert not groups['train'] & groups['validation']
    report=dict(passed=True,action_representation=DECISION,episodes=reports,
        counts={s:dict(local=sum(r['transitions'] for r in reports if r['split']==s),demo=len(demo[s]['actions']),
                      critic_anchors=len(terms[s]['actions'])) for s in groups},
        train_episodes=sorted(groups['train']),validation_episodes=sorted(groups['validation']),
        validation_scope='repeated development holdout; not a new blind robot success test',
        exact_original_actor_inputs_outputs=True,raw_mutations=0,robot_commands=0,parameter_updates=0,
        migration_sha256=sha(a.data/'migration.json'))
    atomic_json(a.data/'integrity-report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='episodes'},indent=2))


if __name__=='__main__':main()
