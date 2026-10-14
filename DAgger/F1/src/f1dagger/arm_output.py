"""F1 numerical units adapter; public targets and dynamics remain HAL degrees.

Ruckig's absolute numerical tolerances make the degree-scale braking state
captured on F1 fail in step 1. Solve the identical trajectory in radians while
retaining ApprovedArmOutput's watchdog, hold and fault handling unchanged.
"""
import numpy as np


class RadianArmTrajectory:
    """Wrap the selected infra planner without round-tripping committed state."""
    internal_units = 'radian'

    def __init__(self, degree_planner):
        self.size = degree_planner.size
        self.dt = degree_planner.dt
        self.max_velocity = degree_planner.max_velocity
        self.max_acceleration = degree_planner.max_acceleration
        self.max_jerk = degree_planner.max_jerk
        self.target_cutoff_hz = degree_planner.target_cutoff_hz
        self.deadband = degree_planner.deadband
        self._radians = type(degree_planner)(
            self.size, self.dt, float(np.deg2rad(self.max_velocity)),
            float(np.deg2rad(self.max_acceleration)), float(np.deg2rad(self.max_jerk)),
            self.target_cutoff_hz, float(np.deg2rad(self.deadband)))
        self._pending = None
        self.bounds = None
        self._certificate = None
        self._next_certificate = None
        self._enabled = np.ones(self.size, dtype=bool)
        self.limit_diagnostics = {'mode': 'unbounded_adapter'}
        self._position_degrees = np.rad2deg(self._radians.position).tolist()

    @property
    def position(self):
        return self._position_degrees.copy()

    @property
    def velocity(self):
        return np.rad2deg(self._radians.velocity).tolist()

    @property
    def acceleration(self):
        return np.rad2deg(self._radians.acceleration).tolist()

    @property
    def filtered_target(self):
        return np.rad2deg(self._radians.filtered_target).tolist()

    @property
    def recovery_events(self):
        return self._radians.recovery_events

    def set_limits(self, limits):
        bounds = np.asarray(limits, dtype=float)
        if self.size != 14 or bounds.shape != (14, 2) or not np.isfinite(bounds).all() or np.any(bounds[:,0] >= bounds[:,1]):
            raise ValueError('F1 output requires fourteen finite position bounds')
        self.bounds = np.deg2rad(bounds)

    def reset(self, position):
        self._pending = None
        self._certificate = self._next_certificate = None
        self._radians.reset(np.deg2rad(position))
        self._position_degrees = np.asarray(position, dtype=float).tolist()

    def propose(self, target, dt=None):
        self._pending = None
        radians = self._radians.propose(np.deg2rad(target), dt=dt)
        if self.bounds is not None:
            radians = self._certified_proposal(radians, self.dt if dt is None else float(dt))
        position = np.rad2deg(radians.position).tolist()
        # An unchanged native coordinate must preserve the exact last HAL
        # value, including inactive arms, rather than add conversion roundoff.
        for index, (before, after) in enumerate(zip(self._radians.position, radians.position)):
            if before == after:
                position[index] = self._position_degrees[index]
        degrees = type(radians)(tuple(position), *(tuple(np.rad2deg(getattr(radians, field)))
            for field in ('velocity', 'acceleration', 'filtered_target')))
        self._pending = (degrees, radians)
        return degrees

    def commit(self, proposal, accepted=None):
        if self._pending is None or proposal is not self._pending[0]:
            raise ValueError('F1 trajectory commit requires its current proposal')
        # Keep the exact native proposal: repeated degree/radian conversions
        # of the current velocity and acceleration would add numerical drift.
        self._radians.commit(self._pending[1], accepted=accepted)
        self._certificate = self._next_certificate
        self._next_certificate = None
        mask = [True] * self.size if accepted is None else list(accepted)
        for index, enabled in enumerate(mask):
            if enabled:
                self._position_degrees[index] = proposal.position[index]
        self._pending = None

    def hold(self):
        self._pending = None
        self._certificate = self._next_certificate = None
        self._radians.hold()

    def hold_axes(self, enabled):
        self._pending = None
        self._enabled = np.asarray(enabled, dtype=bool)
        self._radians.hold_axes(enabled)

    def _trajectory_safe(self, trajectory):
        extrema = trajectory.position_extrema
        low = np.array([e.min for e in extrema])
        high = np.array([e.max for e in extrema])
        if not np.isfinite(np.r_[low, high]).all():
            raise RuntimeError('non-finite Ruckig position extrema')
        if np.any(low < self.bounds[:,0]) or np.any(high > self.bounds[:,1]):
            return False
        # This rectangular enclosure is conservative but valid for every
        # continuous-time point, including between publication instants.
        magnitude = np.maximum(np.abs(low), np.abs(high))
        from .trajectory_bounds import wrist_safe
        bound = float(np.deg2rad(110.5))
        for offset in (0,7):
            if 1.025*magnitude[offset+5]+magnitude[offset+6] > bound:
                if not wrist_safe(trajectory, offset, bound):
                    return False
        return True

    def _certified_proposal(self, proposal, dt):
        import ruckig
        native = self._radians
        inp = ruckig.InputParameter(self.size)
        for field in ('position', 'velocity', 'acceleration'):
            setattr(inp, 'current_'+field, getattr(native, field))
        inp.target_position = list(proposal.filtered_target)
        inp.target_velocity = inp.target_acceleration = [0.]*self.size
        for field in ('max_velocity', 'max_acceleration', 'max_jerk'):
            setattr(inp, field, [getattr(native, field)]*self.size)
        trajectory = ruckig.Trajectory(self.size)
        try:
            result = ruckig.Ruckig(self.size).calculate(inp, trajectory)
            if int(result) < 0:
                raise RuntimeError('Ruckig trajectory result='+str(result))
        except Exception:
            inp.synchronization = ruckig.Synchronization.No
            result = ruckig.Ruckig(self.size).calculate(inp, trajectory)
            if int(result) < 0:
                raise RuntimeError('Ruckig independent trajectory result='+str(result))
        safe = self._trajectory_safe(trajectory)
        synchronization = 'time_or_independent'
        if not safe:
            # Coupled wrist tangent motion can be legal as a straight path but
            # leave the polytope under time-only synchronization. Phase sync
            # preserves compatible axis ratios; still certify its full curve.
            inp.synchronization = ruckig.Synchronization.Phase
            phase = ruckig.Trajectory(self.size)
            result = ruckig.Ruckig(self.size).calculate(inp, phase)
            if int(result) < 0:
                raise RuntimeError('Ruckig phase trajectory result='+str(result))
            if self._trajectory_safe(phase):
                trajectory, safe, synchronization = phase, True, 'phase'
        if safe:
            elapsed = dt
            self.limit_diagnostics = dict(mode='certified_target',
                duration_s=trajectory.duration, remaining_s=max(0., trajectory.duration-dt),
                synchronization=synchronization)
            filtered = proposal.filtered_target
        elif self._certificate is not None:
            # Continue the already certified path to its zero-v/a endpoint.
            # Unlike HOLD this does not discard momentum or break jerk limits.
            trajectory, elapsed = self._certificate
            elapsed += dt
            filtered = tuple(trajectory.at_time(trajectory.duration)[0])
            self.limit_diagnostics = dict(mode='limit_braking',
                remaining_s=max(0., trajectory.duration-elapsed))
        else:
            # Initialization at rest has a trivial safe stopping trajectory.
            if np.max(np.abs(native.velocity)) > 1e-12 or np.max(np.abs(native.acceleration)) > 1e-12:
                raise RuntimeError('no certified F1 stopping trajectory for moving state')
            self.limit_diagnostics = dict(mode='limit_at_rest', remaining_s=0.)
            self._next_certificate = None
            return type(proposal)(tuple(native.position), tuple(native.velocity),
                                  tuple(native.acceleration), tuple(native.position))
        q, v, a = trajectory.at_time(min(elapsed, trajectory.duration))
        if elapsed >= trajectory.duration:
            # Every certified path above ends at the requested zero velocity
            # and acceleration. Ruckig's polynomial evaluation can leave
            # ~1e-17 residuals at that endpoint. Carrying those into the next
            # solve can prevent Phase synchronization along a wrist boundary.
            # Use the exact terminal conditions only AFTER the full certified
            # path has elapsed; never zero a moving state or move its position.
            v, a = [0.]*self.size, [0.]*self.size
        # An explicitly unclutched arm retains the existing immediate HOLD
        # semantics, while the other arm can continue its certified path.
        for i in np.flatnonzero(~self._enabled):
            q[i], v[i], a[i] = native.position[i], 0., 0.
        self._next_certificate = (trajectory, elapsed)
        return type(proposal)(tuple(q), tuple(v), tuple(a), tuple(filtered))
