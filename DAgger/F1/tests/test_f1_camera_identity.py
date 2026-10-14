"""ROS callback identity remains distinct from repeating camera header stamps."""
from dataclasses import replace
from io import BytesIO
import json

import numpy as np
from PIL import Image
import pytest

from f1dagger.contract import CAMERAS, CONTRACT
from f1dagger.export import select_segments
from f1dagger.recording import CALLBACK_IDENTITY_SOURCE, F1Recorder, audit, make_sink
from g2dagger.recording_sinks import EpisodeVideoSink
from test_f1_policy import observation


class MetadataObservation:
    def __init__(self, source, extra):
        self.source, self.extra = source, extra
    def __getattr__(self, name):
        return getattr(self.source, name)
    def metadata(self):
        row = self.source.metadata()
        row.update(self.extra)
        return row


def callback_observation(i, *, declared=True, repeat_stamp=True, frame_ids=None,
                         receipt=None, discontinuity=False, drop_count=0, extra=None):
    # Exact 30 Hz fixture; repeatedly adding a rounded period drifts by ns.
    begin = 1_000_000_000 + round(i*1_000_000_000/30)
    obs = observation(i, begin)
    stream = BytesIO()
    Image.new('RGB', (16, 16), color=(i*17 % 256, i*41 % 256, i*7 % 256)).save(stream, format='JPEG')
    frames = tuple(replace(frame, frame_id=(50+i if frame_ids is None else frame_ids[j]),
        payload=stream.getvalue(), host_received_monotonic_ns=begin+1 if receipt is None else receipt,
        timestamp=replace(frame.timestamp, value=2_000_000_000 if repeat_stamp else 2_000_000_000+i,
                          clock_domain='f1_ros_header_unverified'))
        for j, frame in enumerate(obs.frames))
    obs = replace(obs, frames=frames)
    fields = dict(camera_identity_source=CALLBACK_IDENTITY_SOURCE,
        camera_drop_counts={c: drop_count if c == 'head' else 0 for c in CAMERAS},
        camera_discontinuity=discontinuity) if declared else {}
    if extra:
        fields.update(extra)
    return MetadataObservation(obs, fields)


def write_episode(tmp_path, observations, *, payload_observations=None, legacy=False, controls=False):
    run = tmp_path/'raw'
    recorder = F1Recorder(run/'journal', episode_id='callback-test', task_prompt='offline identity audit',
        binding={'recording_mode':'hil-record', 'execution_mode':'powered' if controls else 'shadow'})
    sink = EpisodeVideoSink(run/'payload') if legacy else make_sink(run/'payload', 'hil-record')
    payload_observations = observations if payload_observations is None else payload_observations
    grip_time = observations[0].capture_started_monotonic_ns - 2
    for i, (obs, payload_obs) in enumerate(zip(observations, payload_observations)):
        assert sink.submit(payload_obs)
        assert recorder.record_timestamped_observation(obs, hil_state='hil_latched', intervention_id=1, policy_epoch=2)
        if controls:
            t = obs.capture_started_monotonic_ns - 1
            publications = [dict(topic_key='joint_command', monotonic_ns=t-1, publisher_gid='ab'*16)]
            if i == 0:
                publications += [dict(topic_key=key, monotonic_ns=grip_time, publisher_gid='ab'*16)
                    for key in ('left_gripper_command', 'right_gripper_command')]
            command = np.zeros(16)
            command[0] = i+1
            assert recorder.control(now=t, state='hil_latched', intervention=1, epoch=2,
                selected=command, submitted=command, source='human_arm', measured=np.zeros(16), sequence=i+1,
                publication_complete=True, execution_mode='powered',
                action_source_timestamps_ns=[t-1]*14+[grip_time]*2, publications=publications)
    assert sink.close() and recorder.close()
    return run


@pytest.mark.parametrize('legacy_payload', [False, True])
def test_new_callback_with_equal_header_stamp_is_auditable_and_reported(tmp_path, legacy_payload):
    obs = [callback_observation(i) for i in range(3)]
    assert len({item.frames[0].payload for item in obs}) == 3
    report = audit(write_episode(tmp_path, obs, legacy=legacy_payload))
    assert report['valid'], report
    assert report['camera_identity_source'] == CALLBACK_IDENTITY_SOURCE
    assert report['camera_duplicate_source_timestamps'] == dict.fromkeys(CAMERAS, 2)
    assert report['camera_drop_counts'] == dict.fromkeys(CAMERAS, 0)


def test_legacy_generated_ids_cannot_claim_same_stamp_is_a_new_camera_frame(tmp_path):
    report = audit(write_episode(tmp_path, [callback_observation(i, declared=False) for i in range(2)]))
    assert not report['valid'] and 'cached/repeated/regressed' in report['errors'][0]


