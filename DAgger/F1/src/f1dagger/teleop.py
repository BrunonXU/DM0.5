"""Reuse the frozen Pink integrator; replace only its G2 model boundary."""
import numpy as np
from dagger_core.powered_teleop import ArmTargetIntegrator
from dagger_core.pink_ik import PinkRelativeArmIK, IKSafety
from dagger_core.model_audit import build_reduced_arm_model
from .contract import MODEL_NAMES, vector
from .constrained_ik import F1ConstrainedIK


class ArmBoundaryHold(RuntimeError):
    """A rejected F1 arm candidate; retain manual control at the last command."""
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.details = dict(details or {}, reason=reason)


class F1ArmTarget(ArmTargetIntegrator):
    def __init__(self, urdf, mapping, ik_config):
        import pinocchio as pin
        from pathlib import Path
        from .config import infra_import
        # The same manifest-bound planner used by the 4242 pose executor.
        self.home_plan_type = infra_import(Path(urdf).resolve().parents[4], 'robot.motion_plan').SmoothBodyPlan
        self.mapping = mapping
        model = pin.buildModelFromUrdf(str(urdf))
        names = {'left': list(MODEL_NAMES[:7]), 'right': list(MODEL_NAMES[7:14])}
        for name, limits in zip(MODEL_NAMES[:14], mapping.model_limits()):
            jid = model.getJointId(name)
            if jid == 0 or model.joints[jid].nq != 1:
                raise ValueError('missing F1 model joint ' + name)
            model.lowerPositionLimit[model.idx_qs[jid]], model.upperPositionLimit[model.idx_qs[jid]] = limits
        safety = IKSafety(**ik_config['safety'])
        super().__init__(pin, model, names, safety=safety)
        self.fixed_reference = None
        self.seed_diagnostics = None

    def release(self):
        super().release()
        self.fixed_reference = None
        self.seed_diagnostics = None

    def _seed_details(self, side, full, hardware_arm, source):
        offset = 0 if side == 'left' else 7
        measured = full[offset:offset+7]
        return dict(source=source, side=side, measured_arm_deg=measured.tolist(),
                    seed_arm_deg=hardware_arm.tolist(),
                    max_feedback_error_deg=float(np.max(np.abs(measured-hardware_arm))))

    def warmup(self, full22, state16):
        """Load Pink/QP native modules before entering the timed owner loop.

        A zero-twist solve exercises the exact lazy path used by first RB.
        No output owner is touched; all references are discarded afterward.
        """
        import time
        from types import SimpleNamespace
        timings = {}
        try:
            for side in ('left', 'right'):
                started = time.monotonic_ns()
                packet = SimpleNamespace(active_side=side, mode='translation',
                    axes=(0., 0., 0.), angular_axes=(0., 0., 0.), speed_scale=1.)
                self.target(packet, full22, state16, .02)
                timings[side+'_ms'] = (time.monotonic_ns()-started)/1e6
                self.release()
        finally:
            self.release()
        return timings

    def begin(self, side, full22, last_submitted=None):
        if side not in ('left', 'right'):
            raise ValueError('select an arm first')
        self.release()
        physical = vector(full22, 22)
        # A commanded configuration is the virtual controller state. Preserve
        # actual feedback for fixed-body FK, collision checks and recording;
        # never present a clipped measured angle as feedback. Re-clutching from
        # the last publication also prevents zero input from chasing a tracking
        # offset (the F1 J2 feedback can sit slightly beyond its legal command).
        offset = 0 if side == 'left' else 7
        seed = physical.copy()
        source = 'measured'
        if last_submitted is not None:
            command = self.mapping.check_action(last_submitted)
            seed[offset:offset+7] = command[offset:offset+7]
            source = 'last_submitted'
        model22 = self.mapping.to_model(seed)
        full_q = self.pin.neutral(self.full_model)
        for name, value in zip(MODEL_NAMES, model22):
            self._set(full_q, name, value)
        reduced = build_reduced_arm_model(self.pin, self.full_model, self.arm_joints[side], reference_q=full_q)
        if list(reduced.names)[1:] != self.arm_joints[side]:
            raise ValueError('unexpected F1 reduced joint order')
        offset = 0 if side == 'left' else 7
        arm = model22[offset:offset+7]
        self.ik = F1ConstrainedIK(reduced, 'arm_L_Link7' if side == 'left' else 'arm_R_Link7', arm, safety=self.safety)
        self.side = side
        self.ik_virtual_q = arm.copy()
        self.virtual_q = arm.copy()
        self.fixed_reference = physical[14:].copy()
        self.seed_diagnostics = self._seed_details(side, physical, seed[offset:offset+7], source)

    def _boundary(self, reason, full, command, details):
        """Discard a rejected tick and all target/filter lead before HOLD.

        Reuse the reduced model so persistent outward input does not rebuild
        the URDF/Pink model at 50 Hz. Only the virtual controller is reset;
        the caller's actual full-body feedback remains unchanged.
        """
        self.accept_safe_candidate(full, command)
        side = self.side
        offset = 0 if side == 'left' else 7
        arm = command[offset:offset+7]
        self.last_target_limit_reason = reason
        self.seed_diagnostics = self._seed_details(side, full, arm, 'last_submitted')
        raise ArmBoundaryHold(reason, dict(details, side=side,
            last_submitted_arm_deg=arm.tolist(),
            measured_arm_deg=full[offset:offset+7].tolist(),
            seed_source='last_submitted'))

    def accept_safe_candidate(self, full, command):
        """Discard rejected IK/filter lead while retaining the reduced model.

        The selected command is a controller reference, never sensor feedback.
        Output planner velocity/acceleration remains owned by the output owner.
        """
        side, ik = self.side, self.ik
        offset = 0 if side == 'left' else 7
        arm = command[offset:offset+7]
        q = np.deg2rad(arm * self.mapping.signs[offset:offset+7])
        super().release()
        self.side, self.ik = side, ik
        ik.configuration.update(q)
        pose = ik.configuration.get_transform_frame_to_world(ik.frame).copy()
        ik.takeover_q = q.copy()
        ik.takeover_pose = pose.copy()
        ik.target_pose = pose.copy()
        ik.frame_task.set_target(pose)
        ik.posture_task.set_target(q)
        self.ik_virtual_q = q.copy()
        self.virtual_q = q.copy()
        self.seed_diagnostics = self._seed_details(side, full, arm, 'approved_reference')

    def target(self, packet, full22, last_submitted, dt):
        full = vector(full22, 22)
        # Validate the source command independently; a bad gripper or inactive
        # arm must remain a fault, never be hidden by manual boundary handling.
        command = self.mapping.check_action(last_submitted)
        if self.ik is None or self.side != packet.active_side:
            self.begin(packet.active_side, full, command)
        elif np.max(np.abs(full[14:] - self.fixed_reference)) > .5:
            self.release()
            from .faults import ManualReferenceChanged
            raise ManualReferenceChanged('body/head reference changed; release and re-clutch')
        # Anchor to the last feasible virtual configuration each tick. Clear
        # only reversed filter components so input reverses on the next solve;
        # the output planner retains physical velocity/acceleration for braking.
        linear = np.asarray(packet.axes) if packet.mode != 'rotation' else np.zeros(3)
        angular = (np.asarray(packet.axes) if packet.mode == 'rotation' else
                   np.asarray(packet.angular_axes) if packet.mode != 'translation' else np.zeros(3))
        offset = 0 if self.side == 'left' else 7
        published_q = np.deg2rad(command[offset:offset+7]*self.mapping.signs[offset:offset+7])
        # A moving QP window bounds NEW increments around the publication.
        # Keep the previous feasible reference: withdrawing the entire lead
        # here forces the output planner to brake even under constant input.
        # Reversal clears only the reversed filter components below; the
        # bounded joint reference moves back through ordinary feasible steps.
        self.ik.arm_limits.set_publication(published_q, self.max_otg_ik_lag_rad)
        self.ik.anchor(self.ik_virtual_q)
        self.filtered_linear[self.filtered_linear*linear < 0.] = 0.
        self.filtered_angular[self.filtered_angular*angular < 0.] = 0.
        try:
            q, _ = self.step(mode=packet.mode, axes=packet.axes, angular_axes=packet.angular_axes,
                             speed_scale=packet.speed_scale, dt=dt)
        except Exception as exc:
            # The frozen shared solver wraps solve_ik failures in IKHold but
            # lets the final Configuration check propagate directly. Match
            # the actual typed error in either form, never a message substring.
            from pink.exceptions import NotWithinConfigurationLimits
            cause = exc
            while cause is not None and not isinstance(cause, NotWithinConfigurationLimits):
                cause = cause.__cause__
            if cause is None:
                raise
            self._boundary('pink_joint_limit', full, command, dict(
                joint_index=int(cause.joint), value_rad=float(cause.value),
                lower_rad=float(cause.lower), upper_rad=float(cause.upper)))
        result = command.copy()
        offset = 0 if self.side == 'left' else 7
        result[offset:offset+7] = np.rad2deg(vector(q, 7, 'IK candidate')) * self.mapping.signs[offset:offset+7]
        arm = result[offset:offset+7]
        limits = self.mapping.limits[offset:offset+7]
        outside = np.maximum(limits[:, 0]-arm, arm-limits[:, 1])
        if np.any(outside > 0):
            # Pink's 1e-6 rad configuration tolerance is not a HAL allowance.
            # Reject even numerical residue before it reaches the owner and
            # re-anchor to an actual legal publication, with no limit widening.
            self._boundary('arm_joint_limit', full, command, dict(
                candidate_arm_deg=arm.tolist(),
                joint_indices=np.flatnonzero(outside > 0).tolist(),
                outside_deg=np.maximum(outside, 0).tolist()))
        wrist = 1.025*abs(arm[5])+abs(arm[6])
        if wrist > 110.5+1e-7:
            self._boundary('arm_wrist_limit', full, command, dict(
                candidate_arm_deg=arm.tolist(), wrist_sum_deg=float(wrist)))
        return self.mapping.check_action(result)


