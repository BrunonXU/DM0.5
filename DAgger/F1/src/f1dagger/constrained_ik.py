"""F1-only differential IK with a feasible, non-accumulating pose reference."""
import numpy as np
from dagger_core.pink_ik import PinkRelativeArmIK, IKCandidate, IKHold


class ArmLimits:
    """Linear limits on delta-q (model radians), including the coupled wrist."""
    def __init__(self, model, safety):
        self.model, self.safety = model, safety
        self.published_q = None
        self.max_lead_rad = None
        wrist = np.zeros((4, 7))
        # Absolute values make the four planes invariant under HAL axis signs.
        for row, (a, b) in zip(wrist, ((1,1), (1,-1), (-1,1), (-1,-1))):
            row[5:7] = (1.025*a, b)
        self.A = np.vstack((np.eye(7), -np.eye(7), wrist))
        self.b = np.r_[model.upperPositionLimit, -model.lowerPositionLimit,
                       np.full(4, np.deg2rad(110.5))]

    def set_publication(self, q, max_lead_rad):
        q = np.asarray(q, dtype=float)
        if q.shape != (7,) or not np.isfinite(q).all() or not np.isfinite(max_lead_rad) or max_lead_rad <= 0:
            raise ValueError('invalid F1 publication lead bound')
        self.published_q = q.copy()
        self.max_lead_rad = float(max_lead_rad)

    def compute_qp_inequalities(self, configuration, dt):
        distance = self.b-self.A@configuration.q
        if np.min(distance) < -1e-10:
            raise IKHold('F1 IK reference outside feasible joint/wrist set')
        speed = min(self.safety.max_joint_speed_rad_s*dt, self.safety.max_joint_step_rad)
        # Zero displacement is always feasible, including exactly on a plane.
        # Merge parallel joint-box, velocity and lead planes rather than
        # duplicating identical normals at an active corner of the QP.
        G = self.A
        h = np.maximum(distance, 0.)*.95
        h[:14] = np.minimum(h[:14], speed)
        if self.published_q is not None:
            lead = configuration.q-self.published_q
            # Zero increment remains feasible if output momentum moves the
            # publication away during braking. Such existing excess is not
            # pulled back discontinuously and cannot grow through a new IK
            # increment; inward and tangent increments remain available.
            room = np.maximum(np.r_[self.max_lead_rad-lead,
                                     self.max_lead_rad+lead], 0.)
            h[:14] = np.minimum(h[:14], room)
        return G, h


class F1ConstrainedIK(PinkRelativeArmIK):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.arm_limits = ArmLimits(self.model, self.safety)
        self.limit_diagnostics = {}
        # Incremental velocity tracking: gain 1 gives the requested displacement
        # per tick. There is no persistent Cartesian error to integrate away.
        self.frame_task.gain = 1.

    def anchor(self, q):
        self.configuration.update(q)
        pose = self.configuration.get_transform_frame_to_world(self.frame).copy()
        self.target_pose = pose
        self.takeover_pose = pose.copy()
        self.frame_task.set_target(pose)
        self.posture_task.set_target(np.asarray(q))

    def solve(self, measured_q, dt):
        q = np.asarray(measured_q, dtype=float)
        if q.shape != (7,) or not np.isfinite(q).all() or not 0 < dt <= self.safety.max_dt_s:
            raise IKHold('invalid F1 IK reference or dt')
        self.configuration.update(q)
        _, scale, damping = self.jacobian_policy(q)
        G, h = self.arm_limits.compute_qp_inequalities(self.configuration, dt)
        # Unknown numerical failures still fault; they are not called saturation.
        velocity = np.asarray(self.pink.solve_ik(self.configuration,
            [self.frame_task, self.posture_task], dt, self.solver,
            damping=damping, limits=[self.arm_limits], safety_break=True,
            eps_abs=1e-10, eps_rel=0.))
        if velocity.shape != (7,) or not np.isfinite(velocity).all():
            raise IKHold('non-finite F1 IK velocity')
        step = velocity*dt
        violation = G@step-h
        if np.max(violation) > 1e-6:
            raise IKHold('F1 QP constraint residual exceeds numerical tolerance')
        # Remove only feasibility residue, plane by plane. Scaling the whole
        # step at an active plane would wrongly discard tangential movement.
        correction = 0.
        for _ in range(20):
            residual = G@step-h
            if np.max(residual) <= 0.:
                break
            for row, excess in zip(G, residual):
                if excess > 0.:
                    delta = (excess+1e-14)*row/np.dot(row,row)
                    step -= delta
                    correction += float(np.linalg.norm(delta))
        if np.max(G@step-h) > 1e-12 or correction > 1e-5:
            raise IKHold('F1 QP numerical repair could not certify feasibility')
        candidate = q+step
        requested_pose = self.target_pose.copy()
        self.configuration.update(candidate)
        actual = self.configuration.get_transform_frame_to_world(self.frame).copy()
        position_error = float(np.linalg.norm(actual.translation-requested_pose.translation))
        orientation_error = float(np.linalg.norm(self.pin.log3(actual.rotation@requested_pose.rotation.T)))
        margins = self.arm_limits.b-self.arm_limits.A@candidate
        self.limit_diagnostics = dict(min_constraint_margin_rad=float(np.min(margins)),
            active_planes=np.flatnonzero(margins < 1e-5).tolist(),
            discarded_translation_m=position_error, discarded_rotation_rad=orientation_error,
            numerical_correction_rad=correction, reference='feasible_fk_each_tick',
            lead_governor='continuous_increment_qp',
            publication_lead_rad=None if self.arm_limits.published_q is None else
                (candidate-self.arm_limits.published_q).tolist(),
            publication_lead_limit_rad=self.arm_limits.max_lead_rad)
        # Keep only achievable pose. A prolonged outward command adds no debt.
        self.anchor(candidate)
        singular = np.linalg.svd(self.pin.computeFrameJacobian(self.model,
            self._diagnostic_data, candidate, self._frame_id,
            self.pin.ReferenceFrame.LOCAL_WORLD_ALIGNED), compute_uv=False)
        return IKCandidate(candidate, step/dt, position_error, orientation_error,
            candidate-self.model.lowerPositionLimit, self.model.upperPositionLimit-candidate,
            singular, scale, damping)
