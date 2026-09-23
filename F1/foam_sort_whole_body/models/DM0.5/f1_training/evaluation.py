"""Fixed-window generated-action evaluation; reports, never ranks checkpoints."""
from __future__ import annotations
import json
import platform
import time
from pathlib import Path
import numpy as np
from f1_data.reader import decode_rotation6d, require, sha256

from .validation_metrics import extra_metrics, seed_dispersion
from .validation_protocol import paired_seed, digest_document, describe_values, macro_summary

def generation_inputs(batch):
    """Generate all physical actions without exposing future label availability."""
    inputs = {k: v for k, v in batch.items() if k not in ('action', 'action_mask')}
    mask = batch['action_mask'].clone()
    mask[..., :23] = True
    mask[..., 23:] = False
    inputs['action_mask'] = mask
    return inputs

def physical_metrics(prediction, truth, state19, action_mask=None, state_mask=None):
    """No thresholds or mixed-unit composite score. Invalid rotations counted."""
    pred, truth = np.asarray(prediction), np.asarray(truth)
    require(pred.shape == truth.shape == (50, 23), 'metric shape mismatch')
    mask = np.ones_like(truth, dtype=bool) if action_mask is None else np.asarray(action_mask, dtype=bool)[:, :23]
    require(mask.shape == truth.shape and mask[:, :20].all(), 'invalid upper-body metric mask')
    current_known = state_mask is None or np.asarray(state_mask, dtype=bool)[16:19].all()
    metrics = {}
    for horizon in (20, 50):
        p, t = pred[:horizon], truth[:horizon]
        result = {'nonfinite_elements': int((~np.isfinite(p)).sum())}
        for side, offset in (('left', 0), ('right', 9)):
            distances = np.linalg.norm(p[:, offset:offset+3] - t[:, offset:offset+3], axis=-1) * 1000
            result[side + '_position_mm'] = float(distances.mean()) if np.isfinite(distances).all() else None
            angles, invalid = [], 0
            for actual, target in zip(p[:, offset+3:offset+9], t[:, offset+3:offset+9]):
                try:
                    ra, rt = decode_rotation6d(actual), decode_rotation6d(target)
                    angles.append(float(np.rad2deg(np.arccos(np.clip((np.trace(ra @ rt.T)-1)/2, -1, 1)))))
                except ValueError:
                    invalid += 1
            result[side + '_rotation_deg_valid_only'] = float(np.mean(angles)) if angles else None
            result[side + '_rotation_invalid_steps'] = invalid
        error = np.abs(p[:, 18:23] - t[:, 18:23])
        for i, name in enumerate(('left_gripper', 'right_gripper', 'vx', 'vy', 'wz')):
            valid = mask[:horizon, 18+i]
            result[name + '_mae'] = float(error[valid, i].mean()) if valid.any() and np.isfinite(error[valid, i]).all() else None
        known = mask[:horizon, 20:23].all(axis=1)
        result['base_known_steps'] = int(known.sum())
        result['base_unknown_steps'] = int((~known).sum())
        moving = np.any(t[:, 20:23] != 0, axis=-1)
        for name, subset in (('moving', moving & known), ('stopped', ~moving & known)):
            result[name + '_steps'] = int(subset.sum())
            result[name + '_base_mae'] = (np.abs(p[subset, 20:23]-t[subset, 20:23]).mean(axis=0).tolist()
                                         if subset.any() and np.isfinite(p[subset, 20:23]).all() else None)
        result['zero_base_mae'] = np.abs(t[known, 20:23]).mean(axis=0).tolist() if known.any() else None
        result['hold_current_base_mae'] = np.abs(t[known, 20:23] - state19[16:19]).mean(axis=0).tolist() if known.any() and current_known else None
        future = known.copy(); future[0] = False
        result['future_k1_base_mae'] = (np.abs(p[future, 20:23] - t[future, 20:23]).mean(axis=0).tolist()
                                       if future.any() and np.isfinite(p[future, 20:23]).all() else None)
        result.update(extra_metrics(p, t, horizon, mask[:horizon]))
        metrics[str(horizon)] = result
    return metrics


