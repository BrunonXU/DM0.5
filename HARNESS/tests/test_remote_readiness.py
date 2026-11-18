import pytest

from f1_harness.contracts import HarnessError,Observation,Task,SkillIntent,digest
from f1_harness.remote import RemoteTaskBackend


class Clock:
    def __init__(self):self.value=0.
    def monotonic(self):return self.value
    def sleep(self,duration):self.value+=duration


class Store:
    def __init__(self):self.events=[]
    def event(self,kind,**values):self.events.append(dict(kind=kind,**values))


def combined_backend(monkeypatch,observations,*,change_epoch=False):
    b=backend(monkeypatch,observations,change_epoch=change_epoch)
    b.observation_snapshot_supported=True;original=b.call;b.wire_calls=[]
    def call(method,**kwargs):
        b.wire_calls.append(method)
        if method=='observe_with_status':
            return dict(observation=original('observe',**kwargs),status=original('status',**kwargs))
        return original(method,**kwargs)
    b.call=call
    return b


def test_combined_observation_uses_one_read_rpc_before_submit(monkeypatch):
    b=combined_backend(monkeypatch,[True])
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success'
    assert b.wire_calls==['observe_with_status','submit_skill','skill_result']
    assert len(b.submissions)==1 and b.submissions[0]['observation_id']=='obs1'


def test_inspect_plan_acquires_once_then_requests_atomic_owner_preview(monkeypatch):
    b=combined_backend(monkeypatch,[True]);original=b.call;previews=[]
    def call(method,**kwargs):
        if method=='preview_skill':
            previews.append(kwargs)
            return dict(planned=True,geometry={'observation_id':'obs1'},preview={'observation_id':'obs1'})
        return original(method,**kwargs)
    b.call=call
    result=b.execute_tool('inspect_plan',{'name':'approach_grasp','parameters':{'pitch_offset_deg':10}})
    assert b.wire_calls==['observe_with_status']
    assert len(previews)==1 and previews[0]['include_geometry'] is True
    assert previews[0]['intent']['observation_id']=='obs1'
    assert result['planned'] and not b.submissions


def test_failed_combined_read_checks_authority_then_waits_without_rereading(monkeypatch):
    b=combined_backend(monkeypatch,[ValueError('scene_expired'),True])
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success'
    assert b.wire_calls==['observe_with_status','status','observe_with_status','submit_skill','skill_result']


