import numpy as np
import pytest
from f1_harness.contracts import Task, SKILLS, SkillIntent, SceneObject, digest, HarnessError
from f1_harness.skill_services import VerifyService
from f1_harness.revalidation import proof_observation
from f1_harness.owner_bridge import JointProposal
from test_plan_revalidation import service, observation, Numerics
from test_return_route import fixture


@pytest.fixture
def return_case():
    cfg=fixture();clock=[1_000_000_000]
    task=Task(task_id='return',object_id='block',hole_id='hole',calibration_id='coarse',
              admission_profile='development_coarse',allowed_skills=SKILLS)
    serv=service(clock,task)
    class ReturnNumerics(Numerics):
        def call(self,method,**kw):
            if method=='forward_pose':
                k=kw['held16'][7]
                return (cfg['home'] if k==100 else self.poses.get(k,cfg['current'])).tolist()
            return super().call(method,**kw)
    serv.numerics=ReturnNumerics()
    serv.fixture.update(return_route=dict(schema='f1-empty-tool-return-v1',home_right_deg=[100,0,0,0,0,0,0],
        right_axis_ik_world=cfg['right_axis'],source='fixture',source_sha256='a'*64),
        fixture_reference_hole=cfg['hole'].tolist(),tool_local_boxes=cfg['tool_boxes'],
        obstacles=[dict(o,pose=np.eye(4).tolist()) for o in cfg['obstacles']],
        planning_margin_m=.02,tool_padding_policy='advisory',release_opening=18,
        eef_tip_reference=np.eye(4).tolist(),attached_radius_m=.05)
    def obs(now,name):
        original=observation(now,name=name)
        return original.model_copy(update=dict(geometry_scope='coarse_fixture_return',geometry_camera='head',
            frames=tuple(f for f in original.frames if f.camera=='head'),
            pairings=tuple(p for p in original.pairings if p.camera=='head'),
            objects=(SceneObject(object_id='hole',shape='square',frame_id='native',
                pose=tuple(cfg['hole'].ravel()),evidence_ids=('hole-evidence',)),)))
    original=obs(clock[0],'original')
    intent=SkillIntent(request_id='return',session_id='s',owner_instance_id='o',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=original.observation_id,scene_version=original.scene_version,
        calibration_id=task.calibration_id,skill='withdraw_reset')
    return serv,clock,original,intent,obs


def test_return_plan_revalidation_arrival_and_fixture_only_verification(return_case):
    serv,clock,original,intent,observe=return_case
    proposal=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        held16=original.state16,full22=[0]*22))
    assert [r['semantic_stage'] for r in proposal['geometry_trace']][:2]==['leave_hole','right_of_bucket']
    assert proposal['geometry_trace'][-1]['semantic_stage']=='ready'
    assert all(q[15]==18 for q in proposal['targets'])
    after=clock[0];clock[0]+=100_000_000;latest=observe(clock[0],'latest')
    proof=serv.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=proposal,
        original_observation=original.model_dump(),observation=latest.model_dump(),full22=[0]*22,
        held16=original.state16,after_ns=after))
    assert proof_observation(proof,proposal=JointProposal(**proposal),original=original,intent=intent,
        task=serv.task,now_ns=clock[0],max_age_ns=500_000_000)==latest
    # The normal owner arrival gate uses original frozen candidate and measured FK.
    serv.fixture['free_space_tracking_tolerance_deg']=1.
    proposal['free_space_tracking_tolerance_deg']=1.
    result=serv.check_waypoint(intent=intent.model_dump(),proposal=proposal,observation=original.model_dump(),
        index=len(proposal['targets'])-1,full22=[0]*22,measured16=proposal['targets'][-1])
    assert result['accepted']
    v=VerifyService(serv.task,clock=lambda:clock[0]);v.dispatch('update_scene',{'observation':latest.model_dump()})
    result=v.dispatch('verify_skill',dict(intent=intent.model_dump(),after_ns=after))
    assert result['outcome']=='success' and result['evidence_ids']==('hole-evidence',)


@pytest.mark.parametrize('change',['missing_home','closed','wrong_scope','neighbour'])
def test_return_does_not_bypass_registration_empty_tool_or_collision(return_case,change):
    serv,clock,obs,intent,_=return_case
    if change=='missing_home':serv.fixture.pop('return_route')
    elif change=='closed':obs=obs.model_copy(update={'state16':obs.state16[:15]+(100.,)})
    elif change=='wrong_scope':obs=obs.model_copy(update={'geometry_scope':'coarse_static_task'})
    else:serv.fixture['obstacles'].append(dict(role='neighbour',min=[-.05,-.4,.51],max=[.05,-.15,.65]))
    with pytest.raises((HarnessError,ValueError)):
        serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            held16=obs.state16,full22=[0]*22))


@pytest.mark.parametrize('error',['bounded_IK_did_not_reach_goal','return_route_sweep_blocked:return_bend'])
def test_return_rejected_bend_tries_original_route_with_same_start(return_case,monkeypatch,error):
    serv,clock,obs,intent,_=return_case
    serv.fixture['candidate_order']={'withdraw_reset':[1,0,2,3]}
    original=serv._solve_candidate;attempts=[]
    def solve(intent,scene,held,full,variant,*,deadline_ns=None): 
        attempts.append((variant,tuple(held)))
        if variant==1:raise HarnessError(error)
        return original(intent,scene,held,full,variant)
    monkeypatch.setattr(serv,'_solve_candidate',solve)
    result=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        held16=obs.state16,full22=[0]*22))
    assert result['candidate_variant']==0
    assert attempts==[(1,obs.state16),(0,obs.state16)]


def test_return_bend_is_bound_to_fresh_revalidation_and_arrival(return_case):
    serv,clock,obs,intent,observe=return_case
    serv.fixture['candidate_order']={'withdraw_reset':[1,0,2,3]}
    serv.fixture['execution_mode']='timed_reference';serv.fixture['reference_velocity_deg_s']=20.
    proposal=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        held16=obs.state16,full22=[0]*22))
    assert proposal['candidate_variant']==1
    assert len(proposal['trajectory']['stops'])==4
    after=clock[0];clock[0]+=100_000_000;latest=observe(clock[0],'latest')
    kwargs=dict(intent=intent.model_dump(),proposal=proposal,original_observation=obs.model_dump(),
        observation=latest.model_dump(),full22=[0]*22,held16=obs.state16,after_ns=after)
    proof=serv.dispatch('revalidate_plan',kwargs)
    assert proof_observation(proof,proposal=JointProposal(**proposal),original=obs,intent=intent,
        task=serv.task,now_ns=clock[0],max_age_ns=500_000_000)==latest
    for entry in proposal['geometry_trace']:
        index=entry['end_index']-1
        result=serv.check_waypoint(intent=intent.model_dump(),proposal=proposal,observation=obs.model_dump(),
            index=index,full22=[0]*22,measured16=proposal['targets'][index])
        assert result['accepted']
    proposal['candidate_variant']=2
    with pytest.raises(HarnessError,match='revalidation_stage_binding_mismatch'):
        serv.dispatch('revalidate_plan',kwargs)


def test_return_candidate_fallback_does_not_hide_identity_errors(return_case,monkeypatch):
    serv,clock,obs,intent,_=return_case
    def solve(*args,deadline_ns=None): raise HarnessError('model_binding_mismatch')
    monkeypatch.setattr(serv,'_solve_candidate',solve)
    with pytest.raises(HarnessError,match='model_binding_mismatch'):
        serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            held16=obs.state16,full22=[0]*22))
