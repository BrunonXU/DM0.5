"""A reachable grasp must not strand the object with no placement geometry."""
from dataclasses import replace
import numpy as np
import pytest
from f1_harness.skills import propose, grasp_route_lookahead
from test_full_development_skills import scene


def test_clear_chain_returns_prediction_without_claiming_in_hand_evidence():
    original=replace(scene(),object_held=False,eef_object=None)
    approach=propose('approach_grasp',original)
    report=grasp_route_lookahead(original,approach)
    assert report['transfer_candidate_variants']
    assert report['predicted_attachment_only'] and not report['numerical_feasibility_checked']
    assert report['checked_skills']==['grasp','transfer_above_hole','align','insert','release']
    assert original.object_held is False and original.eef_object is None
    with pytest.raises(RuntimeError,match='measured_in_hand_transform_required'):
        propose('transfer_above_hole',original)
    # Recheck with the freshly observed pose immediately before closing too.
    near=replace(original,base_eef=np.array(approach['stages'][-1]['goals'][-1]))
    for skill in ('grasp','regrasp'):
        assert grasp_route_lookahead(near,propose(skill,near))['transfer_candidate_variants']


def test_grasp_clear_but_future_hole_blocked_is_advice_not_current_motion_veto():
    from f1_harness.contracts import Task,SkillIntent,digest
    from test_plan_revalidation import service,observation
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    svc=service([1_000_000_000],task);obs=observation(1_000_000_000)
    svc.fixture['grasp_route_lookahead']=True
    svc.fixture['obstacles']=[dict(min=[.4,-.1,.2],max=[.6,.1,.5],role='neighbor')]
    geometry=svc._scene(obs,[0.]*22,list(obs.state16))
    assert propose('approach_grasp',geometry)['kind']=='motion'
    intent=SkillIntent(request_id='request',session_id='s',owner_instance_id='o',task_id='task',
        task_hash=digest(task),epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id='coarse',skill='approach_grasp')
    result=svc._solve_candidate(intent,geometry,list(obs.state16),[0.]*22,0)
    advice=result['geometry_trace'][0]['grasp_route_lookahead']
    assert advice['advisory_only'] and not advice['motion_authorized']
    assert advice['diagnostic']['code']=='grasp_has_no_placement_route'
    assert 'solve_path' in svc.numerics.calls


def test_known_attachment_cannot_be_reinterpreted_as_pregrasp():
    geometry=scene()
    with pytest.raises(RuntimeError,match='grasp_lookahead_scope_mismatch'):
        grasp_route_lookahead(geometry,propose('approach_grasp',geometry))


def test_registered_transport_keeps_future_insert_collision_as_advisory_only():
    from f1_harness.contracts import HarnessError
    value=replace(scene(),object_held=False,eef_object=None,transfer_policy='registered_transport',
        obstacles=({'min':[.42,-.2,.25],'max':[.65,.2,.34],
                    'role':'bucket','name':'closed_aperture'},))
    approach=propose('approach_grasp',value)
    report=grasp_route_lookahead(value,approach)
    assert report['transfer_candidate_variants']
    assert report['required_path_skills']==['grasp']
    preview=report['precision_placement_preview']
    assert preview['advisory_only'] and not preview['motion_authorized']
    assert preview['requires_fresh_in_hand_evidence']
    assert all(c['outcome']=='advisory' and c['blocked_skill']=='insert' for c in preview['candidates'])
    assert all(c['alignment_attempts'] for c in preview['candidates'])
    assert any(a['diagnostic']['metrics'].get('route_family')=='aligned_insertion_preview'
               for c in preview['candidates'] for a in c['alignment_attempts'])
    # The physical obstruction is still enforced at actual insertion.
    grasp=propose('grasp',replace(value,base_eef=np.array(approach['stages'][-1]['goals'][-1])))
    lift=np.array(grasp['stages'][-1]['goals'][-1]);attachment=np.array(report['predicted_eef_object'])
    loaded=replace(value,base_eef=lift,base_object=lift@attachment,eef_object=attachment,object_held=True)
    transfer=propose('transfer_above_hole',loaded,variant=report['transfer_candidate_variants'][0])
    end=np.array(transfer['stages'][-1]['goals'][-1])
    aligned=propose('align',replace(loaded,base_eef=end,base_object=end@attachment))
    end=np.array(aligned['stages'][-1]['goals'][-1])
    with pytest.raises(HarnessError,match='no_clear_square_symmetry_route'):
        propose('insert',replace(loaded,base_eef=end,base_object=end@attachment))


