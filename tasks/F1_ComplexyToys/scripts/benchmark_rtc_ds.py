"""Replay historical observations through live RTC, under F1 lock; no ROS outputs."""
import argparse,fcntl,hashlib,json,multiprocessing,time,uuid
from pathlib import Path
from urllib.request import urlopen
import numpy as np
from dagger_core.recording_observation import TimestampedFrame,DeviceTimestamp
from f1dagger.rtc_pixels import PixelEncoder
from f1dagger.websocket_policy import WebSocketPolicy,encode_request,network_worker

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  a.output.mkdir(parents=True,exist_ok=False)
  health=json.load(urlopen('http://127.0.0.1:8148/health',timeout=5))
  assert health['model_version']=='1d55a3e0017742b05750ecab6bfc3f73486b8cfd73d1dd13f6844079fd785b47'
  encoder=PixelEncoder('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/rtc-pixels-v2',health['input_preprocessing'],workers=3)
  context=multiprocessing.get_context('spawn');client=WebSocketPolicy({'endpoint':'http://127.0.0.1:8148','timeout_s':10},'',lambda _:None,held_getter=lambda:np.zeros(16))
  client.pipe,child=context.Pipe();client.process=context.Process(target=network_worker,args=(child,'ws://127.0.0.1:8148/ws',10),daemon=True);client.process.start();child.close()
  records=[];rid=0;session=uuid.uuid4().hex
  try:
   for ri,run in enumerate(('f1_hil_20261020T054220Z_13f99c58','f1_hil_20261020T060908Z_9a690c1e','f1_hil_20261020T060944Z_6487bd7a','f1_hil_20261020T061043Z_ed37ca4a')):
    root=Path('/home/user/Bruno/data/hil_data/policy_hil')/run
    observations=list(map(json.loads,(root/'payload/frames.jsonl').open()))
    first=next(e for e in map(json.loads,(root/'journal/events.jsonl').open()) if e['event']=='powered_policy_request')
    anchor=next(o['available_monotonic_ns'] for o in observations if o['observation_id']==first['observation_id'])
    configs=[(20,20),(14,20),(14,16)];configs=configs[ri%3:]+configs[:ri%3]
    for d,s in configs:
     old=None;base_id=0
     for step in range(12):
      target=anchor+round(step*s/30*1e9);obs=max((o for o in observations if o['available_monotonic_ns']<=target),key=lambda o:o['available_monotonic_ns'])
      frames=[]
      for f in obs['frames']:
       with (root/'payload'/f['path']).open('rb') as stream:stream.seek(f['payload_offset']);data=stream.read(f['payload_bytes'])
       assert hashlib.sha256(data).hexdigest()==f['payload_sha256']
       frames.append(TimestampedFrame(f['camera'],f['frame_id'],DeviceTimestamp(**f['timestamp']),f['host_received_monotonic_ns'],data,f['media_type'],f['width'],f['height']))
      rid+=1;bootstrap=old is None;k=s+d+2
      rtc=dict(bootstrap=bootstrap,delay_steps=d,shift_steps=s,coverage_steps=k,plan_origin_ns=0 if bootstrap else target,base_plan_id=0 if bootstrap else base_id,committed_actions16=[] if bootstrap else old['actions'][s:s+d],previous_absolute_eef10=[] if bootstrap else old['absolute_eef10'][s:])
      meta=dict(contract='f1_upper_16d_v1',protocol='f1_eef_rtc_v2',session_id=session,request_id=rid,observation_id=obs['observation_id'],policy_epoch=0,observation_monotonic_ns=obs['capture_started_monotonic_ns'],state16=obs['state16'],last_submitted16=obs['state16'] if bootstrap else old['actions'][s-1],state_timestamps_ns=obs['state_timestamps_ns'],prediction_horizon=50,execute_steps=20,chunk_steps=20,frequency_hz=30.,shadow_only=True,model_id=health['model_id'],model_version=health['model_version'],prepared_pixels=encoder.identity,rtc=rtc)
      start=time.perf_counter();payload=encode_request(meta,encoder.encode(frames));packed=time.perf_counter();result=client._exchange(payload);elapsed=(time.perf_counter()-start)*1000
      assert result['session_id']==session and result['request_id']==rid and result['model_version']==health['model_version'] and result['shadow_only'] is True
      assert result['rtc_context']=={x:rtc[x] for x in ('bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id')}
      assert 'error' not in result,result
      row=dict(run=run,step=step,d=d,s=s,bootstrap=bootstrap,success=result['success'],client_total_ms=elapsed,pack_ms=(packed-start)*1000,server=result['timing_ms'],observation_id=obs['observation_id'],prefix_exact=None,deadline_ms=d*1000/30)
      if result['success']:
       actions=np.asarray(result['actions']);assert actions.shape==(k,16) and np.isfinite(actions).all()
       if not bootstrap:
        row['prefix_exact']=np.array_equal(actions[:d],np.asarray(rtc['committed_actions16']));assert row['prefix_exact']
        row['join_joint_delta_deg']=float(abs(actions[d,7:14]-actions[d-1,7:14]).max())
       old=result;base_id=rid
      else:
       assert result.get('reason')=='ik_rejected';old=None
      records.append(row)
      with (a.output/'requests.jsonl').open('a') as out:out.write(json.dumps(row)+'\n')
     print(json.dumps(dict(run=run,d=d,s=s,completed=len(records))),flush=True)
  finally:client.close();encoder.close()
  def stats(v):return dict(zip(('p50','p95','p99','max'),np.percentile(v,[50,95,99,100]).tolist())) if v else {}
  groups={}
  for d,s in ((20,20),(14,20),(14,16)):
   rr=[r for r in records if (r['d'],r['s'])==(d,s)];gg=[r for r in rr if not r['bootstrap']]
   groups[f'd{d}s{s}']=dict(requests=len(rr),guided=len(gg),success=sum(r['success'] for r in rr),guided_success=sum(r['success'] for r in gg),prefix_exact=all(r['prefix_exact'] for r in gg if r['success']),deadline_exceeded=sum(r['client_total_ms']>=r['deadline_ms'] for r in gg),client=stats([r['client_total_ms'] for r in gg]),model=stats([r['server']['model_h2d_d2h'] for r in gg]),ik=stats([r['server']['ik'] for r in gg]),join_joint_delta_deg=stats([r['join_joint_delta_deg'] for r in gg if 'join_joint_delta_deg' in r]))
  result=dict(health=health,groups=groups,robot_commands=0,limitation='Sequential historical-state replay with recursively generated prefixes. Not closed-loop; noise differs; no live recording load. Warmup/bootstrap excluded from guided timings.')
  (a.output/'summary.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
if __name__=='__main__':main()
