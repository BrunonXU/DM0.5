"""Only used by local process lifecycle tests; no numerics or hardware."""
import os,signal,socket,sys,time,struct
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from audit_workers import PROTOCOL,_bound_result
from audit_worker_launch import verify_parent_death_signal
from f1_harness.ipc import send,receive
verify_parent_death_signal()
fd=int(sys.argv[-1]);channel=socket.socket(fileno=fd)
init=receive(channel);mode=init['spec'].get('test_mode','normal')
life=init['spec'].get('lifecycle_dir')
if life:
 import json
 Path(life,str(init['worker_index'])+'.json').write_text(json.dumps(dict(pid=os.getpid(),phase='initialize')))
if mode=='init_hang':time.sleep(30)
if mode=='ignore_term':signal.signal(signal.SIGTERM,signal.SIG_IGN)
send(channel,dict(protocol=PROTOCOL,kind='ready',generation='old' if mode=='bad_ready' else init['generation'],
    spec_sha256=init['spec_sha256'],worker_index=init['worker_index'],pid=os.getpid(),
    asset_id=None if init.get('assets') is None else init['assets']['asset_id']))
while True:
 try:r=receive(channel)
 except (OSError,ConnectionError):break
 if life:Path(life,str(init['worker_index'])+'.json').write_text(json.dumps(dict(pid=os.getpid(),phase='busy')))
 if mode=='crash':os._exit(9)
 if mode in ('hang','ignore_term'):time.sleep(30)
 if mode=='hang_once':
  marker=Path(init['spec']['marker'])
  if not marker.exists():marker.write_text('first request entered');time.sleep(30)
 if mode=='partial_frame':channel.sendall(struct.pack('!I',100)+b'{');time.sleep(30)
 result=dict(samples=0 if mode=='partial' else len(r['job']['points']),failures=[],
   worker_pid=os.getpid(),private_credential_present='TEST_PRIVATE_CREDENTIAL' in os.environ)
 if mode=='rejected_prefix':
  result.update(samples=1,failures=[dict(virtual_ns=100,fraction=1.)],stopped_on_first_failure=True)
 reply=_bound_result(r,ok=mode!='native_error',result=result)
 if mode=='bad_binding':reply['context_binding']='old'
 if mode=='bad_asset':reply['asset_id']='another-asset'
 send(channel,reply)
 if mode=='duplicate':send(channel,reply)
