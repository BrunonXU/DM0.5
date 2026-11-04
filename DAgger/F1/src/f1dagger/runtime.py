"""One local control loop for policy, F710, home, and final F1 publication."""
import time
import numpy as np
from dagger_core.hil_supervisor import HILState
from .contract import home_target, ArmLimitRejected
from .clearance_home import FeedbackAlignedClearanceHome as ClearanceHomeMotion, ClearanceHomeStopped
from .clearance_home import DirectWorkbenchHomeMotion
from .teleop import ArmBoundaryHold, GripperIntent, HomeMotion
from .robot_io import CandidateSafetyRejected, StationaryOutputTimingFault
from .faults import FeedbackUnavailable, TargetExpired, ManualReferenceChanged


class ControlLoop:
    def __init__(self, engine, owner, arm, recorder, profile, *, clock=time.monotonic_ns, manual_planner=None):
        self.engine, self.owner, self.arm, self.recorder, self.profile = engine, owner, arm, recorder, profile
        # Versioned opt-in for the local RLT runtime (VLA, actor and HIL modes).
        # Existing standalone/manual releases keep their established protocol.
        self.preventive = bool(getattr(engine, 'preventive_boundary_recovery', False))
        owner.preventive_boundary_recovery = self.preventive
        self.clock = clock
        self.manual_planner = manual_planner
        self.gripper = GripperIntent()
        self.gripper.anchor(owner.last_submitted, None)
        self.home = None
        self.home_motion = None
        self.home_retry_context = None
        self.home_deadline = None
        self.home_settle = None
        self.home_gripper_started = None
        self.y_down = False
        self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
        self.next_target = 0
        self.last_target = None
        self.source = 'hold'
        self.candidate = self.policy = None
        self.started_output = False
        self.last_packet = None
        self.arm_boundary = None
        self.recording_episode = None
        self.checked_policy_path = None
        self.timing_healthy_since = self.timing_last_publish = None
        self.engine.home_stop_reason = None
        self.engine.home_stage = None
        self.engine.manual_recovery_guard = self.recovery_anchor
        self.engine.manual_output_ready = self._timing_ready
        if self.preventive and hasattr(owner.gate, 'plan_worker_spec'):
            from .policy_safety import ProcessPolicyPlanGuard, RestartablePlanGuard
            guard = RestartablePlanGuard if getattr(engine, 'fault_lifecycle', False) else ProcessPolicyPlanGuard
            engine.plan_validator = guard(owner.gate.plan_worker_spec, engine.emit)
            engine.candidate_rejection_handler = self._rejected_policy_plan
        elif self.preventive and hasattr(owner.gate, 'independent_copy'):
            from .policy_safety import PolicyPlanGuard
            full, _, _ = owner.source.feedback()
            engine.plan_validator = PolicyPlanGuard(owner.gate.independent_copy(full), owner.mapping, engine.emit)
            engine.candidate_rejection_handler = self._rejected_policy_plan

    def _rejected_policy_plan(self, now, error):
        try:
            self._pause_candidate(now, self.last_packet, None, error, policy=True)
        except Exception as exc:
            self.engine.disconnect(now, 'target_rejected:' + str(exc))
            self.reset_targets(now, self.last_packet)

    def _check_safe_anchor(self):
        owner = self.owner
        full, measured, stamps = owner.source.feedback()
        now = self.clock()
        valid = getattr(owner, 'state_valid_mask', (True,) * 16)
        if self.preventive and (len(stamps) != 16 or not np.isfinite(measured).all()
                or not all(0 <= now-t <= self.engine.watchdog_ns
                           for t, enabled in zip(stamps, valid) if enabled)):
            raise FeedbackUnavailable('candidate recovery requires fresh physical feedback')
        held = owner.mapping.check_action(owner.last_submitted)
        physical = held.copy()
        physical[:14] = measured[:14]
        owner.mapping.check_action(physical)
        check = getattr(owner.gate, 'check_preventive', owner.gate.check) if self.preventive else owner.gate.check
        check(held, full)
        check(physical, full)
        return full, measured, held

    def _pause_candidate(self, now, packet, target, error, *, policy=False):
        # Never infer a safe current pose from rejection of a future candidate.
        _, measured, held = self._check_safe_anchor()
        details = dict(error=str(error), candidate=None if target is None else target.tolist(),
                       measured=measured.tolist(), held=held.tolist(),
                       rejected_before_publication=True,
                       safety_evidence=getattr(error, 'details', {}))
        pause = self.engine.pause_policy_candidate if policy else self.engine.pause_manual_candidate
        if not pause(now, details):
            raise error
        self.reset_targets(now, packet)
        self.y_down = bool(packet and packet.home_reset)
        self.source = 'input_pause_hold'
        self.previous = (self.engine.supervisor.state, self.engine.supervisor.policy_epoch)
        self.event('candidate_boundary_paused', now, previous_source='policy' if policy else 'human', **details)

    def _preview_policy_path(self, full):
        """Check the unexecuted joint plan before its first sample is queued.

        Waypoints and midpoints are conservative screening, not a continuous
        collision proof. The actual smoothed publication is checked separately.
        """
        e, o = self.engine, self.owner
        chunk = e.chunk
        if chunk is None:
            return
        key = (e.supervisor.policy_epoch, e.chunk_request_id, tuple(full[14:]))
        if key == self.checked_policy_path:
            return
        check = getattr(o.gate, 'check_preventive', o.gate.check)
        previous = o.last_submitted.copy()
        start = max(0, e.action_index or 0)
        for row in chunk[start:]:
            target = np.array(row, copy=True)
            if getattr(e.policy, 'right_only', False):
                target[:7], target[14] = previous[:7], previous[14]
            for candidate in ((previous+target)*.5, target):
                o.mapping.check_action(candidate)
                check(candidate, full)
            previous = target
        self.checked_policy_path = key

    def _timing_ready(self, now):
        return (self.timing_healthy_since is not None and self.timing_last_publish is not None
                and now-self.timing_healthy_since >= 200_000_000
                and 0 <= now-self.timing_last_publish <= 50_000_000)

    def recovery_anchor(self, packet, now, paused_at):
        """Validate real feedback before a new RB edge can regain authority."""
        pause = getattr(self.engine, 'input_pause', None)
        if pause and (pause.get('reason') == 'stationary_output_timing' or pause.get('output_timing_recovery_required')):
            if not self._timing_ready(now):
                raise RuntimeError('output scheduling not yet stable; keep RB released')
        if pause:
            paused_at = max(paused_at, pause.get('output_timing_paused_at', paused_at))
        owner = self.owner
        full, measured, stamps = owner.source.feedback()
        checked_at = max(now, self.clock())
        mask = getattr(owner, 'state_valid_mask', (True,) * 16)
        if (len(stamps) != 16 or not np.isfinite(measured).all()
                or not all(paused_at < stamp <= checked_at
                           and checked_at-stamp <= self.engine.watchdog_ns
                           for stamp, valid in zip(stamps, mask) if valid)):
            raise RuntimeError('fresh feedback after pause required')
        offset = 0 if packet.active_side == 'left' else 7
        target = owner.last_submitted.copy()
        physical = target.copy()
        physical[:14] = measured[:14]
        owner.mapping.check_action(physical)
        check = getattr(owner.gate, 'check_preventive', owner.gate.check) if self.preventive else owner.gate.check
        check(physical, full)
        target[offset:offset+7] = measured[offset:offset+7]
        # A legal command must not conceal illegal measured joints or an actual
        # collision. Never clip feedback into the command range to resume.
        target = owner.mapping.check_action(target)
        check(target, full)
        return target

    def event(self, event, now, **fields):
        if not self.recorder.record_event(event, monotonic_ns=now, **fields):
            from .faults import RecordingUnavailable
            raise RecordingUnavailable('event journal rejected write')

    def reset_targets(self, now, packet):
        if self.manual_planner is not None:
            self.manual_planner.invalidate()
        self.owner.hold(now / 1e9)
        self.arm.release()
        self.home = None
        self.home_settle = None
        self.home_gripper_started = None
        self.home_motion = None
        self.engine.home_stage = None
        self.home_retry_context = None
        self.gripper.anchor(self.owner.last_submitted, packet)
        self.next_target = now
        self.last_target = None
        self.source = 'hold'
        self.candidate = self.policy = None
        self.arm_boundary = None
        self.checked_policy_path = None

    def _home_gripper_arrived(self, now, measured, stamps):
        start = self.home_gripper_started
        return (start is not None and len(stamps) == 16
                and getattr(self.owner, 'state_valid_mask', (True,)*16)[15]
                and start < stamps[15] <= now and now-stamps[15] <= 200_000_000
                and np.isfinite(measured[15])
                and abs(measured[15]) <= self.profile['gripper_tolerance']
                and self.owner.last_submitted[15] == 0.
                and self.owner.selected[15] == 0.)

    def step(self, now, incoming=None, disconnected=None):
        engine, owner = self.engine, self.owner
        buttons = getattr(engine, "episode_buttons", None)
        if buttons:
            buttons.poll(now)
            buttons.home_active = self.home is not None
            if buttons.home_goal is not None:
                _, feedback, stamps = owner.source.feedback()
                stable = (not self.home and (self.last_packet is None or not self.last_packet.deadman)
                          and all(0 <= now-t <= 200_000_000 for t in stamps[7:14])
                          and np.all(np.isfinite(feedback[7:14]))
                          and np.max(np.abs(feedback[7:14]-buttons.home_goal[7:14])) <= self.profile["joint_tolerance_deg"]
                          and not np.any(owner.selected[14:] != owner.last_submitted[14:]))
                if getattr(engine, 'ending_episode', False):
                    stable = (stable and self._home_gripper_arrived(now, feedback, stamps)
                              and buttons.home_goal[15] == 0.)
                if not stable:
                    buttons.hold_since = None
                elif buttons.hold_since is None:
                    buttons.hold_since = now
            finish_poll = getattr(engine, 'poll_episode_finish', None)
            if finish_poll:
                finish_poll(now)
        if disconnected:
            engine.disconnect(now, disconnected)
        if incoming:
            packet, received = incoming
            # START may cancel Home into HOLD. Only unfinished gripper commands
            # block handoff; a Home trajectory must not trap the recording.
            pending = np.any(owner.selected[14:] != owner.last_submitted[14:])
            engine.operator(packet, received, now, hand_pending=pending)
            # Invalid/replayed packets never become human candidates.
            if engine.packet is not None and (engine.packet.session_id, engine.packet.sequence) == (packet.session_id, packet.sequence):
                self.last_packet = packet
                if packet.deadman:
                    self.home_retry_context = None
                if self.home is not None and packet.deadman and engine.supervisor.state is HILState.HIL_LATCHED:
                    self.event('home_cancelled', now, reason='manual_rb_takeover')
                    self.reset_targets(now, packet)
        input_done = self.clock()
        engine.tick(now)
        engine_done = self.clock()
        state = engine.supervisor.state
        episodes = getattr(engine, 'episodes', None)
        if episodes:
            if episodes.phase != 'recording':
                # Keep the owner in HOLD during open/commit; no disk waits on
                # this thread and no motion before an episode is ready.
                state = HILState.HANDOFF_HOLD if buttons or state is HILState.HIL_LATCHED else state
            elif self.recording_episode != episodes.run_id:
                self.recording_episode = episodes.run_id
                # Establish real, same-owner publication provenance in EACH
                # episode, including held grippers. Targets remain unchanged.
                owner.gripper_sent = [False, False]
        current = (state, engine.supervisor.policy_epoch)
        if current != self.previous:
            self.reset_targets(now, self.last_packet)
            recovery_anchor = getattr(engine, 'manual_recovery_anchor', None)
            if state is HILState.HIL_LATCHED and recovery_anchor is not None:
                # The new edge was proved fresh and this measured target passed
                # the unchanged mapping/collision gates. Commit only this anchor
                # this tick; neither old IK/filter lead nor paused RT is replayed.
                owner.approve(recovery_anchor, now / 1e9, enabled=(False, False))
                owner.output.reset()
                owner.output.push(recovery_anchor[:14], now / 1e9, enabled=(False, False))
                self.next_target = now + 20_000_000
                self.source = 'input_recovery_anchor'
                engine.manual_recovery_anchor = None
                self.event('input_recovery_anchor_selected', now,
                           policy_epoch=engine.supervisor.policy_epoch,
                           selected=recovery_anchor.tolist(),
                           previous_submitted=owner.last_submitted.tolist())
            elif state is HILState.HIL_LATCHED and getattr(engine, 'measured_takeover', False):
                # Commit a fresh measured arm anchor through this same owner
                # before manual IK can use last_submitted. Gripper targets and
                # their original publication provenance remain untouched.
                owner.hold_measured_right(now / 1e9)
                self.next_target = now + 20_000_000
                self.source = 'takeover_hold'
                self.event('rtc_hil_takeover_anchor', now,
                           policy_epoch=engine.supervisor.policy_epoch,
                           selected=owner.selected.tolist(),
                           previous_submitted=owner.last_submitted.tolist())
            self.previous = current
        if not engine.started and not (buttons and self.started_output):
            return
        if not self.started_output:
            # Enter/START may follow model loading by minutes; use fresh feedback.
            owner.initialize(now / 1e9)
            self.gripper.anchor(owner.last_submitted, self.last_packet)
            owner.prepare()
            # Endpoint setup may take time. Consume no stale input after it.
            after = self.clock()
            owner.hold(after / 1e9)
            self.next_target = after
            self.started_output = True
            return
        state_done = self.clock()
        packet = self.last_packet
        full, measured, state_stamps = owner.source.feedback()
        recovery_hold = getattr(engine, 'ik_hold_pending', False)
        rlt_hold = getattr(engine, 'rlt_hold_pending', False)
        if rlt_hold:
            if getattr(engine, 'rlt_hold_completed_target', False):
                # A normally completed, stationary trajectory keeps its last
                # publication. Do not replace it with an earlier feedback pose.
                owner.hold(now / 1e9)
            else:
                owner.hold_measured_right(now / 1e9)
            self.next_target = now + 20_000_000
            self.source = 'rlt_boundary_hold'
            self.candidate = self.policy = None
        if recovery_hold:
            owner.hold_measured_right(now / 1e9)
            self.next_target = now + 20_000_000
            self.source = 'ik_recovery_hold'
            self.candidate = self.policy = None
        feedback_done = self.clock()
        if now >= self.next_target:
            dt = .02 if self.last_target is None else (now-self.last_target)/1e9
            self.last_target, self.next_target = now, now + 20_000_000
            target = owner.last_submitted.copy()
            enabled = (False, False)
            self.candidate = self.policy = None
            boundary_recovered = False
            try:
                if (getattr(engine, 'fault_lifecycle', False)
                        and state is HILState.HIL_LATCHED and packet
                        and (packet.deadman or self.home is not None)
                        and (not np.isfinite(dt) or not 0 < dt <= .05)):
                    if self.home is not None or not np.isfinite(dt) or dt <= 0:
                        raise ManualReferenceChanged('invalid manual/Home sample interval')
                    # A delayed manual reference is discarded, never integrated
                    # as a larger joystick displacement. The publication watchdog
                    # below remains independently responsible for output health.
                    self.arm.release()
                    if self.manual_planner is not None:
                        self.manual_planner.invalidate()
                    owner.hold_measured_right(now / 1e9)
                    target = owner.last_submitted.copy()
                    dt = .02
                    self.event('manual_reference_reanchored', now, skipped_wait=True)
                if state is HILState.POLICY:
                    target, self.source = engine.sample(now, owner.last_submitted)
                    if getattr(engine.policy, 'right_only', False):
                        target[:7] = owner.last_submitted[:7]
                        target[14] = owner.last_submitted[14]
                    self.policy = target.copy() if self.source == 'policy' else None
                    enabled = ((False, True) if getattr(engine.policy, 'right_only', False) else (True, True)) if self.source == 'policy' else (False, False)
                elif state is HILState.HIL_LATCHED and packet:
                    self.source = 'human_hold'
                    local_rlt = hasattr(engine, 'home_request')
                    y_edge = engine.home_request if local_rlt else packet.home_reset and not self.y_down
                    if y_edge and not packet.deadman and self.home is None:
                        if local_rlt:
                            engine.home_request = False
                        engine.home_stop_reason = None
                        if buttons:
                            buttons.home_goal = buttons.hold_since = None
                        self.home = owner.mapping.check_action(home_target(self.profile, owner.last_submitted))
                        self.home_gripper_started = None
                        distance = float(np.max(np.abs(self.home[:14]-owner.last_submitted[:14])))
                        self.home_deadline = now + int((10+distance/self.profile['max_joint_speed_deg_s']*3)*1e9)
                        self.arm.release()
                        if self.profile.get('f710_home', {}).get('route') == 'direct':
                            self.home_motion = DirectWorkbenchHomeMotion(self.profile, owner.last_submitted,
                                self.home, self.arm, full, now)
                        elif self.profile.get('f710_home', {}).get('clearance') is not None:
                            options = {} if self.home_retry_context is None else {'retry': self.home_retry_context}
                            self.home_motion = ClearanceHomeMotion(self.profile, owner.last_submitted, self.home, self.arm, full, now, **options)
                        else:
                            self.home_motion = HomeMotion(self.profile, owner.last_submitted, self.home)
                        self.event('home_started', now, target=self.home.tolist(),
                                   stage=self.home_motion.stage if isinstance(self.home_motion, (ClearanceHomeMotion, DirectWorkbenchHomeMotion)) else 'return',
                                   preset=self.profile.get('f710_home', {}).get('name', 'LXL XY'))
                    if self.home is not None:
                        if self.home_gripper_started is not None:
                            target = self.home.copy()
                            enabled, self.source, completed = (False, True), 'home', True
                        elif isinstance(self.home_motion, (ClearanceHomeMotion, DirectWorkbenchHomeMotion)):
                            stage = self.home_motion.stage
                            target = self.home_motion.propose(dt, full, owner.last_submitted, measured, state_stamps, now)
                            enabled, self.source = self.home_motion.enabled, 'home'
                            engine.home_stage = self.home_motion.stage
                            if stage != self.home_motion.stage:
                                self.event('home_stage_completed_feedback', now, stage=stage, next_stage=self.home_motion.stage)
                            completed = self.home_motion.done
                        else:
                            target = self.home_motion.propose(dt)
                            enabled, self.source = (True, True), 'home'
                            if now > self.home_deadline:
                                raise RuntimeError('home feedback timeout')
                            close = np.max(np.abs(measured[:14]-self.home[:14])) <= self.profile['joint_tolerance_deg']
                            if close and self.home_settle is None:
                                self.home_settle = now
                            elif not close:
                                self.home_settle = None
                            completed = (self.home_settle is not None and now-self.home_settle >= 150_000_000
                                         and min(state_stamps[:14]) > self.home_settle)
                        if completed and local_rlt and getattr(engine, 'ending_episode', False):
                            # Preserve the grasp through the existing arm Home;
                            # open only at arrival, through the same output owner.
                            if self.home_gripper_started is None:
                                self.home_gripper_started = now
                                self.event('home_gripper_open_requested', now, side='right', target=0.)
                            self.home[15] = target[15] = 0.
                            engine.home_stage = 'right_gripper_open'
                            completed = self._home_gripper_arrived(now, measured, state_stamps)
                            if not completed and now-self.home_gripper_started > int(self.profile['settle_timeout_s']*1e9):
                                raise ClearanceHomeStopped('home right gripper feedback timeout')
                        if completed:
                            self.event('home_completed_feedback', now, measured=measured.tolist())
                            if buttons:
                                buttons.home_goal = self.home.copy()
                                buttons.hold_since = now
                            engine.home_stage = None
                            self.home_retry_context = None
                            self.home = None
                            self.arm.release()
                            self.gripper.anchor(owner.last_submitted, packet)
                    elif getattr(engine, 'ending_episode', False):
                        self.source = 'episode_reset_hold'
                        self.arm.release()
                    else:
                        if packet.deadman:
                            try:
                                manual_available = True
                                if self.manual_planner is None:
                                    target = self.arm.target(packet, full, owner.last_submitted, dt)
                                else:
                                    reply = self.manual_planner.poll(packet, full, owner.last_submitted,
                                                                     dt, engine.supervisor.policy_epoch)
                                    if reply is not None:
                                        target = np.asarray(reply['selected'])
                                        self.event('manual_compute_selected', now,
                                            sequence=reply['sequence'], candidate=reply['candidate'],
                                            selected=reply['selected'], shortened=reply.get('shortened'),
                                            packet_sequence=reply.get('packet_sequence'),
                                            requested_monotonic_ns=reply.get('requested_monotonic_ns'),
                                            input_axes=reply.get('input_axes'),
                                            compute_ms=reply['compute_ms'], policy_epoch=engine.supervisor.policy_epoch)
                                    elif self.manual_planner.continuing(packet):
                                        target = owner.selected.copy()
                                    else:
                                        # No valid proposal yet: stay at the current
                                        # publication; never execute an uncomputed input.
                                        manual_available = False
                            except (ArmBoundaryHold, CandidateSafetyRejected) as exc:
                                # Require a new neutral/RB edge; never let a held
                                # stick retry a joint-limit candidate each tick.
                                if self.preventive:
                                    self._pause_candidate(now, packet, None, exc)
                                else:
                                    owner.hold(now / 1e9)
                                target = owner.last_submitted.copy()
                                details = dict(exc.details)
                                signature = (packet.active_side, details.get('reason', 'arm_boundary'))
                                if signature != self.arm_boundary:
                                    self.event('arm_boundary_hold', now, active_side=packet.active_side,
                                               details=details)
                                self.arm_boundary = signature
                            else:
                                engine.home_stop_reason = None
                                enabled = (packet.active_side == 'left', packet.active_side == 'right') if manual_available else (False, False)
                                self.source = 'human_arm' if manual_available else 'human_compute_wait'
                                boundary_recovered = self.arm_boundary is not None
                        else:
                            self.arm.release()
                            if self.manual_planner is not None:
                                self.manual_planner.invalidate()
                        target[14:] = self.gripper.update(packet)
                        if np.any(target[14:] != owner.last_submitted[14:]):
                            self.source = 'human_arm_and_hand' if any(enabled) else 'human_hand'
                        self.candidate = target.copy()
                else:
                    self.source = 'input_pause_hold' if getattr(engine, 'input_pause', None) is not None else 'hold'
                self.y_down = bool(packet and packet.home_reset)
                if getattr(engine, 'input_pause', None) is not None:
                    owner.hold(now/1e9)
                    self.source = 'input_pause_hold'
                    self.candidate = self.policy = None
                else:
                    try:
                        if self.preventive and self.source == 'policy' and not getattr(engine, 'plan_validator', None):
                            self._preview_policy_path(full)
                        owner.approve(target, now/1e9, enabled=enabled)
                    except (CandidateSafetyRejected, ArmLimitRejected) as exc:
                        # This is the narrow pre-queue gate, not an owner,
                        # feedback or publish failure. A held/current pose veto
                        # is fatal; only a rejected *new manual candidate* pauses.
                        if isinstance(exc, ArmLimitRejected) and not self.preventive:
                            raise
                        policy = self.preventive and state is HILState.POLICY and self.source == 'policy'
                        manual = state is HILState.HIL_LATCHED and self.source in ('human_arm', 'human_arm_and_hand')
                        if not (policy or manual):
                            raise
                        shortened = False
                        if (manual and self.manual_planner is None and self.preventive and isinstance(exc, CandidateSafetyRejected)
                                and getattr(exc, 'details', {}).get('reason') == 'preventive_collision_boundary'):
                            from .manual_candidate import shorten_manual_candidate
                            # A future reference can cross the proxy boundary
                            # while measured/held poses are safe. Reduce only
                            # this human increment, never a policy or Home plan.
                            full, measured, held = self._check_safe_anchor()
                            try:
                                selected, evidence = shorten_manual_candidate(
                                    owner.gate, owner.mapping, target, held, full,
                                    packet.active_side, exc)
                                owner.approve(selected, now/1e9, enabled=enabled)
                            except CandidateSafetyRejected:
                                pass
                            else:
                                self.arm.accept_safe_candidate(full, selected)
                                self.event('manual_candidate_shortened', now,
                                    candidate=target.tolist(), selected=selected.tolist(),
                                    measured=measured.tolist(), held=held.tolist(),
                                    active_side=packet.active_side, **evidence)
                                shortened = True
                        if not shortened:
                            self._pause_candidate(now, packet, target, exc, policy=policy)
                            boundary_recovered = False
                            if manual:
                                self.event('manual_candidate_paused', now, **engine.input_pause['details'])
                if boundary_recovered:
                    self.event('arm_boundary_recovered', now, active_side=packet.active_side,
                               previous_reason=self.arm_boundary[1], stage='target_approved')
                    self.arm_boundary = None
                if self.source == 'home' and self.home_motion.pending is not None:
                    self.home_motion.commit()
            except ManualReferenceChanged as exc:
                if not getattr(engine, 'fault_lifecycle', False):
                    raise
                self._check_safe_anchor()
                engine.pause_runtime_condition(now, 'runtime_wait:manual_reference', dict(error=str(exc)))
                if getattr(engine, 'ending_episode', False):
                    engine.ending_episode = engine.home_request = False
                self.reset_targets(now, packet)
                self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
                self.event('manual_reference_paused', now, error=str(exc), training_gap=True)
            except ClearanceHomeStopped as exc:
                engine.home_stop_reason = str(exc)
                retry = (self.home_motion.retry_context() if isinstance(self.home_motion, (ClearanceHomeMotion, DirectWorkbenchHomeMotion))
                         else self.home_retry_context)
                self.event('home_stopped', now, error=str(exc),
                           stage=getattr(self.home_motion, 'stage', 'initializing'), measured=measured.tolist(),
                           submitted=owner.last_submitted.tolist(),
                           right_error_deg=(owner.last_submitted[7:14]-measured[7:14]).tolist(),
                           right_feedback_age_ms=[(now-stamp)/1e6 for stamp in state_stamps[7:14]])
                self.reset_targets(now, packet)
                self.home_retry_context = retry
                if getattr(engine, 'ending_episode', False):
                    if getattr(engine, 'fault_lifecycle', False):
                        engine.ending_episode = engine.home_request = False
                        engine.pause_runtime_condition(now, 'runtime_wait:home', dict(error=str(exc)))
                    else:
                        engine.disconnect(now, 'episode_home_failed:' + str(exc))
                # Consume Y even if propose() raised before y_down was updated.
                # A held button never restarts a failed route automatically.
                self.y_down = bool(packet and packet.home_reset)
            except Exception as exc:
                engine.disconnect(now, 'target_rejected:' + str(exc))
                self.reset_targets(now, packet)
                self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
                self.event('target_rejected', now, error=str(exc))
        dispatch_now = self.clock()
        cycle_timing = dict(input_ms=(input_done-now)/1e6,
            engine_ms=(engine_done-input_done)/1e6, state_ms=(state_done-engine_done)/1e6,
            feedback_hold_ms=(feedback_done-state_done)/1e6,
            target_ms=(dispatch_now-feedback_done)/1e6)
        # Standalone local authorization is separate from operator transport.
        # Recheck after target computation, immediately before any publication.
        guard = getattr(engine, 'before_publication', None)
        if guard is not None:
            guard(dispatch_now)
        if getattr(engine, 'ik_hold_pending', False) and not recovery_hold:
            # RTC may cross its deadline during target computation. Flush the
            # already-selected target before publication in this same cycle.
            owner.hold_measured_right(dispatch_now / 1e9)
            self.arm.release()
            recovery_hold = True
            self.next_target = dispatch_now + 20_000_000
            self.source = 'rtc_timing_hold'
            self.candidate = self.policy = None
            self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
        missing_input = engine.last_received_ns is None
        if getattr(engine, 'operator_required', True) and (missing_input or dispatch_now-engine.last_received_ns > engine.watchdog_ns):
            # A prior fault deliberately clears the receipt timestamp. Keep its
            # cause instead of turning that reset into a second, false watchdog.
            if not (missing_input and engine.supervisor.state is HILState.SAFE_HOLD):
                engine.disconnect(dispatch_now, 'operator_watchdog_before_publication')
            # Owner.hold flushes trajectory state; fresh packets cannot resume it.
            if not recovery_hold:
                owner.hold(dispatch_now/1e9)
                self.source = 'input_pause_hold' if getattr(engine, 'input_pause', None) is not None else 'hold'
                self.candidate = self.policy = None
        try:
            command, measured, evidence = owner.publish(dispatch_now/1e9)
        except StationaryOutputTimingFault as exc:
            evidence = owner.last_evidence
            self.event('publication_failed', now, error=str(exc), evidence=evidence,
                       cycle_timing_ms=cycle_timing)
            if (not self.preventive or self.home is not None
                    or evidence.get('publications') or evidence.get('publication_complete')):
                self.event('output_timing_recovery_rejected', dispatch_now,
                    reason='runtime_not_eligible', preventive=self.preventive, home_active=self.home is not None,
                    publication_complete=evidence.get('publication_complete'), publications=evidence.get('publications'))
                raise
            try:
                _, measured, held = self._check_safe_anchor()
            except Exception as safety_error:
                self.event('output_timing_recovery_rejected', dispatch_now,
                           reason='unsafe_or_stale_anchor', error=str(safety_error))
                raise
            details = dict(error=str(exc), evidence=evidence,
                           measured=measured.tolist(), held=held.tolist())
            if not engine.pause_stationary_output(dispatch_now, details):
                self.event('output_timing_recovery_rejected', dispatch_now, reason='control_not_eligible',
                    state=engine.supervisor.state.value, hold_reason=engine.supervisor.hold_reason,
                    hard_hold_reason=engine.hard_hold_reason, rb=None if packet is None else packet.deadman,
                    home=None if packet is None else packet.home_reset)
                raise
            self.reset_targets(dispatch_now, packet)
            self.timing_healthy_since = self.timing_last_publish = None
            self.source = 'input_pause_hold'
            self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
            # Failed sample publishes nothing and must not fabricate a control row.
            return
        except Exception as exc:
            self.event('publication_failed', now, error=str(exc), evidence=owner.last_evidence)
            raise
        pause = getattr(engine, 'input_pause', None)
        if pause and (pause.get('reason') == 'stationary_output_timing' or pause.get('output_timing_recovery_required')):
            if (self.timing_last_publish is None
                    or not 0 <= dispatch_now-self.timing_last_publish <= 50_000_000):
                self.timing_healthy_since = dispatch_now
            self.timing_last_publish = dispatch_now
        sup = engine.supervisor
        if getattr(engine, 'rlt_final_pending', False):
            from .rlt_tracking import endpoint_tracking
            evidence['rlt_endpoint_tracking'] = endpoint_tracking(
                owner.output, engine.rlt_final_target, command, measured,
                self.profile['joint_tolerance_deg'])
            evidence['rlt_endpoint_tracking']['settle_timeout_s'] = self.profile['settle_timeout_s']
        if not self.recorder.control(now=evidence['completed_monotonic_ns'], state=sup.state.value,
                intervention=sup.intervention_id, epoch=sup.policy_epoch,
                selected=owner.selected, submitted=command if owner.powered else None,
                source=self.source, measured=measured, sequence=owner.sequence,
                candidate=self.candidate, policy=self.policy,
                policy_request_id=engine.chunk_request_id,
                policy_sampling=engine.control_sampling() if hasattr(engine, 'control_sampling') else engine.policy_sampling,
                ik_diagnostics=dict(sigma_min=getattr(self.arm,'singularity_sigma_min',None),
                    speed_scale=getattr(self.arm,'singularity_scale',None),
                    damping=getattr(self.arm,'solver_damping',None),
                    target_limit=getattr(self.arm,'last_target_limit_reason',None),
                    seed=getattr(self.arm,'seed_diagnostics',None),
                    constraints=getattr(getattr(self.arm,'ik',None),'limit_diagnostics',None)),
                **{k: v for k, v in evidence.items() if k != 'command_sequence'}):
            from .faults import RecordingUnavailable
            raise RecordingUnavailable('control journal full/failed')
        if recovery_hold:
            engine.recovery_hold_committed(evidence['completed_monotonic_ns'])
        if rlt_hold:
            engine.rlt_hold_committed(evidence, powered=owner.powered)
        if hasattr(engine, 'after_publication'):
            engine.after_publication(evidence, powered=owner.powered)