@pytest.mark.parametrize('fault', ['id', 'stamp_regression', 'clock', 'identity', 'identity_missing', 'drop_regression', 'gap_hidden'])
def test_callback_identity_does_not_relax_freshness_or_provenance(tmp_path, fault):
    first, second = callback_observation(0), callback_observation(1)
    if fault == 'id':
        second = callback_observation(1, frame_ids=(50, 51, 51))
    elif fault == 'stamp_regression':
        frames = list(second.source.frames)
        frames[0] = replace(frames[0], timestamp=replace(frames[0].timestamp, value=1_999_999_999))
        second = MetadataObservation(replace(second.source, frames=tuple(frames)), second.extra)
    elif fault == 'clock':
        frames = list(second.source.frames)
        frames[0] = replace(frames[0], timestamp=replace(frames[0].timestamp, clock_domain='other_clock'))
        second = MetadataObservation(replace(second.source, frames=tuple(frames)), second.extra)
    elif fault == 'identity':
        second.extra['camera_identity_source'] = 'capture_counter'
    elif fault == 'identity_missing':
        second = callback_observation(1, declared=False)
    elif fault == 'drop_regression':
        first = callback_observation(0, drop_count=1, discontinuity=True)
    elif fault == 'gap_hidden':
        second = callback_observation(1, frame_ids=(52, 51, 51))
    assert not audit(write_episode(tmp_path, [first, second]))['valid']


def test_reused_host_receipt_is_not_new_even_with_new_callback_id(tmp_path):
    first = callback_observation(0)
    # Overlapping intervals keep receipt-in-window valid; the per-camera
    # strictly-increasing receipt rule must independently reject this replay.
    raw = observation(1, first.capture_started_monotonic_ns+1)
    frames = tuple(replace(frame, frame_id=51,
        host_received_monotonic_ns=old.host_received_monotonic_ns,
        timestamp=replace(frame.timestamp, value=2_000_000_000, clock_domain='f1_ros_header_unverified'))
        for frame, old in zip(raw.frames, first.frames))
    second = MetadataObservation(replace(raw, frames=frames), dict(first.extra))
    report = audit(write_episode(tmp_path, [first, second]))
    assert not report['valid'] and 'cached/repeated/regressed' in report['errors'][0]


@pytest.mark.parametrize('field, value', [
    ('camera_identity_source', 'other'), ('camera_drop_counts', dict.fromkeys(CAMERAS, 1)),
    ('camera_discontinuity', True), ('state_source', 'fabricated')])
def test_payload_cannot_replace_journal_callback_provenance(tmp_path, field, value):
    obs = [callback_observation(0)]
    payload = [MetadataObservation(obs[0].source, dict(obs[0].extra, **{field: value}))]
    report = audit(write_episode(tmp_path, obs, payload_observations=payload))
    assert not report['valid'] and 'mismatch' in report['errors'][0]


@pytest.mark.parametrize('boundary', ['sequence_gap', 'reported_drop'])
def test_training_window_never_crosses_callback_loss_with_same_epoch(tmp_path, boundary):
    obs = []
    for i in range(6):
        ids = ((50+i+(i >= 3), 100+i, 1000+i) if boundary == 'sequence_gap'
               else (50+i, 100+i, 1000+i))
        obs.append(callback_observation(i, frame_ids=ids, discontinuity=i == 3,
            drop_count=int(i >= 3) if boundary == 'reported_drop' else 0))
    run = write_episode(tmp_path, obs, controls=True)
    report = audit(run)
    assert report['valid'], report
    review = dict(contract=CONTRACT, manifest_sha256=report['manifest_sha256'],
                  reviewer='offline-test', accepted_interventions=[1])
    segments, _, _ = select_segments(run, review)
    assert [[row['observation_id'] for row in segment] for segment in segments] == [[0, 1, 2], [3, 4, 5]]
    assert report['camera_discontinuities'] == 1


@pytest.mark.parametrize('declared', [False, True])
def test_runtime_binding_cannot_be_replaced_by_a_different_observation_identity(tmp_path, declared):
    run = write_episode(tmp_path, [callback_observation(i, declared=declared, repeat_stamp=False) for i in range(2)])
    manifest_path = run/'journal/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['binding']['camera_identity_source'] = CALLBACK_IDENTITY_SOURCE
    manifest_path.write_text(json.dumps(manifest))
    report = audit(run)
    assert report['valid'] is declared, report
    if not declared:
        assert 'differs from runtime binding' in report['errors'][0]
