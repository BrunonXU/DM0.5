#!/usr/bin/env python3
"""Real stored images through the production client/WS child; no robot IO.

Replay timestamps use an explicit diagnostic clock. Simulated commitments and
these server requests must never be admitted as physical training transitions.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.request import urlopen

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dagger',type=Path,required=True)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--endpoint',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    sys.path[:0]=[str(a.dagger/'F1/src'),str(a.dagger/'shared/src')]
    import numpy as np
    from f1dagger.rlt_policy import RLTPolicy
    from f1_toys.rlt_priority import read_frame
    from f1_toys.rlt_local_pipeline import FrozenFeatureClient
    from f1_toys.rlt_online import atomic_json
    cfg=json.loads((a.data/'candidate-data-config.json').read_text())['f1']
    entries=json.loads((a.data/'migration.json').read_text())['sources']
    health=json.load(urlopen(a.endpoint+'/rlt/health',timeout=5))
    assert health['rtc'] and health['ready']
    client_cfg=dict(endpoint=a.endpoint+'/rlt',timeout_s=5.,max_age_s=2.,chunk_steps=16,
        frequency_hz=30.,model_id=health['model_id'],model_version=health['model_version'],
        expected_rlt=health['rlt'],require_capabilities=True,shadow_only=True,
        rlt_rtc=True,allow_boundary_updates=True)
    probes=[];versions=[];rid=0
    a.output.mkdir(parents=True,exist_ok=False)
    client=None
    for entry in (entries[0],entries[len(entries)//2],entries[-1]):
        raw=Path(cfg['raw_root'])/entry['episode']
        rows=list(map(json.loads,(Path(entry['cache'])/'transitions.jsonl').read_text().splitlines()))
        oid=next(r['observation_id'] for r in rows if r['decision_kind']=='actor_request')
        frame=next(r for r in map(json.loads,(raw/'payload/frames.jsonl').read_text().splitlines()) if r['observation_id']==oid)
        obsrow=next(r for r in map(json.loads,(raw/'journal/observations.jsonl').read_text().splitlines()) if r['observation_id']==oid)
        frames=tuple(SimpleNamespace(**{k:v for k,v in f.items() if k not in ('payload','media_type')},
            payload=read_frame(raw/'payload',f),media_type=f.get('media_type','image/jpeg')) for f in frame['frames'])
        obs=SimpleNamespace(**dict(obsrow,frames=frames,
            state_timestamps_ns=[v or 0 for v in obsrow['state_timestamps_ns']]))
        state=np.asarray(frame['state16'])
        if client is None:
            client=RLTPolicy(client_cfg,'stored-image zero-output RTC acceptance',lambda _:None,
                held_getter=lambda:state,clock=lambda:obs.available_monotonic_ns)
            client.health();client.prepare()
        for stage in ('initial','boundary_candidate'):
            if stage=='boundary_candidate':
                limit=time.monotonic()+5
                while client._pending_identity is None and time.monotonic()<limit:time.sleep(.1)
                assert client.adopt_pending(),client.candidate_error
            client.local_segment_id=entry['episode']+'-zero-output-'+stage
            context=dict(bootstrap=True,delay_steps=6,shift_steps=8,coverage_steps=16,
                plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[],
                rlt=True,next_decision_steps=8)
            fixed=client.model_version;versions.append(dict(stage=stage,**client.rlt_identity))
            for index in range(4):
                rid+=1
                actions,absolute=client.infer_rtc(obs,rid,42,context)
                exact=not context['committed_actions16'] or np.array_equal(actions[:6],context['committed_actions16'])
                assert exact and client.model_version==fixed
                probes.append(dict(episode=entry['episode'],stage=stage,request_id=rid,
                    model_version=fixed,committed_prefix_exact=exact,timing=client.last_timing))
                context=dict(context,bootstrap=False,base_plan_id=rid,plan_origin_ns=1000+rid,
                    committed_actions16=actions[8:14].tolist(),previous_absolute_eef10=absolute[8:].tolist())
        # Give the priority service its normal quiet interval before asking for
        # eager frozen features, then verify the old feature contract still works.
        time.sleep(2.1)
        feature=FrozenFeatureClient(a.endpoint.replace('http:','ws:')+'/rlt/features',cfg['replay']['feature_identity'])
        values=feature(obsrow,frame,raw/'payload')
        assert set(values)=={'z_rl','proprio','ref_chunk'}
    client.close()
    latency=[r['timing']['client_total_ms'] for r in probes]
    atomic_json(a.output/'report.json',dict(passed=True,requests=len(probes),probes=probes,
        versions=versions,client_median_ms=float(np.median(latency)),client_p95_ms=float(np.percentile(latency,95)),
        feature_contract_verified=True,robot_commands=0,training_admission=False,
        scope='stored images and simulated commitments; loopback network; not ARM or physical RTC'))
    print(json.dumps(dict(passed=True,requests=len(probes),median_ms=float(np.median(latency)),versions=sorted({r['model_version'] for r in probes}))))


if __name__=='__main__':main()
