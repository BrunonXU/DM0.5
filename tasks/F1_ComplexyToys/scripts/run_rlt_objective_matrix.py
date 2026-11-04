#!/usr/bin/env python3
"""Bounded, W&B-monitored objective ablations; never publishes a snapshot."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--data', required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--steps', type=int, default=256)
p.add_argument('--run-prefix', required=True)
a=p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
variants = [
    ('prefix', []),
    ('tail', ['--bc-coverage', 'prefix_plus_reference_tail_v2']),
    ('full', ['--bc-coverage', 'full_reference_v2']),
    ('full-so3', ['--bc-coverage', 'full_reference_v2', '--project-critic-rotation']),
    ('full-so3-warm', ['--bc-coverage', 'full_reference_v2', '--project-critic-rotation',
                       '--critic-warmup', '128', '--q-ramp', '128']),
]
reports=[]
for name, flags in variants:
    command=[sys.executable, '-u', str(Path(__file__).with_name('rlt_reference_trial.py')),
             '--data', a.data, '--output', str(a.output/name), '--run-prefix', a.run_prefix+'-'+name,
             '--steps', str(a.steps), *flags]
    print('START', name, flush=True)
    with (a.output/(name+'.log')).open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    reports.append(dict(name=name, result=json.loads((a.output/name/'comparison.json').read_text())))
    (a.output/'matrix.json').write_text(json.dumps(reports, indent=2))
    print('DONE', name, flush=True)
