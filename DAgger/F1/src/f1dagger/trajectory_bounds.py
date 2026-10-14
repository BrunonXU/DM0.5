"""Continuous-time extrema for coupled F1 wrist planes on Ruckig profiles."""
import math
import numpy as np


def wrist_safe(trajectory, offset, bound):
    """Certify all four +/-1.025*q6 +/-q7 planes, including between samples.

    Merge jerk breakpoints of both axes. Each plane is cubic on each interval;
    its extrema are the endpoints and roots of its quadratic velocity.
    """
    if len(trajectory.profiles) != 1:
        raise ValueError('F1 output only supports single-section Ruckig trajectories')
    profiles = trajectory.profiles[0]
    knots = {0., float(trajectory.duration)}
    for index in (offset+5, offset+6):
        profile = profiles[index]
        if profile.accel.duration != 0.:
            raise ValueError('unexpected Ruckig acceleration post-profile')
        t = 0.
        for duration in list(profile.brake.t)+list(profile.t):
            t += duration
            if 0. < t < trajectory.duration:
                knots.add(t)
    ordered = sorted(knots)
    for start, end in zip(ordered, ordered[1:]):
        length = end-start
        if length <= 0.:
            continue
        q, v, a = trajectory.at_time(start)
        q_end, _, a_end = trajectory.at_time(end)
        for left, right in ((1.025,1.), (1.025,-1.), (-1.025,1.), (-1.025,-1.)):
            def plane(values):
                return left*values[offset+5]+right*values[offset+6]
            position, velocity, acceleration = plane(q), plane(v), plane(a)
            jerk = (plane(a_end)-acceleration)/length
            if max(position, plane(q_end)) > bound:
                return False
            roots = []
            if abs(jerk) < 1e-14:
                if abs(acceleration) > 1e-14:
                    roots = [-velocity/acceleration]
            else:
                discriminant = acceleration*acceleration-2.*jerk*velocity
                if discriminant >= 0.:
                    d = math.sqrt(discriminant)
                    roots = [(-acceleration-d)/jerk, (-acceleration+d)/jerk]
            for t in roots:
                if 0. < t < length:
                    peak = position+velocity*t+.5*acceleration*t*t+jerk*t*t*t/6.
                    if peak > bound:
                        return False
    return True
