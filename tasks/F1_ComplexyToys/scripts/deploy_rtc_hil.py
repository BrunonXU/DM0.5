"""Install the separate Full1K HIL release; does not run a powered entry."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

TASK = Path(__file__).resolve().parents[1]
PROJECTS = TASK.parents[1]
OUT = TASK/'artifacts/20261021-rtc-hil'
SSH = ['ssh', '-F', str(PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'), 'f1-09-deploy']

REMOTE = r'''
import fcntl,hashlib,json,os,sys,tarfile
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin)
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
base=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20')
cfg=json.loads((base/'startup-read-v2/1000/config.json').read_text())
health=json.load(urlopen(cfg['policy']['endpoint']+'/health',timeout=5))
assert health['ready'] and health['rtc'] and health['model_id']=='f1-toys-eef-efe3e5fa54ed0fb2'
assert health['model_version']=='efe3e5fa54ed0fb2e7a60cd96b0da4161b327af903d3bb397946668c59504f3b'
assert health['denoise_steps']==5 and health['prediction_horizon']==50
old=Path('/home/user/Bruno/DAgger/releases/20261021-f1-toys-rtc-v6-param-read-r2/DAgger')
assert sha(old/'F1_RELEASE_MANIFEST.json')==cfg['accepted_release_manifest_sha256']
runtime=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r1')
archive=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-client-r1.tar.gz')
assert sha(archive)==p['archive_sha256']
parent=Path('/home/user/Bruno/DAgger/releases/20261021-f1-toys-rtc-hil-r1')
entry=Path('/home/user/Bruno/DAgger/start_f1_toys_rtc_hil.sh')
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert not parent.exists() and not runtime.exists() and not entry.exists(), 'immutable destination exists'
 parent.mkdir()
 with tarfile.open(archive) as tar:tar.extractall(parent,filter='data')
 release=parent/'DAgger'
 manifest=json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
 for name,digest in manifest['files'].items():assert sha(release/name)==digest,name
 previous=json.loads((old/'F1_RELEASE_MANIFEST.json').read_text())['files']
 changed=[n for n,d in previous.items() if manifest['files'].get(n)!=d]
 assert set(changed)<= {'src/f1dagger/cli.py','src/f1dagger/runtime.py','docs/STATUS.md'},changed
 assert set(manifest['files'])-set(previous)=={'src/f1dagger/rtc_hil.py'}
 infra=base/'startup-read-v2/infra'
 for name,digest in json.loads((release/'configs/f1_infra_binding.json').read_text())['files'].items():
  assert sha(infra/name)==digest,name
 pixels=Path(cfg['policy']['rtc_pixels_directory'])
 assert sha(pixels/'contract.json')==health['input_preprocessing']['contract_sha256']
 assert sha(pixels/'processing.py')==health['input_preprocessing']['implementation_sha256']
 runtime.mkdir()
 cfg['accepted_release_manifest_sha256']=sha(release/'F1_RELEASE_MANIFEST.json')
 cfg['acceptance']['physical_motion_accepted']=False
 cfg['acceptance']['windows_f710_accepted']=False
 (runtime/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
 entry.write_text(p['entry']);entry.chmod(0o755)
 receipt=dict(release=str(release),runtime=str(runtime),entry=str(entry),health=health,
  release_sha256=cfg['accepted_release_manifest_sha256'],config_sha256=sha(runtime/'config.json'),
  entry_sha256=sha(entry),changed_files=changed,robot_commands=0,services_stopped=False,
  onsite_accepted=False,real_f710_shadow_accepted=False,source_release=str(old))
 (runtime/'installation.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
'''


def main():
    archive = OUT/'client-isolated-r1.tar.gz'
    subprocess.run(['scp', *SSH[1:-1], str(archive),
        'f1-09-deploy:/home/user/Bruno/DAgger-runtime/rtc-hil-client-r1.tar.gz'], check=True)
    payload = dict(archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                   entry=(TASK/'scripts/start_f1_rtc_hil.sh').read_text())
    result = subprocess.check_output([*SSH, 'python3 -c '+shlex.quote(REMOTE)],
                                     input=json.dumps(payload), text=True)
    (OUT/'installation.json').write_text(result)
    print(result)


if __name__ == '__main__':
    main()
