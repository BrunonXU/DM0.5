"""Coarse transport uses current anchors, without forging aperture evidence."""
from dataclasses import replace
import numpy as np
import pytest

from f1_harness.contracts import FixturePose, HarnessError
from f1_harness.fixture_tracking import locate_fixture
from f1_harness.geometry_context import entity_status
from f1_harness.revalidation import require_planning_geometry, _scene_unchanged, tolerances, RevalidationSceneChanged
from f1_harness.skills import propose
from test_fixture_tracking import fixture
from test_grasp_selection_planning import setup


def test_anchor_registration_without_any_aperture_mask():
    _,depth,masks,cfg,reference,anchors=fixture()
    moved={name:np.roll(mask,5,axis=1) for name,mask in masks.items()}
    pose,_,details=locate_fixture(depth,moved,cfg,np.eye(4),reference,anchors,identity_tolerance_m=.015)
    assert np.allclose(pose[:3,3],[.05,0,1])
    assert np.allclose(pose[:3,:3],np.eye(3))
    assert not details['aperture_measured'] and details['registration_residual_m']<1e-10


def test_reference_camera_is_not_replaced_by_current_head_pose():
    _,depth,masks,cfg,reference,anchors=fixture()
    camera=np.eye(4);camera[0,3]=-.05
    moved={name:np.roll(mask,5,axis=1) for name,mask in masks.items()}
    pose,_,_=locate_fixture(depth,moved,dict(cfg,base_camera=camera.tolist()),np.eye(4),reference,anchors,
        identity_tolerance_m=.015)
    assert np.allclose(pose,reference)


@pytest.mark.parametrize('fault',['missing','depth','identity'])
def test_anchor_failure_cannot_produce_stale_fixture(fault):
    _,depth,masks,cfg,reference,anchors=fixture()
    if fault=='missing':masks.pop('a')
    if fault=='depth':depth[:]=0
    if fault=='identity':masks['a']=np.roll(masks['a'],10,axis=0)
    with pytest.raises(ValueError):locate_fixture(depth,masks,cfg,np.eye(4),reference,anchors,identity_tolerance_m=.015)


def coarse_case():
    service,obs,intent,clock=setup()
    reference=obs.objects[1].pose
    measured=FixturePose(fixture_id='bucket',pose=reference,reference_pose=reference,
        frame_id='native',evidence_ids=('head-depth','head-rgb'))
    obs=obs.model_copy(update=dict(objects=obs.objects[:1],fixture=measured))
    service.fixture.update(independent_fixture_localization=True,fixture_reference_hole=list(reference))
    return service,obs,intent,clock


@pytest.mark.parametrize('aperture_present',[False,True])
def test_optional_precise_lookahead_does_not_change_coarse_approach(aperture_present):
    from copy import deepcopy
    from f1_harness.placement_advisory import placement_advisory
    service,obs,intent,clock=coarse_case()
    service.fixture['transfer_policy']='registered_transport'
    service.fixture['grasp_route_lookahead']=True
    if aperture_present:
        _,original,_,_=setup()
        obs=obs.model_copy(update={'objects':original.objects})
    geometry=service._scene(obs,[0.]*22,obs.state16,skill='approach_grasp')
    assert not geometry.aperture_measured
    approach=propose('approach_grasp',geometry)
    result={'geometry_trace':deepcopy(approach['stages'])}
    original=deepcopy(result)
    service._measured_placement_advisory(obs,[0.]*22,obs.state16,geometry,result)
    value=placement_advisory(result['geometry_trace'])
    if aperture_present:
        assert value['status']=='predicted_clear'
        result['geometry_trace'][0].pop('grasp_route_lookahead')
    else:assert value['status']=='unavailable'
    assert result==original
    assert not geometry.aperture_measured


