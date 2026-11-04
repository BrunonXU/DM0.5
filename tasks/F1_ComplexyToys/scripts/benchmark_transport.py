#!/usr/bin/env python3
"""Zero-output F1/6000D transport benchmark, live subscriptions or recorded replay."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import threading
import uuid
from urllib.request import urlopen

import numpy as np
from websockets.sync.client import connect

from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.observation import Observation
from f1dagger.websocket_policy import encode_request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--endpoint',required=True)
    p.add_argument('--dataset',type=Path)
    p.add_argument('--live-config',type=Path)
    p.add_argument('--count',type=int,default=500)
    p.add_argument('--echo',action='store_true')
    p.add_argument('--rtc',action='store_true',help='Measure full RTC RPC or same-payload echo, zero output')
    p.add_argument('--rtc-pixels',type=Path,help='Frozen preprocessing assets for lossless model-pixel transport')
    p.add_argument('--continuous-capture',action='store_true',help='Drain ROS observations independently, as the runtime capture thread does')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    health=json.load(urlopen(a.endpoint+'/health',timeout=3))
    encoder=None
    if a.rtc_pixels:
        from f1dagger.rtc_pixels import PixelEncoder
        encoder=PixelEncoder(a.rtc_pixels,health['input_preprocessing'])
    transport=source=None
    capture_thread=None
    latest={}
    capture_stop=threading.Event()
    reports=[]
    try:
        if a.live_config:
            from f1dagger.config import load_config,infra_import
            from f1dagger.observation import make_camera_store,RosObservationSource
            cfg,profile=load_config(a.live_config)
            adapter=infra_import(cfg['_infra_root'],'robot.adapter')
            store=make_camera_store(adapter.StateStore)
            transport=adapter.RosTransport(profile,store)
            transport.start()
            source=RosObservationSource(store,profile,cfg)
            deadline=time.monotonic()+5
            while True:
                try:source.capture(time.monotonic_ns());break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.1)
            assert not transport.publishers
            if a.continuous_capture:
                def acquire():
                    while not capture_stop.is_set():
                        try: latest['observation']=source.capture(time.monotonic_ns())
                        except Exception as exc:
                            if not capture_stop.is_set():latest['error']=exc
                            break
                capture_thread=threading.Thread(target=acquire,daemon=True)
                capture_thread.start()
                deadline=time.monotonic()+5
                while 'observation' not in latest:
                    if 'error' in latest:raise latest['error']
                    if time.monotonic()>deadline:raise TimeoutError('continuous capture warmup')
                    time.sleep(.005)
        else:
            if not a.dataset:raise ValueError('select live subscriptions or recorded dataset')
            rows=[json.loads(x) for x in (a.dataset/'samples.jsonl').read_text().splitlines()]
            with np.load(a.dataset/'arrays.npz') as arrays:
                states,submitted=arrays['states'],arrays['submitted']
        endpoint=a.endpoint.replace('https://','wss://',1).replace('http://','ws://',1)+('/echo' if a.echo else '/ws')
        session=uuid.uuid4().hex
        previous=None
        with connect(endpoint,compression=None,proxy=None,open_timeout=5,close_timeout=1,max_size=4*1024**2,max_queue=1) as ws:
            for i in range(a.count):
                capture_begin=time.perf_counter()
                if source:
                    if 'error' in latest:raise latest['error']
                    obs=latest['observation'] if a.continuous_capture else source.capture(time.monotonic_ns())
                    held=np.asarray(obs.state16)
                else:
                    index=(i*137)%len(rows);now=time.monotonic_ns()
                    frames=[]
                    for camera,rel in rows[index]['images'].items():
                        size=(1280,720) if camera=='head' else (640,480)
                        frames.append(TimestampedFrame(camera,i,DeviceTimestamp(now,'ns','recorded_replay'),now+1,
                            (a.dataset/rel).read_bytes(),'image/jpeg',*size))
                    order={'head':0,'left_wrist':1,'right_wrist':2};frames.sort(key=lambda f:order[f.camera])
                    obs=Observation(i,now,now+2,tuple(frames),tuple(states[index]),(now,)*16,(0.,)*22)
                    held=submitted[index,0]
                captured=time.perf_counter()
                metadata=dict(contract='f1_upper_16d_v1',protocol='f1_eef_sync_v2',request_id=i,
                    observation_id=obs.observation_id,policy_epoch=0,session_id=session,
                    observation_monotonic_ns=obs.capture_started_monotonic_ns,state16=[float(value) for value in obs.state16],
                    last_submitted16=held.tolist(),prediction_horizon=50,execute_steps=20,chunk_steps=20,frequency_hz=30.,
                    model_id=health['model_id'],model_version=health['model_version'],
                    shadow_only=True,hold_source='measured_shadow_placeholder' if source else 'recorded_submitted')
                if a.rtc:
                    bootstrap=previous is None or not previous.get('success') or a.echo
                    metadata.update(protocol='f1_eef_rtc_v2',request_id=i+1,
                        rtc=dict(bootstrap=bootstrap,delay_steps=15,shift_steps=20,coverage_steps=37,
                            plan_origin_ns=time.monotonic_ns(),base_plan_id=0 if bootstrap else i,
                            committed_actions16=[] if bootstrap else previous['actions'][20:35],
                            previous_absolute_eef10=[] if bootstrap else previous['absolute_eef10'][20:]))
                frames=obs.frames
                if encoder:
                    frames=encoder.encode(frames);metadata['prepared_pixels']=encoder.identity
                payload=encode_request(metadata,frames);packed=time.perf_counter()
                ws.send(payload);response=json.loads(ws.recv(timeout=10));finished=time.perf_counter()
                previous=response
                for key in ('request_id','observation_id','policy_epoch','session_id','model_id','model_version'):
                    if response.get(key)!=metadata[key]:raise ValueError('response identity mismatch: '+key)
                handler=response.get('timing_ms',{}).get('handler',0)
                report=dict(request_id=i,success=response.get('success'),reason=response.get('reason'),
                    capture_ms=(captured-capture_begin)*1000,pack_ms=(packed-captured)*1000,rtt_ms=(finished-packed)*1000,
                    observation_age_at_send_ms=(time.monotonic_ns()-obs.capture_started_monotonic_ns)/1e6-(finished-packed)*1000,
                    residual_ms=(finished-packed)*1000-handler,observation_to_response_ms=(time.monotonic_ns()-obs.capture_started_monotonic_ns)/1e6,
                    server=response.get('timing_ms'),payload_bytes=len(payload),state_diagnostics=response.get('state_diagnostics'))
                reports.append(report)
                with (a.output/'requests.jsonl').open('a') as f:f.write(json.dumps(report)+'\n')
                if i%50==0:print(json.dumps(report),flush=True)
        percentiles={k:dict(zip(('p50','p95','p99'),np.percentile([r[k] for r in reports[5:]], [50,95,99]).tolist()))
                     for k in ('rtt_ms','residual_ms','observation_to_response_ms','pack_ms')} if len(reports)>5 else {}
        result=dict(completed=len(reports),requested=a.count,source='live_ros' if source else 'recorded_replay',
                    continuous_capture=a.continuous_capture,
                    echo=a.echo,robot_commands=0,control_owner_created=False,success_rate=float(np.mean([r['success'] for r in reports])),
                    percentiles=percentiles,health=health)
        (a.output/'report.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    except Exception as exc:
        (a.output/'failure.json').write_text(json.dumps(dict(completed=len(reports),robot_commands=0,error=f'{type(exc).__name__}: {exc}'),indent=2)+'\n')
        raise
    finally:
        capture_stop.set()
        if source:source.stop_capture()
        if capture_thread:capture_thread.join(timeout=3)
        if transport:
            assert not transport.publishers
            transport.close()


if __name__=='__main__':main()
