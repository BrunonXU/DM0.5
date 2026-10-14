#!/usr/bin/env python3
"""Incremental web-only synchronization. No backend restart or robot command."""
import argparse,base64,hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DEFAULT_REMOTE='/home/user/Bruno/F1-infra/dev/web'

def remote(code, ssh):
    return subprocess.run(ssh,input=code,text=True,check=True,capture_output=True,timeout=60).stdout

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--host',required=True,help='Explicit SSH host or alias')
    parser.add_argument('--ssh-config',type=Path,help='Optional external SSH configuration')
    parser.add_argument('--remote',default=DEFAULT_REMOTE,help='Absolute remote web directory')
    args=parser.parse_args()
    if not args.host or args.host.startswith('-'):
        parser.error('--host must be an SSH host or alias')
    if not args.remote.startswith('/') or '..' in Path(args.remote).parts or len(Path(args.remote).parts)<4:
        parser.error('--remote must be a dedicated absolute web directory')
    ssh=['ssh']
    if args.ssh_config: ssh.extend(['-F',str(args.ssh_config.expanduser().resolve())])
    ssh.extend([args.host,'python3 -'])
    remote_path=args.remote
    cached={};published={}
    while True:
        try:
            hashes={};changed={}
            for path in sorted((ROOT/'web').rglob('*')):
                if not path.is_file() or path.is_symlink():continue
                name=path.relative_to(ROOT/'web').as_posix();stat=path.stat();stamp=(stat.st_mtime_ns,stat.st_size)
                if name not in cached or cached[name][0]!=stamp:
                    raw=path.read_bytes();cached[name]=(stamp,hashlib.sha256(raw).hexdigest())
                hashes[name]=cached[name][1]
            if not published:
                published=json.loads(remote(f"import json,pathlib\np=pathlib.Path({remote_path!r}).parent/'sync.json'\nprint(json.dumps(json.loads(p.read_text()).get('files',{{}}) if p.exists() else {{}}))",ssh))
            if hashes!=published:
                # One bounded transfer per file avoids encoding every mesh on each CSS edit.
                for name,digest in hashes.items():
                    if published.get(name)==digest:continue
                    raw=(ROOT/'web'/name).read_bytes()
                    payload=base64.b64encode(raw).decode()
                    code=f'''import base64,hashlib,os,pathlib
root=pathlib.Path({remote_path!r});assert root.resolve()==root
p=root/{name!r};assert p.resolve().is_relative_to(root)
p.parent.mkdir(parents=True,exist_ok=True)
raw=base64.b64decode({payload!r});assert hashlib.sha256(raw).hexdigest()=={digest!r}
tmp=p.with_name('.'+p.name+'.sync');tmp.write_bytes(raw);os.replace(tmp,p)
'''
                    remote(code,ssh);changed[name]=digest
                # Keep old unreferenced assets for rollback. Publish revision after the batch.
                state={'updated_unix':time.time(),'files':hashes}
                state_text=json.dumps(state)+'\n'
                remote(f'''import json,os,pathlib
p=pathlib.Path({remote_path!r}).parent/'sync.json';p.parent.mkdir(parents=True,exist_ok=True)
tmp=p.with_name('.sync.tmp');tmp.write_text({state_text!r});os.replace(tmp,p)
''',ssh)
                published=dict(hashes)
                print(time.strftime('%Y-%m-%d %H:%M:%S'),'web synced',len(changed),'changed files',flush=True)
            if args.once:return
        except Exception as exc:
            print('sync failed:',str(exc),flush=True)
            if args.once:raise
        time.sleep(2)
if __name__=='__main__':main()
