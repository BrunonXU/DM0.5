#!/usr/bin/env python3
"""Rebuild causal replay from raw journals, reusing only source-bound features."""
import argparse
import copy
import json
from pathlib import Path
import sys
TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    import torch
    from f1_toys.rlt_online import sha, atomic_json
    from f1_toys.rlt_local_pipeline import prepare_local_cache, FrozenFeatureClient
    from f1_toys.rlt_decision_replay import prediction_key
    cfg = json.loads((args.data/'candidate-data-config.json').read_text())
    migration = json.loads((args.data/'migration.json').read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    c = cfg['f1']; recipe = copy.deepcopy(c['replay'])
    policy = json.loads(Path(c['data_policy']).read_text())
    for excluded in migration['excluded']:
        old = json.loads((Path(excluded['cache'])/'manifest.json').read_text())
        policy['episodes'][excluded['episode']] = dict(split='exclude',
            source_receipt_sha256=old['source_receipt_sha256'], reason=excluded['reason'])
    atomic_json(args.output/'data-policy.json', policy)
    c['data_policy'] = recipe['data_policy'] = str(args.output/'data-policy.json')
    c['data_policy_sha256'] = recipe['data_policy_sha256'] = sha(args.output/'data-policy.json')
    sources = []
    for entry in migration['sources']:
        old = Path(entry['cache']); meta = json.loads((old/'manifest.json').read_text())
        rows = [json.loads(s) for s in (old/'transitions.jsonl').read_text().splitlines()]
        data = torch.load(old/'replay.pt', weights_only=True)
        features = {}
        for i, row in enumerate(rows):
            for field, column in [('observation_id','obs'),('next_observation_id','next_obs')]:
                feature = {k:data[column][k][i].tolist() for k in ('z_rl','proprio','ref_chunk')}
                if row[field] in features and features[row[field]] != feature:
                    raise ValueError('Same source observation has different frozen features')
                features[row[field]] = feature
        roots = [Path(c[k])/entry['episode'] for k in ('raw_root','priority_root') if c.get(k)]
        run = next((r for r in roots if (r/'local_segments/placement-1.json').exists()
                    and sha(r/'local_segments/placement-1.json') == meta['source_receipt_sha256']), None)
        if run is None: raise ValueError('No immutable matching raw source: '+entry['episode'])
        predictions = {prediction_key(v):v for v in map(json.loads,(old/'predictions.jsonl').read_text().splitlines())}
        remote = FrozenFeatureClient(c['feature_endpoint'], recipe['feature_identity'])
        def provider(observation, payload, root):
            return features.get(observation['observation_id']) or remote(observation, payload, root)
        rebuilt = prepare_local_cache(run, args.output/'local', recipe, provider, predictions=predictions)
        new = torch.load(rebuilt/'replay.pt', weights_only=True)
        for key in ('actions','action_time_mask','intervene_flags','reward','terminated','discount','execution_fraction'):
            torch.testing.assert_close(new[key], data[key], rtol=0, atol=0)
        for name in ('obs','next_obs'):
            for key in data[name]: torch.testing.assert_close(new[name][key],data[name][key],rtol=0,atol=0)
        sources.append(dict(entry, cache=str(rebuilt), raw_source=str(run),
                            exact_action_and_feature_rebuild=True, parent_manifest_sha256=sha(old/'manifest.json')))
        print(json.dumps(sources[-1]), flush=True)
    c.update(replay=recipe, cache_root=str(args.output/'local'),seed_caches=[s['cache'] for s in sources])
    atomic_json(args.output/'candidate-data-config.json', cfg)
    atomic_json(args.output/'migration.json', dict(migration, sources=sources,
        full_raw_alignment_rebuild=True, parent_migration_sha256=sha(args.data/'migration.json')))


if __name__ == '__main__': main()
