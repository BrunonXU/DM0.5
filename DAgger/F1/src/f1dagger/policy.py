"""Bounded multipart transport and the shared DAgger scheduler's F1 hooks."""
from dataclasses import replace
import json
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import numpy as np
from dagger_core.powered_hil_policy import ChunkPolicy
from dagger_core.hil_supervisor import HILState
from .contract import CONTRACT
from .observation import CameraUnavailable, input_issue, frames_advance


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('policy redirects are not allowed')


class HttpPolicy:
    def __init__(self, config, task, validate_action, *, opener=None, clock=time.monotonic_ns):
        self.config, self.task, self.validate_action, self.clock = config, task, validate_action, clock
        parts = urlsplit(config['endpoint'])
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('configure a credential-free F1 policy endpoint before policy mode')
        self.endpoint = config['endpoint'].rstrip('/')
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.model_id = None
        self.model_version = None

    def read(self, request):
        with self.opener.open(request, timeout=self.config['timeout_s']) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('policy response too large')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('contract') != CONTRACT:
            raise ValueError('wrong policy contract (G2 and whole-body F1 are not upper16)')
        return result

    def health(self):
        result = self.read(Request(self.endpoint + '/health'))
        identity = result.get('model_id')
        if result.get('ready') is not True or not isinstance(identity, str) or not identity:
            raise ValueError('policy not ready or model identity missing')
        if self.config.get('model_id') and identity != self.config['model_id']:
            raise ValueError('configured model identity mismatch')
        if self.config.get('require_capabilities',False) or 'capabilities' in result:
            from .session import validate_health
            validate_health(result,self.config)
        self.model_version = result.get('model_version')
        self.model_id = identity
        return result

    def infer(self, obs, request_id, epoch):
        if self.model_id is None:
            raise ValueError('policy health/binding has not been checked')
        metadata = dict(contract=CONTRACT, request_id=request_id, observation_id=obs.observation_id,
            policy_epoch=epoch, observation_monotonic_ns=obs.capture_started_monotonic_ns,
            state16=list(obs.state16), task=self.task,
            chunk_steps=self.config['chunk_steps'], frequency_hz=self.config['frequency_hz'])
        boundary = 'f1-dagger-' + uuid.uuid4().hex
        fields = [('metadata', json.dumps(metadata, allow_nan=False).encode(), 'application/json')]
        fields += [(f.camera, f.payload, f.media_type) for f in obs.frames]
        body = []
        for name, data, mime in fields:
            disposition = f'form-data; name="{name}"' + ('' if name == 'metadata' else f'; filename="{name}.image"')
            body += [f'--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: {mime}\r\n\r\n'.encode(), data, b'\r\n']
        body.append(f'--{boundary}--\r\n'.encode())
        result = self.read(Request(self.endpoint + '/process_frame', data=b''.join(body),
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST'))
        return self.validate(result, metadata, obs)

    def validate(self, result, metadata, obs):
        for key in ('contract', 'request_id', 'observation_id', 'policy_epoch', 'observation_monotonic_ns', 'frequency_hz'):
            if type(result.get(key)) is not type(metadata[key]) and key != 'frequency_hz':
                raise ValueError('policy identity type mismatch: ' + key)
            if result.get(key) != metadata[key]:
                raise ValueError('policy identity mismatch: ' + key)
        if self.model_version is not None and result.get('model_version') != self.model_version:
            raise ValueError('policy version changed during session')
        if result.get('model_id') != self.model_id:
            raise ValueError('policy model changed during session')
        age = self.clock() - obs.capture_started_monotonic_ns
        if not 0 <= age <= self.config['max_age_s'] * 1e9:
            raise ValueError('policy observation expired')
        raw_actions = result.get('actions')
        if not isinstance(raw_actions, list) or any(not isinstance(row,list) or any(type(v) not in (int,float) for v in row) for row in raw_actions):
            raise ValueError('actions require numeric Hx16 arrays')
        actions = np.asarray(raw_actions, dtype=float)
        if actions.shape != (self.config['chunk_steps'], 16) or not np.isfinite(actions).all():
            raise ValueError('policy must return complete finite Hx16')
        for row in actions:
            self.validate_action(row)
        return actions.copy()


class F1PolicyEngine(ChunkPolicy):
    def __init__(self, emit, source, policy, recorder, sink, config, *, teleop_only=False, episodes=None):
        if episodes is not None and not teleop_only:
            raise ValueError('START-bounded recording is only for manual demonstration')
        self.policy = policy
        self.episodes = episodes
        self.teleop_only = teleop_only
        self.policy_enabled = not teleop_only
        self._capture_deadline_ns = None
        self.camera_issue = None
        self._camera_fault_pending = None
        self._camera_was_missing = False
        super().__init__(emit, source, None, recorder, sink,
            action_dim=16, chunk_steps=config['policy']['chunk_steps'], waypoint_hz=config['policy']['frequency_hz'],
            input_validator=self.policy_input_issue, frame_advancer=frames_advance,
            capture_hz=config['capture_hz'], request_period=1. / config['capture_hz'])

    def _capture_loop(self):
        """F1 FIFO acquisition on a stable grid; the shared G2 loop is unchanged."""
        try:
            while not self.stop_event.is_set():
                with self.context_lock:
                    started = time.monotonic_ns()
                    state, epoch, intervention = (self.supervisor.state.value,
                        self.supervisor.policy_epoch, self.supervisor.intervention_id)
                try:
                    obs = self.source.capture(started)
                except CameraUnavailable as exc:
                    with self.context_lock:
                        if self.camera_issue is None:
                            self._camera_fault_pending = str(exc)
                        self.camera_issue = str(exc)
                    self._capture_deadline_ns = None
                    self.stop_event.wait(.02)
                    continue
                with self.context_lock:
                    self.camera_issue = None
                # Preserve the shared loop's single-owner prebuffer/admission
                # order and immutable observation-to-epoch binding.
                if self.record_trigger.is_set():
                    triggered = self.sink.trigger()
                    if self.strict_recording and not triggered:
                        raise RuntimeError('prebuffer sink rejected trigger')
                    self.record_trigger.clear()
                submitted = self.sink.submit(obs)
                recorded = self.recorder.record_timestamped_observation(obs, hil_state=state,
                    intervention_id=intervention, policy_epoch=epoch)
                if self.strict_recording and (not submitted or not recorded):
                    raise RuntimeError('observation sink/journal rejected record')
                with self.latest_lock:
                    self.latest = (obs, epoch)
                self._wait_for_capture(started)
        except Exception as exc:
            self.acquisition_error = f'{type(exc).__name__}: {exc}'

    def tick(self, now):
        with self.context_lock:
            fault = self._camera_fault_pending
            if fault is not None:
                self._camera_fault_pending = None
                self._camera_was_missing = True
                self.emit('camera_acquisition_interrupted', monotonic_ns=now, reason=fault)
                # Keep hardware HOLD/re-authorization semantics. Only camera
                # wait is retryable; disk, feedback and clock faults stay fatal.
                self.disconnect(now, 'camera_unavailable:' + fault)
            if self._camera_was_missing and self.camera_issue is None:
                self._camera_was_missing = False
                self.emit('camera_acquisition_restored', monotonic_ns=now,
                          requires_new_start=True)
        return super().tick(now)

    def _wait_for_capture(self, started):
        # Late wakeups do not move the time grid. Catch-up still calls capture
        # for distinct real callbacks; it never repeats an observation/action.
        origin = started if self._capture_deadline_ns is None else self._capture_deadline_ns
        self._capture_deadline_ns = origin + round(self.capture_period * 1e9)
        remaining = max(0, self._capture_deadline_ns-time.monotonic_ns())
        self.stop_event.wait(remaining/1e9)

    def policy_input_issue(self, obs):
        issue = input_issue(obs)
        if issue:
            return issue
        # Camera acquisition and feedback callbacks are independent. New images
        # alone do not prove state16 was received after START / policy handoff.
        # Keep recording at its normal cadence while waiting for all 16 fields;
        # do not relabel cached feedback with the capture timestamp.
        if any(t <= self.minimum_observation_ns for t in obs.state_timestamps_ns):
            return 'state_not_received_after_control_boundary'
        return None

    def _infer(self, obs, epoch, request_id):
        started = time.monotonic_ns()
        actions = self.policy.infer(obs, request_id, epoch)
        response = time.monotonic_ns()
        self.record_chunk(obs, epoch, request_id, started, response, actions)
        return epoch, request_id, obs.observation_id, response, actions

    def record_chunk(self, obs, epoch, request_id, started, response, actions):
        if not self.recorder.record_chunk(obs, epoch, request_id, started, response, actions,
                                          self.waypoint_hz, self.policy.model_id):
            raise RuntimeError('F1 policy journal full/failed')

    def sampling_metadata(self, lower, upper, alpha):
        return dict(kind='linear_anchor', source_hz=self.waypoint_hz,
            chunk_started_monotonic_ns=self.chunk_started_ns, lower_index=lower, upper_index=upper, alpha=alpha,
            anchor=self.chunk_anchor.tolist() if lower == -1 else None,
            raw_waypoint=self.chunk[self.action_index].tolist())

    def operator(self, packet, received_ns, now, *, hand_pending=False):
        if self.camera_issue is not None or self._camera_fault_pending is not None:
            # Consume input edges while unavailable, never queue a START for
            # later automatic motion. Do not open a new demonstration episode.
            super().operator(packet, received_ns, now, hand_pending=hand_pending)
            super().disconnect(now, 'camera_unavailable:waiting_for_fresh_cameras')
            return False
        was_started = self.started
        before = self.supervisor.state
        before_reason = self.supervisor.hold_reason
        changed = super().operator(packet, received_ns, now, hand_pending=hand_pending)
        if self.teleop_only and not was_started and self.started:
            self.supervisor.state = HILState.HIL_LATCHED
            self.supervisor.policy_epoch += 1
            self.supervisor.intervention_id += 1
            self.record_trigger.set()
            self._record_update(self.supervisor._update(False, False,
                transition='teleop_started', reason='first_start'), now)
            changed = True
        if self.episodes and changed:
            if self.supervisor.state is HILState.HIL_LATCHED:
                if not self.episodes.start(now):
                    # A START during finalization is consumed, never deferred
                    # into automatic control authorization when saving ends.
                    self.supervisor.state = before if was_started else HILState.HANDOFF_HOLD
                    self.supervisor.hold_reason = before_reason
            elif before is HILState.HIL_LATCHED:
                self.episodes.finish(now)
        return changed

    def disconnect(self, now, reason='operator_disconnected'):
        super().disconnect(now, reason)
        if self.episodes:
            self.episodes.finish(now, reason=reason,
                                 interrupted=reason.startswith('camera_unavailable:'))

    def close(self):
        self.source.stop_event.set()
        super().close()
