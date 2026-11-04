"""Offline same-observation seed resampling with a case-scoped prefix cache.

No sampler equations are copied or changed: Policy.predict_rtc and its complete
VJP sampler are called unchanged. Only build_prefix_cache is memoized during one
case. Baseline/build/hit raw32 must compare bitwise for the same seed first.
Robot interfaces and production mutation are absent; run in a separate process.
"""
import argparse
import contextlib
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image


def tensor_leaves(value):
    import torch
    if isinstance(value, torch.Tensor):
        yield value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from tensor_leaves(value[key])
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from tensor_leaves(item)
    elif hasattr(value, '__dict__'):
        yield from tensor_leaves(vars(value))
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(f'Unsupported cache/observation object {type(value)}')


def clone_tree(value):
    import torch
    if isinstance(value, torch.Tensor):
        assert not value.requires_grad and value.grad_fn is None
        return value.detach().clone()
    if isinstance(value, tuple):
        return tuple(clone_tree(v) for v in value)
    if isinstance(value, list):
        return [clone_tree(v) for v in value]
    if isinstance(value, dict):
        return {k:clone_tree(v) for k,v in value.items()}
    if value is None:
        return None
    raise TypeError(f'Unsupported prefix cache value {type(value)}')


def fingerprint(value):
    """Outside timed policy calls: catches CUDA writes that bypass _version."""
    digest = hashlib.sha256()
    for t in tensor_leaves(value):
        digest.update(str((t.dtype,tuple(t.shape))).encode())
        digest.update(t.detach().contiguous().view(__import__('torch').uint8).cpu().numpy().tobytes())
    return digest.hexdigest()


