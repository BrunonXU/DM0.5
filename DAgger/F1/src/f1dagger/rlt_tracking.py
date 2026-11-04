"""Read-only completion evidence for a normally finishing RLT trajectory.

This does not authorize a target, publish, clear a fault, or wait in the owner.
Joint feedback tolerance is the existing robot profile's arrival tolerance.
"""
import math
import numpy as np


def endpoint_tracking(output, target, command, measured, joint_tolerance_deg):
    planner = getattr(output, 'planner', None)
    diagnostics = getattr(planner, 'limit_diagnostics', {})
    remaining = diagnostics.get('remaining_s')
    if planner is None:
        return dict(complete=False, reason='missing_planner_evidence')
    arrays = [np.asarray(x, dtype=float)[7:14] for x in (target, command, measured)]
    if any(a.shape != (7,) or not np.isfinite(a).all() for a in arrays):
        return dict(complete=False, reason='invalid_endpoint_evidence')
    tolerance = float(joint_tolerance_deg)
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('Existing positive feedback arrival tolerance required')
    target, command, measured = arrays
    command_error = float(np.max(np.abs(command-target)))
    feedback_error = float(np.max(np.abs(measured-target)))
    velocity = np.asarray(planner.velocity, dtype=float)[7:14]
    acceleration = np.asarray(planner.acceleration, dtype=float)[7:14]
    stopped = (velocity.shape == (7,) and acceleration.shape == (7,)
               and np.isfinite(velocity).all() and np.isfinite(acceleration).all()
               and np.all(velocity == 0.) and np.all(acceleration == 0.))
    # This numerical tolerance is the planner's existing target deadband,
    # not an expanded mechanical arrival threshold.
    deadband = float(planner.deadband)
    drained = (diagnostics.get('mode') == 'certified_target' and remaining == 0.
               and stopped and math.isfinite(deadband) and 0 <= deadband
               and command_error <= deadband + 1e-9
               and getattr(output, 'fault', None) is None)
    return dict(complete=bool(drained and feedback_error <= tolerance),
                trajectory_drained=bool(drained), remaining_s=remaining,
                command_error_deg=command_error, feedback_error_deg=feedback_error,
                joint_tolerance_deg=tolerance)
