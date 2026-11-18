"""Slow measured sampling and rejected requests must preserve stage continuity."""
import time
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.remote import RemoteTaskBackend
from test_grasp_tracking import pipeline_fixture
from test_admission_diagnostics import rejected_intent
from test_unpublished_plan import make_bridge


def test_slow_independent_lift_can_prove_hold_and_stationary_hold_does_not_oscillate(monkeypatch):
    pipe,push=pipeline_fixture(monkeypatch)
    pipe.config['search_object_box']=dict(min=[-.015,-.015,0.],max=[.015,.015,.06])
    try:
        for index,z in zip((0,6,12),(0.,.02,.04)):
            result=push(index,z=z)
        assert result.objects[0].held is True
        for index in (18,24,30,36,70):
            result=push(index,z=.04,phase='verify_grasp')
            assert result.objects[0].held is True
            assert 'measurement_gap' in pipe.last_timing['quality_warnings']
        # Actual geometric departure is still unknown first, then confirmed.
        for index in (76,82,88,94):
            result=push(index,z=.04,relative_x=.15,phase='verify_grasp')
        assert result.objects[0].held is False
    finally:pipe.close()


@pytest.mark.parametrize('change',['opening','feedback','scope'])
def test_slow_cadence_does_not_override_real_invalidation(monkeypatch,change):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        args={'grip':0} if change=='opening' else {'valid':False} if change=='feedback' else {'epoch':2}
        result=push(8,z=.04,phase='verify_grasp',**args)
        assert result.objects[0].held is not True
    finally:pipe.close()


def test_rejected_request_has_idempotent_receipt_even_after_lost_submit_reply(tmp_path):
    engine,bridge,*_=make_bridge(tmp_path)
    intent=rejected_intent(engine)
    backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
    backend.session=engine.session;backend.steps=0;backend.task=engine.task
    backend.pending=None;calls=[]
    def call(method,**kw):
        kw.pop('timeout_s',None);calls.append(method)
        result=getattr(engine,method)(**kw)
        if method=='submit_skill':raise TimeoutError('lost reply after stored rejection')
        return result
    backend.call=call
    original_pending=engine.pending
    try:
        result=backend._submit_and_wait(intent,deadline=time.monotonic()+1)
        assert result['lifecycle']=='rejected' and result['no_motion']
        assert result['reason']=='failed_insertion_requires_retreat_and_alignment'
        assert backend.pending is None and calls==['submit_skill','skill_result']
        assert engine.submit_skill(intent.model_dump())==result
        assert engine.pending==original_pending and not engine.request_publications
        changed=intent.model_copy(update={'skill':'grasp'})
        with pytest.raises(HarnessError,match='request_id_content_conflict'):
            engine.submit_skill(changed.model_dump())
    finally:bridge.close()


def test_unknown_pending_cancel_still_revokes_task_without_replaying():
    backend=RemoteTaskBackend.__new__(RemoteTaskBackend)
    backend.session='session';backend.pending='unknown';backend._closed=False
    backend.defer_session_close=True;calls=[]
    def call(method,**kw):
        calls.append(method)
        if method=='cancel_skill':raise HarnessError('unknown_request')
        return {'outcome':'pending_review','hold_requested':True}
    backend.call=call
    result=backend.cancel()
    assert calls==['cancel_skill','finish_task']
    assert backend.pending=='unknown' and result['hold_requested']
    assert backend._closed
