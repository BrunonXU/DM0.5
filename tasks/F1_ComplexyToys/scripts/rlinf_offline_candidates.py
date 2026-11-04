# Upstream-derived guidance/ODE routines: Copyright 2026 The RLinf Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Isolated RTC algorithm candidates; never installs a service or moves hardware.

``jacobian_identity`` changes only d(clean)/dx to I in the current F1 sampler.
``upstream_approx`` adapts the complete approximate sampler from RLinf commit
44418454146c1d89e428d3d1b2c1ac5928258238 (Apache-2.0). Its post-Euler guidance,
gain, timestep arithmetic and RNG consumption intentionally differ from F1.
Neither candidate is numerically equivalent to full VJP RTC.

The optional installation affects only the registered policy.model. The original
Policy.predict_rtc still owns reanchor, decode_window and the exact joint prefix.
Use one worker per policy: installation is for isolated offline experiments.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import weakref

import torch

MODES = ("baseline", "jacobian_identity", "upstream_approx")
UPSTREAM_COMMIT = "44418454146c1d89e428d3d1b2c1ac5928258238"
_INSTALLATIONS = weakref.WeakKeyDictionary()
_BASELINE_SAMPLER = None


def _baseline_sampler():
    global _BASELINE_SAMPLER
    if _BASELINE_SAMPLER is None:
        from f1_toys import rtc
        _BASELINE_SAMPLER = rtc.sample_actions
    return _BASELINE_SAMPLER


def _check_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Unknown RTC candidate {mode!r}; choose {MODES}")


def _validate_noise(model, observation, noise):
    if noise is None:
        return None
    noise = torch.as_tensor(noise, device=observation.state.device)
    expected = (observation.state.shape[0], model.action_horizon, model.action_dim)
    if tuple(noise.shape) != expected:
        raise ValueError(f"Explicit noise shape {tuple(noise.shape)} != {expected}")
    if noise.dtype != torch.float32:
        raise ValueError("Paired RTC noise must be float32")
    return noise


@torch.no_grad()
def _identity_sample(model, observation, preprocess, *, target=None, delay=0,
                     shift=20, steps=5, beta=5., noise=None, rng=None):
    from f1_toys.rtc import soft_mask
    observation = preprocess(observation, train=False)
    _, prefix_mask, cache = model.build_prefix_cache(observation)
    if noise is None:
        noise = torch.randn(observation.state.shape[0], model.action_horizon,
                            model.action_dim, device=observation.state.device,
                            generator=rng)
    x = model.mask_action_coordinates(noise)
    weights = None if target is None else torch.as_tensor(
        soft_mask(model.action_horizon, delay, shift), device=x.device)[None, :, None]
    if target is not None:
        target = model.mask_action_coordinates(
            torch.as_tensor(target, device=x.device, dtype=x.dtype))
    for index in range(steps):
        t = 1. - index / steps
        times = torch.full((x.shape[0],), t, device=x.device, dtype=torch.float32)
        hidden = model.run_suffix(observation, x, times, cache, prefix_mask)
        v = model.mask_action_coordinates(model.velocity_from_suffix(hidden))
        if target is not None:
            clean = x - t * v
            correction = (target - clean) * weights
            tau = 1. - t
            gain = beta if tau == 0 else min(beta, (tau*tau + t*t) / (tau*t))
            v = v - gain * correction
        x = model.mask_action_coordinates(x - v / steps)
    return x.detach()


def _upstream_target_and_mask(prev_remaining, horizon, action_dim, delay_steps,
                              device, dtype):
    """Fixed upstream build_rtc_target_and_mask, with its original arithmetic."""
    batch_size = 1 if prev_remaining is None else prev_remaining.shape[0]
    target = torch.zeros((batch_size, horizon, action_dim), device=device, dtype=dtype)
    mask = torch.zeros((batch_size, horizon, 1), device=device, dtype=dtype)
    if prev_remaining is None or prev_remaining.numel() == 0:
        return target, mask
    overlap = min(prev_remaining.shape[1], horizon)
    if overlap <= 0:
        return target, mask
    target[:, :overlap] = prev_remaining[:, :overlap].to(device=device, dtype=dtype)
    hard_end = min(max(int(delay_steps), 0), overlap)
    if hard_end > 0:
        mask[:, :hard_end, 0] = 1.0
    if hard_end < overlap:
        i = torch.arange(hard_end, overlap, device=device, dtype=dtype)
        denom = max(float(overlap - hard_end + 1), 1.0)
        c_i = (overlap - i) / denom
        soft = c_i * (torch.expm1(c_i) / (math.e - 1.0))
        mask[:, hard_end:overlap, 0] = soft
    return target, mask


def _upstream_timesteps(num_steps, device):
    timesteps = torch.linspace(1.0, 1.0 / num_steps, num_steps, device=device)
    return torch.cat([timesteps, torch.zeros(1, device=device)])


def _upstream_flow_ode(x_t, v_t, idx, num_steps):
    """Exact flow_ode branch; do not simplify to x - v/steps for parity."""
    timesteps = _upstream_timesteps(num_steps, x_t.device).to(x_t.dtype)
    t_input = timesteps[idx][:, None, None].expand_as(x_t)
    delta = (timesteps[idx] - timesteps[idx + 1])[:, None, None].expand_as(x_t)
    x0_pred = x_t - v_t * t_input
    x1_pred = x_t + v_t * (1 - t_input)
    x0_weight = 1 - (t_input - delta)
    x1_weight = t_input - delta
    return x0_pred * x0_weight + x1_pred * x1_weight, torch.zeros_like(t_input)


