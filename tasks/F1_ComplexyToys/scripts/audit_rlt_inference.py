#!/usr/bin/env python3
"""GPU zero-output audit of the shared B/BC400 server inference paths.

Compare the immutable training feature function with the new sidecar using
identical observations and reference noise seeds. No socket or robot is used.
W&B is deliberately offline; all acceptance artifacts stay on the run host.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/"src"))
from f1_toys.rlt_contract import file_sha


def load_module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare(actual, expected):
    actual,expected=np.asarray(actual),np.asarray(expected)
    return dict(shape=list(actual.shape), exact=bool(np.array_equal(actual,expected)),
                max_abs_error=float(np.max(np.abs(actual-expected))))


def audit(args, run):
    # Import the actual production optimizer, without starting its CLI/server.
    load_module("rlt_audit_h20_wrapper",TASK/"scripts/h20_rtc_fast_server.py")
    from f1_toys.inference import Policy
    from f1_toys.rlt_inference import RLTInference
    from f1_toys.dataset import F1Dataset
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.rtc_prefix_optimization import prefix_graph_status
    from f1_toys.server import warm_policy
    os.environ["F1_TOYS_TOKENIZER"]=str(args.bundle/"paligemma_tokenizer.model")
    policy=Policy(args.bundle,args.vla_core)
    sidecar=RLTInference(policy,args.head,args.encoder,args.rlt_core)
    dataset=F1Dataset(args.dataset)
    with np.load(args.dataset/"arrays.npz") as arrays:
        held=np.array(arrays["submitted"][:,0],copy=True)
    saved_prefix=policy.model.build_prefix_cache
    saved_suffix=policy.model.run_suffix
    compile_suffix(policy)
    optimized_prefix=policy.model.build_prefix_cache
    optimized_suffix=policy.model.run_suffix
    sys.path.insert(0,str(args.training_scripts))
    offline=load_module("rlt_audit_frozen_feature",args.training_scripts/"rlt_stage2_offline.py")
    saved_read_images=offline.read_images
    probes=[]
    indices=sorted(set([0,len(dataset)//2,len(dataset)-1]))
    # The real frozen training helper includes @no_grad, BF16 AMP, its exact
    # Pi0 sampler and token encoder; replace only the file-image loader with
    # this already decoded real dataset sample.
    try:
        for index in indices:
            images=dataset.raw_images(index)
            policy.model.build_prefix_cache=saved_prefix
            policy.model.run_suffix=saved_suffix
            offline.read_images=lambda root,frame,images=images:images
            features=offline.extract_feature(policy,sidecar.reader,
                {"state16":dataset.states[index]},Path("."),args.seed+index)
            with torch.no_grad():
                expected=sidecar.actor.sac_forward(
                    {key:value[None].to(policy.device) for key,value in features.items()},
                    deterministic=True)[0].reshape(16,10).cpu().numpy()
            probes.append(dict(index=index,features=features,expected=expected))
    finally:
        offline.read_images=saved_read_images
        policy.model.build_prefix_cache=optimized_prefix
        policy.model.run_suffix=optimized_suffix
    warm_policy(policy,dataset,held[0],steps=5,rtc=True,graph_transitions=prefix_graph_status(policy)["enabled"])
    sidecar.warm(dataset.raw_images(0),dataset.states[0],held[0])
    outcomes=[]
    for probe in probes:
        index=probe["index"]
        result,raw,replay=sidecar.predict(dataset.raw_images(index),dataset.states[index],held[index],seed=args.seed+index)
        comparisons=dict(token=compare(replay["z_rl"],probe["features"]["z_rl"].numpy()),
            proprio=compare(replay["proprio"],probe["features"]["proprio"].numpy()),
            reference=compare(np.asarray(replay["reference_normalized_actions32"])[:,:10],probe["features"]["ref_chunk"].numpy()),
            actor=compare(raw[:,:10],probe["expected"]))
        outcomes.append(dict(index=index,seed=args.seed+index,comparisons=comparisons,
            ik_success=bool(result["success"]),timing_ms=result["timing_ms"],prefix_forward_calls=replay["prefix_forward_calls"]))
        run.log({"audit/probes_completed":len(outcomes),"audit/exact":all(v["exact"] for v in comparisons.values())},step=len(outcomes))
    # Ensure a guided B request still works after C16 sidecar requests.
    bootstrap=dict(bootstrap=True,delay_steps=15,shift_steps=20,coverage_steps=37,
        plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
    before,raw_before,_=policy.predict_rtc(dataset.raw_images(0),dataset.states[0],held[0],bootstrap,steps=5,seed=args.seed)
    sidecar.predict(dataset.raw_images(0),dataset.states[0],held[0],seed=args.seed)
    after,raw_after,_=policy.predict_rtc(dataset.raw_images(0),dataset.states[0],held[0],bootstrap,steps=5,seed=args.seed)
    b_comparison=compare(raw_after,raw_before)
    guided=dict(bootstrap,bootstrap=False,base_plan_id=1,
        previous_absolute_eef10=after["absolute_eef10"][20:],committed_actions16=[held[0].tolist()]*15)
    guided_result,_,_=policy.predict_rtc(dataset.raw_images(0),dataset.states[0],held[0],guided,steps=5,seed=args.seed)
    all_frozen=all(not p.requires_grad and p.grad is None
                   for model in (policy.model,sidecar.reader.encoder,sidecar.actor) for p in model.parameters())
    report=dict(schema="f1_rlt_inference_zero_output_audit_v1",identity=sidecar.identity,
        probes=outcomes,base_before_after_rlt=b_comparison,guided_b_path_completed=True,
        guided_b_ik_success=bool(guided_result["success"]),all_parameters_frozen=all_frozen,
        prefix_graph=prefix_graph_status(policy),parameter_updates=0,robot_commands=0,
        frozen_feature_source_sha256=file_sha(args.training_scripts/"rlt_stage2_offline.py"),
        source_sha256=file_sha(__file__),device=policy.device,wandb_mode="offline",wandb_url=None,
        limitations=["Uses stored real observations, not live robot feedback.",
            "Does not validate client hold/execute timing, HIL cancellation or task success.",
            "Original export remains BC-only; no Q-guided actor update or RTC actor is claimed."])
    report["accepted"]=all_frozen and b_comparison["exact"] and all(
        row["prefix_forward_calls"]==1 and all(v["exact"] for v in row["comparisons"].values()) for row in outcomes)
    (args.output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False)+"\n")
    run.summary.update(dict(accepted=report["accepted"],parameter_updates=0,robot_commands=0))
    if not report["accepted"]:
        raise ValueError("The serving path differs from frozen training features; inspect report.json")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("bundle","vla-core","rlt-core","head","encoder","dataset","training-scripts","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--run-id",required=True)
    parser.add_argument("--seed",type=int,default=20261026)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    import wandb
    run=wandb.init(project="f1_complexytoys",entity="robotics-example",
        group="00_Engineering_Checks",job_type="rlt-inference-zero-output-audit",id=args.run_id,
        name="B1250_RLT_BC400_Server_Equivalence",mode="offline",dir=str(args.output),
        tags=["F1","RLT","BC400","inference","zero_output"],
        config=dict(head_manifest_sha256=file_sha(args.head/"manifest.json"),
                    encoder_manifest_sha256=file_sha(args.encoder/"manifest.json"),
                    script_sha256=file_sha(__file__),parameter_updates=0))
    try:
        audit(args,run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__=="__main__":
    main()
