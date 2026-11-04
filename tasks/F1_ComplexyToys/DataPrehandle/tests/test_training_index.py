"""Behavioral tests for independent observation anchors and published actions."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'build_training_index.py'
spec = importlib.util.spec_from_file_location('f1_task_training_index', SCRIPT)
index = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = index
spec.loader.exec_module(index)
T0 = 1_000_000_000
MS = 1_000_000


def records(duration_ms=2000):
    controls = []
    for i, elapsed in enumerate(range(-20, duration_ms + 1, 5)):
        t = T0 + elapsed * MS
        controls.append(dict(episode_id='fixture', monotonic_ns=t, hil_state='hil_latched',
            intervention_id=1, policy_epoch=2, source='human_arm', execution_mode='powered',
            publication_complete=True, submitted=[float(i)] * 14 + [0., 100.],
            action_source_timestamps_ns=[t - 1] * 14 + [T0 - 30 * MS] * 2,
            command_sequence=i + 1, stream_sequence=i))
    observations, payload = [], []
    for i, elapsed in enumerate(range(0, duration_ms + 1, 20)):
        t = T0 + elapsed * MS
        frames = [dict(camera=c, frame_id=i, host_received_monotonic_ns=t - 10 * MS,
                       timestamp={'value': t - 10 * MS, 'unit': 'ns', 'clock_domain': 'unverified'})
                  for c in ('head', 'left_wrist', 'right_wrist')]
        observations.append(dict(episode_id='fixture', available_monotonic_ns=t, observation_id=i,
            capture_started_monotonic_ns=t - 10 * MS, hil_state='hil_latched', intervention_id=1,
            policy_epoch=2, state16=[0.] * 16, state_timestamps_ns=[t - 1] * 16,
            frames=frames, camera_discontinuity=False))
        payload.append(dict(observation_id=i, frames=deepcopy(frames)))
    events = [dict(episode_id='fixture', event='demonstration_started', monotonic_ns=T0 - 20 * MS),
              dict(episode_id='fixture', event='demonstration_stopped', monotonic_ns=T0 + duration_ms * MS)]
    return observations, controls, payload, events


def select(data, config=None, **kwargs):
    return index.select_windows(*data, episode_id='fixture',
        config=config or index.Config(horizon=20), accepted_interventions=[1], **kwargs)


def test_removed_left_gripper_mask_keeps_real_right_expert_actions():
    data = records()
    mask = [True]*14+[False, True]
    for row in data[0]:
        row['state_valid_mask'] = mask.copy()
        row['state_timestamps_ns'][14] = None
    for row in data[1]:
        row['action_valid_mask'] = mask.copy()
        row['action_source_timestamps_ns'][14] = None
    rows, _, _ = select(data)
    assert rows and all(row['state_valid_mask'] == mask for row in rows)
    assert all(row['state_ages_ns'][14] is None for row in rows)
    assert all(action['action_valid_mask'] == mask for row in rows for action in row['action_window'])
    for row in data[0]:
        row['state_timestamps_ns'][15] = None
    assert not select(data)[0]


def test_future_camera_gap_does_not_destroy_good_anchor_action_window():
    data = list(records())
    data[0] = [r for r in data[0] if not T0 + 100 * MS <= r['available_monotonic_ns'] < T0 + 600 * MS]
    data[0][5]['camera_discontinuity'] = True
    original = deepcopy(data)
    rows, rejects, report = select(data)
    first = next(r for r in rows if r['grid_index'] == 0)
    assert len(first['action_window']) == 20
    assert first['action_window'][-1]['grid_monotonic_ns'] > T0 + 600 * MS
    assert any(r['reason'] == 'observation_stale' for r in rejects)
    assert any(r['camera_discontinuity'] for r in rows)
    assert data == original  # Neither original metadata nor action vectors mutated.
    assert report['grid_points'] == len(rows) + len(rejects)


@pytest.mark.parametrize('change,reason', [
    ({'publication_complete': False, 'submitted': None}, 'control_publication_incomplete'),
    ({'source': 'home'}, 'control_not_human_source'),
    ({'source': 'policy'}, 'control_not_human_source'),
    ({'policy_epoch': 3}, 'control_epoch_changed'),
    ({'intervention_id': 2}, 'control_intervention_not_selected'),
    ({'execution_mode': 'shadow'}, 'control_not_powered'),
    ({'hil_state': 'handoff_hold'}, 'control_not_hil_latched'),
])
def test_even_one_invalid_control_between_grid_points_blocks_window(change, reason):
    data = records()
    next(c for c in data[1] if c['monotonic_ns'] == T0 + 115 * MS).update(change)
    rows, rejects, _ = select(data)
    assert not any(r['grid_index'] == 0 for r in rows)
    assert next(r for r in rejects if r['grid_index'] == 0)['reason'] == reason
    assert any(r['anchor_monotonic_ns'] > T0 + 200 * MS for r in rows)


def test_control_gap_detected_even_when_all_target_grid_commands_are_fresh():
    data = list(records())
    # At 20 Hz, 100 ms and 200 ms commands are exact; 150 ms sees 45 ms age.
    # The 105 -> 160 ms hole must still block the window independently.
    data[1] = [c for c in data[1] if not T0 + 110 * MS <= c['monotonic_ns'] <= T0 + 155 * MS]
    rows, rejects, _ = select(data, index.Config(fps=20, horizon=10))
    assert not any(r['grid_index'] == 0 for r in rows)
    assert next(r for r in rejects if r['grid_index'] == 0)['reason'] == 'control_gap'


def test_home_event_blocks_window_even_if_controls_are_mislabeled_human():
    data = records()
    data[3].extend([dict(episode_id='fixture', event='home_started', monotonic_ns=T0 + 115 * MS),
                   dict(episode_id='fixture', event='home_completed_feedback', monotonic_ns=T0 + 250 * MS)])
    rows, rejects, _ = select(data)
    assert next(r for r in rejects if r['grid_index'] == 0)['reason'] == 'home_interval'
    assert all(r['action_window'][-1]['grid_monotonic_ns'] < T0 + 115 * MS
               or r['capture_started_monotonic_ns'] > T0 + 250 * MS for r in rows)


def test_tail_is_rejected_not_padded_and_no_grid_compression():
    rows, rejects, summary = select(records(duration_ms=1000), index.Config(fps=20, horizon=10))
    assert rows[-1]['anchor_monotonic_ns'] == T0 + 550 * MS
    assert rows[-1]['action_window'][-1]['grid_monotonic_ns'] == T0 + 1000 * MS
    assert all(not any(row['action_is_pad']) for row in rows)
    assert summary['rejection_counts']['labels:incomplete_future_window'] == 9
    assert rejects[-1]['anchor_monotonic_ns'] == T0 + 1000 * MS


def test_partial_tail_and_bad_command_keep_only_real_contiguous_prefix():
    cfg = index.Config(fps=20, horizon=10, allow_partial_windows=True)
    data = records(duration_ms=1000)
    next(c for c in data[1] if c['monotonic_ns'] == T0 + 115 * MS)['source'] = 'policy'
    rows, _, _ = select(data, cfg)
    first = rows[0]
    assert first['valid_action_steps'] == 3  # 0, 50, 100 ms; never splice past 115.
    assert first['action_is_pad'] == [False]*3 + [True]*7
    assert first['action_window'][-1]['grid_monotonic_ns'] == T0 + 100*MS
    assert rows[-1]['valid_action_steps'] == 1
    assert rows[-1]['anchor_monotonic_ns'] == T0 + 1000*MS


def test_home_requires_exact_completed_v1_allowlist_and_keeps_transition():
    data = records()
    start, end = T0 + 115*MS, T0 + 250*MS
    data[3].extend([dict(episode_id='fixture', event='home_started', preset='积木demo-V1', monotonic_ns=start),
                   dict(episode_id='fixture', event='home_completed_feedback', monotonic_ns=end)])
    for c in data[1]:
        if start <= c['monotonic_ns'] <= end:
            c['source'] = 'home'
    rows, _, _ = select(data, accepted_home_intervals_ns=[(start, end)])
    assert rows[0]['contains_home'] and rows[0]['supervision_phase'] == 'human'
    assert any(r['supervision_phase'] == 'home' for r in rows)
    assert any(a['source'] == 'home' for a in rows[0]['action_window'])
    for bad in ('home_cancelled', 'home_stopped'):
        changed = deepcopy(data)
        changed[3][-1]['event'] = bad
        with pytest.raises(ValueError):
            select(changed, accepted_home_intervals_ns=[(start, end)])
    data[3][-2]['preset'] = '积木demo-V0'
    with pytest.raises(ValueError):
        select(data, accepted_home_intervals_ns=[(start, end)])


def test_home_allowlist_never_bypasses_epoch_or_publication_boundaries():
    for change in ({'policy_epoch': 3}, {'publication_complete': False}):
        data = records()
        start, end = T0 + 115*MS, T0 + 250*MS
        data[3].extend([dict(episode_id='fixture', event='home_started', preset='积木demo-V1', monotonic_ns=start),
                       dict(episode_id='fixture', event='home_completed_feedback', monotonic_ns=end)])
        next(c for c in data[1] if c['monotonic_ns'] == start).update(source='home', **change)
        rows, _, _ = select(data, accepted_home_intervals_ns=[(start, end)])
        assert not any(r['grid_index'] == 0 for r in rows)


def test_fps_changes_do_not_change_quality_thresholds_or_600ms_stale_anchor():
    data = list(records())
    data[0] = [r for r in data[0] if not T0 + 540 * MS <= r['available_monotonic_ns'] < T0 + 700 * MS]
    for fps, step in [(20, 12), (30, 18)]:
        cfg = index.Config(fps=fps, horizon=3)
        assert cfg.max_observation_age_ns == cfg.max_control_gap_ns == 50 * MS
        _, rejects, _ = select(data, cfg)
        rejected = next(r for r in rejects if r['grid_index'] == step)
        assert rejected['anchor_monotonic_ns'] == T0 + 600 * MS
        assert rejected['reason'] == 'observation_stale'


def test_only_required_cameras_gate_anchor_but_all_provenance_is_preserved():
    data = records()
    for row in data[2]:
        row['frames'][1]['host_received_monotonic_ns'] -= 300 * MS
    rows, _, _ = select(data)
    assert rows[0]['image_mask'] == {'head': True, 'left_wrist': False, 'right_wrist': True}
    assert rows[0]['camera_ages_ns']['left_wrist'] > 200 * MS
    _, rejects, _ = select(data, index.Config(required_cameras=('head', 'left_wrist', 'right_wrist')))
    assert all(r['reason'] == 'required_camera_stale_or_future' for r in rejects)


def test_pre_start_or_wrong_capture_context_cannot_label_post_start_image():
    data = records()
    data[3][0]['monotonic_ns'] = T0 - 5 * MS
    _, rejects, _ = select(data)
    assert rejects[0]['grid_index'] == 0
    assert rejects[0]['reason'] == 'observation_outside_episode_or_noncausal'
    data = records()
    next(c for c in data[1] if c['monotonic_ns'] == T0 - 10 * MS)['source'] = 'hold'
    _, rejects, _ = select(data)
    assert rejects[0]['reason'] == 'control_not_human_source'


def test_review_ranges_require_whole_capture_to_future_action_interval():
    rows, rejects, _ = select(records(), index.Config(fps=20, horizon=10),
        accepted_time_ranges_ns=[[T0 + 200 * MS, T0 + 1000 * MS]])
    assert rows[0]['capture_started_monotonic_ns'] >= T0 + 200 * MS
    assert all(r['action_window'][-1]['grid_monotonic_ns'] <= T0 + 1000 * MS for r in rows)
    assert any(r['reason'] == 'outside_single_reviewed_time_range' for r in rejects)


def test_source_episode_mismatch_and_config_typo_fail_closed(tmp_path):
    data = records()
    data[1][10]['episode_id'] = 'different-session'
    with pytest.raises(ValueError, match='mixed source episode'):
        select(data)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(schema=index.CONFIG_SCHEMA, max_control_gap_ms=50)))
    with pytest.raises(ValueError, match='unknown config'):
        index.load_config(path)


def pause_events(start_ms=200, detected_ms=400, end_ms=600):
    events = [dict(episode_id='fixture', event='input_pause_started',
                   monotonic_ns=T0 + detected_ms * MS,
                   invalid_from_monotonic_ns=T0 + start_ms * MS)]
    if end_ms is not None:
        events.append(dict(episode_id='fixture', event='input_pause_ended',
                           monotonic_ns=T0 + end_ms * MS, outcome='rb_rearmed'))
    return events


def test_input_pause_retroactively_cuts_stale_receipt_and_never_bridges_recovery():
    data = records()
    # Deliberately retain valid-looking human controls/epoch throughout: the
    # event must exclude the watchdog detection lag independently of them.
    data[3].extend(reversed(pause_events()))
    original = deepcopy(data)
    rows, rejects, summary = select(data, index.Config(fps=20, horizon=10))
    assert next(r for r in rejects if r['grid_index'] == 0)['reason'] == 'input_pause_interval'
    assert all(r['action_window'][-1]['grid_monotonic_ns'] < T0 + 200 * MS
               or r['capture_started_monotonic_ns'] > T0 + 600 * MS for r in rows)
    assert any(r['anchor_monotonic_ns'] > T0 + 600 * MS for r in rows)
    assert summary['input_invalid_intervals_ns'] == [(T0 + 200 * MS, T0 + 600 * MS)]
    assert data == original


@pytest.mark.parametrize('end_ms', [600, None])
def test_partial_windows_stop_before_pause_and_unfinished_pause_blocks_tail(end_ms):
    data = records()
    data[3].extend(pause_events(end_ms=end_ms))
    rows, _, summary = select(data, index.Config(fps=20, horizon=10, allow_partial_windows=True))
    first = rows[0]
    assert first['valid_action_steps'] == 4  # 0, 50, 100, 150 ms; 200 is untrusted.
    assert first['truncation_reason'] == 'input_pause_interval'
    assert first['action_is_pad'] == [False] * 4 + [True] * 6
    for row in rows:
        assert row['action_window'][-1]['grid_monotonic_ns'] < T0 + 200 * MS or (
            end_ms is not None and row['capture_started_monotonic_ns'] > T0 + end_ms * MS)
    if end_ms is None:
        assert summary['input_invalid_intervals_ns'] == [(T0 + 200 * MS, None)]


def test_pause_camera_receipt_is_excluded_even_after_fresh_post_resume_poll():
    data = records()
    data[3].extend(pause_events())
    # At 650 ms, capture metadata is fresh but its required head image was
    # received during the pause and remains younger than the usual 200 ms cap.
    source = next(r for r in data[0] if r['available_monotonic_ns'] == T0 + 640 * MS)
    source['capture_started_monotonic_ns'] = T0 + 630 * MS
    payload = next(r for r in data[2] if r['observation_id'] == source['observation_id'])
    payload['frames'][0]['host_received_monotonic_ns'] = T0 + 590 * MS
    _, rejects, _ = select(data, index.Config(fps=20, horizon=3))
    assert next(r for r in rejects if r['grid_index'] == 13)['reason'] == 'input_pause_interval'


def test_home_allowlist_and_review_range_cannot_authorize_paused_controls():
    data = records()
    start, end = T0 + 100 * MS, T0 + 700 * MS
    data[3].extend([dict(episode_id='fixture', event='home_started', preset='积木demo-V1', monotonic_ns=start),
                   dict(episode_id='fixture', event='home_completed_feedback', monotonic_ns=end)])
    data[3].extend(pause_events())
    for control in data[1]:
        if start <= control['monotonic_ns'] <= end:
            control['source'] = 'home'
    _, rejects, _ = select(data, index.Config(fps=20, horizon=10),
                           accepted_home_intervals_ns=[(start, end)],
                           accepted_time_ranges_ns=[[T0 - 20 * MS, T0 + 2000 * MS]])
    assert next(r for r in rejects if r['grid_index'] == 0)['reason'] == 'input_pause_interval'


def create_auditable_raw(tmp_path):
    from f1dagger.recording import F1Recorder, make_sink
    from f1dagger.observation import Observation
    from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
    run = tmp_path / 'raw'
    run.mkdir()
    recorder = F1Recorder(run / 'journal', episode_id='fixture', task_prompt='synthetic test only',
        binding={'execution_mode': 'powered', 'recording_mode': 'hil-record', 'synthetic': True})
    sink = make_sink(run / 'payload', 'hil-record')
    image = BytesIO()
    Image.new('RGB', (16, 16)).save(image, format='JPEG')
    for i in range(20):
        t = T0 + i * 20 * MS
        frames = tuple(TimestampedFrame(c, i, DeviceTimestamp(t, 'ns', 'test'), t + 1,
                         image.getvalue(), 'image/jpeg', 16, 16) for c in index.CAMERAS)
        obs = Observation(i, t, t + 10, frames, (0.,) * 16, (t + 2,) * 16, (0.,) * 22)
        assert sink.submit(obs)
        recorder.record_timestamped_observation(obs, hil_state='hil_latched', intervention_id=1, policy_epoch=2)
        pubs = [{'topic_key': 'joint_command', 'monotonic_ns': t - 2, 'publisher_gid': 'aa' * 16}]
        if i == 0:
            pubs += [{'topic_key': c, 'monotonic_ns': t - 2, 'publisher_gid': 'aa' * 16}
                     for c in ('left_gripper_command', 'right_gripper_command')]
        recorder.control(now=t - 1, state='hil_latched', intervention=1, epoch=2,
            selected=np.zeros(16), submitted=np.zeros(16), source='human_arm', measured=np.zeros(16),
            sequence=i + 1, publication_complete=True, execution_mode='powered',
            action_source_timestamps_ns=[t - 2] * 14 + [T0 - 2] * 2, publications=pubs)
    assert sink.close()
    assert recorder.close()
    assert index.audit(run)['valid']
    return run


def test_real_raw_audit_transaction_review_binding_and_immutability(tmp_path):
    raw = create_auditable_raw(tmp_path)
    hashes = {str(p.relative_to(raw)): index.sha256(p) for p in raw.rglob('*') if p.is_file()}
    output = tmp_path / 'candidate'
    result = index.build_index(raw, output, mode='technical', config=index.Config(fps=20, horizon=3))
    assert result['summary']['complete_action_windows'] > 0
    assert result['source_raw_unchanged'] and result['raw_audit_valid']
    assert not result['training_ready'] and not result['human_review_accepted']
    assert not result['lerobot_export'] and not result['rlinf_reader_verified']
    assert hashes == {str(p.relative_to(raw)): index.sha256(p) for p in raw.rglob('*') if p.is_file()}
    with pytest.raises(FileExistsError):
        index.build_index(raw, output, mode='technical')
    with pytest.raises(ValueError, match='outside immutable'):
        index.build_index(raw, raw / 'candidate', mode='technical')
    review = tmp_path / 'review.json'
    doc = dict(contract=index.CONTRACT, manifest_sha256='wrong', reviewer='synthetic fixture',
               human_review_accepted=True, accepted_interventions=[1],
               accepted_time_ranges_ns=[[T0, T0 + 400 * MS]])
    review.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='another raw'):
        index.build_index(raw, tmp_path / 'reviewed', mode='reviewed', review_path=review)
    doc['manifest_sha256'] = index.audit(raw)['manifest_sha256']
    review.write_text(json.dumps(doc))
    result = index.build_index(raw, tmp_path / 'reviewed', mode='reviewed', review_path=review,
                               config=index.Config(fps=20, horizon=3))
    assert result['human_review_accepted'] and not result['training_ready']