def test_combined_wait_cannot_cross_owner_epoch(monkeypatch):
    b=combined_backend(monkeypatch,[False,True],change_epoch=True)
    with pytest.raises(HarnessError,match='epoch_changed'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert not b.submissions and b.wire_calls==['observe_with_status','observe_with_status']


@pytest.mark.parametrize('fault',['revoked','hold','attention','moving'])
def test_combined_status_preserves_authority_and_readiness(monkeypatch,fault):
    b=combined_backend(monkeypatch,[True]);original=b.call;b.task_deadline=.2
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='observe_with_status':
            status=value['status']
            if fault=='revoked':status['authorized']=False
            elif fault=='hold':status['hold_requested']=True
            elif fault=='moving':status['motion_wait']=True
            else:status['observation_readiness']={'state':'needs_attention','code':'camera_identity_changed'}
        return value
    b.call=call
    if fault in ('revoked','hold'):
        with pytest.raises(HarnessError,match='authority_changed'):
            b.execute_tool('execute_skill',{'name':'approach_grasp'})
    else:
        assert b.execute_tool('execute_skill',{'name':'approach_grasp'})['outcome']=='pending_review'
    assert not b.submissions


def backend(monkeypatch, observations, *, change_epoch=False):
    clock=Clock()
    monkeypatch.setattr('f1_harness.remote.time.monotonic',clock.monotonic)
    monkeypatch.setattr('f1_harness.remote.time.sleep',clock.sleep)
    b=RemoteTaskBackend.__new__(RemoteTaskBackend)
    b.task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    b.session='session';b.instance='owner';b.pending=None;b.steps=0;b._solved=False
    b.clock=clock
    b.evidence_store=Store();b.calls=[];b.submissions=[]
    statuses=0;index=0
    def call(method,*,timeout_s=2,**kwargs):
        nonlocal statuses,index
        b.calls.append(method)
        if method=='status':
            statuses+=1
            return dict(owner_instance_id='owner',task_hash=digest(b.task),authorized=True,
                hold_requested=False,epoch=2 if change_epoch and statuses>1 else 1,
                perception_context=dict(session_id='session'),mode='fake',
                owner_monotonic_ns=1_000_000_000_000+int(clock.value*1e9),observation_age_ns=500_000_000)
        if method=='observe':
            item=observations[min(index,len(observations)-1)];index+=1
            if isinstance(item,Exception):raise item
            # F1's monotonic epoch intentionally differs from the planner test clock.
            return Observation(observation_id='obs'+str(index),acquisition_instance='a',
                received_ns=1_000_000_000_000+int(clock.value*1e9),clock_domain='robot',
                calibration_id='cal',scene_version='s'+str(index),stationary=item,synthetic=True).model_dump()
        if method=='submit_skill':
            b.submissions.append(kwargs['intent'])
            return dict(request_id=kwargs['intent']['request_id'],lifecycle='accepted')
        if method=='skill_result':
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='success',assisted=False)
        raise AssertionError(method)
    b.call=call
    return b


def test_new_epoch_stationary_history_waits_without_submitting(monkeypatch):
    b=backend(monkeypatch,[ValueError('scene rebuilding'),False,False,True])
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success'
    assert b.calls.count('observe')==4
    assert len(b.submissions)==1 and b.submissions[0]['observation_id']=='obs4'
    assert b.calls.index('submit_skill')>max(i for i,v in enumerate(b.calls) if v=='observe')
    assert b.evidence_store.events[-1]['kind']=='observation_ready_before_submit'


def test_explicit_astra_observe_waits_for_fresh_scene_without_action(monkeypatch):
    b=backend(monkeypatch,[ValueError('scene_expired'),False,True])
    b.task_deadline=12.
    result=b.execute_tool('observe',{})
    assert result['observation']['observation_id']=='obs3'
    assert b.calls.count('observe')==3 and not b.submissions
    assert b.task_deadline==12. and b.pending is None
    assert result['status']['authorized']


@pytest.mark.parametrize('task_deadline',[None,300.])
def test_plain_observe_returns_missing_data_without_extending_task(monkeypatch,task_deadline):
    b=backend(monkeypatch,[ValueError('scene_expired')])
    if task_deadline is not None:b.task_deadline=task_deadline
    result=b.execute_tool('observe',{})
    assert b.clock.value==pytest.approx(4.)
    assert getattr(b,'task_deadline',None)==task_deadline
    assert result['geometry_available'] is False and result['observation'] is None
    assert result['no_motion'] and result['current_scope']
    assert not b.submissions and b.pending is None


@pytest.mark.parametrize('tool',['observe','execute_skill'])
@pytest.mark.parametrize('stop_field',['authorized','hold_requested'])
def test_stop_interrupts_readiness_wait_without_using_remaining_budget(monkeypatch,tool,stop_field):
    b=backend(monkeypatch,[False]);original=b.call;b.task_deadline=300.
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status' and b.clock.value>=.2:
            value[stop_field]=stop_field=='hold_requested'
        return value
    b.call=call
    with pytest.raises(HarnessError,match='observation_wait_authority_changed'):
        b.execute_tool(tool,{} if tool=='observe' else {'name':'approach_grasp'})
    assert b.clock.value==pytest.approx(.2) and b.task_deadline==300.
    assert not b.submissions and b.pending is None
    assert b.calls[-1]=='status' and b.calls.count('observe')==2


