"""Measured CAN and geometry can arrive in separate frames at the transport RPC."""
import pytest

from f1_harness.remote import ObservationWaitEnded
from test_plan_revalidation import observation
from test_remote_readiness import combined_backend


def transport_backend(monkeypatch, measurements, *, completed=('verify_grasp',)):
    backend=combined_backend(monkeypatch,[True]*120)
    backend.task=backend.task.model_copy(update=dict(calibration_id='coarse',admission_profile='development_coarse'))
    backend.task_deadline=60.
    original=backend.call;count=0
    def call(method,**args):
        nonlocal count
        value=original(method,**args)
        if method=='observe_with_status':
            count+=1
            stamp=1_000_000_000+int(backend.clock.value*1e9)
            obs=observation(stamp,boot='boot',name=f'frame-{count}')
            held,measured_transform=measurements[min(count-1,len(measurements)-1)]
            target=obs.objects[0].model_copy(update={'held':held,'eef_object_pose':obs.objects[0].pose if measured_transform else None})
            obs=obs.model_copy(update={'objects':(target,*obs.objects[1:])})
            value['observation']=obs.model_dump()
            value['status'].update(mode='hardware',recording_identity={'host_boot_id':'boot'},
                owner_monotonic_ns=stamp,completed_skills=completed)
        return value
    backend.call=call
    return backend


@pytest.mark.parametrize('first,reason', [
    (True,'held_object_attachment_transform_pending'),
    (None,'verified_grasp_current_attachment_evidence_pending'),
])
def test_transit_only_submits_the_frame_with_current_measured_attachment(monkeypatch,first,reason):
    b=transport_backend(monkeypatch,[(first,False),(True,True)])
    result=b.execute_tool('execute_skill',{'name':'transfer_above_hole'})
    assert result['outcome']=='success'
    assert b.wire_calls==['observe_with_status','observe_with_status','submit_skill','skill_result']
    assert len(b.submissions)==1 and b.submissions[0]['observation_id']=='frame-2'
    assert any(e.get('reason')==reason for e in b.evidence_store.events)


@pytest.mark.parametrize('held,reason', [
    (True,'held_object_attachment_transform_pending'),
    (None,'verified_grasp_current_attachment_evidence_pending'),
])
def test_missing_attachment_exhausts_read_only_budget_without_submission(monkeypatch,held,reason):
    b=transport_backend(monkeypatch,[(held,False)])
    with pytest.raises(ObservationWaitEnded,match=reason):
        b._await_observation(skill='transfer_above_hole',timeout_s=4.)
    assert b.clock.value==pytest.approx(4.) and not b.submissions
    assert set(b.wire_calls)=={'observe_with_status'}


@pytest.mark.parametrize('completed,held', [
    (('verify_grasp',),False),(('verify_grasp','release'),None),
])
def test_explicit_empty_or_already_released_does_not_wait_for_prior_grasp(monkeypatch,completed,held):
    b=transport_backend(monkeypatch,[(held,False)],completed=completed)
    obs,_=b._await_observation(skill='transfer_above_hole',timeout_s=4.)
    assert obs.objects[0].held is held and obs.objects[0].eef_object_pose is None
    assert b.clock.value==0. and not b.submissions
