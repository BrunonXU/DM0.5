from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import torch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from f1_toys.rlt_reference import RLTReferenceAdapter


class FakeModel:
    def build_prefix_cache(self, observation):
        return torch.ones(1,3,4),torch.ones(1,3,dtype=torch.bool),None


class FakePolicy:
    def __init__(self, fail=False):
        self.model=FakeModel()
        self.device="cpu"
        self.contract={"active_state_indices":[7,8,9,10,11,12,13,15]}
        self.fail=fail

    def predict_rtc(self,*args,**kwargs):
        self.model.build_prefix_cache(SimpleNamespace(state=torch.zeros(1,32)))
        if self.fail:
            raise RuntimeError("rejected")
        return {"success":True,"actions":[[0.]*16]},np.zeros((50,32)),{}


@pytest.mark.parametrize("fail",[False,True])
def test_reference_restores_sampler_on_success_and_error(fail):
    policy=FakePolicy(fail)
    reader=SimpleNamespace(policy=policy,encoder=lambda x,m:x[:,-1:])
    adapter=RLTReferenceAdapter(reader)
    if fail:
        with pytest.raises(RuntimeError,match="rejected"):
            adapter.predict_rtc({},np.zeros(16),np.zeros(16),{"committed_actions16":[]})
    else:
        decoded,raw,_,features=adapter.predict_rtc({},np.zeros(16),np.zeros(16),{"committed_actions16":[]})
        assert features["prefix_forward_calls"]==1
        assert features["ref_chunk"].shape==(1,50,10)
        assert not features["executed_transition"]
        assert not raw.any() and decoded["success"]
    assert "build_prefix_cache" not in policy.model.__dict__
