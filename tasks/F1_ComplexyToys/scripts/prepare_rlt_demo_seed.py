#!/usr/bin/env python3
"""Materialize separately audited DAgger seed data for a local RLT experiment."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('audit', 'legacy-cache', 'config', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    args = p.parse_args()
    import numpy as np
    import torch
    from f1_toys.rlt_online import sha, atomic_json
    from f1_toys.rlt_local_pipeline import FrozenFeatureClient
    from f1_toys.eef import solver, encode_actions, normalize_actions
    torch.set_num_threads(2)
    cfg = json.loads(args.config.read_text())['f1']
    audit = json.loads((args.audit/'audit.json').read_text())
    old = json.loads((args.legacy_cache/'manifest.json').read_text())
    expected = cfg['replay']['feature_identity']['rlt']
    if (audit['objective'] != 'approach_place_release_v1'
            or old['parent_bundle_sha256'] != expected['base_model_version']
            or old['encoder_manifest_sha256'] != expected['encoder_manifest_sha256']
            or old['action_norm_stats'] != cfg['replay']['action_norm_stats']):
        raise ValueError('Frozen model/encoder/action/objective identity mismatch')
    for root, manifest in ((args.audit, audit), (args.legacy_cache, old)):
        for name, digest in manifest['files_sha256'].items():
            if sha(root/name) != digest:
                raise ValueError('Cache checksum mismatch: '+name)
    args.output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in (args.audit/'transitions.jsonl').read_text().splitlines()]
    previous = [json.loads(line) for line in (args.legacy_cache/'transitions.jsonl').read_text().splitlines()]
    legacy = torch.load(args.legacy_cache/'replay.pt', map_location='cpu', weights_only=True)
    feature_names = ('z_rl', 'proprio', 'ref_chunk')
    lookup = {}
    for i, row in enumerate(previous):
        for field, index in (('observation_id', 'current'), ('next_observation_id', 'next')):
            key = (row['source_run'], row[field])
            value = int(legacy[index][i])
            if key in lookup and lookup[key] != value:
                raise ValueError('Conflicting legacy observation mapping')
            lookup[key] = value
    wanted = sorted({(r['source_run'], r[k]) for r in rows for k in ('observation_id', 'next_observation_id')})
    client = FrozenFeatureClient(cfg['feature_endpoint'], cfg['replay']['feature_identity'])
    receipts = json.loads((args.audit/'source-receipts.json').read_text())
    raw_root = Path('/u01/user/datasets/f1_rlt_dagger_success_20261026/raw')
    features, raw, requested = {}, {}, 0
    for name, oid in wanted:
        key = (name, oid)
        if key in lookup:
            features[key] = {k: legacy[k][lookup[key]] for k in feature_names}
            continue
        if name not in raw:
            root = raw_root/name
            if sha(root/'journal/manifest.json') != receipts[name]['source_manifest_sha256']:
                raise ValueError('Remote raw identity mismatch')
            for file, digest in receipts[name]['files_sha256'].items():
                if sha(root/file) != digest:
                    raise ValueError('Remote raw checksum mismatch')
            raw[name] = (root,
                {r['observation_id']: r for r in map(json.loads, (root/'journal/observations.jsonl').read_text().splitlines())},
                {r['observation_id']: r for r in map(json.loads, (root/'payload/frames.jsonl').read_text().splitlines())})
        root, observations, frames = raw[name]
        result = client(observations[oid], frames[oid], root/'payload')
        features[key] = {k: torch.as_tensor(result[k], dtype=torch.float32) for k in feature_names}
        requested += 1
        print('new_frozen_feature', name, oid, flush=True)
    kin = solver().model
    actions = torch.tensor(np.stack([normalize_actions(encode_actions(kin, r['state16'], np.asarray(r['submitted16'])),
                                       old['action_norm_stats'])[..., :10] for r in rows]), dtype=torch.float32)
    data = dict(obs={k: torch.stack([features[(r['source_run'],r['observation_id'])][k] for r in rows]) for k in feature_names},
                next_obs={k: torch.stack([features[(r['source_run'],r['next_observation_id'])][k] for r in rows]) for k in feature_names},
                actions=actions, action_time_mask=torch.ones((len(rows),16),dtype=torch.bool),
                intervene_flags=torch.ones((len(rows),16),dtype=torch.bool),
                elapsed_steps=torch.full((len(rows),),16.),
                reward=torch.tensor([sum(r['rewards']) for r in rows],dtype=torch.float32),
                discount=torch.tensor([r['discount'] for r in rows],dtype=torch.float32),
                terminated=torch.tensor([r['terminated'] for r in rows],dtype=torch.bool),
                truncated=torch.tensor([r['truncated'] for r in rows],dtype=torch.bool))
    for split in ('train','validation'):
        mask=torch.tensor([r['split']==split for r in rows])
        part={k: {n:v[mask] for n,v in x.items()} if isinstance(x,dict) else x[mask] for k,x in data.items()}
        if not all(torch.isfinite(v).all() for x in part.values() for v in (x.values() if isinstance(x,dict) else (x,))):
            raise ValueError('Nonfinite prepared seed')
        torch.save(part,args.output/(split+'.pt'))
    atomic_json(args.output/'manifest.json',dict(schema='f1_rlt_reviewed_demo_seed_tensors_v1',
        objective=audit['objective'], capture_mode='DAgger', learning_use='RLT_demo_seed',
        audit_sha256=sha(args.audit/'audit.json'), legacy_manifest_sha256=sha(args.legacy_cache/'manifest.json'),
        feature_identity=cfg['replay']['feature_identity'],counts=audit['transition_counts'],
        cached_features=len(wanted)-requested,new_features=requested,robot_commands=0,parameter_updates=0,
        live_buffer_ingested=False, reference_mode='frozen_B1250_cache_reuse_plus_feature_only_service',
        files_sha256={s+'.pt':sha(args.output/(s+'.pt')) for s in ('train','validation')}))
    print(json.dumps(dict(counts=audit['transition_counts'],reused=len(wanted)-requested,new=requested)))


if __name__ == '__main__':
    main()
