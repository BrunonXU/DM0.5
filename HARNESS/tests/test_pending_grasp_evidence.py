"""Semantic uncertainty waits for bounded new evidence without replaying motion."""
import numpy as np
import pytest

from f1_harness.contracts import SceneObject, SkillIntent, SkillResult, Task, digest
from f1_harness.skill_services import VerifyService
from f1_harness.verification import verify
from test_scene_pipeline import fixture
from test_unpublished_plan import make_bridge
from test_unpublished_retry import fresh_observation


def verifier(monkeypatch, *, skill='verify_grasp'):
    clock = [0.]
    monkeypatch.setattr('f1_harness.skill_services.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('f1_harness.skill_services.require_geometry', lambda *a, **kw: None)
    task = Task(task_id='task', object_id='block', hole_id='hole', calibration_id='test-cal')
    intent = SkillIntent(request_id='verify', session_id='session', owner_instance_id='owner',
        task_id=task.task_id, task_hash=digest(task), epoch=1, observation_id='before',
        scene_version='scene', calibration_id=task.calibration_id, skill=skill)
    service = VerifyService(task, clock=lambda: int(clock[0] * 1e9))
    _, base = fixture()
    def observation(stamp, held, *, attachment=True):
        obj = SceneObject(object_id='block', shape='square', visible=True, held=held,
                          evidence_ids=(str(stamp),),
                          eef_object_pose=tuple(np.eye(4).ravel()) if attachment and held is True else None)
        return base.model_copy(update=dict(observation_id='o-'+str(stamp), received_ns=stamp,
            frames=tuple(f.model_copy(update=dict(received_ns=stamp, source_ns=stamp, sequence=stamp)) for f in base.frames),
            stationary=True, calibration_id=task.calibration_id, objects=(obj,)))
    return service, intent, observation, clock


def arriving(service, clock, updates):
    calls = []
    class Arrival:
        def __enter__(self):return self
        def __exit__(self, *_):pass
        def wait(self, remaining):
            calls.append(remaining)
            if updates:
                clock[0] += .2
                service.latest = updates.pop(0)
            else:
                clock[0] += remaining
    service.condition = Arrival()
    return calls


@pytest.mark.parametrize('held,outcome', [(True, 'success'), (False, 'failure')])
def test_fresh_unknown_grasp_waits_for_new_positive_or_negative_evidence(monkeypatch, held, outcome):
    service, intent, obs, clock = verifier(monkeypatch)
    service.latest = obs(200, None)
    calls = arriving(service, clock, [obs(300, held)])
    result = service.dispatch('verify_skill', dict(intent=intent.model_dump(), after_ns=100))
    assert result['outcome'] == outcome and result['evidence_ids'] == ('300',)
    assert len(calls) == 1 and clock[0] == .2


def test_unknown_never_becomes_success_when_no_new_visual_evidence_arrives(monkeypatch):
    service, intent, obs, clock = verifier(monkeypatch)
    unknown = obs(200, None)
    service.latest = unknown
    # A new scene wrapper asserts held=True but contains the already-consumed
    # RGB-D frames. It must not turn this identical evidence into a success.
    metadata_only = unknown.model_copy(update=dict(observation_id='relabelled', objects=obs(300, True).objects,
        frames=tuple(reversed(unknown.frames))))
    calls = arriving(service, clock, [metadata_only])
    result = service.dispatch('verify_skill', dict(intent=intent.model_dump(), after_ns=100))
    assert result['outcome'] == 'pending_review'
    assert result['reason'] == 'grasp_held_evidence_missing'
    assert result['evidence_ids'] == ('200',)
    assert clock[0] == 2. and len(calls) == 2


def test_in_hand_pose_missing_waits_with_its_own_reason(monkeypatch):
    service, intent, obs, clock = verifier(monkeypatch, skill='inspect_in_hand')
    service.latest = obs(200, True, attachment=False)
    arriving(service, clock, [])
    result = service.dispatch('verify_skill', dict(intent=intent.model_dump(), after_ns=100))
    assert result['outcome'] == 'pending_review' and result['reason'] == 'in_hand_transform_missing'
    assert clock[0] == 2.


def test_confirmed_empty_gripper_is_failure_even_without_in_hand_transform(monkeypatch):
    service, intent, obs, _ = verifier(monkeypatch, skill='inspect_in_hand')
    result = verify('inspect_in_hand', service.task, obs(200, False), intent.request_id)
    assert result.outcome == 'failure' and result.reason == 'grasp_observation'


def test_semantic_pending_gets_bounded_readonly_checkpoint_with_new_measurement_watermark(tmp_path):
    engine, bridge, clock, _, poll, obs = make_bridge(tmp_path)
    engine.pending = engine.pending.model_copy(update=dict(skill='verify_grasp'))
    engine.completed_skills.add('grasp')
    original = engine.pending
    try:
        assert poll() is None
        for _ in range(2):
            clock[0] += 100_000_000
            bridge.future.set_result(SkillResult(request_id=original.request_id, lifecycle='completed',
                outcome='pending_review', reason='grasp_held_evidence_missing', evidence_ids=('unknown-frame',)))
            assert poll() is None
        result = engine.results[original.request_id]
        assert result.verification_request_id == original.request_id
        checkpoint = engine.verification_checkpoints[original.request_id]
        assert checkpoint['after_ns'] == clock[0]
        assert not engine.request_publications
        assert 'verify_grasp' not in engine.completed_skills
        clock[0] += 100_000_000
        fresh = fresh_observation(obs, clock[0])
        engine.update_observation(fresh)
        intent = original.model_copy(update=dict(request_id='readonly-continuation',
            verification_request_id=original.request_id, observation_id=fresh.observation_id,
            scene_version=fresh.scene_version))
        engine.submit_skill(intent.model_dump())
        assert poll() is None
        _, function, arguments = bridge.executor.jobs[-1]
        assert function == bridge.verifier and arguments[1] == checkpoint['after_ns']
        bridge.future.set_result(SkillResult(request_id=intent.request_id, lifecycle='completed',
            outcome='success', reason='grasp_observation', evidence_ids=('new-grasp-evidence',)))
        assert poll() is None
        assert engine.results[intent.request_id].outcome == 'success'
        assert not engine.request_publications and not engine.verification_checkpoints
    finally:
        bridge.close()
