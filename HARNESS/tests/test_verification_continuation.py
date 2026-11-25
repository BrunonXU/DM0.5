from rejection_assertions import assert_rejected
import pytest
from f1_harness.contracts import SceneObject, SkillResult, HarnessError
from f1_harness.engine import VERIFICATION_RECHECK_LIMIT
from f1_harness.verification import verify
from test_unpublished_plan import make_bridge
from test_unpublished_retry import fresh_observation


def missing(tmp_path,skill='approach_grasp'):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    if skill=='align':
        e.pending=e.pending.model_copy(update=dict(skill=skill))
        e.completed_skills.update(('verify_grasp','transfer_above_hole','inspect_in_hand'))
    original=e.pending
    poll()
    # This fixture starts after a physical stage; model its owner publication
    # explicitly, rather than relying on SkillResult's old false default.
    e.record_skill_publication(original.request_id,publication_id='memory-original-stage',now_ns=clock[0])
    b.future.cancel();b.future=None
    b.future_kind='verify';b.verification_after_ns=clock[0];b.verification_wait_attempts=0
    b.future=b.executor.submit(b.verifier,e.running,b.verification_after_ns)
    for _ in range(2):
        b.future.set_result(SkillResult(request_id=e.running.request_id,lifecycle='completed',
            outcome='pending_review',reason='fresh_post_motion_scene_missing'))
        poll()
    return e,b,clock,poll,obs,original


@pytest.mark.parametrize('cancel',[False,True])
def test_recheck_never_plans_or_publishes_and_cannot_survive_stop(tmp_path,cancel):
    e,b,clock,poll,obs,original=missing(tmp_path)
    try:
        assert e.results[original.request_id].verification_request_id==original.request_id
        clock[0]+=100_000_000
        latest=fresh_observation(obs,clock[0]);e.update_observation(latest)
        intent=original.model_copy(update=dict(request_id='check-only',
            verification_request_id=original.request_id,observation_id=latest.observation_id,
            scene_version=latest.scene_version))
        e.submit_skill(intent.model_dump());assert poll() is None
        future,func,args=b.executor.jobs[-1]
        assert func==b.verifier and args[1]==10_000_000_000
        with pytest.raises(HarnessError,match='publication_without_active_skill'):
            e.record_skill_publication(intent.request_id,publication_id='forbidden',now_ns=clock[0])
        if cancel:
            future.set_running_or_notify_cancel();b.revoke('user_stop')
        future.set_result(SkillResult(request_id=intent.request_id,lifecycle='completed',outcome='success',
                                     evidence_ids=('new-evidence',)))
        assert poll() is None
        assert e.results[intent.request_id].outcome==('interrupted' if cancel else 'success')
        assert ('approach_grasp' in e.completed_skills)==(not cancel)
        assert set(e.request_publications)=={original.request_id} and not e.verification_checkpoints
        assert e.results[intent.request_id].no_motion is (not cancel)
        assert e.results[original.request_id].outcome=='pending_review'
    finally:b.close()


@pytest.mark.parametrize('outcome',['success','pending_review','failure'])
def test_alignment_recheck_only_clears_correction_on_verified_success(tmp_path,outcome):
    e,b,clock,poll,obs,original=missing(tmp_path,skill='align')
    try:
        assert e.needs_correction
        clock[0]+=20_000_000
        latest=fresh_observation(obs,clock[0]);e.update_observation(latest)
        intent=original.model_copy(update=dict(request_id='alignment-check',
            verification_request_id=original.request_id,observation_id=latest.observation_id,
            scene_version=latest.scene_version))
        corrections=e.corrections
        e.submit_skill(intent.model_dump());assert poll() is None
        b.future.set_result(SkillResult(request_id=intent.request_id,lifecycle='completed',
            outcome=outcome,reason='actual_scene_result',evidence_ids=('new-alignment-evidence',)))
        assert poll() is None and set(e.request_publications)=={original.request_id}
        assert e.needs_correction==(outcome!='success') and e.corrections==corrections
        insertion=original.model_copy(update=dict(request_id='insert-next',skill='insert',
            observation_id=latest.observation_id,scene_version=latest.scene_version))
        if outcome=='success':
            assert e.submit_skill(insertion.model_dump())['lifecycle']=='accepted'
            assert not e.last_failure and not e.verification_checkpoints
        else:
            assert_rejected(e.submit_skill(insertion.model_dump()), 'requires_retreat')
    finally:b.close()


