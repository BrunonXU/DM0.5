"""Durability, capacity and compatibility checks for F1 grouped image storage."""
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from f1dagger.contract import CAMERAS
from f1dagger.payload_archive import F1PayloadSink, FORMAT, STORAGE, read_payload
from f1dagger.recording import F1Recorder, audit, make_sink, read_rows
from g2dagger.recording_audit import audit_payload_archive
from g2dagger.recording_sinks import EpisodeVideoSink
from test_f1_policy import observation


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('writer condition timed out')
        time.sleep(.005)


def test_grouped_packs_preserve_exact_images_index_and_reduce_sync_barriers(tmp_path):
    sink = F1PayloadSink(tmp_path/'payload')
    original = [observation(i, 1_000_000_000 + i*33_333_333) for i in range(53)]
    assert sink.submit_batch(original)
    assert sink.close()
    manifest = json.loads((sink.path/'manifest.json').read_text())
    rows = read_rows(sink.path/'frames.jsonl')
    assert manifest['schema_version'] == 2 and manifest['format'] == FORMAT
    assert manifest['complete'] and manifest['count'] == len(rows) == 53
    assert sink.checkpoints == 4
    # Four syncs per group (three packs + index), and one final manifest.
    assert sink.sync_calls == 4*4 + 1 < 53*3
    assert sink.pending_items == sink.pending_bytes == 0
    assert len(list(sink.path.iterdir())) == 5
    offsets = dict.fromkeys(CAMERAS, 0)
    for i, (source, row) in enumerate(zip(original, rows)):
        for frame, archived in zip(source.frames, row['frames']):
            assert archived['storage'] == STORAGE
            assert archived['payload_offset'] == offsets[frame.camera]
            assert archived['archive_frame_index'] == i
            assert read_payload(sink.path, archived) == frame.payload
            offsets[frame.camera] += len(frame.payload)
    for camera in CAMERAS:
        pack = manifest['pack_files'][camera]
        assert pack['bytes'] == offsets[camera]
        assert pack['sha256'] == hashlib.sha256((sink.path/pack['path']).read_bytes()).hexdigest()
    assert sink.close()  # Idempotence never reopens/writes the completed archive.
    assert not audit_payload_archive(sink.path)['valid']
    assert 'F1 audit/export' in audit_payload_archive(sink.path)['errors'][0]


