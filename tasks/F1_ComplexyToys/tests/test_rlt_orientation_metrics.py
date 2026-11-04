from pathlib import Path
import sys
import math
import pytest
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_evaluation import orientation_metrics


def actions(angle=0.):
    c,s=math.cos(math.radians(angle)),math.sin(math.radians(angle))
    # Roll changes the full attitude while a chosen axis elevation may not.
    return torch.tensor([[[0.,0.,0.,1.,0.,0.,0.,c,-s,1.]]],dtype=torch.float64)


def test_full_rotation_reports_twenty_degrees_and_ignores_padding():
    predicted=torch.cat([actions(20),actions(120)],dim=1)
    target=torch.cat([actions(),actions()],dim=1)
    m=orientation_metrics(predicted,target,torch.tensor([[True,False]]))
    assert m['rotation_mae_deg']==pytest.approx(20)
    assert m['rotation_valid_steps']==1 and m['invalid_rotation_steps']==0


def test_decoder_normalization_and_invalid_rotation_coverage():
    a=actions(20);a[...,3:6]*=2;a[...,6:9]*=3
    assert orientation_metrics(a,actions(20),torch.ones(1,1,dtype=torch.bool))['rotation_max_deg']<1e-5
    a[...,3:9]=0
    m=orientation_metrics(a,actions(),torch.ones(1,1,dtype=torch.bool))
    assert m['rotation_valid_steps']==0 and m['invalid_rotation_steps']==1
