"""One local control loop for policy, F710, home, and final F1 publication."""
import time
import numpy as np
from dagger_core.hil_supervisor import HILState
from .contract import home_target
from .clearance_home import FeedbackAlignedClearanceHome as ClearanceHomeMotion, ClearanceHomeStopped
from .teleop import ArmBoundaryHold, GripperIntent, HomeMotion


class ControlLoop:
    def __init__(self, engine, owner, arm, recorder, profile, *, clock=time.monotonic_ns):
        self.engine, self.owner, self.arm, self.recorder, self.profile = engine, owner, arm, recorder, profile
        self.clock = clock
        self.gripper = GripperIntent()
        self.gripper.anchor(owner.last_submitted, None)
        self.home = None
        self.home_motion = None
        self.home_retry_context = None
        self.home_deadline = None
        self.home_settle = None
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
        self.engine.home_stop_reason = None
        self.engine.home_stage = None

    def event(self, event, now, **fields):
        if not self.recorder.record_event(event, monotonic_ns=now, **fields):
            raise RuntimeError('event journal rejected write')

    def reset_targets(self, now, packet):
        self.owner.hold(now / 1e9)
        self.arm.release()
        self.home = None
        self.home_settle = None
        self.home_motion = None
        self.engine.home_stage = None
        self.home_retry_context = None
        self.gripper.anchor(self.owner.last_submitted, packet)
        self.next_target = now
        self.last_target = None
        self.source = 'hold'
        self.candidate = self.policy = None
        self.arm_boundary = None

    def step(self, now, incoming=None, disconnected=None):
        engine, owner = self.engine, self.owner
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
        engine.tick(now)
        state = engine.supervisor.state
        episodes = getattr(engine, 'episodes', None)
        if episodes:
            if episodes.phase != 'recording':
                # Keep the owner in HOLD during open/commit; no disk waits on
                # this thread and no motion before an episode is ready.
                state = HILState.HANDOFF_HOLD if state is HILState.HIL_LATCHED else state
            elif self.recording_episode != episodes.run_id:
                self.recording_episode = episodes.run_id
                # Establish real, same-owner publication provenance in EACH
                # episode, including held grippers. Targets remain unchanged.
                owner.gripper_sent = [False, False]
        current = (state, engine.supervisor.policy_epoch)
        if current != self.previous:
            self.reset_targets(now, self.last_packet)
            self.previous = current
        if not engine.started:
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
        packet = self.last_packet
        full, measured, state_stamps = owner.source.feedback()
        if now >= self.next_target:
            dt = .02 if self.last_target is None else (now-self.last_target)/1e9
            self.last_target, self.next_target = now, now + 20_000_000
            target = owner.last_submitted.copy()
            enabled = (False, False)
            self.candidate = self.policy = None
            boundary_recovered = False
            try:
                if state is HILState.POLICY:
                    target, self.source = engine.sample(now, owner.last_submitted)
                    self.policy = target.copy() if self.source == 'policy' else None
                    enabled = (True, True) if self.source == 'policy' else (False, False)
                elif state is HILState.HIL_LATCHED and packet:
                    self.source = 'human_hold'
                    y_edge = packet.home_reset and not self.y_down
                    if y_edge and not packet.deadman and self.home is None:
                        engine.home_stop_reason = None
                        self.home = owner.mapping.check_action(home_target(self.profile, owner.last_submitted))
                        distance = float(np.max(np.abs(self.home[:14]-owner.last_submitted[:14])))
                        self.home_deadline = now + int((10+distance/self.profile['max_joint_speed_deg_s']*3)*1e9)
                        self.arm.release()
                        if self.profile.get('f710_home', {}).get('clearance') is not None:
                            options = {} if self.home_retry_context is None else {'retry': self.home_retry_context}
                            self.home_motion = ClearanceHomeMotion(self.profile, owner.last_submitted, self.home, self.arm, full, now, **options)
                        else:
                            self.home_motion = HomeMotion(self.profile, owner.last_submitted, self.home)
                        self.event('home_started', now, target=self.home.tolist(),
                                   stage=self.home_motion.stage if isinstance(self.home_motion, ClearanceHomeMotion) else 'return',
                                   preset=self.profile.get('f710_home', {}).get('name', 'LXL XY'))
                    if self.home is not None:
                        if isinstance(self.home_motion, ClearanceHomeMotion):
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
                        if completed:
                            self.event('home_completed_feedback', now, measured=measured.tolist())
                            engine.home_stage = None
                            self.home_retry_context = None
                            self.home = None
                            self.arm.release()
                            self.gripper.anchor(owner.last_submitted, packet)
                    else:
                        if packet.deadman:
                            try:
                                target = self.arm.target(packet, full, owner.last_submitted, dt)
                            except ArmBoundaryHold as exc:
                                # The arm adapter has discarded the rejected IK
                                # step. Flush output momentum too, retaining HIL,
                                # the selected side and the independent RT latch.
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
                                enabled = (packet.active_side == 'left', packet.active_side == 'right')
                                self.source = 'human_arm'
                                boundary_recovered = self.arm_boundary is not None
                        else:
                            self.arm.release()
                        target[14:] = self.gripper.update(packet)
                        if np.any(target[14:] != owner.last_submitted[14:]):
                            self.source = 'human_arm_and_hand' if any(enabled) else 'human_hand'
                        self.candidate = target.copy()
                else:
                    self.source = 'hold'
                self.y_down = bool(packet and packet.home_reset)
                owner.approve(target, now/1e9, enabled=enabled)
                if boundary_recovered:
                    self.event('arm_boundary_recovered', now, active_side=packet.active_side,
                               previous_reason=self.arm_boundary[1], stage='target_approved')
                    self.arm_boundary = None
                if self.source == 'home' and self.home_motion.pending is not None:
                    self.home_motion.commit()
            except ClearanceHomeStopped as exc:
                engine.home_stop_reason = str(exc)
                retry = (self.home_motion.retry_context() if isinstance(self.home_motion, ClearanceHomeMotion)
                         else self.home_retry_context)
                self.event('home_stopped', now, error=str(exc),
                           stage=getattr(self.home_motion, 'stage', 'initializing'), measured=measured.tolist(),
                           submitted=owner.last_submitted.tolist())
                self.reset_targets(now, packet)
                self.home_retry_context = retry
                # Consume Y even if propose() raised before y_down was updated.
                # A held button never restarts a failed route automatically.
                self.y_down = bool(packet and packet.home_reset)
            except Exception as exc:
                engine.disconnect(now, 'target_rejected:' + str(exc))
                self.reset_targets(now, packet)
                self.previous = (engine.supervisor.state, engine.supervisor.policy_epoch)
                self.event('target_rejected', now, error=str(exc))
        dispatch_now = self.clock()
        missing_input = engine.last_received_ns is None
        if missing_input or dispatch_now-engine.last_received_ns > engine.watchdog_ns:
            # A prior fault deliberately clears the receipt timestamp. Keep its
            # cause instead of turning that reset into a second, false watchdog.
            if not (missing_input and engine.supervisor.state is HILState.SAFE_HOLD):
                engine.disconnect(dispatch_now, 'operator_watchdog_before_publication')
            # Owner.hold flushes trajectory state; fresh packets cannot resume it.
            owner.hold(dispatch_now/1e9)
            self.source = 'hold'
        try:
            command, measured, evidence = owner.publish(dispatch_now/1e9)
        except Exception as exc:
            self.event('publication_failed', now, error=str(exc), evidence=owner.last_evidence)
            raise
        sup = engine.supervisor
        if not self.recorder.control(now=evidence['completed_monotonic_ns'], state=sup.state.value,
                intervention=sup.intervention_id, epoch=sup.policy_epoch,
                selected=owner.selected, submitted=command if owner.powered else None,
                source=self.source, measured=measured, sequence=owner.sequence,
                candidate=self.candidate, policy=self.policy,
                policy_request_id=engine.chunk_request_id, policy_sampling=engine.policy_sampling,
                ik_diagnostics=dict(sigma_min=getattr(self.arm,'singularity_sigma_min',None),
                    speed_scale=getattr(self.arm,'singularity_scale',None),
                    damping=getattr(self.arm,'solver_damping',None),
                    target_limit=getattr(self.arm,'last_target_limit_reason',None),
                    seed=getattr(self.arm,'seed_diagnostics',None),
                    constraints=getattr(getattr(self.arm,'ik',None),'limit_diagnostics',None)),
                **{k: v for k, v in evidence.items() if k != 'command_sequence'}):
            raise RuntimeError('control journal full/failed')


def receiver_status(engine):
    """The same logical mode is reported by the console and the ACK thread."""
    state = engine.supervisor.state.value if engine.started else 'waiting_for_start'
    episodes = getattr(engine, 'episodes', None)
    if episodes and state != 'safe_hold':
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
        signature = tuple(fields.values())
        if signature == self.previous and now < self.next_report:
            return
        self.previous, self.next_report = signature, now+self.interval_ns
        fields['packet_seq'] = None if packet is None else packet.sequence
        fields['packet_age_ms'] = None if received is None else round((now-received)/1e6, 1)
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
