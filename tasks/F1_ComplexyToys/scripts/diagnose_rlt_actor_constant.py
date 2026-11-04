#!/usr/bin/env python3
"""Replay saved real actor inputs on CPU; never update parameters or call a robot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

os.environ['CUDA_VISIBLE_DEVICES'] = ''
import torch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stats(x):
    x = x.detach().double()
    return dict(shape=list(x.shape), rms=x.square().mean().sqrt().item(),
                abs_max=x.abs().max().item(),
                between_sample_std=x.flatten(1).std(0, unbiased=False).mean().item())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--run-root', type=Path, required=True)
    p.add_argument('--requests', type=Path, required=True)
    p.add_argument('--session', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); a.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(a.release/'tasks/F1_ComplexyToys/src'))
    from f1_toys.rlt_stage2 import make_policy, bc_targets
    from safetensors.torch import load_file
    import wandb
    torch.set_num_threads(4)
    cfg = json.loads((a.run_root/'recipe.json').read_text())
    head = a.run_root/'bc-audit/export/head.safetensors'
    before = sha(head)
    model = make_policy(a.release/'RLinf', cfg['model']).eval()
    model.load_state_dict(load_file(str(head)), strict=True)
    data = torch.load(a.run_root/'cache/replay.pt', map_location='cpu', weights_only=True)
    rows = []
    with a.requests.open() as f:
        for line in f:
            r = json.loads(line)
            if r.get('session_id') == a.session and r.get('success'):
                rows.append(r)
    assert len(rows) == 36, len(rows)
    replays = [r['prediction_replay'] for r in rows]
    live = {k: torch.tensor([r[k] for r in replays], dtype=torch.float32) for k in ('z_rl', 'proprio')}
    live['ref_chunk'] = torch.tensor([r['reference_normalized_actions32'] for r in replays])[..., :10]
    recorded = torch.tensor([r['actor_normalized_actions32'] for r in replays])[..., :10]
    cached = {k: data[k][data['current']] for k in live}
    torch.save(dict(live=live, recorded=recorded, request_ids=[r['request_id'] for r in rows]), a.output/'live-inputs.pt')
    run = wandb.init(project=cfg['wandb']['project'], entity=cfg['wandb']['entity'],
        group='10_RLT_Training_Diagnostics', job_type='actor-input-evaluation',
        name='BC400_RealRollout_Input_Diagnostic', mode='offline', dir=str(a.output),
        tags=['F1', 'RLT', 'BC400', 'CPU', 'zero_parameter_update'],
        config=dict(head_sha256=before, source_sha256=sha(__file__), session=a.session,
                    parameter_updates=0, reference_dropout_prob=cfg['reference_dropout_prob']))
    result = dict(head_sha256=before, source_sha256=sha(__file__), session=a.session,
                  requests=len(rows), parameter_updates=0, groups={})
    with torch.no_grad():
        for name, obs in [('live', live), ('cache', cached)]:
            action = model.sac_forward(obs, deterministic=True)[0].reshape(-1, 16, 10)
            x = model._actor_state(obs)
            layers = []
            for i, layer in enumerate(model.backbone):
                x = layer(x)
                row = dict(index=i, type=type(layer).__name__, **stats(x))
                if isinstance(layer, torch.nn.Tanh):
                    row['saturated_abs_gt_099'] = (x.abs() > .99).double().mean().item()
                    row['mean_derivative'] = (1-x.square()).mean().item()
                layers.append(row)
            contributions = {}
            linear = model.backbone[0]
            state = model._actor_state(obs)
            for key, start, end in [('ref_chunk', 0, 160), ('z_rl', 160, 2208), ('proprio', 2208, 2216)]:
                contributions[key] = stats(state[:,start:end] @ linear.weight[:,start:end].T)
            ablations = {}
            for key in obs:
                for mode in ('zero', 'reverse'):
                    test = dict(obs, **{key: torch.zeros_like(obs[key]) if mode == 'zero' else obs[key].flip(0)})
                    out = model.sac_forward(test, deterministic=True)[0].reshape_as(action)
                    ablations[key+'_'+mode] = stats(out-action)
            result['groups'][name] = dict(inputs={k:stats(v) for k,v in obs.items()},
                layers=layers, first_linear_contributions=contributions, action=stats(action), ablations=ablations)
            if name == 'live':
                result['cpu_vs_recorded_max_abs'] = (action-recorded).abs().max().item()
            else:
                target, _ = bc_targets(model, obs, data['actions'], data['intervene_flags'])
                result['cache_target'] = stats(target)
                result['cache_mse'] = (action-target).square().mean().item()
                constant = target.mean(0, keepdim=True)
                result['constant_mean_target_mse'] = (constant-target).square().mean().item()
        # Zero-update input-scale probes diagnose saturation only; these are not deployable models.
        probes = {}
        for scale in (1., .1, .01, .001, 0.):
            obs = dict(live, z_rl=live['z_rl']*scale)
            x = model.backbone[0](model._actor_state(obs)).tanh()
            out = model.sac_forward(obs, deterministic=True)[0]
            probes[str(scale)] = dict(first_tanh_saturated=(x.abs()>.99).double().mean().item(), action=stats(out))
        result['token_scale_probes_not_for_deployment'] = probes
    assert sha(head) == before
    result['head_unchanged'] = True
    (a.output/'diagnostic.json').write_text(json.dumps(result, indent=2)+'\n')
    run.log({'diagnostic/cpu_vs_recorded_max_abs':result['cpu_vs_recorded_max_abs'],
             'diagnostic/cache_mse':result['cache_mse'], 'diagnostic/parameter_updates':0})
    run.finish()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
