import json
from types import SimpleNamespace

import pytest

from f1_harness.evidence import EvidenceStore
from f1_harness.planner import ResponsesPlanner
from f1_harness.program_experience import (
    archive_run_programs, record_candidate, reference_programs, validate_candidate,
)


# Test-only program: no robot movement and no task solution.
SOURCE = '''def applicable(scene):
    return scene.get("available") is True

def run_skill(robo):
    return robo.observe()
'''


def proposal(source=SOURCE):
    return dict(title='Observation example', claim='Read current observation only', source=source)


@pytest.mark.parametrize('source', [
    SOURCE + '\nrun_skill(robo)',
    SOURCE.replace('def run_skill(robo):', 'def run_skill(robo=move()):'),
    SOURCE.replace('def run_skill(robo):', '@move()\ndef run_skill(robo):'),
    SOURCE.replace('scene.get("available") is True', 'robo.skill("grasp")'),
    SOURCE.replace('scene.get("available") is True', 'scene.clear()'),
    SOURCE.replace('def run_skill(robo):', 'def run_skill(robo: move()):'),
])
def test_candidate_load_cannot_execute_actions(source):
    with pytest.raises(ValueError):
        validate_candidate(proposal(source))


def test_partial_run_archives_exact_program_without_claiming_execution(tmp_path):
    run=tmp_path/'run'; root=tmp_path/'experience'
    planner=EvidenceStore(run/'planner')
    record_candidate(planner,proposal(),call_id='c',request_id='model')
    owner=EvidenceStore(run/'collected/owner/harness')
    owner.event('skill_accepted',intent=dict(calibration_id='cal'))
    owner.event('skill_result',skill='move_tcp',result=dict(outcome='pending_review',no_motion=True))
    ids=archive_run_programs(run,root)
    assert ids == archive_run_programs(run,root) and len(ids)==1
    item=json.loads((root/'program-candidates'/f'{ids[0]}.json').read_text())
    assert item['candidate']['source']==SOURCE and not item['verified']
    assert item['execution_of_proposed_source']=='unverified'
    assert item['observed_skill_results'][0]['result']['outcome']=='pending_review'
    snapshot=reference_programs(root,'cal')
    assert len(snapshot['entries'])==1 and not snapshot['executable_default']
    assert not reference_programs(root,'other')['entries']
    path=root/'program-candidates'/f'{ids[0]}.json'
    path.write_text(path.read_text().replace('Read current observation only','altered claim'))
    assert not reference_programs(root,'cal')['entries']


@pytest.mark.parametrize('valid',[True,False])
def test_planner_metadata_never_reaches_robot_and_preserves_receipt(tmp_path,valid):
    class Arm:
        def get_tools_spec(self):
            return [dict(name=n,description=n,input_schema={}) for n in ('python','finish')]
        def execute_tool(self,name,args):
            assert 'skill_candidate' not in args
            return SimpleNamespace(result=dict(status='stuck',stdout='no action'),images=[])
    class Planner(ResponsesPlanner):
        def __init__(self): self.store=EvidenceStore(tmp_path); self.calls=0
        def request(self,**kwargs):
            self.calls+=1
            if self.calls==2:
                feedback=json.loads(kwargs['input'][-1]['output'])
                assert feedback['program_candidate']['accepted'] is valid
            args=dict(code='pass',skill_candidate=proposal() if valid else {'source':'bad'})
            return dict(output=[dict(type='function_call',name='python' if self.calls==1 else 'finish',
                call_id=str(self.calls),arguments=json.dumps(args if self.calls==1 else {}))])
    p=Planner(); p.run(Arm(),task_text='No action')
    rows=[json.loads(x) for x in (tmp_path/'trace.jsonl').read_text().splitlines()]
    assert any(r['kind']=='planner_tool_result' for r in rows)
    assert any(r['kind']==('program_candidate_proposed' if valid else 'program_candidate_rejected') for r in rows)