@contextlib.contextmanager
def cached_prefix(model):
    """A fresh closure per case; always restore the original bound method."""
    import torch
    original = model.build_prefix_cache
    state = dict(cache=None, observation=None, builds=0, hits=0, build_clone_ms=0.)

    def build(observation):
        leaves = list(tensor_leaves(observation))
        if state['cache'] is None:
            began = time.perf_counter()
            result = original(observation)
            state['cache'] = clone_tree(result)
            # Independent storage prevents aliasing an ephemeral CUDA output.
            for before,after in zip(tensor_leaves(result),tensor_leaves(state['cache'])):
                assert before.data_ptr()!=after.data_ptr()
            state['observation'] = [t.detach().clone() for t in leaves]
            state['versions'] = [t._version for t in tensor_leaves(state['cache'])]
            if leaves and leaves[0].is_cuda:
                torch.cuda.synchronize(leaves[0].device)
            state['build_clone_ms'] += (time.perf_counter()-began)*1000
            state['builds'] += 1
        else:
            assert len(leaves)==len(state['observation'])
            for current,expected in zip(leaves,state['observation']):
                assert current.shape==expected.shape and current.dtype==expected.dtype
                assert torch.equal(current,expected), 'Observation changed inside case cache'
            assert [t._version for t in tensor_leaves(state['cache'])]==state['versions'], 'Cache mutated'
            assert all(not t.requires_grad and t.grad_fn is None for t in tensor_leaves(state['cache']))
            state['hits'] += 1
        return state['cache']

    model.build_prefix_cache = build
    try:
        yield state
    finally:
        model.build_prefix_cache = original
        state['cache'] = None
        state['observation'] = None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','output','bundle','core','compat'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--trials',type=int,default=16)
    p.add_argument('--seed-start',type=int,default=2026102100)
    p.add_argument('--request-ids',type=int,nargs='*',help='Override default remaining-nine selection')
    p.add_argument('--include-first-rejections',action='store_true')
    p.add_argument('--unguided',action='store_true')
    args = p.parse_args()
    assert args.trials>0
    args.output.mkdir(parents=True,exist_ok=False)
    exec(compile(args.compat.read_text().split('from f1_toys.server import main')[0],str(args.compat),'exec'),{'__file__':str(args.compat)})
    from f1_toys.inference import Policy
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.eef import physical_actions,relative_pose
    from f1_toys.rtc import decode_window
    from f1_toys.native.kinematics import ContinuousIK,ArmModel,pose_error
    from f1_toys.native.fast_kinematics import FastArmModel
    from f1_toys.processing import sha256,process_image
    import torch
    policy = Policy(args.bundle,args.core)
    compile_suffix(policy)
    config = Path(__import__('f1_toys.native.kinematics',fromlist=['__file__']).__file__).with_name('ccs_m6_40.MvKDCfg')
    fk = ArmModel(config)
    expanded_model = FastArmModel(config)
    expanded_model.low[1] = np.deg2rad(-97.)
    expanded_ik = ContinuousIK(expanded_model)
    source = json.loads((args.inputs/'requests.json').read_text())
    wanted = {('3dd2babb',i) for i in (58,59,60,61,62,63,91,93,95)}
    if args.include_first_rejections:
        wanted |= {('3dd2babb',38),('977a65ff',28),('64184400',68)}
    source = [r for r in source if (r['request']['request_id'] in args.request_ids if args.request_ids else
              (r['episode'].split('_')[-1],r['request']['request_id']) in wanted)]
    assert source,'No requested cases in input bundle'
    if not args.request_ids:
        assert {(r['episode'].split('_')[-1],r['request']['request_id']) for r in source}==wanted, 'Missing default cases'
    index = {(r['episode'],r['request_id']):r for r in map(json.loads,(args.inputs/'image-index.jsonl').read_text().splitlines())}
    adapter_root = config.parents[1]
    manifest = dict(model_bundle=policy.bundle_id,seed_start=args.seed_start,trials=args.trials,guided=not args.unguided,
        source_sha256={str(q):sha256(q) for q in (Path(__file__),args.compat,config,args.bundle/'bundle.json',args.inputs/'requests.json',args.inputs/'image-index.jsonl',adapter_root/'inference.py',adapter_root/'rtc.py',adapter_root/'rtc_optimization.py')},
        cache_contract='One observation/state/context per scope; clone detached prefix cache; original full VJP sampler unchanged; no gradients retained between candidates.',
        robot_commands=0,training_steps=0,production_changes=0,
        limitations=['Frozen observations, not closed-loop task outcomes. Original random noise was not recorded.',
            'J2 -97 is an unapproved integrated-machine research bound; no hardware commands.',
            'Cache applies only to same observation. New images/state/context begin a fresh scope.',
            'Cache/uncached equivalence is a mandatory runtime gate, not assumed from code review.',
            'Cumulative times sum timed candidate policy+conditional IK; exclude disk/audit/validation. Cold-cache estimate adds measured build+clone cost; no network/deadline guarantee.',
            'Pass@k is observed deterministic seed-order coverage on these cases, not a statistical success probability.'])
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    records,validations,per_case = [],[],[]
    stream = (args.output/'candidates.jsonl').open('x')
    for selected in source:
        original = selected['request']; episode = selected['episode']; rid = original['request_id']
        entry = index[episode,rid]
        images,pixels = {},{}
        for camera,name in entry['images'].items():
            path = args.inputs/name
            assert sha256(path)==entry['image_sha256'][camera]
            images[camera] = Image.open(path).convert('RGB')
            pixels[camera] = hashlib.sha256(process_image(images[camera],camera,policy.contract).tobytes()).hexdigest()
        state = original['prediction_replay']['state16']; held = original['prediction_replay']['last_submitted16']; ctx = original['rtc_request']
        assert state==entry['observation']['state16']
        identity = hashlib.sha256(json.dumps(dict(pixels=pixels,state=state,held=held,context=ctx),sort_keys=True).encode()).hexdigest()
        def predict(seed):
            return policy.predict_rtc(images,state,held,ctx,seed=seed,guided=not args.unguided)
        started = time.perf_counter()
        baseline,baseline_raw,_ = predict(args.seed_start)
        uncached_ms = (time.perf_counter()-started)*1000
        with cached_prefix(policy.model) as cache:
            built,built_raw,_ = predict(args.seed_start)
            assert np.array_equal(baseline_raw,built_raw),'Raw changed when cloning prefix cache'
            before = fingerprint(cache['cache'])
            rows = []
            cumulative = 0.
            for trial in range(args.trials):
                began = time.perf_counter()
                result,raw,_ = predict(args.seed_start+trial)
                policy_ms = (time.perf_counter()-began)*1000
                if trial==0:
                    assert np.array_equal(baseline_raw,raw),'Raw changed on cached prefix reuse'
                    assert result['success']==baseline['success']
                    if result['success']:
                        assert result['actions']==baseline['actions']
                retry,retry_ms = None,0.
                if not result['success']:
                    began=time.perf_counter()
                    retry=decode_window(expanded_ik,physical_actions(raw,policy.stats['action']),state,held,ctx['committed_actions16'],ctx['coverage_steps'])
                    retry_ms=(time.perf_counter()-began)*1000
                chosen=result if result['success'] else retry
                cumulative += policy_ms+retry_ms
                metrics={}
                if chosen['success']:
                    q=np.array(chosen['actions']); n=len(ctx['committed_actions16'])
                    assert chosen['actions'][:n]==ctx['committed_actions16']
                    previous=np.array(ctx['committed_actions16'][-1][7:14] if n else state[7:14])
                    errors,steps=[],[]
                    for k in range(n,len(q)):
                        error=pose_error(relative_pose(np.array(result['absolute_eef10'][k])),fk.fk(1,q[k,7:14]))
                        assert error[0]<=.01 and error[1]<=.001
                        assert (policy.ik if result['success'] else expanded_ik).model.admissible_rad(np.deg2rad(q[k,7:14]))
                        errors.append(error);steps.append(float(abs(q[k,7:14]-previous).max()));previous=q[k,7:14]
                    metrics=dict(max_joint_step_deg=max(steps),join_joint_step_deg=steps[0],minimum_j2_deg=float(q[n:,8].min()),max_pose_error_mm_deg=np.max(errors,axis=0).tolist())
                record=dict(episode=episode,request_id=rid,case_identity_sha256=identity,trial=trial,seed=args.seed_start+trial,
                    bootstrap=ctx['bootstrap'],guided=not args.unguided,strict_success=result['success'],conditional_expanded_success=chosen['success'],
                    measured_cached_policy_ms=policy_ms,expanded_retry_ms=retry_ms,cumulative_candidates_ms=cumulative,
                    estimated_cold_cache_cumulative_ms=cumulative+cache['build_clone_ms'],timing_ms=result['timing_ms'],metrics=metrics,
                    raw32=raw.tolist(),absolute_eef10=result['absolute_eef10'],actions=chosen['actions'],selected_diagnostics=chosen['diagnostics'])
                rows.append(record);records.append(record);stream.write(json.dumps(record)+'\n');stream.flush()
            after=fingerprint(cache['cache'])
            assert before==after,'Cache contents mutated during repeated suffix/VJP'
            assert all(param.grad is None for param in policy.model.parameters()), 'Unexpected parameter gradients'
            validation=dict(episode=episode,request_id=rid,case_identity_sha256=identity,uncached_build_hit_raw_bitwise_equal=True,
                strict_actions_equal_same_seed=True if baseline['success'] else None,cache_fingerprint_before=before,cache_fingerprint_after=after,cache_builds=cache['builds'],cache_hits=cache['hits'],
                cache_build_clone_ms=cache['build_clone_ms'],baseline_uncached_ms=uncached_ms,first_cached_policy_ms=rows[0]['measured_cached_policy_ms'],
                detached_independent_cache=True,parameter_grads_absent=True)
            validations.append(validation)
        def first_time(field):
            hit=next((r for r in rows if r[field]),None)
            return None if hit is None else dict(trial=hit['trial'],samples=hit['trial']+1,cached_cumulative_ms=hit['cumulative_candidates_ms'],cold_cache_estimated_ms=hit['estimated_cold_cache_cumulative_ms'])
        case=dict(episode=episode,request_id=rid,trials=len(rows),strict_successes=sum(r['strict_success'] for r in rows),
            expanded_successes=sum(r['conditional_expanded_success'] for r in rows),first_strict=first_time('strict_success'),first_conditional=first_time('conditional_expanded_success'),
            cached_policy_ms={f'p{v}':float(np.percentile([r['measured_cached_policy_ms'] for r in rows],v)) for v in (50,95)})
        per_case.append(case)
        (args.output/'validation.json').write_text(json.dumps(validations,indent=2)+'\n')
        (args.output/'progress.json').write_text(json.dumps(per_case,indent=2)+'\n')
        print(json.dumps(case),flush=True)
    pass_at_k={}
    for k in sorted(set([1,2,4,8,args.trials])):
        if k>args.trials:continue
        pass_at_k[str(k)]={label:sum(any(r[field] for r in records if r['episode']==c['episode'] and r['request_id']==c['request_id'] and r['trial']<k) for c in per_case)
            for label,field in [('strict','strict_success'),('conditional_expanded','conditional_expanded_success')]}
    summary=dict(cases=len(per_case),sampling_model_calls=len(records),validation_extra_model_calls=2*len(per_case),
        per_case=per_case,pass_at_k_case_counts=pass_at_k,all_cache_equivalence_passed=True,robot_commands=0,production_changes=0,limitations=manifest['limitations'])
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='per_case'}),flush=True)


if __name__=='__main__':
    main()
