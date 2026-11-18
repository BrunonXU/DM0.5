"""Preview scope, bounded work and no leakage of future measured outputs."""
import importlib.util
from pathlib import Path
from copy import deepcopy
import numpy as np
import pytest

spec=importlib.util.spec_from_file_location('output_preview',Path(__file__).parents[1]/'scripts/output_preview.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def example():
    return dict(schema='f1-reference-preview-input-v1',initial_state_kind='reset_at_rest',
        initialized_at_ns=1_000_000_000,full22=[0.]*22,initial_state16=[0.]*16,state_valid_mask=[True]*16,
        references=[dict(virtual_ns=1_020_000_000,reference16=[1.]*16,new_motion_reference=True,request_id='r'),
                    dict(virtual_ns=1_040_000_000,reference16=[99.]*16,new_motion_reference=False,request_id=None)])


@pytest.mark.parametrize('mutation',['moving','duplicate','backwards','gap','nan','state','mask','flag','count'])
def test_invalid_inputs_fail_before_owner_construction(mutation,monkeypatch):
    value=example()
    if mutation=='moving':value['initial_state_kind']='moving'
    elif mutation=='duplicate':value['references'][1]['virtual_ns']=value['references'][0]['virtual_ns']
    elif mutation=='backwards':value['references'][1]['virtual_ns']=0
    elif mutation=='gap':value['references'][1]['virtual_ns']+=1_000_000_000
    elif mutation=='nan':value['references'][0]['reference16'][2]=float('nan')
    elif mutation=='state':value['initial_state16']=[0.]*15
    elif mutation=='mask':value['state_valid_mask'][0]=1
    elif mutation=='flag':value['references'][0]['new_motion_reference']=1
    else:value['references']=[]
    monkeypatch.setattr(module,'_make_owner',lambda *a:pytest.fail('invalid input reached owner'))
    with pytest.raises(ValueError):module.preview_references(value,config={},mapping=None,gate=None)


class FakeOwner:
    def __init__(self):self.closed=False;self.selected=np.zeros(16);self.last_submitted=np.zeros(16);self.calls=[]
    def initialize(self,now):pass
    def prepare(self):pass
    def approve(self,q,now,enabled):self.selected=np.array(q);self.calls.append((now,enabled))
    def publish(self,now):
        self.last_submitted=self.selected.copy()
        return self.last_submitted,None,dict(publication_complete=True)
    def close(self):self.closed=True


def test_disabled_input_cannot_copy_future_recorded_hold_output(monkeypatch):
    owner=FakeOwner();monkeypatch.setattr(module,'_make_owner',lambda *a:owner)
    value=example();before=deepcopy(value)
    result=module.preview_references(value,config={},mapping=None,gate=None)
    assert result['rows'][1]['submitted16']==[1.]*16  # not the supplied 99s
    assert result['rows'][1]['measured16']==[1.]*16
    assert owner.closed and value==before and not result['execution_authorized']


def test_deadline_closes_owner_and_does_not_return_partial_prediction(monkeypatch):
    owner=FakeOwner();monkeypatch.setattr(module,'_make_owner',lambda *a:owner)
    times=iter([0.,2.])
    with pytest.raises(module.PreviewBudgetExceeded):
        module.preview_references(example(),config={},mapping=None,gate=None,max_wall_s=1.,clock=lambda:next(times))
    assert owner.closed and not owner.calls


def test_partial_publication_is_not_a_prediction(monkeypatch):
    owner=FakeOwner();owner.publish=lambda now:(np.zeros(16),None,dict(publication_complete=False))
    monkeypatch.setattr(module,'_make_owner',lambda *a:owner)
    with pytest.raises(RuntimeError,match='publication_incomplete'):
        module.preview_references(example(),config={},mapping=None,gate=None)
    assert owner.closed
