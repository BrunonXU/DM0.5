"""Offline F1 Stage2 head adapter. No robot transport or asynchronous runner.

Network layers are the pinned RLinf RLTMLPPolicy. F1's normalized translation
is unbounded, so only rotation/gripper coordinates receive the stock tanh.
The replay contract preserves termination separately from truncation.
"""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types

import torch
from torch import nn


def load_rlt_class(core_root):
    """Load unchanged model modules without RLinf's Ray worker registration."""
    root = Path(core_root).resolve()
    for name in ("rlinf", "rlinf.models", "rlinf.models.embodiment",
                 "rlinf.models.embodiment.modules", "rlinf.models.embodiment.mlp_policy"):
        directory = root.joinpath(*name.split("."))
        if name in sys.modules:
            if str(directory) not in list(getattr(sys.modules[name], "__path__", [])):
                raise ValueError("Cannot mix RLinf source versions: " + name)
        else:
            package = types.ModuleType(name)
            package.__path__ = [str(directory)]
            sys.modules[name] = package
    return importlib.import_module(
        "rlinf.models.embodiment.mlp_policy.rlt_mlp_policy").RLTMLPPolicy


def make_policy(core_root, config):
    base = load_rlt_class(core_root)

    class F1RLTPolicy(base):
        def sac_forward(self, obs, apply_reference_dropout=False,
                        reference_dropout_prob=0.0, deterministic=False, **kwargs):
            state = self._actor_state(obs, apply_reference_dropout=apply_reference_dropout,
                                     reference_dropout_prob=reference_dropout_prob)
            mean = self.actor_mean(self.backbone(state))
            distribution = torch.distributions.Normal(mean, torch.full_like(mean, self.fixed_std))
            raw = mean if deterministic else distribution.rsample()
            shaped = raw.reshape(-1, self.chunk_len, self.step_action_dim)
            action = torch.cat((shaped[..., :3], torch.tanh(shaped[..., 3:])), dim=-1)
            return action.flatten(1), distribution.log_prob(raw), None

    model = F1RLTPolicy(z_dim=config["z_dim"], proprio_dim=config["proprio_dim"],
                        action_dim=config["action_dim"], num_action_chunks=config["chunk_len"],
                        ref_num_action_chunks=config["ref_chunk_len"], fixed_std=config["fixed_std"])
    # Inherited SAC learned-logstd is unused by the RLT fixed-noise policy.
    model.actor_logstd.requires_grad_(False)
    return model


def actor_parameters(model):
    return list(model.backbone.parameters()) + list(model.actor_mean.parameters())


def parameter_counts(model):
    actor = sum(p.numel() for p in actor_parameters(model))
    critics = [sum(p.numel() for p in q.parameters()) for q in model.q_head.qs]
    unused = sum(p.numel() for p in model.actor_logstd.parameters())
    return {"actor": actor, "critics": critics, "trainable_total": actor + sum(critics),
            "unused_frozen_logstd": unused, "target_critic_copy": sum(critics)}


def td_target(reward, terminated, discount, next_q):
    """External truncations bootstrap; only true task termination zeroes it."""
    return reward + (~terminated.bool()).to(reward.dtype) * discount * next_q


def bc_targets(model, obs, human_action=None, human_mask=None):
    """Use verified human actions only at the supplied intervention steps."""
    reference = model._get_ref_chunk(obs)
    if (human_action is None) != (human_mask is None):
        raise ValueError("Human actions and intervention mask must be supplied together")
    shape = (reference.shape[0], model.chunk_len, model.step_action_dim)
    reference = reference.reshape(shape)
    if human_action is None:
        return reference, torch.zeros(shape[:2], dtype=torch.bool, device=reference.device)
    if human_action.numel() != reference.numel():
        raise ValueError("Human action shape does not match the actor chunk")
    human_action = human_action.reshape(shape)
    if human_mask.shape == reference.shape:
        human_mask = human_mask.bool().any(-1)
    elif human_mask.shape != reference.shape[:2]:
        raise ValueError("Intervention mask must have shape [batch, chunk] or [batch, chunk, action]")
    human_mask = human_mask.bool()
    if not torch.isfinite(human_action).all():
        raise ValueError("Nonfinite human actions")
    return torch.where(human_mask[..., None], human_action, reference), human_mask


def actor_loss(model, obs, *, q_weight, bc_weight, reference_dropout_prob,
               human_action=None, human_mask=None):
    action, _, _ = model.sac_forward(obs, deterministic=True,
        apply_reference_dropout=True, reference_dropout_prob=reference_dropout_prob)
    target, intervention = bc_targets(model, obs, human_action, human_mask)
    error = (action.reshape_as(target) - target).square().mean(-1)
    bc = error.mean()
    # Q gradients flow through candidate actions, never update critic weights here.
    flags = [p.requires_grad for p in model.q_head.parameters()]
    model.q_head.requires_grad_(False)
    try:
        q = model.sac_q_forward(obs, action)[:, 0].mean()
        loss = bc_weight * bc - q_weight * q
    finally:
        for p, flag in zip(model.q_head.parameters(), flags):
            p.requires_grad_(flag)
    metrics = {"actor/bc": bc.detach(), "actor/q": q.detach()}
    if human_action is not None:
        metrics.update({
            "actor/human_fraction": intervention.float().mean().detach(),
            "actor/bc_human": (error * intervention).sum().detach() / intervention.sum().clamp(min=1),
            "actor/bc_reference": (error * ~intervention).sum().detach() / (~intervention).sum().clamp(min=1),
        })
    return loss, metrics


@torch.no_grad()
def update_target(target, source, tau):
    for dst, src in zip(target.parameters(), source.parameters(), strict=True):
        dst.lerp_(src, tau)


def check_source_split(records):
    groups = {}
    for record in records:
        source = record["source_run"]
        if source in groups and groups[source] != record["split"]:
            raise ValueError("Same raw recording appears in train and validation")
        groups[source] = record["split"]
    if set(groups.values()) != {"train", "validation"}:
        raise ValueError("Independent raw-recording validation is required")
    return groups
