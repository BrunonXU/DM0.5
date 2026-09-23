"""Conservatively remove uncertain intervals and crossing H50 windows, never labels."""
from pathlib import Path
import numpy as np


def isolate(parent, *, origin_ns, start_ns, stop_ns, horizon=50):
    if not isinstance(horizon,int) or horizon<=0 or start_ns>=stop_ns:
        raise ValueError('invalid interval or horizon')
    times=parent['time_ns']-int(origin_ns)
    if np.any(np.diff(times)<=0) or start_ns<times[0] or stop_ns>times[-1]:
        raise ValueError('interval outside ordered recording')
    bad=(times>=start_ns)&(times<stop_ns)
    if not bad.any():raise ValueError('interval contains no frames')
    out={k:v.copy() for k,v in parent.items()}
    out['action_candidate_valid'][bad,:]=False
    out['base_timing_reviewed'][bad]=False
    future=parent['future_signal_action_candidate_valid'] & out['action_candidate_valid'].all(axis=1)
    future &= out['state_valid'].all(axis=1) & out['base_timing_reviewed']
    out['future_signal_action_candidate_valid']=future
    out['joint_frame_candidate_valid']=parent['joint_frame_candidate_valid'] & future
    n=len(times);window=np.zeros(n,dtype=bool)
    if n>=horizon:
        prefix=np.r_[0,np.cumsum(~future,dtype=np.int64)]
        window[:n-horizon+1]=(prefix[horizon:]-prefix[:-horizon])==0
    out['chunk_start_candidate_valid']=parent['chunk_start_candidate_valid'] & window
    out['training_approved']=np.array(False)
    return out,bad
