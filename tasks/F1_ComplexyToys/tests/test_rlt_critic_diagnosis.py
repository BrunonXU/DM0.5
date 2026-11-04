"""Exact training probabilities and fixed-recipient counterfactual checks."""
from pathlib import Path
import sys

import pytest
torch = pytest.importorskip("torch")
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from diagnose_rlt_critic import cosine, sampling_probabilities, swap_inputs


def test_sampler_counts_terminal_mass_in_both_sampling_branches():
    records=[dict(segment_id="a",split="train",terminated=True),
             dict(segment_id="a",split="train",terminated=False),
             dict(segment_id="b",split="train",terminated=True),
             dict(segment_id="b",split="train",terminated=False),
             dict(segment_id="b",split="train",terminated=False),
             dict(segment_id="v",split="validation",terminated=True)]
    p=sampling_probabilities(records,.1)
    assert p.tolist()==pytest.approx([.275,.225,.2,.15,.15,0])
    assert float(p[[0,2]].sum())==pytest.approx(.475)


def test_swap_replaces_only_donor_group_and_preserves_diagonal():
    features={"token":torch.tensor([[10.],[20.]]),
              "proprio":torch.tensor([[100.],[200.]]),
              "action":torch.tensor([[1.],[2.]])}
    values=swap_inputs(features,("token",))
    assert values["token"].flatten().tolist()==[10,20,10,20]
    assert values["proprio"].flatten().tolist()==[100,100,200,200]
    assert values["action"].flatten().tolist()==[1,1,2,2]
    for key in features:
        assert torch.equal(values[key][[0,3]],features[key])


def test_conditional_gradient_mass_reconstructs_full_objective_without_update():
    parameter=torch.tensor([1.,2.],requires_grad=True)
    original=parameter.detach().clone()
    terminal=(parameter-3).square().mean()
    nonterminal=(parameter+2).square().mean()
    gt=torch.autograd.grad(terminal,parameter,retain_graph=True)[0]
    gn=torch.autograd.grad(nonterminal,parameter,retain_graph=True)[0]
    total=torch.autograd.grad(.2*terminal+.8*nonterminal,parameter)[0]
    assert torch.allclose(total,.2*gt+.8*gn)
    assert cosine(gt,gn)<0
    assert parameter.grad is None
    assert torch.equal(parameter,original)


def test_high_dimensional_cosine_accumulates_in_double_precision():
    vector=torch.linspace(-1.,2.,1_400_000,dtype=torch.float32)
    assert cosine(vector,vector)==pytest.approx(1.,abs=1e-12)
    assert cosine(vector,-vector)==pytest.approx(-1.,abs=1e-12)
    assert cosine(vector,torch.zeros_like(vector)) is None
