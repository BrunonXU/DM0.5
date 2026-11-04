from types import SimpleNamespace
import numpy as np
import pytest
from f1dagger.rlt_tracking import endpoint_tracking


def output(remaining=0., mode='certified_target'):
    return SimpleNamespace(fault=None,planner=SimpleNamespace(deadband=.0001,
        velocity=np.zeros(14), acceleration=np.zeros(14),
        limit_diagnostics={'remaining_s':remaining,'mode':mode}))


@pytest.mark.parametrize('condition', ['lag','moving','braking','feedback','fault','missing','malformed'])
def test_one_publication_does_not_prove_endpoint_completion(condition):
    o=output();target=np.zeros(16);command=target.copy();measured=target.copy()
    if condition=='lag': o.planner.limit_diagnostics['remaining_s']=.64;command[7]=6.
    if condition=='moving': o.planner.velocity[7]=.01
    if condition=='braking': o.planner.limit_diagnostics['mode']='limit_braking'
    if condition=='feedback': measured[7]=6.
    if condition=='fault': o.fault='latched'
    if condition=='missing': del o.planner
    if condition=='malformed': o.planner.velocity = []
    assert not endpoint_tracking(o,target,command,measured,1.)['complete']


def test_stationary_published_endpoint_with_fresh_close_feedback_completes():
    target=np.arange(16,dtype=float);measured=target.copy();measured[7]+=.2
    evidence=endpoint_tracking(output(),target,target,measured,1.)
    assert evidence['complete'] and evidence['trajectory_drained']
