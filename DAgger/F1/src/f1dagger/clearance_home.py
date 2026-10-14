"""Feedback-gated right-arm lift, base-right clearance, then configured Home.

No obstacle perception: the two clearances are an explicitly taught task route.
Every candidate still goes through the existing owner/limits/collision gate.
"""
import numpy as np
from .contract import clearance_config, vector
from .teleop import HomeMotion


class ClearanceHomeStopped(RuntimeError):
    """End this Home in HOLD without ending the operator's recording."""


class ClearanceHomeMotion:
    enabled = (False, True)
    position_tolerance_m = .002
    orientation_tolerance_rad = np.deg2rad(2.)
    settle_ns = 150_000_000

    def __init__(self, profile, start, goal, arm, full, now):
        route = clearance_config(profile['f710_home'])
        self.profile, self.arm = profile, arm
        self.linear_speed = route.get('linear_speed_m_s', arm.NOMINAL_LINEAR_SPEED_M_S)
        if self.linear_speed > arm.safety.max_linear_speed_m_s:
            raise ValueError('clearance speed exceeds configured IK linear speed limit')
        self.start, self.goal = vector(start, 16), vector(goal, 16)
        self.goal[:7] = self.start[:7]
        self.goal[14:] = self.start[14:]
        full = vector(full, 22)
        if np.max(np.abs(full[7:14]-self.start[7:14])) > profile['joint_tolerance_deg']:
            raise ClearanceHomeStopped('clearance start feedback not aligned with last publication')
        self.fixed = full[14:].copy()
        arm.begin('right', full, self.start)
        self.ik, self.pin = arm.ik, arm.pin
        self.fk_data = self.ik.model.createData()
        origin = self.pose(full[7:14])
        self.rotation = origin.rotation.copy()
        # URDF base_link: +X forward, +Y left, +Z up. Right is -Y.
        self.lift = origin.translation + np.array([0., 0., route['lift_m']])
        self.clear = self.lift + np.array([0., -route['right_m'], 0.])
        self.stage, self.done, self.pending = 'lift', False, None
        self.joint_motion = None
        self.settled_since = None
        self.deadline = now + int((10+route['lift_m']/arm.NOMINAL_LINEAR_SPEED_M_S*6)*1e9)
        self.route = route
        self.command_translation = self.lift.copy()
        self.command_rotation = self.rotation.copy()
        self.stationary_since = None
        self.stationary_command = None
        self.stationary_feedback = None
        self.relaxed = route['schema'] == 'f1_right_clearance_v2'
        if self.relaxed:
            self.rotation_budget = np.deg2rad(route['max_rotation_deg'])
            # The clearance is a position task with a soft orientation
            # preference. Never bend wrist J6 farther out than its starting
            # command; this is a task constraint, not a guessed hardware limit.
            self.ik.frame_task.cost[3:] = .003
            wrist = arm.ik_virtual_q[5]
            self.ik.arm_limits.b[5] = abs(wrist)
            self.ik.arm_limits.b[12] = abs(wrist)
            # A freshly initialized owner starts at measured feedback, which
            # need not equal the saved nominal goal to 0.01 degree. Use the
            # existing physical arrival tolerance for BOTH references here;
            # the short return still settles the published goal normally.
            if (np.max(np.abs(self.start[7:14]-self.goal[7:14])) <= profile['joint_tolerance_deg']
                    and np.max(np.abs(full[7:14]-self.goal[7:14])) <= profile['joint_tolerance_deg']):
                self.stage = 'return'
                self.joint_motion = HomeMotion(profile, self.start, self.goal)

    def pose(self, hardware_arm):
        q = np.deg2rad(vector(hardware_arm, 7)*self.arm.mapping.signs[7:14])
        self.pin.forwardKinematics(self.ik.model, self.fk_data, q)
        self.pin.updateFramePlacements(self.ik.model, self.fk_data)
        return self.fk_data.oMf[self.ik.model.getFrameId(self.ik.frame)].copy()

    def _pose_close(self, pose, target, rotation=None):
        return (np.linalg.norm(pose.translation-target) <= self.position_tolerance_m
                and (self.relaxed or np.linalg.norm(self.pin.log3(pose.rotation@(self.rotation if rotation is None else rotation).T)) <= self.orientation_tolerance_rad))

    def _arrived(self, measured, submitted):
        if self.stage == 'return':
            return (np.max(np.abs(measured[7:14]-self.goal[7:14])) <= self.profile['joint_tolerance_deg']
                    and np.max(np.abs(submitted[7:14]-self.goal[7:14])) <= .01)
        target = self.lift if self.stage == 'lift' else self.clear
        if self.relaxed and np.linalg.norm(self.pin.log3(
                self.pose(measured[7:14]).rotation@self.pose(submitted[7:14]).rotation.T)) > self.orientation_tolerance_rad:
            return False
        return (self._pose_close(self.pose(measured[7:14]), target)
                and self._pose_close(self.pose(submitted[7:14]), self.command_translation, self.command_rotation))

    def _correct_settled_feedback(self, submitted, measured, stamps, now):
        """Estimate a bounded static bias only after publication has stopped.

        Recompute from command minus feedback, never integrate tracking error.
        Moving/delayed feedback cannot build a deferred correction. A tracking
        error outside the existing Home joint tolerance stops this Home.
        """
        command, actual = submitted[7:14], measured[7:14]
        command_pose = self.pose(command)
        at_command = self._pose_close(command_pose, self.command_translation, self.command_rotation)
        if (not at_command or self.stationary_command is None
                or np.max(np.abs(command-self.stationary_command)) > .01
                or np.max(np.abs(actual-self.stationary_feedback)) > self.profile['joint_tolerance_deg']):
            self.stationary_since = now if at_command else None
            self.stationary_command = command.copy() if at_command else None
            self.stationary_feedback = actual.copy() if at_command else None
            return
        if now-self.stationary_since < self.settle_ns or min(stamps[7:14]) <= self.stationary_since:
            return
        error = command-actual
        if np.max(np.abs(error)) > self.profile['joint_tolerance_deg']:
            raise ClearanceHomeStopped('clearance Home '+self.stage+' tracking error: '
                +str(float(np.max(np.abs(error))))+' deg exceeds existing joint tolerance')
        actual_pose = self.pose(actual)
        nominal = self.lift if self.stage == 'lift' else self.clear
        # Replace, rather than add to, the previous compensation. The measured
        # physical waypoint and its arrival tolerances never move.
        self.command_translation = nominal+command_pose.translation-actual_pose.translation
        if not self.relaxed:
            self.command_rotation = self.rotation@actual_pose.rotation.T@command_pose.rotation
        self.stationary_since = None
        self.stationary_command = self.stationary_feedback = None

    def _advance(self, now, submitted):
        self.settled_since = None
        self.stationary_since = None
        self.stationary_command = self.stationary_feedback = None
        if self.stage == 'lift':
            self.stage = 'right'
            self.command_translation += self.clear-self.lift
            self.deadline = now + int((10+self.route['right_m']/self.arm.NOMINAL_LINEAR_SPEED_M_S*6)*1e9)
        elif self.stage == 'right':
            self.stage = 'return'
            self.joint_motion = HomeMotion(self.profile, submitted, self.goal)
            distance = float(np.max(np.abs(self.goal[7:14]-submitted[7:14])))
            self.deadline = now + int((10+distance/self.profile['max_joint_speed_deg_s']*3)*1e9)
        else:
            self.done = True

    @staticmethod
    def _bounded_velocity(delta, speed, dt):
        length = float(np.linalg.norm(delta))
        return delta/dt if length <= speed*dt else delta*(speed/length)

    def propose(self, dt, full, submitted, measured, stamps, now):
        full, submitted, measured = vector(full, 22), vector(submitted, 16), vector(measured, 16)
        if not np.isfinite(dt) or not 0 < dt <= self.arm.safety.max_dt_s:
            raise RuntimeError('invalid clearance control interval')
        if np.max(np.abs(full[14:]-self.fixed)) > .5:
            raise RuntimeError('body/head reference changed during clearance Home')
        if now > self.deadline:
            raise ClearanceHomeStopped('clearance Home '+self.stage+' feedback timeout')
        if self.relaxed and self.stage != 'return':
            for action in (submitted, measured):
                if np.linalg.norm(self.pin.log3(self.pose(action[7:14]).rotation@self.rotation.T)) > self.rotation_budget:
                    raise ClearanceHomeStopped('clearance Home orientation budget exceeded')
        if self._arrived(measured, submitted):
            if self.settled_since is None:
                self.settled_since = now
            elif now-self.settled_since >= self.settle_ns and min(stamps[7:14]) > self.settled_since:
                self._advance(now, submitted)
        else:
            self.settled_since = None
        if self.done:
            return submitted.copy()
        if self.stage == 'return':
            self.pending = 'joint'
            return self.joint_motion.propose(dt)
        if self.settled_since is None:
            self._correct_settled_feedback(submitted, measured, stamps, now)
        arm, ik = self.arm, self.ik
        q = arm.ik_virtual_q
        ik.arm_limits.set_publication(np.deg2rad(submitted[7:14]*arm.mapping.signs[7:14]), arm.max_otg_ik_lag_rad)
        ik.anchor(q)
        target = self.command_translation
        current = ik.target_pose
        sigma, scale, damping = ik.jacobian_policy(q)
        # Limit each incremental Cartesian request; unreachable targets time out
        # instead of accumulating a deferred motion or skipping the clearance.
        linear = self._bounded_velocity(target-current.translation, self.linear_speed*scale, dt)
        rotation = self.pin.log3(self.command_rotation@current.rotation.T)
        angular = self._bounded_velocity(rotation, arm.NOMINAL_ANGULAR_SPEED_RAD_S*scale, dt)
        ik.advance_target(linear, angular, dt)
        candidate = ik.solve(q, dt)
        arm.singularity_sigma_min, arm.singularity_scale, arm.solver_damping = sigma, scale, damping
        arm.last_candidate = candidate
        result = self.start.copy()
        result[7:14] = np.rad2deg(candidate.q)*arm.mapping.signs[7:14]
        if self.relaxed and np.linalg.norm(self.pin.log3(self.pose(result[7:14]).rotation@self.rotation.T)) > self.rotation_budget:
            raise ClearanceHomeStopped('clearance Home orientation budget exceeded')
        self.pending = candidate.q.copy()
        return arm.mapping.check_action(result)

    def commit(self):
        if isinstance(self.pending, str):
            self.joint_motion.commit()
        elif self.pending is not None:
            self.arm.ik_virtual_q = self.pending.copy()
            self.arm.virtual_q = self.pending.copy()
        self.pending = None

    def retry_context(self):
        """Geometric checkpoint only, never a queued command or trajectory."""
        return dict(stage=self.stage, lift=self.lift.copy(), clear=self.clear.copy(),
                    rotation=self.rotation.copy(), fixed=self.fixed.copy())


