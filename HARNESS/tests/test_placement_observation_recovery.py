"""PyRUA placement regression for the V42 zero-output alignment handoff."""
import ast
from copy import deepcopy

import pytest

from f1_harness.contracts import SKILLS, Task
from f1_harness.programs import single_block_program
from f1_harness.remote import OBSERVATION_READINESS_S
from test_astra_programs import cell
from test_candidate_program import OK, setup


ALIGN_REJECT = dict(lifecycle='rejected', outcome='pending_review', no_motion=True,
    reason='observation_readiness_timeout:observation_unavailable:RpcError:observe_with_status: feedback_expired:right',
    retryable=False, request_id='unsubmitted-align')
VIEW = dict(target_frame='world', target_tcp_pose=[1,0,0,.4,0,1,0,.2,0,0,1,.8,0,0,0,1],
            source_observation_id='registered-held-source')
MISSING = dict(role='aperture', object_id='square-hole-1', reason='fixture_aperture_not_detected_in_search_region')


def inspection(stamp, *, measured=False, candidate=True, held=True, visible=None):
    entities = [dict(role='target_object', held=held, in_hand_transform_available=held),
                dict(role='aperture', object_id='square-hole-1', has_pose=measured,
                     visible=measured if visible is None else visible)]
    geometry = dict(observation_id=str(stamp), measurement_received_ns=stamp, entities=entities,
                    missing_entities=[] if measured else [MISSING])
    return dict(planned=measured, current_scope=True, geometry=geometry,
                left_aperture_observation={'observation_move':deepcopy(VIEW)} if candidate else {})


@pytest.mark.parametrize('entry', ['pick_and_place()', 'place_held_block()', 'baseline', 'isolated'])
def test_single_cell_recovers_changed_aperture_and_completes_only_remaining_stages(tmp_path, entry):
    pickup = [] if entry in ('place_held_block()', 'isolated') else ['approach_grasp','grasp','verify_grasp']
    before = pickup + ['transfer_above_hole','inspect_in_hand']
    suffix = ['move_tcp','reobserve','align','insert','release','withdraw_reset','verify_placed']
    backend,arm = setup(tmp_path, [OK]*len(before)+[ALIGN_REJECT]+[OK]*len(suffix),
        inspections=[inspection(70),inspection(72,measured=True)], isolated=entry=='isolated')
    try:
        if entry == 'baseline':
            task = Task(task_id='t', object_id='square', hole_id='hole', calibration_id='c', allowed_skills=SKILLS)
            code = single_block_program(task)+'\nresult'
        else:
            code = 'place_held_block()' if entry=='isolated' else entry
        result = ast.literal_eval(cell(arm,code)['value'])
        assert result['outcome']=='success'
        assert [r['name'] for r in backend.motions] == before+['align']+suffix
        assert [r['name'] for op,r in backend.calls if op=='inspect_plan'] == ['align','align']
        parameters=backend.motions[len(before)+1]['parameters']
        assert parameters.get('target_frame','world')=='world' and 'source_observation_id' not in parameters
        assert list(parameters['target_tcp_pose'])==VIEW['target_tcp_pose']
    finally:
        if entry=='isolated': arm.close()


@pytest.mark.parametrize('after', [inspection(72), inspection(70,measured=True), inspection(72,measured=True,visible=False),
    inspection(72,measured=True,held=False),dict(inspection(72,measured=True),current_scope=False)])
def test_missing_stale_unheld_or_wrong_scope_never_opens_and_hands_off_one_numeric_issue(tmp_path, after):
    backend,arm=setup(tmp_path,[ALIGN_REJECT,OK,OK],inspections=[inspection(70),after])
    reply=cell(arm,'result=place_held_block(start="align")\nresult')
    result=ast.literal_eval(reply['value'])
    assert [r['name'] for r in backend.motions]==['align','move_tcp','reobserve']
    assert result['reason']=='alignment_observation_unresolved' and result['needs_new_strategy']
    assert result['program_stage']=='align' and result['no_motion'] is False
    assert result['placement_recovery']['after']['measurement_received_ns']==after['geometry']['measurement_received_ns']
    assert result['placement_recovery']['view']['local_motion_completed'] and reply['stdout'].count('placement_handoff')==1


def test_no_repeated_view_after_rejection_and_fresh_alignment_joins_same_placement_suffix(tmp_path):
    rejected=dict(current_scope=True,planned=False,planning_diagnostic={'code':'scene_sweep_blocked'})
    backend,arm=setup(tmp_path,[ALIGN_REJECT,ALIGN_REJECT]+[OK]*5,previews=[rejected],
                      inspections=[inspection(70),inspection(71),inspection(74,measured=True)])
    for _ in range(2):
        result=ast.literal_eval(cell(arm,'result=place_held_block(start="align")\nresult')['value'])
        assert result['needs_new_strategy'] and result['no_motion']
        assert result['placement_recovery']['view_source_tried']==VIEW['source_observation_id']
    result=ast.literal_eval(cell(arm,'resume_pick(result)')['value'])
    assert result['outcome']=='success'
    assert [r['name'] for r in backend.motions]==['align','align','align','insert','release','withdraw_reset','verify_placed']
    assert sum(op=='preview_skill' for op,_ in backend.calls)==1


