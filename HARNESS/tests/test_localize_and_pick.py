"""Explicit one-cell execution after localization; no changes to model I/O."""
import ast
from copy import deepcopy
import pytest
from pyrualean.arms import ArmToolkit
from f1_harness.adapters import F1Robot
from f1_harness.astra_programs import install_recovery_helpers
from f1_harness.evidence import EvidenceStore
from f1_harness.sandbox import SandboxedArm
from test_astra_programs import ScriptedBackend, cell

HINTS={'right':[dict(object_id='square-1',box_normalized=[.573,.36,.714,.592])],
       'head':[dict(object_id='square-hole-1',box_normalized=[.437,.453,.486,.515])]}

class Backend(ScriptedBackend):
    def __init__(self,preview,results):
        super().__init__(results);self.preview=deepcopy(preview)
    def execute_tool(self,name,input_dict,**kwargs):
        if name in ('locate_entities','inspect_plan'):
            self.calls.append((name,deepcopy(input_dict)))
            if name=='locate_entities':return dict(accepted=True,search_only=True,motion_evidence=False)
            return dict(preview=self.preview,geometry=dict(geometry_status='complete'))
        return super().execute_tool(name,input_dict,**kwargs)


def make(tmp_path,preview,results,isolated=False):
    backend=Backend(preview,results)
    arm=(SandboxedArm(backend,tmp_path/'worker').start() if isolated else
         ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'worker',images=False))
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'evidence'))
    assert not backend.calls
    return backend,arm


@pytest.mark.parametrize('isolated',[False,True])
def test_localization_uses_owner_plan_once_and_completes_in_one_cell(tmp_path,isolated):
    backend,arm=make(tmp_path,dict(planned=True,current_scope=True),[dict(outcome='success')]*10,isolated)
    try:
        result=ast.literal_eval(cell(arm,f'localize_and_pick({HINTS!r}, pitch_offset_deg=10)')['value'])
        assert result['outcome']=='success'
        assert [n for n,_ in backend.calls[:3]]==['locate_entities','locate_entities','execute_skill']
        assert backend.calls[2][1]==dict(name='approach_grasp',parameters={'pitch_offset_deg':10.})
        assert [m['name'] for m in backend.motions]==['approach_grasp','grasp','verify_grasp',
            'transfer_above_hole','inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']
        assert backend.motions[0]['parameters']=={'pitch_offset_deg':10.}
    finally:
        if isolated:arm.close()


@pytest.mark.parametrize('reason',['observation_unavailable','owner_epoch_changed','task_authority_ended'])
def test_owner_rejection_preserved_without_new_submission(tmp_path,reason):
    rejected=dict(outcome='pending_review',no_motion=True,reason=reason,lifecycle='rejected')
    backend,arm=make(tmp_path,{},[rejected])
    result=ast.literal_eval(cell(arm,f'localize_and_pick({HINTS!r})')['value'])
    assert result['outcome']=='pending_review' and result['no_motion']
    assert len(backend.motions)==1 and result['reason']==reason


def test_stop_between_preview_and_submit_is_not_retried(tmp_path):
    backend,arm=make(tmp_path,dict(planned=True,current_scope=True),[dict(outcome='interrupted',reason='task_authority_ended')])
    result=ast.literal_eval(cell(arm,f'localize_and_pick({HINTS!r})')['value'])
    assert result['outcome']=='interrupted' and len(backend.motions)==1


def test_inspect_plan_remains_readonly(tmp_path):
    backend,arm=make(tmp_path,dict(planned=True,current_scope=True),[])
    cell(arm,'robo.inspect_plan()')
    assert not backend.motions


