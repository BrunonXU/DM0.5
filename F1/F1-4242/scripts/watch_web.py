#!/usr/bin/env python3
"""Incremental web-only synchronization. No backend restart or robot command."""
import argparse,base64,hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT.parents[1]
REMOTE='/home/user/Bruno/F1/F1-4242/dev/web'
SSH=['ssh','-F',str(WORK/'F1/connect/robots/f1-09/client_ssh_config'),'F1-09','python3 -']

def remote(code):
    return subprocess.run(SSH,input=code,text=True,check=True,capture_output=True,timeout=60).stdout

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
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
                published=json.loads(remote(f"import json,pathlib\np=pathlib.Path({REMOTE!r}).parent/'sync.json'\nprint(json.dumps(json.loads(p.read_text()).get('files',{{}}) if p.exists() else {{}}))"))
            if hashes!=published:
                # One bounded transfer per file avoids encoding every mesh on each CSS edit.
                for name,digest in hashes.items():
                    if published.get(name)==digest:continue
                    raw=(ROOT/'web'/name).read_bytes()
                    payload=base64.b64encode(raw).decode()
                    code=f'''import base64,hashlib,os,pathlib
root=pathlib.Path({REMOTE!r});assert root.resolve()==root
p=root/{name!r};assert p.resolve().is_relative_to(root)
p.parent.mkdir(parents=True,exist_ok=True)
raw=base64.b64decode({payload!r});assert hashlib.sha256(raw).hexdigest()=={digest!r}
tmp=p.with_name('.'+p.name+'.sync');tmp.write_bytes(raw);os.replace(tmp,p)
'''
                    remote(code);changed[name]=digest
                # Keep old unreferenced assets for rollback. Publish revision after the batch.
                state={'updated_unix':time.time(),'files':hashes}
                remote(f'''import json,os,pathlib
p=pathlib.Path({REMOTE!r}).parent/'sync.json';p.parent.mkdir(parents=True,exist_ok=True)
tmp=p.with_name('.sync.tmp');tmp.write_text({json.dumps(state)!r}+'\\n');os.replace(tmp,p)
''')
                published=dict(hashes)
                print(time.strftime('%Y-%m-%d %H:%M:%S'),'web synced',len(changed),'changed files',flush=True)
            if args.once:return
        except Exception as exc:
            print('sync failed:',str(exc),flush=True)
            if args.once:raise
        time.sleep(2)
if __name__=='__main__':main()
