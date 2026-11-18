import json
from f1_harness.timing_report import timing_report


def test_separate_clocks_and_cancelled_wait_not_complete_cycle(tmp_path):
    path=tmp_path/'collected/owner/harness/trace.jsonl';path.parent.mkdir(parents=True)
    owner=[dict(kind='skill_accepted',monotonic_ns=1000000000,intent={'request_id':'r','skill':'approach_grasp'}),
        dict(kind='harness_skill_first_publication',request_id='r',published_ns=2000000000),
        dict(kind='harness_measured_waypoint',request_id='r',measured_stamp_ns=3000000000),
        dict(kind='harness_verification_wait',request_id='r',after_ns=3000000000),
        dict(kind='authority_revoked',monotonic_ns=8000000000)]
    path.write_text('\n'.join(map(json.dumps,owner)))
    plan=tmp_path/'planner/trace.jsonl';plan.parent.mkdir()
    plan.write_text(json.dumps(dict(kind='owner_event',monotonic_ns=1009000000000,record=owner[1])))
    (tmp_path/'workspace-run.json').write_text(json.dumps({'started_ns':1000000000000}))
    value=timing_report(tmp_path)
    assert value['click_to_first_publication_receipt_s']==9
    step=value['owner_steps'][0]
    assert step['accepted_to_first_publication_s']==1
    assert step['missing_evidence_wait_s']==5
    assert value['full_grasp_place_reset_s'] is None


def test_complete_cycle_timing_needs_placement_return_and_same_uninterrupted_scope():
    from copy import deepcopy
    from f1_harness.timing_report import measured_cycle
    skills=['approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole',
            'align','insert','release','verify_placed','withdraw_reset']
    rows=[];accepted={}
    for i,skill in enumerate(skills):
        r=str(i);start=(i*2+1)*1_000_000_000
        accepted[r]=dict(kind='skill_accepted',monotonic_ns=start,intent=dict(request_id=r,skill=skill,
            task_hash='task',session_id='s',owner_instance_id='owner',epoch=1))
        rows.extend([accepted[r],dict(kind='harness_skill_first_publication',request_id=r,published_ns=start),
            dict(kind='harness_measured_waypoint',request_id=r,measured_stamp_ns=start+500_000_000),
            dict(kind='skill_result',skill=skill,monotonic_ns=start+800_000_000,
                 result=dict(request_id=r,outcome='success',synthetic=False,evidence_ids=['evidence'],assisted=False))])
    cycle=measured_cycle(rows,accepted)
    assert cycle['motion_s']==18.5 and cycle['verified_s']==18.8
    for broken in ('missing_reset','different_epoch','stop','missing_arrival','unverified_placement'):
        data=deepcopy(rows);scope=deepcopy(accepted)
        if broken=='missing_reset':data=[r for r in data if r.get('skill')!='withdraw_reset']
        if broken=='different_epoch':scope['9']['intent']['epoch']=2
        if broken=='stop':data.append(dict(kind='authority_revoked',monotonic_ns=10_000_000_000))
        if broken=='missing_arrival':data=[r for r in data if not (r.get('kind')=='harness_measured_waypoint' and r['request_id']=='9')]
        if broken=='unverified_placement':data=[r for r in data if r.get('skill')!='verify_placed']
        assert measured_cycle(data,scope) is None,broken
