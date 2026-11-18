import numpy as np
from f1_harness.candidate_ranking import rank_variants
from f1_harness.skills import propose
from test_full_development_skills import scene


def test_pregrasp_requires_full_target_face_inside_calibrated_wrist_image():
    from f1_harness.candidate_ranking import pregrasp_wrist_view
    from test_full_development_skills import pose
    geometry=scene();endpoint=pose(.2,0,.38)
    down=np.diag([1.,-1.,-1.,1.])
    camera=dict(eef_camera=down.tolist(),k_rgb=[[300,0,320],[0,300,240],[0,0,1]],image_size=[640,480])
    result=pregrasp_wrist_view(geometry,endpoint,camera)
    assert result['inside_image'] and not result['occlusion_verified']
    assert not pregrasp_wrist_view(geometry,endpoint,camera|dict(eef_camera=np.eye(4).tolist()))['inside_image']
    assert not pregrasp_wrist_view(geometry,pose(.5,0,.38),camera)['inside_image']


def test_ranking_keeps_registered_goals_and_reports_soft_view_quality():
    geometry=scene();variants=list(range(4))
    target=propose('approach_grasp',geometry,variant=2)['stages'][-1]['goals'][-1]
    prior={'eef_poses':{'approach_grasp':target}}
    camera=dict(eef_camera=np.eye(4).tolist(),k_rgb=[[300,0,320],[0,300,240],[0,0,1]],image_size=[640,480])
    order,rows=rank_variants('approach_grasp',geometry,variants,prior,camera=camera)
    assert set(order)==set(variants) and len(rows)==4
    assert all('visibility_is_occlusion_unverified' in row for row in rows if row['score'] is not None)
    assert [r['score'] for r in rows if r['score'] is not None]==sorted(r['score'] for r in rows if r['score'] is not None)
    # It changes only order: no projection or old demonstration creates a new goal.
    assert np.allclose(target,propose('approach_grasp',geometry,variant=2)['stages'][-1]['goals'][-1])
    assert rank_variants('approach_grasp',geometry,[0,2],None)==([0,2],[])


def test_rejected_high_variant_does_not_discard_lower_registered_options(monkeypatch):
    import f1_harness.skill_services as module
    from f1_harness.contracts import Task,SkillIntent,digest
    from test_plan_revalidation import service,observation
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    owner=service([1_000_000_000],task);obs=observation(1_000_000_000)
    calls=[]
    monkeypatch.setattr(module.PlanService,'_solve_candidate',lambda self,i,s,h,f,v,*,deadline_ns=None:calls.append(v) or {'geometry_trace':[]})
    intent=SkillIntent(request_id='req',session_id='s',owner_instance_id='o',task_id='task',task_hash=digest(task),
        epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id='coarse',skill='approach_grasp',excluded_candidates=(3,))
    owner.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0]*22,held16=list(obs.state16)))
    assert calls==[0]


def test_registered_order_prefers_branch_but_cannot_reenable_excluded_candidate(monkeypatch):
    import f1_harness.skill_services as module
    from f1_harness.contracts import Task,SkillIntent,digest,HarnessError
    from test_plan_revalidation import service,observation
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    owner=service([1_000_000_000],task);obs=observation(1_000_000_000)
    owner.fixture['candidate_order']={'approach_grasp':[2,0,1,3]}
    calls=[]
    def solve(self,i,s,h,f,v,*,deadline_ns=None): 
        calls.append(v)
        if v==2:raise HarnessError('scene_sweep_blocked')
        return {'geometry_trace':[]}
    monkeypatch.setattr(module.PlanService,'_solve_candidate',solve)
    intent=SkillIntent(request_id='req',session_id='s',owner_instance_id='o',task_id='task',task_hash=digest(task),
        epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id='coarse',skill='approach_grasp',excluded_candidates=(0,))
    kwargs=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0]*22,held16=list(obs.state16))
    owner.dispatch('plan_skill',kwargs)
    assert calls==[2,1]
    owner.fixture['candidate_order']['approach_grasp']=[2,4,0,1]
    calls.clear()
    owner.dispatch('plan_skill',kwargs)
    assert calls==[1]  # Bad advice falls back; excluded zero remains excluded.