def validate_selection(release):
    selection = release.document.get('evaluation')
    require(isinstance(selection, dict), 'frozen validation windows/seeds are missing')
    require(selection.get('diffusion_steps') == 10, 'recipe requires 10 Euler steps')
    episodes = {ep.episode: ep for ep in release.for_split('validation')}
    for stream in ('generation', 'flow'):
        seeds = selection.get('flow_seeds', selection.get('seeds')) if stream == 'flow' else selection.get('seeds')
        samples = selection.get('flow_samples', selection.get('samples', [])) if stream == 'flow' else selection.get('samples', [])
        require(seeds and all(type(s) is int and 0 <= s < 2**32 for s in seeds), 'explicit valid seeds required')
        require(len(set(seeds)) == len(seeds), 'duplicate seeds')
        require(samples and len({(s['episode'], s['start']) for s in samples}) == len(samples), 'empty or duplicate validation samples')
        for item in samples:
            require(item['episode'] in episodes, 'evaluation must use inherited validation sources')
            require(item['start'] in episodes[item['episode']].starts, 'invalid validation window')
        if selection.get('all_validation_episodes') or release.document.get('training_approved'):
            require({s['episode'] for s in samples} == set(episodes), 'validation must cover every release validation episode')
    require({s['episode'] for s in selection.get('flow_samples', selection['samples'])} >= {s['episode'] for s in selection['samples']}, 'FM population must cover generated-action episodes')
    if release.document.get('training_approved'):
        require('flow_seeds' in selection and 'flow_samples' in selection, 'formal validation must freeze independent flow protocol')
    require(selection.get('batch_size', 1) == 1, 'F1 paired evaluator currently fixes batch_size=1')
    return selection