@pytest.mark.parametrize('obstacle,reason',[
    ({'min':[.18,-.04,.24],'max':[.22,.04,.26],'role':'neighbor'},'scene_sweep_blocked'),
    ({'min':[.4,-.2,.2],'max':[.6,.2,1.2],'role':'neighbor'},'grasp_has_no_placement_route'),
])
def test_registered_transport_still_rejects_current_grasp_or_transport_collision(obstacle,reason):
    value=replace(scene(),object_held=False,eef_object=None,transfer_policy='registered_transport',
                  obstacles=(obstacle,))
    approach=propose('approach_grasp',value)
    if reason=='grasp_has_no_placement_route':
        report=grasp_route_lookahead(value,approach)
        assert report['transfer_candidate_variants']==[]
        assert report['transport_preview']['status']=='unresolved'
        assert report['transport_preview']['candidates']
        assert not report['transport_preview']['motion_authorized']
        # Actually attempting the blocked transport must still fail.
        with pytest.raises(RuntimeError,match='scene_sweep_blocked'):
            propose('transfer_above_hole',replace(value,object_held=True,eef_object=scene().eef_object))
    else:
        with pytest.raises(RuntimeError,match=reason):
            grasp_route_lookahead(value,approach)


@pytest.mark.parametrize('changes',[
    dict(insertion_depth_m=1.),dict(insertion_depth_m=float('nan')),
    dict(object_insertion_reference_height_m=float('nan')),
])
def test_future_insertion_parameters_do_not_veto_current_grasp(changes):
    value=replace(scene(),object_held=False,eef_object=None,
                  transfer_policy='registered_transport',**changes)
    approach=propose('approach_grasp',value)
    report=grasp_route_lookahead(value,approach)
    assert report['required_path_skills']==['grasp']
    with pytest.raises(RuntimeError):
        propose('insert',replace(value,object_held=True,eef_object=scene().eef_object))


def test_future_configuration_fault_is_not_silently_relabelled_as_advisory(monkeypatch):
    import f1_harness.skills as module
    value=replace(scene(),object_held=False,eef_object=None,transfer_policy='registered_transport')
    approach=propose('approach_grasp',value);actual=module.propose
    def faulty(skill,scene,**kwargs):
        if skill=='align':raise module.HarnessError('invalid_calibration_contract')
        return actual(skill,scene,**kwargs)
    monkeypatch.setattr(module,'propose',faulty)
    with pytest.raises(RuntimeError,match='invalid_calibration_contract'):
        grasp_route_lookahead(value,approach)


def test_grasp_uses_current_obstacles_without_a_destination_pose():
    value=replace(scene(),object_held=False,eef_object=None,base_hole=None,
                  aperture_measured=False,transfer_policy='registered_transport')
    approach=propose('approach_grasp',value)
    report=grasp_route_lookahead(value,approach)
    assert report['required_path_skills']==['grasp']
    assert report['transport_preview']['status']=='unresolved'
    assert all(c['reason']=='destination_geometry_unavailable'
               for c in report['transport_preview']['candidates'])
    with pytest.raises(RuntimeError,match='destination_geometry_unavailable'):
        propose('transfer_above_hole',replace(value,object_held=True,eef_object=scene().eef_object))
    # Relevant obstacles still reject the current grasp even without a hole.
    blocked=replace(value,obstacles=({'min':[.18,-.04,.24],'max':[.22,.04,.26],
                                     'role':'neighbor'},))
    with pytest.raises(RuntimeError,match='scene_sweep_blocked'):
        grasp_route_lookahead(blocked,propose('approach_grasp',blocked))
