#!/usr/bin/env python3
"""Install and bind the F1 task release to the final model; never run motion."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen

TASK = Path(__file__).resolve().parents[1]
PROJECTS = TASK.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-only', action='store_true', help='Verify immutable candidate without changing any active entry/config')
    args = parser.parse_args()
    health=json.load(urlopen('http://127.0.0.1:8096/health',timeout=3))
    bundle=TASK/'artifacts/20261016-v1-eef/bundle-1000/bundle.json'
    identity=hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert health['ready'] and health['model_version']==identity
    assert health['protocol']=='f1_eef_sync_v2' and health['denoise_steps']==5
    archive=TASK/'artifacts/20261016-v1-eef/f1-eef-client-v5-telemetry.tar.gz'
    ssh_config=PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'
    subprocess.run(['scp','-F',str(ssh_config),str(archive),'f1-09-deploy:/home/user/Bruno/DAgger-runtime/f1-toys-20261016/client-v5-telemetry.tar.gz'],check=True)
    with tarfile.open(archive) as package:
        manifest_sha=hashlib.sha256(package.extractfile('DAgger/F1_RELEASE_MANIFEST.json').read()).hexdigest()
    payload=dict(stage_only=args.stage_only,manifest_sha256=manifest_sha,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),health=health,entry=(TASK/'scripts/start_f1_onsite.sh').read_text())
    script=r'''
import json,sys,hashlib,os,tarfile,fcntl,shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
payload=json.loads(sys.stdin.read())
health=json.load(urlopen('http://127.0.0.1:8096/health',timeout=5))
assert health==payload['health'], 'F1 tunnel is not connected to the expected model'
if not payload['stage_only']:
    lock=open('/home/user/Bruno/F1/runtime/control.lock','a+')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)  # Never switch an occupied control session.
release=Path('/home/user/Bruno/DAgger/releases/20261016-f1-toys-eef-v5-telemetry/DAgger')
archive=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/client-v5-telemetry.tar.gz')
assert hashlib.sha256(archive.read_bytes()).hexdigest()==payload['archive_sha256']
if not release.parent.exists():
    release.parent.mkdir()
    with tarfile.open(archive) as package: package.extractall(release.parent,filter='data')
manifest=release/'F1_RELEASE_MANIFEST.json'
assert hashlib.sha256(manifest.read_bytes()).hexdigest()==payload['manifest_sha256']
for rel,expected in json.loads(manifest.read_text())['files'].items():
    assert hashlib.sha256((release/rel).read_bytes()).hexdigest()==expected, rel
runtime=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016')
config=runtime/'config-standalone.json'
cfg=json.loads((runtime/'config.json').read_text())
entry=Path('/home/user/Bruno/DAgger/start_f1_toys_policy.sh')
backup=runtime/'backups'/datetime.now(timezone.utc).strftime('standalone-%Y%m%dT%H%M%S.%fZ')
backup.mkdir(parents=True)
for old in (entry,config,runtime/'config.json'):
    if old.exists(): shutil.copy2(old,backup/old.name)
assert cfg['policy']['transport']=='eef_websocket_v2'
assert cfg['policy']['chunk_steps']==20 and cfg['policy']['frequency_hz']==30
cfg['policy']['model_id']=health['model_id']
cfg['accepted_release_manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
if payload['stage_only']:
    candidate=runtime/'config-telemetry-candidate.json'
    candidate.write_text(json.dumps(cfg,indent=2)+'\n')
    print(json.dumps(dict(staged=True,activated=False,release=str(release),config=str(candidate),
                         manifest_sha256=payload['manifest_sha256'])))
    sys.exit(0)
pending=config.with_suffix('.json.next');pending.write_text(json.dumps(cfg,indent=2)+'\n');os.replace(pending,config)
entry=Path('/home/user/Bruno/DAgger/start_f1_toys_policy.sh')
pending=entry.with_suffix('.sh.next');pending.write_text(payload['entry']);pending.chmod(0o755);os.replace(pending,entry)
receipt=dict(release=str(release),backup=str(backup),manifest_sha256=payload['manifest_sha256'],model_id=health['model_id'],model_version=health['model_version'],
    config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
    entry_sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),robot_commands=0,entry_executed=False)
(runtime/'telemetry-deployment.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
'''
    import shlex
    result=subprocess.check_output(['ssh','-F',str(PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'),
        'f1-09-deploy','python3 -c '+shlex.quote(script)],input=json.dumps(payload),text=True)
    name='telemetry-staging.json' if args.stage_only else 'telemetry-deployment.json'
    (TASK/'artifacts/20261016-v1-eef'/name).write_text(result)
    print(result)


if __name__=='__main__':main()
