"""Held-out action checks for candidate promotion, not a robot success estimate."""
from __future__ import annotations
import hashlib
import math
import torch


def actor_digest(model):
    h = hashlib.sha256()
    from .rlt_conditioning import export_actor_state
    for key, value in sorted(export_actor_state(model).items()):
        if key.startswith(('backbone.', 'actor_mean.')):
            value = value.detach().cpu().contiguous()
            h.update(key.encode()); h.update(str(tuple(value.shape)).encode())
            h.update(value.numpy().tobytes())
    return h.hexdigest()


def orientation_metrics(predicted, target, mask):
    """Report full SO(3) error, not one Euler angle or raw 6D MSE.

    Use the serving decoder's row convention. Degenerate rotations are counted
    explicitly; these diagnostics introduce no motion threshold or clipping.
    """
    def rotation(values):
        a, b = values[..., 3:6], values[..., 6:9]
        norm = a.norm(dim=-1, keepdim=True)
        first = a / norm.clamp_min(1e-6)
        second = b - (b*first).sum(-1, keepdim=True)*first
        second_norm = second.norm(dim=-1, keepdim=True)
        second = second / second_norm.clamp_min(1e-6)
        valid = (norm[..., 0] >= 1e-6) & (second_norm[..., 0] >= 1e-6)
        valid &= torch.isfinite(values[..., 3:9]).all(-1)
        return torch.stack((first, second, torch.cross(first, second, dim=-1)), -2), valid
    actual, av = rotation(predicted)
    desired, dv = rotation(target)
    valid = mask & av & dv
    angles = torch.rad2deg(torch.acos(((actual*desired).sum((-1, -2))-1).div(2).clamp(-1, 1)))
    errors = angles[valid]
    # Zero is a placeholder only when coverage is zero, made explicit below.
    return dict(rotation_mae_deg=float(errors.mean()) if len(errors) else 0.,
                rotation_p95_deg=float(torch.quantile(errors, .95)) if len(errors) else 0.,
                rotation_max_deg=float(errors.max()) if len(errors) else 0.,
                rotation_valid_steps=int(valid.sum()),
                invalid_rotation_steps=int((mask & ~(av & dv)).sum()))


@torch.no_grad()
def action_metrics(model, data, *, allow_incomplete=False):
    predicted = model.sac_forward(data['obs'], deterministic=True)[0].reshape_as(data['actions'])
    from .rlt_prefix import with_prefix
    predicted = with_prefix(predicted,data['obs'])
    from .rlt_supervision import expert_mask
    mask = data['action_time_mask'] & expert_mask(data) if 'expert_approved' in data else data['action_time_mask'] & data['intervene_flags']
    human = data['actions']
    opening = mask & (human[..., 9] < -.5)
    holding = mask & (human[..., 9] > .5)
    if (not mask.any() or not opening.any() or not holding.any()) and not allow_incomplete:
        raise ValueError('Validation needs human placement/release and holding actions')
    diff = predicted-human
    metrics = dict(human_mse=float(diff[mask].square().mean()) if mask.any() else None,
        xyz_rmse=float(diff[..., :3][mask].square().mean().sqrt()) if mask.any() else None,
        gripper_mae=float(diff[..., 9][mask].abs().mean()) if mask.any() else None,
        release_recall=float((predicted[..., 9][opening] < 0).float().mean()) if opening.any() else None,
        premature_release=float((predicted[..., 9][holding] < 0).float().mean()) if holding.any() else None,
        human_steps=int(mask.sum()), release_steps=int(opening.sum()), hold_steps=int(holding.sum()),
        finite=bool(torch.isfinite(predicted).all()))
    metrics.update(orientation_metrics(predicted, human, mask))
    return metrics


def grouped_action_metrics(model, data, episode_ids):
    """Report independent trajectories, without pretending frames are trials."""
    from .rlt_critic_calibration import subset
    if len(episode_ids)!=len(data['actions']):raise ValueError('Evaluation episode identity mismatch')
    groups={}
    for episode in sorted(set(episode_ids)):
        indices=torch.tensor([i for i,e in enumerate(episode_ids) if e==episode])
        groups[episode]=action_metrics(model,subset(data,indices),allow_incomplete=True)
    averages={}
    for key in ('human_mse','xyz_rmse','gripper_mae','release_recall','premature_release','rotation_mae_deg'):
        values=[v[key] for v in groups.values() if v['human_steps'] and v[key] is not None]
        averages[key]=sum(values)/len(values) if values else None
    return dict(episodes=groups,episode_equal_mean=averages,
                scope='development action agreement; not autonomous success rate')