@pytest.mark.parametrize('skill',['approach_grasp','grasp','regrasp','transfer_above_hole','move_tcp'])
def test_transit_ingress_accepts_current_fixture_without_precise_aperture(skill):
    service,obs,intent,clock=coarse_case();intent=intent.model_copy(update={'skill':skill})
    if skill=='regrasp':
        service.fixture['obstacles']=(*service.fixture['obstacles'],dict(type='halfspace',role='table',normal=[0,0,1],
            point=np.array(obs.objects[0].pose).reshape(4,4)[:3,3].tolist()))
    if skill=='move_tcp':
        service.task=service.task.model_copy(update={'allowed_skills':service.task.allowed_skills+('move_tcp',)})
        from f1_harness.contracts import digest
        intent=intent.model_copy(update={'task_hash':digest(service.task)})
    require_planning_geometry(obs,intent=intent,task=service.task,now_ns=clock[0],max_age_ns=500_000_000)
    scene=service._scene(obs,[0.]*22,obs.state16,skill=skill)
    assert not scene.aperture_measured and np.allclose(scene.base_hole,np.array(obs.fixture.pose).reshape(4,4))
    if skill=='approach_grasp':assert propose(skill,scene)['kind']=='motion'


@pytest.mark.parametrize('skill',['align','insert','release'])
def test_engagement_requires_actual_aperture_despite_nominal_fixture(skill):
    service,obs,intent,clock=coarse_case();intent=intent.model_copy(update={'skill':skill})
    with pytest.raises(HarnessError,match='missing_geometry_entities'):
        require_planning_geometry(obs,intent=intent,task=service.task,now_ns=clock[0],max_age_ns=500_000_000)
    with pytest.raises(HarnessError,match='measured_aperture_required'):
        propose(skill,service._scene(obs,[0.]*22,obs.state16))


def test_partial_context_returns_collision_geometry_but_no_hole_pose():
    service,obs,_,_=coarse_case()
    result=service.dispatch('geometry_context',dict(observation=obs.model_dump(),full22=[0.]*22,held16=obs.state16))
    assert result['hole_pose'] is None and result['obstacles'] is not None
    assert result['fixture_transit_available'] and result['missing_entities'][0]['role']=='aperture'
    assert result['fixture_transport_reference_pose']==list(obs.fixture.pose)


def test_coarse_fixture_updates_bucket_obstacles_and_never_table():
    service,obs,_,_=coarse_case()
    pose=np.eye(4);pose[2,3]=.1
    service.fixture['obstacles']=[dict(role=role,min=[-.1]*3,max=[.1]*3,pose=pose.tolist()) for role in ('bucket','table')]
    moved=np.array(obs.fixture.pose).reshape(4,4);moved[0,3]+=.03
    obs=obs.model_copy(update={'fixture':obs.fixture.model_copy(update={'pose':tuple(moved.ravel())})})
    scene=service._scene(obs,[0.]*22,obs.state16,skill='approach_grasp')
    assert np.isclose(np.array(scene.obstacles[0]['pose'])[0,3],.03)
    assert np.isclose(np.array(scene.obstacles[1]['pose'])[0,3],0)


def test_wrong_fixture_reference_never_moves_bucket_geometry():
    service,obs,_,_=coarse_case();reference=np.array(obs.fixture.reference_pose);reference[3]+=.01
    obs=obs.model_copy(update={'fixture':obs.fixture.model_copy(update={'reference_pose':tuple(reference)})})
    with pytest.raises(HarnessError,match='fixture_reference_identity_changed'):
        service._scene(obs,[0.]*22,obs.state16,skill='approach_grasp')


