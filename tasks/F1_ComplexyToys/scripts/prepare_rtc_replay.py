"""Copy selected historical 5K rollout observations, verifying original hashes."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EPISODE = 'f1_hil_20261019T080740Z_0b67a7f3'

REMOTE = r'''
import json,sys,tarfile,io,hashlib
from pathlib import Path
selected=json.load(sys.stdin)
root=Path('/home/user/Bruno/data/hil_data/policy_hil')/selected['episode']/'payload'
observations=[json.loads(l) for l in (root/'frames.jsonl').open()]
by_id={r['observation_id']:r for r in observations}
pairs=[];wanted={}
for request in selected['requests']:
 a=by_id[request['observation_id']]
 assert a['state16']==request['prediction_replay']['state16']
 assert a['capture_started_monotonic_ns']==request['observation_monotonic_ns']
 for shift in (15,20,25):
  deadline=a['available_monotonic_ns']+round(shift/30*1e9)
  candidates=[r for r in observations if r['available_monotonic_ns']<=deadline]
  b=candidates[-1]
  pairs.append(dict(request_id=request['request_id'],shift=shift,a=a['observation_id'],b=b['observation_id'],
                    observation_age_at_replan_ms=(deadline-b['capture_started_monotonic_ns'])/1e6,
                    held=request['prediction_replay']['last_submitted16']))
  wanted[a['observation_id']]=a;wanted[b['observation_id']]=b
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as tar:
 def add(name,data):
  info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
 for oid,obs in wanted.items():
  obs['images']={};obs['image_sha256']={}
  for frame in obs['frames']:
   with (root/frame['path']).open('rb') as f:
    f.seek(frame['payload_offset']);data=f.read(frame['payload_bytes'])
   digest=hashlib.sha256(data).hexdigest();assert digest==frame['payload_sha256']
   name=f"images/{oid}/{frame['camera']}.jpg"
   add(name,data);obs['images'][frame['camera']]=name;obs['image_sha256'][frame['camera']]=digest
 add('observations.json',json.dumps(wanted).encode())
 add('pairs.json',json.dumps(pairs).encode())
'''


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    events=ROOT/'Eval/artifacts/20261019-v1/f1-journals'/EPISODE/'journal/events.jsonl'
    rows=[json.loads(l) for l in events.read_text().splitlines()]
    timings=[r['timing'] for r in rows if r['event']=='policy_transport_timing']
    session=timings[0]['session_id']
    log=ROOT/'artifacts/20261016-v1-eef/service-f1-toys-compare-5000/requests.jsonl'
    requests=[json.loads(l) for l in log.open()]
    requests=[r for r in requests if r.get('session_id')==session and 'prediction_replay' in r]
    chosen=requests[::4]
    selected=dict(episode=EPISODE,requests=chosen)
    config=ROOT.parents[1]/'F1-4242/results/pose_task_20260928/ssh_config'
    result=subprocess.run(['ssh','-F',str(config),'f1-09-deploy','python3 -c '+shlex.quote(REMOTE)],
        input=json.dumps(selected).encode(),stdout=subprocess.PIPE,check=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout),mode='r:') as tar:
        for member in tar:
            path=a.output/member.name
            if not member.isfile() or a.output.resolve() not in path.resolve().parents:
                raise ValueError('Unsafe archive')
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write(tar.extractfile(member).read())
    (a.output/'manifest.json').write_text(json.dumps(dict(episode=EPISODE,session=session,
        events_sha256=hashlib.sha256(events.read_bytes()).hexdigest(),request_ids=[r['request_id'] for r in chosen],
        source_model_versions=sorted({r['model_version'] for r in chosen}),
        all_frame_hashes_verified=True,robot_commands=0,
        limitation='Historical sync observations do not react to hypothetical RTC actions.'),indent=2))
    (a.output/'historical-timings.json').write_text(json.dumps(timings,indent=2))
    print(json.dumps(dict(pairs=len(chosen)*3,source_requests=len(requests),copied_bytes=len(result.stdout))))


if __name__=='__main__':main()