def promotion_checks(candidate, baseline, incumbent=None):
    """Predeclared conservative diagnostic screen; onsite outcome still needed."""
    if not all(isinstance(v, (bool, int, float)) and math.isfinite(v) for v in candidate.values()):
        return {'finite': False}
    reference = incumbent or baseline
    return dict(finite=candidate['finite'],
        validation_coverage=candidate['release_steps'] >= 1 and candidate['hold_steps'] >= 1,
        human_fit=candidate['human_mse'] <= min(reference['human_mse'], baseline['human_mse'])*1.05,
        translation_nonregression=candidate['xyz_rmse'] <= min(reference['xyz_rmse'], baseline['xyz_rmse'])*1.05,
        gripper_improvement=candidate['gripper_mae'] <= min(baseline['gripper_mae']*.95, reference['gripper_mae']*1.05),
        release_response=candidate['release_recall'] > 0 and candidate['release_recall'] >= reference['release_recall'],
        no_extra_premature_release=candidate['premature_release'] <= reference['premature_release'])


def evaluate_candidate(model, baseline, validation, identity, incumbent=None):
    cand, base = action_metrics(model, validation), action_metrics(baseline, validation)
    previous = None if incumbent is None else action_metrics(incumbent, validation)
    checks = promotion_checks(cand, base, previous)
    return dict(schema='f1_rlt_candidate_evaluation_v1', objective='approach_place_release_v1',
        actor_state_sha256=actor_digest(model), validation_identity=identity,
        candidate=cand, baseline=base, incumbent=previous, checks=checks,
        passed=all(checks.values()), scope='offline_action_screen_not_robot_success')


def supervised_online_checks(report):
    """Admit a supervised trial, not a claim that policy performance improved.

    Q calibration and BC distance remain visible diagnostics. They are not
    prerequisites for learning a policy different from the demonstrations.
    Physical target/collision guards remain enforced by the sole F1 owner.
    """
    report = dict(report)
    report['diagnostic_checks'] = dict(report['checks'])
    metrics = report['candidate']
    checks = dict(
        finite=bool(metrics['finite']) and all(math.isfinite(v) for v in metrics.values()),
        validation_coverage=metrics['release_steps'] > 0 and metrics['hold_steps'] > 0,
        ik_nonregression=report.get('kinematics', {}).get('passed') is True)
    if 'critic' in report:
        checks['critic_finite'] = report['critic']['checks']['finite']
    report.update(checks=checks, passed=all(checks.values()),
                  promotion_policy='supervised_online_v1',
                  scope='supervised_trial_requires_runtime_guards_not_success_improvement')
    return report


@torch.no_grad()
def kinematic_screen(model, baseline, data, records, stats):
    """Decode the same logged anchors; never submit targets to robot hardware."""
    import numpy as np
    from .eef import decode_prefix, physical_actions, solver
    if len(records) != len(data['actions']):
        raise ValueError('Kinematic anchors do not match validation tensors')
    chosen = sorted(set(np.linspace(0, len(records)-1, min(12, len(records)), dtype=int).tolist()
                        + torch.nonzero(data['terminated']).flatten().tolist()))
    from .rlt_prefix import with_prefix
    predictions = [with_prefix(m.sac_forward(data['obs'], deterministic=True)[0],data['obs'])
                   .reshape(-1,16,10).cpu().numpy() for m in (baseline, model)]
    ik = solver(); results=[]
    for index in chosen:
        record=records[index]; row={'index':index, 'observation_id':record['observation_id']}
        for label,values in zip(('baseline','candidate'),predictions):
            raw=np.zeros((16,32),np.float32);raw[:,:10]=values[index]
            try:
                # Decision records retain original normalized proposals rather
                # than an invented submitted16 chunk. This offline screen
                # solves right-arm IK from the measured anchor; other axes are
                # held at that observed state and are never published.
                held=record['submitted16'][0] if record.get('submitted16') else record['state16']
                if record.get('rtc') is not None:
                    from .rtc import decode_window
                    result=decode_window(ik,physical_actions(raw,stats),np.asarray(record['state16']),
                        np.asarray(held),np.asarray(record['rtc']['committed_actions16']).reshape(-1,16),16)
                else:
                    result=decode_prefix(ik,physical_actions(raw,stats),np.asarray(record['state16']),
                                         np.asarray(held),execute_steps=16)
                row[label]=bool(result['success'])
            except (ValueError, FloatingPointError):row[label]=False
        results.append(row)
    return dict(passed=not any(r['baseline'] and not r['candidate'] for r in results),
                anchors=results, scope='logged_anchor_ik_nonregression_not_robot_success')
