"""W&B identity and existing evaluation metrics; no new loss/selection score."""
import hashlib
import math
import os
from pathlib import Path


def configure_wandb(output_dir, *, resume=False):
    """One stable online run per independent output directory, shared by DDP ranks."""
    if os.environ.get('WANDB_MODE', 'online') not in ('online', 'run'):
        raise ValueError('Formal F1 training requires online W&B monitoring')
    disabled = os.environ.get('WANDB_DISABLED', '').lower()
    if disabled in ('true', '1', 'yes', 'on'):
        raise ValueError('Formal F1 training cannot disable W&B monitoring')
    identity = hashlib.sha256(str(Path(output_dir).resolve()).encode()).hexdigest()[:20]
    run_id = 'f1-' + identity
    if os.environ.get('WANDB_RUN_ID', run_id) != run_id:
        raise ValueError('WANDB_RUN_ID conflicts with this F1 output directory')
    os.environ['WANDB_RUN_ID'] = run_id
    os.environ['WANDB_RESUME'] = 'must' if resume else 'never'
    os.environ['WANDB_MODE'] = 'online'
    # Metrics/config only. Checkpoints remain in the local task output directory.
    os.environ['WANDB_LOG_MODEL'] = 'false'
    return run_id


def evaluation_scalars(report):
    """Flatten per-episode means into equal-episode dashboards, without ranking."""
    episodes = list(report['episodes'].values())
    metrics = {}
    if not episodes:
        return metrics
    def mean(name, values):
        values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        if values:
            metrics['eval_' + name] = sum(values) / len(values)
    mean('fm_loss_episode_mean', [ep['mean_fm_loss'] for ep in episodes])
    mean('ik_rejection_rate_episode_mean', [ep['ik_rejected_prefixes'] / ep['evaluated_windows_times_seeds'] for ep in episodes])
    for horizon in ('20', '50'):
        keys = ('left_position_mm', 'right_position_mm', 'left_rotation_deg_valid_only',
                'right_rotation_deg_valid_only', 'left_rotation_invalid_steps', 'right_rotation_invalid_steps',
                'left_gripper_mae', 'right_gripper_mae', 'vx_mae', 'vy_mae', 'wz_mae', 'nonfinite_elements')
        for key in keys:
            mean(f'h{horizon}/{key}_episode_mean',
                 [ep[horizon][key]['mean_over_available_windows_and_seeds'] for ep in episodes])
        for subset in ('moving', 'stopped'):
            for dim, axis in enumerate(('vx', 'vy', 'wz')):
                values = [ep[horizon][subset + '_base_mae']['mean_over_available_windows_and_seeds'] for ep in episodes]
                mean(f'h{horizon}/{subset}_{axis}_mae_episode_mean', [v[dim] if v is not None else None for v in values])
    # The evaluator already aggregates these by episode; no change to ranking/loss.
    for name, summary in report.get('macro', {}).items():
        for statistic in ('mean', 'median', 'p90', 'worst', 'available', 'total'):
            value=summary.get(statistic)
            if value is not None and math.isfinite(float(value)):
                metrics[f'eval_macro/{name}/{statistic}']=value
    if 'elapsed_seconds' in report:
        metrics['eval_elapsed_seconds']=report['elapsed_seconds']
    return metrics
