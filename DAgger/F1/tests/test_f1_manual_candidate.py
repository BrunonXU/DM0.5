from types import SimpleNamespace
import numpy as np
import pytest

from f1dagger.manual_candidate import shorten_manual_candidate
from f1dagger.robot_io import CandidateSafetyRejected
from test_f1_preventive_boundary import system


def rejection(gap=-.001):
    return CandidateSafetyRejected('boundary', details=dict(
        reason='preventive_collision_boundary', clearance_m=gap,
        collision_pair=['arm', 'body']))


def test_shortening_checks_midpoint_preserves_gripper_and_inactive_arm():
    held=np.zeros(16);target=held.copy();target[7]=1.;target[15]=100.
    checked=[]
    def check(q, full):
        checked.append(q[7])
        if q[7] >= .4:raise rejection()
        return dict(clearance_m=.4-q[7],collision_pair=['arm','body'])
    selected,evidence=shorten_manual_candidate(SimpleNamespace(check_preventive=check),
        SimpleNamespace(check_action=lambda q:q),target,held,np.zeros(22),'right',rejection())
    assert checked==[.5,.25,.125] and evidence['scale']==.25
    assert selected[15]==100 and np.array_equal(selected[:7],held[:7])
    assert target[7]==1. and held[7]==0.


@pytest.mark.parametrize('error', [CandidateSafetyRejected('hard collision'),rejection(None),rejection(float('nan'))])
def test_unknown_and_hard_failures_cannot_be_shortened(error):
    with pytest.raises(CandidateSafetyRejected):
        shorten_manual_candidate(None,None,np.zeros(16),np.zeros(16),np.zeros(22),'right',error)


def test_unsafe_midpoints_reject_both_alternatives():
    target=np.zeros(16);target[7]=1.;seen=[]
    def check(q,full):
        seen.append(q[7])
        if q[7] in (.25,.125):raise rejection()
        return dict(clearance_m=1.)
    with pytest.raises(CandidateSafetyRejected):
        shorten_manual_candidate(SimpleNamespace(check_preventive=check),
            SimpleNamespace(check_action=lambda q:q),target,np.zeros(16),np.zeros(22),'right',rejection())
    assert seen==[.5,.25,.25]


def test_manual_lead_reduction_keeps_hil_and_records_original_vs_executed(system):
    m=system;m.send(start=True,hil_button_pressed=True)
    anchors=[];m.loop.arm.accept_safe_candidate=lambda full,q:anchors.append(q.copy())
    def check(q,full):
        if q[7]>.06:raise rejection()
        return dict(clearance_m=.06-q[7],collision_pair=['arm','body'])
    m.o.gate.check_preventive=check
    m.send(deadman=True,axes=(1.,0.,0.))
    assert m.e.input_pause is None
    assert m.o.last_submitted[7]==pytest.approx(.05)
    assert anchors[-1][7]==pytest.approx(.05)
    assert m.records[-1]['candidate'][7]==pytest.approx(.1)
    assert m.records[-1]['submitted'][7]==pytest.approx(.05)
    assert m.records[-1]['source']=='human_arm'
    # Reversal acts through normal gates, without an extra START or RB cycle.
    m.send(deadman=True,axes=(-1.,0.,0.))
    assert m.e.input_pause is None and m.o.last_submitted[7]<.05


def test_reduction_never_hides_unsafe_current_feedback(system):
    m=system;m.send(start=True,hil_button_pressed=True)
    m.loop.arm.accept_safe_candidate=lambda *args:pytest.fail('unsafe anchor accepted')
    m.o.gate.check_preventive=lambda *args:(_ for _ in ()).throw(rejection())
    with pytest.raises(CandidateSafetyRejected):
        m.send(deadman=True,axes=(1.,0.,0.))
    assert m.e.hard_hold_reason is not None
    assert m.o.last_submitted[7]==0.


def test_no_safe_shorter_step_retains_explicit_rb_recovery(system):
    m=system;m.send(start=True,hil_button_pressed=True)
    def check(q,full):
        if q[7]>0:raise rejection()
        return dict(clearance_m=.001,collision_pair=['arm','body'])
    m.o.gate.check_preventive=check
    m.send(deadman=True,axes=(1.,0.,0.))
    assert m.e.input_pause and m.o.last_submitted[7]==0.
    assert m.records[-1]['source']=='input_pause_hold'
