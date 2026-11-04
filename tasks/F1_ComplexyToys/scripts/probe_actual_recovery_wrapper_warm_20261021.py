"""Real frozen50K GPU2-only offline recovery wrapper acceptance; no server or robot."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time
import numpy as np
from PIL import Image


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def jhash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','output','bundle','core','compat','wrapper'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--seed-start',type=int,default=2026102100)
    args=parser.parse_args()
    assert os.environ.get('CUDA_VISIBLE_DEVICES')=='2','This probe is restricted to physicalGPU2'
    args.output.mkdir(parents=True,exist_ok=False)
    expected_wrapper='41c07b196a2e9c214992396d12eb72e179acc356ab39dd809e9b5df583b3034f'
    assert sha(args.wrapper)==expected_wrapper,'Wrapper changed after review freeze'
    exec(compile(args.compat.read_text().split('from f1_toys.server import main')[0],str(args.compat),'exec'),{'__file__':str(args.compat)})
    from f1_toys.inference import Policy
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.native.kinematics import ArmModel,pose_error
    from f1_toys.eef import relative_pose
    from f1_toys.processing import process_image
    import torch
    spec=importlib.util.spec_from_file_location('frozen_ik_recovery_wrapper',args.wrapper)
    recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
    policy=Policy(args.bundle,args.core);compile_suffix(policy)
    cfg=Path(__import__('f1_toys.native.kinematics',fromlist=['__file__']).__file__).with_name('ccs_m6_40.MvKDCfg')
    fk=ArmModel(cfg)
    source=json.loads((args.inputs/'requests.json').read_text())
    assert len(source)==49 and all(not s['request']['success'] for s in source)
    index={(r['episode'],r['request_id']):r for r in map(json.loads,(args.inputs/'image-index.jsonl').read_text().splitlines())}
    manifest=dict(schema='actual_wrapper_gpu_acceptance_v1',model_bundle=policy.bundle_id,
        physical_gpu=2,cuda_visible_devices=os.environ['CUDA_VISIBLE_DEVICES'],gpu_name=torch.cuda.get_device_name(0),
        torch_version=torch.__version__,wrapper_sha256=sha(args.wrapper),seed_start=args.seed_start,
        max_attempts=4,budget_ms=400.,source_sha256={str(p):sha(p) for p in [Path(__file__),args.wrapper,args.compat,cfg,
            args.inputs/'requests.json',args.inputs/'image-index.jsonl',args.bundle/'bundle.json']},
        robot_commands=0,ports_opened=0,training_steps=0,production_changes=0,
        limitations=[
            'Old50K frozen inputs and same-observation re-sampling, not fresh-observation progress recovery or real task success.',
            'Original historical model noise was not recorded. The new first seed is not the original failing trajectory.',
            '400ms bounds additional candidate eligibility, not cancellation of an already running GPU call or total RPC time.',
            'Measured local wrapper walltime excludes network, client adoption and physical action. No original RTC deadline guarantee.',
            'Both guided and bootstrap cold branches are warmed separately and reported. No persistent server or robot interfaces imported.',
            'Same-seed baseline control uses a known first-call-success candidate; no claim that original historical successful input set was replayed.',
            'Fixed seed order and49 development cases are empirical coverage, not a recovery probability.'])
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    def load(selected):
        raw=selected['request'];entry=index[(selected['episode'],raw['request_id'])];images={};pixels={}
        for camera,name in entry['images'].items():
            path=args.inputs/name;assert sha(path)==entry['image_sha256'][camera]
            images[camera]=Image.open(path).convert('RGB')
            pixels[camera]=hashlib.sha256(process_image(images[camera],camera,policy.contract).tobytes()).hexdigest()
        state=raw['prediction_replay']['state16'];held=raw['prediction_replay']['last_submitted16'];ctx=raw['rtc_request']
        assert state==entry['observation']['state16']
        return images,state,held,ctx,pixels
    # Warmup is separate from measured49 wrappers; known case38 seed0 succeeded in prior real sampling.
    first=next(s for s in source if s['episode'].endswith('3dd2babb') and s['request']['request_id']==38)
    imgs,state,held,ctx,pixels=load(first)
    began=time.perf_counter();baseline=policy.predict_rtc(imgs,state,held,ctx,seed=args.seed_start,guided=True)
    warmup_ms=(time.perf_counter()-began)*1000
    assert baseline[0]['success'],'Known same-seed first-success control no longer succeeds'
    bootstrap_case=next(s for s in source if s['request']['rtc_request']['bootstrap'])
    bi,bs,bh,bc,_=load(bootstrap_case)
    began=time.perf_counter();bootstrap_warmup=policy.predict_rtc(bi,bs,bh,bc,seed=args.seed_start,guided=True)
    bootstrap_warmup_ms=(time.perf_counter()-began)*1000
    captured=[];audit=[]
    def control_original(*a,**k):
        value=policy.predict_rtc(*a,**k);captured.append(value);return value
    control=recovery.recover_predict(policy,control_original,(imgs,state,held,ctx),dict(seed=args.seed_start,guided=True),
        max_attempts=4,budget_ms=400.,seed_factory=lambda:args.seed_start+1,audit=audit.append)
    control_checks=dict(original_success_returned_same_tuple_object=control is captured[0],single_attempt=len(captured)==1,
        raw_bitwise_equal_to_uncached_same_seed=np.array_equal(control[1],baseline[1]),
        actions_bitwise_equal_to_uncached_same_seed=np.array_equal(np.asarray(control[0]['actions']),np.asarray(baseline[0]['actions'])),
        absolute_eef_bitwise_equal=np.array_equal(control[0]['absolute_eef10'],baseline[0]['absolute_eef10']),
        no_recovery_field_added='ik_recovery' not in control[0])
    assert all(control_checks.values()),control_checks
    (args.output/'success-control.json').write_text(json.dumps(dict(episode=first['episode'],request_id=38,seed=args.seed_start,
        warmup_ms=warmup_ms,bootstrap_warmup_ms=bootstrap_warmup_ms,checks=control_checks,audit=audit),indent=2)+'\n')
    print(json.dumps(dict(event='warmup_and_success_control_complete',warmup_ms=warmup_ms,bootstrap_warmup_ms=bootstrap_warmup_ms,checks=control_checks)),flush=True)
    records=[];stream=(args.output/'requests.jsonl').open('x')
    for selected in source:
        raw=selected['request'];images,state,held,ctx,pixels=load(selected);capture=[];audit=[]
        before=jhash(dict(state=state,held=held,context=ctx))
        pixel_bytes_before={k:hashlib.sha256(v.tobytes()).hexdigest() for k,v in images.items()}
        original_method=policy.model.build_prefix_cache
        def tracked(*a,**k):
            value=policy.predict_rtc(*a,**k);capture.append(value);return value
        began=time.perf_counter()
        returned=recovery.recover_predict(policy,tracked,(images,state,held,ctx),dict(seed=args.seed_start,guided=True),
            max_attempts=4,budget_ms=400.,seed_factory=lambda:args.seed_start+1,audit=audit.append)
        actual_ms=(time.perf_counter()-began)*1000
        result,action_raw,data=returned;assert len(audit)==1;info=audit[0];order=info['selected_order']
        selected_tuple=capture[order]
        assert np.array_equal(action_raw,selected_tuple[1]),'Selected raw mismatches chosen prediction'
        assert result['absolute_eef10']==selected_tuple[0]['absolute_eef10'],'Selected EEF mismatches chosen prediction'
        assert before==jhash(dict(state=state,held=held,context=ctx))
        assert pixel_bytes_before=={k:hashlib.sha256(v.tobytes()).hexdigest() for k,v in images.items()}
        assert policy.model.build_prefix_cache==original_method,'Cache method not restored'
        assert all(p.grad is None for p in policy.model.parameters())
        attempts=info['attempts'];assert len(attempts)==len(capture)<=4
        if order:
            assert attempts[order]['eligible'] and attempts[order]['cumulative_ms']<400.
        prefix=ctx['committed_actions16'];n=len(prefix)
        full=result['success'] is True
        if full:
            assert result['actions'][:n]==prefix
            assert len(result['actions'])==ctx['coverage_steps']
            qrows=[a[7:14] for a in result['actions'][n:]]
        else:
            assert result['reason']=='ik_rejected' and result['actions'] is None
            valid=recovery.valid_suffix_steps(result)
            qrows=[d['joints'] for d in result['diagnostics'][:valid]]
        errors=[];steps=[]
        previous=np.asarray(prefix[-1][7:14] if prefix else state[7:14])
        for j,q in enumerate(qrows):
            q=np.asarray(q);assert fk.admissible_rad(np.deg2rad(q))
            e=pose_error(relative_pose(np.asarray(result['absolute_eef10'][n+j])),fk.fk(1,q))
            assert e[0]<=.01 and e[1]<=.001,e
            errors.append(e);steps.append(float(np.max(abs(q-previous))));previous=q
        outcome='strict_full' if full else ('strict_partial' if qrows else 'strict_zero')
        semantic_same=(result is capture[0][0] and returned is capture[0]) if capture[0][0]['success'] else None
        if semantic_same is not None:assert semantic_same
        record=dict(episode=selected['episode'],request_id=raw['request_id'],bootstrap=ctx['bootstrap'],
            outcome=outcome,valid_new_points=len(qrows),selected_order=order,selected_seed=attempts[order]['seed'],
            selected_original_prediction=order==0,resampled_prediction_selected=order>0,
            first_candidate_success=capture[0][0]['success'],first_candidate_success_object_unchanged=semantic_same,
            wrapper_wall_ms=actual_ms,over400ms_wall=actual_ms>=400.,audit=info,
            late_retry_not_selected=all(not (a['order']==order and a['order']>0 and not a['eligible']) for a in attempts),
            selected_eef_raw_identity_verified=True,inputs_unchanged=True,prefix_cache_restored=True,parameter_grads_absent=True,
            original_bounds_verified=True,original_pose_tolerance_verified=True,
            max_pose_error_mm_deg=np.max(errors,axis=0).tolist() if errors else None,
            max_neighbor_joint_step_deg=max(steps) if steps else None,
            prefix_seam_joint_step_deg=steps[0] if steps else None,
            source_pixels_sha256=pixels,
            selected_raw32=action_raw.tolist(),selected_absolute_eef10=result['absolute_eef10'],
            selected_diagnostics=result['diagnostics'],selected_actions=result['actions'])
        records.append(record);stream.write(json.dumps(record)+'\n');stream.flush()
        print(json.dumps({k:record[k] for k in ['episode','request_id','outcome','valid_new_points','selected_order','wrapper_wall_ms']}),flush=True)
    stream.close()
    summary=dict(cases=len(records),outcomes={k:sum(r['outcome']==k for r in records) for k in ['strict_full','strict_partial','strict_zero']},
        selected_resampled=sum(r['resampled_prediction_selected'] for r in records),
        first_successes_returned_unchanged=sum(r['first_candidate_success_object_unchanged'] is True for r in records),
        wrapper_wall_ms={f'p{p}':float(np.percentile([r['wrapper_wall_ms'] for r in records],p)) for p in [50,95,99]},
        max_wrapper_wall_ms=max(r['wrapper_wall_ms'] for r in records),over400ms_wall=sum(r['over400ms_wall'] for r in records),
        attempts_histogram={str(n):sum(len(r['audit']['attempts'])==n for r in records) for n in range(1,5)},
        overbudget_retry_rejections=sum(any(a['order']>0 and not a['eligible'] for a in r['audit']['attempts']) for r in records),
        all_late_retries_not_selected=all(r['late_retry_not_selected'] for r in records),
        all_bounds_pose_prefix_checks_passed=True,success_control=control_checks,
        model_calls_in_measured49=sum(len(r['audit']['attempts']) for r in records),extra_model_calls_control_warmup=3,
        robot_commands=0,ports_opened=0,production_changes=0,limitations=manifest['limitations'])
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)
if __name__=='__main__':main()
