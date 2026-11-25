"""Real coarse revalidation distinguishes stale evidence from changed geometry."""
import pytest

from f1_harness.contracts import digest
from f1_harness.revalidation import RevalidationExpired, proof_observation
from f1_harness.revalidation import require_planning_geometry
from test_plan_revalidation import make_owner, observation


def ready_proof(tmp_path):
    engine,bridge,service,clock,state,poll,original=make_owner(tmp_path)
    poll();future,func,args=bridge.executor.jobs[-1]
    candidate=func(*args);clock[0]+=300_000_000;future.set_result(candidate)
    assert poll() is None
    clock[0]+=20_000_000
    latest=observation(clock[0],boot=engine.host_boot_id,name='fresh')
    engine.update_live_observation(latest,engine.status()['perception_context'])
    assert poll() is None
    future,func,args=bridge.executor.jobs[-1];proof=func(*args)
    return engine,bridge,service,clock,state,poll,original,latest,future,proof


def changed_object(obs):
    obj=obs.objects[0];matrix=list(obj.pose);matrix[3]+=.02
    return obs.model_copy(update={'objects':(obj.model_copy(update={'pose':tuple(matrix)}),obs.objects[1])})


@pytest.mark.parametrize('fault',[None,'hash','camera','pairing','object','future_clock','generation'])
def test_delayed_coarse_proof_accepts_age_but_rejects_identity_or_geometry_change(tmp_path,fault):
    engine,bridge,_,clock,_,poll,original,latest,future,proof=ready_proof(tmp_path)
    try:
        clock[0]+=600_000_000
        if fault=='hash':proof['observation_sha256']='bad'
        elif fault=='generation':proof['after_planning_ns']-=1
        elif fault=='future_clock':proof['checked_at_ns']=clock[0]+1
        elif fault in ('camera','pairing','object'):
            live=latest
            if fault=='camera':
                frames=list(live.frames);frames[0]=frames[0].model_copy(update={'camera_info_sha256':'a'*64})
                live=live.model_copy(update={'frames':tuple(frames)})
            elif fault=='pairing':
                live=live.model_copy(update={'pairings':tuple(p.model_copy(update={'quality':'invalid'}) for p in live.pairings)})
            else:live=changed_object(live)
            engine.update_live_observation(live,engine.status()['perception_context'])
        future.set_result(proof)
        target=poll()
        assert not engine.request_publications
        result=engine.results['request']
        if fault is None:
            assert target is not None and engine.authorized and result.lifecycle=='running'
            assert bridge.revalidation_proof is proof
        elif fault=='object':
            assert target is None and bridge.proposal is None
            assert engine.authorized and result.lifecycle=='running'
            assert bridge.pending_revalidation_plan is not None
            # Same request/candidate, fresh worker sweep; never publish an
            # old proof and never recompute IK just because the view changed.
            assert bridge.revalidation_proof is None
        else:
            assert target is None and bridge.proposal is None
            assert not engine.authorized and not result.retryable and result.no_motion
    finally:bridge.close()


def test_expired_proof_changed_geometry_cannot_hide_behind_updated_hash(tmp_path):
    engine,bridge,service,clock,_,_,original,latest,_,proof=ready_proof(tmp_path)
    try:
        from f1_harness.owner_bridge import JointProposal
        proposal=bridge.pending_revalidation_plan
        altered=changed_object(latest)
        proof.update(observation=altered.model_dump(),observation_sha256=digest(altered))
        clock[0]+=600_000_000
        with pytest.raises(RuntimeError,match='proof_measurement_mismatch') as exc:
            proof_observation(proof,proposal=JointProposal(**proposal.__dict__),original=original,
                intent=engine.running,task=service.task,now_ns=clock[0],max_age_ns=500_000_000)
        assert not isinstance(exc.value,RevalidationExpired)
    finally:bridge.close()


