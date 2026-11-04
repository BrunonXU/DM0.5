#!/usr/bin/env python3
"""Same-noise heldout stage evaluation. No networking, robot I/O or deployment."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','panel','bundle','core','output'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    assert os.environ['CUDA_VISIBLE_DEVICES'] == '4'
    a.output.mkdir(parents=True, exist_ok=False)
    compat = TASK / 'scripts/h20_rtc_fast_server.py'
    exec(compile(compat.read_text().split('from f1_toys.server import main')[0],str(compat),'exec'), {'__file__':str(compat)})
    from f1_toys.dataset import F1Dataset
    from f1_toys.eef import physical_actions
    from f1_toys.inference import Policy
    from f1_toys.processing import sha256
    from f1_toys.rtc_optimization import compile_suffix
    from evaluate_eef import errors
    import torch
    panel = json.loads(a.panel.read_text())
    assert panel['dataset_manifest_sha256'] == sha256(a.dataset/'manifest.json')
    ds = F1Dataset(a.dataset)
    assert ds.manifest['split'] == 'validation'
    policy = Policy(a.bundle, a.core)
    compile_suffix(policy)
    ctx = dict(bootstrap=True, delay_steps=14, shift_steps=16, coverage_steps=32,
               committed_actions16=[], previous_absolute_eef10=[], plan_origin_ns=0, base_plan_id=0)
    records = []
    stream = (a.output/'results.jsonl').open('x')
    cache = {}
    for case in panel['cases']:
        i, L = case['index'], case['valid_steps']
        assert ds.rows[i]['episode_id'] == case['episode_id'] and int(ds.action_time_mask[i].sum()) == L
        images = ds.raw_images(i)
        for seed in panel['seeds']:
            key = (i,seed)
            if key not in cache:
                result, raw, _ = policy.predict_rtc(images, ds.states[i], ds.states[i], ctx,
                    seed=seed, guided=False, steps=panel['denoise_steps'])
                cache[key] = (result, raw)
            result, raw = cache[key]
            physical = physical_actions(raw, ds.stats['action'])
            grip = np.clip(physical[:L,9]*50+50,0,100)
            opens, closes = np.flatnonzero(grip<=10), np.flatnonzero(grip>=90)
            first = int(opens[0]) if len(opens) else None
            row = dict(**case, seed=seed, first_predicted_open=first,
                first_predicted_closed=int(closes[0]) if len(closes) else None,
                ik_success=bool(result['success']), valid_error=errors(physical,ds.actions[i],L),
                prefix16_error=errors(physical,ds.actions[i],min(16,L)),
                normalized_actions32=raw.tolist())
            if case['group']=='release_imminent':
                expert = case['first_expert_open']
                row.update(open_missing=first is None, early_open=first is not None and first<expert-3,
                    open_within_3_frames=first is not None and abs(first-expert)<=3,
                    open_timing_error_lower_bound_frames=(first if first is not None else L)-expert)
            if case['group']=='closed_no_release_h50': row['false_open'] = first is not None
            records.append(row)
            stream.write(json.dumps(row)+'\n');stream.flush()
        print(json.dumps(dict(case=i, group=case['group'], predictions=len(records))),flush=True)
    groups = defaultdict(list)
    for row in records: groups[row['group']].append(row)
    summary = {}
    for group, rows in groups.items():
        metrics = dict(cases=len(rows)//len(panel['seeds']), predictions=len(rows),
            ik_success_rate=float(np.mean([r['ik_success'] for r in rows])),
            valid_error={k:float(np.mean([r['valid_error'][k] for r in rows])) for k in rows[0]['valid_error']},
            prefix16_error={k:float(np.mean([r['prefix16_error'][k] for r in rows])) for k in rows[0]['prefix16_error']})
        for k in ('open_missing','early_open','open_within_3_frames','open_timing_error_lower_bound_frames','false_open'):
            if k in rows[0]: metrics[k] = float(np.mean([r[k] for r in rows]))
        summary[group] = metrics
    report = dict(bundle_id=policy.bundle_id, panel_sha256=sha256(a.panel), scope=panel['scope'],
        robot_commands=0, production_changes=0, parameter_grads_absent=all(p.grad is None for p in policy.model.parameters()),
        seeds=panel['seeds'], denoise_steps=panel['denoise_steps'], summary=summary)
    assert report['parameter_grads_absent']
    (a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__': main()
