#!/usr/bin/env python3
"""Make a small diagnostic subset only from release-chain-audited anchors.

The original package, labels and statistics remain immutable. Each event supplies
two distinguishable hold, one pre-release and one post-release observation, without joining
time intervals or changing any action. This is a learning-chain test, not a new
formal training recipe or an insertion-success annotation.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import numpy as np


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def choose_event(event, rows, arrays, stats):
    ids = [r['index'] for r in event['neighborhood']]
    assert rows[event['index']] == event['source_sample']
    assert all(rows[i]['episode_id'] == event['episode'] for i in ids)
    masks = arrays['action_time_mask']
    L = masks.sum(1)
    grip = arrays['actions'][:, :, 9] * 50 + 50
    state = arrays['states'][:, 15]
    first = np.where((grip <= 10) & masks, np.arange(50), 50).min(1)
    event_ns = event['release_publish_ns']
    low, high = np.asarray(stats['state']['low']), np.asarray(stats['state']['high'])
    span = np.where(high-low < 1e-4, 1., high-low)
    scaled = np.clip(2*(arrays['states']-(low+high)/2)/span, -1, 1).astype(np.float32)
    bins = (np.digitize(scaled, np.linspace(-1, 1, 257)[:-1])-1)[:, [7,8,9,10,11,12,13,15]]
    different_bins = (bins != bins[event['index']]).sum(1)
    groups = {
        'closed_no_release_h50': ([i for i in ids if state[i] >= 90 and L[i] == 50 and first[i] == 50 and different_bins[i] >= 2],
                                  (-2.4, -2.2)),
        'release_imminent': ([event['index']], (8,)),
        'post_release_open_proxy': ([i for i in ids if state[i] <= 10 and L[i] >= 16
                                    and np.all(grip[i, :L[i]] <= 10)
                                    and 0 < rows[i]['anchor_monotonic_ns'] - event_ns < 800_000_000],
                                   (0.1,)),
    }
    result, used = [], set()
    for group, (pool, targets) in groups.items():
        for target in targets:
            available = [i for i in pool if i not in used]
            if not available:
                raise ValueError(f"Insufficient audited {group} anchors for {event['episode']}")
            def distance(i):
                value = int(first[i]) if group == 'release_imminent' else (rows[i]['anchor_monotonic_ns'] - event_ns) / 1e9
                return abs(value - target), i
            i = min(available, key=distance)
            used.add(i)
            result.append(dict(source_index=i, group=group, episode_id=event['episode'],
                event_id=f"{event['split']}:{event['episode']}:{event_ns}",
                source_group=event['source_group'], role=rows[i]['dataset_role'],
                valid_steps=int(L[i]), first_expert_open=int(first[i]) if first[i] < L[i] else None,
                release_relative_ms=(rows[i]['anchor_monotonic_ns']-event_ns)/1e6,
                state_bins_different_from_release=int(different_bins[i]),
                panel_role='basic_fit', phase=rows[i]['supervision_phase']))
    assert len(result) == len(used) == 4
    probes = []
    pools = {
        'closed_no_release_h50': [i for i in ids if state[i]>=90 and L[i]==50 and first[i]==50 and i not in used],
        'release_imminent': [i for i in ids if state[i]>=90 and L[i]>=30 and 0<first[i]<30 and i not in used],
    }
    for group, targets in [('closed_no_release_h50', [None]), ('release_imminent', [16,28])]:
        for target in targets:
            candidates = [i for i in pools[group] if i not in used]
            assert candidates
            key = (lambda i:(int(different_bins[i]), abs(rows[i]['anchor_monotonic_ns']-event_ns), i)) if target is None else (lambda i:(abs(int(first[i])-target),i))
            i = min(candidates,key=key); used.add(i)
            probes.append(dict(source_index=i, group=group, episode_id=event['episode'],
                event_id=f"{event['split']}:{event['episode']}:{event_ns}", source_group=event['source_group'],
                role=rows[i]['dataset_role'], valid_steps=int(L[i]), first_expert_open=int(first[i]) if first[i]<L[i] else None,
                release_relative_ms=(rows[i]['anchor_monotonic_ns']-event_ns)/1e6,
                state_bins_different_from_release=int(different_bins[i]), panel_role='timing_ambiguity_probe',
                phase=rows[i]['supervision_phase']))
    return result, probes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--chain-audit', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    audit = json.loads(a.chain_audit.read_text())
    assert audit['schema'] == 'f1_release_chain_audit_v1' and audit['case_count'] == 8
    assert len([e for e in audit['events'] if e['split'] == 'train']) == 4
    a.output.mkdir(parents=True, exist_ok=False)
    protocol = dict(schema='f1_release_fit_protocol_v1', diagnostic_only=True,
        chain_audit_sha256=sha(a.chain_audit), parent='new_DAgger_1000',
        initialization='parent weights only; new Adam, LR schedule and dataloader cursor',
        image_augmentation=False, phase_counts_per_event={'hold': 2, 'release': 1, 'post_release': 1},
        seeds=[2026102301, 2026102302, 2026102303], denoise_steps=5,
        scope='Expert release command timing, not visual proof of insertion; same-layout event-heldout diagnostic; no deployment',
        datasets={})
    sources, split_manifest = {}, {}
    for split in ('train', 'validation'):
        root = a.dataset / split
        manifest = json.loads((root / 'manifest.json').read_text())
        # Verify every accessed metadata/label asset before deriving a new subset.
        for name in ('samples.jsonl', 'arrays.npz', 'contract.json', 'norm_stats.json'):
            assert sha(root/name) == manifest['files_sha256'][name], name
        rows = [json.loads(line) for line in (root/'samples.jsonl').open()]
        with np.load(root/'arrays.npz', allow_pickle=False) as data:
            arrays = {k: data[k] for k in data.files}
        cases, probes = [], []
        stats = json.loads((root/'norm_stats.json').read_text())
        for event in audit['events']:
            if event['split'] == split:
                selected, extra = choose_event(event, rows, arrays, stats)
                cases.extend(selected); probes.extend(extra)
        ids = [c['source_index'] for c in cases]
        assert len(ids) == len(set(ids)) == 16
        episodes = {c['episode_id'] for c in cases}
        source_groups = {c['source_group'] for c in cases}
        sources[split] = dict(root=root, manifest=manifest, rows=rows, arrays=arrays, cases=cases, probes=probes)
        split_manifest[split] = dict(episodes=sorted(episodes), source_groups=sorted(source_groups), indices=ids,
                                    parent_manifest_sha256=sha(root/'manifest.json'))
    assert not set(split_manifest['train']['episodes']) & set(split_manifest['validation']['episodes'])
    assert not set(split_manifest['train']['source_groups']) & set(split_manifest['validation']['source_groups'])
    dump(a.output/'split.json', split_manifest)
    for split, source in sources.items():
        root, manifest, rows, arrays, cases = (source[k] for k in ('root', 'manifest', 'rows', 'arrays', 'cases'))
        dest = a.output/split
        dest.mkdir()
        ids = [c['source_index'] for c in cases]
        selected_rows = []
        for new_index, case in enumerate(cases):
            row = copy.deepcopy(rows[case['source_index']])
            case['index'] = new_index
            row['diagnostic_case'] = copy.deepcopy(case)
            selected_rows.append(row)
            for name in row['images'].values():
                src, target = root/name, dest/name
                assert sha(src) == manifest['files_sha256'][name], name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(src, target)
                    except OSError:
                        shutil.copyfile(src, target)
        np.savez_compressed(dest/'arrays.npz', **{k: v[ids] for k, v in arrays.items()})
        (dest/'samples.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in selected_rows))
        contract = json.loads((root/'contract.json').read_text())
        contract['train_image_augmentation'] = False
        dump(dest/'contract.json', contract)
        shutil.copyfile(root/'norm_stats.json', dest/'norm_stats.json')
        dump(dest/'eval_indices.json', list(range(len(ids))))
        report = copy.deepcopy(manifest)
        report.pop('sampling', None)
        report.update(diagnostic_only=True, formal_training_ready=False, samples=len(ids), windows=len(ids),
            source_episodes=[r for r in manifest['source_episodes'] if r['episode'] in split_manifest[split]['episodes']],
            dataset_role_counts=dict(Counter(r['dataset_role'] for r in selected_rows)),
            phase_counts=dict(Counter(r['supervision_phase'] for r in selected_rows)),
            partial_windows=int((~arrays['action_time_mask'][ids].all(1)).sum()),
            valid_action_steps=int(arrays['action_time_mask'][ids].sum()),
            diagnostic_provenance=dict(parent_manifest_sha256=split_manifest[split]['parent_manifest_sha256'],
                selected_indices=ids, chain_audit_sha256=sha(a.chain_audit), labels_unchanged=True,
                statistics='Reused parent train-only statistics, not refitted on subset or validation'),
            contract_sha256=sha(dest/'contract.json'), norm_stats_sha256=sha(dest/'norm_stats.json'),
            split_sha256=sha(a.output/'split.json'),
            files_sha256={str(f.relative_to(dest)): sha(f) for f in dest.rglob('*') if f.is_file()},
            data_validation='Audited observation/command provenance; selected arrays and image bytes unchanged; augmentation disabled for memorization diagnostic')
        dump(dest/'manifest.json', report)
        protocol['datasets'][split] = dict(dataset_manifest_sha256=sha(dest/'manifest.json'),
            source_dataset_manifest_sha256=sha(root/'manifest.json'), cases=cases, probes=source['probes'])
    assert sources['train']['manifest']['norm_stats_sha256'] == sources['validation']['manifest']['norm_stats_sha256']
    dump(a.output/'protocol.json', protocol)
    print(json.dumps(dict(result='PASS', train=16, validation=16, audited_events=8,
        train_groups=dict(Counter(c['group'] for c in protocol['datasets']['train']['cases'])),
        validation_groups=dict(Counter(c['group'] for c in protocol['datasets']['validation']['cases'])),
        protocol_sha256=sha(a.output/'protocol.json'))))


if __name__ == '__main__':
    main()
