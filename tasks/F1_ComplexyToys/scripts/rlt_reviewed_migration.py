#!/usr/bin/env python3
"""Version expert supervision on hash-verified decision caches; no raw rewrites.

This is a supervision-only migration, not a claim to recompute frozen features
or repair a different action representation. The parent adapter remains recorded.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--exclude', action='append', default=[])
    args = p.parse_args()
    import torch
    from f1_toys.rlt_online import sha, atomic_json
    from f1_toys.rlt_supervision import approved_steps, CONTRACT
    from f1_toys.rlt_priority import effective_result
    from f1_toys.rlt_data_policy import load_policy, select_receipt
    from f1_toys.rlt_action_contract import DECISION
    cfg = json.loads(args.config.read_text()); c = cfg['f1']
    policy = load_policy(c['data_policy'], c['data_policy_sha256'])
    args.output.mkdir(parents=True, exist_ok=False)
    sources = []; excluded = []; by_episode = {}
    paths = [Path(v) for v in c['seed_caches']]
    paths += sorted(Path(c['cache_root']).glob('*/manifest.json'))
    for path in paths:
        root = path.parent if path.name == 'manifest.json' else path
        meta = json.loads((root/'manifest.json').read_text())
        episode = meta['episode_id']
        if episode in args.exclude:
            excluded.append(dict(episode=episode, cache=str(root), reason='explicit_label_uncertainty_quarantine'))
            continue
        if meta.get('action_representation') != DECISION:
            raise ValueError('Requires previously aligned decision actions')
        for name, digest in meta['files_sha256'].items():
            if Path(name).name != name or sha(root/name) != digest:
                raise ValueError('Source cache hash mismatch')
        receipt = json.loads((root/'source-receipt.json').read_text())
        split = select_receipt(policy, receipt, meta['source_receipt_sha256'])
        if split not in ('train', 'validation'):
            excluded.append(dict(episode=episode, cache=str(root), reason=split)); continue
        review_path = Path(c['replay']['reviews_root'])/(episode+'.json')
        review = json.loads(review_path.read_text()) if review_path.exists() else None
        if (sha(review_path) if review else None) != meta.get('review_sha256'):
            excluded.append(dict(episode=episode,cache=str(root),reason='review_revision_needs_full_rebuild')); continue
        outcome = effective_result(receipt, meta['source_receipt_sha256'], review)
        if outcome['status'] == 'failure' and (review is None or review.get('failure_confirmed') is not True):
            excluded.append(dict(episode=episode,cache=str(root),reason='failure_requires_explicit_confirmation')); continue
        if outcome['status'] not in ('success', 'failure', 'nonterminal_prefix'):
            excluded.append(dict(episode=episode,cache=str(root),reason='result_unknown')); continue
        if meta['split'] != split or meta['feature_identity'] != c['replay']['feature_identity']:
            raise ValueError('Split or frozen feature identity changed')
        digest = sha(root/'manifest.json')
        if episode in by_episode:
            if by_episode[episode] != digest:
                raise ValueError('Multiple active revisions require explicit selection: '+episode)
            continue
        by_episode[episode] = digest
        rows = [json.loads(line) for line in (root/'transitions.jsonl').read_text().splitlines()]
        data = torch.load(root/'replay.pt', map_location='cpu', weights_only=True)
        if len(rows) != len(data['actions']): raise ValueError('Transition count mismatch')
        for i, row in enumerate(rows):
            for key in ('action_time_mask','intervene_flags','terminated','reward'):
                if data[key][i].tolist() != row[key]: raise ValueError('Tensor/record mismatch: '+key)
            row['expert_approved'] = approved_steps(row, review)
        data['expert_approved'] = torch.tensor([r['expert_approved'] for r in rows], dtype=torch.bool)
        target = args.output/'local'/digest
        target.mkdir(parents=True)
        for name in meta['files_sha256']:
            if name not in ('replay.pt', 'transitions.jsonl'): shutil.copy2(root/name, target/name)
        torch.save(data, target/'replay.pt')
        (target/'transitions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        meta.update(supervision_contract=CONTRACT, supervision_migration=dict(
            parent_cache=str(root), parent_manifest_sha256=digest, script_sha256=sha(__file__),
            actions_and_features_recomputed=False, changed_fields=['expert_approved']))
        meta['files_sha256'] = {n:sha(target/n) for n in meta['files_sha256']}
        atomic_json(target/'manifest.json', meta)
        sources.append(dict(episode=episode, split=split, cache=str(target),
                            expert_steps=int(data['expert_approved'].sum()), transitions=len(rows)))
    c['seed_caches'] = [s['cache'] for s in sources]
    c.pop('bootstrap_checkpoint', None)
    atomic_json(args.output/'candidate-data-config.json', cfg)
    atomic_json(args.output/'migration.json', dict(schema='f1_reviewed_supervision_migration_v1',
        action_representation=DECISION, decision_anchor_migration=True,
        parent_config_sha256=sha(args.config), sources=sources, excluded=excluded,
        supervision_contract=CONTRACT, scope='offline clean comparison; online admission requires current adapter rebuild'))
    print(json.dumps(dict(sources=len(sources),transitions=sum(s['transitions'] for s in sources),excluded=excluded)))


if __name__ == '__main__': main()
