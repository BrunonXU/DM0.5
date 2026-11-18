from copy import deepcopy
import json
from types import SimpleNamespace

from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.evidence import EvidenceStore
from f1_harness.planner import ResponsesPlanner
from f1_harness.planner_progress import PlannerProgress


def cell(*calls, **kwargs):
    return dict(primitive_calls=list(calls), **kwargs)


def test_frame_ids_prompts_and_claims_cannot_extend_idle_budget():
    guard = PlannerProgress()
    for i in range(3):
        receipt = guard.update(cell(dict(tool='locate_target', ok=True, accepted=True,
            search_only=True, motion_evidence=False), moved=True,
            stdout='success: grasped and placed', state={'observation_id': str(i)}))
        assert receipt['exhausted'] is (i == 2)


def test_failure_retains_owner_checkpoint_and_never_counts_rpc_success():
    guard = PlannerProgress()
    failure = dict(tool='execute_skill', kwargs={'name': 'verify_grasp'}, ok=True,
        outcome='pending_review', reason='grasp_held_evidence_missing',
        verification_request_id='owner-checkpoint', cancel_stage='measured_stationary')
    for _ in range(3): receipt = guard.update(cell(failure))
    assert receipt['exhausted']
    assert receipt['last_failure']['verification_request_id'] == 'owner-checkpoint'
    assert receipt['last_failure']['skill'] == 'verify_grasp'
    assert not receipt['motion_authorized']


def test_readonly_recheck_projects_new_failure_alongside_original_receipt(tmp_path):
    class Backend:
        def execute_tool(self,name,args):
            assert name=='execute_skill' and args['verification_request_id']=='original'
            return dict(request_id='original',outcome='pending_review',reason='post_motion_verification_unavailable:old',
                verification_request_id='original',observation_retry_reason='aperture_measurement_required:hole:anchor_identity_changed',
                continuation_submitted=False,no_motion=False)
        def env_steps(self):return 0
        def solved(self):return False
    arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    result=arm.execute_tool('python',dict(code='checked = robo.recheck("transfer_above_hole", "original")')).result
    assert not result.get('exception')
    blocker=PlannerProgress().update(result)['last_failure']
    assert blocker['request_id']=='original' and blocker['no_motion'] is False
    assert blocker['reason']=='post_motion_verification_unavailable:old'
    assert blocker['observation_retry_reason'].endswith('anchor_identity_changed')
    assert blocker['continuation_submitted'] is False


def test_same_semantic_evidence_does_not_reset_after_stage_change():
    guard = PlannerProgress()
    observed = dict(tool='observe', ok=True, scene_entities=[dict(object_id='square-1', visible=True, has_pose=True)])
    assert guard.update(cell(observed))['idle_turns'] == 0
    assert guard.update(cell(observed))['idle_turns'] == 1
    assert guard.update(cell())['idle_turns'] == 2
    assert guard.update(cell(observed))['exhausted']
    done = dict(tool='execute_skill', ok=True, kwargs={'name': 'approach_grasp'}, outcome='success')
    assert not guard.update(cell(done))['exhausted']
    assert guard.update(cell(observed))['idle_turns'] == 1
    assert guard.update(cell(done))['idle_turns'] == 2


def test_candidate_search_is_bounded_and_duplicate_previews_do_not_reset():
    guard = PlannerProgress()
    for i in range(5):
        preview = dict(tool='preview_skill', ok=True, planned=True, current_scope=True,
            kwargs={'name': 'approach_grasp', 'parameters': {'pitch_offset_deg': i}})
        receipt = guard.update(cell(preview))
        assert receipt['exhausted'] is (i == 4)
    guard = PlannerProgress()
    assert guard.update(cell(preview))['idle_turns'] == 0
    assert guard.update(cell(preview))['idle_turns'] == 1


def test_actual_pyrua_observation_ledger_contains_semantic_evidence(tmp_path):
    class Backend:
        def execute_tool(self, name, args):
            assert name == 'observe'
            return dict(observation=dict(observation_id='fresh', objects=[dict(
                object_id='square-1', visible=True, pose=list(range(16)), held=None)]))
        def env_steps(self): return 0
        def solved(self): return False
    arm = ArmToolkit(F1Robot(Backend()), arm='cells', workspace=tmp_path, images=False)
    result = arm.execute_tool('python', {'code': 'obs = robo.observe()'}).result
    assert not result.get('exception')
    assert result['primitive_calls'][0]['scene_entities'][0]['has_pose']
    assert PlannerProgress().update(result)['progress']


def test_inspect_plan_recovered_geometry_resets_idle_once_even_if_path_rejected(tmp_path):
    class Backend:
        def execute_tool(self,name,args):
            assert name=='inspect_plan'
            return dict(planned=False,current_scope=True,geometry_status='complete',
                geometry=dict(entities=[dict(object_id='square-1',visible=True,has_pose=True),
                    dict(object_id='square-hole-1',visible=True,has_pose=True)]),
                planning_diagnostic=dict(code='registered_candidates_exhausted'))
        def env_steps(self):return 0
        def solved(self):return False
    arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    guard=PlannerProgress()
    assert guard.update(cell())['idle_turns']==1
    result=arm.execute_tool('python',dict(code='p = robo.inspect_plan()')).result
    assert not result.get('exception')
    assert guard.update(result)['idle_turns']==0
    assert guard.last_failure['reason']=='registered_candidates_exhausted'
    # Querying the same evidence again cannot repeatedly buy more model turns.
    assert guard.update(result)['idle_turns']==1
    assert guard.update(result)['idle_turns']==2
    assert guard.update(result)['exhausted']


