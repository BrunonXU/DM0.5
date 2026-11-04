"""Stage or activate frozen episode releases; never start robot motion."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[1]
OUT = TASK/'artifacts/20261021-continuous-episodes'
SSH = ['ssh', '-F', str(ROOT/'F1-4242/results/pose_task_20260928/ssh_config'), 'f1-09-deploy']

MANUAL_ENTRY = '''#!/usr/bin/env bash
set -eo pipefail
route=${1:-}; shift || true
case "$route" in
 policy) exec bash /home/user/Bruno/DAgger/start_f1_toys_rtc_hil.sh "$@" ;;
 play|demonstration) ;;
 *) echo 'Usage: start_f1_hil_route.sh play|demonstration|policy [--shadow|--preflight]' >&2; exit 2 ;;
esac
option=${1:-}
[[ $# -le 1 && ( -z "$option" || "$option" == --shadow || "$option" == --preflight ) ]] || exit 2
set -a
source /home/user/Bruno/DAgger-runtime/f1-v18-site.env
set +a
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
unset F1_DAGGER_ENV
export F1_DAGGER_RUNTIME=/home/user/Bruno/DAgger-runtime/continuous-episodes-20261021/manual/runtime
export F1_HIL_ROUTE_EXECUTION=--powered
[[ "$option" != --shadow ]] || export F1_HIL_ROUTE_EXECUTION=--shadow
release=/home/user/Bruno/DAgger/releases/20261021-f1-hil-v33-continuous-episodes/DAgger
config=/home/user/Bruno/DAgger-runtime/continuous-episodes-20261021/manual/config.json
mkdir -p "$F1_DAGGER_RUNTIME"
if [[ "$option" == --preflight ]]; then
 exec bash "$release/scripts/50_f1_hil.sh" preflight --config "$config" --runtime "$F1_DAGGER_RUNTIME"
fi
episode=()
[[ "$route" != demonstration ]] || episode=(--episode-controls)
set -u
bash "$release/scripts/67_f1_route.sh" "$route" "${episode[@]}" --config "$config" \
 --runtime "$F1_DAGGER_RUNTIME" --data-root /home/user/Bruno/data/hil_data \
 2>&1 | tee -a "$F1_DAGGER_RUNTIME/onsite-powered-console.log"
'''

REMOTE = r'''
import fcntl,hashlib,json,os,shutil,sys,tarfile
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin)
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
get=lambda u:json.load(urlopen('http://127.0.0.1:4242'+u,timeout=10))
assert Path('/etc/machine-id').read_text().strip()=='00000000000000000000000000000000'
base=Path('/home/user/Bruno/DAgger-runtime/continuous-episodes-20261021')
dagger=Path('/home/user/Bruno/DAgger')
registry=Path('/home/user/Bruno/F1/F1-4242/runtime/hil-routes.json')
versions={'policy':'20261021-f1-toys-rtc-hil-r6','manual':'20261021-f1-hil-v33-continuous-episodes'}
old={'policy':'20261021-f1-toys-rtc-hil-r5','manual':'20261015-f1-hil-v32-direct-home-transport'}
oldcfg={'policy':Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r5/config.json'),
        'manual':Path('/home/user/Bruno/DAgger-runtime/f1-09-v32-direct-home-transport.json')}
runtime={'policy':Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r6'),'manual':base/'manual'}
def idle():
 for route in ('play','demonstration','policy'):assert not get('/api/hil/state?route='+route)['hil']['active']
 s=get('/api/state');assert not s['control'].get('owner') and not s['control'].get('stopping')
 assert (s.get('action') or {}).get('status') not in ('accepted','queued','running','stopping')
 assert not get('/api/teleop/state')['teleop'].get('can_stop')
idle()
health=json.load(urlopen(json.loads(oldcfg['policy'].read_text())['policy']['endpoint']+'/health',timeout=5))
assert health['ready'] and health['model_id']=='f1-toys-eef-efe3e5fa54ed0fb2'
assert health['denoise_steps']==5 and health['prediction_horizon']==50
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if p['action']=='stage':
  report={}
  for mode,version in versions.items():
   archive=base/(mode+'-episodes.tar.gz');expected=p['packages'][mode]
   assert sha(archive)==expected['archive_sha256']
   oldrelease=dagger/'releases'/old[mode]/'DAgger'
   cfg=json.loads(oldcfg[mode].read_text())
   assert sha(oldrelease/'F1_RELEASE_MANIFEST.json')==cfg['accepted_release_manifest_sha256']
   destination=dagger/'releases'/version
   assert not destination.exists() and not runtime[mode].exists()
   destination.mkdir()
   with tarfile.open(archive) as tar:tar.extractall(destination,filter='data')
   release=destination/'DAgger'
   manifest=json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
   assert sha(release/'F1_RELEASE_MANIFEST.json')==expected['manifest_sha256']
   for name,digest in manifest['files'].items():assert sha(release/name)==digest,name
   before=json.loads((oldrelease/'F1_RELEASE_MANIFEST.json').read_text())['files']
   assert set(n for n,d in before.items() if manifest['files'].get(n)!=d)==set(expected['changed'])
   assert set(manifest['files'])-set(before)==set(expected['added'])
   runtime[mode].mkdir(parents=True)
   cfg['accepted_release_manifest_sha256']=sha(release/'F1_RELEASE_MANIFEST.json')
   (runtime[mode]/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
   if mode=='policy':shutil.copy2(oldcfg[mode].parent/'panel-session.json',runtime[mode]/'panel-session.json')
   report[mode]=dict(release=str(release),runtime=str(runtime[mode]),manifest_sha256=expected['manifest_sha256'])
  (base/'staged.json').write_text(json.dumps(report,indent=2)+'\n')
  (base/'candidate-policy.sh').write_text(p['policy_entry'])
  (base/'candidate-manual.sh').write_text(p['manual_entry'])
  print(json.dumps(dict(staged=report,robot_commands=0,health_model_id=health['model_id'])))
 else:
  backup=base/'activation-backup';assert not backup.exists();backup.mkdir()
  entries={'policy':dagger/'start_f1_toys_rtc_hil.sh','manual':dagger/'start_f1_hil_route.sh'}
  for mode,entry in entries.items():
   assert sha(entry)==p['previous_entries'][mode]
   shutil.copy2(entry,backup/entry.name)
  assert sha(registry)==p['previous_registry']
  shutil.copy2(registry,backup/registry.name)
  routes=json.loads(registry.read_text())
  for mode,entry in entries.items():
   tmp=entry.with_suffix('.episode-next');tmp.write_text(p[mode+'_entry']);tmp.chmod(0o755);os.replace(tmp,entry)
  routes['routes']['policy'].update(runtime=str(runtime['policy']/'runtime'),
      session_config=str(runtime['policy']/'panel-session.json'),
      hint='Full1K连续分条：START开始策略，再START接管；人工START不交还。松RB、Y回位停稳至少2秒后↑保存。保存完成，整理现场，再START下一条。新版Windows sender必须安装；默认右臂。音效：开始→C接管→保存。真实连续分条待现场验收。')
  for route in ('play','demonstration'):
   routes['routes'][route]['runtime']=str(runtime['manual']/'runtime')
   routes['routes'][route]['hint']=('自由接入：默认右臂，START后直接RB，不录制。' if route=='play' else
     '连续示教：START开始，直接RB控右臂；松RB、Y回位停稳至少2秒后↑保存，保存完成再整理现场并START下一条。人工START不结束；需新版Windows sender。')
  tmp=registry.with_suffix('.episode-next');tmp.write_text(json.dumps(routes,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,registry)
  report=dict(entries={k:sha(v) for k,v in entries.items()},backup=str(backup),
      registry_sha256=sha(registry),routes=get('/api/hil/routes'),robot_commands=0,
      real_f710_shadow_accepted=False,physical_motion_accepted=False)
  (base/'activation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps(report,ensure_ascii=False))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['stage','activate'])
    action=parser.parse_args().action
    payload=dict(action=action,packages=json.loads((OUT/'package-summary.json').read_text()),
                 policy_entry=(TASK/'scripts/start_f1_rtc_hil.sh').read_text(),manual_entry=MANUAL_ENTRY,
                 previous_entries={m:hashlib.sha256((OUT/(m+'-entry.sh')).read_bytes()).hexdigest() for m in ['policy','manual']},
                 previous_registry=hashlib.sha256((OUT/'routes.json').read_bytes()).hexdigest())
    if action=='stage':
        remote='/home/user/Bruno/DAgger-runtime/continuous-episodes-20261021'
        subprocess.run([*SSH,'mkdir -p '+remote],check=True)
        subprocess.run(['scp',*SSH[1:-1],str(OUT/'policy-episodes.tar.gz'),str(OUT/'manual-episodes.tar.gz'),
                        'f1-09-deploy:'+remote+'/'],check=True)
    result=subprocess.check_output([*SSH,'python3 -c '+shlex.quote(REMOTE)],input=json.dumps(payload),text=True)
    (OUT/(action+'-receipt.json')).write_text(result)
    print(result)


if __name__=='__main__':
    main()
