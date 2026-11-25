import json
from f1_harness.timing_report import timing_report


def test_transport_retry_retains_original_solve_and_counts_only_actual_publication():
    from f1_harness.timing_report import _transport_timing
    scope=dict(task_hash='task',session_id='s',owner_instance_id='owner',epoch=1)
    accepted={r:dict(intent=dict(request_id=r,skill=s,**scope)) for r,s in
              [('grip','verify_grasp'),('first','transfer_above_hole'),('second','transfer_above_hole')]}
    rows=[dict(kind='skill_result',monotonic_ns=10_000_000_000,
               result=dict(request_id='grip',outcome='success',evidence_ids=['can'])),
          dict(kind='harness_planning_started',request_id='first',monotonic_ns=11_000_000_000),
          dict(kind='harness_plan_waiting_fresh_revalidation',request_id='first',monotonic_ns=13_000_000_000,
               candidate=dict(candidate_variant=2)),
          dict(kind='harness_revalidation_started',request_id='first'),
          dict(kind='harness_plan_revalidated',proof=dict(request_id='first'))]
    before=_transport_timing(rows,accepted)
    assert before['requests'][0]['solves'][0]['seconds']==2
    assert before['requests'][0]['solve_count']==before['requests'][0]['revalidated_count']==1
    assert before['verified_grasp_to_first_transfer_publication_s'] is None
    rows.append(dict(kind='harness_skill_first_publication',request_id='second',published_ns=18_000_000_000))
    report=_transport_timing(rows,accepted)
    assert report['first_published_request']=='second' and report['verified_grasp_to_first_transfer_publication_s']==8
    accepted['second']['intent']['epoch']=2
    assert _transport_timing(rows,accepted)['verified_grasp_to_first_transfer_publication_s'] is None


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


def test_budget_report_uses_owner_intervals_and_charges_model_wait_to_request_start():
    from f1_harness.timing_report import _budget_report
    sec = 10**9
    owner = [dict(kind='harness_skill_first_publication', published_ns=2*sec)]
    for end, skill in [(7,'approach_grasp'), (8,'grasp'), (10,'verify_grasp'),
                       (17,'transfer_above_hole'), (20,'align'), (21,'insert'),
                       (22,'release'), (23,'verify_placed'), (40,'withdraw_reset')]:
        owner.append(dict(kind='skill_result', skill=skill, monotonic_ns=end*sec,
                          result={'outcome': 'success'}))
    planner = [dict(kind='model_request_started', request_id='first', monotonic_ns=100*sec),
               dict(kind='model_transport_error', request_id='first', automatic_retry=True, elapsed_s=0.25)]
    for event in owner:
        stamp = event.get('monotonic_ns', event.get('published_ns'))
        planner.append(dict(kind='owner_event', record=event, monotonic_ns=100*sec+stamp))
        if event is owner[0]:
            planner.append(dict(kind='model_response', request_id='first', elapsed_s=3))
        if event.get('skill') == 'verify_grasp':
            planner.extend([dict(kind='model_request_started', request_id='second', monotonic_ns=110*sec),
                            dict(kind='model_rejected', request_id='second', monotonic_ns=111*sec)])
    planner.append(dict(kind='model_request_started', request_id='last', monotonic_ns=141*sec))
    report = _budget_report(owner, planner, 100*sec, True)
    budget = report['stage_budget_s']
    assert list(budget.values()) == [2.5, 4.0, 0.8, 0.7, 5.0, 2.5, 2.5]
    stages = report['stage_timing']
    assert [stages[k]['actual_s'] for k in budget] == [2, 5, 1, 2, 7, 3, 3]
    assert stages['start_to_first_publication']['over_budget_s'] == 0
    assert stages['transport'] == dict(actual_s=7, over_budget_s=2, model_calls=1, model_wait_s=1)
    assert stages['start_to_first_publication']['model_calls'] == 1
    assert stages['start_to_first_publication']['model_wait_s'] == 3
    assert report['placement_target'] == dict(allocated_s=18, target_s=20, actual_s=23, over_budget_s=3)
    assert report['model_timing'] == dict(model_calls=3, model_wait_s=None)
    assert report['model_timing_after_placement'] == dict(model_calls=1, model_wait_s=None)


def test_missing_or_unbound_trace_keeps_budget_measurements_unknown(tmp_path):
    report = timing_report(tmp_path)
    assert all(row == dict(actual_s=None, over_budget_s=None, model_calls=None, model_wait_s=None)
               for row in report['stage_timing'].values())
    trace = tmp_path/'planner/trace.jsonl'
    trace.parent.mkdir()
    trace.write_text(json.dumps(dict(kind='model_response', monotonic_ns=1)))
    report = timing_report(tmp_path)
    assert report['model_timing'] == dict(model_calls=None, model_wait_s=None)
    assert report['model_response_s'] is None