@pytest.mark.parametrize('skill',['align','insert','release'])
def test_fine_engagement_refines_cutout_and_goal_with_same_aperture(skill):
    service,partial,_,_=coarse_case();_,full,_,_=setup()
    reference=np.array(partial.fixture.reference_pose).reshape(4,4)
    coarse=reference.copy();coarse[0,3]+=.03
    measured=reference.copy();measured[1,3]+=.01
    full=full.model_copy(update=dict(fixture=partial.fixture.model_copy(update={'pose':tuple(coarse.ravel())}),
        objects=(full.objects[0],full.objects[1].model_copy(update={'pose':tuple(measured.ravel())}))))
    service.fixture['obstacles']=[dict(role=role,fixture_part='rim_x_low',
        min=[-.1]*3,max=[.1]*3,pose=reference.tolist()) for role in ('bucket','table')]
    scene=service._scene(full,[0.]*22,full.state16,skill=skill)
    assert scene.aperture_measured
    np.testing.assert_allclose(scene.base_hole,measured)
    np.testing.assert_allclose(scene.obstacles[0]['pose'],measured)
    np.testing.assert_allclose(scene.obstacles[1]['pose'],reference)
    transit=service._scene(full,[0.]*22,full.state16,skill='transfer_above_hole')
    np.testing.assert_allclose(transit.base_hole,coarse)
    np.testing.assert_allclose(transit.obstacles[0]['pose'],coarse)


def test_aperture_disappearance_does_not_invalidate_unchanged_transit_geometry():
    service,partial,_,_=coarse_case();_,full,_,_=setup()
    full=full.model_copy(update={'fixture':partial.fixture})
    _scene_unchanged(full,partial,task=service.task,threshold=tolerances(service.fixture,'approach_grasp'),skill='approach_grasp')
    a=service._scene(full,[0.]*22,full.state16,skill='approach_grasp')
    b=service._scene(partial,[0.]*22,partial.state16,skill='approach_grasp')
    assert np.allclose(a.base_hole,b.base_hole)
    moved=np.array(partial.fixture.pose);moved[3]+=.1
    changed=partial.model_copy(update={'fixture':partial.fixture.model_copy(update={'pose':tuple(moved)})})
    with pytest.raises(RevalidationSceneChanged,match='fixture_changed'):
        _scene_unchanged(full,changed,task=service.task,threshold=tolerances(service.fixture,'approach_grasp'),skill='approach_grasp')


def test_actual_planner_and_revalidator_share_fixture_only_scene():
    from test_plan_revalidation import observation
    from f1_harness.owner_bridge import JointProposal
    from dataclasses import asdict
    service,obs,intent,clock=coarse_case()
    planned=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    proposal=JointProposal(**planned)
    clock[0]+=1_000_000
    fresh=observation(clock[0],name='newer')
    fresh=fresh.model_copy(update=dict(objects=fresh.objects[:1],fixture=obs.fixture))
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=asdict(proposal),
        original_observation=obs.model_dump(),observation=fresh.model_dump(),full22=[0.]*22,
        held16=list(fresh.state16),after_ns=clock[0]-1))
    assert proof['observation']['observation_id']=='newer'
    assert 'forward_path' in service.numerics.calls and proposal.targets


def test_confirmed_grasp_can_plan_transport_without_visible_hole():
    from test_full_development_skills import scene as geometry
    service,obs,intent,clock=coarse_case();original=geometry()
    obj=obs.objects[0].model_copy(update=dict(held=True,eef_object_pose=tuple(original.eef_object.ravel())))
    obs=obs.model_copy(update={'objects':(obj,)})
    value=service._scene(obs,[0.]*22,obs.state16,skill='transfer_above_hole')
    assert not value.aperture_measured
    proposal=propose('transfer_above_hole',value)
    assert proposal['kind']=='motion' and proposal['stages']
    with pytest.raises(HarnessError,match='in_hand'):
        propose('transfer_above_hole',replace(value,object_held=None,eef_object=None))