@pytest.mark.parametrize('delay',['perception_latency_exceeds_observation_budget','camera_sample_feedback_aged:right'])
def test_known_live_scene_delay_waits_and_revalidates_without_new_motion(tmp_path,delay):
    engine,bridge,_,clock,state,poll,original,_,future,proof=ready_proof(tmp_path)
    try:
        epoch=engine.epoch;deadline=engine.deadline_ns
        engine.invalidate_live_observation(delay)
        future.set_result(proof)
        assert poll() is None and engine.authorized and engine.epoch==epoch
        assert bridge.proposal is None and bridge.current_target is None
        assert bridge.pending_revalidation_plan is not None and engine.deadline_ns==deadline
        assert engine.results['request'].lifecycle=='running' and not engine.request_publications
        # No old cached proof or request can publish while the live scene is absent.
        assert poll() is None
        clock[0]+=20_000_000
        current=observation(clock[0],boot=engine.host_boot_id,name='after-delay')
        engine.update_live_observation(current,engine.status()['perception_context'])
        assert poll() is None
        fresh_future,func,args=bridge.executor.jobs[-1]
        assert args[3].observation_id=='after-delay' and args[2]==original
        fresh_future.set_result(func(*args))
        assert poll() is not None and engine.authorized
        assert bridge.revalidation_proof['observation_sha256']==digest(current)
        assert not engine.request_publications
    finally:bridge.close()


def test_live_scene_wait_is_bounded_by_original_skill_deadline(tmp_path):
    engine,bridge,_,clock,_,poll,_,_,future,proof=ready_proof(tmp_path)
    try:
        engine.invalidate_live_observation('scene_expired');future.set_result(proof)
        assert poll() is None and engine.authorized
        clock[0]=engine.deadline_ns
        assert poll() is None
        assert not engine.authorized and engine.results['request'].reason=='skill_deadline'
        assert not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('reason',['ValueError','source_time_reversed:right','calibration_mismatch',
                                    'feedback_expired:right'])
def test_unknown_or_non_scene_time_invalidation_remains_hard(tmp_path,reason):
    engine,bridge,_,_,_,poll,_,_,future,proof=ready_proof(tmp_path)
    try:
        engine.invalidate_live_observation(reason);future.set_result(proof)
        assert poll() is None and not engine.authorized
        assert not engine.results['request'].retryable and not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('change_geometry',[False,True])
def test_worker_expiry_after_complete_candidate_check_is_distinct_from_scene_change(tmp_path,change_geometry):
    engine,bridge,_,clock,_,poll,_,_,future,_=ready_proof(tmp_path)
    try:
        _,func,args=bridge.executor.jobs[-1]
        if change_geometry:
            args=list(args);args[3]=changed_object(args[3])
        clock[0]+=600_000_000
        try:value=func(*args)
        except Exception as exc:future.set_exception(exc)
        else:future.set_result(value)
        target=poll()
        assert not engine.request_publications
        result=engine.results['request']
        if change_geometry:
            assert target is None
            assert engine.authorized and result.retryable and result.no_motion
            assert 'goal_changed' in result.reason
        else:
            assert target is not None and engine.authorized and result.lifecycle=='running'
            assert bridge.revalidation_proof is not None
    finally:bridge.close()


