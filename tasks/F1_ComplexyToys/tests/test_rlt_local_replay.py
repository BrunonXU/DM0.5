"""Synthetic clocks/publications: local terminals, partial chunks and masks."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_local_replay import build_transitions, OBJECTIVE


def fixture(assisted=False, outcome='success'):
    start = 1_000_000_000
    observations, controls = [], []
    for i in range(13):
        t = start+i*20_000_000
        human = assisted and i >= 6
        observations.append(dict(observation_id=i, available_monotonic_ns=t,
            capture_started_monotonic_ns=t, state16=[0.]*16, state_timestamps_ns=[t]*16,
            frames=[dict(host_received_monotonic_ns=t)]*3))
        controls.append(dict(monotonic_ns=t, started_monotonic_ns=t-1, completed_monotonic_ns=t,
            command_sequence=i+1, source='human_arm' if human else 'policy',
            execution_mode='powered', publication_complete=True, stage='completed',
            policy_epoch=2 if human else 1, intervention_id=int(human),
            submitted=[0.]*16, action_source_timestamps_ns=[t]*16,
            publications=[dict(topic_key=key, monotonic_ns=t, publisher_gid='owner')
                          for key in ('joint_command', 'right_gripper_command')],
            policy_sampling=dict(task_phase='placement', local_task=dict(segment_id='e:p', status='active'))))
    result = dict(objective=OBJECTIVE, episode_id='e', segment_id='e:p', started_ns=start,
                  ended_ns=start+240_000_001, terminal_observation=dict(observation_id=12,
                  available_monotonic_ns=start+240_000_000), actor_version='fixed', assisted=assisted,
                  status=outcome, reward=float(outcome == 'success'), terminated=True, physical_reset=False)
    return dict(observations=observations, control=controls), result


def test_partial_terminal_uses_real_duration_not_gamma16():
    streams, result = fixture()
    rows = build_transitions(streams, result)
    assert len(rows) == 1 and sum(rows[0]['action_time_mask']) == 8
    assert len(rows[0]['submitted16']) == 8
    assert rows[0]['discount'] == pytest.approx(.999**7.2)
    assert rows[0]['reward'] == 1 and rows[0]['terminated']


def test_human_boundary_keeps_both_sources_and_only_one_reward():
    streams, result = fixture(assisted=True)
    rows = build_transitions(streams, result)
    assert len(rows) == 2
    assert not any(rows[0]['intervene_flags'])
    assert any(rows[1]['intervene_flags'])
    assert sum(r['reward'] for r in rows) == 1
    assert not rows[0]['terminated'] and rows[1]['terminated']


@pytest.mark.parametrize('fault', ['home', 'unpublished', 'wrong_segment', 'gap', 'missing_gripper'])
def test_invalid_actual_action_never_enters_replay(fault):
    streams, result = fixture()
    row = streams['control'][5]
    if fault == 'home': row['source'] = 'home'
    if fault == 'unpublished': row['publication_complete'] = False
    if fault == 'wrong_segment': row['policy_sampling']['local_task']['segment_id'] = 'other'
    if fault == 'gap': row['command_sequence'] += 4
    if fault == 'missing_gripper': row['action_source_timestamps_ns'][15] = None
    with pytest.raises(ValueError): build_transitions(streams, result)


def test_default_failure_is_terminal_and_interruption_is_quarantined():
    streams, result = fixture(outcome='failure')
    row = build_transitions(streams, result)[-1]
    assert row['terminated'] and row['reward'] == 0
    result.update(status='interrupted', reward=None, terminated=False)
    with pytest.raises(ValueError, match='review'): build_transitions(streams, result)


def test_camera_discontinuity_splits_without_bridge_or_false_failure():
    streams, result = fixture()
    streams['observations'][6]['camera_discontinuity'] = True
    rows = build_transitions(streams, result)
    assert len(rows) == 2
    assert rows[0]['next_observation_id'] == 5 and rows[1]['observation_id'] == 7
    assert rows[0]['truncated'] and not rows[0]['terminated']
    assert rows[0]['reward'] == 0 and rows[1]['reward'] == 1
    assert rows[0]['next_decision_ns'] < rows[1]['decision_ns']


def test_missing_observations_split_but_controls_must_still_be_complete():
    streams, result = fixture()
    streams['observations'] = [r for r in streams['observations'] if r['observation_id'] not in (5, 6, 7)]
    rows = build_transitions(streams, result)
    assert [r['observation_id'] for r in rows] == [0, 8]
    assert rows[0]['next_observation_id'] == 4
    assert sum(r['terminated'] for r in rows) == 1
    assert all(sum(r['action_time_mask']) == len(r['submitted16']) for r in rows)
    streams['control'][6]['publication_complete'] = False
    with pytest.raises(ValueError, match='Unpublished'): build_transitions(streams, result)


def test_invalid_success_endpoint_stays_quarantined():
    streams, result = fixture()
    streams['observations'][-1]['camera_discontinuity'] = True
    with pytest.raises(ValueError, match='terminal'): build_transitions(streams, result)


@pytest.mark.parametrize('pause_source', ['input_pause_hold', 'rtc_timing_hold', 'ik_recovery_hold', 'hold', 'human_compute_wait'])
def test_predictive_pause_and_recovery_anchor_are_not_actor_or_expert_actions(pause_source):
    streams, result = fixture(assisted=True)
    for row in streams['control'][3:6]:
        row['source'] = pause_source
    streams['control'][6]['source'] = 'input_recovery_anchor'
    rows = build_transitions(streams, result)
    assert len(rows) == 2
    assert rows[0]['boundary_reason'] == 'protective_pause' and rows[0]['truncated']
    assert rows[0]['next_decision_ns'] < rows[1]['decision_ns']
    sequences = {s for row in rows for s in row['command_sequences']}
    assert not sequences.intersection({4,5,6,7})
    assert sum(row['reward'] for row in rows) == 1 and rows[-1]['terminated']
    assert any(rows[-1]['intervene_flags'])


def test_compute_wait_needs_real_publication_and_cannot_hide_an_unknown_source():
    streams, result = fixture(assisted=True)
    streams['control'][7]['source'] = 'human_compute_wait'
    streams['control'][7]['publication_complete'] = False
    with pytest.raises(ValueError, match="source='human_compute_wait', command_sequence=8"):
        build_transitions(streams, result)
    streams['control'][7]['publication_complete'] = True
    streams['control'][7]['source'] = 'unknown_wait'
    with pytest.raises(ValueError, match="source='unknown_wait'"):
        build_transitions(streams, result)


def test_compute_wait_at_terminal_cannot_fabricate_success_transition():
    streams, result = fixture(assisted=True)
    for row in streams['control'][7:]:
        row['source'] = 'human_compute_wait'
    with pytest.raises(ValueError, match='No valid local terminal'):
        build_transitions(streams, result)


def test_terminal_suffixes_preserve_real_capture_timing_and_single_endpoint():
    from f1_toys.rlt_local_replay import terminal_suffixes
    streams,result=fixture(assisted=True)
    for row in streams['observations'][1:]: row['capture_started_monotonic_ns']-=5_000_000
    rows=terminal_suffixes(streams,result)
    assert len(rows)>1 and all(r['terminated'] and r['reward']==1 for r in rows)
    assert len({r['next_observation_id'] for r in rows})==1
    assert len({r['decision_ns'] for r in rows})==len(rows)
    assert all(r['decision_ns']>=streams['control'][6]['monotonic_ns'] for r in rows)


def test_long_run_chunk_ends_at_real_observation_not_arbitrary_grid():
    streams, result = fixture()
    # 16 steps at 30 Hz would end at 533.333 ms; regular 20ms observations
    # instead cut at 520ms and preserve the measured duration.
    import copy
    template_o, template_c = streams['observations'][0], streams['control'][0]
    streams = dict(observations=[], control=[])
    start = result['started_ns']
    for i in range(36):
        t = start+i*20_000_000
        o=copy.deepcopy(template_o);o.update(observation_id=i, available_monotonic_ns=t,
            capture_started_monotonic_ns=t, state_timestamps_ns=[t]*16, frames=[dict(host_received_monotonic_ns=t)]*3)
        c=copy.deepcopy(template_c);c.update(monotonic_ns=t,started_monotonic_ns=t-1,completed_monotonic_ns=t,command_sequence=i+1,
            action_source_timestamps_ns=[t]*16)
        for pub in c['publications']:pub['monotonic_ns']=t
        streams['observations'].append(o);streams['control'].append(c)
    result.update(ended_ns=t+1,terminal_observation=dict(observation_id=35,available_monotonic_ns=t))
    rows=build_transitions(streams,result)
    assert rows[0]['next_decision_ns']==start+520_000_000
    assert rows[0]['elapsed_steps']==pytest.approx(15.6)
    assert rows[0]['next_decision_ns']==rows[1]['decision_ns']


def control_time_gap_fixture(*, assisted=False, outcome='success'):
    streams, result = fixture(assisted=assisted, outcome=outcome)
    # These publications were genuinely consecutive, but the controller waited
    # 60ms between them. Observations still arrived throughout the missing time.
    streams['control'] = [r for i, r in enumerate(streams['control']) if i not in (5, 6)]
    for i, row in enumerate(streams['control']):
        row['command_sequence'] = i+1
    return streams, result


@pytest.mark.parametrize('assisted', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_control_time_gap_preserves_both_sides_without_bridge_or_false_terminal(assisted, outcome):
    streams, result = control_time_gap_fixture(assisted=assisted, outcome=outcome)
    rows = build_transitions(streams, result)
    assert len(rows) == 2
    before, after = rows
    assert (before['observation_id'], before['next_observation_id']) == (0, 4)
    assert (after['observation_id'], after['next_observation_id']) == (7, 12)
    assert before['boundary_reason'] == 'control_quality_gap'
    assert before['truncated'] and not before['terminated'] and before['reward'] == 0
    assert before['discount'] == pytest.approx(.999**2.4)
    assert before['next_decision_ns'] < after['decision_ns']
    assert before['observation_run'] != after['observation_run']
    assert after['terminated'] and not after['truncated']
    assert after['reward'] == float(outcome == 'success')
    assert any(after['intervene_flags']) == assisted
    assert all(sum(r['action_time_mask']) == len(r['submitted16']) for r in rows)
    assert sum(r['elapsed_steps'] for r in rows) == pytest.approx(5.4)
    # The gap's 1.8 would-be steps are excluded, never padded into real actions.
    assert sum(r['elapsed_steps'] for r in rows) < 7.2


def test_control_gap_recovery_needs_observation_captured_after_resumed_publication():
    streams, result = control_time_gap_fixture()
    # Available after recovery but capture started inside the omitted interval.
    streams['observations'][7]['capture_started_monotonic_ns'] -= 10_000_000
    rows = build_transitions(streams, result)
    assert rows[0]['next_observation_id'] == 4
    assert rows[1]['observation_id'] == 8
    assert rows[-1]['terminated'] and rows[-1]['reward'] == 1


@pytest.mark.parametrize('gap_ns, expected_rows', [(50_000_000, 1), (51_458_000, 2)])
def test_control_gap_threshold_is_not_relaxed(gap_ns, expected_rows):
    streams, result = fixture()
    delta = gap_ns-20_000_000
    for row in streams['control'][6:]:
        for name in ('monotonic_ns', 'started_monotonic_ns', 'completed_monotonic_ns'):
            row[name] += delta
        row['action_source_timestamps_ns'] = [t+delta for t in row['action_source_timestamps_ns']]
        for publication in row['publications']:
            publication['monotonic_ns'] += delta
    rows = build_transitions(streams, result)
    assert len(rows) == expected_rows
    if expected_rows == 2:
        assert rows[0]['next_observation_id'] == 5
        assert rows[0]['boundary_reason'] == 'control_quality_gap'
        assert rows[1]['observation_id'] == 8
    assert sum(r['terminated'] for r in rows) == 1


def test_control_time_gap_and_sensor_cut_remain_separate_quality_boundaries():
    streams, result = control_time_gap_fixture()
    streams['observations'][10]['camera_discontinuity'] = True
    rows = build_transitions(streams, result)
    assert [(r['observation_id'], r['next_observation_id']) for r in rows] == [(0, 4), (7, 9), (11, 12)]
    assert [r['boundary_reason'] for r in rows] == ['control_quality_gap', 'observation_quality_gap', 'local_terminal']
    assert [r['truncated'] for r in rows] == [True, True, False]
    assert sum(r['reward'] for r in rows) == 1


def test_control_gap_cannot_invent_a_terminal_when_recovery_has_no_valid_pair():
    streams, result = fixture()
    streams['control'] = streams['control'][:5]+streams['control'][-1:]
    for i, row in enumerate(streams['control']):
        row['command_sequence'] = i+1
    with pytest.raises(ValueError, match='No valid local terminal'):
        build_transitions(streams, result)


def test_terminal_suffixes_do_not_rejoin_the_control_time_gap():
    from f1_toys.rlt_local_replay import terminal_suffixes
    streams, result = control_time_gap_fixture(assisted=True)
    rows = terminal_suffixes(streams, result)
    assert rows and all(r['terminated'] and r['reward'] == 1 for r in rows)
    assert all(r['decision_ns'] >= streams['observations'][7]['available_monotonic_ns'] for r in rows)
    assert {r['next_observation_id'] for r in rows} == {12}
