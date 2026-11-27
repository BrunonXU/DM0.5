"""Separate mutable IK and FK state behind the existing numerical RPC socket."""
from copy import deepcopy
import threading

import numpy as np


MAX_FORWARD_TARGETS = 100
NUMERICAL_CLIENTS = 4
FORWARD_LOCK_TIMEOUT_S = .05


class ForwardKinematics:
    """Use the bound F1 model/mapping without constructing Pink for every sample.

    This has the same right-arm seed as F1ArmTarget.begin: replace only right7
    from the checked 16D reference and retain full22 for the other body joints.
    The model, Pinocchio Data and mapping arrays belong to this FK instance.
    """

    def __init__(self, arm, mapping):
        from f1dagger.contract import MODEL_NAMES, vector
        self.arm, self.mapping = arm, mapping
        self.names, self.vector = MODEL_NAMES, vector
        self.model, self.pin = arm.full_model, arm.pin
        self.data = self.model.createData()
        self.frame = self.model.getFrameId('arm_R_Link7')
        if not 0 <= self.frame < self.model.nframes:
            raise ValueError('right_eef_frame_missing')
        self.arm_q_idx = [self.model.idx_qs[self.model.getJointId(name)] for name in self.names[7:14]]
        self.arm_v_idx = [self.model.idx_vs[self.model.getJointId(name)] for name in self.names[7:14]]

    def _pose(self, full, held):
        seed = full.copy()
        seed[7:14] = held[7:14]
        model22 = self.mapping.to_model(seed)
        q = self.pin.neutral(self.model)
        for name, value in zip(self.names, model22):
            self.arm._set(q, name, value)
        self.pin.forwardKinematics(self.model, self.data, q)
        self.pin.updateFramePlacements(self.model, self.data)
        pose = self.data.oMf[self.frame]
        result = np.eye(4)
        result[:3, :3], result[:3, 3] = pose.rotation, pose.translation
        if not np.isfinite(result).all():
            raise ValueError('nonfinite_forward_pose')
        return result.tolist()

    def forward_pose(self, *, full22, held16):
        full = self.vector(full22, 22, 'full22')
        held = self.mapping.check_action(held16)
        return self._pose(full, held)

    def forward_path(self, *, full22, targets):
        full = self.vector(full22, 22, 'full22')
        if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= MAX_FORWARD_TARGETS:
            raise ValueError('forward_path_requires_1_to_100_targets')
        # Validate every target before changing mutable FK Data. No target is
        # silently dropped, clipped, or accepted past the batch bound.
        checked = [self.mapping.check_action(target) for target in targets]
        return [self._pose(full, held) for held in checked]

    def endpoint_ik(self, *, goals, full22, held16, posture_reference_deg=None, envelope_deg=None):
        """Endpoint-only IK for variant ordering; never plans or authorizes a path."""
        import time
        from .geometry import transform
        started = time.perf_counter()
        full = self.vector(full22, 22, 'full22')
        held = self.mapping.check_action(held16)
        if not isinstance(goals, (list, tuple)) or not 1 <= len(goals) <= 8:
            raise ValueError('endpoint_ik_requires_1_to_8_goals')
        signs = np.asarray(self.mapping.signs[7:14], float)
        limits = np.asarray(self.mapping.limits[7:14], float)
        eff_limits = limits.copy()
        if isinstance(envelope_deg, dict):
            for idx_key, bounds in envelope_deg.items():
                idx = int(idx_key) - 7
                if 0 <= idx < 7 and isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                    eff_limits[idx, 0] = max(eff_limits[idx, 0], float(bounds[0]))
                    eff_limits[idx, 1] = min(eff_limits[idx, 1], float(bounds[1]))
        mid = 0.5 * (eff_limits[:, 0] + eff_limits[:, 1])
        mid_rad = np.deg2rad(mid) * signs
        seeds = [np.asarray(held[7:14], float)]
        if posture_reference_deg is not None:
            ref = np.asarray(posture_reference_deg, float)
            if ref.shape == (7,) and np.isfinite(ref).all():
                seeds.append(np.clip(ref, limits[:, 0], limits[:, 1]))
        seeds.append(mid.copy())
        seed_base = full.copy()
        seed_base[7:14] = held[7:14]
        model22 = self.mapping.to_model(seed_base)
        base_q = self.pin.neutral(self.model)
        for name, value in zip(self.names, model22):
            self.arm._set(base_q, name, value)
        results = []
        eye6 = np.eye(6)
        eye7 = np.eye(7)
        for raw_goal in goals:
            g_start = time.perf_counter()
            goal = transform(raw_goal)
            best = None
            best_err = (float('inf'), float('inf'))
            for seed in seeds:
                q_deg = seed.copy()
                converged = False
                pos_err_norm = float('inf')
                rot_err_norm = float('inf')
                for _ in range(60):
                    q_rad = np.deg2rad(q_deg) * signs
                    full_q = base_q.copy()
                    for idx_q, val in zip(self.arm_q_idx, q_rad):
                        full_q[idx_q] = val
                    self.pin.forwardKinematics(self.model, self.data, full_q)
                    self.pin.computeJointJacobians(self.model, self.data, full_q)
                    self.pin.updateFramePlacements(self.model, self.data)
                    pose = self.data.oMf[self.frame]
                    pos_err = goal[:3, 3] - pose.translation
                    rot_err = self.pin.log3(goal[:3, :3] @ pose.rotation.T)
                    pos_err_norm = float(np.linalg.norm(pos_err))
                    rot_err_norm = float(np.linalg.norm(rot_err))
                    if pos_err_norm <= 1e-3 and rot_err_norm <= 1e-2:
                        converged = True
                        break
                    jac = self.pin.getFrameJacobian(
                        self.model, self.data, self.frame,
                        self.pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
                    )[:, self.arm_v_idx]
                    jjt = jac @ jac.T + 1e-3 * eye6
                    err = np.r_[pos_err, rot_err]
                    dq_task = jac.T @ np.linalg.solve(jjt, err)
                    j_pinv = jac.T @ np.linalg.solve(jjt, eye6)
                    dq_null = (eye7 - j_pinv @ jac) @ (mid_rad - q_rad) * 0.1
                    dq_deg = np.rad2deg(dq_task + dq_null) * signs
                    step_max = float(np.max(np.abs(dq_deg)))
                    if step_max > 15.0:
                        dq_deg *= 15.0 / step_max
                    q_deg = np.clip(q_deg + dq_deg, limits[:, 0], limits[:, 1])
                    wrist = 1.025 * abs(q_deg[5]) + abs(q_deg[6])
                    if wrist > 110.5:
                        scale = 110.49 / wrist
                        q_deg[5] *= scale
                        q_deg[6] *= scale
                if (pos_err_norm, rot_err_norm) < best_err:
                    best_err = (pos_err_norm, rot_err_norm)
                if converged:
                    candidate = held.copy()
                    candidate[7:14] = q_deg
                    try:
                        self.mapping.check_action(candidate)
                    except Exception:
                        continue
                    joint_margins = np.minimum(q_deg - eff_limits[:, 0], eff_limits[:, 1] - q_deg)
                    wrist_margin = 110.5 - (1.025 * abs(float(q_deg[5])) + abs(float(q_deg[6])))
                    min_margin = float(min(float(np.min(joint_margins)), wrist_margin))
                    if best is None or min_margin > best['min_margin_deg']:
                        best = dict(
                            converged=True,
                            min_margin_deg=round(min_margin, 4),
                            final_arm_deg=[round(float(x), 4) for x in q_deg],
                            position_error_m=round(pos_err_norm, 6),
                            rotation_error_rad=round(rot_err_norm, 6),
                        )
            if best is None:
                best = dict(
                    converged=False,
                    min_margin_deg=None,
                    final_arm_deg=None,
                    position_error_m=round(best_err[0], 6) if np.isfinite(best_err[0]) else None,
                    rotation_error_rad=round(best_err[1], 6) if np.isfinite(best_err[1]) else None,
                )
            best['elapsed_ms'] = round((time.perf_counter() - g_start) * 1000.0, 3)
            results.append(best)
        return dict(results=results, elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3))


