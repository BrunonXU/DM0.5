"""Historical-image inference only: verify real server version pinning and rollback.

No robot transport, command publisher, reset, or new episode is created.
"""
import argparse,json,struct,time,uuid
from pathlib import Path
from urllib.request import urlopen


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--validation-cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();cfg=json.loads(args.config.read_text())['f1']
    from websockets.sync.client import connect
    endpoint=cfg['feature_endpoint'].removesuffix('/rlt/features')
    http=endpoint.replace('ws://','http://')
    baseline=json.load(urlopen(http+'/rlt/health'))
    candidate=None
    for _ in range(20):
        candidate=json.load(urlopen(http+'/rlt/candidates')).get('candidate')
        if candidate:break
        time.sleep(.5)
    assert candidate is not None
    record=json.loads((args.validation_cache/'transitions.jsonl').read_text().splitlines()[0])
    manifest=json.loads((args.validation_cache/'manifest.json').read_text())
    run=Path(cfg['raw_root'])/manifest['episode_id'];oid=record['observation_id']
    obs=next(json.loads(line) for line in (run/'journal/observations.jsonl').read_text().splitlines() if json.loads(line)['observation_id']==oid)
    payload=next(json.loads(line) for line in (run/'payload/frames.jsonl').read_text().splitlines() if json.loads(line)['observation_id']==oid)
    frames,blobs=[],[]
    for f in payload['frames']:
        with (run/'payload'/f['path']).open('rb') as handle:
            handle.seek(f.get('payload_offset',0));blob=handle.read(f['payload_bytes'])
        assert len(blob)==f['payload_bytes']
        frames.append(dict(camera=f['camera'],encoding=f.get('media_type','image/jpeg'),width=f['width'],height=f['height'],length=len(blob),host_received_monotonic_ns=f['host_received_monotonic_ns']))
        blobs.append(blob)
    session='offline-boundary-'+uuid.uuid4().hex;responses=[]
    def request(identity,segment,request_id):
        meta=dict(contract='f1_upper_16d_v1',protocol='f1_eef_rlt_sync_v1',session_id=session,
            local_segment_id=session+':'+segment,request_id=request_id,observation_id=oid,policy_epoch=request_id,
            observation_monotonic_ns=obs['capture_started_monotonic_ns'],state16=obs['state16'],
            last_submitted16=record['submitted16'][0],state_timestamps_ns=[0 if x is None else x for x in obs['state_timestamps_ns']],
            state_valid_mask=obs['state_valid_mask'],absent_state_semantics='zero_storage_placeholder_not_measurement',
            chunk_steps=16,prediction_horizon=16,reference_horizon=50,execute_steps=16,frequency_hz=30.,
            control_mode='rlt_hil_supervised',shadow_only=True,model_id=identity['model_id'],model_version=identity['model_version'],frames=frames)
        raw=json.dumps(meta).encode()
        with connect(endpoint+'/rlt/infer',max_size=4*1024**2,proxy=None,open_timeout=10) as ws:
            ws.send(struct.pack('!I',len(raw))+raw+b''.join(blobs));response=json.loads(ws.recv(timeout=15))
        responses.append({k:response.get(k) for k in ('success','error','reason','model_version','local_segment_id','proposal_only','shadow_only')})
        return response
    try:
        first=request(candidate,'one',1);assert first.get('model_version')==candidate['model_version'] and first.get('proposal_only') is True
        repeated=request(candidate,'one',2);assert repeated.get('model_version')==candidate['model_version']
        rejected=request(baseline,'one',3);assert rejected.get('success') is False and 'inside a local task' in str(rejected)
        restored=request(baseline,'two',4);assert restored.get('model_version')==baseline['model_version']
        adopted=request(candidate,'three',5);assert adopted.get('model_version')==candidate['model_version']
    finally:
        request(baseline,'restore',6)
    args.output.write_text(json.dumps(dict(passed=True,candidate=candidate['model_version'],baseline=baseline['model_version'],responses=responses,
        robot_actions_sent=0,historical_observation_id=oid),indent=2)+'\n')
    print(json.dumps({'passed':True,'requests':len(responses),'robot_actions_sent':0,'candidate':candidate['model_version']}))


if __name__=='__main__':main()
