"""The next model turn sees measured outcomes even for assignment-only cells."""
from copy import deepcopy
import json

from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.planner import ResponsesPlanner


class Backend:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute_tool(self, name, arguments):
        if name == 'observe':
            return dict(observation=dict(observation_id='fresh', objects=[]),
                        status=dict(phase='authorized', authorized=True))
        self.calls.append((name, arguments))
        return deepcopy(self.result)

    def env_steps(self): return len(self.calls)
    def solved(self): return False
    def cancel(self): pass


def result(**changes):
    value = dict(request_id='request', lifecycle='completed', outcome='pending_review',
        reason='numerical_plan_rejected:registered_candidates_exhausted', no_motion=True,
        retryable=False, cancel_stage='none',
        planning_diagnostic=dict(code='registered_candidates_exhausted', candidates=[
            dict(variant=0, code='scene_sweep_blocked', metrics=dict(
                stage_index=0, segment_index=2, obstacle_role='bucket',
                obstacle_name='wall_x_low', collision_body='tool'))]))
    value.update(changes)
    return value


def test_assignment_only_cell_preserves_outcome_and_bounded_collision_details(tmp_path):
    backend = Backend(result())
    arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=tmp_path, images=False)
    feedback = arm.execute_tool('python', {'code': 'r = robo.skill("approach_grasp")'}).result
    assert not feedback.get('exception') and 'value' not in feedback and feedback['stdout'] == ''
    call = next(c for c in feedback['primitive_calls'] if c['tool'] == 'execute_skill')
    assert call['ok'] is True  # RPC returned; the distinct outcome is not success.
    assert call['outcome'] == 'pending_review' and call['no_motion']
    assert call['reason'] == backend.result['reason']
    assert call['planning_diagnostic']['candidates'][0]['metrics']['obstacle_name'] == 'wall_x_low'


def test_actual_planner_function_output_contains_recovery_checkpoint(tmp_path):
    backend = Backend(result(outcome='failure', reason='motion_stalled', no_motion=False,
        cancel_stage='measured_stationary', resume_request_id='owner-issued-checkpoint',
        planning_diagnostic=None))
    arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=tmp_path, images=False)
    planner = object.__new__(ResponsesPlanner)  # No client, credentials, network or robot.
    planner.store = type('Store', (), {'event': lambda *args, **kwargs: None})()
    requests = []
    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            name, arguments = 'python', {'code': 'r = robo.skill("approach_grasp")'}
        else:
            name, arguments = 'finish', {'status': 'failure', 'summary': 'Motion stalled; checkpoint retained.'}
        return {'output': [dict(type='function_call', call_id=str(len(requests)),
                                name=name, arguments=json.dumps(arguments))]}
    planner.request = request
    planner.run(arm, task_text='Registered square task', max_turns=2)
    output = next(item for item in requests[1]['input'] if item.get('type') == 'function_call_output')
    payload = json.loads(output['output'])
    call = next(c for c in payload['primitive_calls'] if c['tool'] == 'execute_skill')
    assert call['outcome'] == 'failure' and call['reason'] == 'motion_stalled'
    assert call['resume_request_id'] == 'owner-issued-checkpoint'
    assert call['cancel_stage'] == 'measured_stationary' and not call['no_motion']
    assert backend.calls == [('execute_skill', {'name': 'approach_grasp'})]


def test_feedback_excludes_raw_images_and_limits_diagnostic_arrays(tmp_path):
    payload = result(verification_request_id='verify-checkpoint', images=['private-image'] * 10000)
    payload['planning_diagnostic']['raw_image'] = 'private-image' * 10000
    candidate = payload['planning_diagnostic']['candidates'][0]
    candidate['metrics'].update(raw_image='private-image' * 10000, joint_path=list(range(10000)),
                                collision_pair=['link'] * 10000)
    payload['planning_diagnostic']['candidates'] *= 1000
    arm = ArmToolkit(F1Robot(Backend(payload)), arm='cells', workspace=tmp_path, images=False)
    feedback = arm.execute_tool('python', {'code': 'r = robo.skill("approach_grasp")'}).result
    call = next(c for c in feedback['primitive_calls'] if c['tool'] == 'execute_skill')
    encoded = json.dumps(call)
    assert 'private-image' not in encoded and 'joint_path' not in encoded
    assert len(call['planning_diagnostic']['candidates']) == 4
    assert len(call['planning_diagnostic']['candidates'][0]['metrics']['collision_pair']) == 8
    assert len(json.dumps(call['planning_diagnostic'])) < 4096
    assert call['verification_request_id'] == 'verify-checkpoint'


def test_preview_ledger_also_bounds_diagnostics_and_preserves_missing_entities(tmp_path):
    payload=result()
    payload.update(planned=False,current_scope=True,missing_entities=[dict(object_id='hole',role='aperture')])
    payload['planning_diagnostic']['candidates'] *= 1000
    arm=ArmToolkit(F1Robot(Backend(payload)),arm='cells',workspace=tmp_path,images=False)
    feedback=arm.execute_tool('python',dict(code='r = robo.inspect_plan()')).result
    call=feedback['primitive_calls'][0]
    assert call['planned'] is False and call['missing_entities'][0]['object_id']=='hole'
    assert len(call['planning_diagnostic']['candidates'])==4
    assert len(json.dumps(call))<5000


def test_real_tool_ledger_tells_model_repeated_strategy_count_and_stops_loop(tmp_path):
    payload=result()
    payload['planning_diagnostic']['metrics']=dict(reused_failure=True,
        previous_request_id='previous',recovery_hint='change_candidate_or_observe_changed_geometry')
    arm=ArmToolkit(F1Robot(Backend(payload)),arm='cells',workspace=tmp_path,images=False)
    planner=object.__new__(ResponsesPlanner)
    from f1_harness.evidence import EvidenceStore
    planner.store=EvidenceStore(tmp_path/'evidence');requests=[]
    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        return dict(output=[dict(type='function_call',call_id=str(len(requests)),name='python',
            arguments=json.dumps({'code':'r = robo.skill("approach_grasp", pitch_offset_deg=10)'}))])
    planner.request=request
    outcome=planner.run(arm,task_text='pick and place')
    assert len(requests)==3 and outcome['reason']=='planner_no_progress_budget_exhausted'
    outputs=[json.loads(v['output']) for v in requests[-1]['input'] if v.get('type')=='function_call_output']
    feedback=outputs[-1]
    assert feedback['planner_progress']['recent_failed_strategies'][0]['attempts']==2
    assert feedback['planner_progress']['recent_failed_strategies'][0]['parameters']=={'pitch_offset_deg':10.}
    assert feedback['primitive_calls'][0]['planning_diagnostic']['metrics']['reused_failure']
