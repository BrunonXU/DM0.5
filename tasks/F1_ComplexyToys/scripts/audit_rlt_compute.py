#!/usr/bin/env python3
"""Compare eager and shared graph RLT math on real stored observations.

No server, ROS, weight updates, or deployment. The identical sampler, noise and
actor are used; a failed equality check cannot silently enable optimization.
"""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import numpy as np
import torch

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'src'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('bundle','vla-core','rlt-core','encoder','head','output'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--dataset',type=Path)
    p.add_argument('--decision-data',type=Path)
    p.add_argument('--repeats',type=int,default=3)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    os.environ['F1_TOYS_TOKENIZER']=str(args.bundle/'paligemma_tokenizer.model')
    torch.set_num_threads(4)
    spec=importlib.util.spec_from_file_location('rlt_compute_wrapper',TASK/'scripts/h20_rtc_fast_server.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    from f1_toys.inference import Policy
    from f1_toys.rlt_inference import RLTInference
    from f1_toys.dataset import F1Dataset
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.rtc_prefix_optimization import prefix_graph_status
    from f1_toys.rlt_online import sha,atomic_json
    policy=Policy(args.bundle,args.vla_core)
    sidecar=RLTInference(policy,args.head,args.encoder,args.rlt_core)
    if (args.dataset is None)==(args.decision_data is None):
        raise ValueError('Select one real input source')
    if args.repeats<1:raise ValueError('Positive measurement count required')
    compile_suffix(policy)
    from f1_toys.eef import physical_actions,relative_pose
    from PIL import Image
    probes=[]
    if args.dataset:
        dataset=F1Dataset(args.dataset)
        for i in sorted({0,len(dataset)//2,len(dataset)-1}):
            probes.append((dict(dataset_index=i),dataset.raw_images(i),dataset.states[i]))
    else:
        from f1_toys.rlt_priority import read_frame
        config=json.loads((args.decision_data/'candidate-data-config.json').read_text())['f1']
        migration=json.loads((args.decision_data/'migration.json').read_text())
        for entry in migration['sources']:
            raw=Path(config['raw_root'])/entry['episode']
            rows=list(map(json.loads,(Path(entry['cache'])/'transitions.jsonl').read_text().splitlines()))
            actor=[r for r in rows if r['decision_kind']=='actor_request']
            ids=list(dict.fromkeys([actor[0]['observation_id'],actor[-1]['observation_id'],rows[-1]['next_observation_id']]))
            payload={r['observation_id']:r for r in map(json.loads,(raw/'payload/frames.jsonl').read_text().splitlines())}
            for oid in ids:
                row=payload[oid];images={}
                for frame in row['frames']:
                    with Image.open(io.BytesIO(read_frame(raw/'payload',frame))) as im:images[frame['camera']]=im.convert('RGB')
                probes.append((dict(episode=entry['episode'],observation_id=oid),images,row['state16']))
    modes=('training_eager_prefix_and_suffix','serving_graph_prefix_eager_suffix','serving_graph_prefix_and_suffix')
    def physical_difference(left,right):
        a,b=(physical_actions(v,policy.stats['action']) for v in (left,right))
        angles=[]
        for x,y in zip(a,b):
            r,s=relative_pose(x)[:3,:3],relative_pose(y)[:3,:3]
            angles.append(np.rad2deg(np.arccos(np.clip((np.sum(r*s)-1)/2,-1,1))))
        return dict(xyz_max_mm=float(np.linalg.norm(a[:,:3]-b[:,:3],axis=1).max()*1000),
                    rotation_max_deg=float(max(angles)),
                    gripper_max_units=float(np.max(np.abs(np.clip(a[:,9]*50+50,0,100)-np.clip(b[:,9]*50+50,0,100)))))
    results=[]
    for index,(identity,images,state) in enumerate(probes):
        outputs={}; timings={};decoded_plans={}
        for mode in modes:
            sidecar.reference_compute_path=mode
            # First graph invocation may compile; it is not a latency sample.
            for _ in range(3 if index==0 else 1):sidecar.predict(images,state,state,seed=20261102+index)
            samples=[]
            for _ in range(args.repeats):
                torch.cuda.synchronize();start=time.perf_counter()
                decoded,action,replay=sidecar.predict(images,state,state,seed=20261102+index)
                torch.cuda.synchronize();samples.append(dict(wall_ms=(time.perf_counter()-start)*1000,**decoded['timing_ms']))
            outputs[mode]={key:np.asarray(replay[key]) for key in ('z_rl','proprio','reference_normalized_actions32','actor_normalized_actions32')}
            timings[mode]=samples
            decoded_plans[mode]=decoded
        a=outputs['training_eager_prefix_and_suffix']
        comparisons={mode:{key:dict(exact=bool(np.array_equal(a[key],b[key])),max_abs=float(np.max(np.abs(a[key]-b[key])))) for key in a}
                     for mode,b in outputs.items() if mode!='training_eager_prefix_and_suffix'}
        physical={}
        for mode in modes[1:]:
            original,optimized=decoded_plans[modes[0]],decoded_plans[mode]
            item=physical_difference(a['actor_normalized_actions32'],outputs[mode]['actor_normalized_actions32'])
            item.update(ik_original=original['success'],ik_optimized=optimized['success'])
            if original['success'] and optimized['success']:
                x,y=np.asarray(original['actions']),np.asarray(optimized['actions'])
                item.update(joint_max_deg=float(np.abs(x[:,7:14]-y[:,7:14]).max()),
                    joint_increment_difference_deg=float(np.abs(np.diff(x[:,7:14],axis=0)-np.diff(y[:,7:14],axis=0)).max()))
            physical[mode]=item
        results.append(dict(index=index,source=identity,comparisons=comparisons,physical=physical,timings=timings))
        print(json.dumps(dict(index=index,source=identity,physical=physical)),flush=True)
        atomic_json(args.output/'partial-report.json',dict(probes=results,complete=False))
    summary={}
    for mode in modes[1:]:
        values=[r['physical'][mode] for r in results]
        summary[mode]=dict(probes=len(values),
            maxima={k:max(v.get(k,0) for v in values) for k in ('xyz_max_mm','rotation_max_deg','gripper_max_units','joint_max_deg','joint_increment_difference_deg')},
            ik_new_rejections=sum(v['ik_original'] and not v['ik_optimized'] for v in values),
            ik_changed=sum(v['ik_original']!=v['ik_optimized'] for v in values),
            model_median_ms=float(np.median([t['model_h2d_d2h'] for r in results for t in r['timings'][mode]])))
    atomic_json(args.output/'report.json',dict(probes=results,
        physical_summary=summary,
        eager_model_median_ms=float(np.median([t['model_h2d_d2h'] for r in results for t in r['timings'][modes[0]]])),
        exact={mode:all(v['exact'] for row in results for v in row['comparisons'][mode].values())
               for mode in results[0]['comparisons']},
        graph=prefix_graph_status(policy),source_sha256=sha(__file__),
        robot_commands=0,parameter_updates=0,deployed=False,
        scope='stored real observations, compute equivalence and latency only; not RTC acceptance'))


if __name__=='__main__':main()
