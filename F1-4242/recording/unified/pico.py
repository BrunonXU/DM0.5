"""Pico source adapter for the existing subscriber, with no inline video encoding."""
import json
import time
import uuid
from pathlib import Path
import numpy as np
from .capture import CaptureWriter, commit_capture, read_capture
from .contract import CAMERAS, CONTRACT, atomic_json

KEYS = ('observation.images.base_0_rgb', 'observation.images.left_wrist_0_rgb', 'observation.images.right_wrist_0_rgb')
ALIASES = dict(zip(KEYS, CAMERAS))


class RawDataset:
    def __init__(self, root, *args, **kwargs):
        self.root = Path(root)

    def commit(self, path, arrays, video_info):
        meta = json.loads((Path(path)/'capture.json').read_text())
        target = commit_capture(path, self.root/meta['capture_id'])
        # Successful raw save does not mean approval or successful v3 conversion.
        return 0, target


class RawCapture:
    def __init__(self, root, config, camera_info):
        self.config = dict(config); self.camera_info = camera_info
        self.started_monotonic = time.monotonic()
        self.state = []; self.stamps = []; self.rows = []
        self.dropped = self.alignment_extra_count = 0; self.source_gaps = []
        self.session = config['recording_session_id']
        self.writer = CaptureWriter(Path(root)/'.staging', dict(
            capture_id='pico_'+time.strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8],
            collection_method='pico', session_id=self.session, task=config['task'],
            fps=config['fps'], execution_mode='powered', started_unix=time.time(),
            source_basis=config['source_basis'], pico_session_identity=config['pico_session_identity'],
            pico_source_proof=config['pico_source_proof'], profile_sha256=config.get('profile_sha256'),
            machine_id=config.get('machine_id')), queue_size=int(config.get('raw_queue_frames',90)))
        self.path = self.writer.path

    @property
    def frame_count(self): return self.writer.submitted

    def add(self, state, action, timestamp_ns, cameras, timing):
        receipt = timing['camera_arrival_timestamp_ns']
        ids = timing['camera_frame_ids']
        source_stamps = timing['camera_original_timestamp_ns']
        reference = max(receipt)
        row = dict(contract=CONTRACT, time_ns=reference, clock_basis='host_monotonic_receipt',
            session_id=self.session, state=np.asarray(state).tolist(), action=np.asarray(action).tolist(),
            action_valid=[True]*16, action_source='verified_pico_session_and_graph',
            control_source='pico_human', phase='human', execution_mode='powered',
            owner_epoch=self.config['pico_source_epoch'], intervention_id=0,
            state_max_age_ns=int(timing['state_max_age_ns']), arm_action_max_age_ns=int(timing['action_max_age_ns']),
            timing=dict(timing), source_ros_reference_ns=timestamp_ns,
            images={camera: dict(frame_id=ids[i], received_ns=receipt[i],
                    original_timestamp_ns=source_stamps[i], original_clock='ros_header_unverified',
                    **self.camera_info[KEYS[i]]) for i,camera in enumerate(CAMERAS)})
        self.writer.append(row, {ALIASES[k]: cameras[k][0] for k in KEYS})
        self.state.append(state); self.stamps.append(timestamp_ns); self.rows.append(row)

    def add_alignment_extra(self, key, frame, reason='alignment'):
        self.writer.append_extra(ALIASES[key], dict(frame_id=frame[5], received_ns=frame[2],
            original_timestamp_ns=frame[7] if len(frame)>7 else frame[6], reason=reason), frame[0])
        self.alignment_extra_count += 1

    def note_source_gaps(self, gaps): self.source_gaps.append(gaps)

    def stop(self):
        self.writer.metadata["quality"] = dict(source_gaps=self.source_gaps, alignment_extras=self.alignment_extra_count)
        self.writer.close(commit=False)
        atomic_json(self.path/'quality.json', dict(source_gaps=self.source_gaps,
            alignment_extras=self.alignment_extra_count, training_ready=False))
        stamps = np.array([r['time_ns'] for r in self.rows],dtype=np.int64)
        delta = np.diff(stamps)
        timing = dict(training_ready=False, raw_save_allowed=True,
            coverage_fault='Raw capture needs offline selection, official v3 readback and human review',
            capture_fps=(len(stamps)-1)*1e9/(stamps[-1]-stamps[0]) if len(stamps)>1 else 0,
            max_camera_skew_ms=max((max(r['images'][c]['received_ns'] for c in CAMERAS)-min(r['images'][c]['received_ns'] for c in CAMERAS))/1e6 for r in self.rows),
            max_camera_integrity_gap_ms=float(max(delta,default=0))/1e6,
            max_camera_arrival_gap_ms=float(max(delta,default=0))/1e6,
            max_state_age_ms=max(r['state_max_age_ns'] for r in self.rows)/1e6,
            max_action_age_ms=max(r['arm_action_max_age_ns'] for r in self.rows)/1e6)
        arrays=dict(timestamp_ns=np.array(self.stamps,dtype=np.int64),state=np.array(self.state))
        media={k:dict(**self.camera_info[k],fps=float(self.config['fps']),frames=len(stamps),codec='original_jpeg_pack') for k in KEYS}
        return arrays,media,timing

    def preserve_failure(self, reason):
        try: self.writer.close(commit=False)
        finally:
            atomic_json(self.path/'failure.json',dict(error=reason,training_ready=False))

    def abort(self, preserve=True):
        try: self.writer.close(commit=False)
        except RuntimeError: pass
