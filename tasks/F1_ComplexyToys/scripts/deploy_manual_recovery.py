"""Stage recovery candidates, then switch only idle F1 entries under their lock.

No robot control session or model service is started here. Baseline hashes and
rollback copies cover the manual entry, unified assembly and workbench changes.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[1]
OUT = TASK / 'artifacts/20261023-manual-recovery'
STAGE = OUT / 'staging'
SSH = ['ssh', '-F', str(ROOT / 'F1-4242/results/pose_task_20260928/ssh_config'), 'f1-09-deploy']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remote(code, payload):
    return subprocess.check_output(SSH + ['python3 -c ' + shlex.quote(code)],
        input=json.dumps(payload), text=True)


def upload(path):
    dest = '/tmp/f1-recovery-' + sha(path) + '.tar.gz'
    subprocess.run(['scp', '-F', SSH[2], str(path), 'f1-09-deploy:' + dest], check=True)
    return dest


COMMON = r'''
from pathlib import Path
import fcntl,hashlib,json,os,shutil,subprocess,sys,tarfile,time
from urllib.request import urlopen
p=json.load(sys.stdin)
assert Path('/etc/machine-id').read_text().strip()=='00000000000000000000000000000000'
sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
b=Path('/home/user/Bruno/F1/F1-4242')
data=Path('/home/user/Bruno/DAgger-runtime/manual-recovery-20261023')
get=lambda path:json.load(urlopen('http://127.0.0.1:4242'+path,timeout=10))
def idle(lock_held=False):
 s=get('/api/state');assert not s['control'].get('stopping')
 if lock_held:
  # Workbench reports the exclusive deployment lock as an external owner;
  # there must still be no web lease or active action/session.
  assert s['control'].get('lease_id') is None and s['control'].get('reason')=='共享控制锁已占用'
 else:assert not s['control'].get('owner')
 assert (s.get('action') or {}).get('status') not in ('accepted','queued','running','stopping')
 for route in ('play','demonstration','policy'):assert not get('/api/hil/state?route='+route)['hil']['active']
 assert not get('/api/teleop/state')['teleop'].get('can_stop')
 assert get('/api/recording/state')['recording']['mode'] in ('not_started','unavailable','idle','ready','review')
def extract(archive,dest,expected):
 assert sha(archive)==expected
 assert not dest.exists(),str(dest)
 dest.mkdir(parents=True)
 with tarfile.open(archive) as tar:
  for item in tar.getmembers():
   assert item.isfile() and not item.name.startswith('/') and '..' not in Path(item.name).parts
  for item in tar.getmembers():
   target=dest/item.name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(tar.extractfile(item).read());target.chmod(item.mode&0o777)
def verify(root,manifest):
 m=json.loads((root/manifest).read_text())
 for name,expected in m['files'].items():assert sha(root/name)==expected,name
 return sha(root/manifest)
'''

STAGE_REMOTE = COMMON + r'''
idle()
results={}
for mode,item in p['packages'].items():
 dest=Path(item['destination'])
 extract(item['upload'],dest.parent,item['archive_sha256'])
 assert verify(dest,'F1_RELEASE_MANIFEST.json')==item['manifest_sha256']
 subprocess.run(['python3','-m','compileall','-q',str(dest/'src')],check=True)
 results[mode]=dict(release=str(dest),manifest_sha256=item['manifest_sha256'])
assembly=Path(p['assembly_destination'])
extract(p['assembly_upload'],assembly,p['assembly_sha256'])
verify(assembly,'assembly-manifest.json')
data.mkdir(exist_ok=False)
(data/'config.json').write_text(p['manual_config'])
(data/'candidate-manual.sh').write_text(p['manual_entry'])
extract(p['tests_upload'],data/'validation',p['tests_sha256'])
results.update(assembly=str(assembly),validation=str(data/'validation'),robot_commands=0)
(data/'stage.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
'''

ACTIVATE_REMOTE = COMMON + r'''
idle()
assembly=Path(p['assembly_destination']);verify(assembly,'assembly-manifest.json')
for item in p['packages'].values():assert verify(Path(item['destination']),'F1_RELEASE_MANIFEST.json')==item['manifest_sha256']
assert (data/'config.json').read_text()==p['manual_config']
old=(b/'current').resolve();assert str(old)==p['old_web_current']
pointer=Path('/home/user/Bruno/DAgger-runtime/inference-current')
assert str(pointer.resolve())==p['old_assembly']
manual=Path('/home/user/Bruno/DAgger/start_f1_hil_route.sh')
registry=b/'runtime/hil-routes.json'
assert sha(manual)==p['manual_before'] and sha(registry)==p['registry_before']
for name,item in p['web'].items():
 source=old/name if name.endswith('.py') else b/'dev'/name
 assert sha(source)==item['before'],name+' changed concurrently'
new=b/'releases/20261023-manual-recovery-r1/workspace/F1-infra'
backup=data/'activation-backup';assert not new.exists() and not backup.exists()
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);idle(lock_held=True)
 backup.mkdir();shutil.copytree(old,new,symlinks=True)
 shutil.copy2(manual,backup/'manual-entry.sh');shutil.copy2(registry,backup/'routes.json');shutil.copy2(b/'dev/sync.json',backup/'sync.json')
 (backup/'pointers.json').write_text(json.dumps(dict(web=str(old),inference=str(pointer.resolve()))))
 for name in p['web']:
  source=old/name if name.endswith('.py') else b/'dev'/name
  target=backup/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
 def replace_text(target,content):
  temp=target.with_name(target.name+'.recovery-next');temp.write_text(content);temp.chmod(target.stat().st_mode&0o777 if target.exists() else 0o644);os.replace(temp,target)
 def replace_link(target,value):
  temp=target.with_name(target.name+'.recovery-next');temp.symlink_to(value);os.replace(temp,target)
 try:
  for name,item in p['web'].items():
   target=new/name if name.endswith('.py') else b/'dev'/name
   replace_text(target,item['source']);assert sha(target)==item['after']
   if not name.endswith('.py'):shutil.copy2(target,new/name)
  subprocess.run(['python3','-c','import sys;sys.path.insert(0,sys.argv[1]);import hil_service',str(new)],check=True)
  sync=json.loads((b/'dev/sync.json').read_text());sync['revision']='20261023-manual-recovery-r1'
  for name,item in p['web'].items():
   if not name.endswith('.py'):sync['files'][Path(name).name]=item['after']
  replace_text(b/'dev/sync.json',json.dumps(sync,ensure_ascii=False))
  routes=json.loads(registry.read_text())
  for route,hint in p['route_hints'].items():routes['routes'][route]['hint']=hint
  replace_text(registry,json.dumps(routes,ensure_ascii=False,indent=2)+'\n')
  replace_text(manual,p['manual_entry']);replace_link(pointer,assembly);replace_link(b/'current',new)
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  for _ in range(60):
   try:ready=get('/api/hil/routes');break
   except Exception:time.sleep(.2)
  else:raise RuntimeError('workbench did not recover')
  assert all(r['ready'] for r in ready['routes']),ready
  policy=next(r for r in ready['routes'] if r['id']=='policy')['commands'][0]['command']
  assert 'start_f1_toys_rtc_hil.sh --continuous' in policy and ' policy ' not in policy
  for name,item in p['web'].items():
   if not name.endswith('.py'):assert hashlib.sha256(urlopen('http://127.0.0.1:4242/'+Path(name).name).read()).hexdigest()==item['after']
  idle(lock_held=True)
 except BaseException:
  shutil.copy2(backup/'manual-entry.sh',manual);shutil.copy2(backup/'routes.json',registry);shutil.copy2(backup/'sync.json',b/'dev/sync.json')
  for name in p['web']:
   if not name.endswith('.py'):shutil.copy2(backup/name,b/'dev'/name)
  replace_link(pointer,p['old_assembly']);replace_link(b/'current',old)
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  raise
receipt=dict(manual=sha(manual),assembly=str(assembly),web=str(new),backup=str(backup),
 routes=ready,robot_commands=0,model_services_restarted=False,windows_update_required=False,
 web_files={name:item['after'] for name,item in p['web'].items()})
(data/'activation.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2));print(json.dumps(receipt,ensure_ascii=False))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['stage','activate'])
    args=parser.parse_args()
    summary=json.loads((STAGE/'summary.json').read_text())
    catalog=json.loads((STAGE/'catalog.json').read_text())
    payload=dict(packages=summary['packages'],assembly_destination=catalog['robot_release'],
        manual_config=(STAGE/'manual-config.json').read_text(),manual_entry=(STAGE/'manual-entry.sh').read_text())
    if args.action=='stage':
        for item in payload['packages'].values():item['upload']=upload(Path(item['archive']))
        for name,folder in [('assembly',STAGE/'assembly'),('tests',ROOT/'DAgger')]:
            archive=STAGE/(name+'.tar.gz')
            with tarfile.open(archive,'w:gz') as tar:
                paths=folder.rglob('*') if name=='assembly' else [p for sub in ['F1/tests','G2/tests','shared/tests'] for p in (folder/sub).rglob('*')]
                for path in sorted(paths):
                    if path.is_file() and '__pycache__' not in path.parts:tar.add(path,arcname=str(path.relative_to(folder)),recursive=False)
            payload[name+'_upload']=upload(archive);payload[name+'_sha256']=sha(archive)
        result=remote(STAGE_REMOTE,payload)
    else:
        paths=json.loads((OUT/'baseline/installed/paths.json').read_text())
        payload.update(old_web_current=paths['web_current'],old_assembly=paths['inference_current'],
            manual_before=sha(OUT/'baseline/installed/manual-entry.sh'),registry_before=sha(OUT/'baseline/installed/routes.json'))
        payload['web']={}
        entries=json.loads((ROOT/'F1-4242/results/hil-recovery-20261023/manifest.json').read_text())
        web_stage=STAGE/'web';web_stage.mkdir(exist_ok=True)
        for item in entries:
            if not item['path'].startswith('F1-4242/') or item['path'].endswith('.md'):continue
            name=item['path'].removeprefix('F1-4242/')
            current=ROOT/item['path'];before=ROOT/item['before'];baseline=OUT/'baseline/web-baseline'/name
            assert sha(current)==item['after_sha256'] and sha(before)==item['before_sha256']
            delta=''.join(difflib.unified_diff(before.read_text().splitlines(True),current.read_text().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
            target=web_stage/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(baseline.read_bytes())
            subprocess.run(['patch','--batch','--fuzz=0','-p1'],input=delta,cwd=web_stage,text=True,check=True,capture_output=True)
            payload['web'][name]=dict(before=sha(baseline),after=sha(target),source=target.read_text())
        payload['route_hints']={
            'play':'自由接入：默认右臂，START后RB操作；可恢复暂停时松RB、回中、等提示后再按RB。不录制。',
            'demonstration':'连续示教：START开条，RB控右臂；可恢复暂停仍录同一条，松RB、回中再按RB。完成后松RB、Y回位停稳2秒再↑保存。',
            'policy':'DAgger1K：START开始策略，再START人工接管；人工阶段不交还。可恢复暂停松RB、回中再按RB；Y回位停稳2秒后↑保存。'}
        result=remote(ACTIVATE_REMOTE,payload)
    (OUT/(args.action+'-receipt.json')).write_text(result)
    print(result)


if __name__=='__main__':
    main()