@pytest.mark.parametrize('fault',[None,'binding','shape','pairing','reference'])
def test_coarse_planning_ingress_age_does_not_override_identity_and_feedback_checks(tmp_path,fault):
    engine,bridge,service,clock,state,poll,original=make_owner(tmp_path)
    try:
        poll();future,func,args=bridge.executor.jobs[-1]
        clock[0]+=600_000_000
        if fault=='binding':args=(args[0].model_copy(update={'task_hash':'wrong'}),*args[1:])
        elif fault=='shape':
            obj=original.objects[0].model_copy(update={'shape':'triangle'})
            altered=original.model_copy(update={'objects':(obj,original.objects[1])})
            args=(args[0],altered,*args[2:])
        elif fault=='pairing':
            altered=original.model_copy(update={'pairings':tuple(p.model_copy(update={'quality':'invalid'}) for p in original.pairings)})
            args=(args[0],altered,*args[2:])
        elif fault=='reference':state['measured16'][7]=.6
        try:value=func(*args)
        except Exception as exc:future.set_exception(exc)
        else:future.set_result(value)
        assert poll() is None and not engine.request_publications
        result=engine.results['request']
        if fault is None:
            assert engine.authorized and result.lifecycle=='running'
            assert bridge.pending_revalidation_plan is not None and service.numerics.calls
            require_planning_geometry(original,intent=args[0],task=engine.task,now_ns=clock[0],max_age_ns=500_000_000)
        else:
            assert not result.retryable
            # Rejected inputs never reach publication. The planning receipt
            # reports that fact without authorizing a retry of bad identity or
            # pairing; physical reference changes remain owner hard failures.
            assert result.no_motion is (fault!='reference')
            assert result.outcome!='success'
            assert bridge.proposal is None
            if fault!='reference':assert engine.running is None
    finally:bridge.close()


def test_expired_worker_cannot_mark_a_new_changed_live_scene_as_age_only(tmp_path):
    engine,bridge,_,clock,_,poll,_,latest,future,_=ready_proof(tmp_path)
    try:
        _,func,args=bridge.executor.jobs[-1]
        clock[0]+=600_000_000
        # A legacy worker can still return the typed expiry receipt; current
        # coarse workers no longer turn processing latency into this failure.
        future.set_exception(RevalidationExpired('revalidation_measurements_expired'))
        current=changed_object(observation(clock[0],boot=engine.host_boot_id,name='changed'))
        engine.update_live_observation(current,engine.status()['perception_context'])
        assert poll() is None and engine.authorized
        assert engine.results['request'].lifecycle=='running' and not engine.results['request'].retryable
        clock[0]+=20_000_000
        current=changed_object(observation(clock[0],boot=engine.host_boot_id,name='changed-fresh'))
        engine.update_live_observation(current,engine.status()['perception_context'])
        assert poll() is None
        new_future,new_func,new_args=bridge.executor.jobs[-1]
        try:new_func(*new_args)
        except Exception as exc:new_future.set_exception(exc)
        assert poll() is None and engine.authorized
        assert 'goal_changed' in engine.results['request'].reason
        assert engine.results['request'].no_motion and engine.results['request'].retryable
        assert not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('expiry_at',['worker','owner'])
def test_legacy_worker_expiry_recovers_but_owner_age_alone_does_not_restart(tmp_path,expiry_at):
    engine,bridge,_,clock,_,poll,original,latest,future,proof=ready_proof(tmp_path)
    try:
        plan=bridge.pending_revalidation_plan;deadline=engine.deadline_ns;epoch=engine.epoch
        jobs_before=len(bridge.executor.jobs)
        clock[0]+=600_000_000
        if expiry_at=='worker':future.set_exception(RevalidationExpired('revalidation_measurements_expired'))
        else:future.set_result(proof)
        if expiry_at=='owner':
            assert poll() is not None and bridge.revalidation_proof is proof
            assert engine.deadline_ns==deadline and engine.epoch==epoch and not engine.request_publications
            return
        assert poll() is None and bridge.pending_revalidation_plan is plan
        assert engine.deadline_ns==deadline and engine.epoch==epoch
        assert not engine.request_publications and bridge.proposal is None
        # The same old frame cannot trigger another worker or publish a target.
        for _ in range(3):assert poll() is None
        assert len(bridge.executor.jobs)==jobs_before
        clock[0]+=20_000_000
        current=observation(clock[0],boot=engine.host_boot_id,name='next-source-frame')
        engine.update_live_observation(current,engine.status()['perception_context'])
        assert poll() is None
        new_future,func,args=bridge.executor.jobs[-1]
        assert args[1] is plan and args[2]==original and args[3]==current
        new_future.set_result(func(*args))
        assert poll() is not None
        assert engine.deadline_ns==deadline and not engine.request_publications
    finally:bridge.close()
