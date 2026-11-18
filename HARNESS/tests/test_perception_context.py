from f1_harness.contracts import Task
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.host import fake_host
from test_authority import request


def test_context_tracks_owner_execution_and_revocation(tmp_path):
    engine, plant, _, _ = fake_host(tmp_path)
    initial = engine.status()['perception_context']
    assert initial['authorized'] and initial['phase'] == 'observe'
    engine.submit_skill(request(engine, plant))
    pending = engine.status()['perception_context']
    assert pending['phase'] == 'observe'
    assert pending['requested_skill'] == 'approach_grasp' and not pending['action_published']
    plant.advance()
    completed = engine.status()['perception_context']
    assert completed['phase'] == 'approach_grasp' and completed['request_id'] is None
    engine.revoke('manual_takeover', preempted=True)
    revoked = engine.status()['perception_context']
    assert revoked['epoch'] != initial['epoch'] and not revoked['authorized']
    assert revoked['task_hash'] == initial['task_hash']


def test_identical_task_preparation_changes_perception_generation(tmp_path):
    engine = TaskEngine(EvidenceStore(tmp_path))
    session = engine.open_session()['session_id']
    task = Task(task_id='same', object_id='block', hole_id='hole', calibration_id='cal').model_dump()
    engine.prepare_task(session, task)
    before = engine.status()['perception_context']
    engine.prepare_task(session, task)
    after = engine.status()['perception_context']
    assert before['task_hash'] == after['task_hash']
    assert before['epoch'] == after['epoch']
    assert before['task_generation'] != after['task_generation']
