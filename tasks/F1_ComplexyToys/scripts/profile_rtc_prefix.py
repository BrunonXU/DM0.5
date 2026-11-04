"""Isolated H20 prefix experiment; fixed input/noise, no robot or deployment."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch

from f1_toys.inference import Policy
from f1_toys import rtc_optimization


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'bundle', 'core', 'compat', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.compat.read_text().split('from f1_toys.server import main')[0]
    exec(compile(source, str(args.compat), 'exec'), {'__file__': str(args.compat)})
    row = json.loads((args.inputs/'inputs.jsonl').read_text().splitlines()[0])
    replay = row['request']['prediction_replay']
    images = {k: Image.open(args.inputs/v).convert('RGB') for k, v in row['images'].items()}
    policy = Policy(args.bundle, args.core)
    rtc_optimization.compile_suffix(policy)
    context = dict(bootstrap=True, delay_steps=20, shift_steps=20, coverage_steps=42,
                   plan_origin_ns=0, base_plan_id=0, committed_actions16=[], previous_absolute_eef10=[])
    first, _, _ = policy.predict_rtc(images, replay['state16'], replay['last_submitted16'], context, seed=42)
    assert first['success']
    context.update(bootstrap=False, base_plan_id=1,
                   committed_actions16=first['actions'][20:40],
                   previous_absolute_eef10=first['absolute_eef10'][20:])
    original_prefix = policy.model.build_prefix_cache
    reference, report = None, {}
    for name in ('current', 'eager_prefix_graph'):
        print(json.dumps({'starting': name}), flush=True)
        try:
            from f1_toys.rtc_prefix_graph import EagerPrefixGraph
            selected = original_prefix if name == 'current' else EagerPrefixGraph(original_prefix)
            events = []

            def measured_prefix(*a, **kw):
                start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                start.record()
                value = selected(*a, **kw)
                end.record()
                events.append((start, end))
                return value

            policy.model.build_prefix_cache = measured_prefix
            rows = []
            errors = []
            for i in range(30):
                result, raw, _ = policy.predict_rtc(images, replay['state16'],
                    replay['last_submitted16'], context, seed=42)
                if reference is None:
                    reference = raw.copy()
                errors.append(float(np.max(abs(raw-reference))))
                assert np.array_equal(result['actions'][:20], context['committed_actions16'])
                if i >= 10:
                    rows.append(dict(**result['timing_ms'], prefix_gpu_ms=events[-1][0].elapsed_time(events[-1][1])))
            report[name] = {key: dict(zip(('p50', 'p95', 'max'),
                np.percentile([r[key] for r in rows], [50, 95, 100]).tolist())) for key in rows[0]}
            report[name]['max_raw_error'] = max(errors)
        except Exception as exc:
            report[name] = {'error': repr(exc)}
        (args.output/'results.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({name: report[name]}), flush=True)


if __name__ == '__main__':
    main()
