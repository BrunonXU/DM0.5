"""Real contracts/cells and owner lifecycle; no robot or model API."""
import ast
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from f1_harness.contracts import MotionParameters, SkillIntent, HarnessError, SkillResult
from test_grasp_selection_planning import setup
from test_astra_programs import setup_cells, cell
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('params',[{'pitch_offset_deg':float('nan')},{'pitch_offset_deg':True},
    {'pitch_offset_deg':46.},{'variant':True},{'variant':4},{'joints':[0]*7}])
def test_nonphysical_or_unrecognized_parameters_rejected(params):
    with pytest.raises(ValidationError):MotionParameters.model_validate(params)


def test_continuous_nonregistered_angle_and_specific_direction_reach_planner(monkeypatch):
    plan,obs,intent,_=setup();plan.fixture['parameterized_grasp']=True
    intent=SkillIntent.model_validate(dict(intent.model_dump(),parameters={'pitch_offset_deg':22.75,'variant':2}))
    seen=[]
    def solve(intent,scene,held,full,variant,*,deadline_ns=None): 
        seen.append((scene.grasp_candidate_descriptor,variant))
        return {'geometry_trace':[{}]}
    monkeypatch.setattr(plan,'_solve_candidate',solve)
    plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16)))
    assert seen==[({'strategy':'tcp_pitch','pitch_offset_deg':22.75},2)]


def test_actual_owner_zero_publication_rejection_allows_changed_plan(tmp_path):
    engine,bridge,_,_,poll,_=make_bridge(tmp_path)
    try:
        intent=engine.pending
        poll()
        bridge.future.set_exception(HarnessError('registered_candidates_exhausted:'+json.dumps({
            'diagnostic':{'code':'registered_candidates_exhausted','metrics':{}},'candidates':[]})))
        poll()
        result=engine.results[intent.request_id]
        assert result.no_motion and 'approach_grasp' not in engine.attempted_motion
        changed=dict(intent.model_dump(),request_id='new-strategy',parameters={'pitch_offset_deg':22.75,'variant':2})
        assert engine.submit_skill(changed)['lifecycle']=='accepted'
        assert engine.pending.parameters.pitch_offset_deg==22.75
    finally:bridge.close()


def test_physical_publication_cannot_be_declared_zero_motion(tmp_path):
    engine,bridge,clock,_,poll,_=make_bridge(tmp_path)
    try:
        intent=engine.pending;poll()
        engine.record_skill_publication(intent.request_id,publication_id='actual',now_ns=clock[0])
        with pytest.raises(HarnessError,match='unpublished_replan'):
            engine.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
                outcome='pending_review',reason='numerical_plan_rejected:registered_candidates_exhausted',no_motion=True))
        assert 'approach_grasp' in engine.attempted_motion
    finally:bridge.close()


def test_pyrua_parameter_choice_then_fixed_macro_without_model_roundtrips(tmp_path):
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'}]*10)
    cell(arm,'motion("approach_grasp", pitch_offset_deg=22.75, variant=2)')
    cell(arm,'motion("grasp")')
    cell(arm,'skill("verify_grasp")')
    cell(arm,'place_held_block()')
    assert backend.motions[0]['parameters']=={'pitch_offset_deg':22.75,'variant':2}
    assert [v['name'] for v in backend.motions]==['approach_grasp','grasp','verify_grasp',
        'transfer_above_hole','inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']


def test_macro_stops_before_release_if_insert_has_no_success(tmp_path):
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'}]*3+[{'outcome':'pending_review'}])
    assert ast.literal_eval(cell(arm,'place_held_block()')['value'])['outcome']=='pending_review'
    assert [v['name'] for v in backend.motions]==['transfer_above_hole','inspect_in_hand','align','insert']


def test_one_real_pyrua_cell_runs_entire_guarded_pick_place_without_planner_between_steps(tmp_path):
    backend,arm,receipt=setup_cells(tmp_path,[{'outcome':'success'}]*10)
    result=ast.literal_eval(cell(arm,'pick_and_place(pitch_offset_deg=10., variant=2)')['value'])
    assert result['outcome']=='success'
    assert [v['name'] for v in backend.motions]==['approach_grasp','grasp','verify_grasp',
        'transfer_above_hole','inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']
    assert backend.motions[0]['parameters']=={'pitch_offset_deg':10.,'variant':2}
    assert 'pick_and_place' in receipt['helper_names']


@pytest.mark.parametrize('grasp_result',[{'outcome':'pending_review','reason':'grasp_held_evidence_missing'},
    {'outcome':'failure','reason':'grasp_observation'}, {'outcome':'interrupted'}])
def test_same_cell_never_transports_an_unconfirmed_grasp(tmp_path,grasp_result):
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'}]*2+[grasp_result])
    result=ast.literal_eval(cell(arm,'pick_and_place()')['value'])
    assert result==dict(grasp_result,program_stage='verify_grasp')
    assert [v['name'] for v in backend.motions]==['approach_grasp','grasp','verify_grasp']


@pytest.mark.parametrize('second_outcome',['success','failure','pending_review'])
def test_measured_miss_uses_one_prepared_regrasp_then_verifies_before_transport(tmp_path,second_outcome):
    from f1_harness.astra_programs import install_recovery_helpers
    from f1_harness.evidence import EvidenceStore
    miss=dict(outcome='failure',reason='grasp_observation')
    second=dict(outcome=second_outcome,reason='grasp_observation')
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'}]*2+[miss,{'outcome':'success'},{'outcome':'success'},second]+[{'outcome':'success'}]*7)
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'new-receipt'),regrasp_limit=1)
    result=ast.literal_eval(cell(arm,'pick_and_place()')['value'])
    names=[v['name'] for v in backend.motions]
    assert names[:6]==['approach_grasp','grasp','verify_grasp','reobserve','regrasp','verify_grasp']
    assert names.count('regrasp')==1
    assert ('transfer_above_hole' in names)==(second_outcome=='success')
    assert result['outcome']==second_outcome


def test_unknown_hold_does_not_regrasp_even_with_budget(tmp_path):
    from f1_harness.astra_programs import install_recovery_helpers
    from f1_harness.evidence import EvidenceStore
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'}]*2+[
        {'outcome':'pending_review','reason':'grasp_held_evidence_missing'}])
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'new-receipt'),regrasp_limit=1)
    assert ast.literal_eval(cell(arm,'pick_and_place()')['value'])['outcome']=='pending_review'
    assert [v['name'] for v in backend.motions]==['approach_grasp','grasp','verify_grasp']


@pytest.mark.parametrize('cancel_stage',['cancel_requested','owner_hold_committed','measured_stationary'])
def test_early_lift_miss_requires_confirmed_hold_before_single_regrasp(tmp_path,cancel_stage):
    from f1_harness.astra_programs import install_recovery_helpers
    from f1_harness.evidence import EvidenceStore
    backend,arm,_=setup_cells(tmp_path,[{'outcome':'success'},
        {'outcome':'failure','reason':'grasp_not_attached','cancel_stage':cancel_stage}]+[{'outcome':'success'}]*10)
    install_recovery_helpers(arm,2,EvidenceStore(tmp_path/'new-receipt'),regrasp_limit=1)
    result=ast.literal_eval(cell(arm,'pick_and_place()')['value'])
    names=[v['name'] for v in backend.motions]
    if cancel_stage=='measured_stationary':
        assert names[:5]==['approach_grasp','grasp','reobserve','regrasp','verify_grasp']
        assert names.count('regrasp')==1 and result['outcome']=='success'
    else:
        assert names==['approach_grasp','grasp'] and result['outcome']=='failure'