def test_pending_capacity_includes_entire_batch_until_all_syncs_finish(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class StalledSync(F1PayloadSink):
        def _sync_file(self, handle):
            entered.set()
            assert release.wait(3)
            super()._sync_file(handle)
    sink = StalledSync(tmp_path/'payload', max_items=1)
    try:
        assert sink.submit(observation(0))
        assert entered.wait(2)
        stats = sink.diagnostics()
        assert stats['pending_items'] == 1 and stats['count'] == 0
        assert not sink.submit(observation(1))
        assert sink.diagnostics()['last_rejection_reason'] == 'pending_item_limit'
        assert sink.diagnostics()['last_rejected_observation_id'] == 1
        assert not sink.close(timeout=.001)
        assert sink.partial_path.is_dir() and not sink.path.exists()
    finally:
        release.set()
    assert not sink.close()
    manifest = json.loads((sink.partial_path/'manifest.json').read_text())
    assert manifest['count'] == 1 and manifest['drop_count'] == 1 and not manifest['complete']


def test_time_checkpoint_flushes_a_low_rate_tail_without_closing(tmp_path):
    sink = F1PayloadSink(tmp_path/'payload', checkpoint_interval_s=.03)
    try:
        assert sink.submit(observation())
        wait_for(lambda: sink.diagnostics()['count'] == 1)
        assert sink.diagnostics()['pending_items'] == 0
        assert not sink.path.exists()  # A checkpoint never publishes the episode.
    finally:
        assert sink.close()


def test_byte_capacity_is_unchanged_and_rejection_is_explicit(tmp_path):
    source = observation()
    sink = F1PayloadSink(tmp_path/'payload', max_bytes=source.retained_bytes-1)
    assert not sink.submit(source)
    assert sink.diagnostics()['last_rejection_reason'] == 'pending_byte_limit'
    assert not sink.close()
    assert not sink.path.exists()


@pytest.mark.parametrize('failure_sync', [1, 4])
def test_pack_or_index_sync_failure_stays_partial_and_never_frees_as_durable(tmp_path, failure_sync):
    class BrokenSync(F1PayloadSink):
        calls = 0
        def _sync_file(self, handle):
            self.calls += 1
            if self.calls == failure_sync:
                raise OSError('injected checkpoint disk failure')
            super()._sync_file(handle)
    sink = BrokenSync(tmp_path/'payload', checkpoint_observations=1)
    assert sink.submit(observation())
    wait_for(lambda: sink.writer_error is not None)
    assert sink.count == 0
    assert not sink.submit(observation(1))
    assert sink.diagnostics()['last_rejection_reason'] == 'writer_error'
    assert not sink.close()
    manifest = json.loads((sink.partial_path/'manifest.json').read_text())
    assert not manifest['complete'] and 'checkpoint disk failure' in manifest['writer_error']
    assert not sink.path.exists()


def test_final_manifest_sync_failure_cannot_publish(tmp_path):
    class BrokenManifest(F1PayloadSink):
        def _sync_file(self, handle):
            if Path(handle.name).name == 'manifest.json.tmp':
                raise OSError('injected manifest sync failure')
            super()._sync_file(handle)
    sink = BrokenManifest(tmp_path/'payload')
    assert sink.submit(observation())
    with pytest.raises(OSError, match='manifest sync'):
        sink.close()
    assert sink.partial_path.exists() and not sink.path.exists()
    assert sink.diagnostics()['writer_error']


def test_aborted_close_remains_partial_after_successful_drain(tmp_path):
    sink = F1PayloadSink(tmp_path/'payload')
    assert sink.submit(observation())
    assert not sink.close(status='failed')
    assert not sink.close(status='completed')
    assert not sink.path.exists()
    assert json.loads((sink.partial_path/'manifest.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('legacy', [False, True])
def test_f1_audit_supports_both_payload_generations_and_detects_corruption(tmp_path, legacy):
    run = tmp_path/'run'
    recorder = F1Recorder(run/'journal', episode_id='archive-check', task_prompt='offline')
    sink = EpisodeVideoSink(run/'payload') if legacy else make_sink(run/'payload', 'hil-record')
    for i in range(3):
        obs = observation(i, 1_000_000_000 + i*33_333_333)
        assert sink.submit(obs)
        assert recorder.record_timestamped_observation(obs, hil_state='hold', intervention_id=0, policy_epoch=0)
    assert sink.close() and recorder.close()
    report = audit(run)
    assert report['valid'], report
    frames = read_rows(sink.path/'frames.jsonl')
    target = sink.path/frames[1]['frames'][0]['path']
    with target.open('r+b') as handle:
        handle.seek(frames[1]['frames'][0].get('payload_offset', 0))
        handle.write(b'broken')
    assert not audit(run)['valid']
    with pytest.raises(ValueError, match='hash/size'):
        read_payload(sink.path, frames[1]['frames'][0])


@pytest.mark.parametrize('mutation', ['offset', 'length', 'path', 'storage'])
def test_pack_reader_rejects_bad_bounds_paths_and_unknown_storage(tmp_path, mutation):
    sink = F1PayloadSink(tmp_path/'payload')
    assert sink.submit(observation()) and sink.close()
    frame = read_rows(sink.path/'frames.jsonl')[0]['frames'][0]
    changes = {'offset': {'payload_offset': -1}, 'length': {'payload_bytes': 10**12},
               'path': {'path': '../outside'}, 'storage': {'storage': 'unsupported'}}
    with pytest.raises(ValueError):
        read_payload(sink.path, dict(frame, **changes[mutation]))


def test_prebuffer_keeps_original_observation_ids_and_provides_diagnostics(tmp_path):
    sink = make_sink(tmp_path/'payload', 'prebuffer-record')
    for i in range(4):
        assert sink.submit(observation(i, 1_000_000_000+i*33_333_333))
    assert not sink.diagnostics()['prebuffer_active']
    assert sink.trigger() and sink.trigger()
    assert sink.diagnostics()['prebuffer_active']
    assert sink.submit(observation(4, 1_200_000_000))
    assert sink.close()
    assert [r['observation_id'] for r in read_rows(tmp_path/'payload/frames.jsonl')] == list(range(5))


@pytest.mark.parametrize('mode, retained, valid', [
    ('hil-record', [0, 1], False), ('hil-record', [0, 1, 2], True),
    ('prebuffer-record', [1, 2], True), ('prebuffer-record', [0, 2], False),
    ('prebuffer-record', [], True)])
def test_complete_recording_cannot_hide_missing_images(tmp_path, mode, retained, valid):
    run = tmp_path/'run'
    recorder = F1Recorder(run/'journal', episode_id='coverage', task_prompt='offline',
                          binding={'recording_mode': mode})
    sink = F1PayloadSink(run/'payload')
    for i in range(3):
        obs = observation(i, 1_000_000_000+i*33_333_333)
        if i in retained:
            assert sink.submit(obs)
        assert recorder.record_timestamped_observation(obs, hil_state='hold', intervention_id=0, policy_epoch=0)
    assert sink.close() and recorder.close()
    report = audit(run)
    assert report['valid'] is valid, report
    if not valid:
        assert 'coverage' in report['errors'][0]
