from types import SimpleNamespace
import numpy as np
import pytest
from f1dagger.rtc_prefix_recovery import PrefixStopRequest
from f1dagger.rtc_prefix_runtime import PrefixRecoveryControlLoop
from test_f1_rtc_prefix_recovery import fixture_engine


def setup_loop(powered=True):
    engine,held,_,_,now,journal=fixture_engine(enabled=False)
    native=SimpleNamespace(position=[0.]*14,velocity=[0.]*14,acceleration=[0.]*14)
    trajectory=SimpleNamespace(duration=.1,at_time=lambda t:([0.]*14,[0.]*14,[0.]*14))
    planner=SimpleNamespace(internal_units='radian',bounds=np.zeros((14,2)),deadband=.0002,
                            _radians=native,_certificate=(trajectory,.1))
    stamps=[now+20]*16
    owner=SimpleNamespace(powered=powered,output=SimpleNamespace(planner=planner),last_submitted=held.copy(),
        last_evidence=dict(publication_complete=True,execution_mode='powered' if powered else 'shadow',completed_monotonic_ns=now+10),
        source=SimpleNamespace(feedback=lambda:(np.zeros(22),held.copy(),stamps)))
    loop=PrefixRecoveryControlLoop(engine,owner,SimpleNamespace(),journal,
        dict(joint_tolerance_deg=1.,feedback_max_age_s=.25,settle_timeout_s=3.))
    req=PrefixStopRequest(engine.supervisor.policy_epoch,2,engine.prefix_owner_token,tuple(held),now)
    engine.prefix_recovery=dict(request=req)
    return loop,engine,owner,req,now,stamps


def test_actual_stationary_dispatch_waits_for_feedback_and_retains_first_publication():
    loop,engine,owner,req,now,stamps=setup_loop()
    try:
        loop.capture_stationary_publication()
        proof=loop.stop_probe(req,now+30)
        assert engine._stopped(proof,req,now+30)
        owner.last_evidence['completed_monotonic_ns']=now+31
        loop.capture_stationary_publication()
        assert loop.stop_probe(req,now+40).published_monotonic_ns==now+10
        stamps[7]=now+9
        assert not engine._stopped(loop.stop_probe(req,now+40),req,now+40)
    finally: engine.close()


@pytest.mark.parametrize('problem',['shadow','moving','accelerating','unfinished','uncertified','unpublished','wrong_target','gripper_changed'])
def test_no_stationary_evidence_from_invalid_output(problem):
    loop,engine,owner,req,now,stamps=setup_loop(powered=problem!='shadow')
    try:
        planner=owner.output.planner
        if problem=='moving': planner._radians.velocity[7]=.01
        if problem=='accelerating': planner._radians.acceleration[7]=.01
        if problem=='unfinished': planner._certificate=(planner._certificate[0],.05)
        if problem=='uncertified': planner._certificate=None
        if problem=='unpublished': owner.last_evidence['publication_complete']=False
        if problem=='wrong_target': owner.last_submitted[7]=1.
        if problem=='gripper_changed': owner.last_submitted[15]+=1.
        loop.capture_stationary_publication()
        assert loop.stop_probe(req,now+30) is None
    finally: engine.close()


def test_motion_after_stationary_dispatch_revokes_evidence():
    loop,engine,owner,req,now,stamps=setup_loop()
    try:
        loop.capture_stationary_publication()
        assert loop.stop_probe(req,now+30) is not None
        owner.output.planner._radians.velocity[7]=.01
        loop.capture_stationary_publication()
        assert loop.stop_probe(req,now+30) is None
    finally: engine.close()


def test_j6_tracking_plateau_is_diagnosed_without_accepting_stop():
    loop,engine,owner,req,now,stamps=setup_loop()
    try:
        loop.capture_stationary_publication()
        measured=np.asarray(req.target16).copy();measured[12]-=3.
        owner.source.feedback=lambda:(np.zeros(22),measured,stamps)
        report=loop.stop_diagnostics(req,now+30)
        assert report['blockers']==['measured_target_not_reached']
        assert report['joints_outside_arrival_tolerance']==[6]
        assert report['max_error_deg']==3. and report['feedback_fresh']
        assert not engine._stopped(loop.stop_probe(req,now+30),req,now+30)
        assert engine.prefix_stop_tolerance_deg==1.
    finally:engine.close()


@pytest.mark.parametrize('fault',['moving','stale','future','nan','shadow'])
def test_stop_diagnostics_distinguishes_other_blockers(fault):
    loop,engine,owner,req,now,stamps=setup_loop(powered=fault!='shadow')
    try:
        if fault=='moving':owner.output.planner._radians.velocity[7]=.01
        loop.capture_stationary_publication()
        if fault=='stale':stamps[7]=now-300_000_000
        if fault=='future':stamps[7]=now+100
        if fault=='nan':
            bad=np.asarray(req.target16).copy();bad[12]=np.nan
            owner.source.feedback=lambda:(np.zeros(22),bad,stamps)
        report=loop.stop_diagnostics(req,now+30)
        import json
        json.dumps(report,allow_nan=False)
        expected={'moving':'planner_motion_or_terminal_publication_mismatch',
            'stale':'feedback_stale_or_future','future':'feedback_stale_or_future',
            'nan':'invalid_feedback','shadow':'shadow_no_physical_evidence'}[fault]
        assert expected in report['blockers']
    finally:engine.close()