def test_complete_status_without_measured_entities_does_not_invent_progress():
    robot=F1Robot.__new__(F1Robot)
    summary=robot._summary('inspect_plan',dict(planned=False,geometry_status='complete',
        geometry=dict(entities=[dict(object_id='square-1',visible=False,has_pose=False)])))
    guard=PlannerProgress()
    for _ in range(3):result=guard.update(cell(dict(tool='inspect_plan',ok=True,**summary)))
    assert result['exhausted'] and not result['progress']


def test_actual_planner_stops_model_loop_and_saves_budget_receipt(tmp_path):
    class Arm:
        def get_tools_spec(self):
            return [dict(name=n, description=n, input_schema={}) for n in ('python', 'finish')]
        def execute_tool(self, name, args):
            return SimpleNamespace(result=cell(stdout='still thinking'), images=[])
    planner = object.__new__(ResponsesPlanner)
    planner.store = EvidenceStore(tmp_path)
    requests = []
    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        return dict(output=[dict(type='function_call', name='python',
            call_id=str(len(requests)), arguments=json.dumps({'code': 'print("still thinking")'}))])
    planner.request = request
    result = planner.run(Arm(), task_text='pick and place')
    assert len(requests) == 3
    assert result['outcome'] == 'pending_review'
    assert result['next_model_call'] is False
    feedback = [json.loads(item['output']) for item in requests[-1]['input'] if item.get('type') == 'function_call_output']
    assert feedback[-1]['planner_progress']['remaining_idle_turns'] == 1


def test_complete_skill_sequence_in_single_cell_is_not_interrupted():
    guard = PlannerProgress()
    names = ('approach_grasp', 'grasp', 'verify_grasp', 'transfer_above_hole',
             'inspect_in_hand', 'align', 'insert', 'release', 'verify_placed', 'withdraw_reset')
    result = guard.update(cell(*(dict(tool='execute_skill', ok=True,
        kwargs={'name': name}, outcome='success') for name in names)))
    assert not result['exhausted']
    assert len(result['progress']) == 10


def test_failed_strategy_history_is_bounded_and_does_not_reset_idle_budget():
    guard=PlannerProgress()
    call=dict(tool='execute_skill',ok=True,no_motion=True,outcome='pending_review',
        kwargs=dict(name='approach_grasp',parameters={'pitch_offset_deg':10.}),
        planning_diagnostic=dict(code='registered_candidates_exhausted',metrics={}))
    for i in range(3):
        call['request_id']=str(i)
        result=guard.update(cell(call))
    assert result['exhausted']
    assert result['recent_failed_strategies'][0]['attempts']==3
    for pitch in (20.,30.,40.):
        call['kwargs']['parameters']['pitch_offset_deg']=pitch
        result=guard.update(cell(call))
    assert len(result['recent_failed_strategies'])==3
    assert result['idle_turns']==6
    result=guard.update(cell(dict(tool='execute_skill',ok=True,outcome='success',kwargs={'name':'approach_grasp'})))
    assert not result['recent_failed_strategies'] and not result['exhausted']


def test_visual_recovery_gets_one_view_not_unbounded_new_image_resets():
    guard = PlannerProgress()
    guard.update(cell())
    guard.update(cell())
    assert guard.update(cell(images=['right.png']))['idle_turns'] == 0
    for i in range(3):
        receipt = guard.update(cell(images=[str(i)+'.png']))
    assert receipt['exhausted']


def test_web_result_explains_budget_and_underlying_failure(tmp_path):
    from f1_harness.web_execution import WorkspaceSupervisor
    (tmp_path/'planner').mkdir()
    failure = dict(skill='verify_grasp', reason='grasp_held_evidence_missing')
    result = dict(planner_declaration=dict(reason='planner_no_progress_budget_exhausted', last_failure=failure),
        measured=dict(outcome='pending_review'), attention=dict(skill='verify_grasp',
            reason='planner_no_progress_budget_exhausted', last_failure=failure))
    (tmp_path/'planner/result.json').write_text(json.dumps(result))
    kind, message, _ = WorkspaceSupervisor._planner_outcome(tmp_path, 2)
    assert kind == 'review' and 'grasp_held_evidence_missing' in message
    assert 'verify_grasp' in WorkspaceSupervisor._planner_failure(tmp_path)


def test_failed_continuous_strategy_preserves_pose_and_direction_for_next_turn():
    parameters=dict(grasp_tcp_pose=[1.,0.,0.,.02,0.,1.,0.,-.03,0.,0.,1.,.06,0.,0.,0.,1.],
                    approach_direction_object=[0.,-1.,0.],approach_distance_m=.06)
    result=PlannerProgress().update(cell(dict(tool='preview_skill',ok=True,planned=False,
        kwargs=dict(name='approach_grasp',parameters=parameters),
        planning_diagnostic=dict(code='scene_sweep_blocked'))))
    assert result['recent_failed_strategies'][0]['parameters']==parameters