@torch.no_grad()
def _upstream_sample(model, observation, preprocess, *, target=None, delay=0,
                     shift=20, steps=5, beta=5., noise=None, rng=None):
    observation = preprocess(observation, train=False)
    batch = observation.state.shape[0]
    device = observation.state.device
    if noise is None:
        noise = torch.randn(batch, model.action_horizon, model.action_dim,
                            device=device, dtype=torch.float32, generator=rng)
    else:
        noise = noise.to(device=device, dtype=torch.float32)
    # F1 adaptation: preserve the trained inactive-action input contract.
    noise = model.mask_action_coordinates(noise)
    _, prefix_mask, cache = model.build_prefix_cache(observation)
    # F1 reanchor already produces the remaining EEF tail in the new anchor,
    # then pads to H. Upstream receives that tail without the padding.
    prev_remaining = None if target is None else model.mask_action_coordinates(
        torch.as_tensor(target, device=device, dtype=noise.dtype))[:, :model.action_horizon-shift]
    target, mask = _upstream_target_and_mask(
        prev_remaining, model.action_horizon, model.action_dim, delay,
        device, noise.dtype)
    x_t = noise
    paper_tau = torch.tensor(0.0, device=device, dtype=noise.dtype)
    dt = torch.tensor(1.0 / steps, device=device, dtype=noise.dtype)
    idx_step = torch.empty(batch, device=device, dtype=torch.long)
    for idx in range(steps):
        # The pinned upstream calls .item() here; retain it so benchmark costs
        # include the actual implementation, including its host synchronization.
        t_val = float(_upstream_timesteps(steps, device)[idx].item())
        t_tensor = torch.full((batch,), t_val, device=device, dtype=torch.float32)
        hidden = model.run_suffix(observation, x_t, t_tensor, cache, prefix_mask)
        v_t = model.mask_action_coordinates(model.velocity_from_suffix(hidden)).to(torch.float32)
        idx_step.fill_(idx)
        x_t_mean, x_t_std = _upstream_flow_ode(x_t.to(torch.float32), v_t, idx_step, steps)
        if prev_remaining is not None and prev_remaining.numel() > 0:
            guidance_term = (target - x_t_mean) * mask
            guidance_scale = torch.clamp(
                (1.0 - paper_tau) / torch.clamp(paper_tau + 1e-4, min=1e-4),
                max=float(beta))
            x_t_mean = x_t_mean + guidance_scale * guidance_term
        # Upstream consumes noise even in deterministic ODE mode (std=0).
        step_noise = torch.randn(x_t.shape, device=device, dtype=torch.float32, generator=rng)
        x_t = model.mask_action_coordinates(x_t_mean + step_noise * x_t_std)
        paper_tau = paper_tau + dt
    return x_t


def sample_actions_candidate(model, observation, preprocess, *, mode="baseline",
                             target=None, delay=0, shift=20, steps=5, beta=5.,
                             noise=None, rng=None):
    """Baseline signature plus mode; an explicit noise tensor wins over RNG."""
    _check_mode(mode)
    if steps <= 0:
        raise ValueError("steps must be positive")
    if target is not None and not 0 <= delay <= shift <= model.action_horizon-delay:
        raise ValueError("RTC requires d <= s <= H-d")
    noise = _validate_noise(model, observation, noise)
    sampler = (_baseline_sampler() if mode == "baseline" else
               _identity_sample if mode == "jacobian_identity" else _upstream_sample)
    return sampler(model, observation, preprocess, target=target, delay=delay,
                   shift=shift, steps=steps, beta=beta, noise=noise, rng=rng)


@dataclass
class RTCCandidateInstallation:
    """Restore with close() or use as a context manager; fixed noise is reusable."""
    model_ref: object
    mode: str
    noise: object = None
    closed: bool = False

    def set_noise(self, noise):
        if self.closed:
            raise RuntimeError("RTC candidate installation is closed")
        self.noise = noise

    def close(self):
        if self.closed:
            return
        model = self.model_ref()
        if model is not None and _INSTALLATIONS.get(model) is self:
            del _INSTALLATIONS[model]
        self.closed = True
        if not _INSTALLATIONS:
            from f1_toys import rtc
            if rtc.sample_actions is _dispatch_sample_actions:
                rtc.sample_actions = _baseline_sampler()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _dispatch_sample_actions(model, observation, preprocess, **kwargs):
    installation = _INSTALLATIONS.get(model)
    if installation is None:
        return _baseline_sampler()(model, observation, preprocess, **kwargs)
    if installation.noise is not None:
        if kwargs.get("noise") is not None:
            raise ValueError("Explicit noise supplied both by installation and caller")
        kwargs["noise"] = installation.noise
    return sample_actions_candidate(model, observation, preprocess,
                                    mode=installation.mode, **kwargs)


def install_rtc_candidate(policy, mode, *, noise=None):
    """Temporarily route this policy's existing predict_rtc to one candidate.

    Example::

        with install_rtc_candidate(policy, 'jacobian_identity', noise=noise) as h:
            decoded, raw, data = policy.predict_rtc(...)
            h.set_noise(next_explicit_noise)

    Close before changing mode for a model. Other policy instances remain on
    their original sampler. No checkpoint, core or frozen source is modified.
    """
    _check_mode(mode)
    from f1_toys import rtc
    _baseline_sampler()
    if policy.model in _INSTALLATIONS:
        raise ValueError("Close this model's existing RTC installation first")
    if rtc.sample_actions not in (_BASELINE_SAMPLER, _dispatch_sample_actions):
        raise RuntimeError("RTC sampler was independently replaced; install candidates before wrappers")
    installation = RTCCandidateInstallation(weakref.ref(policy.model), mode, noise)
    _INSTALLATIONS[policy.model] = installation
    rtc.sample_actions = _dispatch_sample_actions
    return installation