def test_explicit_observe_cannot_wait_past_task_deadline_or_cross_epoch(monkeypatch):
    from f1_harness.remote import ObservationWaitEnded
    b=backend(monkeypatch,[ValueError('scene_expired')]);b.task_deadline=.2
    result=b.execute_tool('observe',{})
    assert result['no_motion'] and not b.submissions
    assert b.clock.value==pytest.approx(.2)
    b=backend(monkeypatch,[True],change_epoch=True);b.task_deadline=2.
    with pytest.raises(HarnessError,match='epoch_changed'):
        b.execute_tool('observe',{})
    assert not b.submissions


def test_wait_timeout_has_no_request_or_submission(monkeypatch):
    b=backend(monkeypatch,[False])
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion']
    assert b.clock.value==pytest.approx(4.) and b.task_deadline==300.
    assert not b.submissions and b.pending is None
    assert b.evidence_store.events[-1]['submitted'] is False


def test_epoch_change_while_observing_aborts_without_submit(monkeypatch):
    b=backend(monkeypatch,[True],change_epoch=True)
    with pytest.raises(HarnessError,match='epoch_changed'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert not b.submissions and b.pending is None


def test_unknown_submission_never_reenters_observation_wait(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='submit_skill':
            original(method,**kwargs)
            raise TimeoutError('lost acknowledgement')
        if method=='skill_result':raise TimeoutError('unknown')
        return original(method,**kwargs)
    b.call=call
    with pytest.raises(HarnessError,match='submission_unknown_stop_and_query'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert len(b.submissions)==1 and b.calls.count('observe')==1 and b.pending is not None


def test_acquisition_budget_does_not_interrupt_an_accepted_motion(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='heartbeat':return {}
        if method=='skill_result' and b.clock.value<6.:
            return dict(request_id=kwargs['request_id'],lifecycle='running')
        return original(method,**kwargs)
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success' and b.clock.value>=6.
    assert len(b.submissions)==1 and b.submissions[0]['budget_ms']==10000
    assert b.calls.count('observe')==1 and 'cancel_skill' not in b.calls
    assert b.pending is None


def test_status_transport_failure_preserves_last_observation_cause(monkeypatch):
    from rpent.utils.rpc.rpc_client import RpcError
    b=backend(monkeypatch,[RpcError('observe','frame_feedback_skew')]);original=b.call
    statuses=0
    def call(method,**kwargs):
        nonlocal statuses
        if method=='status':
            statuses+=1
            if statuses==2:raise RpcError('status','HTTP request failed: timed out')
        return original(method,**kwargs)
    b.call=call
    with pytest.raises(HarnessError,match='frame_feedback_skew'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert not b.submissions and b.pending is None
    assert 'timed out' in b.evidence_store.events[-1]['transport_error']


@pytest.mark.parametrize('retryable,no_motion,expected',[(True,True,3),(False,False,1),(False,True,1)])
def test_only_confirmed_unpublished_candidates_retry_with_new_ids(monkeypatch,retryable,no_motion,expected):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='skill_result':
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                assisted=False,retryable=retryable,no_motion=no_motion,reason='planning_frame_expired_before_publication')
        return original(method,**kwargs)
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='failure' and len(b.submissions)==expected
    assert len({row['request_id'] for row in b.submissions})==expected
    assert len({row['observation_id'] for row in b.submissions})==expected
    assert b.pending is None


def test_replanning_cannot_cross_epoch_or_reuse_unknown_submission(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='skill_result':
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                assisted=False,retryable=True,no_motion=True,reason='planning_frame_expired_before_publication')
        value=original(method,**kwargs)
        if method=='status' and b.submissions:value['epoch']=2
        return value
    b.call=call
    with pytest.raises(HarnessError,match='observation_wait_epoch_changed'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert len(b.submissions)==1


@pytest.mark.parametrize('channel',['submit_skill','skill_result'])
@pytest.mark.parametrize('fault',['wrong_id','invalid_retry','invalid_lifecycle'])
def test_unconfirmed_or_malformed_receipt_keeps_pending_and_never_retries(monkeypatch,channel,fault):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        result=original(method,**kwargs)
        if method==channel:
            if fault=='wrong_id':result['request_id']='other-request'
            elif fault=='invalid_retry':
                result.update(lifecycle='completed',outcome='failure',retryable=True,no_motion=False)
            else:result['lifecycle']='not-a-known-terminal-state'
        return result
    b.call=call
    with pytest.raises(HarnessError,match='skill_result_request_mismatch|skill_result_contract_invalid'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert len(b.submissions)==1 and b.pending==b.submissions[0]['request_id']
    assert b.steps==0 and b.calls.count('observe')==1


def test_lost_ack_query_terminal_at_deadline_stays_known(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call;queries=[]
    def call(method,**kwargs):
        if method=='submit_skill':
            original(method,**kwargs);b.clock.sleep(2.)
            raise TimeoutError('ack lost')
        if method=='skill_result':
            queries.append(kwargs['request_id']);b.clock.sleep(2.)
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure')
        return original(method,**kwargs)
    b.call=call
    result=b._submit_and_wait(intent(b),deadline=4.)
    assert result['outcome']=='failure' and b.pending is None and b.steps==1
    assert len(queries)==1 and len(b.submissions)==1 and b.clock.value==4.


def intent(b):
    return SkillIntent(request_id='bound-request',session_id=b.session,owner_instance_id=b.instance,
        task_id=b.task.task_id,task_hash=digest(b.task),epoch=1,observation_id='obs',
        scene_version='scene',calibration_id=b.task.calibration_id,skill='approach_grasp',budget_ms=10000)


def test_absolute_deadline_bounds_submit_poll_heartbeat_and_then_requests_stop(monkeypatch):
    b=backend(monkeypatch,[True]);calls=[]
    def call(method,*,timeout_s=2,**kwargs):
        calls.append((method,timeout_s,b.clock.value))
        if method=='submit_skill':
            b.clock.sleep(.8)
            return dict(request_id='bound-request',lifecycle='accepted')
        if method=='skill_result':
            b.clock.sleep(.7)
            return dict(request_id='bound-request',lifecycle='running')
        if method=='heartbeat':b.clock.sleep(timeout_s);return {}
        if method=='cancel_skill':return {}
        raise AssertionError(method)
    b.call=call
    with pytest.raises(HarnessError,match='skill_deadline_stop_pending'):
        b._submit_and_wait(intent(b),deadline=3.)
    assert [row[0] for row in calls]==['submit_skill','skill_result','heartbeat','cancel_skill']
    assert [row[1] for row in calls]==pytest.approx([2.,2.,1.5,2.])
    assert calls[-1][2]==pytest.approx(3.) and b.pending=='bound-request'


@pytest.mark.parametrize('rejection',['unpublished_candidate','owner_motion_wait'])
def test_candidate_retries_share_initial_four_second_readiness_deadline(monkeypatch,rejection):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='observe':
            value=original(method,**kwargs)
            value['stationary']=b.clock.value>=1. and not b.submissions
            return value
        if method=='submit_skill' and rejection=='owner_motion_wait':
            original(method,**kwargs);b.clock.sleep(1.)
            return dict(request_id=kwargs['intent']['request_id'],lifecycle='rejected',
                no_motion=True,reason='owner_motion_wait')
        if method=='skill_result':
            b.clock.sleep(1.)
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                no_motion=True,retryable=True,reason='planning_frame_expired_before_publication')
        return original(method,**kwargs)
    b.call=call;b.task_deadline=60.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['no_motion'] and b.task_deadline==60.
    assert len(b.submissions)==1 and b.pending is None
    assert b.submissions[0]['budget_ms']==10000
    if rejection=='owner_motion_wait':
        assert result['outcome']=='pending_review'
        assert result['reason'].startswith('observation_readiness_timeout:')
        assert b.clock.value==pytest.approx(4.)
        assert b.evidence_store.events[-1]['submitted'] is False
    else:
        assert result['outcome']=='failure'
        assert result['request_id']==b.submissions[0]['request_id']
        assert result['observation_retry_reason'].startswith('observation_readiness_timeout:')
        assert b.clock.value==pytest.approx(5.)
        assert b.evidence_store.events[-1]['submitted'] is True



def test_already_expired_preparation_never_marks_or_submits_a_request(monkeypatch):
    b=backend(monkeypatch,[True])
    with pytest.raises(HarnessError,match='skill_preparation_budget_exhausted'):
        b._submit_and_wait(intent(b),deadline=0.)
    assert b.pending is None and not b.submissions


def test_owner_idle_wait_skips_observe_and_uses_same_readiness_deadline(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':value['motion_wait']={'reason':'stationary_output_timing'}
        return value
    b.call=call
    b.task_deadline=30.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion']
    assert b.clock.value==pytest.approx(4.) and b.task_deadline==30.
    assert 'observe' not in b.calls and not b.submissions and b.pending is None


@pytest.mark.parametrize('reason',['owner_motion_wait','observation_retired_by_motion_wait'])
def test_idle_wait_race_does_not_consume_numerical_replan_credits(monkeypatch,reason):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='submit_skill' and len(b.submissions)<=4:
            b.clock.sleep(.1)
            return dict(request_id=kwargs['intent']['request_id'],lifecycle='rejected',
                reason=reason,no_motion=True)
        if method=='skill_result':
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                no_motion=True,retryable=True,reason='planning_frame_expired_before_publication')
        return value
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='failure' and len(b.submissions)==7
    assert len({r['request_id'] for r in b.submissions})==7 and b.pending is None
    assert sum(row['kind']=='unpublished_candidate_retry' for row in b.evidence_store.events)==2


def test_repeated_pre_admission_races_cannot_extend_shared_task_deadline(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='submit_skill':
            b.clock.sleep(10.)
            return dict(request_id=kwargs['intent']['request_id'],lifecycle='rejected',
                reason='owner_motion_wait',no_motion=True)
        return value
    b.call=call
    b.task_deadline=30.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion']
    assert b.clock.value==pytest.approx(10.) and len(b.submissions)==1 and b.pending is None


def test_wait_race_rejects_changed_epoch_before_any_new_observation(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='submit_skill':
            return dict(request_id=kwargs['intent']['request_id'],lifecycle='rejected',
                reason='owner_motion_wait',no_motion=True)
        if method=='status' and b.submissions:
            value.update(epoch=2,motion_wait={'reason':'stationary_output_timing'})
        return value
    b.call=call
    with pytest.raises(HarnessError,match='observation_wait_epoch_changed'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert b.calls.count('observe')==1 and len(b.submissions)==1 and b.pending is None


def test_lost_ack_retired_scene_queries_original_id_before_new_observation(monkeypatch):
    b=backend(monkeypatch,[True]);original=b.call;queries=[]
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='submit_skill' and len(b.submissions)==1:
            raise TimeoutError('lost terminal rejection acknowledgement')
        if method=='skill_result' and len(b.submissions)==1:
            queries.append(kwargs['request_id'])
            return dict(request_id=kwargs['request_id'],lifecycle='rejected',
                reason='observation_retired_by_motion_wait',no_motion=True)
        return value
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success' and len(b.submissions)==2 and b.pending is None
    assert queries==[b.submissions[0]['request_id']]
    assert b.submissions[0]['observation_id']!=b.submissions[1]['observation_id']
    assert b.calls[:7]==['status','observe','status','submit_skill','skill_result','status','observe']


def racing_scene(monkeypatch):
    """Exercise the actual observer phase race and its public readiness contract."""
    import threading
    from test_phase_live_scene import observer
    scene,_=observer(monkeypatch)
    original=scene.owner.call
    context=dict(owner_instance_id='owner',session_id='session',task_hash='task',
                 task_generation=1,epoch=1,authorized=True,phase='approach_grasp',request_id='old')
    contexts=iter([context,{**context,'phase':'observe','request_id':None}])
    def call(method,**kwargs):
        if method=='status':return {'perception_context':next(contexts)}
        return original(method,**kwargs)
    scene.owner.call=call
    scene.stop=threading.Event()
    scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert scene.latest is None and scene.error=='owner_perception_stage_advanced'
    return scene


def test_expired_zero_publication_retry_waits_through_actual_phase_race(monkeypatch):
    scene=racing_scene(monkeypatch)
    b=backend(monkeypatch,[True]);original=b.call
    phase_reads=0
    def call(method,**kwargs):
        nonlocal phase_reads
        if method=='status':
            value=original(method,**kwargs)
            if b.submissions:value['observation_readiness']=scene.readiness()
            return value
        if method=='observe' and b.submissions:
            phase_reads+=1
            if phase_reads==1:
                # The racing sample was discarded, not relabelled fresh.
                with pytest.raises(ValueError,match='owner_perception_stage_advanced'):
                    scene.observe()
                raise ValueError('owner_perception_stage_advanced')
            value=original(method,**kwargs)
            scene.latest=Observation.model_validate(value);scene.error=None
            return value
        if method=='skill_result' and len(b.submissions)==1:
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                no_motion=True,retryable=True,reason='numerical_plan_rejected:planning_measurements_expired')
        return original(method,**kwargs)
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='success' and len(b.submissions)==2
    assert phase_reads==2 and b.clock.value==pytest.approx(.1)
    assert b.submissions[0]['observation_id']!=b.submissions[1]['observation_id']
    assert b.submissions[1]['budget_ms']<b.submissions[0]['budget_ms']
    assert not any(e['kind']=='observation_needs_attention' for e in b.evidence_store.events)


def test_phase_race_wait_uses_original_deadline_without_submission(monkeypatch):
    scene=racing_scene(monkeypatch)
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        if method=='observe':return scene.observe().model_dump()
        value=original(method,**kwargs)
        if method=='status':value['observation_readiness']=scene.readiness()
        return value
    b.call=call;b.task_deadline=.25
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion']
    assert b.clock.value==pytest.approx(.25) and not b.submissions
    assert result['reason'].startswith('observation_readiness_timeout:')


@pytest.mark.parametrize('changed',['epoch','authorized','session','owner','task'])
def test_waiting_phase_race_never_masks_authority_change(monkeypatch,changed):
    scene=racing_scene(monkeypatch)
    b=backend(monkeypatch,[ValueError('scene_not_ready')]);original=b.call
    status_count=0
    def call(method,**kwargs):
        nonlocal status_count
        value=original(method,**kwargs)
        if method=='status':
            status_count+=1
            value['observation_readiness']=scene.readiness()
            if status_count>1:
                if changed=='epoch':value['epoch']=2
                elif changed=='authorized':value['authorized']=False
                elif changed=='session':value['perception_context']['session_id']='other'
                elif changed=='owner':value['owner_instance_id']='other'
                else:value['task_hash']='other'
        return value
    b.call=call
    with pytest.raises(HarnessError,match='observation_wait_(epoch|authority)_changed'):
        b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert not b.submissions and b.calls.count('observe')==1


@pytest.mark.parametrize('reason',['camera_calibration_identity_changed','feedback_clock_mismatch',
                                 'unregistered_scene_failure'])
def test_unknown_or_identity_readiness_faults_still_require_attention(monkeypatch,reason):
    from f1_harness.conditions import observation_condition
    b=backend(monkeypatch,[True]);original=b.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':value['observation_readiness']=observation_condition(reason)
        return value
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion']
    assert result['reason']=='observation_needs_attention:'+reason
    assert not b.submissions and 'observe' not in b.calls


def test_recheck_observation_wait_is_bounded_without_replaying(monkeypatch):
    b=backend(monkeypatch,[False]);b.task_deadline=300.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp','verification_request_id':'completed-motion'})
    assert result['outcome']=='pending_review' and not b.submissions
    assert b.clock.value==pytest.approx(4.) and b.task_deadline==300.


def test_return_scene_wait_returns_in_four_seconds_without_motion(monkeypatch):
    b=backend(monkeypatch,[False]);b.task_deadline=300.
    result=b.execute_tool('execute_skill',{'name':'withdraw_reset'})
    assert result['outcome']=='pending_review' and not b.submissions
    assert b.clock.value==pytest.approx(4.)


def test_post_placement_observe_cannot_wait_another_whole_task_budget(monkeypatch):
    from f1_harness.remote import ObservationWaitEnded
    b=backend(monkeypatch,[False]);b.task_deadline=300.;b.placement_verified=True
    with pytest.raises(ObservationWaitEnded):b.execute_tool('observe',{})
    assert b.clock.value==pytest.approx(4.) and not b.submissions


@pytest.mark.parametrize('placed',[True,False])
def test_read_only_post_placement_observe_uses_owner_verified_fixture_scope(monkeypatch,placed):
    from f1_harness.contracts import SKILLS
    from f1_harness.remote import ObservationWaitEnded
    from test_plan_revalidation import observation
    b=backend(monkeypatch,[True]);original=b.call
    b.task=b.task.model_copy(update={'allowed_skills':SKILLS,'admission_profile':'development_coarse'})
    obs=observation(1_000_000_000_000)
    obs=obs.model_copy(update=dict(calibration_id='cal',geometry_scope='coarse_fixture_return',geometry_camera='head',
        frames=tuple(f for f in obs.frames if f.camera=='head'),pairings=tuple(p for p in obs.pairings if p.camera=='head'),
        objects=tuple(o for o in obs.objects if o.object_id=='hole')))
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':
            value.update(mode='hardware',recording_identity={'host_boot_id':'boot'})
            value['perception_context']['placement_verified']=placed
        if method=='observe':return obs.model_dump()
        return value
    b.call=call
    if placed:
        actual,_=b._await_observation(timeout_s=.2,skill='reobserve')
        assert actual.geometry_scope=='coarse_fixture_return'
    else:
        with pytest.raises(ObservationWaitEnded):b._await_observation(timeout_s=.2,skill='reobserve')
    assert not b.submissions


@pytest.mark.parametrize('ready',[True,False])
def test_slow_unpublished_revalidation_preserves_acquisition_budget_and_receipt(monkeypatch,ready):
    b=backend(monkeypatch,[True,ready]);base=b.call
    def call(method,**kwargs):
        if method=='skill_result' and len(b.submissions)==1:
            b.clock.sleep(5.349406664)
            return dict(request_id=kwargs['request_id'],lifecycle='completed',outcome='failure',
                        reason='plan_revalidation_rejected:revalidation_object_or_hole_changed',
                        no_motion=True,retryable=True,planning_diagnostic={'source':'owner'})
        return base(method,**kwargs)
    b.call=call
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    if ready:
        assert result['outcome']=='success' and len(b.submissions)==2
        assert b.submissions[1]['observation_id']!=b.submissions[0]['observation_id']
    else:
        assert len(b.submissions)==1 and result['request_id']==b.submissions[0]['request_id']
        assert result['planning_diagnostic']=={'source':'owner'} and result['retryable']
        assert result['observation_retry_reason'].startswith('observation_readiness_timeout')
        assert b.attention['reason']==result['reason']
    assert b.clock.value<=9.35
