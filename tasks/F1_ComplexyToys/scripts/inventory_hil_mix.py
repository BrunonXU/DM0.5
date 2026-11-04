"""Traceable sampling counts only; preserves frozen V1 and starts no training."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(baseline, additions):
    entries, identities, groups = [], {}, {}
    counts = {role: {s: dict(episodes=0, windows=0, valid_action_steps=0)
                     for s in ('train', 'validation')}
              for role in ('original_demo', 'new_demo', 'correction')}
    sources = [(baseline/s, 'original_demo', s) for s in ('train', 'validation')]
    sources += [(p, None, None) for p in additions]
    for root, role, split in sources:
        manifest_path = root/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('synthetic') or manifest.get('smoke_only'):
            raise ValueError('Synthetic/smoke fixtures cannot enter real data inventory')
        role = role or manifest.get('dataset_role')
        split = split or manifest.get('prospective_split')
        if role not in counts or split not in counts[role]:
            raise ValueError('Explicit dataset role and split required')
        if role != 'original_demo' and not manifest.get('human_correction_reviewed'):
            raise ValueError('New data must have reviewed supervision')
        if digest(root/'samples.jsonl') != manifest['files_sha256']['samples.jsonl']:
            raise ValueError('Sample manifest changed')
        rows = [json.loads(line) for line in (root/'samples.jsonl').read_text().splitlines()]
        by_episode = {}
        for row in rows:
            by_episode.setdefault(row['episode_id'], []).append(row)
        if len(rows) != manifest['samples']:
            raise ValueError('Window count mismatch')
        for source in manifest['source_episodes']:
            episode = source['episode']
            group = manifest.get('source_group') or source.get('source_group')
            if role != 'original_demo' and not group:
                raise ValueError('New data requires original session/scene source_group')
            group = group or episode  # Frozen V1 grouping is not retrospectively changed.
            for identity in (episode, source['manifest_sha256']):
                if identity in identities:
                    raise ValueError('Repeated episode/raw hash; merge review ranges before counting')
                identities[identity] = split
            if group in groups and groups[group] != split:
                raise ValueError('Original session/scene leaks across splits')
            groups[group] = split
            selected = by_episode.pop(episode, [])
            lengths = Counter(len(r['action_grid_ns']) for r in selected)
            phases = Counter(r.get('supervision_phase', 'legacy_unspecified') for r in selected)
            item = dict(episode=episode, source_group=group, dataset_role=role, split=split,
                        source_manifest_sha256=source['manifest_sha256'],
                        dataset_manifest_sha256=digest(manifest_path), dataset=str(root),
                        windows=len(selected), valid_action_steps=sum(k*v for k,v in lengths.items()),
                        valid_length_counts=dict(lengths), phase_counts=dict(phases))
            entries.append(item)
            target = counts[role][split]
            target['episodes'] += 1
            target['windows'] += item['windows']
            target['valid_action_steps'] += item['valid_action_steps']
        if by_episode:
            raise ValueError('Unlisted episode in samples')
    return dict(schema='f1_hil_mix_inventory_v1', counts=counts, entries=entries,
                training_started=False, mixing_ratio=None, next_initialization=None,
                formal_training_ready=False,
                note='Counts only. New groups stay together; preserve original V1 12/3. Refit future train-only statistics.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--addition', type=Path, action='append', default=[])
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = inventory(a.baseline, a.addition)
    with a.output.open('x') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write('\n')
