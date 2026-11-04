#!/usr/bin/env python3
"""Check B RTC, fast RLT and frozen feature requests sharing one model.

Uses stored real observations on an isolated GPU. No sockets or robot output.
"""
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
    spec=importlib.util.spec_from_file_location('shared_compute_wrapper',TASK/'scripts/h20_rtc_fast_server.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    from f1_toys.inference import Policy
    from f1_toys.rlt_inference import RLTInference
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.rlt_priority import read_frame
    from f1_toys.rlt_online import atomic_json,sha
    a.output.mkdir(parents=True,exist_ok=False)
    cfg=json.loads((a.data/'candidate-data-config.json').read_text())['f1']
    sources=json.loads((a.data/'migration.json').read_text())['sources']
    policy=Policy(a.bundle,a.vla_core)
    sidecar=RLTInference(policy,a.head,a.encoder,a.rlt_core)
    compile_suffix(policy)
    sidecar.configure_reference_compute('serving_graph_prefix_and_suffix')
    reports=[]
    for entry in (sources[0],sources[len(sources)//2],sources[-1]):
        rows=list(map(json.loads,(Path(entry['cache'])/'transitions.jsonl').read_text().splitlines()))
        oid=next(r['observation_id'] for r in rows if r['decision_kind']=='actor_request')
        raw=Path(cfg['raw_root'])/entry['episode']
        row=next(r for r in map(json.loads,(raw/'payload/frames.jsonl').read_text().splitlines()) if r['observation_id']==oid)
        images={f['camera']:Image.open(io.BytesIO(read_frame(raw/'payload',f))).convert('RGB') for f in row['frames']}
        state=row['state16'];seed=20261102
        bootstrap=dict(bootstrap=True,delay_steps=14,shift_steps=16,coverage_steps=32,
            plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
        first,_,_=policy.predict_rtc(images,state,state,bootstrap,steps=5,seed=seed)
        guided=dict(bootstrap,bootstrap=False,base_plan_id=1,
            previous_absolute_eef10=first['absolute_eef10'][16:],committed_actions16=[state]*14)
        # Warm both RTC traces before comparing shared state.
        policy.predict_rtc(images,state,state,guided,steps=5,seed=seed)
        sidecar.warm(images,state,state)
        features=sidecar.predict(images,state,state,seed=seed,features_only=True)
        for iteration in range(4):
            context=bootstrap if iteration%2==0 else guided
            before,braw,_=policy.predict_rtc(images,state,state,context,steps=5,seed=seed)
            fast,_,replay=sidecar.predict(images,state,state,seed=seed)
            again=sidecar.predict(images,state,state,seed=seed,features_only=True)
            after,araw,_=policy.predict_rtc(images,state,state,context,steps=5,seed=seed)
            item=dict(episode=entry['episode'],observation_id=oid,iteration=iteration,
                bootstrap=context['bootstrap'],base_raw_exact=bool(np.array_equal(braw,araw)),
                base_decoded_exact=before['actions']==after['actions'],
                frozen_features_exact=features==again,fast_ik_success=fast['success'],
                recorded_compute_path=replay['reference_compute_path'])
            reports.append(item);print(json.dumps(item),flush=True)
            atomic_json(a.output/'partial-report.json',dict(probes=reports,complete=False))
    frozen=all(not p.requires_grad and p.grad is None for model in
               (policy.model,sidecar.reader.encoder,sidecar.actor) for p in model.parameters())
    passed=frozen and all(r['base_raw_exact'] and r['base_decoded_exact'] and r['frozen_features_exact'] for r in reports)
    atomic_json(a.output/'report.json',dict(passed=passed,probes=reports,all_parameters_frozen=frozen,
        source_sha256=sha(__file__),parameter_updates=0,robot_commands=0,deployed=False,
        scope='shared compute state isolation; not continuous C16 execution or onsite acceptance'))
    if not passed:raise ValueError('Shared compute state changed across inference modes')


if __name__=='__main__':main()
