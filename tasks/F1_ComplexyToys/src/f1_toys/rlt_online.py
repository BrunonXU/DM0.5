"""F1 temporal/action-mask adapter for RLinf's RLT actor and twin critics."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import torch
from torch import nn

from .rlt_stage2 import make_policy, actor_parameters, bc_targets, td_target, update_target


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.partial')
    with tmp.open('w') as handle:
        json.dump(value, handle, allow_nan=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(8*1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def actor_bc_loss(pi, labels, mask, mode='executed_prefix_v1'):
    """The reference tail is regularization, never a fabricated RL transition.

    labels must already substitute experts only on real executed human steps.
    The split variant preserves the historical expert-prefix loss weight.
    """
    error = (pi.reshape_as(labels)-labels).square().mean(-1)
    prefix = (error*mask).sum()/mask.sum()
    tail = (error*~mask).sum()/(~mask).sum().clamp_min(1)
    if mode == 'executed_prefix_v1':
        loss = prefix
    elif mode == 'full_reference_v2':
        loss = error.mean()
    elif mode == 'prefix_plus_reference_tail_v2':
        loss = prefix+tail
    else:
        raise ValueError('Unknown actor BC coverage contract')
    return loss, prefix, tail


class TemporalCritic(nn.Module):
    """Condition Q on the executed prefix and its real elapsed duration.

    The existing actor architecture/weights are unchanged. Critics are a new
    version: a partial action is not represented as an executed padded tail.
    """
    def __init__(self, original, chunk_len, *, condition_on_elapsed=True,
                 rotation_projection=None):
        super().__init__()
        self.q_head = copy.deepcopy(original)
        self.chunk_len = chunk_len
        self.condition_on_elapsed = condition_on_elapsed
        if rotation_projection not in (None, 'serving_gram_schmidt_v1'):
            raise ValueError('Unknown critic rotation projection')
        self.rotation_projection = rotation_projection
        for head in self.q_head.qs:
            first = head.net[0]
            widened = nn.Linear(first.in_features+chunk_len+1, first.out_features)
            with torch.no_grad():
                widened.weight.zero_()
                widened.weight[:, :first.in_features].copy_(first.weight)
                widened.bias.copy_(first.bias)
            head.net[0] = widened

    def forward(self, state, actions, mask, elapsed, execution_fraction=None):
        actions = actions.reshape(len(state), self.chunk_len, -1)
        if (mask.dtype != torch.bool or mask.shape != actions.shape[:2]
                or not mask[:, 0].all() or (mask[:, 1:] & ~mask[:, :-1]).any()):
            raise ValueError('Critic requires a nonempty contiguous executed prefix')
        if self.rotation_projection:
            # FK labels are orthonormal but raw actor 6D outputs need not be.
            # Evaluate the same physical rotation as serving; equivalent 6D
            # representatives must not offer different fictitious Q rewards.
            first, second = actions[..., 3:6], actions[..., 6:9]
            norm = first.norm(dim=-1, keepdim=True)
            first = first / norm.clamp_min(1e-6)
            second = second - (second*first).sum(-1, keepdim=True)*first
            other = second.norm(dim=-1, keepdim=True)
            if (((norm[..., 0] < 1e-6) | (other[..., 0] < 1e-6)) & mask).any():
                raise ValueError('Critic cannot decode degenerate active rotation6D')
            second = second / other.clamp_min(1e-6)
            actions = torch.cat((actions[..., :3], first, second, actions[..., 9:]), -1)
        # In the controller-input contract elapsed duration is an outcome of
        # execution, not known to the actor before acting. It belongs in the
        # SMDP discount; don't query Q with a fictional fixed 16-step duration.
        duration = elapsed if self.condition_on_elapsed else torch.zeros_like(elapsed)
        fraction=mask.float() if execution_fraction is None else execution_fraction
        if (fraction.shape != mask.shape or not torch.isfinite(fraction).all()
                or ((fraction <= 0) & mask).any() or (fraction > 1).any()
                or (fraction[~mask] != 0).any()):
            raise ValueError('Invalid consumed action fractions')
        action = torch.cat(((actions*mask[..., None]).flatten(1), fraction,
                            duration.reshape(-1, 1)/self.chunk_len), dim=-1)
        return self.q_head(state, action)


class LocalLearner:
    """CPU/GPU small-head learner; called by the official async runner worker."""
    def __init__(self, core, cfg, initial_head, *, device='cpu'):
        self.cfg, self.device = dict(cfg), torch.device(device)
        manifest = json.loads((Path(initial_head)/'manifest.json').read_text())
        if (manifest['head_sha256'] != sha(Path(initial_head)/'head.safetensors')
                or manifest['adapter_sha256'] != sha(Path(__file__).with_name('rlt_stage2.py'))
                or manifest['core_rlt_policy_sha256'] != sha(Path(core)/'rlinf/models/embodiment/mlp_policy/rlt_mlp_policy.py')
                or manifest['model_config'] != dict(cfg['model'])):
            raise ValueError('Initial actor weights/code/config identity mismatch')
        self.model = make_policy(core, cfg['model']).to(self.device)
        from safetensors.torch import load_file
        weights = load_file(str(Path(initial_head)/'head.safetensors'))
        self.model.load_state_dict(weights, strict=True)
        if cfg.get("input_conditioning"):
            from .rlt_conditioning import install_conditioning
            install_conditioning(self.model, cfg["input_conditioning"])
        # Changed objective: do not inherit the old retreat critic values.
        fresh = make_policy(core, cfg['model']).to(self.device)
        from .rlt_action_contract import REFERENCE, SUBMITTED, DECISION
        representation = cfg.get('action_representation', SUBMITTED)
        if representation not in (REFERENCE, SUBMITTED, DECISION):
            raise ValueError('Unknown learner action representation')
        projection = cfg.get('critic_rotation_projection')
        if projection and representation not in (REFERENCE, DECISION):
            raise ValueError('Rotation projection requires the controller-input contract')
        self.critic = TemporalCritic(fresh.q_head, cfg['model']['chunk_len'],
            condition_on_elapsed=representation == SUBMITTED,
            rotation_projection=projection).to(self.device)
        self.target = copy.deepcopy(self.critic).requires_grad_(False)
        self.model.q_head.requires_grad_(False)
        self.actor_optim = torch.optim.Adam(actor_parameters(self.model), lr=cfg['learning_rate'])
        self.critic_optim = torch.optim.Adam(self.critic.parameters(), lr=cfg['learning_rate'])
        self.step = self.q_updates = 0

    def update(self, batch, *, terminal_batch=None, update_actor=True, bootstrap=True):
        data = {k: {n: v.to(self.device) for n, v in x.items()} if isinstance(x, dict)
                else x.to(self.device) for k, x in batch.items()}
        obs, nxt = data['obs'], data['next_obs']
        actions, mask, elapsed = data['actions'], data['action_time_mask'], data['elapsed_steps']
        if not all(torch.isfinite(v).all() for v in (*obs.values(), *nxt.values(), actions, elapsed, data['reward'], data['discount'])):
            raise ValueError('Nonfinite replay batch')
        with torch.no_grad():
            from .rlt_prefix import with_prefix, planned_mask
            next_actions = self.model.sac_forward(nxt, deterministic=not self.cfg.get('stochastic_actor', False))[0]
            next_actions = with_prefix(next_actions, nxt)
            next_mask = planned_mask(nxt, mask)
            next_q = self.target(self.model._critic_state(nxt), next_actions, next_mask,
                                 next_mask.sum(-1).to(elapsed)).min(-1, keepdim=True).values
            target = td_target(data['reward'].reshape(-1, 1), data['terminated'].reshape(-1, 1),
                               data['discount'].reshape(-1, 1), next_q)
        predicted = self.critic(self.model._critic_state(obs), actions, mask, elapsed, data.get('execution_fraction'))
        supervised = data['terminated'].bool()
        if not bootstrap and not supervised.any():
            raise ValueError('Terminal-only calibration needs real terminal samples')
        q_loss = ((predicted-target).square().mean() if bootstrap else
                  (predicted[supervised]-data['reward'][supervised, None]).square().mean())
        conservative_loss = None
        conservative_weight = float(self.cfg.get('conservative_q_weight', 0.))
        if conservative_weight < 0:
            raise ValueError('Negative conservative Q weight')
        if conservative_weight:
            # CQL-style regularization at the SAME real prefix/duration.
            # Counterfactual proposals have no invented rewards or outcomes.
            with torch.no_grad():
                actor_actions = self.model.sac_forward(obs, deterministic=True)[0].reshape_as(actions)
                teacher = self.model._get_ref_chunk(obs).reshape_as(actions)
                generator = torch.Generator(device=actions.device).manual_seed(int(self.cfg['seed'])+104729*self.step+31)
                perturbed = actions + .05*torch.randn(actions.shape, device=actions.device,
                                                     dtype=actions.dtype, generator=generator)
                proposals = [with_prefix(x, obs) for x in (actor_actions, teacher, perturbed)]
            qs = torch.stack([self.critic(self.model._critic_state(obs), a, mask, elapsed,
                              data.get('execution_fraction')) for a in proposals], dim=0)
            conservative_loss = (torch.logsumexp(qs, dim=0)-torch.log(qs.new_tensor(len(proposals)))-predicted).mean()
            q_loss = q_loss + conservative_weight*conservative_loss
        terminal_loss = None
        if terminal_batch is not None:
            extra = {k: {n: v.to(self.device) for n, v in x.items()} if isinstance(x, dict)
                     else x.to(self.device) for k, x in terminal_batch.items()}
            if not extra['terminated'].all() or extra['truncated'].any():
                raise ValueError('Auxiliary critic anchors must be verified task terminals')
            tq = self.critic(self.model._critic_state(extra['obs']), extra['actions'],
                             extra['action_time_mask'], extra['elapsed_steps'], extra.get('execution_fraction'))
            terminal_loss = (tq-extra['reward'].reshape(-1, 1)).square().mean()
            q_loss = q_loss + float(self.cfg.get('terminal_anchor_weight', 1.))*terminal_loss
        if not torch.isfinite(q_loss):
            raise ValueError('Nonfinite critic loss')
        self.critic_optim.zero_grad(set_to_none=True)
        q_loss.backward()
        q_grad = torch.nn.utils.clip_grad_norm_(self.critic.parameters(), self.cfg['clip_grad'], error_if_nonfinite=True)
        self.critic_optim.step()
        update_target(self.target, self.critic, self.cfg['tau'])
        metrics = dict(critic_loss=q_loss.item(), critic_grad=float(q_grad),
                       q_mean=predicted.mean().item(), target_mean=target.mean().item(),
                       q_min=predicted.min().item(), q_max=predicted.max().item(),
                       q_bound_violation_fraction=((predicted < 0) | (predicted > 1)).float().mean().item(),
                       learning_rate=self.actor_optim.param_groups[0]['lr'])
        terminal = data['terminated'].bool()
        if terminal.any():
            metrics['training_terminal_mae'] = (predicted[terminal]-target[terminal]).abs().mean().item()
        if terminal_loss is not None:
            metrics['critic_terminal_anchor_loss'] = terminal_loss.item()
        if conservative_loss is not None:
            metrics['critic_conservative_loss'] = conservative_loss.item()
        if (update_actor and self.step >= int(self.cfg.get('critic_warmup_steps', 0))
                and self.step % self.cfg['actor_update_interval'] == 0):
            pi = self.model.sac_forward(obs, deterministic=not self.cfg.get('stochastic_actor', False), apply_reference_dropout=True,
                reference_dropout_prob=self.cfg['reference_dropout_prob'])[0]
            pi = with_prefix(pi, obs)
            from .rlt_supervision import expert_mask
            labels, human = bc_targets(self.model, obs, actions, expert_mask(data))
            bc, prefix_bc, tail_bc = actor_bc_loss(pi, labels, mask,
                self.cfg.get('actor_bc_coverage', 'executed_prefix_v1'))
            self.critic.requires_grad_(False)
            try:
                plan_mask = planned_mask(obs, mask)
                q = self.critic(self.model._critic_state(obs), pi, plan_mask,
                                plan_mask.sum(-1).to(elapsed))[:, 0].mean()
                weight = self.cfg['q_weight'] if self.cfg.get('enable_q_guidance', False) else 0.
                warmup = int(self.cfg.get('critic_warmup_steps', 0))
                ramp = int(self.cfg.get('q_ramp_steps', 0))
                if warmup < 0 or ramp < 0:
                    raise ValueError('Negative Q schedule')
                if self.step < warmup:
                    weight = 0.
                elif ramp:
                    weight *= min(1., (self.step-warmup+1)/ramp)
                loss = self.cfg['bc_weight']*bc-weight*q
                if self.cfg.get('gradient_diagnostics', False):
                    params = list(actor_parameters(self.model))
                    bg = torch.autograd.grad(self.cfg['bc_weight']*bc, params, retain_graph=True, allow_unused=True)
                    qg = torch.autograd.grad(-weight*q, params, retain_graph=True, allow_unused=True)
                    bn = sum(g.square().sum() for g in bg if g is not None).sqrt()
                    qn = sum(g.square().sum() for g in qg if g is not None).sqrt()
                    dot = sum((a*b).sum() for a,b in zip(bg,qg) if a is not None and b is not None)
                    metrics.update(actor_bc_grad=float(bn), actor_q_grad=float(qn),
                        actor_gradient_cosine=float(dot/(bn*qn).clamp_min(1e-20)))
                self.actor_optim.zero_grad(set_to_none=True)
                loss.backward()
                grad = torch.nn.utils.clip_grad_norm_(actor_parameters(self.model), self.cfg['clip_grad'], error_if_nonfinite=True)
                self.actor_optim.step()
                self.q_updates += int(weight != 0)
            finally:
                self.critic.requires_grad_(True)
            metrics.update(actor_loss=loss.item(), bc_loss=bc.item(), actor_grad=float(grad),
                           bc_prefix_loss=prefix_bc.item(), bc_reference_tail_loss=tail_bc.item(),
                           actor_q_value=q.item(), actor_q_weight=weight,
                           actor_q_loss=-weight*q.item(),
                           actor_reference_mse=(pi.reshape_as(labels)-obs['ref_chunk'][:, :mask.shape[1]]).square().mean().item(),
                           human_fraction=((human & mask).sum()/mask.sum()).item())
        self.step += 1
        return dict(metrics, step=self.step, q_guidance_updates=self.q_updates)

    def save(self, directory, identity):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory/'learner.pt'
        temporary = directory/'learner.partial'
        torch.save(dict(identity=identity, step=self.step, q_updates=self.q_updates,
                        model=self.model.state_dict(), critic=self.critic.state_dict(), target=self.target.state_dict(),
                        actor_optimizer=self.actor_optim.state_dict(), critic_optimizer=self.critic_optim.state_dict(),
                        torch_rng=torch.get_rng_state()), temporary)
        os.replace(temporary, target)
        return sha(target)

    def restore(self, directory, identity):
        saved = torch.load(Path(directory)/'learner.pt', map_location=self.device, weights_only=True)
        if saved['identity'] != identity:
            raise ValueError('Resume identity mismatch')
        for name in ('model', 'critic', 'target'):
            getattr(self, name).load_state_dict(saved[name], strict=True)
        self.actor_optim.load_state_dict(saved['actor_optimizer'])
        self.critic_optim.load_state_dict(saved['critic_optimizer'])
        self.step, self.q_updates = saved['step'], saved['q_updates']
        torch.set_rng_state(saved['torch_rng'].cpu())
