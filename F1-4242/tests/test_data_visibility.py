"""Catalog integration: server archives and RLT audits do not authorize Replay."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_rlt_assessment
from data_service import DataService
from replay_service import ReplayService
from test_data_archives import archive_record, write_index
from test_data_auth import episode
from test_data_rlt_assessment import sample, review_for, sidecar


def store(tmp_path, extras=None):
    episode(tmp_path / 'raw')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    return DataService(tmp_path / 'raw', runtime, tmp_path / 'web',
                       extra_catalog=(lambda: extras) if extras is not None else None)


def rlt_record(tmp_path, **options):
    row = sample(tmp_path, **options)
    files = [p for p in Path(row['path']).rglob('*') if p.is_file()]
    row.update(id=hashlib.sha256(row['path'].encode()).hexdigest()[:24],
               relative_path=row['path'], source='hil_policy_hil', library_folder='HIL/RLT',
               started_at='2026-10-29T03:25:41+00:00', size_bytes=sum(p.stat().st_size for p in files),
               files=[dict(name=p.name, relative_path=str(p.relative_to(row['path'])), size_bytes=p.stat().st_size) for p in files],
               recording_status='committed')
    return row


def test_archives_stay_outside_local_counts_bytes_episode_and_replay_lookup(tmp_path, monkeypatch):
    service = store(tmp_path)
    before = service.catalog()
    archive = archive_record()
    write_index(service.archives.path, [archive])
    result = service.catalog()
    assert len(result['archives']) == 1
    assert result['episodes'] == before['episodes']
    assert result['summary'] == before['summary']
    assert result['summary']['episode_count'] == 1
    assert result['summary']['total_bytes'] == sum(e['size_bytes'] for e in result['episodes'])
    with pytest.raises(ValueError, match='不存在'):
        service.episode(archive['id'])
    monkeypatch.setattr('replay_service.read_recording', lambda *a: pytest.fail('archive must never reach motion-data reader'))
    replay = ReplayService(SimpleNamespace(data=service))
    with pytest.raises(ValueError, match='不存在'):
        replay.load(archive['id'], 0, None)
    assert replay.tickets == {}
    assert not {'path', 'replay_url', 'size_bytes'} & result['archives'][0].keys()


@pytest.mark.parametrize('content', ['{bad', '{"schema":"wrong","records":[]}', '[]'])
def test_bad_archive_index_never_hides_local_catalog(tmp_path, content):
    service = store(tmp_path)
    before = service.catalog()
    service.archives.path.write_text(content)
    result = service.catalog()
    assert result['episodes'] == before['episodes'] and result['summary'] == before['summary']
    assert result['archives'] == [] and result['archives_error']
    assert result['error'] is None
    assert service.episode(before['episodes'][0]['id'])['id'] == before['episodes'][0]['id']
    assert service.archives.path.read_text() == content


def test_restored_local_identity_suppresses_duplicate_archive_without_mutating_index(tmp_path):
    service = store(tmp_path)
    local = service.catalog()['episodes'][0]
    archive = archive_record()
    archive['id'] = local['id']
    write_index(service.archives.path, [archive])
    before = service.archives.path.read_bytes()
    result = service.catalog()
    assert result['archives'] == [] and len(result['episodes']) == 1
    assert result['summary']['episode_count'] == 1
    assert service.archives.path.read_bytes() == before


def test_rlt_audit_snapshot_and_user_review_are_separate_and_notes_preserve_both(tmp_path):
    row = rlt_record(tmp_path / 'hil')
    service = store(tmp_path, [row])
    initial = service.episode(row['id'])
    assert initial['review']['status'] == 'pending'
    assert initial['rlt_assessment']['split'] == 'pending'
    data_rlt_assessment.save_training_review(row, review_for(row))
    assessed = service.episode(row['id'], refresh=True)
    assert assessed['rlt_assessment']['split'] == 'train'
    assert assessed['rlt_assessment']['original_status'] == 'failure'
    assert assessed['rlt_assessment']['outcome'] == 'assisted_success'
    assert assessed['review']['status'] == 'pending' and assessed['review']['revision'] == 'none'
    review_snapshot = json.loads((Path(row['path']) / 'f1-annotations.json').read_text())['rlt_training_review']
    service.save_notes(row['id'], '用户新备注', 'tester', row['source_signature'], assessed['review']['revision'])
    edited = service.episode(row['id'], refresh=True)
    assert edited['review']['notes'] == '用户新备注' and edited['review']['status'] == 'pending'
    assert edited['rlt_assessment'] == assessed['rlt_assessment']
    assert json.loads((Path(row['path']) / 'f1-annotations.json').read_text())['rlt_training_review'] == review_snapshot


def test_generic_approved_review_does_not_admit_rlt_to_training(tmp_path):
    row = rlt_record(tmp_path / 'hil', status='success')
    service = store(tmp_path, [row])
    before = service.episode(row['id'])
    service.save_review(row['id'], 'approved', '人工内容通过', 'operator', row['source_signature'], before['review']['revision'])
    after = service.episode(row['id'])
    assert after['review']['status'] == 'approved'
    assert after['rlt_assessment']['split'] == 'pending'
    assert after['rlt_assessment']['outcome'] == 'assisted_success'


def test_stale_structured_review_is_visible_but_never_counts_as_training(tmp_path):
    row = rlt_record(tmp_path / 'hil')
    sidecar(row, review_for(row, source_signature='b' * 64))
    service = store(tmp_path, [row])
    result = service.episode(row['id'])
    assert result['rlt_assessment']['quality'] == 'source_changed'
    assert result['rlt_assessment']['split'] == 'pending'
    assert result['rlt_assessment']['transition_count'] is None
    assert result['review']['status'] == 'pending'


def test_malformed_rlt_evidence_does_not_hide_following_extra_record(tmp_path):
    broken = rlt_record(tmp_path / 'bad')
    good = rlt_record(tmp_path / 'good', status='success')
    (Path(broken['path']) / 'journal/events.jsonl').write_text('{broken')
    service = store(tmp_path, [broken, good])
    result = service.catalog()
    assert len(result['episodes']) == 3 and result['error'] is None
    assert service.episode(broken['id'])['rlt_assessment']['quality'] == 'unreadable'
    assert service.episode(good['id'])['rlt_assessment']['outcome'] == 'assisted_success'


@pytest.mark.parametrize('error_type', [ValueError, AttributeError, RecursionError])
def test_one_assessment_failure_is_isolated_from_other_extra_rows(tmp_path, monkeypatch, error_type):
    broken = rlt_record(tmp_path / 'bad')
    good = rlt_record(tmp_path / 'good', status='success')
    real = data_rlt_assessment.assess
    def injected(row, *args):
        if row['id'] == broken['id']:
            raise error_type('bad evidence field')
        return real(row, *args)
    monkeypatch.setattr(data_rlt_assessment, 'assess', injected)
    service = store(tmp_path, [broken, good])
    result = service.catalog()
    assert len(result['episodes']) == 3 and result['error'] is None
    assert service.episode(broken['id'])['rlt_assessment']['split'] == 'pending'
    assert service.episode(good['id'])['rlt_assessment']['outcome'] == 'assisted_success'


def test_unknown_capture_mode_gets_no_rlt_approval_and_other_rows_remain(tmp_path):
    unknown = rlt_record(tmp_path / 'unknown')
    unknown.update(capture_mode='unknown', library_folder='其他录制/待确认来源')
    good = rlt_record(tmp_path / 'good', status='success')
    service = store(tmp_path, [unknown, good])
    assert len(service.catalog()['episodes']) == 3
    assert 'rlt_assessment' not in service.episode(unknown['id'])
    assert service.episode(good['id'])['rlt_assessment']['split'] == 'pending'
