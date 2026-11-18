"""Cross-layer state/receipt counterexamples; all output uses memory fixtures."""
import pytest

from f1_harness.adapters import F1ArmToolkit, F1Robot
from f1_harness.contracts import MOTION_SKILLS, HarnessError, SceneObject, SkillIntent, SkillResult, digest
from test_astra_programs import ScriptedBackend, cell
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('skill', sorted(MOTION_SKILLS))
def test_all_motion_skills_preserve_state_and_recover_when_unpublished(tmp_path, skill):
    engine, bridge, clock, _, poll, _ = make_bridge(tmp_path)
    try:
        poll()
        engine.running = engine.running.model_copy(update={'skill': skill})
        before = engine.status()['perception_context']
        assert before['phase'] == 'observe'
        assert before['requested_skill'] == skill and not before['action_published']
        reason = 'plan_revalidation_rejected:revalidation_object_or_hole_changed'
        bridge._discard_unpublished_plan(reason, inputs_unchanged=True)
        result = engine.results['one-request']
        assert result.no_motion and result.retryable and result.outcome == 'failure'
        assert engine.authorized and not engine.hold_requested
        assert not engine.request_publications
        assert engine.status()['perception_context']['phase'] == 'observe'
    finally:
        bridge.close()


def test_publication_changes_phase_and_cannot_be_reclassified_as_no_motion(tmp_path):
    engine, bridge, clock, _, poll, _ = make_bridge(tmp_path)
    try:
        poll()
        engine.record_skill_publication('one-request', publication_id='memory-only', now_ns=clock[0])
        context = engine.status()['perception_context']
        assert context['phase'] == 'approach_grasp' and context['action_published']
        with pytest.raises(HarnessError, match='unpublished_replan_not_eligible'):
            engine.reject_unpublished_for_replan('one-request', reason='owner_candidate_rejected')
        engine.complete('one-request', SkillResult(request_id='one-request', lifecycle='completed',
            outcome='success', evidence_ids=('measured-arrival',)))
        assert engine.results['one-request'].no_motion is False
    finally:
        bridge.close()


def test_completed_evidence_only_request_does_not_claim_publication(tmp_path):
    engine, bridge, _, _, poll, _ = make_bridge(tmp_path)
    try:
        poll()
        engine.running = engine.running.model_copy(update={'skill': 'verify_grasp'})
        engine.complete('one-request', SkillResult(request_id='one-request', lifecycle='completed',
            outcome='success', evidence_ids=('measured-hold',)))
        assert engine.results['one-request'].no_motion is True
        assert 'verify_grasp' in engine.completed_skills
    finally:
        bridge.close()


def test_grasp_can_be_reobserved_after_previously_verified_success(tmp_path):
    engine, bridge, _, _, _, obs = make_bridge(tmp_path)
    try:
        engine.pending = None
        engine.results.clear()
        engine.task = engine.task.model_copy(update={'allowed_skills': tuple(sorted(MOTION_SKILLS))})
        target = SceneObject(object_id='block', shape='square', held=None,
            pose=(1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.),
            frame_id='native', visible=True, evidence_ids=('new-view',))
        engine.update_observation(obs.model_copy(update={'objects': (target,), 'state16': (0.,)*15+(100.,)}))
        engine.completed_skills.update(('approach_grasp','grasp','verify_grasp'))
        engine.attempted_motion.update(('approach_grasp','grasp','verify_grasp'))
        intent = SkillIntent(request_id='recover-after-success', session_id=engine.session,
            owner_instance_id=engine.instance, task_id=engine.task.task_id, task_hash=digest(engine.task),
            epoch=engine.epoch, observation_id=obs.observation_id, scene_version=obs.scene_version,
            calibration_id=obs.calibration_id, skill='recover_grasp')
        result = engine.submit_skill(intent.model_dump())
        assert result['lifecycle'] == 'accepted'
        assert 'verify_grasp' in engine.completed_skills
        assert not engine.request_publications
    finally:
        bridge.close()


@pytest.mark.parametrize('receipt,moved,unknown', [
    (dict(lifecycle='completed', outcome='failure', no_motion=True, retryable=True), False, False),
    (dict(lifecycle='completed', outcome='success', no_motion=False, evidence_ids=['arrival']), True, False),
    (dict(lifecycle='cancelled', outcome='interrupted', no_motion=False), None, True),
])
def test_toolkit_motion_feedback_uses_receipt_not_stateful_call(tmp_path, receipt, moved, unknown):
    backend = ScriptedBackend([dict(request_id='owner-request', **receipt)])
    arm = F1ArmToolkit(F1Robot(backend), arm='cells', workspace=tmp_path, images='on-demand')
    result = cell(arm, "result = robo.skill('approach_grasp')")
    assert result['moved'] is moved and result['motion_status_unknown'] is unknown
    assert result['published_skill_requests'] == int(moved is True)
    assert result['primitive_calls'][0]['evidence_recorded'] == bool(receipt.get('evidence_ids'))
    assert 'env_steps' not in result


def test_unpublished_recovery_keeps_numerical_diagnostic(tmp_path):
    engine, bridge, _, _, poll, _ = make_bridge(tmp_path)
    try:
        poll()
        diagnostic = {'code':'revalidation_object_or_hole_changed','metrics':{'rotation_deg':5.2}}
        engine.reject_unpublished_for_replan('one-request',
            reason='plan_revalidation_rejected:revalidation_object_or_hole_changed',
            planning_diagnostic=diagnostic)
        assert engine.results['one-request'].planning_diagnostic == diagnostic
        assert engine.authorized
    finally:
        bridge.close()
