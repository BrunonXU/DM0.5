"""RTC scheduling with genuine F710 ownership; no publisher or local resume."""
from dagger_core.hil_supervisor import HILState
from .policy import F1PolicyEngine
from .policy_errors import IKRejected
from .rtc import RTCPolicyEngine
from .standalone import StandaloneFinished


class RTCHILPolicyEngine(RTCPolicyEngine):
    operator_required = True
    measured_takeover = True

    def __init__(self, *args, **kwargs):
        self.recovery_epoch = None
        self.timing_replan_epoch = None
        super().__init__(*args, **kwargs)

    def authorize_local(self, now):
        raise RuntimeError('RTC HIL requires genuine F710 START')

    def resume_local(self, now):
        raise RuntimeError('RTC HIL recovery requires START takeover then explicit handoff')

    def invalidate(self, now):
        old_plan = self.plan_id
        super().invalidate(now)
        self.chunk_anchor = self.policy_sampling = None
        self.ik_hold_pending = False
        self.recovery_epoch = None
        self.timing_replan_epoch = None
        self.ik_state, self.ik_failures = 'running', 0
        self.delays.clear()
        self.delays.append(self.minimum_delay_steps)
        self.emit('rtc_hil_authority_revoked', monotonic_ns=now, plan_id=old_plan,
                  policy_epoch=self.supervisor.policy_epoch,
                  pending_request_id=getattr(self, 'real_future_request_id', None))

    def operator(self, packet, received_ns, now, *, hand_pending=False):
        with self.context_lock:
            was_started = self.started
            before = (self.supervisor.state, self.supervisor.policy_epoch)
            changed = F1PolicyEngine.operator(self, packet, received_ns, now,
                                              hand_pending=hand_pending)
            if not was_started and self.started:
                self.deadline_ns = (None if self.duration_seconds is None else
                                    now + round(self.duration_seconds * 1e9))
            after = (self.supervisor.state, self.supervisor.policy_epoch)
            if after[0] is HILState.HIL_LATCHED:
                self.startup_policy_authorized = False
            if before != after:
                self.policy_enabled = after[0] in (HILState.POLICY, HILState.HANDOFF_HOLD)
                if self.unified_episodes and self.episodes.phase != 'recording':
                    self.policy_enabled = False
                self.emit('rtc_hil_control_boundary', monotonic_ns=now,
                          previous_state=before[0].value, previous_epoch=before[1],
                          state=after[0].value, policy_epoch=after[1],
                          intervention_id=self.supervisor.intervention_id,
                          session_id=packet.session_id, input_sequence=packet.sequence,
                          active_side=packet.active_side, reason=self.supervisor.hold_reason)
            return changed

    def disconnect(self, now, reason='operator_disconnected'):
        transient = reason in ('operator_watchdog', 'operator_watchdog_before_publication',
                               'operator_transport_stale')
        lease = getattr(self, '_input_resume_lease', None)
        if lease is not None and (not transient or now > lease['deadline']):
            self._input_resume_lease = lease = None
            self.startup_policy_authorized = False
        if (lease is None and transient and getattr(self, 'transient_policy_resume', False)
                and self.input_pause is None and self.started and self.unified_episodes
                and self.episodes.phase == 'recording' and self.packet is not None
                and self.last_received_ns is not None
                and now <= self.last_received_ns+1_000_000_000
                and self.supervisor.state in (HILState.POLICY, HILState.HANDOFF_HOLD)
                and self.hard_hold_reason is None and self.runtime_fault is None):
            lease = self._input_resume_lease = dict(deadline=self.last_received_ns+1_000_000_000,
                session=self.packet.session_id, phase=getattr(self, 'rlt_phase', 'base'))
        startup_wait = (self.startup_policy_authorized
            and reason in ('operator_watchdog', 'operator_watchdog_before_publication', 'operator_transport_stale')
            and self.started and self.unified_episodes
            and self.episodes.phase in ('starting', 'recording')
            and getattr(self, 'rlt_phase', 'base') == 'base'
            and self.hard_hold_reason is None and self.runtime_fault is None
            and (self.input_pause is None or self.input_pause.get('startup_policy_wait'))
            and self.supervisor.state in (HILState.POLICY, HILState.HANDOFF_HOLD, HILState.SAFE_HOLD))
        startup_wait = startup_wait or lease is not None
        if not startup_wait:
            self.startup_policy_authorized = False
            if self.input_pause is not None:
                self.input_pause.pop('startup_policy_wait', None)
        self._startup_input_wait_eligible = startup_wait
        try:
            F1PolicyEngine.disconnect(self, now, reason)
        finally:
            self._startup_input_wait_eligible = False
        if startup_wait and self.input_pause is not None:
            self.policy_enabled = False
            self.input_pause['startup_policy_wait'] = True
            self.input_pause['policy_input_wait'] = lease is not None
            self.supervisor.hold_reason = 'policy_wait_operator' if lease else 'startup_wait_operator'

    def resume_startup_input(self, packet, received_ns, now, *, delta, hand_pending):
        """Resume an accepted START or a bounded same-session policy lease.

        Called after the receiver-clock freshness challenge has passed. No
        joystick command is replayed and no new intervention is fabricated.
        """
        lease = getattr(self, '_input_resume_lease', None)
        if lease is not None and (now > lease['deadline'] or packet.session_id != lease['session']
                or getattr(self, 'rlt_phase', 'base') != lease['phase']):
            self._input_resume_lease = None
            self.input_pause.pop('startup_policy_wait', None)
            return None
        if delta or packet.deadman or packet.home_reset or hand_pending:
            self._input_resume_lease = None
            self.startup_policy_authorized = False
            self.input_pause.pop('startup_policy_wait', None)
            return None  # Explicit takeover continues through the normal path.
        if ((not self.startup_policy_authorized and lease is None) or self.hard_hold_reason is not None
                or self.runtime_fault is not None or self.acquisition_error
                or self.camera_issue or self._camera_fault_pending
                or getattr(self, 'ending_episode', False)):
            return False
        if (packet.controls_neutral is not True or packet.hil_button_pressed
                or self.episodes.phase != 'recording'):
            self.supervisor.hold_reason = 'startup_wait_operator'
            return False
        self.input_pause = None
        self.last_received_ns = received_ns
        self.session.process(packet)
        self.invalidate(now)
        self.supervisor.policy_epoch += 1
        self.supervisor.state = HILState.HANDOFF_HOLD
        self.supervisor.hold_reason = 'waiting_for_fresh_policy'
        self.minimum_observation_ns = self.supervisor.handoff_monotonic_ns = now
        self.supervisor._fresh_request = None
        self.episode_buttons.opening = False
        self.episodes.epoch_floor = self.supervisor.policy_epoch
        with self.latest_lock:
            self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
        self.policy_enabled = True
        self._input_resume_lease = None
        self.emit('policy_input_restored' if lease and not self.startup_policy_authorized else 'startup_input_restored', monotonic_ns=now,
                  policy_epoch=self.supervisor.policy_epoch, fresh_bootstrap_required=True,
                  original_start_authorization=True, recording_continues=True)
        return True

    def after_publication(self, evidence, *, powered):
        if (powered and evidence.get('publication_complete') is True
                and evidence.get('execution_mode') == 'powered'
                and self.supervisor.state is HILState.POLICY):
            self.startup_policy_authorized = False

    def before_publication(self, now):
        if self.stop_requested():
            raise StandaloneFinished('local_stop')
        if self.deadline_ns is not None and now >= self.deadline_ns:
            raise StandaloneFinished('duration_elapsed')
        # Operator freshness is checked by tick and by Control immediately
        # before publishing. A revoked policy deadline cannot interrupt HIL.
        if not self.started or self.supervisor.state is not HILState.POLICY:
            return
        context = self.pending_context
        if (self.real_future is not None and context is not None
                and self.real_future_epoch == self.supervisor.policy_epoch
                and not context['bootstrap']
                and (now-context['plan_origin_ns'])/1e9*self.waypoint_hz >= context['delay_steps']):
            self.pause_timing(now, 'inference_deadline')
        if (self.chunk is not None and self.chunk_started_ns is not None
                and (now-self.chunk_started_ns)/1e9*self.waypoint_hz >= len(self.chunk)):
            self.pause_timing(now, 'plan_exhausted')

    def _policy_hold(self, now, reason):
        # Candidate recovery is explicitly operator owned. Never import the
        # independent feasible-prefix experiment or auto-resume after a reply.
        self.disconnect(now, reason)
        self.ik_state, self.ik_hold_pending, self.policy_enabled = 'paused', True, False
        self.recovery_epoch = self.supervisor.policy_epoch
        self.emit('rtc_hil_recovery_required', monotonic_ns=now, reason=reason,
                  policy_epoch=self.recovery_epoch, authorization='START_to_human_then_UP_save' if self.unified_episodes else 'START_to_human_then_START_handoff')

    def pause_timing(self, now, reason):
        with self.context_lock:
            timing_only = reason in ('inference_deadline', 'late_adoption', 'plan_exhausted') or (
                getattr(self, 'rlt_rtc', False) and getattr(self, 'rlt_phase', 'base') == 'actor'
                and reason in ('horizon_coverage', 'ik_window_coverage'))
            if (self.rtc_timing_replan and timing_only
                    and self.started and self.supervisor.state is HILState.POLICY
                    and (getattr(self, 'rlt_phase', 'base') == 'base' or getattr(self, 'rlt_rtc', False))
                    and self.input_pause is None and self.hard_hold_reason is None
                    and self.runtime_fault is None and not self.camera_issue
                    and not self.acquisition_error and self._camera_fault_pending is None
                    and self.last_received_ns is not None
                    and 0 <= now-self.last_received_ns <= self.watchdog_ns
                    and (not self.unified_episodes or self.episodes.phase == 'recording')
                    and not getattr(self, 'ending_episode', False)):
                old_epoch = self.supervisor.policy_epoch
                self.invalidate(now)
                with self.latest_lock:
                    self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
                self._record_update(self.supervisor.begin_policy_replan(now), now)
                self.timing_replan_epoch = self.recovery_epoch = self.supervisor.policy_epoch
                self.ik_state, self.ik_hold_pending, self.policy_enabled = 'paused', True, False
                self.emit('rtc_timing_replan', monotonic_ns=now, reason=reason,
                          revoked_epoch=old_epoch, policy_epoch=self.timing_replan_epoch,
                          requires_measured_hold=True, recording_continues=True,
                          rlt_phase=getattr(self, 'rlt_phase', 'base'))
                return
            self._policy_hold(now, 'rtc_' + reason)

    def policy_failed(self, exc, now):
        with self.context_lock:
            self._policy_hold(now, ('ik_rejected:' if isinstance(exc, IKRejected)
                                   else 'policy_failed:') + str(exc)[:500])

    def recovery_hold_committed(self, now):
        with self.context_lock:
            if (self.ik_hold_pending and self.timing_replan_epoch is not None
                    and self.timing_replan_epoch == self.supervisor.policy_epoch
                    and self.supervisor.state is HILState.HANDOFF_HOLD):
                if (self.last_received_ns is None or not 0 <= now-self.last_received_ns <= self.watchdog_ns
                        or self.input_pause is not None or self.hard_hold_reason is not None
                        or self.runtime_fault is not None or self.camera_issue
                        or self.acquisition_error or self._camera_fault_pending is not None):
                    self._policy_hold(now, 'rtc_replan_conditions_changed')
                    return
                # Only a post-publication observation may seed the new plan.
                self.minimum_observation_ns = self.supervisor.handoff_monotonic_ns = now
                self.ik_hold_pending = False
                self.policy_enabled = True
                self.timing_replan_epoch = None
                self.emit('rtc_hil_hold_committed', monotonic_ns=now,
                          policy_epoch=self.recovery_epoch, automatic_resume=True,
                          fresh_bootstrap_required=True)
                return
            if (not self.ik_hold_pending or self.recovery_epoch != self.supervisor.policy_epoch
                    or self.supervisor.state is not HILState.SAFE_HOLD):
                self.emit('rtc_hil_callback_discarded', monotonic_ns=now,
                          reason='recovery_authority_revoked', policy_epoch=self.supervisor.policy_epoch)
                return
            self.ik_hold_pending = False
            self.emit('rtc_hil_hold_committed', monotonic_ns=now,
                      policy_epoch=self.recovery_epoch, automatic_resume=False)

    def tick(self, now):
        with self.context_lock:
            lease = getattr(self, '_input_resume_lease', None)
            if lease and now > lease['deadline']:
                self._input_resume_lease = None
                self.startup_policy_authorized = False
                if self.input_pause is not None:
                    self.input_pause.pop('startup_policy_wait', None)
                    self.input_pause.pop('policy_input_wait', None)
                    self.supervisor.hold_reason = 'input_wait_start_takeover'
            self._observe_rlt_request_duration(now)
            self.before_publication(now)
            writer = getattr(self.sink, 'downstream', self.sink)
            if getattr(writer, 'writer_error', None) or getattr(self.recorder, 'writer_error', None):
                raise RuntimeError('RTC HIL recording writer failed')
            if self.acquisition_error:
                raise RuntimeError('RTC HIL acquisition stopped: ' + self.acquisition_error)
            fault = self._camera_fault_pending
            if fault is not None:
                self._camera_fault_pending = None
                self._camera_was_missing = True
                self.emit('camera_acquisition_interrupted', monotonic_ns=now, reason=fault)
                self.disconnect(now, 'camera_unavailable:' + fault)
            if self._camera_was_missing and self.camera_issue is None:
                self._camera_was_missing = False
                self.emit('camera_acquisition_restored', monotonic_ns=now, requires_new_start=True)
            if (self.started and self.last_received_ns is not None
                    and now-self.last_received_ns > self.watchdog_ns):
                self.disconnect(now, 'operator_watchdog')
            if self.real_future is not None and self.real_future.done() and (
                    self.real_future_epoch != self.supervisor.policy_epoch
                    or self.supervisor.state not in (HILState.POLICY, HILState.HANDOFF_HOLD)):
                future, self.real_future = self.real_future, None
                error = 'cancelled' if future.cancelled() else future.exception()
                self.emit('stale_policy_result_discarded', monotonic_ns=now,
                          request_id=self.real_future_request_id, policy_epoch=self.real_future_epoch,
                          current_epoch=self.supervisor.policy_epoch, reason='authority_revoked',
                          error=None if error is None else str(error)[:500])
                self.pending_context = None
                return
            super().tick(now)
