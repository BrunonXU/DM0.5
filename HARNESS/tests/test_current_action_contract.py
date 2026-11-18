"""Current movement, explanation and recovery share one execution contract."""
import ast
from dataclasses import replace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError, MotionParameters, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import JointProposal, OwnerBridge
from f1_harness.geometry import attached_object_sweep_clear
from f1_harness.support_motion import current_support_departure
from test_local_environment_motion import partial_setup
from test_grasp_selection_planning import setup as planning_setup
from test_candidate_program import setup as program_setup, OK, CANDIDATES
from test_astra_programs import cell
from test_full_development_skills import scene, pose
from test_support_evidence import observed, BOX, PLANE
from f1_harness.support_evidence import measure_support


@pytest.mark.parametrize('value,expected',[(0,True),(17.4,True),(18,True),(18.5,True),(18.6,False),
    (-.1,False),(float('nan'),False),(True,False)])
def test_open_fact_is_not_release_arrival_range(value,expected):
    from f1_harness.gripper_state import observed_open
    assert observed_open(value,[17.5,18.5]) is expected


def test_diagnostic_geometry_does_not_change_local_motion_admission(tmp_path):
    service,obs,intent,clock=partial_setup()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id'];engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    obs=obs.model_copy(update={'host_boot_id':engine.host_boot_id});engine.update_observation(obs)
    intent=intent.model_copy(update=dict(owner_instance_id=engine.instance,session_id=session,epoch=engine.epoch))
    def plan(i,o,full,held):
        return JointProposal(**service.dispatch('plan_skill',dict(intent=i.model_dump(),
            observation=o.model_dump(),full22=full,held16=held)))
    bridge=OwnerBridge(engine,numerical_planner=plan,result_verifier=lambda *a:None,
        clock=lambda:clock[0],feedback_age_ns=200_000_000,reference_tolerance_deg=.5,stop_gripper_delta=.1)
    bridge.geometry_provider=lambda o,full,held:service.dispatch('geometry_context',dict(
        observation=o.model_dump(),full22=full,held16=held))
    bridge.measured_feedback=lambda:dict(full22=[0.]*22,measured16=obs.state16,stamps16=(clock[0],)*16)
    try:
        plain=bridge.preview_skill(intent.model_dump())
        explained=bridge.preview_skill(intent.model_dump(),include_geometry=True)
        assert plain['planned'] and explained['planned'] and explained['missing_entities']
        assert not engine.request_publications and not explained['motion_authorized']
    finally:bridge.close()


@pytest.mark.parametrize('macro',['pick_with_candidates','move_and_reobserve'])
def test_remaining_candidate_after_owner_zero_publication_revalidation(macro,tmp_path):
    failure=dict(outcome='failure',lifecycle='completed',no_motion=True,retryable=True,
        reason='plan_revalidation_rejected:revalidation_object_or_hole_changed',request_id='first')
    backend,arm=program_setup(tmp_path,[failure]+[OK]*10)
    candidates=CANDIDATES
    if macro=='move_and_reobserve':
        from test_move_reobserve import CANDIDATES as candidates
    result=ast.literal_eval(cell(arm,f'{macro}({candidates!r})')['value'])
    assert result['outcome']=='success'
    assert backend.motions[0]['parameters']!=backend.motions[1]['parameters']


