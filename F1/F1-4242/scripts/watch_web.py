#!/usr/bin/env python3
"""6000D → F1 dev web only, atomic file writes; no backend restart or motion."""
import argparse,base64,hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT.parents[1]
REMOTE='/home/user/Bruno/F1/console/dev/web'
def sync():
    files={str(p.relative_to(ROOT/'web')):base64.b64encode(p.read_bytes()).decode() for p in sorted((ROOT/'web').rglob('*')) if p.is_file()}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    return files,digest
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');a=parser.parse_args();previous=None
    while True:
        try:
            files,digest=sync()
            if digest!=previous:
                code='''import base64,json,os,pathlib,time,hashlib
root=pathlib.Path('/home/user/Bruno/F1/console/dev/web')
assert root.resolve()==root
root.mkdir(parents=True,exist_ok=True)
files=json.loads(PAYLOAD)
for name,value in files.items():
 p=root/name;assert p.resolve().is_relative_to(root)
 p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_name('.'+p.name+'.sync');tmp.write_bytes(base64.b64decode(value));os.replace(tmp,p)
(root.parent/'sync.json').write_text(json.dumps({'updated_unix':time.time(),'files':{n:hashlib.sha256(base64.b64decode(v)).hexdigest() for n,v in files.items()}})+'\\n')
'''.replace('PAYLOAD',repr(json.dumps(files)))
                subprocess.run(['ssh','-F',str(WORK/'F1/connect/robots/f1-09/client_ssh_config'),'F1-09','python3 -'],input=code,text=True,check=True,timeout=20)
                previous=digest;print(time.strftime('%Y-%m-%d %H:%M:%S'), 'web synced',digest[:16],flush=True)
            if a.once:return
        except Exception as exc:
            print('sync failed:',str(exc),flush=True)
            if a.once:raise
        time.sleep(2)
if __name__=='__main__':main()
