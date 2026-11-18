"""Expected missing measurements are preview results; ownership faults are not."""
import pytest
from rpent.utils.rpc.rpc_client import RpcError

from f1_harness.contracts import HarnessError
from f1_harness.remote import ObservationWaitEnded
from test_remote_readiness import combined_backend


MISSING='object_not_visible_in_either_calibrated_view'


@pytest.mark.parametrize('tool',['preview_skill','inspect_plan','geometry_context'])
def test_missing_observation_returns_bounded_readonly_result(monkeypatch,tool):
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',MISSING)])
    backend.task_deadline=.25
    result=backend.execute_tool(tool,{} if tool=='geometry_context' else {'name':'approach_grasp'})
    preview=result['preview'] if tool=='inspect_plan' else result
    assert not preview['planned'] and preview['no_motion'] and not preview['executable']
    assert preview['current_scope'] and not preview['geometry_available']
    assert preview['planning_diagnostic']['code']=='observation_unavailable'
    assert preview['reason'].endswith(MISSING)
    assert preview['condition']['code']==MISSING and preview['recovery_hint']=='refresh_scene'
    assert not {'observation_id','scene_version','objects','received_ns'} & preview.keys()
    assert not backend.submissions and backend.pending is None
    assert backend.clock.value==pytest.approx(.25) and backend.task_deadline==.25
    assert backend.calls[-1]=='status'
    assert backend.evidence_store.events[-1]['kind']=='geometric_preview_observation_unavailable'


@pytest.mark.parametrize('fault',[
    'camera_identity_changed','owner_perception_context_changed','FileNotFoundError',
    'feedback_expired','unknown_server_fault',
])
def test_identity_feedback_or_unknown_fault_is_not_a_missing_measurement(monkeypatch,fault):
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',fault)])
    backend.task_deadline=.15
    with pytest.raises(ObservationWaitEnded):
        backend.execute_tool('preview_skill',{'name':'approach_grasp'})
    assert not backend.submissions
    assert not any(e['kind']=='geometric_preview_observation_unavailable' for e in backend.evidence_store.events)


@pytest.mark.parametrize('fault',['epoch','authorization','hold','identity','connection'])
def test_final_status_fault_cannot_recover_missing_observation(monkeypatch,fault):
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',MISSING)])
    backend.task_deadline=.15
    original=backend.call
    def call(method,**kwargs):
        if method=='status' and backend.clock.value>=.15 and fault=='connection':
            raise ConnectionError('lost owner')
        result=original(method,**kwargs)
        if method=='status' and backend.clock.value>=.15:
            if fault=='epoch':result['epoch']+=1
            elif fault=='authorization':result['authorized']=False
            elif fault=='hold':result['hold_requested']=True
            elif fault=='identity':result['observation_readiness']=dict(state='needs_attention',code='camera_identity_changed')
        return result
    backend.call=call
    with pytest.raises(ConnectionError if fault=='connection' else HarnessError):
        backend.execute_tool('preview_skill',{'name':'approach_grasp'})
    assert not backend.submissions


def test_pending_submission_never_enters_readonly_recovery(monkeypatch):
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',MISSING)])
    backend.pending='original'
    with pytest.raises(HarnessError,match='unresolved_submission_requires_query'):
        backend.execute_tool('preview_skill',{'name':'approach_grasp'})
    assert not backend.calls


def test_planning_time_does_not_consume_acquisition_budget(monkeypatch):
    backend=combined_backend(monkeypatch,[True]);backend.task_deadline=30.
    original=backend.call
    def call(method,**kwargs):
        if method=='preview_skill':
            backend.clock.value+=6.
            return dict(planned=True,current_scope=True,no_motion=True,executable=False)
        return original(method,**kwargs)
    backend.call=call
    assert backend.execute_tool('preview_skill',{'name':'approach_grasp'})['planned']
    assert backend.clock.value==6. and backend.task_deadline==30.
    assert not backend.submissions


def test_actual_pyrua_ledger_keeps_wait_cause_without_invented_geometry(monkeypatch,tmp_path):
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',MISSING)])
    backend.task_deadline=.15
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images=False)
    feedback=arm.execute_tool('python',dict(code='r = robo.inspect_plan("approach_grasp")')).result
    assert not feedback.get('exception')
    call=next(c for c in feedback['primitive_calls'] if c['tool']=='inspect_plan')
    assert call['planned'] is False and call['current_scope']
    assert call['planning_diagnostic']['metrics']['reason'].endswith(MISSING)
    assert not call.get('geometry') and not call.get('missing_entities')
    assert not backend.submissions
