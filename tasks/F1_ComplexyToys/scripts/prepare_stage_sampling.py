#!/usr/bin/env python3
"""Freeze label-derived training focus and episode-heldout diagnostic cases."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    for name, digest in manifest['files_sha256'].items():
        assert sha(root / name) == digest, name
    rows = [json.loads(line) for line in (root / 'samples.jsonl').open()]
    arrays = np.load(root / 'arrays.npz', allow_pickle=False)
    return manifest, rows, arrays


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    train, rows, arrays = read(a.dataset / 'train')
    val, vrows, varrays = read(a.dataset / 'validation')
    assert train['split'] == 'train' and val['split'] == 'validation'
    assert not {r['episode_id'] for r in rows} & {r['episode_id'] for r in vrows}
    for key in ('contract_sha256', 'split_sha256', 'norm_stats_sha256'):
        assert train[key] == val[key]
    grip = arrays['actions'][:, :, 9] * 50 + 50
    focus = (arrays['states'][:, 15] >= 90) & ((grip[:, :30] <= 10) & arrays['action_time_mask'][:, :30]).any(1)
    assert focus.any()
    pools = defaultdict(list)
    for i, row in enumerate(rows):
        pools[(row['dataset_role'], row['episode_id'])].append(i)
    support = []
    for (role, episode), ids in sorted(pools.items()):
        q = float(focus[ids].mean())
        support.append(dict(role=role, episode=episode, anchors=len(ids), focus=int(focus[ids].sum()),
                            probability_A=q, probability_B=4*q/(1+3*q)))
    exposure = {}
    for arm in ('A', 'B'):
        exposure[arm] = sum(weight * np.mean([r['probability_'+arm] for r in support if r['role']==role])
            for role, weight in [('original_demo', .75), ('correction', .25)])
    focus_doc = dict(schema='f1_release_focus_v1',
        dataset_manifest_sha256=sha(a.dataset / 'train/manifest.json'),
        criterion='observed right grip >=90 and a valid expert right grip <=10 at h=0..29; grip 0=open,100=closed',
        indices=np.flatnonzero(focus).tolist(), multiplier_B=4, support=support,
        expected_focus_fraction=exposure)
    (a.output / 'focus.json').write_text(json.dumps(focus_doc, indent=2)+'\n')

    masks = varrays['action_time_mask']
    lengths = masks.sum(1)
    vg = varrays['actions'][:, :, 9] * 50 + 50
    state = varrays['states'][:, 15]
    first_open = np.where((vg <= 10) & masks, np.arange(50), 50).min(1)
    first_closed = np.where((vg >= 90) & masks, np.arange(50), 50).min(1)
    episodes = sorted({r['episode_id'] for r in vrows})
    panel = []
    for episode in episodes:
        ids = [i for i, row in enumerate(vrows) if row['episode_id']==episode]
        groups = {
            'release_imminent': [i for i in ids if state[i]>=90 and first_open[i]<30 and lengths[i]>=30],
            'closed_no_release_h50': [i for i in ids if state[i]>=90 and first_open[i]==50 and lengths[i]==50],
            'grasp_imminent': [i for i in ids if state[i]<=10 and first_closed[i]<30 and lengths[i]>=30],
            'home_open': [i for i in ids if state[i]<=10 and vrows[i]['supervision_phase'] in ('home','native_home','terminal_hold')],
        }
        # Observational proxy: open after a closed observation within one second.
        last_closed = None
        post = []
        for i in sorted(ids, key=lambda j: vrows[j]['anchor_monotonic_ns']):
            t = vrows[i]['anchor_monotonic_ns']
            if state[i]>=90:
                last_closed = t
            if (state[i]<=10 and last_closed is not None and 0<t-last_closed<=1_000_000_000
                    and lengths[i]>=30 and np.all(vg[i, :30]<=10)):
                post.append(i)
        groups['post_release_open_proxy'] = post
        for group, candidates in groups.items():
            if group in ('release_imminent', 'grasp_imminent'):
                times = first_open if group=='release_imminent' else first_closed
                chosen = sorted({min(candidates, key=lambda i: (abs(int(times[i])-target), i))
                                 for target in (8,16,28)}) if candidates else []
            else:
                chosen = sorted({candidates[j] for j in np.linspace(0,len(candidates)-1,3,dtype=int)}) if candidates else []
            for i in chosen:
                panel.append(dict(index=i, group=group, episode_id=episode, role=vrows[i]['dataset_role'],
                    valid_steps=int(lengths[i]), first_expert_open=int(first_open[i]) if first_open[i]<lengths[i] else None,
                    first_expert_closed=int(first_closed[i]) if first_closed[i]<lengths[i] else None,
                    phase=vrows[i]['supervision_phase']))
    panel_doc = dict(schema='f1_stage_panel_v1', dataset_manifest_sha256=sha(a.dataset / 'validation/manifest.json'),
        seeds=[2026102201,2026102202,2026102203], denoise_steps=5, cases=panel,
        scope='Fixed existing heldout episodes, same object/layout; stage proxies, not independent closed-loop success. Some cases overlap across groups.')
    assert all(any(r['group']==g for r in panel) for g in ('release_imminent','closed_no_release_h50','grasp_imminent','home_open'))
    (a.output / 'panel.json').write_text(json.dumps(panel_doc, indent=2)+'\n')
    print(json.dumps(dict(train_anchors=len(rows), focus=int(focus.sum()), expected_focus_fraction=exposure,
                          validation_cases=len(panel), validation_episodes=len(episodes),
                          focus_sha256=sha(a.output/'focus.json'), panel_sha256=sha(a.output/'panel.json'))))


if __name__ == '__main__':
    main()
