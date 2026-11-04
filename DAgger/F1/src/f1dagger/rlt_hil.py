"""B RTC to explicitly authorized C16 sync/RTC RLT, with one control owner."""
import time
from dataclasses import replace

import numpy as np

from dagger_core.hil_supervisor import HILState
from dagger_core.powered_hil_policy import ChunkPolicy
from .policy import F1PolicyEngine
from .rtc_hil import RTCHILPolicyEngine
from .rtc import RTCPolicyEngine
from .standalone import StandaloneFinished
from .rlt_episode import LocalPlacement, OPERATOR_PROTOCOL
from .robot_io import CandidateSafetyRejected
from .contract import ArmLimitRejected


class RLTHILPolicyEngine(RTCHILPolicyEngine):
    allow_policy_handoff = True
    preventive_boundary_recovery = True
    transient_policy_resume = True

    def _record_update(self, update, now):
        # Keep the supervisor's stop and epoch invalidation intact. An input
        # gap after saving is not a second interruption of the saved episode.
        idle = (not self.started and self.episodes is not None
                and self.episodes.phase in ('idle', 'saving'))
        if (idle and update.transition == 'safe_hold_entered'
                and update.reason in ('operator_watchdog', 'operator_watchdog_before_publication',
                                      'operator_transport_stale', 'controller_disconnected',
                                      'operator_disconnected')):
            self.emit('rlt_idle_input_wait', monotonic_ns=now, reason=update.reason,
                      episode_phase=self.episodes.phase, recording=False)
            update = replace(update, transition=None)
        return super()._record_update(update, now)

    def __init__(self, *args, rlt_policy, evaluation=None, **kwargs):
        self.evaluation = evaluation
        self.rlt_policy = rlt_policy
        self.rlt_phase = 'base'
        self.rlt_cycle = 'base_rtc'
        self.rlt_hold_pending = False
        self.rlt_hold_epoch = None
        self.rlt_hold_committed_ns = None
        self.rlt_final_pending = False
        self.rlt_final_target = None
        self.rlt_final_sampling = None
        self.rlt_hold_completed_target = False
        self.plan_details = {}
        self.switch_session = None
        self.switch_id = None
        self.switch_sequence = -1
        self.home_end_id = None
        self.local_task = LocalPlacement()
        self.ending_episode = False
        self.home_request = False
        # A collision veto is a session boundary, not a new-episode retry.
        # Only constructing a new runtime clears this latch; reset_start does not.
        self.collision_restart_required = False
        super().__init__(*args, **kwargs)
        self.base_policy = self.policy
        self.rlt_rtc = bool(getattr(rlt_policy, 'rtc', False))
        self.base_rtc_parameters = (self.minimum_delay_steps, self.s_min)
        if not self.unified_episodes:
            # Shadow has deliberately no disk episode recorder.
            if not self.rlt_policy.config.get('shadow_only'):
                raise ValueError('RLT HIL requires START/UP independent episodes')

    def _reset_base(self, now):
        self.rlt_phase, self.rlt_cycle = 'base', 'base_rtc'
        self.policy = self.base_policy
        self.chunk_steps = 20
        self.minimum_delay_steps, self.s_min = self.base_rtc_parameters
        self.delays.clear(); self.delays.append(self.minimum_delay_steps)
        self.rtc_horizon, self.rtc_observation_grid = 50, False
        self.rlt_hold_pending = self.rlt_final_pending = False
        self.rlt_final_target = self.rlt_final_sampling = None
        self.rlt_hold_completed_target = False
        self.rlt_hold_epoch = self.rlt_hold_committed_ns = None
        self.plan_details.clear()
        self.emit('rlt_episode_reset', monotonic_ns=now, phase='base')

    def episode_reset(self, now):
        self._finish_local(now, 'interrupted', 'episode_closed_without_local_result')
        self._reset_base(now)
        self.local_task = LocalPlacement()
        self.ending_episode = self.home_request = False

    def _terminal_observation(self, now):
        with self.latest_lock:
            obs = None if self.latest is None else self.latest[0]
        if obs is None or obs.available_monotonic_ns > now:
            return None
        return dict(observation_id=obs.observation_id,
                    available_monotonic_ns=obs.available_monotonic_ns,
                    capture_started_monotonic_ns=obs.capture_started_monotonic_ns)

    def _finish_local(self, now, outcome, reason):
        result = self.local_task.finish(now, outcome, reason, self._terminal_observation(now))
        if result is not None:
            self.emit('rlt_local_closed', monotonic_ns=now, **result)
        return result

    def _return_base(self, now):
        # Revoke even completed/in-flight actor proposals before fresh B RTC.
        self.supervisor.policy_epoch += 1
        self.supervisor.last_policy_request_id = None
        self.supervisor._fresh_request = None
        self.invalidate(now)
        self.pending_context = None
        self._reset_base(now)
        self.supervisor.state = HILState.HANDOFF_HOLD
        self.supervisor.hold_reason = 'waiting_for_fresh_policy'
        self.supervisor.handoff_monotonic_ns = now
        with self.latest_lock:
            self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
        self.policy_enabled = True
        self.emit('rlt_return_to_vla', monotonic_ns=now, policy_epoch=self.supervisor.policy_epoch)

    def local_success(self, now):
        if (self.local_task.status != 'active' or self.ending_episode
                or self.input_pause is not None or self.hard_hold_reason is not None
                or self.supervisor.state not in (HILState.POLICY, HILState.HANDOFF_HOLD, HILState.HIL_LATCHED)):
            self.emit('rlt_success_rejected', monotonic_ns=now, reason='no_active_healthy_local_task')
            return False
        human = self.supervisor.state is HILState.HIL_LATCHED
        self._finish_local(now, 'success', 'human_confirmed' if human else 'autonomous_confirmed')
        if not human:
            self._return_base(now)
        return True

    def _request_episode_finish(self, packet, now, hand_pending, *, control_boundary=True):
        phase = None if self.episodes is None else self.episodes.phase
        reason = None
        if getattr(self, 'recording_failed', None): reason = 'recording_failed_manual_exit_only'
        elif phase == 'saving': reason = 'episode_saving'
        elif phase == 'starting': reason = 'episode_starting'
        elif self.hard_hold_reason is not None:
            # Match the policy faults that preserve this recording. This only
            # changes guidance: Y still cannot clear the hold or start Home.
            recoverable = (self.unified_episodes and self.started and phase == 'recording'
                           and not self.collision_restart_required
                           and self.hard_hold_reason.startswith(('ik_rejected:', 'policy_failed:', 'rtc_')))
            reason = 'policy_recovery_required' if recoverable else 'protection_active'
        elif not self.started or phase != 'recording': reason = 'not_recording'
        elif self.ending_episode: reason = 'already_finishing'
        elif self.input_pause is not None: reason = 'manual_pause'
        elif packet.deadman: reason = 'release_rb'
        elif packet.hil_button_pressed: reason = 'release_start'
        elif packet.episode_end_pressed: reason = 'release_success'
        elif packet.policy_switch_pressed: reason = 'release_switch'
        elif hand_pending: reason = 'wait_for_gripper'
        elif not control_boundary: reason = 'conflicting_controls'
        elif self.local_task.status == 'active': reason = 'local_rating_required'
        if reason:
            self.emit('rlt_episode_finish_rejected', monotonic_ns=now, queued=False,
                      reason=reason, episode_phase=phase,
                      protection_reason=self.hard_hold_reason,
                      pause_reason=None if self.input_pause is None else self.input_pause.get('reason'))
            return False
        self._finish_local(now, 'pending_review', 'operator_y_without_success')
        self.supervisor.policy_epoch += 1
        self.invalidate(now)
        self.pending_context = None
        self.policy_enabled = False
        self.supervisor.state = HILState.HIL_LATCHED
        self.supervisor.hold_reason = 'episode_home_finishing'
        self.ending_episode = self.home_request = True
        self.emit('rlt_episode_finish_requested', monotonic_ns=now, home_end_id=packet.home_end_id)
        return True

    def local_failure(self, now):
        # A rating is not a motion command. A confirmed failure may be recorded
        # during a protective pause, but cannot clear it or authorize Home.
        if self.local_task.status != 'active' or self.ending_episode:
            self.emit('rlt_failure_rejected', monotonic_ns=now, reason='no_active_local_task')
            return False
        self._finish_local(now, 'failure', 'operator_confirmed_failure')
        if self.supervisor.state in (HILState.POLICY, HILState.HANDOFF_HOLD):
            self.supervisor.policy_epoch += 1
            self.invalidate(now)
            self.pending_context = None
            self.policy_enabled = False
            self.supervisor.state = HILState.HANDOFF_HOLD
            self.supervisor.hold_reason = 'local_failure_confirmed'
        # Human ownership and any existing pause/lock remain unchanged.
        return True

    def poll_episode_finish(self, now):
        buttons = self.episode_buttons
        if (self.ending_episode and buttons and not buttons.home_active
                and buttons.hold_since is not None and now-buttons.hold_since >= 2_000_000_000):
            self.emit('episode_end_accepted', monotonic_ns=now, reason='y_home_stable',
                      terminal_hold_seconds=(now-buttons.hold_since)/1e9, training_ready=False)
            buttons.reset_start(now)
            self.episodes.finish(now, reason='y_home_stable')

    def disconnect(self, now, reason='operator_disconnected'):
        with self.context_lock:
            return super().disconnect(now, reason)

    def _disconnect(self, now, reason):
        from .faults import transient_control_reason
        if ((self.fault_lifecycle and transient_control_reason(reason)) or
                (self.input_pause is not None and (reason in (
                    'operator_watchdog', 'operator_watchdog_before_publication',
                    'operator_transport_stale', 'controller_disconnected',
                    'operator_disconnected', 'operator_session_changed')
                    or reason.startswith('operator_transport_error:')))):
            # Infrastructure interruptions do not adjudicate placement. Keep
            # the local task open, with a recorded gap and revoked old actions.
            if self.local_task.status == 'active':
                self.emit('rlt_local_paused', monotonic_ns=now, reason=reason,
                          segment_id=self.local_task.segment_id, training_gap=True)
            return super()._disconnect(now, reason)
        if reason.startswith(('target_rejected:F1 collision/CoM rejected target:',
                              'target_rejected:F1 preventive boundary:',
                              'target_rejected:arm target outside F1 HAL limits',
                              'target_rejected:F1 coupled wrist limit exceeded')):
            self.collision_restart_required = True
        self._finish_local(now, 'interrupted', reason)
        if self.ending_episode:
            self.ending_episode = self.home_request = False
            if self.episodes is not None:
                self.episodes.finish(now, reason=reason, interrupted=True)
        return super()._disconnect(now, reason)

    def close(self):
        # Ctrl+C/exception cleanup is a local interruption, never an implicit
        # failure or successful Home. Persist it before the recorder is sealed.
        with self.context_lock:
            self._finish_local(time.monotonic_ns(), 'interrupted', 'process_shutdown')
        return super().close()

    def policy_failed(self, exc, now):
        handler = getattr(self, 'candidate_rejection_handler', None)
        if handler and isinstance(exc, (CandidateSafetyRejected, ArmLimitRejected)):
            return handler(now, exc)
        return super().policy_failed(exc, now)

    def operator(self, packet, received_ns, now, **kwargs):
        with self.context_lock:
            if not self.started and self.rlt_phase != 'base':
                self._reset_base(now)
            if (packet.session_id, packet.sequence) == self.last_operator_key:
                return False
            if packet.policy_switch_id is None or packet.operator_protocol != OPERATOR_PROTOCOL:
                if self.started:
                    # A fresh connection negotiates optional fields through
                    # its first ACK. Hold/re-anchor it; never interpret the
                    # handshake packet as control or as a legacy-sender fault.
                    reason = ('operator_disconnected' if packet.session_id != self.switch_session
                              else 'rlt_sender_update_required')
                    self.disconnect(now, reason)
                else:
                    self.supervisor.hold_reason = 'rlt_sender_update_required'
                    self.startup_session = self.startup_received_ns = None
                    self.last_operator_key = (packet.session_id, packet.sequence)
                return False
            if not 0 <= now-received_ns <= self.watchdog_ns:
                self.disconnect(now, 'operator_transport_stale')
                return False
            if not self.started and self.supervisor.hold_reason == 'rlt_sender_update_required':
                self.supervisor.hold_reason = 'waiting_for_start'
            switch = False
            finish = False
            if packet.session_id != self.switch_session:
                self.switch_session = packet.session_id
                self.switch_id, self.switch_sequence = packet.policy_switch_id, packet.sequence
                self.home_end_id = packet.home_end_id
            else:
                delta = packet.policy_switch_id-self.switch_id
                home_delta = packet.home_end_id-self.home_end_id
                if packet.sequence <= self.switch_sequence or delta not in (0, 1) or home_delta not in (0, 1):
                    self.disconnect(now, 'policy_switch_sequence_or_counter_invalid')
                    return False
                switch = delta == 1
                finish = home_delta == 1
                self.home_end_id = packet.home_end_id
                self.switch_id, self.switch_sequence = packet.policy_switch_id, packet.sequence
            before = self.supervisor.state
            previous_toggle = self.supervisor._last_toggle_id
            if self.ending_episode and (packet.deadman or (previous_toggle is not None and packet.hil_toggle_id != previous_toggle)):
                self.disconnect(now, 'episode_home_operator_cancelled')
                return False
            changed = super().operator(packet, received_ns, now, **kwargs)
            if (self.local_task.status == 'failure' and self.rlt_phase == 'actor'
                    and self.supervisor.hold_reason == 'local_failure_confirmed'):
                # The common RTC boundary handler enables HANDOFF_HOLD for
                # normal replanning. A scored failure intentionally waits for
                # a new explicit START/Y instead.
                self.policy_enabled = False
            if (getattr(self, 'recording_failed', None) and before is HILState.HIL_LATCHED
                    and self.supervisor.state is HILState.HANDOFF_HOLD):
                self.supervisor.state = HILState.HIL_LATCHED
                self.supervisor.hold_reason = 'recording_failed_manual_exit_only'
                self.policy_enabled = False
                self.invalidate(now)
                self.emit('recording_failed_operation_rejected', monotonic_ns=now,
                          reason='manual_exit_only_no_home_or_policy')
                return False
            if before is not HILState.HIL_LATCHED and self.supervisor.state is HILState.HIL_LATCHED and self.local_task.status == 'active':
                self.local_task.assisted = True
            if (before is HILState.HIL_LATCHED
                    and self.supervisor.state is HILState.HANDOFF_HOLD
                    and self.input_pause is None):
                self._finish_local(now, 'pending_review', 'operator_handoff_without_success')
                self._return_base(now)
            if finish:
                changed = self._request_episode_finish(packet, now, kwargs.get('hand_pending', False),
                    control_boundary=previous_toggle == packet.hil_toggle_id and not switch) or changed
            if switch:
                if (before is HILState.POLICY and previous_toggle == packet.hil_toggle_id
                        and not packet.deadman and not packet.home_reset
                        and not packet.episode_end_pressed and not kwargs.get('hand_pending', False)):
                    changed = self.enter_actor(now) or changed
                else:
                    self.emit('rlt_switch_rejected', monotonic_ns=now,
                              reason='control_boundary_or_active_controls', queued=False)
            return changed

    def enter_actor(self, now):
        """One observed gamepad event, never stored for later authorization."""
        with self.context_lock:
            recording = (self.episodes is not None and self.episodes.phase == 'recording')
            if self.rlt_policy.config.get('shadow_only') and self.episodes is None:
                recording = self.started
            accepted = (self.started and recording and self.rlt_phase == 'base'
                and self.local_task.status == 'unused' and not self.ending_episode
                and self.supervisor.state is HILState.POLICY and self.policy_enabled
                and self.last_received_ns is not None and 0 <= now-self.last_received_ns <= self.watchdog_ns
                and self.camera_issue is None and self._camera_fault_pending is None)
            if not accepted:
                self.emit('rlt_switch_rejected', monotonic_ns=now, phase=self.rlt_phase,
                          state=self.supervisor.state.value, queued=False)
                return False
            if self.evaluation and self.evaluation['strategy'] == 'b1250':
                local = self.local_task.begin(self.episodes.run_id, now, self.base_policy.model_version)
                self.emit('rlt_local_opened', monotonic_ns=now, **local)
                self.emit('evaluation_local_started', monotonic_ns=now,
                          evaluation=self.evaluation, phase='base', continued_base_policy=True)
                return True
            self.supervisor.policy_epoch += 1
            self.supervisor.last_policy_request_id = None
            self.supervisor._fresh_request = None
            self.invalidate(now)
            self.pending_context = None
            adopt = getattr(self.rlt_policy, 'adopt_pending', None)
            if adopt and not self.evaluation:
                adopt()
            self.rlt_phase, self.policy, self.chunk_steps = 'actor', self.rlt_policy, 16
            if self.rlt_rtc:
                self.minimum_delay_steps = int(self.rlt_policy.config.get('rlt_rtc_delay_steps', 6))
                self.s_min = int(self.rlt_policy.config.get('rlt_rtc_min_steps', 8))
                if not 1 <= self.minimum_delay_steps <= self.s_min <= 16-self.minimum_delay_steps:
                    raise ValueError('RLT RTC requires committed-prefix coverage within C16')
                self.delays.clear(); self.delays.append(self.minimum_delay_steps)
                self.rtc_horizon, self.rtc_observation_grid = 16, True
            local = self.local_task.begin(self.episodes.run_id if self.episodes else 'shadow', now,
                                          self.rlt_policy.rlt_identity['actor_version'])
            self.emit('rlt_local_opened', monotonic_ns=now, **local)
            self.rlt_policy.local_segment_id = local['segment_id']
            self._begin_hold(now, 'phase_switch')
            self.emit('rlt_switch_accepted', monotonic_ns=now, phase='actor',
                policy_epoch=self.supervisor.policy_epoch, rlt=dict(self.rlt_policy.rlt_identity),
                model_id=self.rlt_policy.model_id, model_version=self.rlt_policy.model_version,
                source='f710_dpad_right', queued=False)
            return True

    def _begin_hold(self, now, reason):
        self.policy_enabled = False
        self.supervisor.state = HILState.HANDOFF_HOLD
        self.supervisor.hold_reason = 'rlt_' + reason
        self.supervisor._fresh_request = None
        self.rlt_hold_epoch = self.supervisor.policy_epoch
        self.rlt_hold_pending = True
        self.rlt_hold_completed_target = reason == 'cycle_boundary'
        self.rlt_cycle = 'holding'
        self.minimum_observation_ns = now

    def invalidate(self, now):
        if self.rlt_phase == 'actor' and self.chunk_request_id in self.plan_details:
            self.emit('rlt_plan_cancelled', monotonic_ns=now,
                      plan=dict(self.plan_details[self.chunk_request_id]),
                      current_epoch=self.supervisor.policy_epoch)
        super().invalidate(now)
        self.rlt_hold_pending = self.rlt_final_pending = False
        self.rlt_final_target = self.rlt_final_sampling = None
        self.rlt_hold_completed_target = False
        self.rlt_hold_epoch = None
        if self.rlt_phase == 'actor':
            self.rlt_cycle = 'cancelled'

    def rlt_hold_committed(self, evidence, *, powered):
        """Called only by Control after publication and its journal succeeded."""
        now = evidence['completed_monotonic_ns']
        valid_publication = (evidence.get('publication_complete') is True
                             and evidence.get('execution_mode') == 'powered')
        if not powered:
            valid_publication = (self.rlt_policy.config.get('shadow_only') is True
                                 and evidence.get('execution_mode') == 'shadow')
        if (not valid_publication or not self.rlt_hold_pending
                or self.rlt_hold_epoch != self.supervisor.policy_epoch
                or self.supervisor.state is not HILState.HANDOFF_HOLD):
            return False
        self.rlt_hold_pending = False
        self.rlt_hold_committed_ns = now
        self.minimum_observation_ns = now
        self.supervisor.handoff_monotonic_ns = now
        self.supervisor._fresh_request = None
        with self.latest_lock:
            self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
        self.policy_enabled = True
        self.rlt_cycle = 'waiting_for_fresh_observation'
        self.emit('rlt_hold_committed', monotonic_ns=now,
            policy_epoch=self.supervisor.policy_epoch, command_sequence=evidence.get('command_sequence'),
            execution_mode='powered' if powered else 'shadow', actual_publication=powered)
        return True

    def before_publication(self, now):
        if self.rlt_phase == 'base' or self.rlt_rtc:
            return super().before_publication(now)
        if self.stop_requested():
            raise StandaloneFinished('local_stop')
        if self.deadline_ns is not None and now >= self.deadline_ns:
            raise StandaloneFinished('duration_elapsed')

    def _discard_revoked(self, now):
        if self.real_future is None or not self.real_future.done():
            return False
        if (self.real_future_epoch == self.supervisor.policy_epoch
                and self.supervisor.state in (HILState.POLICY, HILState.HANDOFF_HOLD)):
            return False
        future, self.real_future = self.real_future, None
        error = 'cancelled' if future.cancelled() else future.exception()
        self.emit('stale_policy_result_discarded', monotonic_ns=now,
            request_id=self.real_future_request_id, policy_epoch=self.real_future_epoch,
            current_epoch=self.supervisor.policy_epoch, reason='authority_revoked',
            error=None if error is None else str(error)[:500])
        return True

    def tick(self, now):
        if self.rlt_phase == 'base' or self.rlt_rtc:
            previous = self.chunk_request_id
            super().tick(now)
            if self.chunk_request_id != previous and self.chunk_request_id in self.plan_details:
                self.plan_details[self.chunk_request_id]['adopted_monotonic_ns'] = now
                if self.rlt_phase == 'actor':
                    self.rlt_cycle = 'rtc_executing'
                    self.emit('rlt_plan_adopted', monotonic_ns=now, policy_epoch=self.supervisor.policy_epoch,
                              plan=dict(self.plan_details[self.chunk_request_id]))
            return
        self.before_publication(now)
        with self.context_lock:
            self._discard_revoked(now)
            F1PolicyEngine.tick(self, now)
            if self.supervisor.state is HILState.HIL_LATCHED:
                self.rlt_cycle = 'human'
            elif self.real_future is not None and self.real_future_epoch == self.supervisor.policy_epoch:
                self.rlt_cycle = 'inference_hold'
            elif self.ready is not None:
                self.rlt_cycle = 'ready'

    def _remember(self, obs, epoch, rid, started, response, phase, policy):
        details = dict(phase=phase, model_id=policy.model_id, model_version=policy.model_version,
            actor_version=self.rlt_policy.rlt_identity['actor_version'] if phase == 'actor' else None,
            request_id=rid, observation_id=obs.observation_id, policy_epoch=epoch,
            observation_monotonic_ns=obs.capture_started_monotonic_ns,
            request_monotonic_ns=started, response_monotonic_ns=response,
            adopted_monotonic_ns=None, execution_completed_monotonic_ns=None,
            hold_committed_monotonic_ns=self.rlt_hold_committed_ns if phase == 'actor' else None)
        if phase == 'actor':
            details['actor_sampling'] = getattr(policy, 'last_actor_sampling', None)
        self.plan_details[rid] = details
        # One executing and one in-flight plan; bounded diagnostic retention.
        for old in sorted(self.plan_details)[:-4]:
            self.plan_details.pop(old, None)
        self.emit('rlt_plan_proposed', monotonic_ns=response, policy_epoch=epoch,
                  plan=dict(details), proposal_only=True)

    def _rtc_infer(self, obs, epoch, rid, context):
        if context.get('rlt') is True:
            started = time.monotonic_ns()
            try:
                actions, absolute = self.rlt_policy.infer_rtc(obs, rid, epoch, context)
            finally:
                self.emit('policy_transport_timing', request_id=rid, observation_id=obs.observation_id,
                          policy_epoch=epoch, phase='actor', timing=self.rlt_policy.last_timing)
            response = time.monotonic_ns()
            self._remember(obs, epoch, rid, started, response, 'actor', self.rlt_policy)
            self.plan_details[rid]['rtc'] = dict(context)
            if not self.recorder.record_chunk(obs, epoch, rid, started, response, actions,
                                             self.waypoint_hz, self.rlt_policy.model_id):
                raise RuntimeError('RLT RTC policy journal failed')
            if getattr(self, 'plan_validator', None):
                self.plan_validator(obs, actions, epoch, rid,
                    committed_prefix=len(context['committed_actions16']), max_new_waypoints=1)
            return actions, absolute, response
        started = time.monotonic_ns()
        try:
            actions, absolute = self.base_policy.infer_rtc(obs, rid, epoch, context)
        finally:
            self.emit('policy_transport_timing', request_id=rid, observation_id=obs.observation_id,
                      policy_epoch=epoch, phase='base', timing=self.base_policy.last_timing)
        response = time.monotonic_ns()
        self._remember(obs, epoch, rid, started, response, 'base', self.base_policy)
        if not self.recorder.record_chunk(obs, epoch, rid, started, response, actions,
                                         self.waypoint_hz, self.base_policy.model_id):
            raise RuntimeError('B policy journal full/failed')
        if getattr(self, 'plan_validator', None):
            # RTC has only D/Hz seconds to join the committed prefix. Checking
            # every speculative suffix point here delayed valid replies past
            # that deadline on ARM. Check the join now; each later selected
            # target AND actual smoothed command still pass the owner gate.
            self.plan_validator(obs, actions, epoch, rid,
                                committed_prefix=0 if context['bootstrap'] else len(context['committed_actions16']),
                                max_new_waypoints=1)
        return actions, absolute, response

    def _infer(self, obs, epoch, rid):
        started = time.monotonic_ns()
        try:
            actions = self.rlt_policy.infer(obs, rid, epoch)
        finally:
            self.emit('policy_transport_timing', request_id=rid, observation_id=obs.observation_id,
                      policy_epoch=epoch, phase='actor', timing=self.rlt_policy.last_timing)
        response = time.monotonic_ns()
        if np.asarray(actions).shape != (16, 16) or not np.isfinite(actions).all():
            raise ValueError('RLT requires a complete finite C16 plan')
        self._remember(obs, epoch, rid, started, response, 'actor', self.rlt_policy)
        if not self.recorder.record_chunk(obs, epoch, rid, started, response, actions,
                                         self.waypoint_hz, self.rlt_policy.model_id):
            raise RuntimeError('RLT policy journal full/failed')
        if getattr(self, 'plan_validator', None):
            self.plan_validator(obs, actions, epoch, rid)
        return epoch, rid, obs.observation_id, response, actions

    def sample(self, now, last_submitted):
        if self.rlt_phase == 'base' or self.rlt_rtc:
            return super().sample(now, last_submitted)
        if self.rlt_final_pending and self.supervisor.state is HILState.POLICY:
            # Refresh the same authorized endpoint through the existing owner.
            # Sampling all C16 points does not finish the bounded trajectory.
            self.policy_sampling = dict(self.rlt_final_sampling)
            return self.rlt_final_target.copy(), 'policy'
        adopting = self.ready is not None and self.supervisor.state is HILState.POLICY
        if adopting:
            self.plan_details[self.chunk_request_id]['adopted_monotonic_ns'] = now
            self.rlt_cycle = 'executing'
            self.emit('rlt_plan_adopted', monotonic_ns=now, policy_epoch=self.supervisor.policy_epoch,
                      plan=dict(self.plan_details[self.chunk_request_id]))
        had_plan = self.chunk is not None or adopting
        result = ChunkPolicy.sample(self, now, last_submitted)
        if had_plan and self.chunk is None and result[1] == 'policy':
            # Do not replace the final waypoint with HOLD in this same tick.
            # The publication callback finishes the chunk after it really sent.
            self.rlt_final_pending = True
            self.rlt_final_target = result[0].copy()
            self.rlt_final_sampling = dict(self.policy_sampling)
            self.rlt_cycle = 'finishing_trajectory'
            self.plan_details[self.chunk_request_id]['sampling_completed_monotonic_ns'] = now
            self.emit('rlt_sampling_completed', monotonic_ns=now,
                      plan=dict(self.plan_details[self.chunk_request_id]),
                      policy_epoch=self.supervisor.policy_epoch)
            self.policy_enabled = False
        return result

    def after_publication(self, evidence, *, powered):
        super().after_publication(evidence, powered=powered)
        if not self.rlt_final_pending:
            return
        if self.supervisor.state is not HILState.POLICY or self.rlt_phase != 'actor':
            self.rlt_final_pending = False
            return
        if powered and (evidence.get('publication_complete') is not True
                        or evidence.get('execution_mode') != 'powered'):
            return
        tracking = evidence.get('rlt_endpoint_tracking') or {}
        if tracking.get('complete') is not True:
            sampled = self.plan_details[self.chunk_request_id].get('sampling_completed_monotonic_ns')
            timeout = tracking.get('settle_timeout_s')
            if (sampled is not None and timeout is not None
                    and evidence['completed_monotonic_ns']-sampled > timeout*1e9):
                self.emit('rlt_endpoint_tracking_timeout',
                          monotonic_ns=evidence['completed_monotonic_ns'], tracking=tracking)
                self.policy_failed(RuntimeError('rlt_endpoint_tracking_timeout'),
                                   evidence['completed_monotonic_ns'])
            return
        now = evidence['completed_monotonic_ns']
        details = self.plan_details[self.chunk_request_id]
        details['execution_completed_monotonic_ns'] = now
        self.emit('rlt_execution_completed', monotonic_ns=now, plan=dict(details),
                  policy_epoch=self.supervisor.policy_epoch,
                  command_sequence=evidence.get('command_sequence'), actual_publication=powered)
        self.rlt_final_pending = False
        self.rlt_final_target = self.rlt_final_sampling = None
        self._begin_hold(now, 'cycle_boundary')

    def control_sampling(self):
        phase = 'human' if self.supervisor.state is HILState.HIL_LATCHED else self.rlt_phase
        details = dict(self.plan_details.get(self.chunk_request_id, {})) if phase != 'human' else {}
        # Full prefix arrays live once in the proposal/adoption journal, not
        # in every 250 Hz publication record.
        details.pop('rtc', None)
        result = dict(self.policy_sampling or {}) if phase != 'human' else {}
        result.update(details)
        result.update(phase=phase,
            execution_contract='rtc_c16_committed_prefix_v1' if self.rlt_rtc and self.rlt_phase == 'actor' else 'c16_endpoint_settled_v1',
            local_task=self.local_task.metadata(),
            task_phase='reset' if self.ending_episode else 'placement' if self.local_task.status == 'active' else 'post_placement' if self.local_task.status != 'unused' else 'pre_placement',
            cycle=self.rlt_cycle if phase != 'human' else 'human',
            actor_version=self.rlt_policy.rlt_identity['actor_version'] if phase == 'actor' else None,
            policy_epoch=self.supervisor.policy_epoch,
            sampling_is_execution_evidence=False)
        return result

