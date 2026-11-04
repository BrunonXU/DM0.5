"""Explicit START/new-episode and D-pad UP/save; no hardware or disk waits."""
from dagger_core.hil_supervisor import HILState


class EpisodeButtons:
    def __init__(self, engine):
        self.e = engine
        self.input_session = None
        self.end_id = None
        self.sequence = -1
        self.opening = False
        self.epoch_floor = 0
        self.home_active = False
        self.home_goal = None
        self.hold_since = None
        self.end_hint = None

    def reset_start(self, now):
        e = self.e
        e.supervisor.policy_epoch += 1
        e.invalidate(now)
        e.supervisor.state = HILState.HANDOFF_HOLD
        e.supervisor.hold_reason = 'episode_ready'
        e.supervisor._session_id = None
        e.supervisor._last_toggle_id = None
        e.supervisor._fresh_request = None
        e.started = False
        e.policy_enabled = False
        e.startup_session = e.startup_received_ns = None
        e.startup_released = False
        e.input_pause = None
        e.startup_policy_authorized = False
        e.manual_recovery_anchor = None
        if hasattr(e, 'deadline_ns'):
            e.deadline_ns = None
        hook = getattr(e, 'episode_reset', None)
        if hook:
            hook(now)

    def poll(self, now):
        e = self.e
        if self.opening and e.episodes.phase in ('saving', 'idle', 'failed'):
            self.opening = False
        if not self.opening and e.started and e.episodes.phase in ('saving', 'idle', 'failed'):
            self.reset_start(now)
        if self.opening and e.episodes.phase == 'recording' and e.input_pause is None:
            self.opening = False
            e.supervisor.policy_epoch += 1
            e.invalidate(now)
            e.episodes.epoch_floor = e.supervisor.policy_epoch
            e.supervisor.state = HILState.HIL_LATCHED if e.teleop_only else HILState.HANDOFF_HOLD
            e.supervisor.hold_reason = None if e.teleop_only else 'waiting_for_fresh_policy'
            e.supervisor.handoff_monotonic_ns = now
            e.supervisor._fresh_request = None
            with e.latest_lock:
                e.handoff_frame_anchor = None if e.latest is None else e.latest[0]
            e.policy_enabled = not e.teleop_only
            e.emit('episode_control_ready', monotonic_ns=now, run_id=e.episodes.run_id,
                   policy_epoch=e.supervisor.policy_epoch, mode='manual' if e.teleop_only else 'policy')

    def operator(self, packet, received_ns, now, hand_pending):
        e = self.e
        key = (packet.session_id, packet.sequence)
        if key == e.last_operator_key:
            return False
        if packet.episode_end_id is None:
            e.disconnect(now, 'episode_sender_update_required')
            e.last_operator_key = key
            return False
        if not 0 <= now-received_ns <= e.watchdog_ns:
            e.disconnect(now, 'operator_transport_stale')
            return False
        end = False
        if packet.session_id != self.input_session:
            self.input_session, self.end_id, self.sequence = packet.session_id, packet.episode_end_id, packet.sequence
        else:
            if packet.sequence <= self.sequence:
                e.disconnect(now, 'episode_input_sequence_regressed')
                return False
            delta = packet.episode_end_id-self.end_id
            self.sequence, self.end_id = packet.sequence, packet.episode_end_id
            if delta not in (0, 1):
                e.disconnect(now, 'episode_end_id_gap_or_regression')
                return False
            end = delta == 1
        phase = e.episodes.phase
        was_started = e.started
        # Consume genuine START counters while human control or save/open is
        # active. Gaps are still rejected by the original supervisor.
        last = e.supervisor._last_toggle_id
        rating_required = (phase == 'recording' and e.supervisor.state is HILState.HIL_LATCHED
                           and getattr(getattr(e, 'local_task', None), 'status', None) == 'active')
        suppress = (phase in ('starting', 'saving') or end or
                    rating_required or
                    (phase == 'recording' and e.supervisor.state is HILState.HIL_LATCHED
                     and not getattr(e, 'allow_policy_handoff', False)))
        if (suppress and e.supervisor._session_id == packet.session_id
                and last is not None and packet.hil_toggle_id == last+1):
            e.supervisor._last_toggle_id = packet.hil_toggle_id
            e.emit('rlt_rating_required' if rating_required else 'episode_start_ignored',
                   monotonic_ns=now, reason='rate_before_handoff' if rating_required else 'use_up_to_save',
                   phase=phase, queued=False)
        if not was_started and (phase != 'idle' or end or packet.episode_end_pressed
                               or packet.deadman or packet.home_reset or hand_pending):
            # Consume pre-start buttons without creating delayed authorization.
            e.startup_session = e.startup_received_ns = None
            e.last_operator_key = key
            return False
        if not was_started:
            # First/new START uses the original release/counter/camera gates.
            e.supervisor.state = HILState.POLICY
        grip_changed = (e.packet is not None and packet.grip_target is not None
                        and packet.grip_target != e.packet.grip_target)
        changed = e._operator(packet, received_ns, now, hand_pending=hand_pending)
        if not e.started:
            e.last_received_ns = received_ns
        if not was_started and e.started:
            if getattr(e, 'collision_restart_required', False):
                reason = e.hard_hold_reason or 'collision_requires_restart'
                self.reset_start(now)
                e.hard_hold_reason = e.supervisor.hold_reason = reason
                e.emit('episode_start_rejected', monotonic_ns=now,
                       reason='collision_requires_restart', protection_reason=reason,
                       phase=e.episodes.phase, queued=False)
                return False
            if not e.episodes.start(now):
                self.reset_start(now)
                return False
            e.episodes.epoch_floor = e.supervisor.policy_epoch + 1
            e.hard_hold_reason = None  # A fresh explicit START passed every startup gate.
            self.opening = True
            e.startup_policy_authorized = not e.teleop_only
            self.home_goal = self.hold_since = None
            self.end_hint = None
            e.supervisor.state = HILState.HANDOFF_HOLD
            e.supervisor.hold_reason = 'episode_starting'
            e.policy_enabled = False
            e.invalidate(now)
            changed = True
        if end:
            reason = None
            if e.episodes.phase != 'recording': reason = 'not_recording'
            elif packet.deadman: reason = 'release_rb'
            elif packet.home_reset or self.home_active: reason = 'wait_for_home'
            elif hand_pending or grip_changed: reason = 'wait_for_gripper'
            elif packet.hil_button_pressed: reason = 'release_start'
            if reason:
                self.end_hint = reason
                e.emit('episode_end_rejected', monotonic_ns=now, reason=reason,
                       episode_end_id=packet.episode_end_id)
            else:
                success = getattr(e, 'local_failure' if getattr(packet, 'local_result', None) == 'failure'
                                  else 'local_success', None)
                if success:
                    self.end_hint = None
                    return success(now) or changed
                e.emit('episode_end_accepted', monotonic_ns=now, reason='dpad_up',
                       home_completed=self.home_goal is not None,
                       terminal_hold_seconds=None if self.hold_since is None else (now-self.hold_since)/1e9,
                       episode_end_id=packet.episode_end_id, training_ready=False,
                       paused=e.input_pause is not None,
                       pause_reason=None if e.input_pause is None else e.input_pause.get('reason'))
                if e.input_pause is not None:
                    e.emit('input_pause_ended', monotonic_ns=now, outcome='operator_finished',
                           recovery_reason=e.input_pause.get('reason'))
                self.reset_start(now)
                e.episodes.finish(now, reason='dpad_up')
                self.end_hint = None
                changed = True
        return changed
