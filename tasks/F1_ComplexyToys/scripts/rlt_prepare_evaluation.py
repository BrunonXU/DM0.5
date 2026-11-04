#!/usr/bin/env python3
"""Build immutable per-attempt assemblies and a Windows single-window adapter.

Uses the already verified controller and its unique output owner. Does not
activate the default entry, start a process, or send an input packet.
"""
import argparse
import copy
import json
import hashlib
import shlex
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_task_evaluation import preregister
from inference_runtime import build


def prepare(catalog, health, output, batch_id, remote_root):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',batch_id):raise ValueError('Invalid batch identity')
    base,initial,candidate=health['health'],health['rlt/health'],health['rlt/candidates']['candidate']
    if candidate is None:raise ValueError('No fixed candidate available')
    identities=dict(b1250=base['model_version'],bc400=initial['model_version'],rlt=candidate['model_version'])
    protocol=preregister(batch_id,identities)
    output.mkdir(parents=True,exist_ok=False)
    (output/'protocol.json').write_text(json.dumps(protocol,indent=2)+'\n')
    for attempt in protocol['attempts']:
        c=copy.deepcopy(catalog);key=c['default_rlt_hil'];actor=candidate if attempt['strategy']=='rlt' else initial
        c['robot_release']=remote_root+'/'+attempt['attempt_id']
        c['release']=attempt['attempt_id']
        c['models'][key]['modes']['rlt-hil']['evaluation']=dict(schema='f1_local_evaluation_v1',
            batch_id=batch_id,attempt_id=attempt['attempt_id'],start_group=attempt['start_group'],
            strategy=attempt['strategy'],budget_seconds=30,base_version=base['model_version'],
            actor_version=actor['rlt']['actor_version'],
            actor_identity={k:actor[k] for k in ('model_id','model_version','rlt')})
        c['models'][key]['label']+=' · EVAL '+attempt['attempt_id']+' · '+attempt['strategy']
        assembly=output/attempt['attempt_id']
        build(c,assembly)
        entry='#!/usr/bin/env bash\nset -euo pipefail\nexec python3 '+shlex.quote(c['robot_release']+'/inference_runtime.py')+' "$@"\n'
        (assembly/'start_evaluation.sh').write_text(entry)
        manifest=json.loads((assembly/'assembly-manifest.json').read_text())
        manifest['files']['start_evaluation.sh']=hashlib.sha256(entry.encode()).hexdigest()
        (assembly/'assembly-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    # Load the installed, hash-verified operator package. A distinct entry path
    # selects a pre-registered assembly; sender, watchdog and logs are unchanged.
    helper='''import argparse, importlib.util, os, pathlib, sys
p=argparse.ArgumentParser(description='Fixed F1 evaluation: one episode per attempt; Y then Ctrl+C.')
p.add_argument('attempt',type=int,choices=range(1,13));p.add_argument('--robot-host');p.add_argument('--shadow',action='store_true')
args=p.parse_args();root=pathlib.Path.home()/'DAgger-F1'
sys.path.insert(0,str(root/'scripts'));from windows_f1_bootstrap import resolve_package
package=resolve_package(root);os.environ['DAGGER_CLIENT_ROOT']=str(root)
sys.path[:0]=[str(package/'scripts'),str(package/'src')]
import windows_f1_session as session
session.ROBOT_ENTRY=REMOTE+'/'+BATCH+f'-{args.attempt:02}'+'/start_evaluation.sh'
print('EVALUATION '+BATCH+f'-{args.attempt:02}'+': fixed weights, excluded from training. One episode only; Y then Ctrl+C.',flush=True)
sys.argv=[str(package/'scripts/windows_f1_session.py')]+(['--robot-host',args.robot_host] if args.robot_host else [])+(['--shadow'] if args.shadow else [])
raise SystemExit(session.main())
'''.replace('REMOTE',repr(remote_root)).replace('BATCH',repr(batch_id))
    (output/'f1_evaluate.py').write_text(helper)
    return protocol


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog',type=Path,required=True);p.add_argument('--health',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--batch-id',required=True)
    p.add_argument('--remote-root',required=True);a=p.parse_args()
    prepare(json.loads(a.catalog.read_text()),json.loads(a.health.read_text()),a.output,a.batch_id,a.remote_root)
