"""F1 ports of NewLoRA fixed manifests, paired seeds and episode statistics.

Counts/seeds are explicit caller inputs, never silently chosen for a formal run.
"""
from __future__ import annotations
import hashlib
import json
import math
import numpy as np
from f1_data.reader import require


def digest_document(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def paired_seed(seed, episode, start, stream):
    # Unlike an iteration offset, this remains stable if windows are reordered.
    key = json.dumps([int(seed), episode, int(start), stream], separators=(',', ':'))
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'little')


def build_selection(release, *, windows_per_episode, generation_seeds, flow_windows_per_episode, flow_seeds):
    """G2 equal-bin midpoints, applied to F1's valid-start list (no gap crossing)."""
    require(windows_per_episode > 0 and flow_windows_per_episode > 0, 'positive window counts required')
    def samples(count):
        result = []
        for ep in sorted(release.for_split('validation'), key=lambda e: e.episode):
            n = len(ep.starts); require(n > 0, 'validation episode has no valid starts')
            used = min(count, n)
            for slot in range(used):
                index = min(n - 1, ((2 * slot + 1) * n) // (2 * used))
                result.append({'episode': ep.episode, 'start': int(ep.starts[index])})
        return result
    require(generation_seeds and flow_seeds, 'explicit independent flow/generation seed sets required')
    return {'diffusion_steps': 10, 'seeds': list(generation_seeds), 'samples': samples(windows_per_episode),
            'flow_seeds': list(flow_seeds), 'flow_samples': samples(flow_windows_per_episode),
            'batch_size': 1, 'seed_schedule': 'sha256_seed_episode_start_stream_v1',
            'sampling': 'equal_bin_midpoints_over_valid_starts', 'all_validation_episodes': True}


def describe_values(values):
    """Means/P90 are over episode means when used for the macro report."""
    finite = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not finite:
        return {'mean': None, 'median': None, 'p90': None, 'worst': None, 'available': 0, 'total': len(values)}
    return {'mean': float(np.mean(finite)), 'median': float(np.median(finite)),
            'p90': float(np.quantile(finite, .9)), 'worst': max(finite),
            'available': len(finite), 'total': len(values)}


def episode_metrics(report):
    """Named scalar fields only; no aggregate metres/degrees/velocity MAE."""
    result = {}
    for name, ep in report['episodes'].items():
        values = {'fm_loss': ep['mean_fm_loss'],
                  'ik_rejection_rate': ep['ik_rejected_prefixes'] / ep['evaluated_windows_times_seeds']}
        for horizon in ('20', '50'):
            for key, value in ep[horizon].items():
                mean = value['mean_over_available_windows_and_seeds']
                if isinstance(mean, (int, float)) or mean is None:
                    values[f'h{horizon}/{key}'] = mean
                elif isinstance(mean, list):
                    for dim, component in enumerate(mean):
                        values[f'h{horizon}/{key}/{dim}'] = component
        for key, value in ep.get('ik_diagnostics', {}).items():
            values['ik/' + key] = value['mean']
        for key, value in ep.get('seed_dispersion', {}).items():
            values['seed/' + key] = value['mean']
        result[name] = values
    return result


def macro_summary(report):
    values = episode_metrics(report)
    keys = sorted(set().union(*(v.keys() for v in values.values())))
    return {key: describe_values([v.get(key) for v in values.values()]) for key in keys}


def paired_comparison(left, right, *, repeats, seed):
    """Port paired episode bootstrap; require exact evaluation/data provenance.

    Invalid/missing metrics are not assigned optimistic ranks or imputed zeros.
    """
    require(repeats > 0, 'positive bootstrap repeat count required')
    require(left.get('comparison_contract') and left['comparison_contract'] == right.get('comparison_contract'),
            'checkpoint reports have different or missing comparison contracts')
    a, b = episode_metrics(left), episode_metrics(right)
    require(a and set(a) == set(b), 'checkpoint episode populations differ')
    episodes = sorted(a)
    keys = sorted(set.intersection(*(set(a[e]) & set(b[e]) for e in episodes)))
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(episodes), size=(repeats, len(episodes)))
    metrics = {}
    for key in keys:
        va, vb = [a[e][key] for e in episodes], [b[e][key] for e in episodes]
        if any(v is None or not np.isfinite(v) for v in va + vb):
            metrics[key] = {'status': 'incomplete_metric', 'paired_episodes': sum(x is not None and y is not None for x,y in zip(va,vb))}
            continue
        delta = np.asarray(va) - np.asarray(vb)
        boot = delta[indices].mean(axis=1)
        metrics[key] = {'status': 'complete', 'left_mean': float(np.mean(va)), 'right_mean': float(np.mean(vb)),
                        'mean_delta_left_minus_right': float(delta.mean()),
                        'ci95_low': float(np.quantile(boot, .025)), 'ci95_high': float(np.quantile(boot, .975)),
                        'left_episode_win_rate': float((delta < 0).mean()), 'paired_episodes': len(episodes)}
    return {'episodes': episodes, 'bootstrap_repeats': repeats, 'bootstrap_seed': seed,
            'automatic_selection': False, 'metrics': metrics,
            'limitation': 'Paired bootstrap supports relative comparison, not equivalence or closed-loop success; multiple comparisons uncorrected.'}


