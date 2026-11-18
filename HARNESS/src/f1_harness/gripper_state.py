"""Measured opening facts are distinct from arriving at a commanded aperture."""
import math
from numbers import Real


def observed_open(value, opening_range):
    """F1 SDK closure grows toward closed; values below the release range are open.

    The original range still describes arrival at a release command. Its upper
    bound also limits the open-hand observation hypothesis. Feedback validity
    and time remain the caller's responsibility.
    """
    if (isinstance(value,bool) or not isinstance(value,Real) or not math.isfinite(value)
            or not isinstance(opening_range,(tuple,list)) or len(opening_range)!=2
            or any(isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(v) for v in opening_range)
            or not 0<=opening_range[0]<opening_range[1]):
        return False
    return 0<=value<=opening_range[1]
