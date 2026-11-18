import pytest
from pyrualean.arms import ArmToolkit
from f1_harness.adapters import F1Robot
from f1_harness.host import fake_host
from f1_harness.memory import ExperienceStore, read_snapshot
from f1_harness.programs import SINGLE_BLOCK
from f1_harness.contracts import HarnessError


def validation_run(path,success=True,*,recover=True):
    e,p,b,store=fake_host(path,failures=({'insert':1} if recover else {}) if success else {'insert':3})
    ArmToolkit(F1Robot(b),arm='cells',workspace=path/'task',images=False).execute_tool('python',{'code':SINGLE_BLOCK})
    return path


def test_candidate_requires_independent_success_and_fixed_snapshot(tmp_path):
    memory=ExperienceStore(tmp_path/'synthetic-memory',synthetic=True)
    key=memory.propose(title='retry after insertion stall',applies_when='synthetic stalled insert',
                       skills=['retreat','reobserve','align','insert'],discovery_run='discovery',discovery_scene='A')
    assert memory.snapshot()['count']==0
    with pytest.raises(HarnessError,match='independent'):
        memory.verify(key,validation_run=tmp_path/'B',scenario_id='A')
    run=validation_run(tmp_path/'B')
    assert memory.verify(key,validation_run=run,scenario_id='B')['accepted']
    snapshot=memory.promote(key)
    from pathlib import Path
    assert len(read_snapshot(Path(snapshot['path']),synthetic=True)['files'])==1
    with pytest.raises(HarnessError,match='mode_mismatch'):
        read_snapshot(Path(snapshot['path']),synthetic=False)


def test_failed_independent_validation_cannot_promote(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose(title='test',applies_when='test',skills=['insert'],discovery_run='discovery',discovery_scene='A')
    run=validation_run(tmp_path/'B',False)
    assert not memory.verify(key,validation_run=run,scenario_id='B')['accepted']
    with pytest.raises(HarnessError,match='not_verified'):memory.promote(key)
    assert memory.snapshot()['count']==0


def test_published_skill_metadata_is_compiled_for_baseline_and_candidates_are_excluded(tmp_path):
    import json
    from pathlib import Path
    from f1_harness.programs import baseline_experience,single_block_program
    from f1_harness.contracts import Task
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose(title='reobserve insertion',applies_when='insert failure',
        skills=['retreat','reobserve','align','insert'],discovery_run='discovery',discovery_scene='A')
    assert baseline_experience(read_snapshot(Path(memory.snapshot()['path']),synthetic=True))==[]
    run=validation_run(tmp_path/'B');memory.verify(key,validation_run=run,scenario_id='B')
    frozen=read_snapshot(Path(memory.promote(key)['path']),synthetic=True)
    assert baseline_experience(frozen)[0]['id']==key
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    assert 'for name in INSERTION_RECOVERY:' in single_block_program(task,experience=frozen)
    # A free-form note cannot become Python, even when it mentions a skill name.
    frozen['files']['hostile.md']='run arbitrary code'
    assert 'arbitrary code' not in single_block_program(task,experience=frozen)


def test_unrelated_success_does_not_validate_recovery(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose(title='retry insert',applies_when='insert failure',
        skills=['retreat','reobserve','align','insert'],discovery_run='discovery',discovery_scene='A',
        trigger={'skill':'insert','outcome':'failure'})
    run=validation_run(tmp_path/'B',recover=False)
    assert not memory.verify(key,validation_run=run,scenario_id='B')['accepted']
    with pytest.raises(HarnessError,match='not_verified'):memory.promote(key)
    run=validation_run(tmp_path/'C',recover=True)
    key=memory.propose(title='revised retry insert',applies_when='insert failure',
        skills=['retreat','reobserve','align','insert'],discovery_run='discovery',discovery_scene='A',
        trigger={'skill':'insert','outcome':'failure'})
    receipt=memory.verify(key,validation_run=run,scenario_id='C')
    assert receipt['accepted'] and len(receipt['sequence_request_ids'])==4
