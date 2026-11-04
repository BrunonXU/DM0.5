#!/usr/bin/env python3
"""Isolated paired F1 inference experiments. No sockets, ROS or model updates.

Uses the deployed frozen package via PYTHONPATH. Candidate changes are local to
this process; the checkpoint, input adapter and robot-side execution stay frozen.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

import numpy as np
from PIL import Image
import torch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quantiles(values):
    return dict(zip(('p50', 'p95', 'p99', 'max'),
                    np.percentile(values, [50, 95, 99, 100]).tolist())) if values else {}


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def tree_clone(x):
    if torch.is_tensor(x):
        return x.detach().clone()
    if isinstance(x, tuple):
        return tuple(tree_clone(v) for v in x)
    if isinstance(x, list):
        return [tree_clone(v) for v in x]
    if isinstance(x, dict):
        return {k: tree_clone(v) for k, v in x.items()}
    return x


class MaskedImageCache:
    """Reuse only an exactly equal, fully masked image's frozen vision result.

    Keeps all token slots, masks and LLM tensor shapes. The cache is per model
    instance and checks the input, autocast dtype, training state and device.
    Never reuses an active view or a previous observation's KV cache.
    """
    def __init__(self, model):
        self.model = model
        self.original = model.embed_prefix
        self.cache = {}
        self.hits = 0

    def __call__(self, obs):
        if self.model.training or torch.is_grad_enabled():
            raise RuntimeError('MaskedImageCache is frozen inference-only')
        views = [(k, v, bool(obs.image_masks[k].any())) for k, v in obs.images.items()]
        active_ids = {id(v) for _, v, active in views if active}
        inactive = {id(v): k for k, v, active in views if not active and id(v) not in active_ids}
        forward = self.model.img.forward
        had_forward = 'forward' in self.model.img.__dict__

        def cached(image, *a, **kw):
            name = inactive.get(id(image))
            if name is None:
                return forward(image, *a, **kw)
            signature = (tuple(image.shape), str(image.dtype), str(image.device),
                         torch.is_autocast_enabled(), str(torch.get_autocast_dtype('cuda')))
            old = self.cache.get(name)
            if old is not None and old[0] == signature and torch.equal(image, old[1]):
                self.hits += 1
                return old[2]
            result = forward(image, *a, **kw)
            self.cache[name] = (signature, image.detach().clone(), tree_clone(result))
            return result

        self.model.img.forward = cached
        try:
            return self.original(obs)
        finally:
            if had_forward:
                self.model.img.forward = forward
            else:
                del self.model.img.forward


@contextmanager
def explicit_noise(model, noise):
    import f1_toys.rtc as rtc
    original = rtc.sample_actions

    def sample(given_model, *a, **kw):
        if given_model is model:
            kw['noise'] = noise.clone()
        return original(given_model, *a, **kw)

    rtc.sample_actions = sample
    try:
        yield
    finally:
        rtc.sample_actions = original


class Profile:
    """CUDA-event spans are separate diagnostic runs, not the latency sample."""
    def __init__(self, model):
        self.model = model
        self.events = defaultdict(list)
        self.restore = []

    def wrap(self, obj, name, label):
        original = getattr(obj, name)
        self.restore.append((obj, name, original))

        def measured(*a, **kw):
            start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            cpu = time.perf_counter()
            start.record()
            value = original(*a, **kw)
            end.record()
            self.events[label].append((start, end, (time.perf_counter()-cpu)*1000))
            return value

        setattr(obj, name, measured)

    def __enter__(self):
        self.wrap(self.model, 'build_prefix_cache', 'prefix_total')
        self.wrap(self.model.img, 'forward', 'vision_total')
        self.wrap(self.model, 'run_suffix', 'suffix_forward_total')
        self.wrap(torch.autograd, 'grad', 'vjp_total')
        return self

    def __exit__(self, *a):
        for obj, name, original in reversed(self.restore):
            setattr(obj, name, original)

    def result(self):
        torch.cuda.synchronize()
        return {k: dict(gpu_ms=sum(s.elapsed_time(e) for s, e, _ in v),
                        cpu_dispatch_ms=sum(c for _, _, c in v), calls=len(v))
                for k, v in self.events.items()}


def load_cases(args, policy):
    from f1_toys.processing import process_image
    rows = json.loads((args.inputs/'cases.json').read_text())
    idx = {r['id']: r for r in json.loads((args.inputs/'image-index.json').read_text())}
    cases = []
    for row in rows:
        entry = idx[row['id']]
        images = {}
        for camera, filename in entry['images'].items():
            assert sha(args.inputs/filename) == entry['sha256'][camera]
            with Image.open(args.inputs/filename) as im:
                images[camera] = Image.fromarray(process_image(im.convert('RGB'), camera, policy.contract))
        req = row['request']
        state = np.array(req['prediction_replay']['state16'], float)
        held = np.array(req['prediction_replay']['last_submitted16'], float)
        state[14] = held[14] = 0.  # Same explicit missing-hardware slot as runtime.
        ctx = copy.deepcopy(req['rtc_request'])
        assert (ctx['delay_steps'], ctx['shift_steps'], ctx['coverage_steps']) == (14, 16, 32)
        for q in ctx['committed_actions16']:
            q[14] = 0.
        cases.append(dict(id=row['id'], group=row['group'], images=images,
                          state=state, held=held, rtc=ctx, origin='recorded_rollout',
                          expert=None, valid_steps=50))
    if args.dataset and args.panel:
        from f1_toys.dataset import F1Dataset
        ds = F1Dataset(args.dataset)
        panel = json.loads(args.panel.read_text())
        assert panel['dataset_manifest_sha256'] == sha(args.dataset/'manifest.json')
        assert ds.manifest['split'] == 'validation'
        selected = defaultdict(list)
        for row in panel['cases']:
            selected[(row['episode_id'], row['group'])].append(row)
        for key, rows in selected.items():
            # Keep endpoints in each episode/phase; they may share a frame across groups.
            for row in [rows[j] for j in sorted({0, len(rows)-1})]:
                i = row['index']
                images = {k: Image.fromarray(process_image(im, k, policy.contract))
                          for k, im in ds.raw_images(i).items()}
                state = ds.states[i].astype(float).copy()
                state[14] = 0.
                ctx = dict(bootstrap=True, delay_steps=14, shift_steps=16, coverage_steps=32,
                           committed_actions16=[], previous_absolute_eef10=[], plan_origin_ns=0, base_plan_id=0)
                cases.append(dict(id=f'heldout-{i}-{row["group"]}', group=row['group'], images=images,
                                  state=state, held=state.copy(), rtc=ctx, origin='heldout_bootstrap',
                                  expert=ds.actions[i].copy(), valid_steps=row['valid_steps']))
    return cases


def compare_outputs(base, candidate, context, policy, expert, valid_steps):
    from f1_toys.eef import physical_actions, relative_pose
    b, br, _ = base
    c, cr, _ = candidate
    bp, cp = (physical_actions(x, policy.stats['action']) for x in (br, cr))
    rot = []
    for x, y in zip(bp, cp):
        r = relative_pose(x)[:3,:3].T @ relative_pose(y)[:3,:3]
        rot.append(float(np.degrees(np.arccos(np.clip((np.trace(r)-1)/2, -1, 1)))))
    d = len(context['committed_actions16'])
    for out in (b, c):
        if out['success'] and d:
            assert np.array_equal(np.asarray(out['actions'][:d]), np.asarray(context['committed_actions16']))
    row = dict(raw_exact=bool(np.array_equal(br,cr)), max_raw_delta=float(abs(br-cr).max()),
               max_eef_mm=float(np.linalg.norm(bp[:,:3]-cp[:,:3],axis=1).max()*1000),
               max_rotation_deg=max(rot), max_gripper_delta=float(abs(bp[:,9]-cp[:,9]).max()*50),
               base_success=bool(b['success']), candidate_success=bool(c['success']),
               prefix_exact=True)
    if b['success'] and c['success']:
        ba, ca = np.array(b['actions']), np.array(c['actions'])
        row['max_joint_delta_deg'] = float(abs(ba[:,7:14]-ca[:,7:14]).max())
        for key, out in [('base',ba),('candidate',ca)]:
            row[key+'_join_joint_delta_deg'] = float(abs(out[d,7:14]-out[d-1,7:14]).max()) if d else None
    for name, pp in [('base',bp),('candidate',cp)]:
        grip = np.clip(pp[:valid_steps,9]*50+50,0,100)
        opens = np.flatnonzero(grip<=10)
        row[name+'_first_open'] = int(opens[0]) if len(opens) else None
        row[name+'_recent_open'] = bool(np.any(grip[d:min(d+16,valid_steps)]<=10))
        future = np.flatnonzero(grip[d:valid_steps]<=10)
        executable = np.flatnonzero(grip[d:min(context['coverage_steps'],valid_steps)]<=10)
        row[name+'_first_open_future'] = int(future[0]+d) if len(future) else None
        row[name+'_first_open_window'] = int(executable[0]+d) if len(executable) else None
        if expert is not None:
            eg = np.clip(expert[:valid_steps,9]*50+50,0,100)
            row[name+'_gripper_mae'] = float(abs(grip-eg).mean())
            row[name+'_position_rmse_mm'] = float(np.sqrt(np.mean((pp[:valid_steps,:3]-expert[:valid_steps,:3])**2))*1000)
            row['expert_first_open'] = int(np.flatnonzero(eg<=10)[0]) if np.any(eg<=10) else None
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'bundle', 'core', 'compat', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--dataset', type=Path)
    p.add_argument('--panel', type=Path)
    p.add_argument('--mode', choices=['baseline','inactive_cache','prefix_compile','prefix_constants','prefix_graph','prefix_graph_no_autocast_cache','jacobian_identity','upstream_approx'], default='baseline')
    p.add_argument('--timing-pairs', type=int, default=240)
    p.add_argument('--seeds', type=int, nargs='+', default=[2026102301,2026102302,2026102303])
    p.add_argument('--profile-count', type=int, default=12)
    p.add_argument('--prefix-debug-correctness', action='store_true')
    args = p.parse_args()
    assert os.environ['CUDA_VISIBLE_DEVICES'] in ('4','5','6','7'), 'Use an independently checked idle test GPU'
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.compat.read_text().split('from f1_toys.server import main')[0]
    exec(compile(source,str(args.compat),'exec'), {'__file__':str(args.compat)})
    from f1_toys.inference import Policy
    from f1_toys.rtc_optimization import compile_suffix
    import f1_toys.inference as inference
    policy = Policy(args.bundle,args.core)
    compile_suffix(policy)
    cases = load_cases(args, policy)
    noises = {s:torch.randn((1,50,32), device='cuda', generator=torch.Generator(device='cuda').manual_seed(s)) for s in args.seeds}
    manifest = dict(mode=args.mode, bundle_id=policy.bundle_id, bundle=str(args.bundle),
                    core=str(args.core), compat_sha256=sha(args.compat), benchmark_sha256=sha(__file__),
                    inference_sha256=sha(inference.__file__), processor=policy.input_preprocessing,
                    torch=torch.__version__, python=platform.python_version(),
                    cuda_visible_devices=os.environ['CUDA_VISIBLE_DEVICES'],
                    gpu=torch.cuda.get_device_name(), threads=torch.get_num_threads(),
                    started_wall_ns=time.time_ns(), seeds=args.seeds,
                    noise_sha256={s:hashlib.sha256(x.cpu().numpy().tobytes()).hexdigest() for s,x in noises.items()},
                    cases=[{k:v for k,v in c.items() if k not in ('images','state','held','rtc','expert')} for c in cases],
                    source_cases_sha256=sha(args.inputs/'cases.json'), robot_commands=0, production_changes=0,
                    prefix_debug_correctness=args.prefix_debug_correctness,
                    limitations=['Recorded observations do not react to generated actions.',
                                 'Independent process; no live recording, network or actuation load.',
                                 'CUDA-event profiling is separate from uninstrumented latency.'])
    save(args.output/'manifest.json',manifest)
    original_prefix = policy.model.build_prefix_cache
    original_embed = policy.model.embed_prefix
    cache = MaskedImageCache(policy.model)
    compiled_prefix = None
    if args.mode == 'prefix_compile':
        compiled_prefix = torch.compile(original_prefix, fullgraph=True, dynamic=False,
            options={'emulate_precision_casts':True,'force_same_precision':True,'triton.cudagraphs':True})
    elif args.mode in ('prefix_constants','prefix_graph','prefix_graph_no_autocast_cache'):
        from rlinf_prefix_candidate import install_prefix_candidate
        compiled_prefix = install_prefix_candidate(policy, args.mode.removeprefix('prefix_'),
            error_path=args.output/'prefix-failure.txt')
        if args.mode == 'prefix_graph_no_autocast_cache':
            compiled_prefix.debug_sync = args.prefix_debug_correctness
        policy.model.build_prefix_cache = original_prefix

    def predict(case, seed, candidate=False, configure=True):
        if configure:
            policy.model.build_prefix_cache = compiled_prefix if candidate and compiled_prefix else original_prefix
            policy.model.embed_prefix = cache if candidate and args.mode=='inactive_cache' else original_embed
        if candidate and args.mode in ('jacobian_identity','upstream_approx'):
            from rlinf_offline_candidates import install_rtc_candidate
            with install_rtc_candidate(policy,args.mode,noise=noises[seed]):
                return policy.predict_rtc(case['images'],case['state'],case['held'],case['rtc'],steps=5,prepared_pixels=True)
        with explicit_noise(policy.model,noises[seed]):
            return policy.predict_rtc(case['images'],case['state'],case['held'],case['rtc'],steps=5,prepared_pixels=True)

    warm = []
    for candidate in (False, True):
        for index in range(8):
            started = time.perf_counter()
            value = predict(cases[index%len(cases)],args.seeds[0],candidate)
            warm.append(dict(candidate=candidate,index=index,wall_ms=(time.perf_counter()-started)*1000,
                             timing_ms=value[0]['timing_ms'],success=value[0]['success']))
            save(args.output/'warmup.json',warm)
    print(json.dumps({'event':'warmed','mode':args.mode,'cases':len(cases)}),flush=True)

    # Correctness/behavior comparisons use identical explicit noise, input and prefix.
    comparisons=[]
    predictions={}
    with (args.output/'comparisons.jsonl').open('x') as stream:
        for ci,case in enumerate(cases):
            for seed in args.seeds:
                order=(False,True) if (ci+seed)%2 else (True,False)
                outputs={choice:predict(case,seed,choice) for choice in order}
                row=dict(case=case['id'],group=case['group'],origin=case['origin'],seed=seed,
                         **compare_outputs(outputs[False],outputs[True],case['rtc'],policy,case['expert'],case['valid_steps']))
                comparisons.append(row);stream.write(json.dumps(row)+'\n');stream.flush()
                predictions[f'{ci}_{seed}_baseline']=outputs[False][1]
                predictions[f'{ci}_{seed}_candidate']=outputs[True][1]
            print(json.dumps({'event':'case','case':ci+1,'total':len(cases),'mode':args.mode}),flush=True)
    np.savez_compressed(args.output/'predictions.npz',**predictions)
    if args.mode == 'prefix_graph_no_autocast_cache':
        # Localize capture/replay errors in correctness; keep timing uninstrumented.
        compiled_prefix.debug_sync = False
    timing=[]
    history=[c for c in cases if c['origin']=='recorded_rollout']
    with (args.output/'timings.jsonl').open('x') as stream:
        for i in range(args.timing_pairs):
            case=history[i%len(history)];seed=args.seeds[i%len(args.seeds)]
            for candidate in ((False,True) if i%2 else (True,False)):
                started=time.perf_counter();out=predict(case,seed,candidate)
                row=dict(pair=i,case=case['id'],seed=seed,candidate=candidate,success=bool(out[0]['success']),
                         wall_ms=(time.perf_counter()-started)*1000,**out[0]['timing_ms'])
                timing.append(row);stream.write(json.dumps(row)+'\n');stream.flush()
            if i%40==0:print(json.dumps({'event':'timing','pairs':i+1,'mode':args.mode}),flush=True)

    profiles=[]
    if args.mode=='baseline':
        for case in history[:args.profile_count]:
            reference=predict(case,args.seeds[0],False)[1]
            with Profile(policy.model) as profile:
                out=predict(case,args.seeds[0],False,configure=False)
            profiles.append(dict(case=case['id'],raw_exact=bool(np.array_equal(reference,out[1])),spans=profile.result()))
        save(args.output/'profiles.json',profiles)
    summary=dict(mode=args.mode,comparison_count=len(comparisons),timing_pairs=args.timing_pairs,
                 exact=sum(r['raw_exact'] for r in comparisons),
                 baseline_failures=sum(not r['base_success'] for r in comparisons),
                 candidate_failures=sum(not r['candidate_success'] for r in comparisons),
                 new_failures=sum(r['base_success'] and not r['candidate_success'] for r in comparisons),
                 rescued=sum(not r['base_success'] and r['candidate_success'] for r in comparisons),
                 max_raw_delta=max(r['max_raw_delta'] for r in comparisons),
                 max_eef_mm=max(r['max_eef_mm'] for r in comparisons),
                 max_rotation_deg=max(r['max_rotation_deg'] for r in comparisons),
                 max_gripper_delta=max(r['max_gripper_delta'] for r in comparisons),
                 release_timing_changed=sum(r['base_first_open']!=r['candidate_first_open'] for r in comparisons),
                 executable_open_changed=sum(r['base_first_open_window']!=r['candidate_first_open_window'] for r in comparisons),
                 origins={origin:dict(comparisons=len(rows),exact=sum(r['raw_exact'] for r in rows),
                    new_failures=sum(r['base_success'] and not r['candidate_success'] for r in rows),
                    executable_open_changed=sum(r['base_first_open_window']!=r['candidate_first_open_window'] for r in rows))
                    for origin in sorted({r['origin'] for r in comparisons})
                    if (rows:=[r for r in comparisons if r['origin']==origin])},
                 masked_cache_hits=cache.hits,
                 latency={label:{k:quantiles([r[k] for r in timing if r['candidate']==flag])
                          for k in ('wall_ms','preprocess','model_h2d_d2h','ik','policy')}
                          for label,flag in [('baseline',False),('candidate',True)]},
                 parameter_grads_absent=all(p.grad is None for p in policy.model.parameters()),
                 max_memory_allocated=torch.cuda.max_memory_allocated(),
                 max_memory_reserved=torch.cuda.max_memory_reserved(),finished_wall_ns=time.time_ns(),
                 robot_commands=0,production_changes=0)
    assert summary['parameter_grads_absent']
    both_success={i for i in range(args.timing_pairs)
                  if all(r['success'] for r in timing if r['pair']==i)}
    summary['both_success_timing_pairs']=len(both_success)
    summary['latency_both_success']={label:{k:quantiles([r[k] for r in timing
        if r['candidate']==flag and r['pair'] in both_success])
        for k in ('wall_ms','preprocess','model_h2d_d2h','ik','policy')}
        for label,flag in [('baseline',False),('candidate',True)]}
    if profiles:
        summary['profile']={k:quantiles([r['spans'][k]['gpu_ms'] for r in profiles if k in r['spans']])
                            for k in profiles[0]['spans']}
    save(args.output/'summary.json',summary)
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    main()
