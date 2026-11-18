"""Support candidate failures remain actionable numerical preview results."""
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import numerical_failure_detail
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('reason',[
    'support_release_attachment_unresolved','support_release_contact_unavailable',
    'support_release_object_not_stationary','support_motion_requires_attachment_and_support',
])
def test_supported_candidate_failure_keeps_exact_reason(reason,tmp_path):
    assert numerical_failure_detail(HarnessError(reason))['code']==reason
    engine,bridge,clock,state,_,_=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None;engine.attempted_motion.clear()
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    def rpc_reject(*args):raise RuntimeError(reason)
    bridge.preview_planner=rpc_reject
    try:
        result=bridge.preview_skill(intent.model_dump())
        assert result['planning_diagnostic']['code']==reason
        assert not result['planned'] and result['no_motion'] and result['current_scope']
        assert not engine.pending and not engine.running and not engine.request_publications
    finally:bridge.close()


def test_unknown_support_server_fault_still_propagates(tmp_path):
    engine,bridge,clock,state,_,_=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None;engine.attempted_motion.clear()
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    def failed(*args):raise RuntimeError('support_service_connection_lost')
    bridge.preview_planner=failed
    try:
        with pytest.raises(RuntimeError,match='connection_lost'):bridge.preview_skill(intent.model_dump())
    finally:bridge.close()
