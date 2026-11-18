"""No repeated numerical work, without treating moved scenes as old failures."""
from dataclasses import replace
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.candidate_failures import CandidateFailures,candidate_key
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.adapters import _planning_feedback
from test_grasp_selection_planning import setup


def request(service,obs,intent):
    return service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=obs.state16))


def test_same_scene_new_ids_reuses_failed_candidates_but_changed_scene_recomputes(monkeypatch):
    service,obs,intent,clock=setup();calls=[]
    def fail(*args,deadline_ns=None): 
        calls.append(args)
        raise HarnessError('scene_sweep_blocked:{"obstacle_role":"bucket","collision_body":"tool"}')
    monkeypatch.setattr(service,'_solve_candidate',fail)
    with pytest.raises(HarnessError):request(service,obs,intent)
    count=len(calls);assert count==8
    clock[0]+=1000
    fresh=obs.model_copy(update=dict(observation_id='new',scene_version='new',received_ns=clock[0]))
    new=intent.model_copy(update=dict(request_id='new-request',observation_id='new',scene_version='new'))
    with pytest.raises(HarnessError) as caught:request(service,fresh,new)
    assert len(calls)==count
    detail=_planning_feedback(numerical_failure_detail(caught.value))
    assert detail['metrics']['reused_failure'] is True
    assert detail['metrics']['previous_request_id']==intent.request_id
    assert detail['metrics']['recovery_hint']=='change_candidate_or_observe_changed_geometry'
    # Even a small change is new input. There is no position/angle threshold.
    pose=list(fresh.objects[0].pose);pose[3]+=1e-6
    moved=fresh.model_copy(update={'objects':(fresh.objects[0].model_copy(update={'pose':tuple(pose)}),fresh.objects[1])})
    with pytest.raises(HarnessError):request(service,moved,new)
    assert len(calls)==2*count


@pytest.mark.parametrize('field',['full','held','fixture','epoch','acquisition','descriptor'])
def test_cache_key_binds_every_changed_planning_context(field):
    service,obs,intent,_=setup();scene=service._scene(obs,[0.]*22,obs.state16)
    full=[0.]*22;held=list(obs.state16)
    def key():return candidate_key(service.task,service.fixture,intent,obs,scene,held,full,0)
    before=key();assert before
    if field=='full':full[21]+=.0001
    elif field=='held':held[7]+=.0001
    elif field=='fixture':service.fixture['attached_radius_m']+=.00001
    elif field=='epoch':intent=intent.model_copy(update={'epoch':2})
    elif field=='acquisition':obs=obs.model_copy(update={'acquisition_instance':'restarted'})
    else:scene=replace(scene,grasp_candidate_descriptor=dict(strategy='tcp_pitch',pitch_offset_deg=10))
    assert key()!=before


def test_timeout_and_transport_failures_are_not_cached():
    cache=CandidateFailures()
    for code,metrics in [('bounded_IK_wall_time_exceeded',{}),('RpcError',{}),
            ('grasp_continuation_unreachable',{'continuation_code':'bounded_IK_wall_time_exceeded'})]:
        cache.remember('same',dict(code=code,metrics=metrics),'original')
        assert cache.get('same') is None


def test_failure_storage_is_bounded_and_defensive():
    cache=CandidateFailures(capacity=2)
    detail=dict(code='scene_sweep_blocked',metrics={})
    for key in ('a','b','c'):cache.remember(key,detail,'request')
    assert cache.get('a') is None
    value=cache.get('b');value['metrics']['recovery_hint']='mutated'
    assert cache.get('b')['metrics']['recovery_hint']!='mutated'


def test_successful_calculation_is_rebound_to_current_request_without_execution_permission(monkeypatch):
    service,obs,intent,_=setup();calls=[]
    def solve(i,*args,deadline_ns=None): calls.append(i.request_id);return dict(geometry_trace=[{}],request_id=i.request_id)
    monkeypatch.setattr(service,'_solve_candidate',solve)
    assert request(service,obs,intent)['request_id']=='request'
    new=intent.model_copy(update={'request_id':'new'})
    result=request(service,obs,new)
    assert result['request_id']=='new' and calls==['request']
    assert result['geometry_trace'][0]['reused_numerical_candidate']['motion_authorized'] is False
