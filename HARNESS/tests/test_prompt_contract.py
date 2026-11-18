import ast
import inspect
from types import SimpleNamespace

import pytest
from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.prompt_contract import api_reference, instructions, INITIAL_CONTINUATION, tool_description
from f1_harness.planner_decision import PlannerDecision
from f1_harness.recovery_programs import compile_library


def receipt(name, **fields):
    return dict(tool='execute_skill', kwargs={'name': name}, ok=True, **fields)


def test_api_uses_live_keyword_signatures_and_f1_receipts():
    text = api_reference()
    for name in ('preview', 'inspect_plan', 'skill'):
        sig = inspect.signature(getattr(F1Robot, name))
        sig = sig.replace(parameters=[p for p in sig.parameters.values() if p.name != 'self'])
        assert f'robo.{name}{sig}' in text
    assert '**parameters' in text
    assert 'class Move(' not in text and '0.30 m' not in text
    assert 'pick_with_candidates(candidates, withdraw=True)' in text
    assert 'dictionaries' in text
    assert 'movement alone does NOT attach' in instructions()
    assert 'motion does not automatically attach' in tool_description('python','old')


class Backend:
    def __init__(self): self.calls = []
    def solved(self): return False
    def env_steps(self): return 0
    def execute_tool(self, name, args):
        self.calls.append((name,args))
        assert name == 'execute_skill'
        return dict(outcome='success', request_id=str(len(self.calls)))


def test_normal_example_executes_installed_macro_in_real_pyrua_cell(tmp_path):
    backend = Backend()
    arm = ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images=False)
    code = compile_library(2) + '\ninitial_plan = '+repr(dict(planned=True,current_scope=True,geometry={}))+'\n'+INITIAL_CONTINUATION
    result = arm.execute_tool('python',dict(code=code)).result
    assert not result.get('error') and not result.get('exception'), result
    names = [args['name'] for _,args in backend.calls]
    assert names == ['approach_grasp','grasp','verify_grasp','transfer_above_hole',
                     'inspect_in_hand','align','insert','release','verify_placed','withdraw_reset']


@pytest.mark.parametrize('stage', ['transfer_above_hole','inspect_in_hand','align','insert',
                                  'release','verify_placed','withdraw_reset'])
def test_macro_stopped_stage_always_matches_last_receipt(stage):
    calls = []
    def skill(name, **parameters):
        calls.append(name)
        return dict(outcome='pending_review' if name==stage else 'success',
                    request_id=name, reason='unknown_evidence' if name==stage else None)
    scope = {'robo':SimpleNamespace(skill=skill)}
    exec(compile_library(0),scope)
    result = scope['place_held_block']()
    assert result['program_stage']==stage and calls[-1]==stage
    assert result['outcome']=='pending_review'


def test_cached_unobserved_does_not_override_feasible_receipt():
    ctx = PlannerDecision().update(dict(state={'phase':'unobserved'},primitive_calls=[
        dict(tool='inspect_plan',kwargs={},ok=True,planned=True,current_scope=True)]))
    assert ctx['latest_receipt']['planned'] is True
    assert ctx['next_decision']=='execute_guarded_program_no_preview_confirmation_needed'
    assert not ctx['motion_authorized'] and not ctx['execution_requested']


def test_only_matching_success_resolves_failure_not_unrelated_motion():
    decision = PlannerDecision()
    failed = receipt('verify_grasp',outcome='pending_review',verification_request_id='checkpoint',reason='missing')
    decision.update({'primitive_calls':[failed]})
    ctx = decision.update({'primitive_calls':[receipt('recover_grasp',outcome='success')]})
    assert ctx['unresolved_blockers'][0]['verification_request_id']=='checkpoint'
    ctx = decision.update({'primitive_calls':[receipt('verify_grasp',outcome='success')]})
    assert not ctx['unresolved_blockers'] and ctx['resolved_failures']


def test_unknown_motion_and_visual_interpretation_are_not_auto_execution():
    decision = PlannerDecision()
    ctx = decision.update({'primitive_calls':[receipt('grasp',outcome='pending_review',reason='unknown_publication')]})
    assert ctx['unresolved_blockers'] and not ctx['motion_authorized']
    ctx = decision.update({'primitive_calls':[], 'images':['new-view']})
    assert ctx['next_decision']=='interpret_requested_visual_evidence'
    assert ctx['unresolved_blockers'] and not ctx['motion_authorized']
