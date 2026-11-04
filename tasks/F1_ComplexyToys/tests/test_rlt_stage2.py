"""F1 head output semantics, gradient isolation and replay split tests."""
import os
from pathlib import Path
import sys

import pytest
torch=pytest.importorskip("torch")
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/"src"))
from f1_toys.rlt_stage2 import (actor_loss,check_source_split,make_policy,
    parameter_counts,td_target,update_target)


@pytest.fixture
def model():
    core=Path(os.environ.get("F1_RLT_CORE",str(TASK.parents[1]/"RLinf")))
    return make_policy(core,dict(z_dim=2048,proprio_dim=8,action_dim=10,
        chunk_len=16,ref_chunk_len=50,fixed_std=0.002))


def obs():
    return {"z_rl":torch.randn(2,2048),"proprio":torch.randn(2,8),"ref_chunk":torch.randn(2,50,10)}


def test_counts_match_actual_trainable_parameters(model):
    counts=parameter_counts(model)
    assert counts["actor"]==740256
    assert counts["critics"]==[700929,700929]
    assert counts["trainable_total"]==sum(p.numel() for p in model.parameters() if p.requires_grad)


def test_f1_translation_not_clipped_to_stock_tanh(model):
    with torch.no_grad():
        model.actor_mean.weight.zero_()
        model.actor_mean.bias.fill_(3.0)
    action=model.sac_forward(obs(),deterministic=True)[0].reshape(2,16,10)
    assert torch.equal(action[...,:3],torch.full_like(action[...,:3],3))
    assert torch.all(action[...,3:]<1)


def test_critic_not_updated_by_actor_loss(model):
    loss,_=actor_loss(model,obs(),q_weight=.1,bc_weight=5,reference_dropout_prob=.5)
    loss.backward()
    assert model.actor_mean.weight.grad is not None
    assert all(p.grad is None for p in model.q_head.parameters())
    assert all(p.requires_grad for p in model.q_head.parameters())


def test_true_terminal_vs_external_truncation_target():
    reward=torch.tensor([[1.],[0.]])
    done=torch.tensor([[True],[False]])
    target=td_target(reward,done,torch.full((2,1),.9),torch.full((2,1),2.))
    torch.testing.assert_close(target,torch.tensor([[1.],[1.8]]))


def test_raw_group_cannot_leak_across_attempts():
    with pytest.raises(ValueError,match="Same raw recording"):
        check_source_split([dict(source_run="x",split="train"),dict(source_run="x",split="validation")])


def test_target_polyak_copy(model):
    import copy
    target=copy.deepcopy(model.q_head).requires_grad_(False)
    old=[p.clone() for p in target.parameters()]
    with torch.no_grad():
        for p in model.q_head.parameters():p.add_(1)
    update_target(target,model.q_head,.25)
    for p,before in zip(target.parameters(),old):
        torch.testing.assert_close(p,before+.25)
        assert not p.requires_grad


def test_state_dictionary_roundtrip(model):
    probe=obs()
    first=model.sac_forward(probe,deterministic=True)[0]
    model.load_state_dict(model.state_dict(),strict=True)
    assert torch.equal(first,model.sac_forward(probe,deterministic=True)[0])
