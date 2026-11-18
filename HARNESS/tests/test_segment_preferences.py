"""Offline ordering hints cannot remove candidates, authorize or skip fresh checks."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from segment_preferences import propose_preference,preferred_order,render_preference,preference_scope
from f1_harness.contracts import digest


def fixture():
    scope=dict(skill='transfer_above_hole',start_index=0,registered_speeds=[40.,50.,60.],recipe='fixed')
    r=dict(schema='f1-offline-segment-recommendation-v1',binding='b'*64,preference_scope=scope,
        recommended_reference_speed=40,execution_authorized=False,whole_skill_verified=False,
        attempts=[dict(reference_speed=40,outcome='conditional_segment_clear',audit=[dict(
            complete=True,passed=True,failures=[],samples=100)])])
    return scope,r,propose_preference(r,'discovery-run')


def test_roundtrip_preserves_all_candidates_and_machine_human_roles():
    scope,r,hint=fixture();order,usage=preferred_order([hint],scope,[60,50,40])
    assert order==[40,60,50] and sorted(order)==[40,50,60]
    assert usage['applied'] and usage['fresh_audit_required'] and not usage['removes_candidates']
    assert hint['recommendation_sha256']==digest(r)
    assert not hint['execution_enabled'] and not hint['promoted']
    assert '不进入编译器' in render_preference(hint)


@pytest.mark.parametrize('change', ['scope','tamper','enable','remove','missing_source'])
def test_unusable_optional_hint_falls_back_without_failing_task(change):
    scope,r,hint=fixture();value=deepcopy(hint)
    if change=='scope':value['scope']['recipe']='other'
    elif change=='tamper':value['preferred_reference_speed']=60
    elif change=='enable':value['execution_enabled']=True
    elif change=='remove':value['removes_candidates']=True
    else:del value['discovery_run']
    if change!='tamper':value['id']=digest({k:v for k,v in value.items() if k!='id'})
    order,usage=preferred_order([value],scope,[60,50,40])
    assert order==[60,50,40] and not usage['applied']


def test_conflicting_hints_do_not_choose_an_arbitrary_winner():
    scope,r,a=fixture();r['recommended_reference_speed']=50;r['attempts'][0]['reference_speed']=50
    b=propose_preference(r,'other-run')
    order,usage=preferred_order([a,b],scope,[60,50,40])
    assert order==[60,50,40] and usage['reason']=='conflicting_preferences'
    assert preferred_order([a,a],scope,[60,50,40])[0]==[40,60,50]


@pytest.mark.parametrize('mutation', ['partial','failure','unselected','authorized'])
def test_discovery_requires_complete_clear_non_executable_evidence(mutation):
    scope,r,hint=fixture()
    if mutation=='partial':r['attempts'][0]['audit'][0]['complete']=False
    elif mutation=='failure':r['attempts'][0]['audit'][0]['failures']=[dict(virtual_ns=1,fraction=1)]
    elif mutation=='unselected':r['recommended_reference_speed']=None
    else:r['execution_authorized']=True
    with pytest.raises(ValueError):propose_preference(r,'run')


def test_no_hint_or_malformed_collection_preserves_baseline_order():
    scope,r,hint=fixture()
    for values in (None,[],{},'bad',[None],['bad'],[{}]):
        order,usage=preferred_order(values,scope,[60,50,40])
        assert order==[60,50,40] and not usage['applied']


def test_control_implementation_change_invalidates_hint_with_same_profile_and_assets():
    context=dict(planning_scope={'recipe':'same'},control_profile_sha256='same-profile',
        numerical_asset_id='same-assets',control_source_sha256='old-control')
    inputs=dict(trajectory={'stops':[1]},start_index=0,skill='transfer_above_hole',post_boundary_samples=100)
    stages=[dict(start_index=0,end_index=2)]
    old=preference_scope(context,inputs,stages,[60,50,40])
    _,r,_=fixture();r['preference_scope']=old
    hint=propose_preference(r,'old-implementation-discovery')
    assert preferred_order([hint],old,[60,50,40])[1]['applied']
    updated=preference_scope(dict(context,control_source_sha256='new-control'),inputs,stages,[60,50,40])
    order,usage=preferred_order([hint],updated,[60,50,40])
    assert order==[60,50,40] and not usage['applied']
    legacy=deepcopy(hint);del legacy['scope']['control_source_sha256']
    legacy['id']=digest({k:v for k,v in legacy.items() if k!='id'})
    assert not preferred_order([legacy],old,[60,50,40])[1]['applied']
