"""Install tested alternative RTC entry files; never replace the default or move F1."""
import fcntl,hashlib,json
from pathlib import Path

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 root=Path('/home/user/Bruno/DAgger');runtime=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/h20-rtc')
 with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  default=root/'start_f1_toys_rtc_h20.sh';original=sha(default)
  assert original=='fc013ba7c2b24bdcc656970faee144133bfa7fe78d889b255c1b3cb4c51c7577'
  receipt={'default_sha256':original,'default_changed':False,'robot_commands':0,'candidates':{}}
  replay=json.loads((runtime/'ds-replay-20261020/summary.json').read_text())
  for tag,d,s in (('d14s20',14,20),('d14s16',14,16)):
   group=replay['groups'][tag];assert group['prefix_exact'] and group['guided']>=40 and group['deadline_exceeded']==0
   summary=json.loads((runtime/f'ds-shadow-{tag}-20261020/summary.json').read_text())
   assert summary['published_records']==summary['submitted_records']==0
   assert summary['events'].get('rtc_timing_paused',0)==0 and summary['events'].get('standalone_finished')==1
   assert summary['phase_metrics_ms']['guided']['count']>=20
   telemetry=json.loads((Path(summary['run'])/'telemetry-summary.json').read_text())
   assert telemetry['journal_complete'] and telemetry['binding']['execution_mode']=='shadow'
   assert telemetry['binding']['rtc_parameters']=={'delay_steps':d,'min_steps':s}
   source=runtime/f'ds-entries-20261020/{tag}.sh';target=root/f'start_f1_toys_rtc_h20_{tag}.sh'
   assert f'--rtc-delay-steps {d} --rtc-min-steps {s}' in source.read_text()
   if target.exists():assert sha(target)==sha(source)
   else:
    with target.open('xb') as f:f.write(source.read_bytes())
    target.chmod(0o755)
   receipt['candidates'][tag]={'entry':str(target),'sha256':sha(target),'shadow_run':summary['run'],'guided_requests':summary['phase_metrics_ms']['guided']['count'],'checkpoint_validated':50000}
  assert sha(default)==original
  (runtime/'ds-entries-20261020/installation.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))
if __name__=='__main__':main()
