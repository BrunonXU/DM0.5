from types import SimpleNamespace
import pytest
from test_inference_execution import NOW,BINDING,response,capture,snapshot,profile
from f1_policy_client.execution import prepare_prefix
from f1_policy_client.native import fresh_start,Timeline,MODE,PERIOD_NS
from f1_replay.runtime import ReplayError
from f1_policy_client.execution import IKPrefixRejected
from f1_policy_client.native import activate_with_ik_retry


def test_ik_rejection_reobserves_and_only_activates_success():
    observations=iter(['failed-observation','new-observation'])
    seen=[];activated=[];retries=[]
    def attempt():
        observation=next(observations);seen.append(observation)
        if observation=='failed-observation':
            raise IKPrefixRejected({'ik':{'arm':0,'step':4}})
        activated.append(observation)
        return observation
    assert activate_with_ik_retry(attempt,3,lambda:True,lambda n,e:retries.append((n,e.ik)))=='new-observation'
    assert len(seen)==2 and activated==['new-observation']
    assert retries==[(1,{'arm':0,'step':4})]


def test_persistent_ik_failure_is_bounded_and_request_budget_respected():
    attempts=[]
    def fail():
        attempts.append(1);raise IKPrefixRejected({'ik':{'arm':0,'step':4}})
    with pytest.raises(IKPrefixRejected):activate_with_ik_retry(fail,3,lambda:True,lambda *_:None)
    assert len(attempts)==4
    attempts.clear()
    with pytest.raises(IKPrefixRejected):activate_with_ik_retry(fail,3,lambda:False,lambda *_:None)
    assert len(attempts)==1


def test_ik_recovery_does_not_retry_unrelated_errors_or_wrong_binding():
    calls=[]
    def fail():
        calls.append(1);raise ReplayError('feedback lost')
    with pytest.raises(ReplayError,match='feedback lost'):
        activate_with_ik_retry(fail,3,lambda:True,lambda *_:pytest.fail('must not retry'))
    assert len(calls)==1
    r=response();r.update(status='rejected',reason='ik_rejected',ik={'step':4,'arm':0})
    with pytest.raises(IKPrefixRejected):
        prepare_prefix(r,capture(),snapshot(),profile(),now_ns=NOW,request_id='request-7',
                       expected_model_binding=BINDING,max_observation_age_s=2)
    r['model_binding']={'wrong':'checkpoint'}
    with pytest.raises(ReplayError,match='binding mismatch'):
        prepare_prefix(r,capture(),snapshot(),profile(),now_ns=NOW,request_id='request-7',
                       expected_model_binding=BINDING,max_observation_age_s=2)


def full_response():
    r=response();r.update(execution_mode=MODE,execute_steps=50)
    r['joint_targets_deg']=[[i/100]+[0.]*13 for i in range(50)]
    r['gripper_targets']=[[50.,100.]]*50;r['base_velocity_targets']=[[.1,-.2,0.]]*50
    return r


def test_native_skips_expired_points_without_reanchoring_or_smoothing():
    r=full_response()
    out=prepare_prefix(r,capture(),snapshot(),profile(),now_ns=NOW,request_id='request-7',
        expected_model_binding=BINDING,max_observation_age_s=2,execute_steps=50,start_index=16)
    assert len(out.bundle.frames)==34
    assert out.bundle.frames[0].action[0]==r['joint_targets_deg'][16][0]
    assert out.bundle.frames[0].action[-3:]==(.1,-.2,0.)
    assert out.bundle.frames[0].time_ns==0
    assert out.bundle.frames[0].source_time_ns==NOW+round(16e9/30)
    assert out.response['anchor_joints14_deg']==[0.]*14


def test_future_horizon_and_identity_remain_gated():
    for mutate in [lambda r:r.update(execution_mode='wrong'),lambda r:r['joint_targets_deg'][35].__setitem__(0,999),
                   lambda r:r.update(request_id='old'),lambda r:r['anchor_joints14_deg'].__setitem__(0,1)]:
        r=full_response();mutate(r)
        with pytest.raises(ReplayError):
            prepare_prefix(r,capture(),snapshot(),profile(),now_ns=NOW,request_id='request-7',
                expected_model_binding=BINDING,max_observation_age_s=2,execute_steps=50,start_index=16)


def test_skip_uses_activation_time_and_rejects_exhausted_or_future_observation():
    assert fresh_start(NOW,NOW+500_000_000,12)==15
    assert fresh_start(NOW,NOW+500_000_001,12)==16
    with pytest.raises(ReplayError):fresh_start(NOW,NOW+1_300_000_000,12)
    with pytest.raises(ReplayError):fresh_start(NOW,NOW-1,12)


def test_queue_never_repeats_last_velocity_and_preserves_rate_across_switch():
    t=Timeline();p=SimpleNamespace(bundle=SimpleNamespace(frames=['a','b']))
    t.activate(p,'first',0,NOW)
    assert t.due(NOW,10_000_000)==('a',0)
    next_due=t.next_ns;t.activate(p,'next',15,NOW+1_000_000)
    assert t.next_ns==next_due and t.due(NOW+1_000_000,10_000_000) is None
    assert t.due(next_due,10_000_000)==('a',15)
    assert t.due(t.next_ns,10_000_000)==('b',16)
    with pytest.raises(ReplayError,match='耗尽'):t.due(t.next_ns,10_000_000)
