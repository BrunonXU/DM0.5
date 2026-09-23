"""Pure PyTorch Real-Time Chunking guidance for DM0.5.

The module deliberately has no HTTP or robot dependency.  RTC operates in the
same normalized mixed-action space as the flow model.  Callers are responsible
for re-anchoring absolute committed robot targets before invoking it.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import torch
from torch import Tensor


def prefix_weights(
    inference_delay: int,
    overlap_steps: int,
    horizon: int,
    *,
    device: torch.device | None = None,
    dtype: torch.dtype = torch.float32,
) -> Tensor:
    """Return RTC hard-prefix plus linear soft-overlap weights.

    Indices before ``inference_delay`` are hard constrained, indices from the
    delay to ``overlap_steps`` linearly decay, and the remaining suffix is free.
    """

    if horizon <= 0:
        raise ValueError("RTC horizon must be positive")
    if not 0 <= inference_delay <= overlap_steps <= horizon:
        raise ValueError(
            "RTC requires 0 <= inference_delay <= overlap_steps <= horizon; "
            f"got {inference_delay}, {overlap_steps}, {horizon}"
        )
    weights = torch.zeros(horizon, device=device, dtype=dtype)
    weights[:inference_delay] = 1.0
    soft_steps = overlap_steps - inference_delay
    if soft_steps:
        # Exclude endpoints: the preceding hard sample is one and the first
        # unconstrained sample after the overlap is zero.
        weights[inference_delay:overlap_steps] = torch.linspace(
            1.0,
            0.0,
            soft_steps + 2,
            device=device,
            dtype=dtype,
        )[1:-1]
    return weights


def _padded_prefix(prefix: Tensor, reference: Tensor) -> Tensor:
    if prefix.ndim == 2:
        prefix = prefix.unsqueeze(0)
    if prefix.ndim != 3:
        raise ValueError(
            "RTC prefix must have shape [T, D] or [B, T, D], "
            f"got {tuple(prefix.shape)}"
        )
    if prefix.shape[0] not in (1, reference.shape[0]):
        raise ValueError("RTC prefix batch must be one or match the model batch")
    if prefix.shape[2] != reference.shape[2]:
        raise ValueError("RTC prefix action dimension must match the model")
    if prefix.shape[1] > reference.shape[1]:
        raise ValueError("RTC prefix cannot exceed the model horizon")
    prefix = prefix.to(device=reference.device, dtype=reference.dtype)
    if prefix.shape[0] == 1 and reference.shape[0] != 1:
        prefix = prefix.expand(reference.shape[0], -1, -1)
    padded = torch.zeros_like(reference)
    padded[:, : prefix.shape[1]] = prefix
    return padded


def project_rtc_prefix(
    output: Tensor,
    prefix: Tensor,
    *,
    inference_delay: int,
    overlap_steps: int,
    release_steps: int = 10,
) -> Tensor:
    """Make the physical switch sample exact and smoothly remove its offset."""

    target = _padded_prefix(prefix, output)
    if not 0 <= inference_delay < overlap_steps <= output.shape[1]:
        raise ValueError(
            "RTC projection requires 0 <= delay < overlap <= horizon; "
            f"got {inference_delay}, {overlap_steps}, {output.shape[1]}"
        )
    if release_steps <= 0:
        raise ValueError("RTC projection release_steps must be positive")
    projected = output.clone()
    stop = min(output.shape[1], inference_delay + release_steps + 1)
    phase = torch.linspace(
        0.0,
        1.0,
        stop - inference_delay,
        device=output.device,
        dtype=output.dtype,
    )
    smootherstep = phase**3 * (phase * (phase * 6.0 - 15.0) + 10.0)
    weights = 1.0 - smootherstep
    residual = target[:, inference_delay] - output[:, inference_delay]
    projected[:, inference_delay:stop] += weights.view(1, -1, 1) * residual[:, None]
    return projected


def guided_velocity(
    *,
    x_t: Tensor,
    denoise_time: float,
    denoise_fn: Callable[[Tensor], Tensor],
    prefix: Tensor | None,
    inference_delay: int,
    overlap_steps: int,
    max_guidance_weight: float,
    guidance_mode: str = "exact_vjp",
) -> Tensor:
    """Evaluate the DM0.5 velocity field with exact RTC VJP guidance.

    ``denoise_time`` follows DM0.5's convention (one at noise, zero at clean
    action).  ``x0_estimate = x_t - t * v_t`` is constrained toward the old
    committed prefix.  Only a vector-Jacobian product with respect to ``x_t``
    is requested; model parameter gradients are never accumulated.
    """

    if prefix is None:
        return denoise_fn(x_t)
    if not 0.0 < denoise_time <= 1.0 or not math.isfinite(denoise_time):
        raise ValueError(f"invalid RTC denoise time {denoise_time}")
    if max_guidance_weight <= 0 or not math.isfinite(max_guidance_weight):
        raise ValueError("RTC max guidance weight must be finite and positive")

    target = _padded_prefix(prefix, x_t)
    weights = prefix_weights(
        inference_delay,
        overlap_steps,
        x_t.shape[1],
        device=x_t.device,
        dtype=x_t.dtype,
    ).view(1, -1, 1)

    if guidance_mode not in {"exact_vjp", "approx_identity"}:
        raise ValueError(f"unsupported RTC guidance mode {guidance_mode!r}")

    # inference_action is normally under no_grad.  Re-enable autograd only for
    # the action latent so the VJP is exact while checkpoint parameters remain
    # gradient-free from the optimizer's perspective.
    if guidance_mode == "exact_vjp":
        with torch.enable_grad():
            differentiable_x = x_t.detach().clone().requires_grad_(True)
            base_velocity = denoise_fn(differentiable_x)
            x0_estimate = differentiable_x - denoise_time * base_velocity
            error = (target - x0_estimate) * weights
            correction = torch.autograd.grad(
                outputs=x0_estimate,
                inputs=differentiable_x,
                grad_outputs=error.detach(),
                retain_graph=False,
                create_graph=False,
                only_inputs=True,
            )[0]
    else:
        # RLinf-style performance fallback: approximate the x0-estimate
        # Jacobian by identity.  This avoids a backward pass while retaining
        # the same hard/soft prefix weights and bounded time schedule.
        base_velocity = denoise_fn(x_t)
        x0_estimate = x_t - denoise_time * base_velocity
        correction = ((target - x0_estimate) * weights).detach()

    tau = 1.0 - denoise_time
    if tau <= 0.0:
        guidance_weight = max_guidance_weight
    else:
        one_minus_tau_sq = (1.0 - tau) ** 2
        inv_r2 = (one_minus_tau_sq + tau**2) / max(one_minus_tau_sq, 1e-12)
        guidance_weight = min(
            max_guidance_weight,
            ((1.0 - tau) / tau) * inv_r2,
        )
    return (base_velocity - guidance_weight * correction).detach()