@pytest.mark.parametrize('changes',[{'no_motion':False},{'lifecycle':'running'},{'outcome':'interrupted'},
    {'outcome':'failure','reason':'motion_stalled'},{'reason':'numerical_plan_rejected:scene_sweep_blocked'}])
def test_other_align_failures_never_enter_insertion_recovery_or_start_an_observation_move(tmp_path,changes):
    failure=dict(ALIGN_REJECT,**changes)
    backend,arm=setup(tmp_path,[failure])
    result=ast.literal_eval(cell(arm,'place_held_block(start="align")')['value'])
    assert result==dict(failure,program_stage='align')
    assert [c for c,_ in backend.calls]==['execute_skill']


def test_unknown_view_publication_preserves_original_motion_receipt(tmp_path):
    pending=dict(outcome='pending_review',reason='unknown_publication',request_id='view',no_motion=False)
    backend,arm=setup(tmp_path,[ALIGN_REJECT,pending],inspections=[inspection(70)])
    result=ast.literal_eval(cell(arm,'place_held_block(start="align")')['value'])
    assert result['request_id']=='view' and result['program_stage']=='move_tcp'
    assert result['resume_placement_from']=='align' and not result['no_motion']
    assert [r['name'] for r in backend.motions]==['align','move_tcp']


def test_recovered_bucket_reruns_previously_unplanned_view_and_rejoins_after_align(tmp_path):
    before,restored=inspection(70),inspection(74)
    before['geometry']['fixture_collision_geometry_available']=False
    restored['geometry']['fixture_collision_geometry_available']=True
    unavailable=dict(planned=False,current_scope=True,planning_diagnostic={'code':'missing_geometry_entities'})
    backend,arm=setup(tmp_path,[ALIGN_REJECT]+[OK]*7,previews=[unavailable],
        inspections=[before,dict(preview=unavailable),restored,inspection(75,measured=True)])
    first=ast.literal_eval(cell(arm,'result=place_held_block(start="align")\nresult')['value'])
    assert first['no_motion'] is True and 'fixture collision reference' in first['placement_recovery']['next']
    assert ast.literal_eval(cell(arm,'resume_pick(result)')['value'])['outcome']=='success'
    assert [a['name'] for a in backend.motions]==['align','move_tcp','reobserve','align','insert','release','withdraw_reset','verify_placed']


@pytest.mark.parametrize('held', [True,False])
def test_fast_align_handoff_requires_fresh_held_observation_and_never_submits(monkeypatch,held):
    from test_plan_revalidation import observation
    from test_remote_readiness import backend
    remote=backend(monkeypatch,[True])
    remote.task=remote.task.model_copy(update={'allowed_skills':SKILLS,'admission_profile':'development_coarse'})
    original=remote.call
    def call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status': value.update(mode='hardware',recording_identity={'host_boot_id':'boot'})
        if method=='observe':
            obs=observation(value['received_ns'])
            obj=obs.objects[0].model_copy(update=dict(visible=True,held=held,eef_object_pose=obs.objects[0].pose if held else None))
            return obs.model_copy(update=dict(calibration_id='cal',objects=(obj,))).model_dump()
        return value
    remote.call=call
    result=remote.execute_tool('execute_skill',{'name':'align'})
    assert result['no_motion'] is True and not remote.submissions
    if held:
        assert result['reason'].startswith('observation_viewpoint_required:aperture_measurement_required:')
        assert remote.clock.value==0 and remote.calls.count('observe')==1
    else:
        assert result['reason'].startswith('observation_readiness_timeout:')
        assert remote.clock.value==pytest.approx(OBSERVATION_READINESS_S)

def test_reobserved_direct_hole_drives_next_align_without_overwriting_request_snapshot(tmp_path):
    measured=inspection(72,measured=True)['geometry']
    receipt=dict(OK,alignment_observation=dict(hole_id='square-hole-1',observation_id=measured['observation_id'],
                                               received_ns=measured['measurement_received_ns']))
    backend,arm=setup(tmp_path,[ALIGN_REJECT,OK,receipt]+[OK]*5,inspections=[inspection(70)])
    result=ast.literal_eval(cell(arm,'place_held_block(start="align")')['value'])
    assert result['outcome']=='success'
    assert [m['name'] for m in backend.motions]==['align','move_tcp','reobserve','align','insert','release','withdraw_reset','verify_placed']
    assert sum(op=='inspect_plan' for op,_ in backend.calls)==1
