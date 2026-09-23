from __future__ import annotations

import pytest
import torch

from opendm.model.dm05.rtc import (
    guided_velocity,
    prefix_weights,
    project_rtc_prefix,
)


def test_rtc_prefix_weights_are_hard_soft_free() -> None:
    weights = prefix_weights(20, 25, 50)
    assert weights.shape == (50,)
    assert torch.all(weights[:20] == 1)
    assert torch.all((weights[20:25] > 0) & (weights[20:25] < 1))
    assert torch.all(weights[20:25][1:] < weights[20:25][:-1])
    assert torch.all(weights[25:] == 0)


def test_final_projection_is_exact_at_switch_and_releases_overlap() -> None:
    output = torch.zeros(1, 50, 3)
    prefix = torch.ones(1, 25, 3)

    projected = project_rtc_prefix(
        output,
        prefix,
        inference_delay=20,
        overlap_steps=25,
    )

    torch.testing.assert_close(projected[:, :20], torch.zeros(1, 20, 3))
    torch.testing.assert_close(projected[:, 20], torch.ones(1, 3))
    assert 0.0 < projected[0, 25, 0].item() < 1.0
    torch.testing.assert_close(projected[:, 30:], torch.zeros(1, 20, 3))


@pytest.mark.parametrize(
    "delay,overlap,horizon",
    [(-1, 25, 50), (26, 25, 50), (20, 51, 50), (0, 0, 0)],
)
def test_rtc_prefix_weights_reject_invalid_contract(
    delay: int, overlap: int, horizon: int
) -> None:
    with pytest.raises(ValueError):
        prefix_weights(delay, overlap, horizon)


def test_exact_vjp_guidance_reduces_prefix_error() -> None:
    torch.manual_seed(7)
    x_t = torch.randn(1, 50, 3)
    prefix = torch.full((1, 25, 3), 0.25)

    # A differentiable non-linear field proves the implementation includes the
    # denoiser Jacobian instead of merely adding an identity correction.
    def denoise_fn(value: torch.Tensor) -> torch.Tensor:
        return 0.2 * value.square() + 0.1 * value

    base_velocity = denoise_fn(x_t)
    guided = guided_velocity(
        x_t=x_t,
        denoise_time=0.5,
        denoise_fn=denoise_fn,
        prefix=prefix,
        inference_delay=20,
        overlap_steps=25,
        max_guidance_weight=10.0,
    )
    dt = -0.1
    base_next = x_t + dt * base_velocity
    guided_next = x_t + dt * guided
    base_error = (base_next[:, :20] - prefix[:, :20]).square().mean()
    guided_error = (guided_next[:, :20] - prefix[:, :20]).square().mean()
    assert guided_error < base_error
    assert torch.equal(guided[:, 25:], base_velocity[:, 25:])


def test_rtc_none_is_bitwise_baseline() -> None:
    x_t = torch.randn(2, 50, 34)

    def denoise_fn(value: torch.Tensor) -> torch.Tensor:
        return value.sin()

    result = guided_velocity(
        x_t=x_t,
        denoise_time=1.0,
        denoise_fn=denoise_fn,
        prefix=None,
        inference_delay=0,
        overlap_steps=0,
        max_guidance_weight=10.0,
    )
    assert torch.equal(result, denoise_fn(x_t))


def test_approx_identity_guidance_needs_no_backward_and_changes_only_prefix() -> None:
    x_t = torch.randn(1, 50, 2)
    prefix = torch.zeros(1, 25, 2)

    def denoise_fn(value: torch.Tensor) -> torch.Tensor:
        assert not value.requires_grad
        return value * 0.1

    base = denoise_fn(x_t)
    result = guided_velocity(
        x_t=x_t,
        denoise_time=0.5,
        denoise_fn=denoise_fn,
        prefix=prefix,
        inference_delay=20,
        overlap_steps=25,
        max_guidance_weight=1.0,
        guidance_mode="approx_identity",
    )
    assert not torch.equal(result[:, :25], base[:, :25])
    assert torch.equal(result[:, 25:], base[:, 25:])
