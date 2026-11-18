"""Template exhaustion must not erase the later general planner's reason."""
import json
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.adapters import _planning_feedback


@pytest.mark.parametrize('preceding',[20,28])
def test_last_joint_search_failure_survives_both_diagnostic_boundaries(preceding):
    candidates=[dict(variant=i%4,diagnostic=dict(code='bounded_IK_did_not_reach_goal',
        metrics=dict(goal=[[1.]*4]*4,final_arm_deg=[20.]*7,
                     route_family='demonstrated_transport',position_error_m=.001))) for i in range(preceding)]
    candidates.append(dict(variant=2,diagnostic=dict(code='numerical_joint_search_no_valid_goal',
        metrics=dict(route_family='joint_space_transport'))))
    raw=HarnessError('registered_candidates_exhausted:'+json.dumps(dict(
        diagnostic=dict(code='registered_candidates_exhausted',metrics={}),candidates=candidates)))
    parsed=numerical_failure_detail(raw)
    for value in (parsed,_planning_feedback(parsed)):
        assert value['candidate_count']==preceding+1
        assert value['last_candidate_failure']['code']=='numerical_joint_search_no_valid_goal'
        assert value['last_candidate_failure']['metrics']['route_family']=='joint_space_transport'
        assert len(json.dumps(value,ensure_ascii=False))<=4095


def test_large_last_failure_keeps_reason_without_bypassing_size_bound():
    from f1_harness.planning_diagnostics import bound_detail
    last=dict(code='missing_geometry_entities',variant=2,metrics=dict(
        route_family='joint_space_transport',
        missing_entities=[dict(object_id='x'*160,reason='y'*160) for _ in range(22)]))
    value=dict(code='registered_candidates_exhausted',metrics=last['metrics'],
               candidates=[last],last_candidate_failure=last)
    result=bound_detail(value)
    assert len(json.dumps(result,ensure_ascii=False))<=4095
    assert result['last_candidate_failure']['code']=='missing_geometry_entities'
