#!/usr/bin/env python3
"""Real-data reconstruction and worker admission; no gradients or robot IO."""
import argparse
import functools
import json
from pathlib import Path
import sys
from types import SimpleNamespace

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--trained-data',type=Path,required=True)
    p.add_argument('--checkpoint',type=Path)
    a=p.parse_args()
    import torch
    torch.set_num_threads(2)
    from omegaconf import OmegaConf
    from f1_toys.rlt_online import atomic_json,sha
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import load_terminals
    from f1_toys.rlt_local_replay import read_prefix
    from f1_toys.rlt_local_pipeline import prepare_local_cache
    from f1_toys.rlt_prediction_index import source_predictions
    from f1_toys.rlt_data_policy import load_policy
    cfg=OmegaConf.load(a.data/'candidate-data-config.json')
    sys.path.insert(0,str(cfg.f1.core_root))
    from f1_toys.rlt_async_workers import F1ActorWorker
    migrated=json.loads((a.data/'migration.json').read_text())
    old=json.loads((a.trained_data/'migration.json').read_text())
    old={e['episode']:e for e in old['sources']}
    oldcfg=json.loads((a.trained_data/'candidate-data-config.json').read_text())['f1']
    def identical(left,right):
        assert set(left)==set(right)
        for key in left:
            if isinstance(left[key],dict):identical(left[key],right[key])
            else:assert torch.equal(left[key],right[key]),key
    demos,records,_=load_demo_seed(OmegaConf.to_container(cfg.f1.demo_seed),OmegaConf.to_container(cfg.f1.replay.feature_identity))
    terminal_data,terminal=load_terminals(cfg.f1.critic_terminal_cache,OmegaConf.to_container(cfg.f1.replay.feature_identity))
    w=SimpleNamespace(cfg=cfg,learner=SimpleNamespace(cfg=OmegaConf.to_container(cfg.f1.training),step=0),
        identity=dict(feature_identity=OmegaConf.to_container(cfg.f1.replay.feature_identity)),
        demo_records=records,critic_anchor_manifest=terminal,selection=load_policy(cfg.f1.data_policy),
        replay={},validation={},received_transitions=0,credited_episodes=set())
    w._admitted=functools.partial(F1ActorWorker._admitted,w)
    gaps=[];bundle_reports=[]
    for e in migrated['sources']:
        root=Path(e['cache'])
        identical(torch.load(root/'replay.pt',weights_only=True),torch.load(Path(old[e['episode']]['cache'])/'replay.pt',weights_only=True))
        F1ActorWorker._ingest(w,root)
        raw=Path(cfg.f1.raw_root)/e['episode']
        receipt=json.loads((raw/'local_segments/placement-1.json').read_text())
        streams=read_prefix(raw,receipt)
        rr=list(map(json.loads,(root/'transitions.jsonl').read_text().splitlines()))
        from f1_toys.rlt_priority import build_bundle, validate_bundle
        review=Path(cfg.f1.replay.reviews_root)/(e['episode']+'.json')
        bundle=build_bundle(raw,a.data/'priority-audit',review_path=review if review.exists() else None)
        bundle_meta=validate_bundle(bundle)
        needed={row[k] for row in rr for k in ('observation_id','next_observation_id')}
        assert needed<=set(bundle_meta['observation_ids'])
        source_data=torch.load(root/'replay.pt',weights_only=True);fm={}
        for i,row in enumerate(rr):
            for key,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
                fm[row[key]]={k:v[i].tolist() for k,v in source_data[part].items()}
        from f1_toys.rlt_decision_replay import prediction_key
        originals={prediction_key(p):p for p in map(json.loads,(root/'predictions.jsonl').read_text().splitlines())}
        packed=prepare_local_cache(bundle,a.data/'packed-replay',OmegaConf.to_container(cfg.f1.replay),
            lambda obs,*_:fm[obs['observation_id']],predictions=originals)
        identical(source_data,torch.load(packed/'replay.pt',weights_only=True))
        bundle_reports.append(dict(episode=e['episode'],images_complete=True,tensors_identical=True,
            selected_observations=len(bundle_meta['observation_ids']),original_observations=len(streams['observations']),
            bundle=str(bundle)))
        for row in rr:
            if row['decision_kind']!='actor_request':continue
            segment=[c for c in streams['control'] if row['execution_started_ns']<=c['monotonic_ns']<=row['execution_last_ns']]
            other=[c['source'] for c in segment if c['source']!='policy']
            gap=max((b['monotonic_ns']-c['monotonic_ns'] for c,b in zip(segment,segment[1:])),default=0)/1e6
            gaps.append(dict(episode=e['episode'],request_id=row['request_id'],max_gap_ms=gap,other_sources=other))
    terminal_permutations={}
    for split in ('train','validation'):
        for sub in ('demo','terminals'):
            newroot=Path(cfg.f1.demo_seed.root if sub=='demo' else cfg.f1.critic_terminal_cache)
            oldroot=Path(oldcfg['demo_seed']['root'] if sub=='demo' else oldcfg['critic_terminal_cache'])
            new=torch.load(newroot/(split+'.pt'),weights_only=True)
            previous=torch.load(oldroot/(split+'.pt'),weights_only=True)
            if sub=='terminals':
                from f1_toys.rlt_critic_calibration import subset
                oldmeta=json.loads((oldroot/'manifest.json').read_text())
                def key(r):return (r['episode'],r['segment'],r['decision_ns'],r['next_decision_ns'])
                lookup={key(r):i for i,r in enumerate(terminal['provenance'][split])}
                order=[lookup[key(r)] for r in oldmeta['provenance'][split]]
                assert len(order)==len(lookup)
                terminal_permutations[split]=order
                new=subset(new,torch.tensor(order))
            identical(new,previous)
    credited=w.received_transitions
    for e in migrated['sources']:F1ActorWorker._ingest(w,e['cache'])
    assert w.received_transitions==credited==sum(e['count'] for e in migrated['sources'] if e['split']=='train')
    # Rebuild the most recent episode through the live log lookup path. Cache
    # feature values are reused; original actor inputs still take precedence.
    e=max(migrated['sources'],key=lambda e:e['episode']);raw=Path(cfg.f1.raw_root)/e['episode']
    streams=read_prefix(raw,json.loads((raw/'local_segments/placement-1.json').read_text()))
    predictions=source_predictions(cfg.f1.prediction_log,[ev['plan'] for ev in streams['events']
        if ev['event']=='rlt_plan_adopted' and ev['plan']['phase']=='actor'])
    source=Path(e['cache']);data=torch.load(source/'replay.pt',weights_only=True)
    rows=list(map(json.loads,(source/'transitions.jsonl').read_text().splitlines()));features={}
    for i,row in enumerate(rows):
        for field,part in [('observation_id','obs'),('next_observation_id','next_obs')]:
            features[row[field]]={k:v[i].tolist() for k,v in data[part].items()}
    rebuilt=prepare_local_cache(raw,a.data/'admission-rebuild',OmegaConf.to_container(cfg.f1.replay),
        lambda obs,*_:features[obs['observation_id']],predictions=predictions)
    identical(torch.load(rebuilt/'replay.pt',weights_only=True),data)
    assert (rebuilt/'predictions.jsonl').is_file()
    F1ActorWorker._ingest(w,rebuilt);assert w.received_transitions==credited
    evaluation=None
    if a.checkpoint:
        import copy
        from f1_toys.rlt_online import LocalLearner
        w.learner=LocalLearner(cfg.f1.core_root,OmegaConf.to_container(cfg.f1.training),cfg.f1.initial_head)
        w.baseline=copy.deepcopy(w.learner.model).eval().requires_grad_(False)
        saved=torch.load(a.checkpoint/'learner.pt',weights_only=True)
        w.learner.restore(a.checkpoint,saved['identity'])
        w.demo_data=demos;w.demo_manifest={};w.incumbent=None;w.critic_anchors=terminal_data
        w.identity['demo_seed_manifest_sha256']=cfg.f1.demo_seed.manifest_sha256
        w.initial_manifest=json.loads((Path(cfg.f1.initial_head)/'manifest.json').read_text())
        evaluation=F1ActorWorker._evaluate(w)
        atomic_json(a.data/'worker-evaluation-report.json',dict(evaluation=evaluation,
            checkpoint_sha256=sha(a.checkpoint/'learner.pt'),parameter_updates=0,deployed=False))
    report=dict(passed=True,training_tensors_identical_by_source=True,terminal_row_permutations=terminal_permutations,training_episodes=len(w.replay),
        validation_episodes=len(w.validation),credited_transitions=credited,repeated_ingestion_credit=0,
        live_prediction_lookup=True,rebuilt_episode=e['episode'],rebuilt_cache=str(rebuilt),
        control_intervals=gaps,priority_bundles=bundle_reports,worker_evaluation_completed=evaluation is not None,
        parameter_updates=0,robot_commands=0,production_changed=False)
    atomic_json(a.data/'online-admission-report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='control_intervals'},indent=2))


if __name__=='__main__':main()
