"""Bounded manual IK lead reduction; no output, threshold changes or detours."""
import numpy as np

from .robot_io import CandidateSafetyRejected


def shorten_manual_candidate(gate, mapping, candidate, held, full, side, error):
    """Try smaller increments along the operator's rejected joint direction.

    Caller must first verify fresh feedback and BOTH held/physical poses. Only
    a finite proxy-boundary veto qualifies; hard collision/CoM, unknown distance
    and arm-limit failures retain the existing pause. At most two alternatives
    are screened, including their midpoints. The normal owner approval and
    actual-output guard still run afterward; this is not a continuous-path proof.
    """
    details = getattr(error, 'details', {})
    gap = details.get('clearance_m')
    if (details.get('reason') != 'preventive_collision_boundary'
            or gap is None or not np.isfinite(gap) or gap > 0
            or not details.get('collision_pair') or side not in ('left', 'right')):
        raise error
    start = 0 if side == 'left' else 7
    active = slice(start, start+7)
    inactive = slice(7, 14) if start == 0 else slice(0, 7)
    if not np.array_equal(candidate[inactive], held[inactive]):
        raise error
    for scale in (.5, .25):
        target = candidate.copy()
        target[active] = held[active] + scale*(candidate[active]-held[active])
        mapping.check_action(target)
        midpoint = target.copy()
        midpoint[active] = (held[active]+target[active])*.5
        try:
            evidence = gate.check_preventive(target, full)
            gate.check_preventive(midpoint, full)
        except CandidateSafetyRejected:
            continue
        return target, dict(scale=scale, clearance_m=evidence['clearance_m'],
                            collision_pair=evidence.get('collision_pair'),
                            rejected_clearance_m=gap)
    raise error