class SupervisedControlLoop(ControlLoop):
    """Keep supervision alive while a failed action path has no authority.

    Only typed feedback/expired-target faults retry automatically, and that
    retry merely restores safe HOLD. Motion still needs the existing human
    handshake. All unclassified/output/publication faults stay latched until
    the operator exits; no catch-and-continue publication is permitted.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = None

    def _notice(self, event, now, **fields):
        try:
            self.engine.emit(event, monotonic_ns=now, **fields)
        except Exception:
            pass  # A broken journal cannot recursively break the fault handler.

    def _recording_failed(self, now, error):
        """End collection; explicit fresh START/RB may permit manual safe exit.

        This mode never reopens a dataset, hands back to policy or runs Home.
        Physical gates and publication health remain the original owner's.
        """
        e = self.engine
        if not e.started or e.packet is None or e.hard_hold_reason is not None:
            self._suspend(now, error, retryable=False)
            return
        try:
            self._check_safe_anchor()
        except Exception as unsafe:
            self._suspend(now, unsafe, retryable=False)
            return
        e.recording_failed = dict(reason=str(error), since_ns=now, collecting=False)
        if hasattr(e, '_finish_local'):
            try: e._finish_local(now, 'interrupted', 'recording_failed')
            except Exception: pass
        episodes = e.episodes
        if episodes:
            try: episodes.finish(now, reason='recording_failed', status='failed', interrupted=True)
            except Exception: pass
        from .unrecorded_egress import UnrecordedEgress
        e.recorder = e.sink = self.recorder = UnrecordedEgress()
        e.episodes = e.episode_buttons = None
        e.unified_episodes = False
        e.ending_episode = e.home_request = False
        e.strict_recording = False
        e.acquisition_error = None
        e._input_resume_lease = None
        e.startup_policy_authorized = False
        e.policy_enabled = False
        e.invalidate(now)
        e._start_manual_pause(now, 'recording_failed', invalid_from=now, takeover_required=True)
        e.supervisor.state = HILState.SAFE_HOLD
        e.supervisor.hold_reason = 'input_wait_start_takeover'
        self.reset_targets(now, self.last_packet)
        self._notice('recording_failed', now, collecting=False, requires_start=True,
                     reason=str(error), home_allowed=False, policy_allowed=False)

    def _suspend(self, now, error, *, retryable):
        e = self.engine
        retryable = retryable and not getattr(e, 'hard_hold_reason', None)
        fault = dict(reason=type(error).__name__ + ': ' + str(error)[:500],
                     since_ns=now, retryable=retryable, motion_authorized=False)
        self.fault = e.runtime_fault = fault
        e.policy_enabled = False
        try:
            if retryable:
                e.pause_runtime_condition(now, 'runtime_wait:' + type(error).__name__, fault)
            else:
                e.disconnect(now, 'runtime_latched:' + fault['reason'])
        except Exception:
            # Fault reporting itself may have encountered the failed writer.
            # Force in-memory revocation independently of every I/O path.
            pass
        e.supervisor.policy_epoch += 1
        e.supervisor.state = HILState.SAFE_HOLD
        e.supervisor.hold_reason = ('runtime_wait_feedback' if retryable else 'runtime_fault_latched')
        e.chunk = e.ready = None
        e.minimum_observation_ns = now
        try:
            self.arm.release()
            self.owner.hold(now / 1e9)  # No publication, no cached feedback approval.
            if not retryable:
                self.owner.close()
        except Exception:
            fault['owner_cleanup_failed'] = True
            fault['retryable'] = False
        self._notice('runtime_fault_paused', now, **fault, training_gap=True)
        if not fault['retryable'] and getattr(e, 'episodes', None):
            try:
                e.episodes.finish(now, reason=fault['reason'], interrupted=True)
            except Exception:
                fault['recording_cleanup_failed'] = True

    def step(self, now, incoming=None, disconnected=None):
        from .standalone import StandaloneFinished
        if self.fault is not None:
            if not self.fault['retryable']:
                return  # Status/receiver/stop remain alive; no actuator calls.
            try:
                self._check_safe_anchor()
                self.reset_targets(now, self.last_packet)
            except FeedbackUnavailable:
                return
            except Exception as error:
                self._suspend(now, error, retryable=False)
                return
            self.fault = self.engine.runtime_fault = None
            if self.engine.input_pause is not None:
                self.engine.input_pause.update(awaiting_session=True, neutral=None,
                    previous_rb=True, proof_after=now)
            self.previous = None
            self._notice('runtime_feedback_restored', now, requires_reauthorization=True,
                         training_gap=True)
            # Do not replay an input that arrived while feedback was missing.
            return
        try:
            return super().step(now, incoming, disconnected)
        except StandaloneFinished:
            raise
        except (FeedbackUnavailable, TargetExpired) as error:
            self._suspend(now, error, retryable=True)
        except Exception as error:
            from .faults import RecordingUnavailable
            if isinstance(error, RecordingUnavailable):
                self._recording_failed(now, error)
            else:
                self._suspend(now, error, retryable=False)


def receiver_status(engine):
    """The same logical mode is reported by the console and the ACK thread."""
    if getattr(engine, 'runtime_fault', None):
        return 'runtime_wait_feedback' if engine.runtime_fault['retryable'] else 'runtime_fault_latched'
    if getattr(engine, 'input_pause', None) is not None:
        return engine.supervisor.hold_reason or 'input_wait_release_rb'
    state = engine.supervisor.state.value if engine.started else 'waiting_for_start'
    episodes = getattr(engine, 'episodes', None)
    if episodes and state != 'safe_hold':
        if getattr(engine, 'unified_episodes', False):
            return {'idle':'episode_ready', 'starting':'episode_starting', 'saving':'episode_saving', 'failed':'failed'}.get(episodes.phase, state)
        return {'idle':'demonstration_ready', 'starting':'demonstration_starting',
                'saving':'demonstration_saving', 'failed':'failed'}.get(episodes.phase, state)
    if engine.teleop_only:
        return ('waiting_for_teleop_start' if not engine.started else
                ('manual_hold' if state == 'handoff_hold' else state))
    return state


class RuntimeReporter:
    """Sparse receiver-side evidence, including while waiting for first START."""
    def __init__(self, execution_mode, sink, *, emit=print, interval_ns=5_000_000_000):
        self.execution_mode, self.sink, self.emit = execution_mode, sink, emit
        self.interval_ns, self.next_report, self.previous = interval_ns, 0, None

    def report(self, now, engine, incoming):
        packet, received = incoming if incoming else (None, None)
        fresh = received is not None and 0 <= now-received <= engine.watchdog_ns
        fields = dict(execution=self.execution_mode.upper(), state=receiver_status(engine),
            epoch=engine.supervisor.policy_epoch, hold_reason=engine.supervisor.hold_reason,
            input_fresh=fresh, session=None if packet is None else packet.session_id,
            start_toggle=None if packet is None else packet.hil_toggle_id,
            start_pressed=False if packet is None else packet.hil_button_pressed,
            side=None if packet is None else packet.active_side,
            rb=False if packet is None else packet.deadman,
            home=False if packet is None else packet.home_reset,
            warning=None if packet is None else packet.warning)
        fields['home_stop_reason'] = getattr(engine, 'home_stop_reason', None)
        fields['home_stage'] = getattr(engine, 'home_stage', None)
        pause = getattr(engine, 'input_pause', None)
        fields['recovery_reason'] = None if pause is None else pause.get('reason')
        fields['recovery_required'] = None if pause is None else 'release_neutral_rb'
        fields['recovery_error'] = None if pause is None else pause.get('recovery_error')
        if hasattr(engine, 'rlt_phase'):
            fields['rlt_phase'] = engine.rlt_phase
            # Read the identity adopted at the real local-task boundary. This
            # is telemetry only; service startup health may still name BC400.
            identity = getattr(getattr(engine, 'rlt_policy', None), 'rlt_identity', {})
            if not isinstance(identity, dict):
                identity = {}
            local = getattr(engine, 'local_task', None)
            fields['rlt_segment_id'] = getattr(local, 'segment_id', None)
            for key in ('actor_version', 'checkpoint_step', 'q_guidance_updates'):
                fields[key] = identity.get(key)
        signature = tuple(fields.values())
        if signature == self.previous and now < self.next_report:
            return
        self.previous, self.next_report = signature, now+self.interval_ns
        fields['packet_seq'] = None if packet is None else packet.sequence
        fields['packet_age_ms'] = None if received is None else round((now-received)/1e6, 1)
        fields['input_paused'] = getattr(engine, 'input_pause', None) is not None
        if hasattr(engine, 'input_timing'):
            fields['input_timing'] = engine.input_timing.snapshot()
        with engine.latest_lock:
            observation = None if engine.latest is None else engine.latest[0]
        fields['observation_id'] = None if observation is None else observation.observation_id
        fields['observation_age_ms'] = (None if observation is None else
            round((now-observation.available_monotonic_ns)/1e6, 1))
        if observation is not None:
            fields['camera_age_ms'] = {frame.camera: round((now-frame.host_received_monotonic_ns)/1e6, 1)
                                       for frame in observation.frames}
        camera_diagnostics = getattr(engine.source, 'diagnostics', None)
        if callable(camera_diagnostics):
            try:
                metrics = camera_diagnostics()
                fields['cameras'] = {key: metrics[key] for key in (
                    'pending_items', 'pending_bytes', 'peak_items', 'drop_counts',
                    'repeated_header_stamps', 'error') if key in metrics}
            except Exception as exc:
                fields['camera_diagnostics_error'] = str(exc)
        diagnostics = getattr(self.sink, 'diagnostics', None)
        if callable(diagnostics):
            try:
                metrics = diagnostics()
                fields['recording'] = {key: metrics[key] for key in (
                    'pending_items', 'pending_bytes', 'count', 'completed', 'drop_count',
                    'writer_error', 'last_rejection_reason', 'phase', 'episode_id',
                    'last_saved') if key in metrics}
            except Exception as exc:
                fields['diagnostics_error'] = str(exc)
        import json
        self.emit('F1 STATUS ' + json.dumps(fields, ensure_ascii=False), flush=True)
