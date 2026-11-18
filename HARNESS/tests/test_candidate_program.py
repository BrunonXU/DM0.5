"""Single-cell candidate replacement with explicit no-publication evidence."""
import ast
from copy import deepcopy
import pytest
from pyrualean.arms import ArmToolkit
from f1_harness.adapters import F1Robot
from f1_harness.astra_programs import install_recovery_helpers
from f1_harness.evidence import EvidenceStore
from f1_harness.sandbox import SandboxedArm
from test_astra_programs import ScriptedBackend,cell,STALL

REJECT=dict(outcome='pending_review',lifecycle='completed',no_motion=True,
    reason='numerical_plan_rejected:registered_candidates_exhausted',request_id='rejected')
OK=dict(outcome='success',lifecycle='completed',no_motion=False)
CANDIDATES=[{'pitch_offset_deg':10},{'pitch_offset_deg':20}]

class CandidateBackend(ScriptedBackend):
    def __init__(self,results,previews=None,inspections=None):
        super().__init__(results);self.previews=deepcopy(previews or [])
        self.inspections=deepcopy(inspections or [])
    def execute_tool(self,name,input_dict,**kwargs):
        if name=='inspect_plan':
            self.calls.append((name,deepcopy(input_dict)))
            return self.inspections.pop(0)
        if name=='preview_skill':
            self.calls.append((name,deepcopy(input_dict)))
            return self.previews.pop(0) if self.previews else {'planned':True,'current_scope':True}
        return super().execute_tool(name,input_dict,**kwargs)

def setup(tmp_path,results,previews=None,isolated=False,inspections=None):
    backend=CandidateBackend(results,previews,inspections)
    arm=(SandboxedArm(backend,tmp_path/'worker').start() if isolated else
         ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'worker',images=False))
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'evidence'))
    return backend,arm

@pytest.mark.parametrize('isolated',[False,True])
def test_changed_scene_zero_output_failure_tries_next_then_completes(tmp_path,isolated):
    backend,arm=setup(tmp_path,[REJECT]+[OK]*10,isolated=isolated)
    try:
        result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
        assert result['outcome']=='success'
        assert [c['name'] for c in backend.motions]==['approach_grasp','approach_grasp','grasp',
            'verify_grasp','transfer_above_hole','inspect_in_hand','align','insert','release','verify_placed','withdraw_reset']
        assert backend.motions[0]['parameters']!=backend.motions[1]['parameters']
        assert len(result['candidate_search'])==2
    finally:
        if isolated:arm.close()

@pytest.mark.parametrize('changes',[
    {'no_motion':False},{'no_motion':None},{'outcome':'interrupted'},
    {'lifecycle':'running'},{'reason':'submission_unknown_stop_and_query'},
    {'reason':'feedback_lost'},{'reason':'observation_wait_authority_changed'}])
def test_unknown_moving_or_authority_failure_never_switches_candidate(tmp_path,changes):
    failure=dict(REJECT,**changes)
    backend,arm=setup(tmp_path,[failure])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['reason']==failure['reason']
    assert len(backend.motions)==1

def test_zero_output_resume_failure_does_not_erase_original_motion(tmp_path):
    backend,arm=setup(tmp_path,[dict(STALL,no_motion=False),REJECT])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['outcome']=='pending_review'
    assert backend.motions==[{'name':'approach_grasp','parameters':CANDIDATES[0]},
        {'name':'approach_grasp','resume_request_id':'known-motion'}]
    assert len(result['candidate_search'])==1

def test_grasp_failure_never_restarts_approach(tmp_path):
    backend,arm=setup(tmp_path,[OK,REJECT])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['program_stage']=='grasp'
    assert [r['name'] for r in backend.motions]==['approach_grasp','grasp']

def test_duplicates_and_exhaustion_remain_bounded(tmp_path):
    previews=[{'planned':False,'current_scope':True,'planning_diagnostic':{'code':'blocked'}}]*2
    backend,arm=setup(tmp_path,[],previews)
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({[CANDIDATES[0]]*4+CANDIDATES!r})')['value'])
    assert result['candidate_search_exhausted'] and len(result['candidate_search'])==2
    assert not backend.motions and len(backend.calls)==2

def test_preview_scope_loss_stops_search(tmp_path):
    backend,arm=setup(tmp_path,[],[{'planned':True,'current_scope':False}])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['reason']=='grasp_candidate_preview_scope_changed'
    assert not backend.motions and len(backend.calls)==1


MISSING_SCENE=dict(planned=False,current_scope=True,no_motion=True,
    planning_diagnostic=dict(code='missing_geometry_entities',metrics=dict(
        missing_entities=[dict(role='aperture',object_id='hole',reason='not_observed',recovery='relocalize_fixture_and_aperture')],
        recovery_hint='refresh_scene')))


