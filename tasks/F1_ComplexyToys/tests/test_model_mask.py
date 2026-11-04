"""Exercise actual RLinf core methods without allocating the 3B model."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.inference import load_core

config_module, _ = load_core(TASK.parents[1] / "RLinf")
import f1_toys_rlinf_core.pi0 as core

MASK = tuple(i in [7,8,9,10,11,12,13,15] for i in range(32))


def model_stub():
    obj = core.Pi0.__new__(core.Pi0)
    torch.nn.Module.__init__(obj)
    obj._config = config_module.Pi0Config(pi05=True, action_loss_mask=MASK, mask_inactive_action_inputs=True, train_image_augmentation=False)
    return obj


def test_real_loss_masks_gradient_and_uses_eight_dim_denominator():
    model = model_stub()
    predicted = torch.ones((2, 50, 32), requires_grad=True)
    target = torch.zeros_like(predicted)
    target[..., torch.tensor(MASK).logical_not()] = float("nan")
    loss = model.action_loss(predicted, target)
    assert loss.shape == (2, 50) and torch.all(loss == 1)
    loss.sum().backward()
    assert torch.all(predicted.grad[..., list(MASK)] == 0.25)
    assert torch.all(predicted.grad[..., torch.tensor(MASK).logical_not()] == 0)


def test_inactive_latent_never_enters_action_projection():
    model = model_stub()
    first = torch.randn(1, 50, 32)
    other = first.clone()
    other[..., torch.tensor(MASK).logical_not()] = float("nan")
    projection = torch.nn.Linear(32, 16)
    assert torch.equal(projection(model.mask_action_coordinates(first)), projection(model.mask_action_coordinates(other)))


def test_default_loss_unchanged_and_invalid_config_rejected():
    model = model_stub()
    model._config = config_module.Pi0Config()
    x, y = torch.randn(3, 4, 32), torch.randn(3, 4, 32)
    assert torch.equal(model.action_loss(x, y), (x-y).square().mean(-1))
    with pytest.raises(ValueError):
        config_module.Pi0Config(action_loss_mask=(False,)*32)
    with pytest.raises(ValueError):
        config_module.Pi0Config(mask_inactive_action_inputs=True)


def test_time_mask_reduction_and_validation():
    loss = torch.arange(12, dtype=torch.float32).reshape(3, 4).requires_grad_()
    mask = torch.tensor([[1, 0, 0, 0], [1, 1, 0, 0], [1, 1, 1, 1]], dtype=torch.bool)
    actual = core.Pi0.reduce_action_loss(loss, mask)
    expected = (loss[0, 0] + loss[1, :2].mean() + loss[2].mean()) / 3
    torch.testing.assert_close(actual, expected)
    split = sum(core.Pi0.reduce_action_loss(loss[i:i+1], mask[i:i+1]) for i in range(3)) / 3
    torch.testing.assert_close(actual, split)
    actual.backward()
    assert not loss.grad[~mask].any()
    for invalid in (mask.float(), torch.zeros_like(mask),
                    torch.tensor([[1, 0, 1, 0]]*3, dtype=torch.bool)):
        with pytest.raises(ValueError):
            core.Pi0.validate_action_time_mask(invalid, torch.zeros(3, 4, 32))


def test_actual_compute_loss_padding_invariance_gradient_and_full_parity(monkeypatch):
    """Run the real loss pipeline with a small attention module, no 3B allocation."""
    model = model_stub()
    model._config = config_module.Pi0Config(
        pi05=True, action_loss_mask=tuple(i < 10 for i in range(32)),
        mask_inactive_action_inputs=True, train_image_augmentation=False)
    model.action_horizon = 4
    model.embed_dtype = torch.float32
    model.action_in_proj = torch.nn.Linear(32, 8)
    model.action_out_proj = torch.nn.Linear(8, 32)
    model.embed_prefix = lambda obs: (torch.zeros(2, 1, 8), torch.ones(2, 1, dtype=torch.bool),
                                     torch.zeros(1, dtype=torch.bool))
    def embed_suffix(obs, actions, time):
        return (model.action_in_proj(model.mask_action_coordinates(actions)),
                torch.ones(2, 4, dtype=torch.bool), torch.tensor([True, False, False, False]), None)
    model.embed_suffix = embed_suffix
    class Attention(torch.nn.Module):
        def forward(self, tokens, *, positions, mask, adarms_cond):
            self.mask = mask
            values = torch.cat(tokens, dim=1)
            weights = mask.float()
            weights = weights / weights.sum(-1, keepdim=True).clamp_min(1)
            output = weights @ values
            return ([output[:, :1], output[:, 1:]], None)
    model.llm = Attention()
    monkeypatch.setattr(core.model, 'preprocess_observation', lambda obs, **kw: obs)
    monkeypatch.setattr(core.model, '_observation_to_dtype', lambda obs, dtype: obs)
    actions = torch.randn(2, 4, 32, requires_grad=True)
    noise = torch.randn_like(actions)
    time = torch.tensor([.3, .8])
    mask = torch.tensor([[1, 1, 0, 0], [1, 1, 1, 1]], dtype=torch.bool)
    base = model.compute_loss(None, actions, noise=noise, time=time, action_time_mask=mask)
    changed = actions.detach().clone()
    changed[~mask] = float('nan')
    changed_noise = noise.clone()
    changed_noise[~mask] = float('nan')
    same = model.compute_loss(None, changed, noise=changed_noise, time=time, action_time_mask=mask)
    torch.testing.assert_close(base, same, rtol=0, atol=0)
    assert not model.llm.mask[0, :, -2:].any()
    assert not base[~mask].any()
    model.reduce_action_loss(base, mask).backward()
    assert not actions.grad[~mask].any() and actions.grad[mask].abs().sum() > 0
    plain = model.compute_loss(None, actions, noise=noise, time=time)
    full = model.compute_loss(None, actions, noise=noise, time=time,
                              action_time_mask=torch.ones_like(mask))
    torch.testing.assert_close(plain, full, rtol=0, atol=0)
