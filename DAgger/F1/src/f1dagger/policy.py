"""Bounded multipart transport and the shared DAgger scheduler's F1 hooks."""
from dataclasses import replace, asdict
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
from .input_recovery import InputTiming
from .faults import FeedbackUnavailable, transient_control_reason


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

    def validate_identity(self, result, metadata, obs):
        # Transport/server failures have no action identity to validate. Reject
        # them explicitly instead of hiding the actual error behind `contract`.
        # This is an unverified diagnostic, never permission to accept actions.
        if isinstance(result, dict) and result.get('success') is False and result.get('error'):
            raise ValueError('policy request failed (unverified response): ' + str(result['error'])[:500])
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

    def validate(self, result, metadata, obs):
        self.validate_identity(result, metadata, obs)
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
        self.unified_episodes = bool(episodes is not None and getattr(episodes, 'unified_buttons', False))
        self.episode_buttons = None
        if episodes is not None and not teleop_only and not self.unified_episodes:
            raise ValueError('START-bounded recording is only for manual demonstration')
        self.policy = policy
        self.episodes = episodes
        self.teleop_only = teleop_only
        self.fault_lifecycle = config.get('fault_lifecycle') == 'f1_fault_lifecycle_v1'
        self.rtc_timing_replan = config.get('rtc_timing_replan', False) is True
        self.runtime_fault = None
        self.policy_enabled = not teleop_only
        self._capture_deadline_ns = None
        self.camera_issue = None
        self._camera_fault_pending = None
        self._camera_was_missing = False
        self.input_pause = None
        self.startup_policy_authorized = False
        self.manual_recovery_guard = None
        self.manual_recovery_anchor = None
        # A transport event must never replace a collision/protocol/feedback
        # fault with the less restrictive RB recovery path.
        self.hard_hold_reason = None
        self.recovery_proof = None
        self.input_timing = InputTiming()
        super().__init__(emit, source, None, recorder, sink,
            action_dim=16, chunk_steps=config['policy']['chunk_steps'], waypoint_hz=config['policy']['frequency_hz'],
            input_validator=self.policy_input_issue, frame_advancer=frames_advance,
            capture_hz=config['capture_hz'], request_period=1. / config['capture_hz'])
        if self.unified_episodes:
            from .episode_controls import EpisodeButtons
            self.episode_buttons = EpisodeButtons(self)

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
                except (CameraUnavailable, FeedbackUnavailable) as exc:
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
                        from .faults import RecordingUnavailable
                        raise RecordingUnavailable('prebuffer sink rejected trigger')
                    self.record_trigger.clear()
                submitted = self.sink.submit(obs)
                recorded = self.recorder.record_timestamped_observation(obs, hil_state=state,
                    intervention_id=intervention, policy_epoch=epoch)
                if self.strict_recording and (not submitted or not recorded):
                    from .faults import RecordingUnavailable
                    raise RecordingUnavailable('observation sink/journal rejected record')
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
                # Missing camera/feedback waits preserve the task. Invalid
                # clocks and raw writer faults still latch motion supervision.
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
        # Require every present field after the control boundary. Explicitly
        # absent hardware has no callback and must never receive a fake timestamp.
        from .hardware import validate_mask
        mask = validate_mask(getattr(obs, 'state_valid_mask', None))
        if any(valid and t <= self.minimum_observation_ns
               for t, valid in zip(obs.state_timestamps_ns, mask)):
            return 'state_not_received_after_control_boundary'
        return None

    def _infer(self, obs, epoch, request_id):
        started = time.monotonic_ns()
        error = None
        try:
            actions = self.policy.infer(obs, request_id, epoch)
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)[:500]
            raise
        finally:
            timing = getattr(self.policy, 'last_timing', {})
            if timing.get('request_id') == request_id and timing.get('policy_epoch') == epoch:
                self.emit('policy_transport_timing', request_id=request_id, observation_id=obs.observation_id,
                          policy_epoch=epoch, timing=timing, error=error)
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
        with self.context_lock:
            if self.episode_buttons:
                return self.episode_buttons.operator(packet, received_ns, now, hand_pending)
            return self._operator(packet, received_ns, now, hand_pending=hand_pending)

    def _operator(self, packet, received_ns, now, *, hand_pending=False):
        key = (packet.session_id, packet.sequence)
        if key == self.last_operator_key:
            return False
        self.input_timing.observe(packet, received_ns, now)
        # Detect the gap BEFORE a new arrival overwrites its evidence.
        if (self.last_received_ns is not None and now-self.last_received_ns > self.watchdog_ns
                and self.supervisor.state is HILState.HIL_LATCHED):
            self.disconnect(now, 'operator_watchdog')
        if packet.warning == 'controller_disconnected':
            self.disconnect(now, 'controller_disconnected')
            self.last_operator_key = key
            return False
        if (self.started and self.packet is not None
                and packet.session_id != self.packet.session_id and self.input_pause is None):
            # The receiver permits only one sender session per TCP connection.
            # A replacement connection can be observed in the same main-loop
            # tick as its EOF; always re-anchor before considering its buttons.
            self.disconnect(now, 'operator_session_changed')
        if self.input_pause is not None:
            return self._paused_operator(packet, received_ns, now, hand_pending=hand_pending)
        if self.camera_issue is not None or self._camera_fault_pending is not None:
            # Consume input edges while unavailable, never queue a START for
            # later automatic motion. Do not open a new demonstration episode.
            super().operator(packet, received_ns, now, hand_pending=hand_pending)
            super().disconnect(now, self.hard_hold_reason or 'camera_unavailable:waiting_for_fresh_cameras')
            return False
        was_started = self.started
        before = self.supervisor.state
        before_reason = self.supervisor.hold_reason
        changed = super().operator(packet, received_ns, now, hand_pending=hand_pending)
        if self.supervisor.state is HILState.SAFE_HOLD and self.input_pause is None:
            # The shared supervisor can reject a toggle gap/regression directly,
            # without calling this class's disconnect hook. Such faults are not
            # a successful operator STOP and must retain the hard-fault latch.
            reason = self.hard_hold_reason or self.supervisor.hold_reason or 'operator_rejected'
            self._disconnect(now, reason)
            self.supervisor.hold_reason = reason
            return changed
        if self.teleop_only and not was_started and self.started:
            self.supervisor.state = HILState.HIL_LATCHED
            self.supervisor.policy_epoch += 1
            self.supervisor.intervention_id += 1
            self.record_trigger.set()
            self._record_update(self.supervisor._update(False, False,
                transition='teleop_started', reason='first_start'), now)
            changed = True
        if self.episodes and not self.unified_episodes and changed:
            if self.supervisor.state is HILState.HIL_LATCHED:
                if self.episodes.phase in ('starting', 'recording'):
                    # Mode authorization cannot create a second recording or
                    # reinterpret START as a new episode while one is active.
                    self.supervisor.state = HILState.HANDOFF_HOLD
                    self.supervisor.hold_reason = 'demonstration_finished'
                    self.episodes.finish(now)
                    self.invalidate(now)
                elif not self.episodes.start(now):
                    # A START during finalization is consumed, never deferred
                    # into automatic control authorization when saving ends.
                    self.supervisor.state = before if was_started else HILState.HANDOFF_HOLD
                    self.supervisor.hold_reason = before_reason
            elif before is HILState.HIL_LATCHED:
                self.episodes.finish(now)
        if changed and self.supervisor.state is HILState.HIL_LATCHED:
            # Clear only after explicit START was accepted by every gate,
            # including episode startup. START consumed during saving is not
            # authorization to erase the latched hardware/protocol fault.
            self.hard_hold_reason = None
        return changed

    def disconnect(self, now, reason='operator_disconnected'):
        with self.context_lock:
            return self._disconnect(now, reason)

    def pause_runtime_condition(self, now, reason, details=None):
        """Revoke motion, preserve the task, and require explicit re-authorization."""
        with self.context_lock:
            if self.hard_hold_reason is not None:
                return False
            self.policy_enabled = False
            if self.input_pause is not None:
                self.input_pause.update(neutral=None, proof_after=now, previous_rb=True)
                return True
            if self.packet is not None and self.started:
                self._start_manual_pause(now, reason, invalid_from=now, details=details,
                    takeover_required=self.supervisor.state is not HILState.HIL_LATCHED)
            else:
                super().disconnect(now, reason)
            return True

    def pause_manual_candidate(self, now, details):
        """Only a pre-publication, independently checked candidate may call this."""
        with self.context_lock:
            if (self.hard_hold_reason is not None or self.packet is None
                    or self.supervisor.state is not HILState.HIL_LATCHED):
                return False
            self._start_manual_pause(now, 'manual_candidate_collision', invalid_from=now,
                                     details=details)
            return True

    def pause_stationary_output(self, now, details):
        """Runtime verified a disabled, stationary, safe held pose, not motion.

        Policy HOLD still belongs to the policy: only a new START may transfer
        that ownership. An existing pause retains its original cause/session.
        """
        with self.context_lock:
            if self.hard_hold_reason is not None or self.packet is None:
                return False
            pause = self.input_pause
            if pause is not None:
                # A second timing fault cannot erase a collision/transport
                # cause, pending START, retired session, or release handshake.
                self.manual_recovery_anchor = None
                pause.update(neutral=None, neutral_ready=False, proof_after=now,
                             previous_rb=self.packet.deadman,
                             output_timing_recovery_required=True,
                             output_timing_paused_at=now,
                             output_timing_details=details)
                self.supervisor.hold_reason = ('input_wait_start_takeover'
                    if pause.get('takeover_required') else 'input_wait_release_rb')
                self.emit('output_timing_pause_restarted', monotonic_ns=now,
                          reason=pause['reason'], details=details,
                          requires_start=bool(pause.get('takeover_required')),
                          recording_continues=True)
                return True
            state = self.supervisor.state
            if (self.packet.deadman or self.packet.home_reset
                    or state not in (HILState.HIL_LATCHED, HILState.POLICY, HILState.HANDOFF_HOLD)):
                return False
            needs_start = state in (HILState.POLICY, HILState.HANDOFF_HOLD)
            self._start_manual_pause(now, 'stationary_output_timing', invalid_from=now,
                                     details=details, takeover_required=needs_start)
            self.input_pause.update(output_timing_recovery_required=True,
                                    output_timing_paused_at=now)
            if needs_start:
                self.supervisor.hold_reason = 'input_wait_start_takeover'
            return True

    def pause_policy_candidate(self, now, details):
        """Pre-queue veto only, after Control verifies held AND measured poses."""
        with self.context_lock:
            if (self.hard_hold_reason is not None or self.packet is None
                    or self.supervisor.state not in (HILState.POLICY, HILState.HANDOFF_HOLD)):
                return False
            self._start_manual_pause(now, 'policy_candidate_boundary', invalid_from=now,
                                     details=details, takeover_required=True)
            self.supervisor.hold_reason = 'input_wait_start_takeover'
            self.emit('policy_candidate_paused', monotonic_ns=now, details=details,
                      recording_continues=True, requires_start=True)
            return True

    def _start_manual_pause(self, now, reason, *, invalid_from, details=None, takeover_required=False):
        self.manual_recovery_anchor = None
        self.input_pause = dict(started=now, session=self.packet.session_id,
            toggle=self.packet.hil_toggle_id, sequence=self.packet.sequence,
            invalid_from=invalid_from, received=now, neutral=None,
            previous_rb=self.packet.deadman, proof_after=now,
            awaiting_session=False, retired_sessions=set(), reason=reason,
            details=details, recovery_error=None, takeover_required=takeover_required)
        self.emit('input_pause_started', monotonic_ns=now, reason=reason,
            invalid_from_monotonic_ns=invalid_from, details=details,
            session_id=self.packet.session_id, recording_continues=True,
            requires_start=takeover_required and not getattr(self, '_startup_input_wait_eligible', False),
            startup_policy_wait=getattr(self, '_startup_input_wait_eligible', False))
        super().disconnect(now, 'input_wait_release_rb')

    def _disconnect(self, now, reason):
        if self.fault_lifecycle and reason.startswith(('camera_unavailable:', 'runtime_wait:')):
            return self.pause_runtime_condition(now, reason)
        watchdog = reason in ('operator_watchdog', 'operator_watchdog_before_publication',
                              'operator_transport_stale', 'controller_disconnected')
        transport = (reason in ('operator_disconnected', 'operator_session_changed')
                     or reason.startswith('operator_transport_error:'))
        if watchdog or transport:
            if self.hard_hold_reason is not None:
                # Keep the first hard fault and its authorization requirements.
                # In particular, a later EOF cannot enable neutral/RB recovery.
                return
            if (self.input_pause is None and self.started
                    and (self.fault_lifecycle or self.supervisor.state is HILState.HIL_LATCHED)
                    and self.packet is not None):
                self._start_manual_pause(now, reason, invalid_from=self.last_received_ns or now,
                    takeover_required=self.supervisor.state is not HILState.HIL_LATCHED)
            if self.input_pause is not None:
                if reason == 'controller_disconnected':
                    self.input_pause.update(neutral=None, previous_rb=True, proof_after=now)
                    self.supervisor.hold_reason = 'input_wait_release_rb'
                if transport:
                    pause = self.input_pause
                    pause['retired_sessions'].add(pause['session'])
                    # Bound recovery bookkeeping even during a prolonged outage.
                    # Old proof tokens expire independently at the watchdog.
                    if len(pause['retired_sessions']) > 128:
                        pause['retired_sessions'] = {pause['session']}
                    pause.update(awaiting_session=True, neutral=None, previous_rb=True,
                                 proof_after=now)
                    self.supervisor.hold_reason = 'input_wait_reconnect'
                    self.emit('input_transport_interrupted', monotonic_ns=now, reason=reason,
                              session_id=pause['session'], recording_continues=True)
                return
            # An idle/pre-START transport loss has no episode to pause or close.
            super().disconnect(now, reason)
            return
        if self.hard_hold_reason is not None:
            return
        self.hard_hold_reason = reason
        self.manual_recovery_anchor = None
        if self.input_pause is not None:
            self.emit('input_pause_escalated', monotonic_ns=now, reason=reason)
            self.input_pause = None
        super().disconnect(now, reason)
        policy_recoverable = self.unified_episodes and transient_control_reason(reason)
        if self.episodes and not policy_recoverable:
            self.episodes.finish(now, reason=reason, interrupted=True)

    def _paused_operator(self, packet, received_ns, now, *, hand_pending=False):
        pause = self.input_pause
        self.last_operator_key = (packet.session_id, packet.sequence)
        if pause['awaiting_session'] or packet.session_id != pause['session']:
            if packet.session_id in pause['retired_sessions']:
                self.emit('input_recovery_packet_discarded', monotonic_ns=now,
                          reason='retired_session', session_id=packet.session_id)
                return False
            pause['retired_sessions'].add(pause['session'])
            pause.update(session=packet.session_id, sequence=packet.sequence,
                         toggle=packet.hil_toggle_id, received=received_ns, neutral=None,
                         previous_rb=packet.deadman, proof_after=now, awaiting_session=False)
            self.packet = packet
            self.session.watchdog_timeout()
            self.supervisor._session_id = packet.session_id
            self.supervisor._last_toggle_id = packet.hil_toggle_id
            self.supervisor.hold_reason = 'input_wait_release_rb'
            self.emit('input_recovery_session_anchored', monotonic_ns=now,
                      session_id=packet.session_id, sequence=packet.sequence,
                      hil_toggle_id=packet.hil_toggle_id, buttons_discarded=True)
            return False
        if (packet.sequence <= pause['sequence']
                or packet.hil_toggle_id < pause['toggle']
                or packet.hil_toggle_id > pause['toggle']+1):
            self.disconnect(now, 'input_recovery_session_or_sequence_changed')
            return False
        self.packet = packet
        self.emit('operator_packet', monotonic_ns=now, packet=asdict(packet),
                  accepted=True, reason='input_pause_no_motion', received_monotonic_ns=received_ns)
        delta = packet.hil_toggle_id-pause['toggle']
        if getattr(self, '_input_resume_lease', None) is not None and (
                delta or packet.deadman or packet.home_reset or hand_pending):
            self._input_resume_lease = None
            self.startup_policy_authorized = False
            pause.pop('startup_policy_wait', None)
            pause.pop('policy_input_wait', None)
        pause['sequence'], pause['toggle'] = packet.sequence, packet.hil_toggle_id
        # Consume START/Y/RT during HOLD. Nothing is queued for later playback.
        self.supervisor._last_toggle_id = packet.hil_toggle_id
        if delta and self.unified_episodes and not pause.get('takeover_required'):
            self.emit('episode_start_ignored', monotonic_ns=now,
                      reason='pause_use_release_neutral_rb_or_up', phase=self.episodes.phase)
        if received_ns-pause['received'] > self.watchdog_ns:
            pause['neutral'] = None
        pause['received'] = received_ns
        rising_rb = packet.deadman and not pause['previous_rb']
        pause['previous_rb'] = packet.deadman
        after = pause['neutral'] if pause['neutral'] is not None else pause['proof_after']
        age = (self.recovery_proof(packet, after, now, self.watchdog_ns)
               if self.recovery_proof is not None else None)
        proven = age is not None and 0 <= now-received_ns <= self.watchdog_ns
        if not proven:
            # A released, neutral sample can still carry the previous challenge
            # while the post-neutral ACK travels to the sender. Keep the anchor
            # only when that sample independently passes the original freshness
            # budget. Never retain/replay an RB press that fails this handshake.
            waiting_ack = (pause['neutral'] is not None
                and packet.controls_neutral is True and not packet.deadman
                and not packet.home_reset and not packet.hil_button_pressed
                and 0 <= now-received_ns <= self.watchdog_ns
                and self.recovery_proof is not None
                and self.recovery_proof(packet, pause['proof_after'], now, self.watchdog_ns) is not None)
            if not waiting_ack:
                pause['neutral'] = None
            self.supervisor.hold_reason = ('policy_wait_operator' if pause.get('policy_input_wait') else
                                          'startup_wait_operator' if pause.get('startup_policy_wait') else
                                          'input_wait_update_sender' if packet.recovery_token is None
                                           else 'input_wait_fresh_packet')
            return False
        if self.fault_lifecycle and (self.camera_issue is not None or self._camera_fault_pending is not None):
            pause['neutral'] = None
            self.supervisor.hold_reason = 'input_wait_observation'
            return False
        resume_startup = getattr(self, 'resume_startup_input', None)
        if pause.get('startup_policy_wait') and resume_startup is not None:
            resumed = resume_startup(packet, received_ns, now, delta=delta, hand_pending=hand_pending)
            if resumed is not None:
                return resumed
        if pause.get('takeover_required'):
            # RB cannot acquire policy ownership. Require a fresh START with
            # all motion controls released, then a separate neutral/RB handshake.
            # Raw controls_neutral includes START itself, so it is necessarily
            # false on a physical START press. This event grants NO motion;
            # the separate all-buttons-up/neutral/RB handshake follows it.
            if (delta == 1 and not packet.deadman and not packet.home_reset
                    and not hand_pending):
                pause.update(takeover_required=False, neutral=None, proof_after=now,
                             previous_rb=False)
                self.supervisor.intervention_id += 1
                self.emit('policy_boundary_takeover_accepted', monotonic_ns=now,
                          intervention_id=self.supervisor.intervention_id)
                self.supervisor.hold_reason = 'input_wait_release_rb'
                return True
            self.supervisor.hold_reason = 'input_wait_start_takeover'
            return False
        if delta and not self.unified_episodes and not packet.deadman and not packet.home_reset and not hand_pending:
            # START while paused explicitly ends this episode; never a recovery shortcut.
            self.supervisor.state = HILState.HANDOFF_HOLD
            self.supervisor.policy_epoch += 1
            self.supervisor.handoff_monotonic_ns = now
            with self.latest_lock:
                self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
            self.supervisor.hold_reason = 'waiting_for_fresh_policy'
            self.input_pause = None
            self.last_received_ns = received_ns
            self.session.process(packet)
            self.invalidate(now)
            self.emit('input_pause_ended', monotonic_ns=now, outcome='operator_finished')
            self._record_update(self.supervisor._update(False, False,
                transition='hil_to_handoff_hold', reason='start_button_toggle'), now)
            if self.episodes:
                self.episodes.finish(now)
            return True
        if packet.controls_neutral is True and not packet.deadman and not packet.home_reset and not packet.hil_button_pressed:
            if pause['neutral'] is None:
                pause['neutral'] = now
                pause['neutral_ready'] = False
                self.session.process(packet)  # Clear require-disabled using a proven neutral sample.
                self.emit('input_recovery_neutral', monotonic_ns=now, proof_age_ns=age)
                self.supervisor.hold_reason = 'input_wait_fresh_packet'
                return False
            if ((pause.get('reason') == 'stationary_output_timing'
                    or pause.get('output_timing_recovery_required'))
                    and not self.manual_output_ready(now)):
                self.supervisor.hold_reason = 'input_wait_fresh_packet'
                return False
            if not pause.get('neutral_ready', False):
                pause['neutral_ready'] = True
                self.emit('input_recovery_ready', monotonic_ns=now, proof_age_ns=age)
            self.supervisor.hold_reason = 'input_wait_press_rb'
            return False
        if rising_rb and pause['neutral'] is not None and packet.active_side is not None and not packet.hil_button_pressed:
            # Real callback timestamps must advance beyond the pause. Source.feedback
            # also retains the profile's existing feedback-age/connection checks.
            feedback_after = max(pause['started'], pause.get('output_timing_paused_at', 0))
            try:
                _, measured, stamps = self.source.feedback()
                feedback_now = max(now, time.monotonic_ns())
                mask = getattr(self.source, 'state_valid_mask', (True,) * 16)
                feedback_ok = (len(stamps) == 16 and np.isfinite(measured).all()
                               and all(feedback_after < stamp <= feedback_now
                                       and feedback_now-stamp <= self.watchdog_ns
                                       for stamp, valid in zip(stamps, mask) if valid))
            except (RuntimeError, AttributeError, ValueError):
                feedback_ok = False
            if feedback_ok:
                if self.manual_recovery_guard is not None:
                    try:
                        self.manual_recovery_anchor = self.manual_recovery_guard(packet, now, feedback_after)
                    except (RuntimeError, ValueError) as exc:
                        self.manual_recovery_anchor = None
                        pause['neutral'] = None
                        pause['recovery_error'] = str(exc)
                        self.supervisor.hold_reason = 'input_wait_safe_anchor'
                        self.emit('input_recovery_anchor_rejected', monotonic_ns=now,
                                  reason=str(exc), active_side=packet.active_side)
                        return False
                ack = self.session.process(packet)
                if not ack.accepted:
                    self.disconnect(now, 'input_recovery_packet_rejected')
                    return False
                self.supervisor.state = HILState.HIL_LATCHED
                self.supervisor.policy_epoch += 1
                self.supervisor.hold_reason = None
                self.last_received_ns = received_ns
                self.invalidate(now)
                self.input_pause = None
                self.emit('input_pause_ended', monotonic_ns=now, outcome='rb_rearmed',
                    proof_age_ns=age, policy_epoch=self.supervisor.policy_epoch,
                    intervention_id=self.supervisor.intervention_id)
                self._record_update(self.supervisor._update(True, False,
                    transition='input_pause_to_hil', reason='fresh_rb_edge'), now)
                return True
            self.supervisor.hold_reason = 'input_wait_feedback'
        # A failed press is consumed: held RB cannot acquire permission later.
        if packet.deadman or packet.home_reset or packet.hil_button_pressed:
            pause['neutral'] = None
        return False

    def close(self):
        self.source.stop_event.set()
        validator = getattr(self, 'plan_validator', None)
        try:
            if validator is not None and hasattr(validator, 'close'):
                validator.close()
        finally:
            super().close()