def test_owner_atomic_preview_accepts_transit_and_reports_missing_alignment(tmp_path):
    from f1_harness.engine import TaskEngine
    from f1_harness.owner_bridge import OwnerBridge, JointProposal
    from f1_harness.evidence import EvidenceStore
    service,obs,intent,clock=coarse_case()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id'];engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    engine.store.event('synthetic_test_contract',robot_transport=False,numerics='test double')
    obs=obs.model_copy(update={'host_boot_id':engine.host_boot_id})
    engine.update_observation(obs)
    def planner(i,o,full,held):
        return JointProposal(**service.dispatch('plan_skill',dict(intent=i.model_dump(),
            observation=o.model_dump(),full22=full,held16=held)))
    bridge=OwnerBridge(engine,numerical_planner=planner,result_verifier=lambda *args:None,clock=lambda:clock[0],
        feedback_age_ns=200_000_000,reference_tolerance_deg=.5,stop_gripper_delta=.1)
    bridge.geometry_provider=lambda o,full,held:service.dispatch('geometry_context',dict(
        observation=o.model_dump(),full22=full,held16=held))
    bridge.measured_feedback=lambda:dict(full22=[0.]*22,measured16=obs.state16,stamps16=(clock[0],)*16)
    intent=intent.model_copy(update=dict(owner_instance_id=engine.instance,session_id=session,epoch=engine.epoch))
    try:
        result=bridge.preview_skill(intent.model_dump(),include_geometry=True)
        assert result['planned'] and result['current_scope'] and result['missing_entities']
        assert result['geometry']['hole_pose'] is None
        assert result['fixture_transit_available']
        assert 'insert' in result['aperture_required_for']
        # Separate synthetic phase fixture: inspect completed, aperture absent.
        engine.completed_skills.add('inspect_in_hand')
        result=bridge.preview_skill(intent.model_copy(update={'skill':'align'}).model_dump(),include_geometry=True)
        assert not result['planned'] and result['missing_entities'][0]['object_id']=='hole'
        assert not engine.pending and not engine.running and not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('include_geometry',[False,True])
def test_object_only_preview_returns_missing_scene_without_weakening_admission(tmp_path,include_geometry):
    from f1_harness.engine import TaskEngine
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.evidence import EvidenceStore
    service,obs,intent,clock=coarse_case()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id'];engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    obs=obs.model_copy(update=dict(host_boot_id=engine.host_boot_id,fixture=None,
        geometry_scope='coarse_object_verification',
        quality_warnings=('fixture_unavailable:capture_faults:duplicate_source_stamp:head',)))
    engine.update_observation(obs)
    planned=[]
    def planner(*args):
        planned.append(args)
        raise AssertionError('partial scene must not enter motion planning')
    bridge=OwnerBridge(engine,numerical_planner=planner,result_verifier=lambda *args:None,
        clock=lambda:clock[0],feedback_age_ns=200_000_000,reference_tolerance_deg=.5,stop_gripper_delta=.1)
    bridge.geometry_provider=lambda o,full,held:service.dispatch('geometry_context',dict(
        observation=o.model_dump(),full22=full,held16=held))
    bridge.measured_feedback=lambda:dict(full22=[0.]*22,measured16=obs.state16,stamps16=(clock[0],)*16)
    intent=intent.model_copy(update=dict(owner_instance_id=engine.instance,session_id=session,epoch=engine.epoch))
    try:
        result=bridge.preview_skill(intent.model_dump(),include_geometry=include_geometry)
        preview=result['preview'] if include_geometry else result
        assert not preview['planned'] and preview['current_scope'] and preview['no_motion']
        assert not preview['executable'] and not planned
        diagnostic=preview['planning_diagnostic']
        assert diagnostic['code']=='full_scene_required_for_motion'
        assert diagnostic['metrics']['missing_entities'][0]['object_id']=='hole'
        assert diagnostic['metrics']['recovery_hint']=='refresh_scene'
        assert 'duplicate_source_stamp:head' in diagnostic['metrics']['reason']
        assert not engine.pending and not engine.running and not engine.request_publications
        rejected=engine.submit_skill(intent.model_dump())
        assert rejected['lifecycle']=='rejected' and rejected['no_motion']
        assert rejected['reason']=='full_scene_required_for_motion'
        assert not engine.pending and not engine.running and not engine.request_publications
        with pytest.raises(HarnessError,match='owner_or_epoch_mismatch'):
            bridge.preview_skill(intent.model_copy(update={'epoch':intent.epoch+1}).model_dump())
    finally:bridge.close()
