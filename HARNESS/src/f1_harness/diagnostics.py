"""Passive joint tracking evidence; never estimates contact force or authorizes motion."""
import numpy as np


def tracking_diagnostic(command16,measured16,previous16,dt_s,*,error_threshold_deg,stationary_speed_deg_s):
    command,measured,previous=(np.asarray(v,float) for v in (command16,measured16,previous16))
    if any(v.shape!=(16,) or not np.isfinite(v).all() for v in (command,measured,previous)):
        raise ValueError('finite physical 16D samples required')
    if not 0<dt_s<=.5 or min(error_threshold_deg,stationary_speed_deg_s)<=0:
        raise ValueError('validated timing and diagnostic thresholds required')
    error=command[7:14]-measured[7:14]
    speed=(measured[7:14]-previous[7:14])/dt_s
    return dict(right_error_deg=error.tolist(),right_velocity_deg_s=speed.tolist(),
        tracking_stall_suspected=bool(np.max(np.abs(error))>error_threshold_deg and
                                      np.max(np.abs(speed))<stationary_speed_deg_s),
        contact_force_known=False,action_authorized=False)
