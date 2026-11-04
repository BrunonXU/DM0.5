import copy
from pathlib import Path

import numpy as np
import pytest
import torch
from torch import nn

from f1_toys.augmentation import augment
from f1_toys.eef import decode_prefix, encode_actions, normalize_actions, normalize_state, physical_actions, relative_pose, solver
from f1_toys.lora import LoRALinear, merge_state


def test_eef_fixed_anchor_and_ik_roundtrip():
    ik = solver()
    state = np.array([0, -30, 20, -60, 0, 0, 0, -70, -80, 80, -90, 0, 10, 0, 30, 60.])
    targets = np.stack([state.copy() for _ in range(3)])
    targets[:, 7] += np.arange(1, 4) * .02
    targets[:, 15] = [40, 50, 60]
    physical = encode_actions(ik.model, state, targets)
    anchor = ik.model.fk(1, state[7:14])
    for a, target in zip(physical, targets):
        np.testing.assert_allclose(anchor @ relative_pose(a), ik.model.fk(1, target[7:14]), atol=2e-7)
    stats = dict(mean=[.1, .2, .3], std=[.01, .02, .03])
    norm = normalize_actions(physical, stats)
    assert not norm[:, 10:].any()
    restored = physical_actions(norm, stats)
    np.testing.assert_allclose(restored, physical, atol=1e-7)
    result = decode_prefix(ik, restored, state, state, 3)
    assert result["success"]
    np.testing.assert_array_equal(np.asarray(result["actions"])[:, [*range(7), 14]], targets[:, [*range(7), 14]])
    with pytest.raises(ValueError, match="Degenerate"):
        relative_pose(np.zeros(10))


def test_state_encoding_physical_diagnostics():
    stats = dict(low=[-10.] * 16, high=[10.] * 16)
    raw = np.array([20.] + [0.] * 15)
    state, diagnostics = normalize_state(raw, stats)
    assert state[0] == 1 and diagnostics["physical_deviation"][0] == 10
    assert raw[0] == 20


def test_augmentation_repeatable_independent_draws():
    pixels = np.random.default_rng(2).integers(0, 256, (224, 224, 3), dtype=np.uint8)
    for c in ("head", "left_wrist", "right_wrist"):
        np.testing.assert_array_equal(augment(pixels, c, 42), augment(pixels, c, 42))
    assert any(not np.array_equal(augment(pixels, "head", s), pixels) for s in range(10))
    assert not np.array_equal(augment(pixels, "head", 42), augment(pixels, "head", 43))


def test_lora_zero_init_updates_and_merge():
    torch.manual_seed(3)
    base = nn.Linear(11, 7).requires_grad_(False)
    old = copy.deepcopy(base.state_dict())
    adapter = LoRALinear(base, rank=4, alpha=2)
    x = torch.randn(8, 11)
    torch.testing.assert_close(adapter(x), base(x), rtol=0, atol=0)
    opt = torch.optim.AdamW([p for p in adapter.parameters() if p.requires_grad], lr=.01, weight_decay=0)
    for _ in range(2):
        opt.zero_grad(); adapter(x).square().mean().backward(); opt.step()
    assert adapter.lora_A.grad.abs().sum() > 0 and adapter.lora_B.grad.abs().sum() > 0
    for k, v in base.state_dict().items():
        torch.testing.assert_close(v, old[k], rtol=0, atol=0)
    state = {"projection." + k: v for k, v in adapter.state_dict().items()}
    merged = merge_state(state)
    reference = nn.Linear(11, 7)
    reference.load_state_dict({k.removeprefix("projection."): v for k, v in merged.items()})
    torch.testing.assert_close(adapter(x), reference(x), rtol=1e-5, atol=1e-6)
