#!/usr/bin/env python3
"""Mirror only operator diagnostics from F1; no control or raw-data coupling."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True)
    p.add_argument('--ssh-config',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--once',action='store_true')
    args=p.parse_args()
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*',args.host):
        p.error('Expected a configured SSH alias')
    args.destination.mkdir(parents=True,exist_ok=True)
    ssh=['ssh','-F',str(args.ssh_config.resolve()),'-o','BatchMode=yes','-o','ConnectTimeout=5']
    command=['rsync','-rtz','--safe-links','--delay-updates','--timeout=10','-e',shlex.join(ssh),
             '--include=*/','--include=launcher.jsonl','--include=sender.log','--include=ssh.log',
             '--include=transport-ssh.log','--include=console.log','--include=sync.json','--exclude=*','--',
             args.host+':/home/user/Bruno/DAgger-runtime/operator-logs/',str(args.destination.resolve())+'/']
    last_success=None
    while True:
        started=time.time()
        try:
            run=subprocess.run(command,capture_output=True,text=True,timeout=25)
            error=run.stderr[-1500:] if run.returncode else None
            if run.returncode==0:last_success=time.time()
            status=dict(checked_unix=time.time(),last_success_unix=last_success,
                        returncode=run.returncode,error=error,elapsed_s=time.time()-started)
        except subprocess.TimeoutExpired:
            status=dict(checked_unix=time.time(),last_success_unix=last_success,error='mirror timeout')
        health=args.destination/'mirror-health.json';temporary=health.with_suffix('.partial')
        temporary.write_text(json.dumps(status)+'\n');temporary.replace(health)
        if args.once:
            print(json.dumps(status));return int(bool(status.get('error')))
        time.sleep(10)


if __name__=='__main__':
    raise SystemExit(main())
