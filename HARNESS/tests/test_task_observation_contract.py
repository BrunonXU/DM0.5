"""Replay the first V27 cell: absent vision must not hide its requested views."""
import json
from pathlib import Path
import pytest
from pyrualean.arms import ArmToolkit
from f1_harness.adapters import F1Robot
from test_remote_readiness import backend


def test_static_instruction_does_not_query_vision_or_owner(monkeypatch):
    b=backend(monkeypatch,[ValueError('object_not_visible_in_either_calibrated_view')])
    b.task=b.task.model_copy(update=dict(operator_instruction='先将躺卧积木扶正再抓走'))
    assert F1Robot(b).task=='先将躺卧积木扶正再抓走'
    assert b.calls==[] and not b.submissions and b.clock.value==0


def test_saved_first_cell_reaches_both_show_calls_when_target_is_missing(monkeypatch,tmp_path):
    b=backend(monkeypatch,[ValueError('object_not_visible_in_either_calibrated_view')])
    b.task=b.task.model_copy(update=dict(operator_instruction='将桌上的方块放入对应方孔，松爪后确认落入。'))
    requested=[]
    def artifact(name,step=-1):
        requested.append(name)
        # This test checks cell/observation control flow, not image accuracy.
        return b'image-fixture'
    b.artifact=artifact
    arm=ArmToolkit(F1Robot(b),arm='cells',workspace=tmp_path,images='on-demand')
    value=arm.execute_tool('python',{'code':"print(robo.task)\nprint(robo.observe())\nrobo.show('head')\nrobo.show('right')"})
    assert not value.result.get('exception') and not value.result.get('error')
    assert requested==['head.png','right.png']
    assert 'geometry_available' in value.result['stdout']
    assert not b.submissions and b.clock.value==0


def test_state_retains_missing_observation_as_unknown_not_a_fabricated_pose(monkeypatch):
    b=backend(monkeypatch,[ValueError('object_not_visible_in_either_calibrated_view')])
    state=F1Robot(b).state()
    assert state.observation_id=='unavailable' and state.objects==()
    assert not b.submissions


def test_v29_actual_first_cell_reaches_requested_views_on_ordinary_missing_mask(monkeypatch,tmp_path):
    saved=Path(__file__).resolve().parents[1]/'runs/workspace/web-0731f656c8614760a4a02d0f48230f3a/planner/trace.jsonl'
    if not saved.exists():pytest.skip('local V29 physical trace unavailable')
    rows=[json.loads(line) for line in saved.read_text().splitlines()]
    first=next(r for r in rows if r['kind']=='planner_tool_call')
    assert first['arguments']['image_reason']
    b=backend(monkeypatch,[ValueError('mask_view=right:mask_uncertain_requires_new_prompt:object=square-1')])
    requested=[]
    b.artifact=lambda name,step=-1:requested.append(name) or b'image-fixture'
    arm=ArmToolkit(F1Robot(b),arm='cells',workspace=tmp_path,images='on-demand')
    result=arm.execute_tool('python',{'code':first['arguments']['code']})
    assert not result.result.get('exception')
    assert requested==['right.png','head.png']
    assert not b.submissions


@pytest.mark.parametrize('fault',['IPC message too large','camera_identity_changed','invalid_observation_schema'])
def test_state_infrastructure_or_identity_fault_is_not_relabelled_missing(monkeypatch,tmp_path,fault):
    b=backend(monkeypatch,[ValueError(fault)]);original=b.call;requested=[]
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':value['observation_readiness']=dict(state='needs_attention',code=fault)
        return value
    b.call=call;b.artifact=lambda name,step=-1:requested.append(name) or b'image-fixture'
    arm=ArmToolkit(F1Robot(b),arm='cells',workspace=tmp_path,images='on-demand')
    result=arm.execute_tool('python',{'code':"robo.state()\nrobo.show('right')"})
    assert fault in result.result['exception'] and not requested and not b.submissions
