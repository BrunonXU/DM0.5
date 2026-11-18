import pytest

from f1_harness.contracts import HarnessError, Observation, Task
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore


def test_continuous_scene_does_not_replace_request_snapshot(tmp_path):
    engine=TaskEngine(EvidenceStore(tmp_path),mode='fake')
    session=engine.open_session()['session_id']
    engine.prepare_task(session,Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal').model_dump())
    original=Observation(observation_id='original',acquisition_instance='capture',received_ns=engine.clock(),clock_domain='test')
    engine.update_observation(original)
    new=original.model_copy(update={'observation_id':'new'})
    context=engine.status()['perception_context']
    engine.update_live_observation(new,context)
    assert engine.observation is original and engine.live_observation is new
    engine.revoke('test_cancel')
    assert engine.live_observation is None and engine.observation is original
    with pytest.raises(HarnessError,match='scope_changed'):
        engine.update_live_observation(new,context)
