"""Allocation-reduced native mathematics; the continuous IK solver is unchanged.

Opt-in candidate: preserve scalar trig and the original matrix multiplication
order. No fast-math, new robot geometry, solver parameters or output clipping.
"""
import numpy as np
from scipy.spatial.transform import Rotation

from .kinematics import ArmModel


class FastArmModel(ArmModel):
    def __init__(self, config):
        super().__init__(config)
        self._constants = []
        for rows in self.dh:
            constants = []
            for alpha, a, d, offset in rows:
                ca, sa = np.cos(alpha), np.sin(alpha)
                constants.append((ca, sa, a, -d*sa, d*ca, offset,
                                  np.array([0., -sa, ca]),
                                  np.array([a, 0., 0.])))
            self._constants.append(constants)

    def fk_rad(self, arm, q, *, jacobian=False):
        if arm not in (0, 1):
            raise ValueError("Arm must be 0 or 1")
        q = self.joint_vector(q)
        t = np.eye(4)
        transform = np.empty((4, 4))
        transform[3] = [0., 0., 0., 1.]
        transform[0, 2] = 0.
        if jacobian:
            axes, origins = np.empty((7, 3)), np.empty((7, 3))
        for i, (ca, sa, a, ds, dc, offset, axis, origin) in enumerate(self._constants[arm]):
            angle = offset + (q[i] if i < 7 else 0)
            c, s = np.cos(angle), np.sin(angle)
            if jacobian and i < 7:
                axes[i] = t[:3, :3] @ axis
                origins[i] = t[:3, 3] + t[:3, :3] @ origin
            transform[0, 0], transform[0, 1], transform[0, 3] = c, -s, a
            transform[1] = [ca*s, ca*c, -sa, ds]
            transform[2] = [sa*s, sa*c, ca, dc]
            t = t @ transform
        if not jacobian:
            return t
        distance = t[:3, 3] - origins
        j = np.empty((6, 7))
        # Exactly the same multiply/subtract ordering as NumPy cross(axis, d).
        j[0] = axes[:, 1]*distance[:, 2] - axes[:, 2]*distance[:, 1]
        j[1] = axes[:, 2]*distance[:, 0] - axes[:, 0]*distance[:, 2]
        j[2] = axes[:, 0]*distance[:, 1] - axes[:, 1]*distance[:, 0]
        j[3:] = axes.T
        return t, j

    def residual(self, arm, q, target):
        t, j = self.fk_rad(arm, q, jacobian=True)
        phi = Rotation.from_matrix(target[:3, :3].T @ t[:3, :3]).as_rotvec()
        theta = np.linalg.norm(phi)
        x, y, z = phi
        k = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
        coef = (1/12 + theta*theta/720 if theta < 1e-4 else
                (1 - .5*theta / np.tan(.5*theta)) / theta**2)
        inv_left = np.eye(3) - .5*k + coef*(k@k)
        residual = np.empty(6)
        residual[:3] = t[:3, 3] - target[:3, 3]
        residual[3:] = phi
        jac = np.empty((6, 7))
        jac[:3] = j[:3]
        jac[3:] = inv_left @ target[:3, :3].T @ j[3:]
        return residual, jac
