import json
import os
import pytest

from data_archives import DataArchives, MAX_BYTES, SCHEMA


def archive_record():
    return dict(id='c5a65b2f960583c8076cc1ba', name='历史纠正', capture_mode='dagger',
                started_at='2026-10-21T15:02:12+08:00', storage_label='6000D',
                availability='archived', source_deleted=True, purpose_label='开发验证',
                notes='机器人原件已删除，服务器保留已核验镜像。',
                source_identity=dict(run_id='f1_hil_20261021T070206Z_c70f9627', manifest_sha256='a' * 64),
                integrity_checked_at='2026-10-29T14:50:00+08:00', verified=True,
                deleted_at='2026-10-21T17:42:04+08:00')


def write_index(path, records):
    path.write_text(json.dumps(dict(schema=SCHEMA, records=records), ensure_ascii=False))


def test_archive_is_only_an_index_and_never_a_recording(tmp_path, monkeypatch):
    archive = DataArchives(tmp_path)
    assert archive.read() == [] and not archive.path.exists()
    write_index(archive.path, [archive_record()])
    before = archive.path.read_bytes()
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('archive index must never use network'))
    row, = archive.read()
    assert row['source_deleted'] and row['read_only'] and row['verified']
    assert row['library_folder'] == 'HIL/DAgger' and row['capture_label'] == 'DAgger'
    assert row['display_timezone'] == 'Asia/Shanghai'
    assert row['availability_label'] == '机器人已删除 · 服务器镜像上次核验完整'
    assert not {'path', 'replay_url', 'delete_url', 'size_bytes', 'training_ready'} & row.keys()
    assert archive.path.read_bytes() == before


@pytest.mark.parametrize('change', [
    {'url': 'http://example.invalid/source'},
    {'path': '/etc/passwd'},
    {'read_only': False},
    {'source_deleted': 'true'},
    {'availability': 'online'},
    {'capture_mode': ['dagger']},
    {'source_identity': {'run_id': '../raw', 'manifest_sha256': 'a' * 64}},
    {'source_identity': {'run_id': 'raw', 'manifest_sha256': 'bad'}},
    {'started_at': '2026-10-21T15:02:12'},
    {'integrity_checked_at': None},
    {'notes': 'x' * 4001},
])
def test_invalid_or_actionable_fields_rejected(tmp_path, change):
    archive = DataArchives(tmp_path)
    write_index(archive.path, [{**archive_record(), **change}])
    before = archive.path.read_bytes()
    with pytest.raises(ValueError):
        archive.read()
    assert archive.path.read_bytes() == before


def test_unverified_archive_does_not_claim_current_availability(tmp_path):
    archive = DataArchives(tmp_path)
    row = archive_record()
    row.update(verified=False, integrity_checked_at=None)
    write_index(archive.path, [row])
    result, = archive.read()
    assert not result['verified'] and result['integrity_checked_at'] is None
    assert result['availability_label'] == '机器人已删除 · 服务器镜像尚未核验'


@pytest.mark.parametrize('payload', [
    '{broken',
    '{"schema":"wrong","records":[]}',
    '{"schema":"f1_data_archives_v1","records":[],"url":"http://invalid"}',
    '{"schema":"wrong","schema":"f1_data_archives_v1","records":[]}',
    '{"schema":"f1_data_archives_v1","records":{}}',
    '[' * 2000 + ']' * 2000,
])
def test_malformed_index_has_bounded_failure(tmp_path, payload):
    archive = DataArchives(tmp_path)
    archive.path.write_text(payload)
    with pytest.raises(ValueError):
        archive.read()


def test_oversize_symlink_and_fifo_do_not_get_read(tmp_path):
    archive = DataArchives(tmp_path)
    archive.path.write_bytes(b' ' * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match='过大'):
        archive.read()
    archive.path.unlink()
    outside = tmp_path / 'outside'
    write_index(outside, [archive_record()])
    archive.path.symlink_to(outside)
    with pytest.raises(OSError):
        archive.read()
    archive.path.unlink()
    os.mkfifo(archive.path)
    with pytest.raises(ValueError):
        archive.read()


def test_duplicate_identity_is_not_double_counted(tmp_path):
    archive = DataArchives(tmp_path)
    write_index(archive.path, [archive_record(), archive_record()])
    with pytest.raises(ValueError, match='身份重复'):
        archive.read()
