"""F1 arm mathematics only; no ROS, publishers, network or implicit motion.

Public joints are degrees, poses are metres in the native per-arm SDK frame.
The optimizer uses radians. A reference chooses a local redundant solution;
EEF alone cannot encode the seventh joint degree of freedom.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

import numpy as np
from scipy.optimize import minimize
from scipy.spatial.transform import Rotation


LOW_DEG = np.array([-178., -94., -178., -145., -178., -60., -90.])
HIGH_DEG = np.array([178., 120., 178., 60., 178., 60., 90.])
CONFIG_SHA256 = "ce20b1a80974c3ed58cdac4973152625096c8ad93a779e5fd4108c26ce19bd52"


def skew(v):
    x, y, z = v
    return np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])


def checked_pose(pose):
    p = np.asarray(pose, dtype=np.float64).copy()
    if p.shape != (4, 4) or not np.isfinite(p).all():
        raise ValueError("Expected finite 4x4 pose in metres")
    r = p[:3, :3]
    if (not np.allclose(p[3], [0, 0, 0, 1], atol=1e-10, rtol=0)
            or np.linalg.norm(r.T @ r - np.eye(3)) > 1e-6
            or abs(np.linalg.det(r) - 1) > 1e-6):
        raise ValueError("Pose is not a proper rigid transform")
    # Native SDK rotations carry ~1e-10 roundoff. Only remove that roundoff.
    p[:3, :3] = Rotation.from_matrix(r).as_matrix()
    return p


def pose_error(target, actual):
    return (float(np.linalg.norm(target[:3, 3] - actual[:3, 3]) * 1000),
            float(np.rad2deg(Rotation.from_matrix(
                target[:3, :3].T @ actual[:3, :3]).magnitude())))


class ArmModel:
    """Modified DH model from the pinned F1 MvKDCfg, including row-8 flange."""

    def __init__(self, config):
        self.config = Path(config)
        self.config_sha256 = hashlib.sha256(self.config.read_bytes()).hexdigest()
        if self.config_sha256 != CONFIG_SHA256:
            raise ValueError("Unverified F1 kinematics config; validate before changing the pin")
        rows = [[float(x) for x in line.split(',') if x.strip()]
                for line in self.config.read_text().splitlines() if line.strip()]
        if len(rows) != 26 or any(int(rows[i][0]) != 1017 for i in (0, 13)):
            raise ValueError("Unsupported F1 configuration layout/type")
        self.dh = np.array([rows[1:9], rows[14:22]], dtype=object)
        self.dh = np.array([[r[:4] for r in arm] for arm in self.dh], float)
        self.dh[:, :, [0, 3]] = np.deg2rad(self.dh[:, :, [0, 3]])
        self.dh[:, :, 1:3] /= 1000
        expected = np.array([[0, -1.025, 110.5], [0, 1.025, 110.5],
                             [0, -1.025, -110.5], [0, 1.025, -110.5]])
        if not all(np.array_equal(np.array(rows[i:i+4]), expected) for i in (9, 22)):
            raise ValueError("Unverified wrist coupling")
        self.low, self.high = np.deg2rad(LOW_DEG), np.deg2rad(HIGH_DEG)
        self.wrist = np.zeros((4, 7))
        self.wrist[:, 5:] = [[1.025, 1], [1.025, -1], [-1.025, 1], [-1.025, -1]]
        self.wrist_max = np.deg2rad(110.5)

    @staticmethod
    def joint_vector(q):
        q = np.asarray(q, dtype=np.float64)
        if q.shape != (7,) or not np.isfinite(q).all():
            raise ValueError("Expected seven finite joint angles")
        return q

    def fk_rad(self, arm, q, *, jacobian=False):
        if arm not in (0, 1):
            raise ValueError("Arm must be 0 or 1")
        q = self.joint_vector(q)
        t = np.eye(4)
        axes, origins = [], []
        for i, (alpha, a, d, offset) in enumerate(self.dh[arm]):
            ca, sa = np.cos(alpha), np.sin(alpha)
            angle = offset + (q[i] if i < 7 else 0)
            c, s = np.cos(angle), np.sin(angle)
            if jacobian and i < 7:
                axes.append(t[:3, :3] @ np.array([0., -sa, ca]))
                origins.append(t[:3, 3] + t[:3, :3] @ np.array([a, 0., 0.]))
            t = t @ np.array([[c, -s, 0., a], [ca*s, ca*c, -sa, -d*sa],
                              [sa*s, sa*c, ca, d*ca], [0., 0., 0., 1.]])
        if not jacobian:
            return t
        axes, origins = np.array(axes), np.array(origins)
        j = np.vstack([np.cross(axes, t[:3, 3] - origins).T, axes.T])
        return t, j

    def fk(self, arm, joints_deg):
        return self.fk_rad(arm, np.deg2rad(self.joint_vector(joints_deg)))

    def admissible_rad(self, q, tol=1e-9):
        return bool(np.isfinite(q).all() and np.all(q >= self.low-tol)
                    and np.all(q <= self.high+tol)
                    and np.all(self.wrist @ q <= self.wrist_max+tol))

    def residual(self, arm, q, target):
        t, j = self.fk_rad(arm, q, jacobian=True)
        phi = Rotation.from_matrix(target[:3, :3].T @ t[:3, :3]).as_rotvec()
        theta = np.linalg.norm(phi)
        k = skew(phi)
        coef = (1/12 + theta*theta/720 if theta < 1e-4 else
                (1 - .5*theta / np.tan(.5*theta)) / theta**2)
        inv_left = np.eye(3) - .5*k + coef*(k@k)
        residual = np.r_[t[:3, 3]-target[:3, 3], phi]
        jac = np.vstack([j[:3], inv_left @ target[:3, :3].T @ j[3:]])
        return residual, jac


@dataclass(frozen=True)
class IKOptions:
    position_tolerance_mm: float = 0.01
    rotation_tolerance_deg: float = 0.001
    max_iterations: int = 80
    fallback_max_iterations: int = 150
    fallback_reference_weight: float = 1e-10

    def __post_init__(self):
        if (not np.isfinite([self.position_tolerance_mm, self.rotation_tolerance_deg]).all()
                or min(self.position_tolerance_mm, self.rotation_tolerance_deg) <= 0
                or self.max_iterations < 1 or self.fallback_max_iterations < 1
                or not np.isfinite(self.fallback_reference_weight)
                or self.fallback_reference_weight <= 0):
            raise ValueError("Invalid IK tolerances/iteration budget")


class ContinuousIK:
    """Deterministic local closest-reference IK, with independently gated results.

    No internal chain state: the caller supplies measured q at each new chunk,
    then the previous accepted result. Failure exposes no command joints.
    No velocity/collision certification is implied by numerical acceptance.
    """

    def __init__(self, model, options=None):
        self.model = model
        self.options = options or IKOptions()

    def solve(self, arm, target_m, reference_deg):
        start = time.perf_counter()
        target = checked_pose(target_m)
        reference = np.deg2rad(self.model.joint_vector(reference_deg))
        # Projection is an optimizer initial guess only, never an output repair.
        seed = np.clip(reference, self.model.low, self.model.high)
        cache_q, cache_result = None, None

        def evaluate(q):
            nonlocal cache_q, cache_result
            if cache_q is None or not np.array_equal(q, cache_q):
                cache_q = q.copy()
                cache_result = self.model.residual(arm, q, target)
            return cache_result

        constraints = [
            {"type": "eq", "fun": lambda q: evaluate(q)[0],
             "jac": lambda q: evaluate(q)[1]},
            {"type": "ineq", "fun": lambda q: self.model.wrist_max-self.model.wrist@q,
             "jac": lambda q: -self.model.wrist},
        ]
        # On the elbow boundary, float32 EEF quantization can put the target
        # nanometres outside the exact equality surface. Use residual restoration
        # directly there, instead of spending 80 iterations proving infeasibility.
        # This only selects an algorithm; limits and acceptance stay unchanged.
        elbow_boundary = min(abs(seed[3]-self.model.low[3]),
                             abs(seed[3]-self.model.high[3])) <= 1e-8
        result = None
        if not elbow_boundary:
            result = minimize(lambda q: .5*np.dot(q-reference, q-reference), seed,
                              jac=lambda q: q-reference, method="SLSQP",
                              bounds=list(zip(self.model.low, self.model.high)),
                              constraints=constraints,
                              options={"maxiter": self.options.max_iterations, "ftol": 1e-11})
        primary_status = int(result.status) if result is not None else None
        primary_iterations = int(result.nit) if result is not None else 0
        fallback_used = result is None or not result.success
        if fallback_used:
            # At an active elbow bound, exact equalities can have inconsistent
            # linearizations. Restore feasibility using the SAME hard bounds.
            # This regularized residual objective is not a global nearest-q proof.
            weight = self.options.fallback_reference_weight

            def residual_objective(q):
                e, j = evaluate(q)
                d = q-reference
                return .5*(e@e + weight*(d@d)), e@j + weight*d

            # Restart at the reference, not the failed equality iterate: that
            # iterate may already have travelled far along the redundant DOF.
            result = minimize(residual_objective, seed, jac=True, method="SLSQP",
                              bounds=list(zip(self.model.low, self.model.high)),
                              constraints=[constraints[1]],
                              options={"maxiter": self.options.fallback_max_iterations, "ftol": 1e-20})
        q = result.x
        position_mm, rotation_deg = pose_error(target, self.model.fk_rad(arm, q))
        bounded = self.model.admissible_rad(q)
        accepted = bool(bounded and position_mm <= self.options.position_tolerance_mm
                        and rotation_deg <= self.options.rotation_tolerance_deg)
        return {"success": accepted,
                "joints": np.rad2deg(q).tolist() if accepted else None,
                "candidate_joints": np.rad2deg(q).tolist(),
                "position_error_mm": position_mm, "rotation_error_deg": rotation_deg,
                "within_limits": bounded,
                "reference_step_max_deg": float(np.max(np.abs(np.rad2deg(q-reference)))),
                "optimizer_success": bool(result.success), "optimizer_status": int(result.status),
                "optimizer_message": str(result.message), "iterations": int(result.nit),
                "fallback_used": fallback_used, "primary_status": primary_status,
                "primary_iterations": primary_iterations,
                "primary_skipped_elbow_boundary": bool(elbow_boundary),
                "latency_ms": (time.perf_counter()-start)*1000}

    def solve_eef_prefix(self, actions23, anchor_joints14, reference_joints14, *, execute_steps=20):
        """Decode physical (unnormalized) action23 using one observation anchor.

        Each arm is xyz3 + first-two-rotation-rows6; then grippers2 and base3.
        Reference may be fresher than the request anchor, but the anchor is fixed.
        A rejected prefix has no joint trajectory. This function sends nothing.
        """
        actions = np.asarray(actions23, dtype=float)
        anchor, reference = np.asarray(anchor_joints14, float), np.asarray(reference_joints14, float)
        if (actions.ndim != 2 or actions.shape[1] != 23 or not np.isfinite(actions).all()
                or anchor.shape != (14,) or reference.shape != (14,)
                or not np.isfinite(anchor).all() or not np.isfinite(reference).all()
                or not isinstance(execute_steps, int) or not 1 <= execute_steps <= len(actions)):
            raise ValueError("Invalid physical action23 prefix or joint14 reference")
        anchors = [self.model.fk(a, anchor[a*7:a*7+7]) for a in (0, 1)]
        refs = [reference[a*7:a*7+7].copy() for a in (0, 1)]
        trajectory, diagnostics = [], []
        for step, action in enumerate(actions[:execute_steps]):
            pair = []
            for arm in (0, 1):
                block = action[arm*9:arm*9+9]
                r1, r2 = block[3:6].copy(), block[6:9].copy()
                if np.linalg.norm(r1) < 1e-8:
                    return {"success": False, "joint_targets_deg": None,
                            "reason": "degenerate_rotation6d", "step": step, "arm": arm}
                r1 /= np.linalg.norm(r1)
                r2 -= np.dot(r1, r2)*r1
                if np.linalg.norm(r2) < 1e-8:
                    return {"success": False, "joint_targets_deg": None,
                            "reason": "degenerate_rotation6d", "step": step, "arm": arm}
                r2 /= np.linalg.norm(r2)
                delta = np.eye(4)
                delta[:3, :3] = np.stack([r1, r2, np.cross(r1, r2)])
                delta[:3, 3] = block[:3]
                result = self.solve(arm, anchors[arm] @ delta, refs[arm])
                diagnostics.append({"step": step, "arm": arm, **result})
                if not result['success']:
                    return {"success": False, "joint_targets_deg": None,
                            "reason": "ik_rejected", "step": step, "arm": arm,
                            "diagnostics": diagnostics}
                refs[arm] = np.array(result['joints'])
                pair.extend(result['joints'])
            trajectory.append(pair)
        return {"success": True, "joint_targets_deg": trajectory, "diagnostics": diagnostics,
                "gripper_targets": actions[:execute_steps, 18:20].tolist(),
                "base_velocity_targets": actions[:execute_steps, 20:23].tolist()}
