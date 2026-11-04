#!/usr/bin/env python3
"""Stage a configuration-composed F1 assembly; activate only under an idle control lock.

Does not restart model servers, change SSH permissions, or initiate robot motion.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'F1-4242'))
from deploy.release import Remote


def activate_machine(args,c,remote,target):
    before=json.loads(args.before.read_text())['files']
    expected={m['run']+'/start.sh':before[m['run']+'/start.sh'] for m in c['models'].values() if m['machine']==args.machine}
    code=r'''from pathlib import Path
import hashlib,json,os,subprocess,time
from urllib.request import urlopen
root=Path(TARGET);c=json.loads((root/'catalog.json').read_text());machine=MACHINE
manifest=json.loads((root/'assembly-manifest.json').read_text())
for rel,sha in manifest['files'].items():
 if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=sha:raise ValueError('Assembly changed')
for path,sha in c['machines'][machine]['files_sha256'].items():
 if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=sha:raise ValueError('Server dependency changed')
checks=[]
for key,m in c['models'].items():
 if m['machine']!=machine:continue
 health=json.load(urlopen('http://127.0.0.1:'+str(m['remote_port'])+'/health',timeout=3))
 expected=dict(c['contracts'][m['contract']]['health'],model_id=m['model_id'],model_version=m['model_version'])
 if any(health.get(k)!=v for k,v in expected.items()) or health.get('ik_recovery',{}).get('enabled'):raise ValueError('Server identity mismatch')
 checked=dict(model=key,model_id=health['model_id'])
 if 'rlt' in m:
  extension=m['rlt']
  actor=json.load(urlopen('http://127.0.0.1:'+str(m['remote_port'])+'/rlt/health',timeout=3))
  expected_actor=dict(c['contracts'][extension['contract']]['health'],model_id=extension['model_id'],model_version=extension['model_version'],rlt=extension['identity'])
  if any(actor.get(k)!=v for k,v in expected_actor.items()) or actor.get('ik_recovery',{}).get('enabled'):raise ValueError('RLT server identity mismatch')
  checked['actor_model_id']=actor['model_id']
 checks.append(checked)
for path,text in EXPECTED.items():
 actual=Path(path).read_text() if Path(path).exists() else None
 if actual!=text:raise ValueError('Server entry changed concurrently')
backup=root.parent/(root.name+'-activation-backup');backup.mkdir(exist_ok=False)
changed=[]
try:
 for key,m in c['models'].items():
  if m['machine']!=machine:continue
  dest=Path(m['run'])/'start.sh';dest.parent.mkdir(parents=True,exist_ok=True)
  existed=dest.exists()
  if existed:(backup/(key+'.sh')).write_bytes(dest.read_bytes())
  else:(backup/(key+'.absent')).write_text('No previous entry\n')
  mode=(dest.stat().st_mode&0o777) if existed else 0o755
  temp=dest.with_name('start.sh.new');temp.write_bytes((root/'servers'/(key+'.sh')).read_bytes());temp.chmod(mode);os.replace(temp,dest);changed.append((dest,key,existed))
except BaseException:
 for dest,key,existed in changed:
  if existed:dest.write_bytes((backup/(key+'.sh')).read_bytes())
  else:dest.unlink()
 raise
print(json.dumps(dict(target=str(root),backup=str(backup),checks=checks,servers_restarted=False,time_ns=time.time_ns())))
'''.replace('TARGET',repr(target)).replace('MACHINE',repr(args.machine)).replace('EXPECTED',repr(expected))
    result=remote.python(code);(args.receipts/(args.machine+'-activation.json')).write_text(result);print(result)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assembly',type=Path,required=True)
    p.add_argument('--before',type=Path,required=True,help='Snapshot of current public entry contents')
    p.add_argument('--receipts',type=Path,required=True)
    p.add_argument('--host',default='f1-09-deploy')
    p.add_argument('--ssh-config',type=Path,default=ROOT/'F1-4242/results/pose_task_20260928/ssh_config')
    p.add_argument('--activate',action='store_true')
    p.add_argument('--machine',help='Stage/activate server launch files on a catalog machine; never starts a server')
    args=p.parse_args();a=args.assembly.resolve();c=json.loads((a/'catalog.json').read_text())
    args.receipts.mkdir(parents=True,exist_ok=True)
    remote=Remote(c['machines'][args.machine]['ssh_host']) if args.machine else Remote(args.host,args.ssh_config)
    target=c['machines'][args.machine]['release_root'] if args.machine else c['robot_release']
    prefix=args.machine or 'robot'
    if not args.activate:
        archive=args.receipts/'assembly.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            for path in sorted(a.rglob('*')):
                if path.is_file():tar.add(path,arcname=str(path.relative_to(a)))
        upload='/tmp/f1-assembly-'+hashlib.sha256(archive.read_bytes()).hexdigest()+'.tar.gz'
        remote.copy(archive,upload)
        code='''from pathlib import Path
import hashlib,json,tarfile,subprocess
root=Path(TARGET)
if root.exists():raise RuntimeError('Immutable assembly already exists')
root.mkdir(parents=True)
with tarfile.open(UPLOAD) as tar:
 for member in tar.getmembers():
  if member.name.startswith('/') or '..' in Path(member.name).parts or not member.isfile():raise ValueError('Invalid assembly member')
 for member in tar.getmembers():
  dest=root/member.name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(tar.extractfile(member).read())
manifest=json.loads((root/'assembly-manifest.json').read_text())
for rel,sha in manifest['files'].items():
 if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=sha:raise ValueError('Upload hash mismatch')
print(json.dumps(dict(target=str(root),files=len(manifest['files']),sha256=hashlib.sha256((root/'assembly-manifest.json').read_bytes()).hexdigest())))
'''.replace('TARGET',repr(target)).replace('UPLOAD',repr(upload))
        receipt=remote.python(code);(args.receipts/(prefix+'-stage.json')).write_text(receipt);print(receipt)
        return
    if args.machine:
        activate_machine(args,c,remote,target);return
    before=json.loads(args.before.read_text())['files']
    wrappers={'start_f1_toys_inference.sh':'start_f1_inference.sh','start_f1_toys_full_h20.sh':'start_f1_full_h20.sh','start_f1_toys_rtc_hil.sh':'start_f1_rtc_hil.sh'}
    expected={name:before['/home/user/Bruno/DAgger/'+name] for name in wrappers}
    code='''from pathlib import Path
import fcntl,json,os,hashlib,subprocess,time
root=Path(TARGET);catalog=json.loads((root/'catalog.json').read_text())
# Recheck staging, model identities and pinned clients BEFORE touching public entries.
checks=[]
for key,m in catalog['models'].items():
 for mode in m['modes']:
  result=subprocess.run(['python3',str(root/'inference_runtime.py'),'--model',key,'--mode',mode,'--check'],capture_output=True,text=True,timeout=45)
  if result.returncode:raise RuntimeError(result.stderr)
  checks.append(json.loads(result.stdout))
base=Path('/home/user/Bruno/DAgger');pointer=root.parent/'inference-current'
backup=root.parent/(root.name+'-activation-backup')
with open(catalog['robot']['control_lock'],'r+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 for name,content in EXPECTED.items():
  if (base/name).read_text()!=content:raise RuntimeError('Entry changed concurrently: '+name)
 backup.mkdir(exist_ok=False)
 previous=os.readlink(pointer) if pointer.is_symlink() else None
 if pointer.exists() and not pointer.is_symlink():raise RuntimeError('Current pointer is not a symlink')
 for name in WRAPPERS:
  (backup/name).write_bytes((base/name).read_bytes())
 (backup/'previous-pointer.json').write_text(json.dumps(previous))
 changed=[]
 try:
  temp=pointer.with_name(pointer.name+'.new');temp.symlink_to(root);os.replace(temp,pointer)
  for name,source in WRAPPERS.items():
   temp=base/(name+'.new');temp.write_bytes((root/source).read_bytes());temp.chmod((base/name).stat().st_mode&0o777);os.replace(temp,base/name);changed.append(name)
 except BaseException:
  for name in changed:(base/name).write_bytes((backup/name).read_bytes())
  if previous is None:pointer.unlink(missing_ok=True)
  else:
   temp=pointer.with_name(pointer.name+'.rollback');temp.symlink_to(previous);os.replace(temp,pointer)
  raise
print(json.dumps(dict(target=str(root),backup=str(backup),checks=checks,entries={k:hashlib.sha256((base/k).read_bytes()).hexdigest() for k in WRAPPERS},time_ns=time.time_ns())))
'''.replace('TARGET',repr(target)).replace('EXPECTED',repr(expected)).replace('WRAPPERS',repr(wrappers))
    receipt=remote.python(code);(args.receipts/'robot-activation.json').write_text(receipt);print(receipt)

if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as e:
        print(e.stderr or str(e),file=sys.stderr);raise