class GripperIntent:
    """Ignore pre-takeover RT latch values; require a new per-side transition."""
    def __init__(self):
        self.latches = {}
        self.positions = None

    def anchor(self, command, packet):
        self.positions = vector(command, 16)[14:]
        self.latches = {packet.active_side: packet.grip_target} if packet and packet.active_side else {}

    def update(self, packet):
        side = packet.active_side
        if side:
            known = side in self.latches
            previous = self.latches.get(side)
            # None is an observed pre-click baseline, not an unknown side.
            # Preserve it so the first new RT click after selection/Home acts.
            # An unseen side's first non-None value may be an old sender latch.
            self.latches[side] = packet.grip_target
            if known and packet.grip_target is not None and previous != packet.grip_target:
                self.positions[0 if side == 'left' else 1] = packet.grip_target * 100.
        return self.positions.copy()


class HomeMotion:
    """Check incremental home waypoints before committing the F1 planner."""
    def __init__(self, profile, start, target):
        from pico_f1.trajectory import JerkLimitedTrajectory
        self.goal = vector(target, 16)
        self.planner = JerkLimitedTrajectory(14, .02,
            profile['max_joint_speed_deg_s'], profile['max_joint_acceleration_deg_s2'],
            300., 30., .0001)
        self.planner.reset(vector(start, 16)[:14])
        self.pending = None

    def propose(self, dt):
        self.pending = self.planner.propose(self.goal[:14], dt=dt)
        return np.r_[self.pending.position, self.goal[14:]]

    def commit(self):
        self.planner.commit(self.pending)
        self.pending = None
