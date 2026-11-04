"""Bind the existing desktop policy route to the isolated RTC HIL receiver."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

TASK=Path(__file__).resolve().parents[1]
PROJECTS=TASK.parents[1]
OUT=TASK/'artifacts/20261021-rtc-hil'

REMOTE=r'''
import fcntl,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from urllib.request import urlopen
p=json.load(sys.stdin)
b=Path('/home/user/Bruno/F1/F1-4242'); old=(b/'current').resolve()
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
get=lambda u:json.load(urlopen('http://127.0.0.1:4242'+u,timeout=10))
assert Path('/etc/machine-id').read_text().strip()=='00000000000000000000000000000000'
assert sha(old/'hil_service.py')==p['before_sha256'],'concurrent backend edit'
s=get('/api/state')
assert not s['control'].get('owner') and not s['control'].get('stopping')
assert (s.get('action') or {}).get('status') not in ('accepted','queued','running','stopping')
assert not get('/api/hil/state')['hil']['active']
assert not get('/api/teleop/state')['teleop'].get('can_stop')
assert get('/api/recording/state')['recording']['mode'] in ('not_started','unavailable','idle','ready','review')
runtime=Path('/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r1')
assert (runtime/'installation.json').is_file()
version='20261021-f1-4242-rtc-hil-r1'
new=b/'releases'/version/'workspace/F1-infra'
backup=b/'runtime/deployment-backups'/version
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert not new.exists() and not backup.exists()
 backup.mkdir(parents=True)
 shutil.copy2(old/'hil_service.py',backup/'hil_service.py')
 registry=b/'runtime/hil-routes.json'
 shutil.copy2(registry,backup/'hil-routes.json')
 (backup/'current.txt').write_text(str(old))
 shutil.copytree(old,new,symlinks=True)
 (new/'hil_service.py').write_text(p['source'])
 assert sha(new/'hil_service.py')==p['after_sha256']
 # Import the patched module without any robot initialization.
 subprocess.run(['/usr/bin/python3','-c','import sys;sys.path.insert(0,sys.argv[1]);import hil_service',str(new)],check=True)
 cfg=json.loads((runtime/'config.json').read_text())
 session=dict(schema='f1_hil_session_v1',purpose='policy_hil',auto_video=False,
  task=p['prompt'],policy={k:cfg['policy'][k] for k in ('endpoint','model_id','frequency_hz','chunk_steps')})
 (runtime/'panel-session.json').write_text(json.dumps(session,indent=2)+'\n')
 routes=json.loads(registry.read_text())
 routes['routes']['policy']=dict(launcher='/home/user/Bruno/DAgger/start_f1_toys_rtc_hil.sh',
  session_config=str(runtime/'panel-session.json'),runtime=str(runtime/'runtime'),rtc_hil=True,candidate=True,
  hint='Full1K RTC-HIL：首次START开始策略，再按START接管；十字键选右并释放，RB控臂、RT切夹爪。松RB并等夹爪完成后START交还。先做SHADOW；Ctrl+C完整保存。真实回稳后保留2–3秒，审核后才作为纠正数据。Y沿当前直接回V1配置。')
 temp=registry.with_suffix('.next');temp.write_text(json.dumps(routes,ensure_ascii=False,indent=2)+'\n');os.replace(temp,registry)
 nextlink=b/'current.rtc-hil-next';nextlink.symlink_to(new);os.replace(nextlink,b/'current')
 # Only this idle workbench service restarts. Drivers/model/IK services stay up.
 try:
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  for _ in range(60):
   try:
    after=get('/api/hil/routes');break
   except Exception:time.sleep(.2)
  else:raise RuntimeError('workbench did not return')
  result=dict(version=version,previous=str(old),current=str(new),backup=str(backup),
   backend_sha256=sha(new/'hil_service.py'),routes=after,robot_commands=0,
   service_restarted='f1-console-4242.service',model_services_unchanged=True)
  (backup/'receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps(result,ensure_ascii=False))
 except BaseException:
  shutil.copy2(backup/'hil-routes.json',registry)
  link=b/'current.rtc-hil-rollback';link.symlink_to(old);os.replace(link,b/'current')
  subprocess.run(['systemctl','--user','restart','f1-console-4242.service'],check=True)
  raise
'''

if __name__=='__main__':
    before=OUT/'web-hil-service.before.py'
    source=(PROJECTS/'F1-4242/hil_service.py').read_text()
    payload=dict(before_sha256=hashlib.sha256(before.read_bytes()).hexdigest(),source=source,
        after_sha256=hashlib.sha256(source.encode()).hexdigest(),
        prompt=json.loads((TASK/'configs/eef_v1_home_mask.json').read_text())['prompt'])
    result=subprocess.check_output(['ssh','-F',str(PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'),
        'f1-09-deploy','python3 -c '+shlex.quote(REMOTE)],input=json.dumps(payload),text=True)
    (OUT/'panel-installation.json').write_text(result)
    print(result)
