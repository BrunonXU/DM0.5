"""Install a separate immutable RTC entry; never invokes robot motion."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
from urllib.request import urlopen

TASK=Path(__file__).resolve().parents[1]
PROJECTS=TASK.parents[1]
OUT=TASK/'artifacts/20261019-rtc'
SSH=PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'

REMOTE=r'''
import fcntl,hashlib,json,os,sys,tarfile,shutil
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin)
health=json.load(urlopen('http://127.0.0.1:8135/health',timeout=5))
assert health==p['health'] and health['rtc'] is True
lock=open('/home/user/Bruno/F1/runtime/control.lock','a+')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
runtime=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016')
archive=runtime/'f1-rtc-client-v4.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()==p['archive_sha256']
parent=Path('/home/user/Bruno/DAgger/releases/20261019-f1-toys-rtc-v4')
if not parent.exists():
 parent.mkdir()
 with tarfile.open(archive) as tar:tar.extractall(parent,filter='data')
release=parent/'DAgger'
manifest=release/'F1_RELEASE_MANIFEST.json'
assert hashlib.sha256(manifest.read_bytes()).hexdigest()==p['manifest_sha256']
for name,digest in json.loads(manifest.read_text())['files'].items():
 assert hashlib.sha256((release/name).read_bytes()).hexdigest()==digest,name
cfg=json.loads((runtime/'config-compare-5000.json').read_text())
cfg['policy'].update(endpoint='http://127.0.0.1:8136',model_id=health['model_id'])
assets=runtime/'rtc-pixels-v2';assets.mkdir(exist_ok=True)
for name,data in p['pixel_assets'].items():
 path=assets/name
 if path.exists():assert path.read_text()==data
 else:path.write_text(data)
cfg['policy']['rtc_pixels_directory']=str(assets)
cfg['accepted_release_manifest_sha256']=p['manifest_sha256']
config=runtime/'config-rtc-5000-v4.json'
entry=Path('/home/user/Bruno/DAgger/start_f1_toys_rtc.sh')
for target,data in ((config,json.dumps(cfg,indent=2)+'\n'),
                    (runtime/'ensure_rtc_tunnel.py',p['tunnel_helper']),(entry,p['entry'])):
 if target.exists() and target.read_text()!=data:
  backup=runtime/'backups'/datetime.now(timezone.utc).strftime('rtc-%Y%m%dT%H%M%S.%fZ')
  backup.mkdir(parents=True);shutil.copy2(target,backup/target.name)
 if not target.exists() or target.read_text()!=data:
  pending=target.with_suffix(target.suffix+'.next');pending.write_text(data)
  if target==entry:pending.chmod(0o755)
  os.replace(pending,target)
receipt=dict(release=str(release),config=str(config),entry=str(entry),model_id=health['model_id'],
 model_version=health['model_version'],manifest_sha256=p['manifest_sha256'],
 archive_sha256=p['archive_sha256'],robot_commands=0,entry_executed=False,
 existing_sync_entries_unchanged=True)
(runtime/'rtc-deployment.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
'''


def main():
    health=json.load(urlopen('http://127.0.0.1:8135/health',timeout=5))
    bundle=TASK/'artifacts/20261016-v1-formal-training/bundle-5000/bundle.json'
    assert health['rtc'] is True and health['model_version']==hashlib.sha256(bundle.read_bytes()).hexdigest()
    if subprocess.run(['tmux','has-session','-t','f1-toys-rtc-tunnel'],capture_output=True).returncode:
        command=shlex.join(['ssh','-F',str(SSH),'-NT','-o','ExitOnForwardFailure=yes',
            '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            '-R','127.0.0.1:8135:127.0.0.1:8135','f1-09-deploy'])
        subprocess.run(['tmux','new-session','-d','-s','f1-toys-rtc-tunnel',command],check=True)
    archive=OUT/'f1-rtc-client-v4.tar.gz'
    subprocess.run(['scp','-F',str(SSH),str(archive),
        'f1-09-deploy:/home/user/Bruno/DAgger-runtime/f1-toys-20261016/f1-rtc-client-v4.tar.gz'],check=True)
    with tarfile.open(archive) as tar:
        digest=hashlib.sha256(tar.extractfile('DAgger/F1_RELEASE_MANIFEST.json').read()).hexdigest()
    payload=dict(health=health,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        manifest_sha256=digest,entry=(TASK/'scripts/start_f1_rtc.sh').read_text(),
        tunnel_helper=(TASK/'scripts/ensure_rtc_tunnel.py').read_text(),
        pixel_assets={name:(OUT/'pixel-assets'/name).read_text() for name in ('processing.py','contract.json')})
    result=subprocess.check_output(['ssh','-F',str(SSH),'f1-09-deploy','python3 -c '+shlex.quote(REMOTE)],
        input=json.dumps(payload),text=True)
    receipt=OUT/'deployment-client-v4.json'
    if not receipt.exists():receipt.write_text(result)
    (OUT/'deployment.json').write_text(result);print(result)


if __name__=='__main__':main()
