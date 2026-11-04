"""Install distinct-cue HIL r5, preserve r4 and never start robot control."""
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
import fcntl,hashlib,json,os,shutil,sys,tarfile
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin)
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
get=lambda u:json.load(urlopen('http://127.0.0.1:4242'+u,timeout=10))
assert Path('/etc/machine-id').read_text().strip()=='00000000000000000000000000000000'
assert not get('/api/hil/state')['hil']['active']
s=get('/api/state');assert not s['control'].get('owner') and not s['control'].get('stopping')
assert (s.get('action') or {}).get('status') not in ('accepted','queued','running','stopping')
assert not get('/api/teleop/state')['teleop'].get('can_stop')
old=Path('/home/user/Bruno/DAgger/releases/20261021-f1-toys-rtc-hil-r4/DAgger')
oldrun=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r4')
cfg=json.loads((oldrun/'config.json').read_text())
health=json.load(urlopen(cfg['policy']['endpoint']+'/health',timeout=5))
assert health['ready'] and health['model_id']=='f1-toys-eef-efe3e5fa54ed0fb2'
assert sha(old/'F1_RELEASE_MANIFEST.json')==cfg['accepted_release_manifest_sha256']
entry=Path('/home/user/Bruno/DAgger/start_f1_toys_rtc_hil.sh')
assert sha(entry)==p['previous_entry_sha256']
archive=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-client-r5.tar.gz')
assert sha(archive)==p['archive_sha256']
parent=Path('/home/user/Bruno/DAgger/releases/20261021-f1-toys-rtc-hil-r5')
runtime=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r5')
registry=Path('/home/user/Bruno/F1/F1-4242/runtime/hil-routes.json')
routes=json.loads(registry.read_text());assert routes['routes']['policy']['runtime']==str(oldrun/'runtime')
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert not parent.exists() and not runtime.exists()
 parent.mkdir()
 with tarfile.open(archive) as tar:tar.extractall(parent,filter='data')
 release=parent/'DAgger';manifest=json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
 for n,d in manifest['files'].items():assert sha(release/n)==d,n
 previous=json.loads((old/'F1_RELEASE_MANIFEST.json').read_text())['files']
 changed=[n for n,d in previous.items() if manifest['files'].get(n)!=d]
 assert set(changed)=={'src/f1dagger/rtc_hil_feedback.py','windows_client/scripts/03_windows_f1_sender.py'},changed
 assert set(manifest['files'])-set(previous)==set()
 runtime.mkdir();backup=runtime/'backup';backup.mkdir()
 shutil.copy2(entry,backup/entry.name);shutil.copy2(registry,backup/registry.name)
 cfg['accepted_release_manifest_sha256']=sha(release/'F1_RELEASE_MANIFEST.json')
 (runtime/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
 shutil.copy2(oldrun/'panel-session.json',runtime/'panel-session.json')
 routes['routes']['policy'].update(runtime=str(runtime/'runtime'),session_config=str(runtime/'panel-session.json'),
  hint='Full1K RTC-HIL：首次START开始策略，再按START接管；新版Windows sender默认右臂，直接RB控臂、RT切右夹爪，无需十字键选侧；松RB并等夹爪完成后START交还。三种状态音：原开始音→人工介入C下行双音（响度已现场确认）→新RTC交还生效上行双音；原B音仍提示Ctrl+C后原件完整保存。沿用原示教音量；START逻辑未改。真实纠正首条reader已审核，后续仍逐条审查。Y直接回V1，先确认净空。')
 tmp=entry.with_suffix('.next');tmp.write_text(p['entry']);tmp.chmod(0o755);os.replace(tmp,entry)
 tmp=registry.with_suffix('.next');tmp.write_text(json.dumps(routes,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,registry)
 receipt=dict(release=str(release),runtime=str(runtime),entry=str(entry),backup=str(backup),
  changed_files=changed,added_files=[],start_control_unchanged=True,
  release_sha256=sha(release/'F1_RELEASE_MANIFEST.json'),config_sha256=sha(runtime/'config.json'),
  entry_sha256=sha(entry),robot_commands=0,services_restarted=[],
  audio_levels=json.loads(Path('/home/user/Bruno/DAgger-runtime/f1-09/recording_audio.json').read_text()),
  health_model_id=health['model_id'],routes=get('/api/hil/routes'))
 (runtime/'installation.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(receipt,ensure_ascii=False))
'''


if __name__ == '__main__':
    archive=OUT/'client-isolated-r5.tar.gz'
    subprocess.run(['scp',*SSH[1:-1],str(archive),'f1-09-deploy:/home/user/Bruno/DAgger-runtime/rtc-hil-client-r5.tar.gz'],check=True)
    payload=dict(archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        previous_entry_sha256=json.loads((OUT/'audio-r4-installation.json').read_text())['entry_sha256'],
        entry=(TASK/'scripts/start_f1_rtc_hil.sh').read_text())
    result=subprocess.check_output([*SSH,'python3 -c '+shlex.quote(REMOTE)],input=json.dumps(payload),text=True)
    (OUT/'audio-r5-installation.json').write_text(result)
    print(result)
