"""F1 historical-input RPC only; no robot/camera imports or command publishers.

Requires the shared control lock to avoid competing with any powered session.
Every RPC is shadow_only. Uses the running Full1500 service without replacing it.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time
import uuid
from urllib.request import urlopen

import numpy as np
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.rtc_pixels import PixelEncoder
from f1dagger.websocket_policy import WebSocketPolicy, encode_request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--requests',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        endpoint='http://127.0.0.1:8154'
        health=json.load(urlopen(endpoint+'/health',timeout=5))
        assert health['model_id']=='f1-toys-eef-e312de43fa3875a8' and health['ready']
        assert health['protocol']=='f1_eef_rtc_v2' and health['denoise_steps']==5
        rows=json.loads(args.requests.read_text())
        assert len(rows)==16 and all(not r['success'] for r in rows)
        run=Path('/home/user/Bruno/data/hil_data/policy_hil/f1_hil_20261021T054754Z_e11beff0/payload')
        observations={r['observation_id']:r for r in map(json.loads,(run/'frames.jsonl').open())}
        encoder=PixelEncoder('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20/1500',health['input_preprocessing'])
        client=WebSocketPolicy(dict(endpoint=endpoint,timeout_s=10),'',lambda _:None,
                               held_getter=lambda:np.zeros(16))
        args.output.mkdir(parents=True,exist_ok=False)
        records=[];session=uuid.uuid4().hex
        try:
            with (args.output/'results.jsonl').open('x') as stream:
                for entry in rows:
                    obs=observations[entry['observation_id']]
                    assert obs['state16']==entry['prediction_replay']['state16']
                    assert obs['capture_started_monotonic_ns']==entry['observation_monotonic_ns']
                    frames=[];hashes={}
                    for frame in obs['frames']:
                        with (run/frame['path']).open('rb') as f:
                            f.seek(frame['payload_offset']);data=f.read(frame['payload_bytes'])
                        assert hashlib.sha256(data).hexdigest()==frame['payload_sha256']
                        hashes[frame['camera']]=frame['payload_sha256']
                        frames.append(TimestampedFrame(frame['camera'],frame['frame_id'],DeviceTimestamp(**frame['timestamp']),
                            frame['host_received_monotonic_ns'],data,frame['media_type'],frame['width'],frame['height']))
                    prepared=encoder.encode(frames)
                    for trial in range(3):
                        rid=entry['request_id']*10+trial
                        meta=dict(contract='f1_upper_16d_v1',protocol='f1_eef_rtc_v2',session_id=session,
                            request_id=rid,observation_id=obs['observation_id'],policy_epoch=0,
                            observation_monotonic_ns=obs['capture_started_monotonic_ns'],state16=obs['state16'],
                            state_timestamps_ns=obs['state_timestamps_ns'],last_submitted16=entry['prediction_replay']['last_submitted16'],
                            prediction_horizon=50,execute_steps=20,chunk_steps=20,frequency_hz=30.,shadow_only=True,
                            model_id=health['model_id'],model_version=health['model_version'],
                            prepared_pixels=encoder.identity,rtc=entry['rtc_request'])
                        payload=encode_request(meta,prepared);started=time.perf_counter()
                        result=client._exchange(payload)
                        for key in ('session_id','request_id','model_id','model_version','shadow_only'):
                            assert result[key]==meta[key],key
                        assert 'error' not in result and result['rtc_context']==entry['rtc_context']
                        if result['success']:
                            prefix=entry['rtc_request']['committed_actions16']
                            assert len(result['actions'])==entry['rtc_request']['coverage_steps']
                            if prefix:assert np.array_equal(np.array(result['actions'])[:len(prefix)],np.array(prefix))
                        else:assert result['reason']=='ik_rejected' and result['actions'] is None
                        record=dict(original_request_id=entry['request_id'],trial=trial,
                            image_sha256=hashes,payload_sha256=hashlib.sha256(payload).hexdigest(),
                            rpc_ms=(time.perf_counter()-started)*1000,result=result)
                        records.append(record);stream.write(json.dumps(record)+'\n');stream.flush()
                    print(json.dumps(dict(request_id=entry['request_id'],full=sum(r['result']['success'] for r in records[-3:]))),flush=True)
        finally:
            client.close();encoder.close()
        groups=[]
        for row in rows:
            fresh=[r for r in records if r['original_request_id']==row['request_id']]
            lengths=[len(r['result']['diagnostics'])-1 if not r['result']['success'] else
                     row['rtc_context']['coverage_steps']-len(row['rtc_request']['committed_actions16']) for r in fresh]
            groups.append(dict(request_id=row['request_id'],full_rescued=any(r['result']['success'] for r in fresh),
                valid_new_points=[len(row['diagnostics'])-1]+lengths,
                selected_trial=next((r['trial'] for r in fresh if r['result']['success']),None)))
        report=dict(cases=len(rows),new_model_calls=len(records),rescued_full=sum(r['full_rescued'] for r in groups),
            any_valid_new=sum(max(r['valid_new_points'])>0 for r in groups),groups=groups,health=health,
            robot_commands=0,online_changes=0,
            limitations=['Recorded images/state/prefix, unchanged Full1500 weights and original IK bounds.',
                'Three fresh stochastic RPCs plus the original rejected candidate; original noise not replayed.',
                'No prefix cache or 400ms wrapper budget tested; RPC selection is not deployment timing proof.',
                'Changed model sample, not a promise to preserve the first model output; no EEF projection.',
                'No actual motion, stopping, contact, new observations or task success verified.'])
        (args.output/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('health','groups')}),flush=True)


if __name__=='__main__':main()
