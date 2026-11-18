"""No-op recognition plus owner lifecycle; native full replay covers geometry."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest
from f1_harness.contracts import SkillResult
from f1_harness.owner_bridge import JointProposal
from f1_harness.stationary_alignment import constant_alignment,check_alignment
from f1_harness.trajectory import schedule
from test_unpublished_plan import make_bridge


def plan_for(intent,anchor):
    targets=(tuple(anchor),tuple(anchor))
    timing=schedule(targets,anchor,[dict(end_index=2)],20.)
    return JointProposal(intent.request_id,intent.epoch,intent.scene_version,intent.calibration_id,
        (0.,)*22,targets,None,300_000_000,.5,trajectory=timing)


@pytest.mark.parametrize('change',['arm','gripper','left','held','skill','legacy','nonfinite'])
def test_only_exact_constant_alignment_qualifies(change):
    intent=SimpleNamespace(skill='align',request_id='r',epoch=1,scene_version='s',calibration_id='c')
    held=[0.]*16;plan=plan_for(intent,held)
    assert constant_alignment(intent,plan,held)
    if change in ('arm','gripper','left','nonfinite'):
        targets=np.array(plan.targets);index={'arm':7,'gripper':15,'left':0,'nonfinite':7}[change]
        targets[1,index]=float('nan') if change=='nonfinite' else 1e-12
        plan=replace(plan,targets=tuple(map(tuple,targets)))
    elif change=='held':held[7]=1e-12
    elif change=='skill':intent.skill='insert'
    else:plan=replace(plan,trajectory=None)
    assert not constant_alignment(intent,plan,held)


def setup(tmp_path,monkeypatch,proof=True):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    e.running=e.pending.model_copy(update=dict(skill='align'));e.pending=None
    plan=plan_for(e.running,state['held16'])
    b.proposal=plan;b.plan_observation=obs
    b.plan_full_reference=tuple(state['full22']);b.plan_held_reference=tuple(state['held16'])
    b.revalidation_proof={'unit_fixture':True}
    # Isolate scheduling from the independently tested geometric revalidator.
    monkeypatch.setattr(b,'validate_first_publication',lambda **kwargs:True)
    b.waypoint_verifier=lambda *a:dict(accepted=proof)
    b.verifier=lambda intent,after:SkillResult(request_id=intent.request_id,lifecycle='completed',
        outcome='success',reason='owner_arrival_and_fresh_scene',evidence_ids=('new-scene',))
    for _ in range(17):
        b.update_feedback(**{k:state[k] for k in ('full22','measured16')},stamps16=(clock[0],)*16)
        clock[0]+=20_000_000
    def finish():
        future,fn,args=b.executor.jobs[-1];future.set_result(fn(*args));clock[0]+=20_000_000
        return poll()
    return e,b,clock,state,poll,finish


def test_constant_alignment_reuses_stationary_window_but_requires_new_geometry_and_scene(tmp_path,monkeypatch):
    e,b,clock,state,poll,finish=setup(tmp_path,monkeypatch)
    try:
        insert=e.running.model_copy(update=dict(request_id='insert-next',skill='insert'))
        assert poll() is None and b.future_kind=='stationary_align'
        assert e.results['one-request'].lifecycle=='accepted' and not e.request_publications
        assert finish() is None and b.future_kind=='verify'
        assert e.results['one-request'].lifecycle=='accepted' and not e.request_publications
        assert finish() is None
        result=e.results['one-request']
        assert result.outcome=='success' and result.no_motion
        assert result.reason=='stationary_alignment_and_fresh_scene'
        assert not e.request_publications
        assert e.observation_after_ns==b.verification_after_ns
        original=e.observation
        with pytest.raises(RuntimeError,match='observation_before_motion_wait_recovery'):
            e.prepare_skill(insert.model_dump())
        frames=tuple(f.model_copy(update={'received_ns':clock[0],'source_ns':clock[0],
                                          'sequence':f.sequence+1}) for f in original.frames)
        fresh=original.model_copy(update=dict(observation_id='post-align',received_ns=clock[0],
                                              state_received_ns=clock[0],frames=frames))
        e.update_observation(fresh)
        assert e.prepare_skill(insert.model_copy(update=dict(observation_id='post-align')).model_dump())['ready']
    finally:b.close()


@pytest.mark.parametrize('failure',['geometry','moving','gripper','held','preflight','exception'])
def test_failed_optional_alignment_check_falls_back_without_false_success(tmp_path,monkeypatch,failure):
    e,b,clock,state,poll,finish=setup(tmp_path,monkeypatch,proof=failure!='geometry')
    try:
        if failure=='exception':
            def bad(*args):raise ValueError('no proof')
            b.waypoint_verifier=bad
        assert poll() is None
        if failure=='moving':state['measured16'][7]+=2*b.stop_delta_deg
        if failure=='gripper':state['measured16'][15]+=2*b.stop_gripper_delta
        if failure=='held':state['held16'][7]+=1e-12
        if failure=='preflight':monkeypatch.setattr(b,'validate_first_publication',lambda **kw:False)
        assert finish() is None
        assert e.results['one-request'].lifecycle=='accepted' and b.stationary_alignment_request is None
        assert (poll() is None) if failure=='preflight' else poll() is not None
        assert len(b.executor.jobs)==1
    finally:b.close()


@pytest.mark.parametrize('failure',['duplicate_feedback','moving_window','published','no_revalidation'])
def test_no_stationary_shortcut_without_prerequisites(tmp_path,monkeypatch,failure):
    e,b,clock,state,poll,finish=setup(tmp_path,monkeypatch)
    try:
        if failure=='duplicate_feedback':
            b.feedback_history.clear()
            for _ in range(20):b.update_feedback(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
        if failure=='moving_window':state['measured16'][7]+=2*b.stop_delta_deg
        if failure=='published':e.request_publications['one-request']={'publication_count':1}
        if failure=='no_revalidation':b.revalidation_proof=None
        assert poll() is not None and not b.executor.jobs
        assert e.results['one-request'].lifecycle=='accepted'
    finally:b.close()


def test_cancelled_alignment_proof_cannot_complete_or_publish(tmp_path,monkeypatch):
    e,b,clock,state,poll,finish=setup(tmp_path,monkeypatch)
    try:
        assert poll() is None
        future,fn,args=b.executor.jobs[-1];future.set_running_or_notify_cancel()
        b.revoke('user_stop');future.set_result(fn(*args))
        assert poll() is None and not e.authorized
        assert b.stationary_alignment_attempt is None and b.stationary_alignment_request is None
        assert 'one-request' not in e.request_publications
        assert e.results['one-request'].outcome!='success'
    finally:b.close()


def test_missing_gripper_feedback_during_proof_revokes_without_completion(tmp_path,monkeypatch):
    e,b,clock,state,poll,finish=setup(tmp_path,monkeypatch)
    try:
        assert poll() is None
        future,fn,args=b.executor.jobs[-1];future.set_result(fn(*args))
        clock[0]+=20_000_000;stamps=[clock[0]]*16;stamps[15]=None
        assert b.poll(**state,stamps16=tuple(stamps)) is None
        assert not e.authorized and e.results['one-request'].outcome!='success'
        assert not e.request_publications
    finally:b.close()
