#!/usr/bin/env python3
"""Receipt-by-receipt priority delivery with atomic H20 visibility.

Runs on 6000D. Upload each completed local bundle immediately, before pulling
the next one. Whole-recording archival is a separate existing process.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_priority import validate_bundle,digest


def remote_path(value):
    host,separator,path=value.partition(':')
    if (not separator or not re.fullmatch(r'[A-Za-z0-9_.@-]+',host)
            or host.startswith('-') or not path.startswith('/') or '..' in Path(path).parts):
        raise ValueError('Expected SSH alias:absolute dedicated directory')
    return host,path.rstrip('/')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True)
    p.add_argument('--ssh-config',type=Path,required=True)
    p.add_argument('--mirror',type=Path,required=True)
    p.add_argument('--destination',required=True)
    p.add_argument('--destination-src',required=True,help='Pinned H20 validator src directory')
    p.add_argument('--once',action='store_true')
    args=p.parse_args();source_host,source_root=remote_path(args.source);dest_host,dest_root=remote_path(args.destination)
    args.mirror.mkdir(parents=True,exist_ok=True)
    journal=args.mirror/'transfer-receipts.jsonl'
    completed={}
    if journal.exists():
        for line in journal.read_text().splitlines():
            row=json.loads(line);completed[(row['revision'],row['destination'])]=row['manifest_sha256']
    ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10']
    source_ssh=[*ssh,'-F',str(args.ssh_config.resolve())]
    errors={}
    while True:
        # Pull only tiny receipt metadata for discovery. No raw packs are read.
        discovery=subprocess.run(['rsync','-az','--protect-args','-e',shlex.join(source_ssh),
            '--include=*/','--include=placement-1.json','--exclude=*','--',args.source.rstrip('/')+'/',str(args.mirror.resolve())+'/'],capture_output=True,text=True)
        candidates=[]
        for receipt in args.mirror.glob('*/local_segments/placement-1.json'):
            revision=receipt.parent.parent.name
            if not re.fullmatch('[a-f0-9]{64}',revision):continue
            value=json.loads(receipt.read_text());candidates.append((value['episode_id'],revision))
        # First drain bundles already pulled by an earlier interrupted attempt.
        candidates.sort(key=lambda item:((args.mirror/item[1]/'learning-bundle.json').exists(),item[0]),reverse=True)
        for episode,revision in candidates:
            if (revision,args.destination) in completed:continue
            started=time.monotonic();root=args.mirror/revision
            try:
                pull=subprocess.run(['rsync','-az','--partial','--protect-args','-e',shlex.join(source_ssh),'--',
                    source_host+':'+source_root+'/'+revision+'/',str(root.resolve())+'/'],capture_output=True,text=True)
                if pull.returncode:raise RuntimeError('Source transfer: '+pull.stderr[-500:])
                manifest=validate_bundle(root);manifest_sha=digest((root/'learning-bundle.json').read_bytes())
                pulled=time.monotonic()
                # The worker ignores hidden incoming directories. Publication is
                # a remote atomic rename only after checksum verification.
                incoming=dest_root+'/.incoming-'+revision
                uploaded=subprocess.run(['rsync','-az','--partial','--protect-args','-e',shlex.join(ssh),'--',
                    str(root.resolve())+'/',dest_host+':'+incoming+'/'],capture_output=True,text=True)
                if uploaded.returncode:raise RuntimeError('Destination transfer: '+uploaded.stderr[-500:])
                code="""import sys,json,os,shutil
from pathlib import Path
sys.path.insert(0,SOURCE)
from f1_toys.rlt_priority import validate_bundle,digest
source=Path(INCOMING);target=Path(TARGET)
assert digest((source/'learning-bundle.json').read_bytes())==EXPECTED
validate_bundle(source)
if target.exists():
 validate_bundle(target)
 assert digest((target/'learning-bundle.json').read_bytes())==EXPECTED
 shutil.rmtree(source)
else:os.rename(source,target)
print(json.dumps({'committed':str(target)}))
""".replace('SOURCE',repr(args.destination_src)).replace('INCOMING',repr(incoming)).replace('TARGET',repr(dest_root+'/'+revision)).replace('EXPECTED',repr(manifest_sha))
                commit=subprocess.run([*ssh,dest_host,'python3 -'],input=code,capture_output=True,text=True)
                if commit.returncode:raise RuntimeError('Destination validation: '+commit.stderr[-500:])
                row=dict(episode_id=episode,revision=revision,destination=args.destination,
                    manifest_sha256=manifest_sha,source_to_mirror_seconds=pulled-started,
                    mirror_to_committed_seconds=time.monotonic()-pulled,total_seconds=time.monotonic()-started,
                    committed_unix=time.time(),transition_count=manifest['transition_count'])
                with journal.open('a') as handle:handle.write(json.dumps(row)+'\n');handle.flush()
                completed[(revision,args.destination)]=manifest_sha
                print(json.dumps(dict(status='committed',**row)),flush=True)
            except (OSError,ValueError,KeyError,RuntimeError) as exc:
                if errors.get(revision)!=str(exc):print(json.dumps(dict(status='retry',episode_id=episode,error=str(exc))),flush=True)
                errors[revision]=str(exc)
        if args.once:return int(discovery.returncode!=0 or any((r,args.destination) not in completed for _,r in candidates))
        time.sleep(2.)


if __name__=='__main__':raise SystemExit(main())
