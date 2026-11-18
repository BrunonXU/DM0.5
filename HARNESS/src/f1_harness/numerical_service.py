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
        elif method in ('forward_pose', 'forward_path'):
            lock, function, busy = self.forward_lock, getattr(self.forward, method), 'numerical_forward_busy'
        else:
            raise ValueError('numerical_method_forbidden')
        # A short FK batch must not turn an otherwise valid camera sample into
        # a perception fault. This wait is outside the control loop and never
        # waits for IK; overload beyond the finite budget remains an error.
        acquired = (lock.acquire(blocking=False) if method == 'solve_path'
                    else lock.acquire(timeout=FORWARD_LOCK_TIMEOUT_S))
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
