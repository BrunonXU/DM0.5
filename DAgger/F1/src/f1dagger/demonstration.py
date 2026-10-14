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


class DemonstrationRecorder:
    def __init__(self, root, runtime, binding, task, *, factory=None, audio=None):
        self.root, self.runtime = Path(root), Path(runtime)
        self.binding = dict(binding, recording_mode='hil-record', recording_boundary='f710_start_v1')
        self.task, self.factory = task, factory or self._create
        self.audio = audio
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
                            self.phase = 'recording'
                            if self.audio:
                                self.audio.notify('started')
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
                        print('F1 VIDEO queue pending recovery: '+str(exc), flush=True)
                    with self.lock:
                        self.last_saved = result
                        self.phase = 'idle'
                        if self.audio and not interrupted:
                            self.audio.notify('saved')
                    print(('F1 DEMONSTRATION interrupted; captured prefix saved: ' if interrupted
                           else 'F1 DEMONSTRATION saved: ')+str(run), flush=True)
            except Exception as exc:
                if opening:
                    for writer in reversed(opening):
                        try: writer.close(status='failed')
                        except Exception: pass
                    opening = None
                with self.lock:
                    self.error, self.phase = str(exc), 'failed'
                print('F1 DEMONSTRATION failed: '+str(exc), flush=True)
            finally:
                self.jobs.task_done()

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

    def record_event(self, *args, **kwargs):
        with self.lock:
            return self.active[0].record_event(*args, **kwargs) if self.active else True

    def control(self, **kwargs):
        with self.lock:
            if not self.active:
                return True
            if self.first_control and kwargs.get('submitted') is not None:
                keys = {p['topic_key'] for p in kwargs.get('publications', ())}
                if keys != {'joint_command', 'left_gripper_command', 'right_gripper_command'}:
                    # Worker can finish opening halfway through a HOLD tick.
                    # Wait for the owner's episode anchor; never seed action
                    # provenance with publications from a different episode.
                    return True
            self.first_control = False
            return self.active[0].control(**kwargs)

    # Payload admission is atomic with the observation journal above.
    def submit(self, obs): return True
    def trigger(self): return True

    def diagnostics(self):
        with self.lock:
            metrics = self.active[1].diagnostics() if self.active else dict(completed=0, count=0)
            return dict(metrics, phase=self.phase, episode_id=self.run_id,
                        last_saved=self.last_saved, writer_error=self.writer_error)

    def close(self, status='completed'):
        self.finish(time.monotonic_ns(), reason='process_exit', status=status)
        self.jobs.put(None)
        self.worker.join(60)
        if self.worker.is_alive():
            raise RuntimeError('episode finalizer still running; retain partial files')
        if self.audio:
            self.audio.close()
        return self.error is None