def test_world_goal_uses_new_scene_sweep_instead_of_unrelated_pose_tolerance():
    from scipy.spatial.transform import Rotation
    service,obs,intent,clock=planning_setup()
    service.task=service.task.model_copy(update={'allowed_skills':(*service.task.allowed_skills,'move_tcp')})
    service.fixture['cartesian_motion']=True
    start=service._scene(obs,[0.]*22,obs.state16).base_eef
    goal=start.copy();goal[2,3]+=.02
    intent=intent.model_copy(update=dict(skill='move_tcp',task_hash=digest(service.task),
        parameters=MotionParameters(target_frame='world',target_tcp_pose=tuple(goal.ravel()))))
    plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    clock[0]+=100_000_000
    matrix=np.array(obs.objects[0].pose).reshape(4,4)
    matrix[:3,:3]=matrix[:3,:3]@Rotation.from_euler('z',12,degrees=True).as_matrix()
    latest=obs.model_copy(update=dict(observation_id='new',scene_version='new',received_ns=clock[0],
        state_received_ns=clock[0],frames=tuple(f.model_copy(update={'received_ns':clock[0]}) for f in obs.frames),
        objects=(obs.objects[0].model_copy(update={'pose':tuple(matrix.ravel())}),*obs.objects[1:])))
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
        original_observation=obs.model_dump(),observation=latest.model_dump(),full22=[0.]*22,
        held16=list(obs.state16),after_ns=clock[0]-1))
    assert proof['geometry_checks']['swept_segments']>0
    assert proof['observation_sha256']==digest(latest)
    # Fresh obstacle geometry is still decisive; a large target at the fixed
    # endpoint cannot be authorized just because the world goal stayed put.
    service.fixture['obstacles']=(*service.fixture['obstacles'],dict(role='blocking_new_object',
        min=[-.1,-.1,-.1],max=[.1,.1,.1],pose=goal.tolist()))
    with pytest.raises(HarnessError,match='sweep'):
        service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
            original_observation=obs.model_dump(),observation=latest.model_dump(),full22=[0.]*22,
            held16=list(obs.state16),after_ns=clock[0]-1))


def test_positive_numerical_work_reused_without_reusing_authority(monkeypatch):
    service,obs,intent,clock=planning_setup();calls=[]
    original=service._solve_candidate
    def solve(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(service,'_solve_candidate',solve)
    args=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16))
    preview=service.dispatch('plan_skill',dict(args,preview=True));count=len(calls)
    clock[0]+=10_000_000
    later=obs.model_copy(update=dict(observation_id='new',scene_version='new',received_ns=clock[0],
        state_received_ns=clock[0],frames=tuple(f.model_copy(update={'received_ns':clock[0]}) for f in obs.frames)))
    request=intent.model_copy(update=dict(request_id='execute',observation_id='new',scene_version='new'))
    executed=service.dispatch('plan_skill',dict(args,intent=request.model_dump(),observation=later.model_dump()))
    assert len(calls)==count and executed['targets']==preview['targets']
    assert executed['request_id']=='execute' and executed['scene_version']=='new'
    assert executed['geometry_trace'][0]['reused_numerical_candidate']['fresh_revalidation_required']
    other=request.model_copy(update={'epoch':request.epoch+1})
    service.dispatch('plan_skill',dict(args,intent=other.model_dump(),observation=later.model_dump()))
    assert len(calls)>count


@pytest.mark.parametrize('held,stationary',[(False,True),(None,False)])
def test_current_model_overlap_only_allows_departure(held,stationary):
    measured=pose(z=-.001212)
    obs=observed(pose=measured,held=held,stationary=stationary)
    evidence=measure_support(obs,object_id='block',object_local_box=BOX,support_surface=PLANE,
        contact_tolerance_m=.001)
    value=replace(scene(),base_object=measured,object_local_box=BOX,object_held=held,eef_object=None,
        target_object_id='block',support_measurement=evidence,
        obstacles=(dict(type='halfspace',role='table',point=[0,0,0],normal=[0,0,1]),))
    current=current_support_departure(value,observed_open_gripper=True)
    assert not evidence['support_observed'] and evidence['relation']=='model_conflict'
    assert np.array_equal(current.base_object,value.base_object)
    def clear(a,b,obstacles=current.obstacles):
        return attached_object_sweep_clear(pose(z=a),pose(z=b),np.eye(4),BOX,obstacles,
            margin_m=.001,support_contact_tolerance_m=.001)
    assert clear(-.001212,.02)
    assert not clear(-.001212,-.0013)
    assert not clear(-.005,.02)
    assert not clear(-.001212,.02,(*current.obstacles,dict(type='halfspace',role='ceiling',
        normal=[0,0,-1],point=[0,0,.05])))
    assert 'support_departure_reference' not in current_support_departure(value).obstacles[0]
