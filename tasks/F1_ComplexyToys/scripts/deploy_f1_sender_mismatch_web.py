"""Publish unified episode instructions and existing desktop status views."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

TASK=Path(__file__).resolve().parents[1]
ROOT=TASK.parents[1]
OUT=TASK/'artifacts/20261021-episode-sender-mismatch'
REMOTE=r'''
import fcntl,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin);b=Path('/home/user/Bruno/F1/F1-4242');old=(b/'current').resolve()
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
get=lambda path:json.load(urlopen('http://127.0.0.1:4242'+path,timeout=10))
assert Path('/etc/machine-id').read_text().strip()=='00000000000000000000000000000000'
for name,entry in p['files'].items():
 source=old/name if name.endswith('.py') else b/'dev/web'/name
 assert sha(source)==entry['before'],name+' concurrently changed'
s=get('/api/state');assert not s['control'].get('owner') and not s['control'].get('stopping')
assert (s.get('action') or {}).get('status') not in ('accepted','queued','running','stopping')
for route in ('play','demonstration','policy'):assert not get('/api/hil/state?route='+route)['hil']['active']
assert not get('/api/teleop/state')['teleop'].get('can_stop')
assert get('/api/recording/state')['recording']['mode'] in ('not_started','unavailable','idle','ready','review')
version='20261021-f1-4242-sender-mismatch'
new=b/'releases'/version/'workspace/F1-infra';backup=b/'runtime/deployment-backups'/version
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert not new.exists() and not backup.exists()
 backup.mkdir(parents=True);shutil.copytree(old,new,symlinks=True)
 for name in p['files']:
  source=old/name if name.endswith('.py') else b/'dev/web'/name
  shutil.copy2(source,backup/name)
 shutil.copy2(b/'dev/sync.json',backup/'sync.json')
 (backup/'previous-current.txt').write_text(str(old))
 try:
  for name,entry in p['files'].items():
   target=new/name if name.endswith('.py') else b/'dev/web'/name
   tmp=target.with_suffix(target.suffix+'.right-arm-next');tmp.write_text(entry['source']);os.replace(tmp,target)
   assert sha(target)==entry['after']
   if not name.endswith('.py'):shutil.copy2(target,new/'web'/name)
  sync=json.loads((b/'dev/sync.json').read_text());sync['revision']=version
  for name,entry in p['files'].items():
   if not name.endswith('.py'):sync['files'][name]=entry['after']
  tmp=b/'dev/sync.next';tmp.write_text(json.dumps(sync,ensure_ascii=False));os.replace(tmp,b/'dev/sync.json')
  subprocess.run(['/usr/bin/python3','-c','import sys;sys.path.insert(0,sys.argv[1]);import hil_service',str(new)],check=True)
  link=b/'current.right-arm-next';link.symlink_to(new);os.replace(link,b/'current')
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  for _ in range(60):
   try:routes=get('/api/hil/routes');break
   except Exception:time.sleep(.2)
  else:raise RuntimeError('workbench restart failed')
  for name,entry in p['files'].items():
   if not name.endswith('.py'):
    assert hashlib.sha256(urlopen('http://127.0.0.1:4242/'+name,timeout=10).read()).hexdigest()==entry['after']
  r=dict(version=version,previous=str(old),current=str(new),backup=str(backup),
    files={n:e['after'] for n,e in p['files'].items()},routes_ready={x['id']:x['ready'] for x in routes['routes']},
    service_restarted='f1-console-4242.service',robot_commands=0,windows_sender_update_required=True)
  (backup/'receipt.json').write_text(json.dumps(r,indent=2));print(json.dumps(r))
 except BaseException:
  for name in p['files']:
   if not name.endswith('.py'):shutil.copy2(backup/name,b/'dev/web'/name)
  shutil.copy2(backup/'sync.json',b/'dev/sync.json')
  link=b/'current.right-arm-rollback';link.symlink_to(old);os.replace(link,b/'current')
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  raise
'''

if __name__=='__main__':
    files={}
    for name in ['hil_service.py','hil.js']:
        source=ROOT/'F1-4242'/(name if name.endswith('.py') else 'web/'+name)
        files[name]=dict(source=source.read_text(),after=hashlib.sha256(source.read_bytes()).hexdigest(),
            before=hashlib.sha256((OUT/'web-backup'/name).read_bytes()).hexdigest())
    result=subprocess.check_output(['ssh','-F',str(ROOT/'F1-4242/results/pose_task_20260928/ssh_config'),
        'f1-09-deploy','python3 -c '+shlex.quote(REMOTE)],input=json.dumps(dict(files=files)),text=True)
    (OUT/'web-installation.json').write_text(result);print(result)