def test_no_unbounded_rechecks_or_rechecking_a_different_skill(tmp_path):
    e,b,clock,poll,obs,original=missing(tmp_path)
    try:
        checkpoint=e.verification_checkpoints[original.request_id]
        bad=original.model_copy(update=dict(request_id='wrong',skill='grasp',verification_request_id=original.request_id))
        assert_rejected(e.submit_skill(bad.model_dump()), 'verification_checkpoint')
        for i in range(VERIFICATION_RECHECK_LIMIT):
            intent=original.model_copy(update=dict(request_id=f'check-{i}',verification_request_id=original.request_id))
            e.submit_skill(intent.model_dump());poll()
            for _ in range(2):
                b.future.set_result(SkillResult(request_id=intent.request_id,lifecycle='completed',
                    reason='fresh_post_motion_scene_missing'))
                poll()
        assert checkpoint['attempts'] == 6
        assert e.corrections == 0
        assert set(e.request_publications) == {original.request_id}
        assert e.results[intent.request_id].verification_request_id is None
        bad=original.model_copy(update=dict(request_id='exhausted',verification_request_id=original.request_id))
        assert_rejected(e.submit_skill(bad.model_dump()), 'verification_checkpoint')
    finally:b.close()


def test_sixth_read_only_recheck_confirms_a_previously_unknown_grasp(tmp_path):
    engine, bridge, clock, _, poll, _ = make_bridge(tmp_path)
    try:
        approach = engine.pending
        grasp = approach.model_copy(update={'skill': 'grasp', 'request_id': 'grasp'})
        for motion in (approach, grasp):
            if motion == grasp:
                assert engine.submit_skill(motion.model_dump())['lifecycle'] == 'accepted'
            assert engine.take_pending() == motion
            engine.record_skill_publication(motion.request_id, publication_id=motion.request_id, now_ns=clock[0])
            engine.complete(motion.request_id, SkillResult(request_id=motion.request_id, lifecycle='completed',
                            outcome='success', evidence_ids=('fixture-arrival',)))

        original = approach.model_copy(update={'skill': 'verify_grasp', 'request_id': 'verify-original'})
        for attempt in range(7):  # The first verification creates the checkpoint; six continuations follow.
            latest = engine.observation
            intent = original if attempt == 0 else original.model_copy(update={
                'request_id': f'verify-{attempt}', 'verification_request_id': original.request_id,
                'observation_id': latest.observation_id, 'scene_version': latest.scene_version,
            })
            assert engine.submit_skill(intent.model_dump())['lifecycle'] == 'accepted'
            assert poll() is None
            held = True if attempt == 6 else None
            for _ in range(1 if held else 2):
                future, function, (running, after_ns) = bridge.executor.jobs[-1]
                assert function == bridge.verifier and running == intent
                clock[0] += 1_000_000
                obj = SceneObject(object_id=engine.task.object_id, shape='square', held=held,
                                  evidence_ids=(f'grasp-evidence-{clock[0]}',))
                latest = fresh_observation(engine.observation, clock[0]).model_copy(update={
                    'objects': (obj,), 'observation_id': f'grasp-scene-{clock[0]}',
                })
                assert latest.received_ns > after_ns
                engine.update_observation(latest)
                future.set_result(verify(intent.skill, engine.task, latest, intent.request_id))
                assert poll() is None
            result = engine.results[intent.request_id]
            assert result.no_motion
            if held:
                assert result.outcome == 'success' and result.evidence_ids == obj.evidence_ids
            else:
                assert result.outcome == 'pending_review'
                assert result.verification_request_id == original.request_id
                assert engine.verification_checkpoints[original.request_id]['attempts'] == attempt

        extra = intent.model_copy(update={'request_id': 'verify-7'})
        assert_rejected(engine.submit_skill(extra.model_dump()), 'verification_checkpoint_or_budget_invalid')
        assert engine.corrections == 0
        assert set(engine.request_publications) == {approach.request_id, grasp.request_id}
    finally:
        bridge.close()
