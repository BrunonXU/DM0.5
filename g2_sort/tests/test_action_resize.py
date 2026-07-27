"""防止 DM0.5 官方32D动作头扩展到34D时发生权重或梯度回归。"""

from types import SimpleNamespace

import torch

from opendm.model.dm05.dm05_arch import resize_dm05_action_projections


def test_resize_to_34_preserves_base_and_trains_new_dimensions():
    core = SimpleNamespace(
        action_in_proj=torch.nn.Linear(32, 8),
        action_out_proj=torch.nn.Linear(8, 32),
        config=SimpleNamespace(action_dim=32),
    )
    model = SimpleNamespace(model=core, config=SimpleNamespace(action_dim=32))
    old_input = core.action_in_proj.weight.detach().clone()
    old_output = core.action_out_proj.weight.detach().clone()

    resize_dm05_action_projections(model, 34)

    assert core.action_in_proj.weight.shape == (8, 34)
    assert core.action_out_proj.weight.shape == (34, 8)
    assert torch.equal(core.action_in_proj.weight[:, :32], old_input)
    assert torch.equal(core.action_out_proj.weight[:32], old_output)
    values = torch.randn(2, 50, 34, requires_grad=True)
    core.action_out_proj(core.action_in_proj(values)).sum().backward()
    assert core.action_in_proj.weight.grad[:, 32:].abs().sum() > 0
    assert core.action_out_proj.weight.grad[32:].abs().sum() > 0


def test_resize_rejects_invalid_dimension():
    core = SimpleNamespace(
        action_in_proj=torch.nn.Linear(32, 8),
        action_out_proj=torch.nn.Linear(8, 32),
        config=SimpleNamespace(action_dim=32),
    )
    model = SimpleNamespace(model=core, config=SimpleNamespace(action_dim=32))

    try:
        resize_dm05_action_projections(model, 0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("invalid action dimension was accepted")
