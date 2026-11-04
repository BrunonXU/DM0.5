#!/usr/bin/env python3
"""Matched 1024-step confirmation with isolated conservative-sampling RNG."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--data',required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--run-prefix',required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
reports=[]
for name,flags in [
    ('prefix',['--variant','q']),
    ('tail',['--variant','q','--bc-coverage','prefix_plus_reference_tail_v2']),
    ('conservative',['--bc-coverage','prefix_plus_reference_tail_v2','--conservative-q-weight','0.1']),
]:
    print('START',name,flush=True)
    with (a.output/(name+'.log')).open('w') as log:
        subprocess.run([sys.executable,'-u',str(Path(__file__).with_name('rlt_reference_trial.py')),
            '--data',a.data,'--output',str(a.output/name),'--steps','1024',
            '--run-prefix',a.run_prefix+'-'+name,*flags],stdout=log,stderr=subprocess.STDOUT,check=True)
    reports.append(dict(name=name,result=json.loads((a.output/name/'comparison.json').read_text())))
    (a.output/'report.json').write_text(json.dumps(reports,indent=2))
    print('DONE',name,flush=True)
