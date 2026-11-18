from copy import deepcopy
import json
from pathlib import Path
import pytest
from f1_harness.tracking_diagnostics import tracking_diagnostics, record_tracking_diagnostics


def case():
    intent = dict(request_id='grasp', epoch=3, task_hash='task', skill='grasp')
    q = [0.] * 16;q[7] = 10.;q[15] = 18.
    plan = dict(request_id='grasp', epoch=3, targets=[q])
    trace = [dict(kind='skill_accepted', monotonic_ns=1_000_000_000, intent=intent),
        dict(kind='harness_plan_waiting_fresh_revalidation', request_id='grasp',
             monotonic_ns=1_100_000_000, candidate=plan),
        dict(kind='harness_motion_checkpoint', request_id='grasp', epoch=3, task_hash='task',
             monotonic_ns=3_000_000_000, index=0)]
    controls = [dict(monotonic_ns=t, source='harness_skill', policy_sampling=intent,
                     publication_complete=True, submitted=q,
                     publications=[dict(topic_key='joint_command')],
                     limit_control=dict(remaining_s=0.)) for t in (2_100_000_000, 2_800_000_000)]
    measured = q.copy();measured[7] = 9.75
    observations = [dict(state16=measured, state_valid_mask=[True]*16, state_timestamps_ns=[t]*16)
                    for t in (2_200_000_000, 2_900_000_000)]
    return trace, controls, observations


def first(data):
    return tracking_diagnostics(*data)['checkpoints'][0]


def test_stable_residual_is_diagnostic_not_grasp_or_force_evidence():
    data = case();original = deepcopy(data)
    item = first(data)
    assert item['status'] == 'measured_tracking_facts'
    assert item['target_minus_last_submitted_deg'] == [0.]*7
    assert item['target_minus_last_encoder_deg'] == [.25]+[0.]*6
    assert item['measured_right_range_deg'] == [0.]*7
    assert item['encoder_span_s'] == .7
    assert item['published_gripper_values'] == []  # Carried target != gripper publication.
    assert not item['control_changed'] and not item['force_estimated']
    assert item['physical_cause'] == 'unknown' and data == original


def test_partial_publication_and_gripper_topic_are_distinguished():
    trace, controls, observations = case()
    partial = deepcopy(controls[-1]);partial['publication_complete'] = False
    partial['monotonic_ns'] += 1
    partial['submitted'][15] = 100.
    partial['publications'] = [dict(topic_key='right_gripper_command')]
    controls.append(partial)
    item = first((trace, controls, observations))
    assert item['incomplete_publication_rows'] == 1
    assert item['published_gripper_values'] == []
    controls[0]['publications'].append(dict(topic_key='right_gripper_command'))
    assert first((trace, controls, observations))['published_gripper_values'] == [18.]


@pytest.mark.parametrize('field,value', [('request_id','other'), ('epoch',4), ('task_hash','other')])
def test_other_control_scope_does_not_supply_publication(field, value):
    trace, controls, observations = case()
    for row in controls:
        row['policy_sampling'] = dict(row['policy_sampling'], **{field:value})
    assert first((trace, controls, observations))['status'] == 'joint_publication_missing'


@pytest.mark.parametrize('field,value', [('epoch',4), ('task_hash','other')])
def test_checkpoint_scope_must_match_accepted_request(field, value):
    data = case();data[0][-1][field] = value
    assert first(data)['status'] == 'checkpoint_plan_scope_mismatch'


def test_missing_evidence_remains_explicit():
    trace, controls, observations = case()
    assert first((trace, [], observations))['status'] == 'joint_publication_missing'
    assert first((trace, controls, []))['status'] == 'insufficient_or_conflicting_encoder_evidence'
    assert first((trace[1:], controls, observations))['status'] == 'request_plan_or_acceptance_missing'


def test_repeated_encoder_stamp_is_one_sample_not_stationarity():
    trace, controls, observations = case()
    observations = [observations[0]]*100
    item = first((trace, controls, observations))
    assert item['unique_right_encoder_samples'] == 1
    assert item['status'] == 'insufficient_or_conflicting_encoder_evidence'
    assert 'measured_right_range_deg' not in item


def test_conflicting_encoder_stamp_is_not_averaged_or_counted_as_motion():
    trace, controls, observations = case()
    contradictory = deepcopy(observations[0]);contradictory['state16'][7] += 1
    observations.append(contradictory)
    item = first((trace, controls, observations))
    assert item['conflicting_encoder_timestamps'] == 1
    assert item['status'] == 'insufficient_or_conflicting_encoder_evidence'


@pytest.mark.parametrize('change', ['invalid_mask', 'mixed_timestamps', 'nonfinite', 'after_checkpoint'])
def test_invalid_or_post_checkpoint_encoder_is_excluded(change):
    trace, controls, observations = case();observations = deepcopy(observations)
    for row in observations:
        if change == 'invalid_mask':row['state_valid_mask'][9] = False
        elif change == 'mixed_timestamps':row['state_timestamps_ns'][9] += 1
        elif change == 'nonfinite':row['state16'][7] = float('nan')
        else:row['state_timestamps_ns'] = [4_000_000_000]*16
    item = first((trace, controls, observations))
    assert item['unique_right_encoder_samples'] == 0


def write_run(root, data):
    run = root/'run'
    for rel, rows in zip(('harness/trace.jsonl','journal/control.jsonl','journal/observations.jsonl'), data):
        path = run/'collected/owner'/rel;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('\n'.join(map(json.dumps,rows)))
    return run


def test_record_is_content_bound_idempotent_and_does_not_mutate_source(tmp_path):
    run = write_run(tmp_path,case());memory = tmp_path/'memory'
    old = {p:p.read_bytes() for p in run.rglob('*.jsonl')}
    result = record_tracking_diagnostics(run,memory)
    assert record_tracking_diagnostics(run,memory) == result
    assert {p:p.read_bytes() for p in old} == old
    report = json.loads(Path(result['path']).read_text())
    assert all(report['sources'].values()) and not report['executable']
    assert not (memory/'corpus').exists()
    path = run/'collected/owner/journal/observations.jsonl'
    path.write_text('')
    updated = record_tracking_diagnostics(run,memory)
    assert updated['id'] != result['id'] and Path(result['path']).exists()


def test_run_review_adds_facts_without_changing_existing_review(tmp_path):
    from f1_harness.run_review import summarize_run
    data = case();run = write_run(tmp_path,data);memory = tmp_path/'memory'
    value = summarize_run(run,memory)
    assert value['tracking_diagnostics']['checkpoint_count'] == 1
    review = run/'experience-review.json';original = review.read_bytes()
    (run/'collected/owner/journal/control.jsonl').write_text('')
    assert summarize_run(run,memory) == value
    assert review.read_bytes() == original
    assert len(list((memory/'tracking').glob('*.json'))) == 2
