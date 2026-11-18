"""Route fallback and bounded view-change support contact contracts."""
from dataclasses import replace
import numpy as np
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.geometry import attached_object_sweep_clear
from f1_harness.support_motion import retain_support_contact
from test_full_development_skills import scene,pose
from test_grasp_selection_planning import setup


def contact_scene(prior_z=-.0009,current_z=-.0013,**kwargs):
    original=replace(scene(),base_object=pose(z=current_z),object_held=False,
        obstacles=({'type':'halfspace','role':'table','point':[0,0,0],'normal':[0,0,1]},),**kwargs)
    return original,retain_support_contact(original,pose(z=prior_z),maximum_change_m=.015,source_observation_id='original-approach')


def sweep(value,start_z,end_z):
    return attached_object_sweep_clear(pose(z=start_z),pose(z=end_z),np.eye(4),value.object_local_box,
        value.obstacles,margin_m=.001,support_contact_tolerance_m=.001)


def test_known_support_relation_survives_bounded_view_change_without_moving_geometry():
    old,new=contact_scene()
    assert not sweep(old,-.0013,.02)
    assert sweep(new,-.0013,.02)
    assert np.array_equal(old.base_object,new.base_object)
    assert new.obstacles[0]['point']==old.obstacles[0]['point']
    assert new.obstacles[0]['support_departure_reference']['physical_contact_verified'] is False


@pytest.mark.parametrize('prior,current',[(-.004,-.0044),(-.0009,-.020)])
def test_unknown_prior_contact_or_large_geometry_change_cannot_create_support_permission(prior,current):
    old,new=contact_scene(prior,current)
    assert 'support_departure_reference' not in new.obstacles[0]
    assert not sweep(new,current,.02)


def test_support_baseline_does_not_reset_during_departure_or_exempt_other_obstacles():
    _,new=contact_scene()
    assert not sweep(new,-.0013,-.0014)
    assert not sweep(new,-.003,.02)
    blocked=replace(new,obstacles=(*new.obstacles,{'type':'halfspace','role':'ceiling','point':[0,0,.03],'normal':[0,0,-1]}))
    assert not sweep(blocked,-.0013,.02)


def test_same_grasp_direct_route_precedes_historical_fallback(monkeypatch):
    plan,obs,intent,_=setup();value=plan._scene(obs,[0.]*22,obs.state16);tried=[]
    def solve(intent,scene,held,full,variant,*,deadline_ns=None): 
        tried.append(scene.approach_route)
        if scene.approach_route=='direct':raise HarnessError('scene_sweep_blocked')
        return {'geometry_trace':[{}]}
    monkeypatch.setattr(plan,'_solve_route',solve)
    result=plan._solve_candidate(intent,value,obs.state16,[0.]*22,0)
    assert tried==['direct','historical']
    assert result['geometry_trace'][0]['preceding_route_failures'][0]['route']=='direct'


def test_both_direct_and_historical_rejections_survive_existing_diagnostic_contract(monkeypatch):
    from f1_harness.owner_bridge import numerical_failure_detail
    plan,obs,intent,_=setup();value=plan._scene(obs,[0.]*22,obs.state16)
    def fail(*args,**kwargs):raise HarnessError('scene_sweep_blocked')
    monkeypatch.setattr(plan,'_solve_route',fail)
    with pytest.raises(HarnessError) as error:
        plan._solve_candidate(intent,value,obs.state16,[0.]*22,0)
    detail=numerical_failure_detail(error.value)
    assert [row['metrics']['route_family'] for row in detail['candidates']]==['direct_approach_grasp','historical_approach_grasp']


def test_cached_route_rejections_keep_direct_and_historical_source(monkeypatch):
    from f1_harness.owner_bridge import numerical_failure_detail
    plan,obs,intent,_=setup()
    def fail(*args,**kwargs):raise HarnessError('scene_sweep_blocked')
    monkeypatch.setattr(plan,'_solve_route',fail)
    for _ in range(2):
        with pytest.raises(HarnessError) as error:
            plan.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
                held16=list(obs.state16),full22=[0.]*22))
        detail=numerical_failure_detail(error.value)
        families={item['metrics'].get('route_family') for item in detail['candidates']}
        assert families=={'direct_approach_grasp','historical_approach_grasp'}


def test_pregrasp_endpoint_adjustment_keeps_final_grasp_and_all_obstacles():
    from f1_harness.skills import propose
    target={'role':'target_object','object_id':'block','min':[-.015,-.015,0.], 'max':[.015,.015,.06]}
    value=replace(scene(),base_object=pose(),object_held=False,eef_object=None,
        pregrasp_distances_m=(.01,),obstacles=(target,))
    proposal=propose('approach_grasp',value)
    stage=proposal['stages'][0]
    assert stage['pregrasp_adjustment_object_m'][2]>0
    restored=replace(value,pregrasp_adjustment_object_m=tuple(stage['pregrasp_adjustment_object_m']))
    rebuilt=propose('approach_grasp',restored,pregrasp_distance_m=.01)
    assert np.allclose(rebuilt['stages'][0]['goals'],stage['goals'])
    grasp=propose('grasp',replace(value,base_eef=np.asarray(stage['goals'][-1])))
    assert np.allclose(grasp['stages'][0]['goals'][-1],value.base_object@value.object_grasp_eef)
    # The same proposed shift cannot bypass an unrelated obstacle.
    ceiling={'type':'halfspace','role':'ceiling','normal':[0,0,-1],'point':[0,0,.13]}
    with pytest.raises(HarnessError,match='scene_sweep_blocked'):
        propose('approach_grasp',replace(value,obstacles=(target,ceiling)))
