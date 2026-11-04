#!/usr/bin/env python3
"""Export reviewed legacy DAgger transitions without relabeling capture mode."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from f1_toys.rlt_human_replay import audit_human_raw, select_human_segment
from f1_toys.rlt_replay import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.recipe.read_text())
    inputs = json.loads(args.inputs.read_text())
    if cfg.get('objective') != 'approach_place_release_v1':
        raise ValueError('Expected reviewed local placement objective')
    args.output.mkdir(parents=True, exist_ok=False)
    records, audits, receipts, groups = [], [], {}, {}
    for segment in cfg['human_segments']:
        name = segment['source_run']
        if name in receipts:
            raise ValueError('Duplicate source episode')
        root, review = Path(inputs[name]['raw']), Path(inputs[name]['review'])
        if digest(review) != segment['review_sha256']:
            raise ValueError('Review digest mismatch: ' + name)
        for image in segment['terminal_evidence']['endpoint_images']:
            if digest(image['path']) != image['sha256']:
                raise ValueError('Endpoint image digest mismatch')
        streams, frames, receipt = audit_human_raw(root, review)
        if receipt['source_manifest_sha256'] != segment['source_manifest_sha256']:
            raise ValueError('Raw source digest mismatch: ' + name)
        group, split = receipt['source_group'], segment['split']
        if split not in ('train', 'validation') or groups.setdefault(group, split) != split:
            raise ValueError('Source group leaks across train/validation')
        selected, audit = select_human_segment(streams, frames, segment, cfg, receipt['review'])
        if not selected or audit['terminal_transitions'] != 1 or audit['success_rewards'] != 1:
            raise ValueError('Reviewed success has no unique valid terminal: ' + name)
        for row in selected:
            row.update(capture_mode='DAgger', learning_use='RLT_demo_seed',
                       objective=cfg['objective'], source_manifest_sha256=receipt['source_manifest_sha256'])
        records.extend(selected)
        audits.append(audit)
        receipts[name] = receipt
        print(name, split, len(selected), flush=True)
    transitions = args.output / 'transitions.jsonl'
    transitions.write_text(''.join(json.dumps(r) + '\n' for r in records))
    (args.output / 'source-receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    report = dict(schema='f1_rlt_local_demo_seed_audit_v1', objective=cfg['objective'],
                  recipe_sha256=digest(args.recipe), capture_mode='DAgger',
                  learning_use='RLT_demo_seed', parameter_updates=0, live_buffer_ingested=False,
                  transition_counts={s: sum(r['split'] == s for r in records) for s in ('train', 'validation')},
                  episodes=len(receipts), source_groups=groups, audits=audits,
                  files_sha256={p.name: digest(p) for p in (transitions, args.output/'source-receipts.json')})
    (args.output / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['transition_counts']))


if __name__ == '__main__':
    main()
