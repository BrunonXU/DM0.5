"""Inference-time RTC (Black et al., arXiv:2506.07339v2, equations 2–5).

The frozen RLinf core uses noise time t=1-tau, hence v_paper=-v_core.
Only noisy actions require gradients; neither weights nor the KV cache do.
"""
import math

import numpy as np
import torch

from .eef import encode_actions, normalize_actions, physical_actions, relative_pose


def soft_mask(horizon, delay, shift):
    if not 0 <= delay <= shift <= horizon - delay:
        raise ValueError('RTC requires d <= s <= H-d')
    i = np.arange(horizon)
    c = (horizon - shift - i) / (horizon - shift - delay + 1)
    return np.where(i < delay, 1., np.where(i < horizon-shift,
                    c * np.expm1(c) / math.expm1(1), 0.)).astype(np.float32)


def guided_velocity(x, core_velocity, t, target, weights, beta=5.):
    """Return core-time velocity; detached cotangent avoids an extra derivative."""
    v = core_velocity(x)
    clean = x - t * v
    error = ((target - clean) * weights).detach()
    correction, = torch.autograd.grad(clean, x, grad_outputs=error)
    tau = 1. - t
    gain = beta if tau == 0 else min(beta, (tau*tau + t*t) / (tau*t))
    return v.detach() - gain * correction.detach()


def sample_actions(model, observation, preprocess, *, target=None, delay=0, shift=20,
                   steps=5, beta=5., noise=None, rng=None):
    # Do not enter inference_mode: backward through the action expert needs
    # ordinary detached cache tensors. No parameter .grad is ever populated.
    with torch.no_grad():
        observation = preprocess(observation, train=False)
        _, prefix_mask, cache = model.build_prefix_cache(observation)
        if noise is None:
            noise = torch.randn(observation.state.shape[0], model.action_horizon,
                                model.action_dim, device=observation.state.device, generator=rng)
        x = model.mask_action_coordinates(noise)
        weights = None if target is None else torch.as_tensor(
            soft_mask(model.action_horizon, delay, shift), device=x.device)[None, :, None]
        if target is not None:
            target = model.mask_action_coordinates(torch.as_tensor(target, device=x.device, dtype=x.dtype))
    for index in range(steps):
        t = 1. - index / steps
        times = torch.full((x.shape[0],), t, device=x.device, dtype=torch.float32)
        def velocity(value):
            hidden = model.run_suffix(observation, value, times, cache, prefix_mask)
            return model.mask_action_coordinates(model.velocity_from_suffix(hidden))
        if target is None:
            with torch.no_grad():
                v = velocity(x)
        else:
            with torch.enable_grad():
                x = x.detach().requires_grad_(True)
                v = guided_velocity(x, velocity, t, target, weights, beta)
        with torch.no_grad():
            x = model.mask_action_coordinates(x - v / steps)
    return x.detach()


def reanchor(ik, state, previous, committed, stats):
    """Absolute EEF tail + actual committed joints, expressed in the NEW anchor."""
    previous, committed = np.asarray(previous, float), np.asarray(committed, float)
    target = np.zeros((50, 10), np.float32)
    inverse = np.linalg.inv(ik.model.fk(1, np.asarray(state)[7:14]))
    for index, row in enumerate(previous):
        pose = inverse @ relative_pose(row)
        target[index, :3] = pose[:3, 3]
        target[index, 3:9] = pose[:2, :3].reshape(6)
        target[index, 9] = row[9]
    if len(committed):
        target[:len(committed)] = encode_actions(ik.model, state, committed)
    return normalize_actions(target, stats)[None]


def absolute_eef(ik, state, raw, stats):
    physical = physical_actions(raw, stats)
    anchor = ik.model.fk(1, np.asarray(state)[7:14])
    result = physical.copy()
    for index, row in enumerate(physical):
        pose = anchor @ relative_pose(row)
        result[index, :3] = pose[:3, 3]
        result[index, 3:9] = pose[:2, :3].reshape(6)
    return result


def decode_window(ik, physical, state, held, committed, count):
    """Keep committed joints EXACT; solve only the future suffix continuously."""
    state, held = np.asarray(state, float), np.asarray(held, float)
    committed = np.asarray(committed, float).reshape(-1, 16)
    anchor = ik.model.fk(1, state[7:14])
    reference = state[7:14] if not len(committed) else committed[-1, 7:14]
    output, diagnostics = committed.tolist(), []
    for row in physical[len(committed):count]:
        result = ik.solve(1, anchor @ relative_pose(row), reference)
        diagnostics.append(result)
        if not result['success']:
            return dict(success=False, actions=None, diagnostics=diagnostics, reason='ik_rejected')
        reference = np.asarray(result['joints'])
        action = held.copy()
        action[7:14], action[15] = reference, np.clip(row[9]*50+50, 0, 100)
        output.append(action.tolist())
    return dict(success=True, actions=output, diagnostics=diagnostics,
                gripper_clipped_count=int(np.count_nonzero(abs(physical[len(committed):count, 9]) > 1)))
