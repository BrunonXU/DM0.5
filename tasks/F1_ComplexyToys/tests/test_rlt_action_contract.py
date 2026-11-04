"""A selected controller input never overwrites actual published evidence."""
from pathlib import Path
import sys
import copy
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_action_contract import action_joints, SUBMITTED, REFERENCE
from f1_toys.rlt_local_replay import build_transitions
from test_rlt_local_replay import fixture


def test_approved_reference_and_actual_publication_remain_distinct():
    streams,result=fixture(assisted=True)
    for r in streams['control']:
        r['selected']=[1.]*16
        r['policy_proposal']=[999.]*16
        r['human_candidate']=[888.]*16
    original=copy.deepcopy(streams)
    rows=build_transitions(streams,result)
    for row in rows:
        assert all(v==[0.]*16 for v in action_joints(row,SUBMITTED))
        assert all(v==[1.]*16 for v in action_joints(row,REFERENCE))
    assert streams==original


@pytest.mark.parametrize('fault',['missing','short','nan','unknown'])
def test_reference_contract_cannot_silently_fall_back_to_submitted(fault):
    streams,result=fixture();row=build_transitions(streams,result)[0]
    if fault!='missing':row['selected16']=copy.deepcopy(row['submitted16'])
    if fault=='short':row['selected16'].pop()
    if fault=='nan':row['selected16'][0][7]=float('nan')
    with pytest.raises(ValueError):action_joints(row,'unknown' if fault=='unknown' else REFERENCE)