def test_wrong_view_localization_keeps_original_checkpoint_without_a_new_action(tmp_path):
    backend,arm=make(tmp_path,{},[])
    rejection=dict(accepted=False,reason='unregistered_entity_hint',search_only=True,motion_evidence=False,
        search_entities={'head':[dict(object_id='square-hole-1',role='target_aperture')]})
    def reject(name,arguments,**kwargs):
        assert name=='locate_entities'
        backend.calls.append((name,arguments))
        return rejection
    backend.execute_tool=reject
    result=ast.literal_eval(cell(arm,f'localize_and_pick({HINTS!r})')['value'])
    assert result['no_motion'] and result['reason']==rejection['reason'] and result['localization']==rejection
    checkpoint=dict(outcome='pending_review',program_stage='grasp',reason='await_verification',
        verification_request_id='existing-grasp',no_motion=False)
    resumed=ast.literal_eval(cell(arm,f'resume_pick({checkpoint!r}, {HINTS!r})')['value'])
    assert resumed==dict(checkpoint,localization=rejection)
    assert [name for name,_ in backend.calls]==['locate_entities','locate_entities']


def test_execution_failure_returns_without_new_localization_or_restart(tmp_path):
    backend,arm=make(tmp_path,dict(planned=True,current_scope=True),[
        dict(outcome='success'),dict(outcome='failure',reason='unknown_publication')])
    result=ast.literal_eval(cell(arm,f'localize_and_pick({HINTS!r})')['value'])
    assert result['program_stage']=='grasp' and result['reason']=='unknown_publication'
    assert len(backend.motions)==2


@pytest.mark.parametrize('stage,remaining',[
    ('approach_grasp',['grasp','verify_grasp','transfer_above_hole','inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']),
    ('grasp',['verify_grasp','transfer_above_hole','inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']),
    ('transfer_above_hole',['inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']),
    ('release',['withdraw_reset','verify_placed']),
    ('withdraw_reset',['verify_placed']),
    ('verify_placed',[]),
])
def test_relocalized_completed_stage_resumes_remaining_cycle(tmp_path,stage,remaining):
    backend,arm=make(tmp_path,{},[dict(outcome='success')]*(1+len(remaining)))
    pending=dict(outcome='pending_review',program_stage=stage,verification_request_id='original')
    result=ast.literal_eval(cell(arm,f'resume_pick({pending!r}, {HINTS!r})')['value'])
    assert result['outcome']=='success'
    assert backend.motions[0]==dict(name=stage,verification_request_id='original')
    assert [m['name'] for m in backend.motions[1:]]==remaining


@pytest.mark.parametrize('outcome,reason',[('failure','unknown_publication'),('interrupted','task_authority_ended'),('failure','motion_stalled')])
def test_no_checkpoint_never_replays_unknown_motion(tmp_path,outcome,reason):
    backend,arm=make(tmp_path,{},[])
    failed=dict(outcome=outcome,reason=reason,program_stage='grasp')
    assert ast.literal_eval(cell(arm,f'resume_pick({failed!r})')['value'])==failed
    assert not backend.motions


def test_localize_rejected_default_consumes_geometry_candidates_same_cell():
    from f1_harness.recovery_programs import compile_library
    class Robot:
        def locate_entities(self,*args):return dict(accepted=True)
        def skill(self,*args,**kwargs):
            return dict(outcome='pending_review',no_motion=True,lifecycle='rejected',
                        reason='numerical_plan_rejected:registered_candidates_exhausted')
        def geometry(self):
            return dict(grasp_suggestions=dict(candidates=[dict(parameters={'pitch_offset_deg':10}),dict(parameters={'pitch_offset_deg':20})]))
    namespace={'robo':Robot()};exec(compile_library(1),namespace)
    calls=[]
    def search(candidates,withdraw=True):
        calls.append((candidates,withdraw));return {'outcome':'pending_review','no_motion':True,'reason':'grasp_candidate_search_exhausted'}
    namespace['pick_with_candidates']=search
    result=namespace['localize_and_pick'](HINTS,withdraw=False)
    assert calls==[([{'pitch_offset_deg':10},{'pitch_offset_deg':20}],False)]
    assert result['no_motion'] and result['reason']=='grasp_candidate_search_exhausted'
