from rejection_assertions import assert_rejected
import pytest
from f1_harness.contracts import Task, SKILLS, PLACEMENT_SKILLS, Mode, HarnessError
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.fake import FakePlant
from f1_harness.adapters import ScopedBackend, F1Robot
from f1_harness.programs import single_block_program


def run(tmp_path, failures=None, *, full=True):
    e=TaskEngine(EvidenceStore(tmp_path),mode=Mode.FAKE,lease_ms=2000)
    session=e.open_session()['session_id']
    task=Task(task_id='cycle',object_id='square',hole_id='hole',calibration_id='fake',
              allowed_skills=SKILLS if full else PLACEMENT_SKILLS)
    e.prepare_task(session,task.model_dump())
    p=FakePlant(e,failures);p.start()
    b=ScopedBackend(e,p,session_id=session,plant=p)
    ns={'robo':F1Robot(b)}
    exec(single_block_program(task),ns)
    return e,p,b,ns['result']


@pytest.mark.parametrize('failure,placed,reset', [({},True,True),({'withdraw_reset':1},False,False),
    ({'verify_placed':1},False,True),({'grasp':1,'regrasp':1},False,False)])
def test_cycle_success_requires_return_and_preserves_placement_on_return_failure(tmp_path,failure,placed,reset):
    e,p,b,result=run(tmp_path,failure)
    assert p.reset is reset
    assert b.solved() is (placed and reset)
    assert e.status()['perception_context']['placement_verified'] is placed
    if placed:
        assert_rejected(b.execute_tool('execute_skill',{'name':'grasp'}), 'placement_already_verified')
    final=e.finish_task(e.session)
    assert final['placement_success'] is placed
    assert final['reset_success'] is reset
    assert final['reset_required'] is True
    assert final['autonomous_success'] is (placed and reset)
    assert not e.status()['perception_context']['placement_verified']


def test_legacy_verify_before_withdraw_still_preserves_placement_on_return_failure(tmp_path):
    e=TaskEngine(EvidenceStore(tmp_path),mode=Mode.FAKE,lease_ms=2000)
    session=e.open_session()['session_id']
    task=Task(task_id='cycle-legacy',object_id='square',hole_id='hole',calibration_id='fake',
              allowed_skills=SKILLS)
    e.prepare_task(session,task.model_dump())
    p=FakePlant(e,{'withdraw_reset':1});p.start()
    b=ScopedBackend(e,p,session_id=session,plant=p)
    robo=F1Robot(b)
    for skill in ('approach_grasp','grasp','verify_grasp','transfer_above_hole',
                  'inspect_in_hand','align','insert','release','verify_placed'):
        assert robo.skill(skill)['outcome']=='success'
    assert robo.skill('withdraw_reset')['outcome']=='failure'
    assert p.reset is False
    assert b.solved() is False
    assert e.status()['perception_context']['placement_verified'] is True
    final=e.finish_task(e.session)
    assert final['placement_success'] is True and final['reset_success'] is False
    assert final['autonomous_success'] is False


def test_legacy_placement_scope_and_hash_serialization_remain_unchanged(tmp_path):
    default=Task(task_id='t',object_id='o',hole_id='h',calibration_id='c')
    assert default.allowed_skills==PLACEMENT_SKILLS
    e,p,b,_=run(tmp_path,full=False)
    assert not p.reset and b.solved()
    result=e.finish_task(e.session)
    assert result['placement_success'] and result['autonomous_success']
    assert not result['reset_required']
