"""Actual owner state machine with simulated publication/observations, no hardware."""
from f1_harness.contracts import SKILLS,SkillResult,digest
from test_unpublished_plan import make_bridge
from test_unpublished_retry import fresh_observation


def test_missing_release_evidence_only_rechecks_and_cannot_repeat_open(tmp_path):
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    engine.task=engine.task.model_copy(update=dict(allowed_skills=SKILLS))
    engine.completed_skills.update(('approach_grasp','grasp','verify_grasp'))
    original=engine.pending.model_copy(update=dict(skill='release_supported',task_hash=digest(engine.task)))
    engine.pending=original
    try:
        assert poll() is None
        bridge.future.cancel();bridge.future=None
        # Simulate a known complete gripper publication, then drive the actual
        # verification branch. No transport or robot publisher is present.
        engine.record_skill_publication(original.request_id,publication_id='simulated-open',now_ns=clock[0])
        bridge.future_kind='verify';bridge.verification_after_ns=clock[0];bridge.verification_wait_attempts=0
        bridge.future=bridge.executor.submit(bridge.verifier,engine.running,clock[0])
        for _ in range(2):
            bridge.future.set_result(SkillResult(request_id=original.request_id,lifecycle='completed',
                outcome='pending_review',reason='supported_release_evidence_missing'))
            assert poll() is None
        result=engine.results[original.request_id]
        assert result.verification_request_id==original.request_id
        publication_count=engine.request_publications[original.request_id]['publication_count']
        replacement=original.model_copy(update=dict(request_id='open-again'))
        rejected=engine.submit_skill(replacement.model_dump())
        assert rejected['reason']=='motion_already_attempted_use_registered_recovery'
        assert rejected['no_motion']
        clock[0]+=100_000_000
        fresh=fresh_observation(obs,clock[0]);engine.update_observation(fresh)
        check=original.model_copy(update=dict(request_id='verify-only',verification_request_id=original.request_id,
            observation_id=fresh.observation_id,scene_version=fresh.scene_version))
        assert engine.submit_skill(check.model_dump())['lifecycle']=='accepted'
        assert poll() is None
        future,function,args=bridge.executor.jobs[-1]
        assert function==bridge.verifier
        future.set_result(SkillResult(request_id=check.request_id,lifecycle='completed',outcome='success',
            reason='supported_release_observed',evidence_ids=('simulated-new-rgbd',)))
        assert poll() is None
        assert engine.completed_skills=={'release_supported'}
        assert engine.request_publications[original.request_id]['publication_count']==publication_count
        assert check.request_id not in engine.request_publications
        assert not engine.verification_checkpoints
        assert engine.results[original.request_id].outcome=='pending_review'
    finally:bridge.close()
