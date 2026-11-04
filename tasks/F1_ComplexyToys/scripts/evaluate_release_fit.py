#!/usr/bin/env python3
"""Paired generation and decomposed flow loss for a diagnostic fit/probe panel.

Reads original train/heldout packages. No RTC, network, robot output, or release
deployment. Parent-training examples and episode-heldout examples stay separate.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
import time

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'protocol', 'weights', 'core', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == '4'
    a.output.mkdir(parents=True, exist_ok=False)
    import torch
    from safetensors.torch import load_file
    from f1_toys.dataset import F1Dataset
    from f1_toys.eef import physical_actions
    from f1_toys.inference import load_core, batched_observation
    from f1_toys.processing import sha256
    from evaluate_eef import errors
    protocol = json.loads(a.protocol.read_text())
    cfg_mod, model_mod = load_core(a.core)
    cfg = cfg_mod.Pi0Config(pi05=True, action_dim=32, action_horizon=50, max_token_len=200,
        action_loss_mask=tuple(i<10 for i in range(32)), mask_inactive_action_inputs=True,
        train_image_augmentation=False)
    with torch.device('meta'):
        model = cfg.create()
    checkpoint = a.weights.suffix == '.pt'
    state = torch.load(a.weights, map_location='cpu', mmap=True, weights_only=True) if checkpoint else load_file(str(a.weights))
    if checkpoint:
        assert all(k.startswith('model.') for k in state)
        state = {k.removeprefix('model.'): v for k,v in state.items()}
    model.load_state_dict(state, strict=True, assign=True)
    del state
    model = model.cuda().eval()
    capture = {}
    def output_hook(module, args, output):
        capture['velocity'] = output.detach()
    hook = model.action_out_proj.register_forward_hook(output_hook)
    records, invariance = [], None
    out = (a.output/'results.jsonl').open('x')
    with torch.inference_mode():
        for split in ('train', 'validation'):
            root = a.dataset/split
            panel = protocol['datasets'][split]
            assert sha256(root/'manifest.json') == panel['source_dataset_manifest_sha256']
            ds = F1Dataset(root, training=False)
            for case in panel['cases'] + panel['probes']:
                index, L = case['source_index'], case['valid_steps']
                assert ds.rows[index]['episode_id'] == case['episode_id'] and int(ds.action_time_mask[index].sum()) == L
                data, labels, mask = ds[index]
                observation = batched_observation(data, model_mod.Observation, 'cuda')
                actions = torch.tensor(labels[None], device='cuda')
                time_mask = torch.tensor(mask[None], device='cuda')
                for seed_index, seed in enumerate(protocol['seeds']):
                    started = time.perf_counter()
                    with torch.autocast('cuda', dtype=torch.bfloat16):
                        pred = model.sample_actions(observation, num_steps=protocol['denoise_steps'],
                            rng=torch.Generator(device='cuda').manual_seed(seed))
                        noise = torch.randn(actions.shape, device='cuda', dtype=torch.float32,
                            generator=torch.Generator(device='cuda').manual_seed(seed+123))
                        flow_time = torch.tensor([(.2,.5,.8)[seed_index]], device='cuda')
                        loss = model.compute_loss(observation, actions, train=False, noise=noise,
                            time=flow_time, action_time_mask=time_mask)
                        reduced = model.reduce_action_loss(loss, time_mask)
                    velocity = capture['velocity'][..., :10].float()
                    # Match core casting/subtraction order, then select active dimensions.
                    target = (noise.to(model.embed_dtype)-actions.to(model.embed_dtype))[..., :10].float()
                    dim_loss = (velocity-target).square()[:, :L].mean((0,1))
                    assert torch.allclose(dim_loss.mean(), reduced, atol=1e-5, rtol=1e-4)
                    if L<50 and invariance is None:
                        altered_actions, altered_noise = actions.clone(), noise.clone()
                        altered_actions[:, L:] = 1000.; altered_noise[:, L:] = -1000.
                        altered_actions[:, :, 10:] = 2000.; altered_noise[:, :, 10:] = -2000.
                        with torch.autocast('cuda', dtype=torch.bfloat16):
                            check = model.compute_loss(observation, altered_actions, train=False,
                                noise=altered_noise, time=flow_time, action_time_mask=time_mask)
                        max_error = float((check[:, :L]-loss[:, :L]).abs().max())
                        assert max_error < 1e-5, max_error
                        invariance = dict(split=split, source_index=index, valid_steps=L,
                            invalid_time_and_coordinate_perturbation_max_valid_loss_difference=max_error)
                    raw = pred[0].float().cpu().numpy()
                    assert np.isfinite(raw).all() and not raw[:,10:].any()
                    physical = physical_actions(raw, ds.stats['action'])
                    grip = np.clip(physical[:L,9]*50+50,0,100)
                    opens = np.flatnonzero(grip<=10)
                    first = int(opens[0]) if len(opens) else None
                    row = dict(**case, split=split, seed=seed, first_predicted_open=first,
                        valid_error=errors(physical,ds.actions[index],L),
                        prefix16_error=errors(physical,ds.actions[index],min(L,16)),
                        predicted_gripper=grip.tolist(),
                        normalized_actions32=raw.tolist(),
                        flow_loss=float(reduced), flow_loss_xyz=float(dim_loss[:3].mean()),
                        flow_loss_rotation=float(dim_loss[3:9].mean()), flow_loss_gripper=float(dim_loss[9]),
                        flow_time=float(flow_time[0]), prediction_and_loss_ms=(time.perf_counter()-started)*1000)
                    if case['group']=='release_imminent':
                        expert=case['first_expert_open']
                        row.update(open_missing=first is None, early_open=first is not None and first<expert-3,
                            open_within_3_frames=first is not None and abs(first-expert)<=3,
                            open_timing_error_lower_bound_frames=(first if first is not None else L)-expert)
                    if case['group']=='closed_no_release_h50':
                        row.update(false_open=first is not None, false_open_prefix16=first is not None and first<16,
                                   false_open_prefix32=first is not None and first<32)
                    if case['group']=='post_release_open_proxy':
                        row['post_release_closed_prefix16'] = bool((grip[:min(16,L)]>=90).any())
                    records.append(row);out.write(json.dumps(row)+'\n');out.flush()
                print(json.dumps(dict(split=split,source_index=index,panel_role=case['panel_role'],predictions=len(records))),flush=True)
    hook.remove();out.close()
    groups = defaultdict(list)
    for row in records:
        groups['/'.join((row['split'],row['panel_role'],row['group']))].append(row)
    summary = {}
    metric_names = ('open_missing','early_open','open_within_3_frames','open_timing_error_lower_bound_frames',
        'false_open','false_open_prefix16','false_open_prefix32','post_release_closed_prefix16',
        'flow_loss','flow_loss_xyz','flow_loss_rotation','flow_loss_gripper')
    for name, rows in groups.items():
        metrics = dict(cases=len(rows)//len(protocol['seeds']), predictions=len(rows),
            episodes=len({r['episode_id'] for r in rows}),
            valid_error={k:float(np.mean([r['valid_error'][k] for r in rows])) for k in rows[0]['valid_error']},
            prefix16_error={k:float(np.mean([r['prefix16_error'][k] for r in rows])) for k in rows[0]['prefix16_error']})
        for key in metric_names:
            if key in rows[0]:
                metrics[key] = float(np.mean([r[key] for r in rows]))
        summary[name] = metrics
    assert invariance is not None, 'Panel must contain a partial-window invariant check'
    assert all(param.grad is None for param in model.parameters())
    report = dict(schema='f1_release_fit_evaluation_v1', weights_sha256=sha256(a.weights),
        protocol_sha256=sha256(a.protocol), robot_commands=0, diagnostic_only=True,
        seeds=protocol['seeds'], denoise_steps=protocol['denoise_steps'],
        scope=protocol['scope'], actual_loss_mask_invariance=invariance, summary=summary)
    (a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__ == '__main__':
    main()