def build_recipe_selection(release, stage):
    from .experiment import read_recipe
    recipe = read_recipe()['evaluation']
    require(stage in ('pass_a', 'pass_b'), 'unknown validation stage')
    spec = recipe[stage]
    selection = build_selection(release, windows_per_episode=spec['generation_windows_per_episode'],
                                generation_seeds=spec['generation_seeds'],
                                flow_windows_per_episode=spec['flow_windows_per_episode'], flow_seeds=spec['flow_seeds'])
    selection['stage'] = stage
    return selection


def rank_candidates(reports, *, weights):
    """Equal-episode H20 metrics, then weighted within-pool fractional ranks.

    This is a shortlist aid, not a loss, success probability or automatic release.
    Incomplete/invalid predictions require inspection and receive no numeric score.
    """
    require(set(weights) == {'arms', 'grippers', 'base'} and
            all(math.isfinite(v) and v >= 0 for v in weights.values()) and
            math.isclose(sum(weights.values()), 1.), 'invalid checkpoint group weights')
    require(len(reports) >= 2, 'at least two frozen candidates required')
    contract = next(iter(reports.values())).get('comparison_contract')
    require(contract and all(r.get('comparison_contract') == contract for r in reports.values()),
            'comparison contracts differ')
    populations = [set(r['episodes']) for r in reports.values()]
    require(populations[0] and all(p == populations[0] for p in populations), 'checkpoint episode populations differ')
    fields = {
        'arms': ['left_position_mm', 'left_rotation_deg_valid_only', 'right_position_mm', 'right_rotation_deg_valid_only'],
        'grippers': ['left_gripper_mae', 'right_gripper_mae'],
        # Exclude k=0: the current reconstructed velocity is already in state19.
        'base': ['future_k1_base_mae/0', 'future_k1_base_mae/1', 'future_k1_base_mae/2'],
    }
    values, rows = {}, {}
    for name, report in reports.items():
        summary = macro_summary(report)
        reasons = []
        values[name] = {}
        for key in sum(fields.values(), []):
            aggregate = summary.get('h20/' + key, {})
            metric = key.split('/')[0]
            if (aggregate.get('mean') is None or aggregate.get('available') != len(populations[0]) or
                any(ep['20'].get(metric, {}).get('available') != ep['20'].get(metric, {}).get('total')
                    for ep in report['episodes'].values())):
                reasons.append('incomplete:' + key)
            else:
                values[name][key] = aggregate['mean']
        for key in ('nonfinite_elements', 'left_rotation_invalid_steps', 'right_rotation_invalid_steps'):
            if any(ep['20'].get(key, {}).get('mean_over_available_windows_and_seeds') != 0
                   for ep in report['episodes'].values()):
                reasons.append('invalid_or_missing:' + key)
        rows[name] = {'status': 'needs_review' if reasons else 'rankable', 'reasons': reasons,
                      'score': None, 'physical_metrics': values[name]}
    eligible = sorted(name for name, row in rows.items() if row['status'] == 'rankable')
    if len(eligible) >= 2:
        for name in eligible:
            ranks = {}
            for key in sum(fields.values(), []):
                pool = [values[n][key] for n in eligible]
                v = values[name][key]
                ranks[key] = (sum(x < v for x in pool) + (sum(x == v for x in pool) - 1) / 2) / (len(pool) - 1)
            groups = {g: float(np.mean([ranks[k] for k in keys])) for g, keys in fields.items()}
            rows[name].update(metric_ranks=ranks, group_ranks=groups,
                              score=sum(weights[g] * groups[g] for g in weights))
    return {'automatic_selection': False, 'lower_is_better': True, 'horizon': 20,
            'weights': weights, 'candidate_pool': sorted(reports), 'rankable_pool': eligible,
            'rows': rows, 'limitation': 'Pool-relative ranks discard error magnitude; review physical deltas, paired intervals, H50, IK and timing before selecting primary/fallback.'}
