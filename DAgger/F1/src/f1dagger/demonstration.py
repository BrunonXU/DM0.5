"""START-bounded episodes; disk creation/finalization never runs on control thread."""
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
import queue
import threading
import time
import uuid

from .recording import F1Recorder, make_sink
from .lifecycle import publish_completion
from .hardware import state_mask


class DemonstrationRecorder:
    def __init__(self, root, runtime, binding, task, *, factory=None, audio=None, unified_buttons=False):
        self.root, self.runtime = Path(root), Path(runtime)
        self.unified_buttons = unified_buttons
        self.epoch_floor = 0
        self.binding = dict(binding, recording_mode='hil-record', recording_boundary=binding.get(
            'recording_boundary', 'f710_start_up_v2' if unified_buttons else 'f710_start_v1'))
        self.task, self.factory = task, factory or self._create
        mask = state_mask(self.binding)
        self.anchor_topics = {'joint_command'} | {
            name for name, valid in zip(
                ('left_gripper_command', 'right_gripper_command'), mask[14:]) if valid}
        self.audio = audio
        self.audio_error = None
        self.report_error = None
        self.lock = threading.RLock()
        self.phase = 'idle'
        self.active = None
        self.run_id = None
        self.last_saved = None
        self.error = None
        self.begin_ns = None
        self.first_observation = True
        self.first_control = True
        self.jobs = queue.Queue(maxsize=4)
        self.worker = threading.Thread(target=self._work, name='f1-episode-finalizer', daemon=True)
        self.worker.start()

    def _create(self, run):
        sink = make_sink(run/'payload', 'hil-record')
        try:
            journal = F1Recorder(run/'journal', episode_id=run.name,
                                 task_prompt=self.task, binding=self.binding)
        except Exception:
            sink.close(status='failed')
            raise
        return journal, sink

    @property
    def writer_error(self):
        with self.lock:
            if self.active:
                journal, sink = self.active
                return self.error or journal.writer_error or sink.writer_error
            return self.error

    def start(self, now):
        with self.lock:
            if self.phase != 'idle':
                return False
            self.run_id = datetime.now(timezone.utc).strftime('f1_hil_%Y%m%dT%H%M%SZ_')+uuid.uuid4().hex[:8]
            self.phase = 'starting'
            self.jobs.put_nowait(('open', self.root/self.run_id, now))
            return True

    def finish(self, now, reason='start_button', status='completed', *, interrupted=False):
        with self.lock:
            if self.phase not in ('starting', 'recording'):
                return
            pair, self.active = self.active, None
            if pair:
                if not pair[0].record_event('demonstration_stopped', monotonic_ns=now, reason=reason):
                    status = 'failed'
            self.phase = 'saving'
            self.jobs.put_nowait(('close', self.root/self.run_id, (pair, status, reason, interrupted)))

    def _work(self):
        opening = None
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                kind, run, value = job
                if kind == 'open':
                    opening = self.factory(run)
                    with self.lock:
                        if self.phase == 'starting':
                            self.begin_ns = time.monotonic_ns()
                            if not opening[0].record_event('demonstration_started',
                                    monotonic_ns=self.begin_ns, requested_monotonic_ns=value):
                                raise RuntimeError('episode start journal rejected')
                            self.active, opening = opening, None
                            self.first_observation = True
                            self.first_control = True
                            # Writer admission succeeded. Queue its cue before
                            # exposing recording readiness to the control loop.
                            self._notify_audio('started')
                            self.phase = 'recording'
                else:
                    pair, status, reason, interrupted = value
                    pair = pair or opening
                    opening = None
                    publish_completion(self.runtime, run, self.binding, 'finalizing')
                    journal, sink = pair
                    if interrupted:
                        journal.binding = dict(journal.binding, termination=dict(
                            outcome='interrupted', reason=reason, requires_review=True))
                    errors = []
                    for writer in (sink, journal):
                        try:
                            if not writer.close(status=status):
                                raise RuntimeError('episode writer did not commit')
                        except Exception as exc:
                            errors.append(str(exc)); status = 'failed'
                    result = publish_completion(self.runtime, run, self.binding,
                        'saved' if status == 'completed' else 'failed', errors)
                    if result['phase'] != 'saved':
                        raise RuntimeError('episode save failed: '+str(result['errors']))
                    try:
                        from .interventions import enqueue_session
                        enqueue_session(run, self.runtime/'video-jobs')
                    except Exception as exc:
                        # Committed source remains available for queue recovery.
                        self._report('F1 VIDEO queue pending recovery: '+str(exc))
                    with self.lock:
                        self.last_saved = result
                        self.phase = 'idle'
                        if not interrupted:
                            self._notify_audio('saved')
                    self._report(('F1 EPISODE interrupted; captured prefix saved: ' if interrupted
                           else 'F1 EPISODE saved: ')+str(run))
            except Exception as exc:
                if opening:
                    for writer in reversed(opening):
                        try: writer.close(status='failed')
                        except Exception: pass
                    opening = None
                with self.lock:
                    self.error, self.phase = str(exc), 'failed'
                self._report('F1 EPISODE failed: '+str(exc))
            finally:
                self.jobs.task_done()

    def _report(self, text):
        try:
            print(text, flush=True)
        except Exception as exc:
            self.report_error = type(exc).__name__ + ": " + str(exc)

    def _notify_audio(self, kind):
        if self.audio:
            try:
                self.audio.notify(kind)
            except Exception as exc:
                self.audio_error = type(exc).__name__ + ': ' + str(exc)

    def record_timestamped_observation(self, obs, **context):
        # One admission lock covers BOTH streams: stop/rotation cannot split a
        # payload and its journal row. Already-in-flight pre-START frames drop.
        with self.lock:
            if not self.active or obs.capture_started_monotonic_ns < self.begin_ns:
                return True
            journal, sink = self.active
            if self.first_observation and getattr(obs, 'camera_identity_source', None):
                # Cumulative callback-drop counters predate this episode; mark
                # the new archive boundary without inventing callback frames.
                obs = replace(obs, camera_discontinuity=True)
            self.first_observation = False
            submitted = sink.submit(obs)
            recorded = journal.record_timestamped_observation(obs, **context)
            return submitted and recorded

    def record_chunk(self, obs, epoch, *args):
        with self.lock:
            if not self.active or obs.capture_started_monotonic_ns < self.begin_ns or epoch < self.epoch_floor:
                return True
            return self.active[0].record_chunk(obs, epoch, *args)

    def record_event(self, *args, **kwargs):
        with self.lock:
            if kwargs.get("policy_epoch", self.epoch_floor) < self.epoch_floor:
                return True
            return self.active[0].record_event(*args, **kwargs) if self.active else True

    def control(self, **kwargs):
        with self.lock:
            if not self.active:
                return True
            if self.first_control and kwargs.get('submitted') is not None:
                keys = {p['topic_key'] for p in kwargs.get('publications', ())}
                if keys != self.anchor_topics:
                    # Worker can finish opening halfway through a HOLD tick.
                    # Wait for the owner's episode anchor; never seed action
                    # provenance with publications from a different episode.
                    return True
            if kwargs.get('submitted') is not None:
                self.first_control = False
            return self.active[0].control(**kwargs)

    # Payload admission is atomic with the observation journal above.
    def submit(self, obs): return True
    def trigger(self): return True

    def diagnostics(self):
        with self.lock:
            metrics = self.active[1].diagnostics() if self.active else dict(completed=0, count=0)
            return dict(metrics, phase=self.phase, episode_id=self.run_id,
                        last_saved=self.last_saved, writer_error=self.writer_error,
                        audio_error=self.audio_error, report_error=self.report_error)

    def close(self, status='completed', termination=None):
        # Process/control failure is not evidence of corrupt storage. Writers
        # still independently reject drops, I/O errors or incomplete admission.
        if self.active and termination is not None:
            with self.lock:
                self.active[0].record_event('session_termination', monotonic_ns=time.monotonic_ns(),
                                            process_status=status, **termination)
        reason = 'process_exit' if status == 'completed' else 'process_failed'
        self.finish(time.monotonic_ns(), reason=reason, status='completed', interrupted=True)
        self.jobs.put(None)
        self.worker.join(60)
        if self.worker.is_alive():
            raise RuntimeError('episode finalizer still running; retain partial files')
        if self.audio:
            try:
                self.audio.close()
            except Exception as exc:
                self.audio_error = type(exc).__name__ + ': ' + str(exc)
        return self.error is None
