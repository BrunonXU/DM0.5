"""A noisy new frame may submit old computation, never old motion permission."""
import numpy as np
import pytest

from f1_harness.contracts import HarnessError,MotionParameters,digest
from f1_harness.owner_bridge import numerical_failure_detail
from test_grasp_selection_planning import setup


def world_case():
    service,obs,intent,clock=setup()
    service.task=service.task.model_copy(update={'allowed_skills':(*service.task.allowed_skills,'move_tcp')})
    service.fixture.update(cartesian_motion=True,execution_mode='timed_reference',reference_velocity_deg_s=10.)
    goal=service._scene(obs,[0.]*22,obs.state16).base_eef.copy();goal[2,3]+=.02
    intent=intent.model_copy(update=dict(skill='move_tcp',task_hash=digest(service.task),
        parameters=MotionParameters(target_frame='world',target_tcp_pose=tuple(goal.ravel()))))
    return service,obs,intent,clock,goal


def fresh(obs,clock):
    clock[0]+=100_000_000
    pose=list(obs.objects[0].pose);pose[3]+=.001
    return obs.model_copy(update=dict(observation_id='new',scene_version='new',received_ns=clock[0],
        state_received_ns=clock[0],frames=tuple(f.model_copy(update={'received_ns':clock[0]}) for f in obs.frames),
        objects=(obs.objects[0].model_copy(update={'pose':tuple(pose)}),*obs.objects[1:])))


def test_preview_changed_measurement_submits_once_then_current_path_is_swept(monkeypatch):
    service,obs,intent,clock,goal=world_case()
    calls=[];solve=service._solve_candidate
    def count(*a,**kw):calls.append(1);return solve(*a,**kw)
    monkeypatch.setattr(service,'_solve_candidate',count)
    args=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16))
    preview=service.dispatch('plan_skill',dict(args,preview=True));before=len(calls)
    latest=fresh(obs,clock);request=intent.model_copy(update=dict(request_id='execute',observation_id='new',scene_version='new'))
    args.update(intent=request.model_dump(),observation=latest.model_dump())
    submitted=service.dispatch('plan_skill',args)
    assert len(calls)==before and np.array_equal(preview['targets'],submitted['targets'])
    reused=submitted['geometry_trace'][0]['reused_numerical_candidate']
    assert reused['current_observation_id']=='new' and reused['source_request_id']==intent.request_id
    assert reused['fresh_revalidation_required'] and not reused['motion_authorized']
    current=fresh(latest,clock)
    proof=service.dispatch('revalidate_plan',dict(intent=request.model_dump(),proposal=submitted,
        original_observation=latest.model_dump(),observation=current.model_dump(),full22=[0.]*22,
        held16=list(obs.state16),after_ns=clock[0]-1))
    assert proof['geometry_checks']['swept_segments']>0
    assert proof['geometry_checks']['native_fixed_path_stages']==len(submitted['geometry_trace'])
    assert proof['geometry_checks']['swept_observation_sha256']==digest(current)
    # A newly blocked endpoint still rejects this reused numerical candidate.
    service.fixture['obstacles']=(*service.fixture['obstacles'],dict(role='new_blocker',
        min=[-.1,-.1,-.1],max=[.1,.1,.1],pose=goal.tolist()))
    with pytest.raises(HarnessError,match='sweep'):
        service.dispatch('revalidate_plan',dict(intent=request.model_dump(),proposal=submitted,
            original_observation=latest.model_dump(),observation=current.model_dump(),full22=[0.]*22,
            held16=list(obs.state16),after_ns=clock[0]-1))
    service.fixture['obstacles']=service.fixture['obstacles'][:-1]
    service.dispatch('plan_skill',args)
    assert len(calls)>before  # The failed submission cannot loop on that preview.


@pytest.mark.parametrize('change',['epoch','camera_epoch','gripper','target','held'])
def test_preview_does_not_cross_changed_authority_identity_or_intent(monkeypatch,change):
    service,obs,intent,clock,goal=world_case();calls=[];solve=service._solve_candidate
    def count(*a,**kw):calls.append(1);return solve(*a,**kw)
    monkeypatch.setattr(service,'_solve_candidate',count)
    args=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16))
    service.dispatch('plan_skill',dict(args,preview=True));before=len(calls)
    latest=fresh(obs,clock);request=intent.model_copy(update=dict(request_id='execute',observation_id='new',scene_version='new'))
    if change=='epoch':request=request.model_copy(update={'epoch':2})
    if change=='camera_epoch':latest=latest.model_copy(update={'frames':tuple(f.model_copy(update={'source_epoch':1}) for f in latest.frames)})
    if change=='gripper':args['held16'][15]=50.
    if change=='target':
        goal[2,3]+=.01;request=request.model_copy(update={'parameters':MotionParameters(target_tcp_pose=tuple(goal.ravel()))})
    if change=='held':latest=latest.model_copy(update={'objects':(latest.objects[0].model_copy(update={'held':None}),*latest.objects[1:])})
    service.dispatch('plan_skill',dict(args,intent=request.model_dump(),observation=latest.model_dump()))
    assert len(calls)>before


@pytest.mark.parametrize('error',[TimeoutError('timed out'),RuntimeError('TimeoutError: timed out'),
    RuntimeError('RuntimeError: TimeoutError: timed out')])
def test_nested_rpc_deadline_keeps_specific_diagnostic(monkeypatch,error):
    service,obs,intent,_,_=world_case();original=service.numerics.call
    def numeric(method,**kwargs):
        if method=='solve_path':raise error
        return original(method,**kwargs)
    monkeypatch.setattr(service.numerics,'call',numeric)
    with pytest.raises(HarnessError) as caught:
        service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            full22=[0.]*22,held16=list(obs.state16)))
    detail=numerical_failure_detail(caught.value)
    assert detail['code']=='numerical_rpc_timeout'
    assert detail['metrics']['reason']=='numerical_transport_deadline_exceeded'