def evaluate_model(model, release, processor, output, *, device, autocast, checkpoint_identity=None):
    """Independent held-out FM and deployed generation; no optimizer or dispatch."""
    import torch
    started = time.monotonic()
    from opendm.data.collator import TrainingCollator
    from opendm.model.dm05.dm05_lora import unwrap_dm05_model
    from .execution import ActionDecoder
    selection = validate_selection(release)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    dataset = release.dataset('validation', processor)
    lookup = {(dataset.episodes[e].episode, t): i for i, (e, t) in enumerate(dataset.index)}
    collator = TrainingCollator(processor.tokenizer.pad_token_id, max_length=1024)
    decoder = ActionDecoder(release.normalizer)
    records, flow_records, seed_records = [], [], []
    was_training = model.training
    model.eval()
    # torch.manual_seed seeds all visible GPUs, so restore every visible device.
    devices = list(range(torch.cuda.device_count())) if torch.device(device).type == 'cuda' else []
    def load(item):
        index = lookup[item['episode'], item['start']]
        episode_index, start = dataset.index[index]
        ep = dataset.episodes[episode_index]
        raw = ep.sample(start)
        batch = {k: v.to(device) for k,v in collator([dataset[index]]).items()}
        return raw, batch
    try:
        with torch.no_grad(), torch.random.fork_rng(devices=devices):
            for item in selection.get('flow_samples', selection['samples']):
                raw, batch = load(item)
                for seed in selection.get('flow_seeds', selection['seeds']):
                    actual_seed = paired_seed(seed, item['episode'], item['start'], 'flow')
                    torch.manual_seed(actual_seed)
                    with autocast():
                        outputs = model(**batch)
                    loss = float(outputs.loss)
                    require(np.isfinite(loss), 'nonfinite held-out FM loss')
                    flow_records.append({**item, 'seed': seed, 'actual_seed': actual_seed, 'fm_loss': loss})
            for sample_index, item in enumerate(selection['samples']):
                raw, batch = load(item)
                predictions = []
                for seed in selection['seeds']:
                    actual_seed = paired_seed(seed, item['episode'], item['start'], 'generation')
                    # Reset directly before sampling: no FM RNG consumption affects generation.
                    torch.manual_seed(actual_seed)
                    inputs = generation_inputs(batch)
                    with autocast():
                        prediction = unwrap_dm05_model(model).inference_action(**inputs, diffusion_steps=10)
                    require(tuple(prediction.shape) == (1,50,32), 'generated action shape mismatch')
                    normalized = prediction[0].float().cpu().numpy()
                    physical = release.normalizer.denormalize_action(normalized)
                    predictions.append(physical)
                    reference = raw['state'][:14]
                    solved = decoder.decode(normalized, reference)
                    ik = {k:solved[k] for k in ('success','reason','step','arm') if k in solved}
                    if solved['success']:
                        joints = np.asarray(solved['joint_targets_deg'])
                        ik['first_step_max_deg'] = float(np.abs(joints[0]-reference).max())
                        ik['consecutive_step_max_deg'] = float(np.abs(np.diff(joints,axis=0)).max())
                        for order, name in ((1,'velocity'),(2,'acceleration'),(3,'jerk')):
                            ik[name + '_max_deg_s' + str(order)] = float(np.abs(np.diff(joints,n=order,axis=0)*30**order).max())
                    records.append({**item,'seed':seed,'actual_seed':actual_seed,'phase':item.get('phase'),
                                    'metrics':physical_metrics(physical, raw['action'][:,:23], raw['state'], raw['action_mask'], raw.get('state_mask')), 'ik':ik})
                    np.savez_compressed(output / f'{sample_index:06d}_seed{seed}.npz', normalized_prediction=normalized,
                                        physical_prediction=physical, physical_target=raw['action'][:,:23], state19=raw['state'],
                                        action_mask=raw['action_mask'], state_mask=raw.get('state_mask', np.ones(19, bool)))
                seed_records.append({**item, 'metrics':seed_dispersion(predictions)})
    finally:
        model.train(was_training)
    grouped = {}
    for ep in sorted({r['episode'] for r in records}):
        subset = [r for r in records if r['episode'] == ep]
        flow = [r['fm_loss'] for r in flow_records if r['episode'] == ep]
        grouped[ep] = {'evaluated_windows_times_seeds':len(subset),
                       'ik_rejected_prefixes':sum(not r['ik']['success'] for r in subset),
                       'mean_fm_loss':float(np.mean(flow)), 'flow':describe_values(flow)}
        for horizon in ('20','50'):
            numeric = {}
            for key in subset[0]['metrics'][horizon]:
                values = [r['metrics'][horizon][key] for r in subset]
                valid = [v for v in values if v is not None]
                numeric[key] = {'mean_over_available_windows_and_seeds':np.mean(valid,axis=0).tolist() if valid else None,
                                'available':len(valid), 'total':len(values)}
                if valid and isinstance(valid[0], (int,float)):
                    numeric[key].update(describe_values(values))
            grouped[ep][horizon] = numeric
        seeds = [r['metrics'] for r in seed_records if r['episode'] == ep]
        keys = sorted(set().union(*(r.keys() for r in seeds)))
        grouped[ep]['seed_dispersion'] = {k:describe_values([r.get(k) for r in seeds]) for k in keys}
        keys = sorted(set().union(*(r['ik'].keys() for r in subset))-{'success','reason','step','arm'})
        grouped[ep]['ik_diagnostics'] = {k:describe_values([r['ik'].get(k) for r in subset]) for k in keys}
    # Content identity, not arbitrary checkpoint names, determines comparability.
    from .experiment import read_recipe
    import transformers
    contract = {'release_sha256':sha256(release.path), 'statistics_sha256':sha256(release.stats_path),
                'selection_sha256':digest_document(selection), 'seed_schedule':'sha256_seed_episode_start_stream_v1',
                'batch_size':1, 'diffusion_steps':10, 'generation_mask':'physical23_padding_only', 'dtype':'model_native_with_caller_autocast',
                'attention':{k:v for k,v in read_recipe()['model'].items() if 'attn' in k},
                'evaluation_code':{p.name:sha256(p) for p in (Path(__file__), Path(__file__).with_name('validation_protocol.py'), Path(__file__).with_name('validation_metrics.py'))},
                'environment':{'torch':torch.__version__,'transformers':transformers.__version__,'numpy':np.__version__, 'python':platform.python_version(), 'device_type':torch.device(device).type,
                               'gpu_name':torch.cuda.get_device_name(device) if torch.device(device).type == 'cuda' else None,
                               'model_dtype':str(next(model.parameters()).dtype) if any(True for _ in model.parameters()) else 'fixture',
                               'peft_wrapped':hasattr(model, 'get_base_model'),
                               'liger_modules':any(type(m).__module__.startswith('liger_kernel') for m in model.modules())}}
    report = {'schema':'f1_generated_action_metrics_v2', 'automatic_checkpoint_selection':False,
              'comparison_contract':contract, 'selection':selection, 'episodes':grouped, 'records':records,
              'flow_records':flow_records, 'seed_records':seed_records,
              'limitations':['No closed-loop success estimate','No invented gripper transition threshold; raw trajectories retained',
                             'IK covers first20, not H50 joint trajectory','No mixed-unit total score',
                             'Dynamics are diagnostics, not newly imposed limits; no hard IK failure-rate cutoff',
                             'Rotation finite-only metrics must be read with invalid counts; tiny validation pools have weak uncertainty estimates']}
    report['macro'] = macro_summary(report)
    report['checkpoint_identity'] = checkpoint_identity
    report['elapsed_seconds'] = time.monotonic() - started
    report['prediction_files'] = {p.name:sha256(p) for p in sorted(output.glob('*.npz'))}
    from .validation_artifacts import write_new
    write_new(output / 'report.json', report)
    return report
