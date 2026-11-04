"""F1 journal format on the existing bounded asynchronous DAgger writer."""
import json
import os
import time
from pathlib import Path
import numpy as np
from dagger_core.hil_episode import EpisodeJournal
from dagger_core.recording_sinks import RingBufferSink, NullSink
from .contract import CAMERAS, CONTRACT, FEATURE_NAMES, vector, sha256
from .payload_archive import F1PayloadSink, FORMAT, STORAGE, read_payload

CALLBACK_IDENTITY_SOURCE = 'ros_callback_sequence_v1'

BLOCKERS = ['camera exposure clock mapping not certified', 'human correction quality not reviewed',
            'F1 observation/actual publication alignment requires export audit']


def control_integrity_error(manifest):
    """Powered observations without control evidence cannot be a complete episode."""
    counts = manifest.get('counts', {})
    if (manifest.get('binding', {}).get('execution_mode') == 'powered'
            and counts.get('observations', 0) > 0 and counts.get('control', 0) == 0):
        return 'powered episode has observations but no control records'
    return None


class F1Recorder(EpisodeJournal):
    schema_version = 3

    def __init__(self, *args, binding=None, **kwargs):
        self.receipt_error = None
        self._pending_receipt = None
        self._receipt_retry_at = 0.
        self.binding = binding or {}
        if self.binding.get('hardware_capabilities') is not None:
            self.schema_version = 4
        super().__init__(*args, **kwargs)

    def format_manifest(self, manifest):
        if self._pending_receipt is not None:
            try:
                self._write_local_receipt(('observations', 'policy_chunks', 'control', 'events'))
            except Exception as exc:
                self.receipt_error = type(exc).__name__ + ': ' + str(exc)
        integrity_error = control_integrity_error(dict(manifest, binding=self.binding))
        if integrity_error:
            manifest = dict(manifest, complete=False, structurally_auditable=False,
                            status='failed', integrity_errors=[integrity_error])
        payload = self.path.parent / 'payload'
        payload_hashes = {name: sha256(payload/name) for name in ('manifest.json','frames.jsonl') if (payload/name).is_file()}
        version = 4 if self.binding.get('hardware_capabilities') is not None else self.schema_version
        return dict(manifest, schema_version=version, contract=CONTRACT,
                    local_receipt_error=self.receipt_error,
                    payload_sha256=payload_hashes,
                    training_blockers=BLOCKERS, binding=self.binding,
                    action_contract=dict(dimension=16, names=FEATURE_NAMES, units=['degree']*14+['0..100']*2),
                    stream_sha256={name: sha256(self.partial_path / (name+'.jsonl'))
                                   for name in ('observations', 'policy_chunks', 'control', 'events')})

    def checkpoint_record(self, stream, record, handles):
        """Seal a journal prefix on the writer thread, without ending recording.

        Payloads have their own durable pack checkpoints. The consumer must
        verify every referenced image before promoting this receipt to replay.
        """
        if stream == 'events' and record.get('event') == 'rlt_local_closed':
            self._pending_receipt = record
            self._receipt_retry_at = 0.
        if self._pending_receipt is None or time.monotonic() < self._receipt_retry_at:
            return
        # Failure of RAW durability stays a writer failure. Derived receipt
        # creation below has its own retry/error state.
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
        try:
            self._write_local_receipt(handles)
        except Exception as exc:
            self.receipt_error = type(exc).__name__ + ': ' + str(exc)
            self._receipt_retry_at = time.monotonic() + 2.

    def _write_local_receipt(self, handles):
        files = {name: dict(bytes=(self.partial_path/(name+'.jsonl')).stat().st_size,
                           sha256=sha256(self.partial_path/(name+'.jsonl')))
                 for name in handles}
        receipt = dict(schema='f1_local_journal_prefix_v1', episode_id=self.episode_id,
                       binding=self.binding, task_prompt=self.task_prompt,
                       streams=files, local_result=self._pending_receipt, drop_count=self.drops,
                       training_ready=False)
        out = self.path.parent/'local_segments'
        out.mkdir(exist_ok=True)
        target = out/'placement-1.json'
        if target.exists():
            existing = json.loads(target.read_text())
            if existing.get('local_result') != self._pending_receipt:
                raise ValueError('Local placement was already sealed with a different result')
            self._pending_receipt = None
            self.receipt_error = None
            return
        temporary = target.with_suffix('.partial')
        with temporary.open('w') as handle:
            json.dump(receipt, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

        self._pending_receipt = None
        self.receipt_error = None

    def record_timestamped_observation(self, observation, *, hil_state, intervention_id, policy_epoch):
        return self._emit('observations', dict(event='observation', **observation.metadata(),
            monotonic_ns=observation.capture_started_monotonic_ns, hil_state=hil_state,
            intervention_id=intervention_id, policy_epoch=policy_epoch))

    def record_chunk(self, obs, epoch, request_id, started, response, actions, frequency, model_id):
        return self._emit('policy_chunks', dict(event='policy_chunk', policy_epoch=epoch,
            request_id=request_id, observation_id=obs.observation_id,
            observation_monotonic_ns=obs.capture_started_monotonic_ns,
            request_monotonic_ns=started, response_monotonic_ns=response,
            actions=np.asarray(actions).copy(), frequency_hz=frequency, model_id=model_id))

    def control(self, *, now, state, intervention, epoch, selected, submitted=None,
                source, measured, sequence, candidate=None, policy=None, **evidence):
        return self._emit('control', dict(event='control', monotonic_ns=now,
            hil_state=state, intervention_id=intervention, policy_epoch=epoch,
            selected=vector(selected, 16), submitted=None if submitted is None else vector(submitted, 16),
            measured=vector(measured, 16), source=source, command_sequence=sequence,
            human_candidate=None if candidate is None else vector(candidate, 16),
            policy_proposal=None if policy is None else vector(policy, 16), **evidence))


class F1PrebufferSink(RingBufferSink):
    def diagnostics(self):
        return dict(self.downstream.diagnostics(), prebuffer_active=self.active,
                    prebuffer_retained_bytes=self.retained_bytes,
                    prebuffer_capacity_evictions=self.capacity_evictions,
                    prebuffer_oversize_drops=self.oversize_drops)


def make_sink(path, mode):
    if mode == 'inference-only':
        return NullSink()
    if mode not in ('hil-record', 'prebuffer-record'):
        raise ValueError('unknown recording mode')
    writer = F1PayloadSink(path, training_blockers=BLOCKERS)
    if mode == 'hil-record':
        return writer
    if mode == 'prebuffer-record':
        return F1PrebufferSink(writer)


def read_rows(path):
    with Path(path).open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def audit(run):
    """Audit immutable evidence; no generated training approval."""
    run = Path(run).resolve()
    report = dict(valid=False, training_ready=False, errors=[], run=str(run))
    try:
        manifest = json.loads((run/'journal/manifest.json').read_text())
        payload_manifest = json.loads((run/'payload/manifest.json').read_text())
        for name in ('manifest.json','frames.jsonl'):
            if manifest.get('payload_sha256',{}).get(name) != sha256(run/'payload'/name):
                raise ValueError('payload index/manifest hash mismatch')
        if manifest.get('contract') != CONTRACT or manifest.get('schema_version') not in (3, 4):
            raise ValueError('not an F1 journal')
        integrity_error = control_integrity_error(manifest)
        if integrity_error:
            raise ValueError(integrity_error)
        from .hardware import state_mask
        mask = state_mask(manifest.get('binding', {}))
        missing = not all(mask)
        if (manifest['schema_version'] == 4) != missing:
            raise ValueError('Journal version does not match hardware capabilities')
        if missing and manifest['binding'].get('state_valid_mask') != list(mask):
            raise ValueError('Missing hardware mask binding')
        if not manifest.get('complete') or not payload_manifest.get('complete'):
            raise ValueError('incomplete journal or payload')
        packed = payload_manifest.get('format') == FORMAT
        if packed:
            if payload_manifest.get('schema_version') != 2 or payload_manifest.get('storage_version') != 1:
                raise ValueError('unsupported F1 image-pack schema/version')
            packs = payload_manifest.get('pack_files', {})
            if set(packs) != set(CAMERAS):
                raise ValueError('missing/unknown camera pack')
            for camera, pack in packs.items():
                if pack.get('path') != camera + '.pack':
                    raise ValueError('invalid camera pack path')
                image_pack = (run/'payload'/pack['path']).resolve()
                if (run/'payload').resolve() not in image_pack.parents:
                    raise ValueError('camera pack escaped archive')
                if image_pack.stat().st_size != pack['bytes'] or sha256(image_pack) != pack['sha256']:
                    raise ValueError('camera pack hash/size mismatch')
        elif payload_manifest.get('format') != 'native-image-sequence' or payload_manifest.get('schema_version') != 1:
            raise ValueError('unsupported F1 payload archive format')
        streams = {}
        for stream in ('observations', 'policy_chunks', 'control', 'events'):
            if sha256(run/'journal'/f'{stream}.jsonl') != manifest['stream_sha256'][stream]:
                raise ValueError('journal hash mismatch: ' + stream)
            rows = read_rows(run/'journal'/f'{stream}.jsonl')
            if len(rows) != manifest['counts'][stream]:
                raise ValueError('journal count mismatch: ' + stream)
            if any(r.get('stream_sequence') != i or r.get('schema_version') != manifest['schema_version'] for i, r in enumerate(rows)):
                raise ValueError('journal sequence/schema mismatch: ' + stream)
            streams[stream] = rows
        bound_identity_present = 'camera_identity_source' in manifest.get('binding', {})
        bound_identity = manifest.get('binding', {}).get('camera_identity_source')
        if bound_identity_present and bound_identity != CALLBACK_IDENTITY_SOURCE:
            raise ValueError('unsupported bound camera identity source')
        last_observation_time = -1
        camera_history = {}
        identity_source = None
        duplicate_source_timestamps = dict.fromkeys(CAMERAS, 0)
        last_drop_counts = dict.fromkeys(CAMERAS, 0)
        camera_discontinuities = 0
        for observation_index, row in enumerate(streams['observations']):
            declared_identity = row.get('camera_identity_source')
            if bound_identity_present and declared_identity != bound_identity:
                raise ValueError('observation camera identity differs from runtime binding')
            if 'camera_identity_source' in row and declared_identity != CALLBACK_IDENTITY_SOURCE:
                raise ValueError('unsupported camera identity source declaration')
            if observation_index == 0:
                identity_source = declared_identity
            elif declared_identity != identity_source:
                raise ValueError('camera identity source changed within episode')
            if identity_source == CALLBACK_IDENTITY_SOURCE:
                drop_counts = row.get('camera_drop_counts')
                discontinuity = row.get('camera_discontinuity')
                if (not isinstance(drop_counts, dict) or set(drop_counts) != set(CAMERAS)
                        or any(type(v) is not int or v < 0 for v in drop_counts.values())
                        or type(discontinuity) is not bool):
                    raise ValueError('invalid callback drop/discontinuity provenance')
                if any(drop_counts[c] < last_drop_counts[c] for c in CAMERAS):
                    raise ValueError('callback drop count regressed')
                if any(drop_counts[c] > last_drop_counts[c] for c in CAMERAS) and not discontinuity:
                    raise ValueError('callback drops lack discontinuity boundary')
                camera_discontinuities += int(discontinuity)
            vector(row['state16'], 16)
            vector(row['full22'], 22)
            begin, end = row['capture_started_monotonic_ns'], row['available_monotonic_ns']
            if not last_observation_time < begin <= end:
                raise ValueError('observation clock regressed')
            last_observation_time = begin
            if missing and (row.get('state_valid_mask') != list(mask) or row['state16'][14] != 0 or row.get('absent_state_semantics') != 'zero_storage_placeholder_not_measurement'):
                raise ValueError('Missing hardware observation provenance')
            if len(row['state_timestamps_ns']) != 16 or any((not 0 < t <= end) if valid else t != 0 for t, valid in zip(row['state_timestamps_ns'], mask)):
                raise ValueError('invalid measured-state provenance')
            if tuple(f['camera'] for f in row['frames']) != ('head','left_wrist','right_wrist'):
                raise ValueError('wrong camera order')
            if any(not begin <= f['host_received_monotonic_ns'] <= end for f in row['frames']):
                raise ValueError('invalid camera receipt interval')
            for frame in row['frames']:
                previous = camera_history.get(frame['camera'])
                current = (frame['frame_id'], frame['timestamp']['value'], frame['host_received_monotonic_ns'])
                if any(type(value) is not int for value in current) or current[0] < 0 or min(current[1:]) <= 0:
                    raise ValueError('invalid camera identity/receipt/source timestamp')
                clock = (frame['timestamp']['unit'], frame['timestamp']['clock_domain'])
                if identity_source == CALLBACK_IDENTITY_SOURCE:
                    if clock != ('ns', 'f1_ros_header_unverified') or row.get('state_source') != 'f1_ros_hal_and_omnipicker':
                        raise ValueError('callback identity lacks F1 ROS source provenance')
                if previous:
                    old, old_clock = previous
                    if clock != old_clock:
                        raise ValueError('camera source clock changed within episode')
                    # New archives bind frame_id to a real ROS callback, whose
                    # header stamp may repeat across different sensor images.
                    # Legacy IDs were generated by capture, so retain the old
                    # strictly-new source-stamp requirement for those records.
                    if (current[0] <= old[0] or current[2] <= old[2]
                            or current[1] < old[1] or (identity_source is None and current[1] == old[1])):
                        raise ValueError('cached/repeated/regressed camera observation')
                    if (identity_source == CALLBACK_IDENTITY_SOURCE and current[0] != old[0] + 1
                            and not discontinuity):
                        raise ValueError('callback sequence gap lacks discontinuity boundary')
                    if current[1] == old[1]:
                        duplicate_source_timestamps[frame['camera']] += 1
                camera_history[frame['camera']] = (current, clock)
            if identity_source == CALLBACK_IDENTITY_SOURCE:
                last_drop_counts = drop_counts
        observations = {r['observation_id']: r for r in streams['observations']}
        if len(observations) != len(streams['observations']):
            raise ValueError('duplicate observation IDs')
        payload = read_rows(run/'payload/frames.jsonl')
        if len(payload) != payload_manifest['count']:
            raise ValueError('payload count mismatch')
        payload_ids = set()
        pack_offsets = dict.fromkeys(CAMERAS, 0)
        for archive_index, row in enumerate(payload):
            oid = row['observation_id']
            if oid in payload_ids or oid not in observations:
                raise ValueError('duplicate/unbound image observation')
            payload_ids.add(oid)
            obs = observations[oid]
            vector(obs['state16'], 16)
            if (('camera_identity_source' in row) != ('camera_identity_source' in obs)
                    or row.get('camera_identity_source') != obs.get('camera_identity_source')
                    or row.get('state_source') != obs.get('state_source')):
                raise ValueError('payload and journal camera identity/source declaration mismatch')
            if missing and (row.get('state_valid_mask') != list(mask) or row.get('absent_state_semantics') != obs.get('absent_state_semantics')):
                raise ValueError('Payload missing-hardware metadata differs from journal')
            if obs.get('camera_identity_source') == CALLBACK_IDENTITY_SOURCE:
                if (row.get('camera_drop_counts') != obs['camera_drop_counts']
                        or type(row.get('camera_discontinuity')) is not bool
                        or row['camera_discontinuity'] != obs['camera_discontinuity']):
                    raise ValueError('payload and journal callback continuity provenance mismatch')
            if row['state16'] != obs['state16'] or len(row['frames']) != 3:
                raise ValueError('payload and observation mismatch')
            for frame, recorded in zip(row['frames'], obs['frames']):
                for key in ('camera', 'frame_id', 'timestamp', 'host_received_monotonic_ns'):
                    if frame[key] != recorded[key]:
                        raise ValueError('camera identity mismatch')
                if frame.get('archive_frame_index') != archive_index:
                    raise ValueError('archive frame index gap')
                if packed:
                    camera = frame['camera']
                    if frame.get('storage') != STORAGE or frame.get('payload_offset') != pack_offsets[camera]:
                        raise ValueError('camera pack offset gap/overlap/storage mismatch')
                    pack_offsets[camera] += frame['payload_bytes']
                elif frame.get('storage') is not None:
                    raise ValueError('image sequence storage mismatch')
                read_payload(run/'payload', frame)
        mode = manifest.get('binding', {}).get('recording_mode')
        recorded_ids = [row['observation_id'] for row in payload]
        observation_ids = list(observations)
        if mode == 'hil-record' and recorded_ids != observation_ids:
            raise ValueError('full recording lacks ordered image coverage for every observation')
        if mode == 'prebuffer-record' and recorded_ids and recorded_ids != observation_ids[-len(recorded_ids):]:
            raise ValueError('prebuffer image coverage is not a continuous observation suffix')
        if packed and any(pack_offsets[c] != packs[c]['bytes'] for c in CAMERAS):
            raise ValueError('unindexed camera pack bytes')
        last_command = -1
        published_values, published_times = [None]*16, [None]*16
        last_time = -1
        for row in streams['control']:
            if row['command_sequence'] <= last_command or row['monotonic_ns'] <= last_time:
                raise ValueError('control sequence/time regressed')
            last_command, last_time = row['command_sequence'], row['monotonic_ns']
            vector(row['selected'], 16)
            vector(row['measured'], 16)
            if row['submitted'] is not None:
                vector(row['submitted'], 16)
                if row.get('publication_complete') is not True or row.get('execution_mode') != 'powered':
                    raise ValueError('submitted command lacks real complete publication evidence')
                stamps = row.get('action_source_timestamps_ns', [])
                if missing and (row.get('action_valid_mask') != list(mask) or row.get('state_valid_mask') != list(mask) or row['submitted'][14] != 0 or row['measured'][14] != 0):
                    raise ValueError('Missing hardware control provenance')
                if len(stamps) != 16 or any((not isinstance(t,int) or not 0 < t <= row['monotonic_ns']) if valid else t is not None for t, valid in zip(stamps, mask)):
                    raise ValueError('invalid per-field publication times')
                pubs = row.get('publications', [])
                if not pubs or pubs[0].get('topic_key') != 'joint_command':
                    raise ValueError('arm publication evidence missing')
                seen = set()
                for publication in pubs:
                    key = publication['topic_key']
                    gid = publication.get('publisher_gid')
                    stamp = publication['monotonic_ns']
                    if key in seen or key not in ('joint_command','left_gripper_command','right_gripper_command'):
                        raise ValueError('duplicate/unknown command publication')
                    if missing and key == 'left_gripper_command':
                        raise ValueError('Publication to absent left gripper')
                    seen.add(key)
                    if not gid or not any(bytes.fromhex(gid)) or not last_time-50_000_000 <= stamp <= last_time:
                        raise ValueError('publication identity/time missing')
                    indices = range(14) if key == 'joint_command' else [14 if key == 'left_gripper_command' else 15]
                    for i in indices:
                        published_times[i] = stamp
                        published_values[i] = row['submitted'][i]
                if published_times != stamps or any(published_values[i] != row['submitted'][i] for i, valid in enumerate(mask) if valid):
                    raise ValueError('action differs from current/held publication evidence')
        for row in streams['policy_chunks']:
            obs = observations[row['observation_id']]
            if not obs['capture_started_monotonic_ns'] <= row['request_monotonic_ns'] <= row['response_monotonic_ns']:
                raise ValueError('noncausal policy request')
            a = np.asarray(row['actions'], dtype=float)
            if a.ndim != 2 or a.shape[1] != 16 or not np.isfinite(a).all():
                raise ValueError('invalid policy actions')
        report.update(valid=True, counts=manifest['counts'], payload_observations=len(payload),
                      observations_without_payload=len(observations)-len(payload_ids),
                      camera_identity_source=identity_source or 'legacy_strict_source_stamp',
                      camera_duplicate_source_timestamps=duplicate_source_timestamps,
                      camera_drop_counts=last_drop_counts if identity_source else None,
                      camera_discontinuities=camera_discontinuities if identity_source else None,
                      manifest_sha256=sha256(run/'journal/manifest.json'))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        report['errors'].append(str(exc))
    return report
