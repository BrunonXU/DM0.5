"""A measured-stationary pre-grasp must preserve its identity across visual recovery."""
import numpy as np
import pytest

from f1_harness.recovery_programs import compile_library
from f1_harness.remote import RemoteTaskBackend
from f1_harness.planner_decision import PlannerDecision
from test_local_environment_motion import partial_setup


VIEW_NEEDED='observation_viewpoint_required:local_environment_only_supports_world_observation_motion'
VIEW=dict(target_frame='world',target_tcp_pose=np.eye(4).ravel().tolist(),observation_id='initial')
STALL=dict(request_id='original',resume_request_id='original',lifecycle='completed',
           outcome='failure',reason='motion_stalled',no_motion=False,cancel_stage='measured_stationary',
           planning_diagnostic=dict(code='motion_stalled',observation_view=VIEW))
BLOCKED=dict(STALL,observation_retry_reason=VIEW_NEEDED,continuation_submitted=False)
SUCCESS=dict(outcome='success')


def test_fresh_local_observation_hands_off_before_polling_and_preserves_actual_publication():
    service,obs,intent,clock=partial_setup()
    obs=obs.model_copy(update=dict(host_boot_id='real-boot'))
    backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
    backend.observation_snapshot_supported=True
    backend.task=service.task
    backend.session=intent.session_id
    backend.instance=intent.owner_instance_id
    backend.pending=None
    calls=[]
    def call(method,**kwargs):
        calls.append(method)
        if method=='skill_result':
            assert kwargs['request_id']=='original'
            return dict(STALL)
        assert method=='observe_with_status'
        return dict(observation=obs.model_dump(),status=dict(mode='hardware',owner_instance_id=backend.instance,
            epoch=intent.epoch,task_hash=intent.task_hash,authorized=True,hold_requested=False,
            owner_monotonic_ns=clock[0],observation_age_ns=500_000_000,
            perception_context=dict(session_id=backend.session),recording_identity=dict(host_boot_id='real-boot')))
    backend.call=call
    result=backend.execute_tool('execute_skill',dict(name='approach_grasp',resume_request_id='original'))
    assert calls==['observe_with_status','skill_result']
    assert result==BLOCKED
    assert backend.attention['no_motion'] is False and backend.attention['submitted'] is False


@pytest.mark.parametrize('planned,visible,expected',[
    (True,False,['resume','preview','move_tcp','reobserve','resume']),
    (False,True,['resume','preview','reobserve','geometry','resume']),
    (False,False,['resume','preview','reobserve','geometry'])])
def test_checked_view_and_stationary_recovery(planned,visible,expected):
    calls=[]
    class Robot:
        def resume(self,name,request):
            calls.append('resume')
            assert request=='original' and name=='approach_grasp'
            return dict(BLOCKED if len(calls)==1 else SUCCESS)
        def preview(self,name,**kwargs):
            calls.append('preview')
            assert name=='move_tcp'
            assert kwargs==dict(target_frame='world',target_tcp_pose=VIEW['target_tcp_pose'])
            return dict(current_scope=True,planned=planned)
        def skill(self,name,**kwargs):
            calls.append(name)
            return dict(SUCCESS)
        def geometry(self):
            calls.append('geometry')
            return dict(observation_id='fresh',object_pose=VIEW['target_tcp_pose'] if visible else None)
    ns={'robo':Robot()}
    exec(compile_library(2),ns)
    result=ns['continue_motion']('approach_grasp',dict(STALL))
    assert calls==expected
    if planned or visible:
        assert result['outcome']=='success'
    else:
        assert result['no_motion'] is False and result['resume_request_id']=='original'
        assert result['observation_recovery']['no_motion'] is True
        assert result['needs_new_strategy']
        assert result['observation_recovery']['current_observation_id']=='fresh'


def test_decision_retains_actionable_checkpoint_when_the_view_preview_is_rejected():
    result=dict(primitive_calls=[dict(BLOCKED,tool='execute_skill',kwargs=dict(name='approach_grasp')),
        dict(tool='preview_skill',kwargs=dict(name='move_tcp'),planned=False,current_scope=True,no_motion=True),
        dict(tool='execute_skill',kwargs=dict(name='reobserve'),ok=True,outcome='success')])
    ctx=PlannerDecision().update(result)
    assert ctx['next_decision']=='relocalize_target_from_a_new_view_then_resume_original_checkpoint'
    assert ctx['unresolved_blockers'][0]['resume_request_id']=='original'
