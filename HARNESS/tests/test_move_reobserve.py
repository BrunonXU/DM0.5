"""Real PyRUA cells, scripted owner replies; no physical movement."""
import ast
import pytest
from test_candidate_program import setup, OK, REJECT, MISSING_SCENE
from test_astra_programs import cell, STALL

CANDIDATES=[{'target_frame':'world','target_tcp_pose':[1,0,0,x,0,1,0,0,0,0,1,.5,0,0,0,1]} for x in (.1,.2)]

@pytest.mark.parametrize('isolated',[False,True])
def test_preview_reject_then_move_observe_only_once(tmp_path,isolated):
    backend,arm=setup(tmp_path,[OK,OK],previews=[dict(planned=False,current_scope=True)],isolated=isolated)
    try:
        result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
        assert result['local_motion_completed'] and result['no_motion'] is False
        assert [x['name'] for x in backend.motions]==['move_tcp','reobserve']
        assert list(backend.motions[0]['parameters']['target_tcp_pose'])==CANDIDATES[1]['target_tcp_pose']
    finally:
        if isolated:arm.close()

@pytest.mark.parametrize('changes',[
    {'no_motion':None},{'no_motion':False},{'reason':'feedback_lost'},
    {'outcome':'interrupted'},{'lifecycle':'running'},
])
def test_unknown_or_control_fault_never_tries_another_motion(tmp_path,changes):
    backend,arm=setup(tmp_path,[dict(REJECT,**changes)])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert len(backend.motions)==1 and result['program_stage']=='move_tcp'

def test_observation_failure_keeps_completed_motion(tmp_path):
    backend,arm=setup(tmp_path,[OK,dict(outcome='pending_review',reason='missing_geometry')])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert result['local_motion_completed'] and result['motion_result']['outcome']=='success'
    assert result['outcome']=='pending_review' and result['no_motion'] is False
    assert len(backend.motions)==2

def test_no_publication_rejection_allows_changed_goal(tmp_path):
    backend,arm=setup(tmp_path,[REJECT,OK,OK])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert result['local_motion_completed']
    assert [x['name'] for x in backend.motions]==['move_tcp','move_tcp','reobserve']

def test_failed_resume_cannot_replay_original_motion(tmp_path):
    backend,arm=setup(tmp_path,[dict(STALL,no_motion=False),REJECT])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert [x['name'] for x in backend.motions]==['move_tcp','move_tcp']
    assert 'resume_request_id' in backend.motions[-1]
    assert result['program_stage']=='move_tcp'


@pytest.mark.parametrize('isolated',[False,True])
def test_missing_scene_refreshes_same_local_goal_then_moves(tmp_path,isolated):
    backend,arm=setup(tmp_path,[OK,OK],previews=[MISSING_SCENE],isolated=isolated,
        inspections=[dict(preview=dict(planned=True,current_scope=True))])
    try:
        result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
        assert result['local_motion_completed'] and result['no_motion'] is False
        assert [name for name,_ in backend.calls[:2]]==['preview_skill','inspect_plan']
        assert backend.calls[0][1]==backend.calls[1][1]
        assert backend.calls[0][1]['name']=='move_tcp'
        assert list(backend.calls[0][1]['parameters']['target_tcp_pose'])==CANDIDATES[0]['target_tcp_pose']
        assert list(backend.motions[0]['parameters']['target_tcp_pose'])==CANDIDATES[0]['target_tcp_pose']
        assert len(result['attempts'])==1 and result['attempts'][0]['scene_refreshed']
        assert [m['name'] for m in backend.motions]==['move_tcp','reobserve']
    finally:
        if isolated:arm.close()


@pytest.mark.parametrize('code',['full_scene_required_for_motion','missing_geometry_entities','observation_unavailable'])
def test_persistent_scene_gap_is_not_local_candidate_exhaustion(tmp_path,code):
    missing=dict(MISSING_SCENE,planning_diagnostic=dict(code=code))
    backend,arm=setup(tmp_path,[],previews=[missing],inspections=[dict(preview=missing)])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert result['reason']=='local_motion_scene_unavailable'
    assert result['no_motion'] and result['recovery_hint']=='refresh_scene'
    assert result['planning_diagnostic']['code']==code
    assert len(result['attempts'])==1 and not backend.motions
    assert [name for name,_ in backend.calls]==['preview_skill','inspect_plan']


def test_scope_loss_during_local_scene_refresh_never_moves(tmp_path):
    backend,arm=setup(tmp_path,[],previews=[MISSING_SCENE],
        inspections=[dict(preview=dict(planned=True,current_scope=False))])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert result['reason']=='local_motion_scope_changed' and result['no_motion']
    assert len(backend.calls)==2 and not backend.motions


def test_local_scene_refresh_budget_shared_across_candidates(tmp_path):
    blocked=dict(planned=False,current_scope=True,planning_diagnostic=dict(code='scene_sweep_blocked'))
    backend,arm=setup(tmp_path,[],previews=[MISSING_SCENE,MISSING_SCENE],
        inspections=[dict(preview=blocked)])
    result=ast.literal_eval(cell(arm,f'move_and_reobserve({CANDIDATES!r})')['value'])
    assert result['reason']=='local_motion_scene_unavailable'
    assert len(result['attempts'])==2 and not backend.motions
    assert [name for name,_ in backend.calls]==['preview_skill','inspect_plan','preview_skill']
