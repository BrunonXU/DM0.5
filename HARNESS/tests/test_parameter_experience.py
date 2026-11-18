"""Synthetic evidence tests parameter provenance, isolation and real consumption."""
import ast
import json
from pathlib import Path
import pytest
from f1_harness.contracts import Task,HarnessError
from f1_harness.memory import ExperienceStore,read_snapshot
from f1_harness.parameter_experience import select_parameters,library_preferences
from test_astra_programs import setup_cells,cell


def journal(path,*,angle=22.75,return_ok=True):
    path.mkdir()
    scope=dict(owner_instance_id='owner',session_id='session',task_hash='task',epoch=1,calibration_id='cal')
    rows=[dict(kind='run_identity',scenario_id=path.name,synthetic=True)]
    for skill in ['approach_grasp','verify_placed','withdraw_reset']:
        rows.append(dict(kind='skill_accepted',intent=dict(scope,request_id=skill,skill=skill,
            parameters={'pitch_offset_deg':angle,'variant':2} if skill=='approach_grasp' else {})))
        rows.append(dict(kind='harness_skill_first_publication',request_id=skill))
        rows.append(dict(kind='skill_result',skill=skill,result=dict(request_id=skill,
            outcome='success' if return_ok or skill!='withdraw_reset' else 'failure',
            synthetic=True,assisted=False,evidence_ids=['evidence'])))
    (path/'trace.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows));return path


def candidate(memory):
    return memory.propose(title='pitch preference',applies_when='square',skills=['approach_grasp'],
        discovery_run='discovery',discovery_scene='A',calibration_id='cal',
        motion_parameters={'approach_grasp':{'pitch_offset_deg':22.75,'variant':2}})


@pytest.mark.parametrize('angle,return_ok',[(15.,True),(22.75,False)])
def test_wrong_parameter_or_incomplete_return_cannot_promote(tmp_path,angle,return_ok):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True);key=candidate(memory)
    run=journal(tmp_path/'B',angle=angle,return_ok=return_ok)
    assert not memory.verify(key,validation_run=run,scenario_id='B')['accepted']
    with pytest.raises(HarnessError):memory.promote(key)


def test_independent_verified_parameters_are_consumed_and_rejected_preference_falls_back(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True);key=candidate(memory)
    run=journal(tmp_path/'B');assert memory.verify(key,validation_run=run,scenario_id='B')['accepted']
    snapshot=read_snapshot(Path(memory.promote(key)['path']),synthetic=True)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    params,sources=select_parameters(snapshot,task)
    assert params=={'approach_grasp':{'pitch_offset_deg':22.75,'variant':2}} and sources
    assert not select_parameters(snapshot,task.model_copy(update={'calibration_id':'other'}))[0]
    backend,arm,_=setup_cells(tmp_path/'execution',[
        dict(lifecycle='completed',outcome='pending_review',no_motion=True,
             reason='numerical_plan_rejected:registered_candidates_exhausted'),
        dict(outcome='success')])
    cell(arm,library_preferences(snapshot,task))
    assert ast.literal_eval(cell(arm,'motion("approach_grasp")')['value'])['outcome']=='success'
    assert backend.motions==[dict(name='approach_grasp',parameters=params['approach_grasp']),dict(name='approach_grasp')]
