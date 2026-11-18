"""Fork the original output smoother for in-process offline candidate studies.

Call between completed samples, on the same thread as the source simulation.
This is not a live owner snapshot protocol, IPC serialization or authorization.
All mutable Python/numpy state is copied. A native braking trajectory is held
through a read-only view because Ruckig trajectories cannot be pickled; the F1
adapter creates a new trajectory per proposal and never rewrites a certificate.
"""
from copy import deepcopy
import math

import numpy as np


class _CertificateView:
    """Expose only the methods used to follow an existing braking certificate."""
    __slots__ = ('__trajectory',)

    def __init__(self, trajectory):
        object.__setattr__(self, '_CertificateView__trajectory', trajectory)

    def __setattr__(self, name, value):
        raise AttributeError('read_only_certificate')

    @property
    def duration(self):
        return self.__trajectory.duration

    def at_time(self, value):
        return deepcopy(self.__trajectory.at_time(value))

    def __deepcopy__(self, memo):
        return self  # immutable view; only new arrays returned by at_time


def fork_output(output):
    """Copy committed numerical state, preserving native radians and momentum.

    No reset, hold, unit conversion, source feedback or future output is used.
    Do not call concurrently with mutation of the source output. The returned
    object has no transport or device handle and cannot publish robot commands.
    """
    from f1dagger.arm_output import RadianArmTrajectory
    from pico_f1.arm_output import ApprovedArmOutput
    if type(output) is not ApprovedArmOutput or type(output.planner) is not RadianArmTrajectory:
        raise ValueError('unsupported_output_implementation')
    planner = output.planner
    if output.position is None or output.fault is not None or output.timed_out:
        raise ValueError('output_not_ready_for_branch')
    if planner._pending is not None or planner._next_certificate is not None:
        raise ValueError('output_proposal_not_committed')
    if (output.updated is None or output.sampled is None
            or not math.isfinite(output.updated) or not math.isfinite(output.sampled)
            or output.updated > output.sampled):
        raise ValueError('output_sample_not_completed')
    memo = {}
    certificate = planner._certificate
    if certificate is not None:
        trajectory, elapsed = certificate
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('invalid_certificate_elapsed')
        memo[id(trajectory)] = _CertificateView(trajectory)
    branch = deepcopy(output, memo)
    state = dict(schema='f1-offline-output-branch-v1', sample_time_s=output.sampled,
        target_time_s=output.updated, enabled=list(output.enabled),
        position_deg=np.asarray(output.position).tolist(), target_deg=np.asarray(output.target).tolist(),
        native_state={name: list(getattr(planner._radians, name))
            for name in ('position', 'velocity', 'acceleration', 'filtered_target')},
        certificate_present=certificate is not None,
        certificate_elapsed_s=None if certificate is None else certificate[1],
        certificate_duration_s=None if certificate is None else certificate[0].duration,
        immutable_native_certificate_view=certificate is not None,
        physical_motion=False, execution_authorized=False, serializable_controller=False)
    return branch, state
