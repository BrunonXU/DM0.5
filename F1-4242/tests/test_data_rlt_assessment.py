"""Capture source, human outcome, structural quality and training use stay separate."""
import hashlib
import json
from pathlib import Path

import pytest

import data_rlt_assessment as assessment
import data_annotations as annotations


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))


def sample(tmp_path, *, local=True, status='failure', assisted=True, controls=True,
           complete=True, old=False):
    root = tmp_path / 'episode'
    journal = root / 'journal'
    journal.mkdir(parents=True)
    episode_id = 'f1_hil_20261029T032541Z_test'
    manifest = dict(episode_id=episode_id, complete=complete, writer_error=None,
                    counts=dict(control=4 if controls else 0),
                    binding=dict(rlt=dict(phase_control='f710_dpad_right_v1' if old else 'f1_rlt_local_v2')))
    write_json(journal / 'manifest.json', manifest)
    (journal / 'control.jsonl').write_text('{}\n' if controls else '')
    events = [dict(event='demonstration_started', monotonic_ns=100_000_000_000, episode_id=episode_id)]
    if local:
        common = dict(episode_id=episode_id, segment_id=episode_id + ':placement:1', started_ns=120_000_000_000)
        events.extend([
            dict(common, event='rlt_local_opened', monotonic_ns=120_000_000_000, status='active'),
            dict(common, event='rlt_local_closed', monotonic_ns=150_000_000_000,
                 ended_ns=150_000_000_000, status=status, assisted=assisted,
                 reward=1 if status == 'success' else (0 if status == 'failure' else None),
                 terminated=status in ('success', 'failure'),
                 termination_reason='human_confirmed' if status == 'success' else 'operator_handoff_without_success',
                 terminal_observation=dict(observation_id=1765, available_monotonic_ns=149_900_000_000)),
        ])
    if old:
        events.append(dict(episode_id=episode_id, event='episode_end_accepted', monotonic_ns=160_000_000_000))
    (journal / 'events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in events))
    row = dict(id='catalog-id', run_id=episode_id, name=episode_id, capture_mode='rlt', path=str(root),
               metadata_sha256=hashlib.sha256((journal / 'manifest.json').read_bytes()).hexdigest(),
               source_signature='a' * 64)
    return row


def review_for(row, **updates):
    result = dict(schema=assessment.REVIEW_SCHEMA, episode_id=row['run_id'],
                  metadata_sha256=row['metadata_sha256'], source_signature=row['source_signature'],
                  approved=True, reviewed_at='2026-10-29T07:00:00Z', split='train',
                  outcome='assisted_success', confirmation_kind='manual_confirmation',
                  confirmation_label='漏按↑，人工补确认', transition_count=53,
                  retained_seconds=23.4, terminal_monotonic_ns=143_220_000_000,
                  terminal_observation_id=1765, notes_summary='人工辅助完成，撤离和Home排除',
                  excluded_summary='撤离、Home和质量缺口', evidence_refs=['receipt-sha-bound-review.json'])
    result.update(updates)
    return result


def sidecar(row, review, **extra):
    document = dict(schema=annotations.SCHEMA, recording_path=row['path'], review=dict(notes='操作者原备注'),
                    review_present=True, rlt_training_review=review, **extra)
    path = Path(row['path']) / annotations.NAME
    write_json(path, document)
    return path


def test_missing_success_key_review_projects_success_without_changing_original_reward(tmp_path):
    row = sample(tmp_path)
    events = Path(row['path']) / 'journal/events.jsonl'
    original = events.read_bytes()
    assessment.save_training_review(row, review_for(row))
    result = assessment.assess(row)
    assert result['outcome'] == 'assisted_success'
    assert result['original_status'] == 'failure'
    assert result['source_bound'] and result['snapshot'] and result['split'] == 'train'
    assert result['transition_count'] == 53 and result['retained_seconds'] == 23.4
    assert result['local_start_s'] == 20 and result['original_end_s'] == 50
    assert result['terminal_s'] == 43.22 and result['terminal_observation_id'] == 1765
    assert result['confirmation_kind'] == 'manual_confirmation'
    assert events.read_bytes() == original


def test_original_success_is_not_training_approval_and_get_never_creates_sidecar(tmp_path):
    row = sample(tmp_path, status='success')
    before = sorted(str(p) for p in Path(row['path']).rglob('*'))
    result = assessment.assess(row)
    assert result['outcome'] == 'assisted_success' and result['confirmation_kind'] == 'live_success'
    assert result['split'] == 'pending' and result['transition_count'] is None
    assert not result['snapshot'] and not result['source_bound']
    assert sorted(str(p) for p in Path(row['path']).rglob('*')) == before


def test_user_notes_cannot_authorize_success_or_training(tmp_path):
    row = sample(tmp_path, status='failure')
    write_json(Path(row['path']) / annotations.NAME,
               dict(schema=annotations.SCHEMA, review=dict(status='approved', notes='成功！已经训练，有效53条')))
    result = assessment.assess(row)
    assert result['outcome'] == 'failure' and result['split'] == 'pending'
    assert result['transition_count'] is None


@pytest.mark.parametrize('field', ['metadata_sha256', 'source_signature', 'episode_id'])
def test_source_bound_review_is_invalidated_after_identity_change(tmp_path, field):
    row = sample(tmp_path)
    review = review_for(row)
    review[field] = ('b' * 64) if field != 'episode_id' else 'another-episode'
    sidecar(row, review)
    result = assessment.assess(row)
    assert result['quality'] == 'source_changed' and result['split'] == 'pending'
    assert result['transition_count'] is None and not result['source_bound']
    assert result['outcome'] == 'failure'  # Raw event still visible, never fake success.


def test_zero_control_is_not_fixed_by_success_review(tmp_path):
    row = sample(tmp_path, controls=False)
    sidecar(row, review_for(row))
    result = assessment.assess(row)
    assert result['quality'] == 'missing_control' and result['split'] == 'exclude'
    assert result['transition_count'] == 0 and not result['snapshot']


def test_old_up_is_save_only_with_no_local_success(tmp_path):
    row = sample(tmp_path, controls=False, local=False, old=True)
    result = assessment.assess(row)
    assert result['confirmation_kind'] == 'legacy_save'
    assert result['outcome'] == 'unknown' and result['quality'] == 'missing_control'


def test_human_reported_success_can_remain_visible_while_missing_actions_stay_excluded(tmp_path):
    row = sample(tmp_path, controls=False, local=False, old=True)
    review = review_for(row, split='exclude', confirmation_kind='legacy_save',
                        confirmation_label='人工报告；旧↑仅保存', notes_summary='用户报告人工介入后完成放置')
    assessment.save_training_review(row, review)
    result = assessment.assess(row)
    assert result['outcome'] == 'assisted_success' and result['outcome_label'] == '人工报告：人工辅助成功'
    assert result['confirmation_kind'] == 'legacy_save'
    assert result['quality'] == 'missing_control' and result['split'] == 'exclude'
    assert result['transition_count'] == 0 and result['retained_seconds'] == 0
    assert result['source_bound'] and result['snapshot'] and result['evidence_refs'] == review['evidence_refs']
    assert result['notes_summary'] == '用户报告人工介入后完成放置'


@pytest.mark.parametrize('split', ['train', 'validation'])
def test_missing_control_does_not_accept_positive_admission_from_human_success(tmp_path, split):
    row = sample(tmp_path, controls=False, local=False, old=True)
    sidecar(row, review_for(row, split=split))
    result = assessment.assess(row)
    assert result['quality'] == 'missing_control' and result['split'] == 'exclude'
    assert result['transition_count'] == 0 and not result['source_bound']
    assert result['outcome'] == 'unknown'


def test_changed_source_cannot_copy_historical_human_success_onto_missing_control_record(tmp_path):
    row = sample(tmp_path, controls=False, local=False, old=True)
    sidecar(row, review_for(row, split='exclude', source_signature='b' * 64))
    result = assessment.assess(row)
    assert result['quality'] == 'missing_control' and result['outcome'] == 'unknown'
    assert not result['source_bound']


def test_rlt_launch_with_only_vla_does_not_generate_failure_reward(tmp_path):
    row = sample(tmp_path, local=False)
    result = assessment.assess(row)
    assert result['outcome'] == 'not_entered' and result['quality'] == 'no_local_segment'
    assert result['split'] == 'exclude' and result['transition_count'] == 0


def test_validation_snapshot_stays_out_of_gradients(tmp_path):
    row = sample(tmp_path, status='success')
    assessment.save_training_review(row, review_for(row, split='validation', transition_count=48,
                                                   confirmation_kind='live_success'))
    result = assessment.assess(row)
    assert result['split'] == 'validation' and '不入梯度' in result['usage_label']
    assert '实时训练' in result['snapshot_notice']


def test_review_cannot_invent_a_local_segment(tmp_path):
    row = sample(tmp_path, local=False)
    sidecar(row, review_for(row))
    result = assessment.assess(row)
    assert result['quality'] == 'review_invalid' and result['split'] == 'pending'


@pytest.mark.parametrize('terminal', [119_000_000_000, 151_000_000_000])
def test_review_terminal_must_stay_inside_actual_local_segment(tmp_path, terminal):
    row = sample(tmp_path)
    sidecar(row, review_for(row, terminal_monotonic_ns=terminal))
    result = assessment.assess(row)
    assert result['quality'] == 'review_invalid' and result['transition_count'] is None


@pytest.mark.parametrize('split', ['train', 'validation'])
def test_one_review_cannot_promote_multiple_local_segments_or_an_outside_terminal(tmp_path, split):
    row = sample(tmp_path, status='success')
    path = Path(row['path']) / 'journal/events.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()]
    start = dict(events[-2], segment_id=row['run_id'] + ':placement:2',
                 started_ns=160_000_000_000, monotonic_ns=160_000_000_000)
    end = dict(events[-1], segment_id=start['segment_id'], started_ns=start['started_ns'],
               ended_ns=170_000_000_000, monotonic_ns=170_000_000_000,
               terminal_observation=dict(observation_id=2000, available_monotonic_ns=169_900_000_000))
    events.extend([start, end])
    path.write_text(''.join(json.dumps(event) + '\n' for event in events))
    sidecar(row, review_for(row, split=split, terminal_monotonic_ns=180_000_000_000))
    result = assessment.assess(row)
    assert result['quality'] == 'pending_review' and result['split'] == 'pending'
    assert result['local_segment_count'] == 2 and result['outcome'] == 'mixed'
    assert result['transition_count'] is None and not result['snapshot']
    assert result['usage_label'] == '含多个局部段，需逐段审核'


def test_incomplete_raw_data_does_not_become_ready_with_review(tmp_path):
    row = sample(tmp_path, complete=False)
    sidecar(row, review_for(row))
    assert assessment.assess(row)['quality'] == 'recording_incomplete'


def test_partial_recording_is_not_read_and_final_rename_automatically_rechecks(tmp_path, monkeypatch):
    row = sample(tmp_path, status='success')
    root = Path(row['path'])
    (root / 'journal').rename(root / 'journal.partial')
    before = {p.name: p.read_bytes() for p in (root / 'journal.partial').iterdir()}
    with monkeypatch.context() as patch:
        patch.setattr(assessment, '_read_evidence', lambda *a: pytest.fail('partial evidence must never be read'))
        result = assessment.assess(row)
    assert result['quality'] == 'recording_incomplete' and result['usage_label'] == '尚未完整封存'
    assert result['notes_summary'] == '录制尚未完整封存，封存后自动复核；当前不批准训练'
    assert result['outcome'] == 'unknown' and result['split'] == 'pending'
    assert result['transition_count'] is None and result['local_segment_count'] is None
    assert {p.name: p.read_bytes() for p in (root / 'journal.partial').iterdir()} == before
    (root / 'journal.partial').rename(root / 'journal')
    finalized = assessment.assess(row)
    assert finalized['quality'] == 'pending_review' and finalized['outcome'] == 'assisted_success'
    assert finalized['confirmation_kind'] == 'live_success' and finalized['split'] == 'pending'


def test_complete_interrupted_recording_keeps_source_bound_failure_prefix_admission(tmp_path):
    row = sample(tmp_path, status='interrupted')
    assessment.save_training_review(row, review_for(row, outcome='failure', confirmation_kind='reviewed_failure'))
    result = assessment.assess(row)
    assert result['quality'] == 'reviewed' and result['split'] == 'train'
    assert result['outcome'] == 'failure' and result['original_status'] == 'interrupted'


def test_missing_assistance_flag_never_becomes_autonomous_success(tmp_path):
    row = sample(tmp_path, status='success', assisted=None)
    assert assessment.assess(row)['outcome'] == 'unknown'


def test_only_explicit_false_assistance_is_autonomous_success(tmp_path):
    row = sample(tmp_path, status='success', assisted=False)
    result = assessment.assess(row)
    assert result['outcome'] == 'autonomous_success' and result['split'] == 'pending'


def test_writer_preserves_notes_capture_metadata_and_review_absence(tmp_path):
    row = sample(tmp_path)
    doc_path = sidecar(row, None, capture_classification=dict(capture_mode='rlt'))
    before = json.loads(doc_path.read_text())
    del before['rlt_training_review']
    write_json(doc_path, before)
    review = review_for(row)
    assessment.save_training_review(row, review)
    after = json.loads(doc_path.read_text())
    assert after['review'] == before['review']
    assert after['capture_classification'] == before['capture_classification']
    first_stat = doc_path.stat().st_mtime_ns
    assessment.save_training_review(row, review)
    assert doc_path.stat().st_mtime_ns == first_stat


def test_writer_does_not_overwrite_a_newer_same_source_review_without_cas(tmp_path):
    row = sample(tmp_path)
    initial = review_for(row)
    path = assessment.save_training_review(row, initial)
    newer = review_for(row, reviewed_at='2026-10-29T08:00:00Z', notes_summary='新的审核结论')
    with pytest.raises(ValueError, match='版本'):
        assessment.save_training_review(row, newer)
    assert json.loads(path.read_text())['rlt_training_review'] == initial
    initial_sha = assessment.training_review_sha256(initial)
    assessment.save_training_review(row, newer, expected_existing_sha256=initial_sha)
    assert json.loads(path.read_text())['rlt_training_review'] == newer
    before = path.read_bytes()
    with pytest.raises(ValueError, match='版本'):
        assessment.save_training_review(row, initial, expected_existing_sha256=initial_sha)
    assert path.read_bytes() == before
    before_stat = path.stat().st_mtime_ns
    assessment.save_training_review(row, newer)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == before_stat


def test_review_canonical_digest_ignores_object_key_order():
    first = dict(notes='人工确认', approved=True, terminal=dict(observation_id=1765, status='success'))
    reordered = dict(terminal=dict(status='success', observation_id=1765), approved=True, notes='人工确认')
    assert assessment.training_review_sha256(first) == assessment.training_review_sha256(reordered)


def test_writer_rejects_manifest_changed_since_catalog(tmp_path):
    row = sample(tmp_path)
    path = Path(row['path']) / 'journal/manifest.json'
    content = json.loads(path.read_text())
    content['complete'] = False
    write_json(path, content)
    with pytest.raises(ValueError, match='原件已变化'):
        assessment.save_training_review(row, review_for(row))
    assert not (Path(row['path']) / annotations.NAME).exists()


def test_cache_reads_once_then_invalidates_on_events_or_sidecar_change(tmp_path):
    row = sample(tmp_path)
    assessment._read_evidence.cache_clear()
    assert assessment.assess(row)['outcome'] == 'failure'
    assert assessment.assess(row)['outcome'] == 'failure'
    assert assessment._read_evidence.cache_info().misses == 1
    assessment.save_training_review(row, review_for(row))
    assert assessment.assess(row)['outcome'] == 'assisted_success'
    assert assessment._read_evidence.cache_info().misses == 2
    path = Path(row['path']) / 'journal/events.jsonl'
    with path.open('a') as stream:
        stream.write('{broken\n')
    assert assessment.assess(row)['quality'] == 'unreadable'
    assert assessment._read_evidence.cache_info().misses == 3


def test_overlarge_events_are_not_scanned_or_projected_as_no_local(tmp_path, monkeypatch):
    row = sample(tmp_path)
    monkeypatch.setattr(assessment, 'MAX_EVENTS_BYTES', 2)
    assessment._read_evidence.cache_clear()
    result = assessment.assess(row)
    assert result['quality'] == 'unreadable' and result['local_segment_count'] is None
    assert result['outcome'] == 'unknown'


def test_symlink_evidence_is_not_followed(tmp_path):
    row = sample(tmp_path)
    events = Path(row['path']) / 'journal/events.jsonl'
    target = tmp_path / 'outside'
    events.rename(target)
    events.symlink_to(target)
    assert assessment.assess(row)['quality'] == 'unreadable'


@pytest.mark.parametrize('field,value', [('terminal_observation', ['bad']), ('ended_ns', 'bad'), ('started_ns', None)])
def test_bad_local_event_fields_degrade_to_unknown_without_raising(tmp_path, field, value):
    row = sample(tmp_path, status='success')
    path = Path(row['path']) / 'journal/events.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[-1][field] = value
    path.write_text(''.join(json.dumps(item) + '\n' for item in events))
    result = assessment.assess(row)
    assert result['quality'] == 'unreadable' and result['outcome'] == 'unknown'


@pytest.mark.parametrize('changes', [dict(approved=False), dict(transition_count=0),
                                   dict(retained_seconds=float('nan')), dict(reviewed_at=''),
                                   dict(requires_remote_prefix_verification=True),
                                   dict(requires_remote_policy_verification=True),
                                   dict(outcome='unknown')])
def test_invalid_approval_is_never_projected(tmp_path, changes):
    row = sample(tmp_path)
    sidecar(row, review_for(row, **changes))
    assert assessment.assess(row)['quality'] == 'review_invalid'


def test_non_rlt_recording_does_not_get_rlt_status(tmp_path):
    row = sample(tmp_path)
    row['capture_mode'] = 'dagger'
    assert assessment.assess(row) is None
