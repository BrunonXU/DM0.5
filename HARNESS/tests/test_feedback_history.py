"""Camera matching preserves measured joint samples and their acquisition stamps."""
import pytest

from f1_harness.contracts import HarnessError, Mode
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import OwnerBridge


@pytest.fixture
def history(tmp_path):
    now=[2_000_000_000]
    engine=TaskEngine(EvidenceStore(tmp_path),mode=Mode.FAKE,clock=lambda:now[0])
    bridge=OwnerBridge(engine,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=500_000_000,reference_tolerance_deg=.5,clock=lambda:now[0])
    yield bridge,now
    bridge.close()


def feedback(bridge,stamp,value):
    bridge.update_feedback(full22=(value,)*22,measured16=(value,)*16,stamps16=(stamp,)*16)


def test_nearest_history_is_real_sample_not_latest_or_interpolated(history):
    bridge,now=history
    feedback(bridge,now[0]-200_000_000,1.)
    feedback(bridge,now[0]-100_000_000,2.)
    feedback(bridge,now[0],3.)
    result=bridge.measured_feedback(target_received_ns=now[0]-180_000_000)
    assert result['measured16']==(1.,)*16
    assert result['full22']==(1.,)*22
    assert result['stamps16']==(now[0]-200_000_000,)*16
    assert result['matched_state_received_ns']==now[0]-200_000_000
    assert result['match_delta_ns']==20_000_000
    assert bridge.measured_feedback()['measured16']==(3.,)*16
    result['measured16']=(9.,)*16
    assert bridge.measured_feedback(target_received_ns=now[0]-180_000_000)['measured16']==(1.,)*16


def test_history_is_bounded_deduplicated_and_expires_without_new_updates(history):
    bridge,now=history
    for index in range(300):feedback(bridge,now[0]-300+index,float(index))
    assert len(bridge.feedback_history)==256
    feedback(bridge,now[0]-1,299.)
    assert len(bridge.feedback_history)==256
    now[0]+=1_100_000_000
    with pytest.raises(HarnessError,match='history_missing'):
        bridge.measured_feedback(target_received_ns=now[0])
    with pytest.raises(HarnessError,match='history_expired'):
        bridge.measured_feedback(target_received_ns=now[0]-1_100_000_000)
    feedback(bridge,now[0],300.)
    assert len(bridge.feedback_history)==1


@pytest.mark.parametrize('stamp',[True,-1,1.5,'2'])
def test_invalid_camera_timestamp_is_rejected(history,stamp):
    bridge,now=history
    feedback(bridge,now[0],1.)
    with pytest.raises(HarnessError,match='invalid_feedback_target_timestamp'):
        bridge.measured_feedback(target_received_ns=stamp)


def test_missing_joint_timestamps_do_not_enter_camera_history(history):
    bridge,now=history
    bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(None,)*16)
    with pytest.raises(HarnessError,match='history_missing'):
        bridge.measured_feedback(target_received_ns=now[0])
