"""Fetch exact historical failure observations read-only; no robot commands."""
import hashlib,io,json,shlex,subprocess,tarfile
from pathlib import Path
TASK=Path(__file__).resolve().parents[1]
PROJECTS=TASK.parents[1]
OUT=TASK/'Eval/artifacts/20261021-ik-recovery-next/resample-inputs'
OUT.mkdir(parents=True,exist_ok=False)
source=TASK/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
rows=[json.loads(l) for l in source.read_text().splitlines()]
selected=[dict(episode=r['episode'],reason='original_ik_rejection',request=r['server']) for r in rows if not r['server']['success']]
REMOTE='\nimport json,sys,tarfile,io,hashlib\nfrom pathlib import Path\nselected=json.load(sys.stdin)\nroot=Path(\'/home/user/Bruno/data/hil_data/policy_hil\')\nepisodes=sorted({s[\'episode\'] for s in selected})\nindex={}\nfor episode in episodes:\n path=root/episode/\'payload/frames.jsonl\'\n for line in path.open():\n  row=json.loads(line)\n  index[(row[\'observation_id\'],row[\'capture_started_monotonic_ns\'])]=(episode,row)\nrecords=[]\nwith tarfile.open(fileobj=sys.stdout.buffer,mode=\'w|\') as tar:\n seen=set()\n for selection in selected:\n  request=selection[\'request\']\n  episode,obs=index[(request[\'observation_id\'],request[\'observation_monotonic_ns\'])]\n  replay=request[\'prediction_replay\']\n  assert obs[\'state16\']==replay[\'state16\'], \'state mismatch\'\n  assert obs[\'state_timestamps_ns\']==replay[\'state_timestamps_ns\'], \'state timestamp mismatch\'\n  images={};hashes={}\n  for stored,wire in zip(obs[\'frames\'],replay[\'frames\']):\n   for key in (\'camera\',\'host_received_monotonic_ns\'):\n    assert stored[key]==wire[key], \'frame identity mismatch\'\n   # RTC uses processed RGB224 PNG on wire; original frame identity remains in journal.\n   with (root/episode/\'payload\'/stored[\'path\']).open(\'rb\') as f:\n    f.seek(stored[\'payload_offset\']);data=f.read(stored[\'payload_bytes\'])\n   digest=hashlib.sha256(data).hexdigest();assert digest==stored[\'payload_sha256\']\n   name=f"images/{request[\'session_id\']}/{request[\'request_id\']}/{stored[\'camera\']}.jpg"\n   if name not in seen:\n    info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data));seen.add(name)\n   images[stored[\'camera\']]=name;hashes[stored[\'camera\']]=digest\n  records.append(dict(episode=episode,reason=selection[\'reason\'],request_id=request[\'request_id\'],observation=obs,images=images,image_sha256=hashes))\n data=(\'\'.join(json.dumps(r)+\'\\n\' for r in records)).encode()\n info=tarfile.TarInfo(\'image-index.jsonl\');info.size=len(data);tar.addfile(info,io.BytesIO(data))\n'
result=subprocess.run(['ssh','-F',str(PROJECTS/'F1-4242/results/pose_task_20260928/ssh_config'),'f1-09-deploy','python3 -c '+shlex.quote(REMOTE)],input=json.dumps(selected).encode(),stdout=subprocess.PIPE,check=True)
with tarfile.open(fileobj=io.BytesIO(result.stdout),mode='r:') as tar:
 for m in tar:
  target=OUT/m.name
  if not m.isfile() or OUT.resolve() not in target.resolve().parents: raise ValueError('Unsafe archive')
  target.parent.mkdir(parents=True,exist_ok=True)
  with target.open('xb') as f: f.write(tar.extractfile(m).read())
(OUT/'requests.json').write_text(json.dumps(selected))
(OUT/'manifest.json').write_text(json.dumps(dict(source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),requests=len(selected),verified=['state16','state_timestamps_ns','frame_camera','frame_host_timestamp','original_frame_payload_sha256'],robot_commands=0,limitation='Original JPEG frames recovered; no recorded noise seeds; frozen observations do not react to alternative actions.'),indent=2))
print(json.dumps(dict(requests=len(selected),archive_bytes=len(result.stdout))),flush=True)