class NumericalDispatcher:
    """Independent IK/FK lanes; only FK allows a bounded short lock wait."""

    def __init__(self, spec, *, numerics_factory=None, arm_factory=None):
        if numerics_factory is None:
            from f1dagger.harness_compute import HarnessNumerics
            numerics_factory = HarnessNumerics
        if arm_factory is None:
            from f1dagger.teleop import F1ArmTarget
            arm_factory = F1ArmTarget
        self.numerics = numerics_factory(spec)
        mapping = deepcopy(self.numerics.mapping)
        arm = arm_factory(spec['urdf'], mapping, deepcopy(spec['ik']))
        if arm is self.numerics.arm or arm.full_model is self.numerics.arm.full_model:
            raise ValueError('independent_forward_model_required')
        self.forward = ForwardKinematics(arm, mapping)
        self.solve_lock, self.forward_lock = threading.Lock(), threading.Lock()

    def clear_run(self):
        """Called only by the pool after all requests finish and lease revocation."""
        with self.solve_lock, self.forward_lock:
            self.numerics.arm.release()
            self.numerics.last_solve_diagnostics = []
            protector = self.numerics.gate.protector
            protector._feedback_positions = {}
            protector._last_com_result = protector._last_collision_result = None
            self.forward.data = self.forward.model.createData()

    def dispatch(self, method, kwargs):
        if method == 'solve_path':
            lock, function, busy = self.solve_lock, self.numerics.solve_path, 'numerical_solver_busy'
        elif method in ('forward_pose', 'forward_path', 'endpoint_ik'):
            lock, function, busy = self.forward_lock, getattr(self.forward, method), 'numerical_forward_busy'
        else:
            raise ValueError('numerical_method_forbidden')
        # A short FK batch must not turn an otherwise valid camera sample into
        # a perception fault. This wait is outside the control loop and never
        # waits for IK; overload beyond the finite budget remains an error.
        wait_s = 0.5 if method == 'endpoint_ik' else FORWARD_LOCK_TIMEOUT_S
        acquired = (lock.acquire(blocking=False) if method == 'solve_path'
                    else lock.acquire(timeout=wait_s))
        if not acquired:
            raise RuntimeError(busy)
        try:
            return function(**kwargs)
        except Exception as exc:
            # This service computes candidates only; it has no publisher. A
            # native collision veto here rejects this route, allowing the
            # caller to try another registered route. Output-owner faults keep
            # their separate hard-stop semantics.
            from f1dagger.robot_io import CandidateSafetyRejected
            if method != 'solve_path' or not isinstance(exc, CandidateSafetyRejected):
                raise
            import json
            from .contracts import HarnessError
            from .owner_bridge import numerical_failure_detail
            diagnostic = numerical_failure_detail(HarnessError(
                'actual_IK_candidate_collision_rejected:' + json.dumps(dict(diagnostic=dict(
                    code='actual_IK_candidate_collision_rejected', metrics=exc.details)))))
            raise HarnessError('actual_IK_candidate_collision_rejected:' +
                json.dumps(dict(diagnostic=diagnostic), separators=(',', ':'))) from None
        finally:
            lock.release()
