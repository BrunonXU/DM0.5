"""Schema 4 must never turn missing-hardware placeholders into robot output."""
import json

import pytest

from test_motion_replay import source, replay_setup, gateway_source
from test_data_auth import http_app
from test_ros_adapter import joints
from test_pose_action import live_feedback_event
from robot.replay_data import read_recording, LEFT_ABSENT_MASK, LEFT_ABSENT_HARDWARE, KEYS
from robot.replay_action import prepare_schedule, review, live_check
from replay_service import ReplayService
from robot.adapter import ActionRejected


def absent_left(manifest, streams):
    manifest['schema_version'] = 4
    manifest['binding'].update(hardware_capabilities=dict(LEFT_ABSENT_HARDWARE),
                               state_valid_mask=list(LEFT_ABSENT_MASK))
    for rows in streams.values():
        for row in rows:
            row['schema_version'] = 4
    for row in streams['control']:
        row.update(state_valid_mask=list(LEFT_ABSENT_MASK), action_valid_mask=list(LEFT_ABSENT_MASK))
        row['submitted'][14] = 0
        row['action_source_timestamps_ns'][14] = None
        row['publications'] = [p for p in row['publications'] if p['topic_key'] != KEYS[1]]
    for row, command in zip(streams['observations'], streams['control']):
        row.update(state_valid_mask=list(LEFT_ABSENT_MASK),
                   absent_state_semantics='zero_storage_placeholder_not_measurement',
                   state16=command['submitted'][:],
                   state_timestamps_ns=[row['available_monotonic_ns']]*14+[0,row['available_monotonic_ns']])


@pytest.mark.parametrize('cropped', [False, True])
def test_schema4_review_alignment_execution_and_report_without_left_feedback(tmp_path, cropped):
    a, clock, transport, _, spec = replay_setup(tmp_path)
    rec = read_recording(source(tmp_path/'schema4', mutate=absent_left))
    segment = rec.select(.01, .25) if cropped else rec.select()
    spec['_segment'] = segment
    a.store.grippers.pop('left')
    assert review(a, segment)
    assert not transport.prepared and not transport.sent
    pose = ReplayService.alignment(segment)
    assert pose['grippers'] == {'right': 10.}
    from robot.pose_review import review as pose_review
    assert pose_review(a, pose)['executable']
    schedule = prepare_schedule(segment)
    assert not any(key == KEYS[1] for _, key, _, _ in schedule)
    result = a.execute(spec, live_feedback_event(a, clock))
    assert result['status'] == 'succeeded' and result['feedback_verified']
    assert set(transport.released) == {KEYS[0], KEYS[2]}
    assert not any(key == KEYS[1] for key, _ in transport.sent)
    assert [v for key, v in transport.sent if key == KEYS[2]] == [10., 30.]
    report = json.loads((tmp_path/'result.json').read_text())
    assert all(s['actual'][14] is None and s['feedback_times_s'][14] is None for s in report['samples'])
    left = report['comparison']['channels'][14]
    assert left['hardware_present'] is False and left['matched'] == 0 and left['metrics'] is None
    assert left['crossings_50_percent']['comparable'] is False
    assert 'left' not in result['final_feedback']['grippers']


@pytest.mark.parametrize('mutation', [
    lambda m,s: m.update(schema_version=5),
    lambda m,s: m.update(schema_version=3),
    lambda m,s: m['binding'].pop('hardware_capabilities'),
    lambda m,s: m['binding']['hardware_capabilities'].update(left_gripper_present=True),
    lambda m,s: m['binding']['hardware_capabilities'].update(right_gripper_present=1),
    lambda m,s: m['binding']['state_valid_mask'].__setitem__(14, True),
    lambda m,s: s['control'][1]['action_valid_mask'].__setitem__(0, 1),
    lambda m,s: s['control'][1]['state_valid_mask'].__setitem__(14, True),
    lambda m,s: s['control'][1]['submitted'].__setitem__(14, 1),
    lambda m,s: s['control'][1]['action_source_timestamps_ns'].__setitem__(14, 0),
    lambda m,s: s['control'][1]['publications'].append(dict(topic_key=KEYS[1], monotonic_ns=1010001000, publisher_gid='02'*16)),
    lambda m,s: s['control'][1].update(publication_complete=False),
    lambda m,s: s['control'][1].update(schema_version=3),
    lambda m,s: s['observations'][1]['state16'].__setitem__(14, 10),
    lambda m,s: s['observations'][1]['state_timestamps_ns'].__setitem__(14, 100),
    lambda m,s: s['observations'][1].pop('state_valid_mask'),
    lambda m,s: s['observations'][1].pop('absent_state_semantics'),
    lambda m,s: m.update(complete=False),
    lambda m,s: m['binding'].update(execution_mode='shadow'),
])
def test_schema4_invalid_provenance_rejected(tmp_path, mutation):
    def alter(m, s):
        absent_left(m, s)
        mutation(m, s)
    with pytest.raises(ValueError):
        read_recording(source(tmp_path/'bad', mutate=alter))


def test_schema4_still_requires_right_feedback(tmp_path):
    a, _, transport, _, _ = replay_setup(tmp_path)
    segment = read_recording(source(tmp_path/'schema4', mutate=absent_left)).select()
    a.store.grippers.pop('right')
    with pytest.raises(ActionRejected):
        live_check(a, segment)
    assert not transport.sent and not transport.prepared


def test_schema3_does_not_guess_absent_hardware(tmp_path):
    a, _, transport, segment, _ = replay_setup(tmp_path)
    a.store.grippers.pop('left')
    with pytest.raises(ActionRejected):
        live_check(a, segment)
    assert not transport.sent


def test_schema4_http_force_start_is_available_for_arm_offset(http_app, monkeypatch, tmp_path):
    app, request = http_app
    a, _, transport = gateway_source(app, monkeypatch, tmp_path)
    source(tmp_path/'recording', mutate=absent_left)
    a.store.grippers.pop('left')
    a.store.update_joints(joints(['arm_l_j4'], [10]))
    _, _, cookie = request('/api/session')
    status, data, _ = request('/api/replay/review?episode_id='+'e'*24, cookie=cookie)
    checked = data['review']
    assert status == 200 and checked['can_force_start'] and not checked['executable']
    assert checked['initial_grippers'] == {'right': 10.}
    assert [g['side'] for g in checked['start_deviations']['grippers']] == ['right']
    assert not transport.prepared and not transport.sent and app.lease.fd is None
    _, auth, _ = request('/api/control/acquire', {}, cookie)
    body = dict(review_id=checked['review_id'], lease_id=auth['lease_id'], confirm=True)
    assert request('/api/replay/start', body, cookie)[0] == 409
    calls = []
    monkeypatch.setattr(a, 'execute', lambda spec,cancel,progress: calls.append(spec) or dict(status='cancelled'))
    assert request('/api/replay/start', {**body, 'force_start': True}, cookie)[0] == 200
    app.actions.thread.join(1)
    assert len(calls) == 1 and calls[0]['_force_start'] is True
    assert not transport.sent