class FeedbackAlignedClearanceHome(ClearanceHomeMotion):
    """An explicit new Y may unload an old target before planning from feedback.

    Never overwrite publication history with measurements. Move the *command*
    towards a captured measured right-arm pose through the existing bounded
    planner/owner, then require fresh arrival before constructing the route.
    A failed Home does not start this recovery automatically.
    """
    def __init__(self, profile, start, goal, arm, full, now, *, retry=None):
        start, full = vector(start, 16), vector(full, 22)
        self.retry = retry
        route = clearance_config(profile['f710_home'])
        if route.get('linear_speed_m_s', arm.NOMINAL_LINEAR_SPEED_M_S) > arm.safety.max_linear_speed_m_s:
            raise ValueError('clearance speed exceeds configured IK linear speed limit')
        self.aligning = np.max(np.abs(full[7:14]-start[7:14])) > profile['joint_tolerance_deg']
        if not self.aligning:
            super().__init__(profile, start, goal, arm, full, now)
            self._restore_checkpoint(full, start, now)
            return
        self.profile, self.arm = profile, arm
        self.start, self.goal = start.copy(), vector(goal, 16)
        self.goal[:7], self.goal[14:] = start[:7], start[14:]
        anchor = start.copy()
        anchor[7:14] = full[7:14]
        arm.mapping.check_action(anchor)
        self.anchor = anchor
        self.fixed = full[14:].copy()
        self.joint_motion = HomeMotion(profile, start, anchor)
        self.stage, self.done, self.pending = 'align', False, None
        self.settled_since = None
        distance = float(np.max(np.abs(anchor[7:14]-start[7:14])))
        self.deadline = now + int((10+distance/profile['max_joint_speed_deg_s']*3)*1e9)

    def retry_context(self):
        return self.retry if self.aligning else super().retry_context()

    def _restore_checkpoint(self, full, submitted, now):
        if self.retry is None:
            return
        if np.max(np.abs(full[14:]-self.retry['fixed'])) > .5:
            raise ClearanceHomeStopped('clearance retry body/head reference changed')
        self.stage = self.retry['stage']
        if self.stage not in ('lift', 'right', 'return'):
            raise ClearanceHomeStopped('invalid clearance retry stage')
        self.lift, self.clear = self.retry['lift'].copy(), self.retry['clear'].copy()
        self.rotation, self.fixed = self.retry['rotation'].copy(), self.retry['fixed'].copy()
        self.command_translation = (self.lift if self.stage == 'lift' else self.clear).copy()
        self.command_rotation = self.rotation.copy()
        if self.stage == 'return':
            self.joint_motion = HomeMotion(self.profile, submitted, self.goal)
            distance = float(np.max(np.abs(self.goal[7:14]-submitted[7:14])))
            self.deadline = now + int((10+distance/self.profile['max_joint_speed_deg_s']*3)*1e9)
        else:
            distance = self.route['lift_m'] if self.stage == 'lift' else self.route['right_m']
            self.deadline = now + int((10+distance/self.arm.NOMINAL_LINEAR_SPEED_M_S*6)*1e9)

    def propose(self, dt, full, submitted, measured, stamps, now):
        if not self.aligning:
            return super().propose(dt, full, submitted, measured, stamps, now)
        full, submitted, measured = vector(full, 22), vector(submitted, 16), vector(measured, 16)
        if not np.isfinite(dt) or not 0 < dt <= self.arm.safety.max_dt_s:
            raise RuntimeError('invalid clearance control interval')
        if np.max(np.abs(full[14:]-self.fixed)) > .5:
            raise RuntimeError('body/head reference changed during clearance Home')
        if now > self.deadline:
            raise ClearanceHomeStopped('clearance Home align feedback timeout')
        close = (np.max(np.abs(submitted[7:14]-self.anchor[7:14])) <= .01
                 and np.max(np.abs(measured[7:14]-submitted[7:14])) <= self.profile['joint_tolerance_deg']
                 and np.max(np.abs(full[7:14]-submitted[7:14])) <= self.profile['joint_tolerance_deg'])
        if close:
            if self.settled_since is None:
                self.settled_since = now
            elif now-self.settled_since >= self.settle_ns and min(stamps[7:14]) > self.settled_since:
                # Rebuild from fresh feedback. Only an explicitly retained
                # geometric checkpoint may survive an otherwise untouched Home.
                super().__init__(self.profile, submitted, self.goal, self.arm, full, now)
                self.aligning = False
                self._restore_checkpoint(full, submitted, now)
                return super().propose(dt, full, submitted, measured, stamps, now)
        else:
            self.settled_since = None
        self.pending = 'joint'
        return self.joint_motion.propose(dt)
