import pytest
from f1_harness.evidence_pack import motion_evidence_request


@pytest.mark.parametrize('broken',[None,'missing_owner_receipt','changed_epoch','wrong_parent','wrong_time'])
def test_recheck_needs_owner_lineage_to_use_original_waypoint(broken):
    original=dict(skill='approach_grasp',task_hash='task',session_id='s',owner_instance_id='owner',epoch=1)
    accepted={'move':original,'check':dict(original,verification_request_id='move')}
    rows=[dict(kind='harness_verification_continued',request_id='check',motion_request_id='move',
               skill='approach_grasp',epoch=1,task_hash='task',after_ns=100,motion_replayed=False),
          dict(kind='harness_verification_escalated',request_id='move',after_ns=100,motion_replayed=False)]
    if broken=='missing_owner_receipt':rows=[]
    if broken=='changed_epoch':accepted['check']['epoch']=2
    if broken=='wrong_parent':accepted['check']['verification_request_id']='unknown'
    if broken=='wrong_time':rows[1]['after_ns']=101
    assert motion_evidence_request('check',accepted,rows)==('move' if broken is None else None)