@pytest.mark.parametrize('isolated',[False,True])
def test_missing_scene_refreshes_same_candidate_once_then_executes(tmp_path,isolated):
    backend,arm=setup(tmp_path,[OK]*10,[MISSING_SCENE],isolated=isolated,
        inspections=[dict(preview=dict(planned=True,current_scope=True))])
    try:
        result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
        assert result['outcome']=='success'
        assert backend.calls[:2]==[('preview_skill',dict(name='approach_grasp',parameters=CANDIDATES[0])),
            ('inspect_plan',dict(name='approach_grasp',parameters=CANDIDATES[0]))]
        assert backend.motions[0]['parameters']==CANDIDATES[0]
        assert len(result['candidate_search'])==1 and result['candidate_search'][0]['scene_refreshed']
    finally:
        if isolated:arm.close()


@pytest.mark.parametrize('refreshed',[
    MISSING_SCENE,
    dict(MISSING_SCENE,planning_diagnostic=dict(code='full_scene_required_for_motion')),
])
def test_persistent_scene_gap_does_not_exhaust_or_submit_other_candidates(tmp_path,refreshed):
    backend,arm=setup(tmp_path,[],[MISSING_SCENE],inspections=[dict(preview=refreshed)])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['reason']=='grasp_candidate_scene_unavailable'
    assert result['no_motion'] and result['recovery_hint']=='refresh_scene'
    assert not result.get('candidate_search_exhausted')
    assert len(result['candidate_search'])==1 and len(backend.calls)==2 and not backend.motions


def test_scope_loss_during_scene_refresh_never_executes(tmp_path):
    backend,arm=setup(tmp_path,[],[MISSING_SCENE],
        inspections=[dict(preview=dict(planned=True,current_scope=False))])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['reason']=='grasp_candidate_preview_scope_changed'
    assert not backend.motions and len(backend.calls)==2


def test_scene_refresh_budget_is_shared_across_candidate_list(tmp_path):
    blocked=dict(planned=False,current_scope=True,planning_diagnostic=dict(code='scene_sweep_blocked'))
    backend,arm=setup(tmp_path,[],[MISSING_SCENE,MISSING_SCENE],inspections=[dict(preview=blocked)])
    result=ast.literal_eval(cell(arm,f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['reason']=='grasp_candidate_scene_unavailable'
    assert len(result['candidate_search'])==2 and not backend.motions
    assert [name for name,_ in backend.calls]==['preview_skill','inspect_plan','preview_skill']


def test_actual_owner_zero_publication_receipt_drives_candidate_replacement(tmp_path):
    """Consume the production owner's receipt, not a hand-written substitute."""
    import time
    from f1_harness.host import fake_host
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.contracts import SkillIntent, digest

    engine, plant, _, _ = fake_host(tmp_path / 'owner')
    observation = plant.observe()
    intent = SkillIntent(request_id='owner-rejected', session_id=engine.session,
        owner_instance_id=engine.instance, task_id=engine.task.task_id,
        task_hash=digest(engine.task), epoch=engine.epoch,
        observation_id=observation.observation_id, scene_version=observation.scene_version,
        calibration_id=engine.task.calibration_id, skill='approach_grasp')
    engine.submit_skill(intent.model_dump())

    def reject(*args):
        raise RuntimeError('registered_candidates_exhausted')

    bridge = OwnerBridge(engine, numerical_planner=reject, result_verifier=lambda *a: None,
        feedback_age_ns=1_000_000_000, reference_tolerance_deg=.5)
    try:
        deadline = time.monotonic() + 1
        while engine.results[intent.request_id].lifecycle != 'completed' and time.monotonic() < deadline:
            assert bridge.poll(full22=(0.,)*22, measured16=(0.,)*16,
                stamps16=(engine.clock(),)*16, held16=(0.,)*16) is None
            time.sleep(.001)
        receipt = engine.results[intent.request_id].model_dump()
        assert receipt['lifecycle'] == 'completed'
        assert intent.request_id not in engine.request_publications
        assert receipt['no_motion'] is True
        assert receipt['reason'] == REJECT['reason']
    finally:
        bridge.close()

    backend, arm = setup(tmp_path / 'program', [receipt] + [OK]*10)
    result = ast.literal_eval(cell(arm, f'pick_with_candidates({CANDIDATES!r})')['value'])
    assert result['outcome'] == 'success'
    assert result['candidate_search'][0]['request_id'] == intent.request_id
    assert len(result['candidate_search']) == 2
    assert [m['parameters'] for m in backend.motions[:2]] == CANDIDATES
