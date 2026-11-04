#!/usr/bin/env python3
"""Stored real images, RTC prefix proof and shared-VLA isolation; zero output."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('bundle','vla-core','rlt-core','encoder','head','data','output'):
        p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args()
    import numpy as np
    import torch
    from PIL import Image
    torch.set_num_threads(4)
    os.environ['F1_TOYS_TOKENIZER']=str(a.bundle/'paligemma_tokenizer.model')
    spec=importlib.util.spec_from_file_location('rtc_compute_wrapper',TASK/'scripts/h20_rtc_fast_server.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    from f1_toys.inference import Policy
    from f1_toys.rlt_inference import RLTInference
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.rlt_priority import read_frame
    from f1_toys.rlt_online import atomic_json,sha
    a.output.mkdir(parents=True,exist_ok=False)
    cfg=json.loads((a.data/'candidate-data-config.json').read_text())['f1']
    sources=json.loads((a.data/'migration.json').read_text())['sources']
    policy=Policy(a.bundle,a.vla_core)
    sidecar=RLTInference(policy,a.head,a.encoder,a.rlt_core,rtc=True)
    compile_suffix(policy)
    sidecar.configure_reference_compute('serving_graph_prefix_and_suffix')
    reports=[]
    for entry in (sources[0],sources[len(sources)//2],sources[-1]):
        rows=list(map(json.loads,(Path(entry['cache'])/'transitions.jsonl').read_text().splitlines()))
        oid=next(r['observation_id'] for r in rows if r['decision_kind']=='actor_request')
        raw=Path(cfg['raw_root'])/entry['episode']
        frame=next(r for r in map(json.loads,(raw/'payload/frames.jsonl').read_text().splitlines()) if r['observation_id']==oid)
        images={f['camera']:Image.open(io.BytesIO(read_frame(raw/'payload',f))).convert('RGB') for f in frame['frames']}
        state=frame['state16'];seed=20261102
        bctx=dict(bootstrap=True,delay_steps=14,shift_steps=16,coverage_steps=32,
            plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
        base,braw,_=policy.predict_rtc(images,state,state,bctx,steps=5,seed=seed)
        sidecar.warm(images,state,state)
        frozen=sidecar.predict(images,state,state,seed=seed,features_only=True)
        context=dict(bootstrap=True,delay_steps=6,shift_steps=8,coverage_steps=16,
            plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[],rlt=True)
        for iteration in range(12):
            decoded,actions,replay=sidecar.predict(images,state,state,seed=seed,rtc=context)
            if not decoded['success']:
                raise ValueError('RTC real-observation candidate IK rejected: '+str(decoded['diagnostics'][-1]))
            prefix=np.asarray(context['committed_actions16'])
            exact=not len(prefix) or np.array_equal(decoded['actions'][:len(prefix)],prefix)
            item=dict(episode=entry['episode'],observation_id=oid,iteration=iteration,
                bootstrap=context['bootstrap'],committed_prefix_exact=exact,
                recorded_mask_count=sum(replay['committed_mask']),timing_ms=decoded['timing_ms'])
            reports.append(item);print(json.dumps(item),flush=True)
            if not exact:raise ValueError('Committed prefix changed')
            # These are deliberately simulated future commitments, not claims
            # of physical execution or samples eligible for the replay buffer.
            context=dict(context,bootstrap=False,base_plan_id=iteration+1,
                previous_absolute_eef10=decoded['absolute_eef10'][8:],
                committed_actions16=decoded['actions'][8:14],plan_origin_ns=100+iteration)
            atomic_json(a.output/'partial-report.json',dict(probes=reports,complete=False))
        again=sidecar.predict(images,state,state,seed=seed,features_only=True)
        after,araw,_=policy.predict_rtc(images,state,state,bctx,steps=5,seed=seed)
        if not np.array_equal(braw,araw) or frozen!=again or base['actions']!=after['actions']:
            raise ValueError('RLT RTC contaminated shared frozen VLA/features')
    fixed=all(not p.requires_grad and p.grad is None for model in
              (policy.model,sidecar.reader.encoder,sidecar.actor) for p in model.parameters())
    timings=[r['timing_ms']['policy'] for r in reports if r['iteration']>=2]
    atomic_json(a.output/'report.json',dict(passed=fixed,probes=reports,all_parameters_frozen=fixed,
        policy_median_ms=float(np.median(timings)),policy_p95_ms=float(np.percentile(timings,95)),
        source_sha256=sha(__file__),parameter_updates=0,robot_commands=0,deployed=False,
        scope='real images with simulated commitments; not physical RTC or training data'))
    if not fixed:raise ValueError('Unexpected trainable state')


if __name__=='__main__':main()
