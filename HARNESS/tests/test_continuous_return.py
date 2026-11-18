"""Explicit empty-return transit, with contact and final barriers preserved."""
from dataclasses import replace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import JointProposal,valid_arrival_contract
from f1_harness.trajectory import schedule,sample,stage_requires_stop
from test_return_services import return_case


def test_schedule_streams_through_only_registered_transit_and_still_stops_for_fingers():
    anchor=np.zeros(16);q=np.zeros((5,16));q[:,7]=[1,2,3,3,4];q[3:,15]=18
    trace=[dict(end_index=1,semantic_stage='leave_hole'),
           dict(end_index=2,semantic_stage='right_of_bucket',continuous_return_transit=True),
           dict(end_index=3,semantic_stage='return_bend',continuous_return_transit=True),
           dict(end_index=5,semantic_stage='ready')]
    plan=schedule(q,anchor,trace,10.)
    assert plan['stops']==[0,2,3,4]  # index 2 is retained before the finger change.
    target,index,stopped,end=sample(plan,q,start_index=1,elapsed_s=.15)
    assert target[7]==pytest.approx(2.5) and not stopped and end==2


@pytest.mark.parametrize('name',['leave_hole','ready','grasp','insert','release',None])
def test_contact_or_final_stage_cannot_be_marked_as_transit(name):
    with pytest.raises(ValueError,match='unregistered_continuous_return_stage'):
        stage_requires_stop(dict(semantic_stage=name,continuous_return_transit=True))


def make_plan(case):
    serv,clock,obs,intent,observe=case
    serv.fixture.update(execution_mode='timed_reference',reference_velocity_deg_s=20.)
    serv.fixture['return_route']['continuous_transit']=True
    serv.fixture['candidate_order']={'withdraw_reset':[1,0,2,3]}
    proposal=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),held16=obs.state16,full22=[0]*22))
    return proposal


def test_full_return_contract_binds_annotations_to_recipe(return_case):
    serv,clock,obs,intent,observe=return_case;p=make_plan(return_case)
    trace=p['geometry_trace'];timing=p['trajectory']
    assert timing['stops']==[trace[0]['end_index']-1,len(p['targets'])-1]
    assert [r.get('continuous_return_transit',False) for r in trace]==[False,True,True,False]
    assert valid_arrival_contract(JointProposal(**p))
    after=clock[0];clock[0]+=100_000_000
    kwargs=dict(intent=intent.model_dump(),proposal=p,original_observation=obs.model_dump(),
        observation=observe(clock[0],'new'),full22=[0]*22,held16=obs.state16,after_ns=after)
    assert serv.dispatch('revalidate_plan',kwargs)
    # A changed recipe cannot reuse the old streaming proposal.
    serv.fixture['return_route']['continuous_transit']=False
    with pytest.raises(HarnessError,match='stage_contract_changed'):serv.dispatch('revalidate_plan',kwargs)


@pytest.mark.parametrize('change',['departure','final','gripper','unknown','false_annotation'])
def test_owner_rejects_missing_mandatory_boundary(return_case,change):
    p=make_plan(return_case);trace=p['geometry_trace'];timing=p['trajectory']
    if change=='departure':timing['stops'].remove(trace[0]['end_index']-1)
    elif change=='final':timing['stops'].pop()
    elif change=='unknown':trace[1]['semantic_stage']='some_motion'
    elif change=='false_annotation':trace[1]['continuous_return_transit']=False
    else:
        q=[list(v) for v in p['targets']];q[trace[1]['end_index']-1][15]+=10;p['targets']=q
    assert not valid_arrival_contract(JointProposal(**p))


@pytest.mark.parametrize('value',[True,'true',1])
def test_continuous_return_requires_explicit_boolean_and_timed_mode(return_case,value):
    serv,clock,obs,intent,observe=return_case
    serv.fixture['return_route']['continuous_transit']=value
    with pytest.raises(HarnessError,match='continuous_return_requires_timed_reference'):
        serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),held16=obs.state16,full22=[0]*22))
