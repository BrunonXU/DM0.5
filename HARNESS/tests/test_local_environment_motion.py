"""World observation motion uses current space evidence, never a fake object pose."""
from dataclasses import replace
import base64
import hashlib
import zlib

import numpy as np
import pytest

from f1_harness.contracts import FreeSpaceView,LocalCollisionEvidence,HarnessError,MotionParameters,digest
from f1_harness.local_environment import local_sweep_evidence,require_local_evidence
from f1_harness.observation import require_geometry
from f1_harness.revalidation import require_planning_geometry
from f1_harness.motion_goals import apply_request
from f1_harness.skills import propose
from test_grasp_selection_planning import setup


def partial_setup():
    service,obs,intent,clock=setup()
    service.task=service.task.model_copy(update={'allowed_skills':(*service.task.allowed_skills,'move_tcp')})
    service.fixture.update(independent_fixture_localization=True,cartesian_motion=True,
        fixture_reference_hole=np.eye(4).tolist(),obstacles=[dict(type='halfspace',role='table',point=[0,0,0],normal=[0,0,1])])
    obj=obs.objects[0].model_copy(update=dict(pose=None,held=None,eef_object_pose=None,stationary=None))
    view=FreeSpaceView(camera='right',camera_pose_world=tuple(np.eye(4).ravel()),
        intrinsics=(60.,0,10.,0,60.,10.,0,0,1.),image_width=20,image_height=20,
        tile_size_px=5,grid_width=4,grid_height=4,min_depth_m=(2.,)*16,near_depth_m=.05,
        source_rgb_sha256='0'*64,source_depth_sha256='0'*64)
    evidence=LocalCollisionEvidence(object_id=service.task.object_id,frame_id='native',
        observation_id=obs.observation_id,calibration_id=obs.calibration_id,
        measured_at_ns=obs.received_ns,state_received_ns=obs.state_received_ns,source_frames=obs.frames,
        observed_surface_min=(.18,-.02,.2),observed_surface_max=(.22,.02,.2),
        possible_occupancy_min=(.08,-.12,.1),possible_occupancy_max=(.32,.12,.3),
        registered_object_diagonal_m=float(np.linalg.norm([.03,.03,.06])),planning_margin_m=.015,
        depth_points=100,mask_pixels=120,free_space_views=(view,))
    obs=obs.model_copy(update=dict(objects=(obj,),geometry_scope='coarse_local_environment',
        stationary=False,quality_warnings=('object_pose_unavailable',),local_collision_evidence=evidence))
    goal=np.eye(4);goal[:3,3]=[.02,0,.42]
    intent=intent.model_copy(update=dict(task_hash=digest(service.task),skill='move_tcp',
        parameters=MotionParameters(target_frame='world',target_tcp_pose=tuple(goal.ravel()))))
    return service,obs,intent,clock


def test_partial_world_motion_plans_and_revalidates_without_object_pose():
    service,obs,intent,clock=partial_setup()
    kwargs=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0.]*22,held16=list(obs.state16))
    require_geometry(obs,now_ns=clock[0],max_age_ns=500_000_000,goal='placed',admission_profile='development_coarse',skill='move_tcp')
    plan=service.dispatch('plan_skill',kwargs)
    assert plan['geometry_trace'][0]['semantic_stage']=='free_space_tcp'
    assert obs.objects[0].pose is None and obs.objects[0].held is None
    clock[0]+=100_000_000
    frames=tuple(f.model_copy(update=dict(received_ns=clock[0],source_ns=clock[0],sequence=clock[0])) for f in obs.frames)
    evidence=obs.local_collision_evidence.model_copy(update=dict(observation_id='next',measured_at_ns=clock[0],
        state_received_ns=clock[0],source_frames=frames))
    later=obs.model_copy(update=dict(observation_id='next',scene_version='next',received_ns=clock[0],
        state_received_ns=clock[0],frames=frames,local_collision_evidence=evidence))
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
        original_observation=obs.model_dump(),observation=later.model_dump(),full22=[0.]*22,
        held16=list(obs.state16),after_ns=clock[0]-1))
    assert proof['observation_sha256']==digest(later)


@pytest.mark.parametrize('skill,frame',[('grasp','world'),('approach_grasp','world'),('move_tcp','object'),('move_tcp','hole')])
def test_partial_evidence_does_not_authorize_pose_dependent_actions(skill,frame):
    service,obs,intent,clock=partial_setup()
    intent=intent.model_copy(update=dict(skill=skill,parameters=intent.parameters.model_copy(update={'target_frame':frame})))
    with pytest.raises((HarnessError,ValueError),match='local_environment'):
        require_planning_geometry(obs,intent=intent,task=service.task,now_ns=clock[0],max_age_ns=500_000_000)


@pytest.mark.parametrize('closed',[True,False])
def test_read_context_survives_missing_or_closed_gripper_but_motion_does_not(closed):
    service,obs,intent,clock=partial_setup()
    state=list(obs.state16);state[15]=100. if closed else 0.
    valid=list(obs.state_valid_mask);valid[15]=closed
    obs=obs.model_copy(update=dict(state16=tuple(state),state_valid_mask=tuple(valid)))
    require_geometry(obs,now_ns=clock[0],max_age_ns=500_000_000,goal='placed',admission_profile='development_coarse',skill='reobserve')
    value=service.dispatch('geometry_context',dict(observation=obs.model_dump(),full22=[0.]*22,held16=state))
    assert value['object_pose'] is None and value['available_skills']==['reobserve']
    assert 'free_space_views' not in str(value) and 'min_depth_m' not in str(value)
    with pytest.raises(HarnessError,match='measured_open_gripper'):
        service._scene(obs,[0.]*22,state,skill='move_tcp')


@pytest.mark.parametrize("visible_hole",[True,False])
def test_held_partial_geometry_keeps_each_entity_and_only_admits_passive_observation(tmp_path,visible_hole):
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.skill_services import VerifyService
    service,obs,intent,clock=partial_setup()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id']
    engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    engine.completed_skills.add('inspect_in_hand')
    obj=obs.objects[0].model_copy(update=dict(held=True))
    hole=obj.model_copy(update=dict(object_id=service.task.hole_id,held=None,
        pose=tuple(np.eye(4).ravel()),stationary=True))
    objects=(obj,hole) if visible_hole else (obj,)
    obs=obs.model_copy(update=dict(objects=objects,host_boot_id=engine.host_boot_id))
    intent=intent.model_copy(update=dict(session_id=session,owner_instance_id=engine.instance,epoch=engine.epoch))
    engine.update_observation(obs)
    ctx=service.dispatch('geometry_context',dict(observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    assert ctx['object_held'] is True and ctx['object_pose'] is None
    assert ctx['hole_pose']==(list(hole.pose) if visible_hole else None)
    assert ctx['available_skills']==['reobserve'] and ctx['cartesian_motion'] is False
    assert {m['role'] for m in ctx['missing_entities']}==(
        {'target_object'} if visible_hole else {'target_object','aperture'})

    refresh=intent.model_copy(update=dict(skill='reobserve',parameters=MotionParameters()))
    require_planning_geometry(obs,intent=refresh,task=service.task,now_ns=clock[0],max_age_ns=500_000_000)
    assert engine.prepare_skill(refresh.model_dump())['ready']
    assert engine.submit_skill(refresh.model_dump())['lifecycle']=='accepted'
    assert engine.take_pending().skill=='reobserve'
    verifier=VerifyService.__new__(VerifyService);verifier.task=service.task
    result=verifier._verify_observation(refresh,obs)
    assert result.outcome=='success'
    engine.complete(refresh.request_id,result)
    assert engine.request_publications=={}
    for skill,reason in [('align','full_scene_required_for_motion'),('move_tcp','cartesian_motion_attachment_unknown')]:
        attempt=intent.model_copy(update=dict(skill=skill,request_id=skill+'-partial',
            parameters=intent.parameters if skill=='move_tcp' else MotionParameters()))
        with pytest.raises(HarnessError,match=reason):
            engine.prepare_skill(attempt.model_dump())
    with pytest.raises(HarnessError,match='unloaded_world_observation_motion'):
        service._scene(obs,[0.]*22,list(obs.state16),skill='move_tcp')

    mismatch=obs.model_copy(update=dict(local_collision_evidence=obs.local_collision_evidence.model_copy(
        update=dict(measured_at_ns=obs.received_ns+1))))
    with pytest.raises(HarnessError,match='local_environment_binding_mismatch'):
        service.dispatch('geometry_context',dict(observation=mismatch.model_dump(),
            full22=[0.]*22,held16=list(obs.state16)))


def test_depth_hole_is_unknown_and_fine_pixels_can_resolve_a_coarse_tile():
    service,obs,intent,_=partial_setup()
    scene=service._scene(obs,[0.]*22,obs.state16,skill='move_tcp')
    goal=scene.base_eef.copy();goal[0,3]+=.02
    assert local_sweep_evidence(scene,scene.base_eef,goal)['clear']
    view=obs.local_collision_evidence.free_space_views[0]
    unknown=view.model_copy(update={'min_depth_m':(0.,)*16})
    bad=replace(scene,local_collision_evidence=obs.local_collision_evidence.model_copy(update={'free_space_views':(unknown,)}))
    assert not local_sweep_evidence(bad,bad.base_eef,goal)['clear']
    pixels=np.full((20,20),2.,dtype='<f4');raw=pixels.tobytes()
    fine=unknown.model_copy(update=dict(fine_depth_zlib_base64=base64.b64encode(zlib.compress(raw)).decode(),
        fine_depth_sha256=hashlib.sha256(raw).hexdigest()))
    fixed=replace(bad,local_collision_evidence=obs.local_collision_evidence.model_copy(update={'free_space_views':(fine,)}))
    assert local_sweep_evidence(fixed,fixed.base_eef,goal)['clear']
    pixels[9:12,9:12]=0;raw=pixels.tobytes()
    hole=fine.model_copy(update=dict(fine_depth_zlib_base64=base64.b64encode(zlib.compress(raw)).decode(),
        fine_depth_sha256=hashlib.sha256(raw).hexdigest()))
    blocked=replace(fixed,local_collision_evidence=obs.local_collision_evidence.model_copy(update={'free_space_views':(hole,)}))
    assert not local_sweep_evidence(blocked,blocked.base_eef,goal)['clear']


def test_constant_depth_plane_proves_positive_rays_before_surface_not_behind():
    service,obs,_,_=partial_setup()
    evidence=obs.local_collision_evidence
    view=evidence.free_space_views[0].model_copy(update=dict(near_depth_m=0.,min_depth_m=(1.,)*16))
    obs=obs.model_copy(update=dict(local_collision_evidence=evidence.model_copy(update=dict(free_space_views=(view,)))))
    scene=service._scene(obs,[0.]*22,obs.state16,skill='move_tcp')
    assert local_sweep_evidence(scene,scene.base_eef,scene.base_eef)['clear']
    behind=scene.base_eef.copy();behind[2,3]=1.1
    assert not local_sweep_evidence(scene,behind,behind)['clear']
    at_camera=scene.base_eef.copy();at_camera[2,3]=0
    assert not local_sweep_evidence(scene,at_camera,at_camera)['clear']


def test_fine_depth_tampering_and_camera_source_substitution_are_not_unknown_quality():
    service,obs,_,_=partial_setup()
    view=obs.local_collision_evidence.free_space_views[0].model_copy(update={'source_depth_sha256':'f'*64})
    bad=obs.model_copy(update={'local_collision_evidence':obs.local_collision_evidence.model_copy(update={'free_space_views':(view,)})})
    with pytest.raises(HarnessError,match='depth_source_missing'):
        require_local_evidence(service.task,bad,service.fixture)


def test_world_proposal_rechecks_new_occupied_depth_region():
    service,obs,intent,_=partial_setup()
    scene=apply_request(service,service._scene(obs,[0.]*22,obs.state16,skill='move_tcp'),intent)
    assert propose('move_tcp',scene)['stages'][0]['allow_target_contact'] is False
    view=obs.local_collision_evidence.free_space_views[0].model_copy(update={'min_depth_m':(.3,)*16})
    blocked=replace(scene,local_collision_evidence=obs.local_collision_evidence.model_copy(update={'free_space_views':(view,)}))
    with pytest.raises(HarnessError,match='local_observation_space_unknown'):
        propose('move_tcp',blocked)


def test_partial_world_owner_completion_is_not_a_grasp_or_placement(tmp_path):
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.contracts import SkillResult
    service,obs,intent,clock=partial_setup()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id']
    engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    obs=obs.model_copy(update={'host_boot_id':engine.host_boot_id})
    engine.update_observation(obs)
    intent=intent.model_copy(update=dict(session_id=session,owner_instance_id=engine.instance,epoch=engine.epoch))
    assert engine.submit_skill(intent.model_dump())['lifecycle']=='accepted'
    assert engine.take_pending().skill=='move_tcp'
    # This lifecycle fixture has no publisher; only the actual observed skill
    # milestone is recorded by the trusted completion contract.
    engine.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
        outcome='success',reason='owner_arrival_and_fresh_scene',evidence_ids=('fixture-fresh-scene',)))
    assert engine.completed_skills=={'move_tcp'}
    assert not engine.request_publications
    assert obs.objects[0].pose is None and obs.objects[0].held is None


def test_partial_world_owner_rejects_an_unresolved_prior_grasp(tmp_path):
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    service,obs,intent,clock=partial_setup()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0])
    session=engine.open_session()['session_id'];engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    engine.update_observation(obs.model_copy(update={'host_boot_id':engine.host_boot_id}))
    engine.attempted_motion.add('grasp')
    intent=intent.model_copy(update=dict(session_id=session,owner_instance_id=engine.instance,epoch=engine.epoch))
    result=engine.submit_skill(intent.model_dump())
    assert result['no_motion'] and result['reason']=='cartesian_motion_attachment_unknown'
    assert engine.pending is None and not engine.request_publications


def test_unknown_space_feedback_retains_bounded_camera_and_cell_diagnostics():
    import json
    from f1_harness.owner_bridge import numerical_failure_detail
    metrics=dict(cell_min=[.1,.2,.3],cell_max=[.2,.3,.4],possible_target_overlap=True,
        views=[dict(camera='right',reason='depth_pixels_unknown',unknown_pixels=4)],
        cells_tested=19,unknown_space_is_free=False,free_space_views=[{'private_grid':[2.]*100}])
    result=numerical_failure_detail(HarnessError('local_observation_space_unknown:'+json.dumps(
        dict(diagnostic=dict(code='local_observation_space_unknown',metrics=metrics)))))
    assert result['code']=='local_observation_space_unknown'
    assert result['metrics']['views'][0]['unknown_pixels']==4
    assert result['metrics']['cell_min']==[.1,.2,.3]
    assert 'free_space_views' not in str(result)


def test_surface_anchor_intersection_never_becomes_an_object_pose():
    from f1_harness.contracts import SurfaceAnchorEvidence,LocalCollisionEvidence
    service,obs,_,_=partial_setup();e=obs.local_collision_evidence
    anchor=SurfaceAnchorEvidence(bounds_min=e.observed_surface_min,bounds_max=e.observed_surface_max,
        inlier_points=80,minimum_points=40,normal_world=(0,0,1),residual_p95_m=.001,
        distance_threshold_m=.002,competing_plane_excluded=True)
    radius=e.registered_object_diagonal_m+e.planning_margin_m
    data=e.model_dump();data.update(surface_anchor=anchor.model_dump(),
        possible_occupancy_min=[p-radius for p in anchor.bounds_max],
        possible_occupancy_max=[p+radius for p in anchor.bounds_min])
    valid=LocalCollisionEvidence.model_validate(data)
    assert valid.possible_occupancy_min[0]>e.possible_occupancy_min[0]
    assert obs.objects[0].pose is None
    data['possible_occupancy_min'][0]+=.001
    with pytest.raises(ValueError,match='occupancy_evidence'):
        LocalCollisionEvidence.model_validate(data)


def test_real_anchor_balls_remove_only_the_extra_corners_of_their_aabb():
    from f1_harness.contracts import SurfaceAnchorEvidence
    service,obs,_,_=partial_setup();scene=service._scene(obs,[0.]*22,obs.state16,skill='move_tcp')
    evidence=obs.local_collision_evidence;r=evidence.registered_object_diagonal_m+evidence.planning_margin_m
    point=(0.,0.,.5)
    anchor=SurfaceAnchorEvidence(bounds_min=point,bounds_max=point,inlier_points=100,minimum_points=100,
        normal_world=(0,0,1),residual_p95_m=0.,distance_threshold_m=.001,competing_plane_excluded=True)
    unknown=evidence.free_space_views[0].model_copy(update=dict(min_depth_m=(0.,)*16))
    evidence=evidence.model_copy(update=dict(observed_surface_min=point,observed_surface_max=point,
        possible_occupancy_min=(-r,-r,.5-r),possible_occupancy_max=(r,r,.5+r),surface_anchor=anchor,
        free_space_views=(unknown,)))
    scene=replace(scene,local_collision_evidence=evidence,local_registered_environment=True,
        tool_local_boxes=({'min':[.07,.07,0.],'max':[.08,.08,.01]},))
    assert not local_sweep_evidence(scene,scene.base_eef,scene.base_eef)['clear']
    evidence=evidence.model_copy(update=dict(surface_anchor=anchor.model_copy(
        update=dict(extremal_points_world=(point,)*6))))
    scene=replace(scene,local_collision_evidence=evidence)
    assert local_sweep_evidence(scene,scene.base_eef,scene.base_eef)['clear']
    # A cell inside the actual D sphere stays unknown, even in the initial
    # outer tool envelope. Empty cavities were never asserted to be solid.
    inner=replace(scene,tool_local_boxes=({'min':[.02,.02,0.],'max':[.03,.03,.01]},))
    assert not local_sweep_evidence(inner,inner.base_eef,inner.base_eef)['clear']


@pytest.mark.parametrize('policy',['required','advisory'])
def test_tracking_lag_is_an_envelope_not_a_reverse_executed_departure(policy):
    from f1_harness.contracts import SurfaceAnchorEvidence,SurfaceSupportingPlane
    from f1_harness.skills import _motion_clear
    service,obs,_,_=partial_setup();scene=service._scene(obs,[0.]*22,obs.state16,skill='move_tcp')
    point=(0.,0.,.406)
    anchor=SurfaceAnchorEvidence(bounds_min=point,bounds_max=point,inlier_points=100,minimum_points=100,
        normal_world=(0,0,1),residual_p95_m=0.,distance_threshold_m=.001,competing_plane_excluded=True,
        supporting_plane=SurfaceSupportingPlane(centre_world=point,normal_world=(0,0,1),selected_normal_world=(0,0,1),
            normal_difference_rad=0,normal_tolerance_rad=.1,camera_plane_signed_distance_m=.2,
            all_target_front_side_max_m=0.,residual_p95_m=0.))
    e=obs.local_collision_evidence.model_copy(update=dict(surface_anchor=anchor,
        observed_surface_min=point,observed_surface_max=point,
        possible_occupancy_min=(-1.,-1.,-1.),possible_occupancy_max=(1.,1.,1.),
        free_space_views=(obs.local_collision_evidence.free_space_views[0].model_copy(update=dict(min_depth_m=(0.,)*16)),)))
    goal=scene.base_eef.copy();goal[2,3]+=.02
    scene=replace(scene,local_collision_evidence=e,local_registered_environment=True,tool_padding_policy=policy,
        cartesian_request=dict(goals=[goal.tolist()],support_contact=False))
    nominal=scene.base_eef.copy();nominal[2,3]+=.001
    actual=scene.base_eef.copy();actual[2,3]+=.0005
    assert _motion_clear(scene,nominal,actual) is (policy=='advisory')
    assert _motion_clear(scene,nominal,actual,measurement_envelope=True)
    actual[2,3]=scene.base_eef[2,3]-.001
    assert _motion_clear(scene,nominal,actual,measurement_envelope=True) is (policy=='advisory')
    # Advisory removes only extra tool padding, not physical target occupancy.
    actual[2,3]=scene.base_eef[2,3]-.02
    assert not _motion_clear(scene,nominal,actual,measurement_envelope=True)


def test_advisory_target_plane_keeps_measured_front_extent_and_unknown_volume():
    from f1_harness.contracts import SurfaceAnchorEvidence,SurfaceSupportingPlane
    from f1_harness.local_environment import target_excluded
    _,obs,_,_=partial_setup();e=obs.local_collision_evidence
    point=(0.,0.,.5)
    plane=SurfaceSupportingPlane(centre_world=point,normal_world=(0,0,1),selected_normal_world=(0,0,1),
        normal_difference_rad=0,normal_tolerance_rad=.1,camera_plane_signed_distance_m=.2,
        all_target_front_side_max_m=.004,residual_p95_m=.001)
    anchor=SurfaceAnchorEvidence(bounds_min=point,bounds_max=point,inlier_points=100,minimum_points=100,
        normal_world=(0,0,1),residual_p95_m=.001,distance_threshold_m=.002,competing_plane_excluded=True,
        supporting_plane=plane)
    e=e.model_copy(update=dict(surface_anchor=anchor))
    def excluded(z,margin):
        return bool(target_excluded(e,[-.001,-.001,z],[.001,.001,z+.0001],margin_m=margin)[0])
    assert not excluded(.503,0.) # Positive fitted-plane gap is still inside measured entity extent.
    assert excluded(.505,0.) and not excluded(.505,.015)
    assert not excluded(.49,0.) # Unseen back remains possible registered volume.


def _fresh_partial(obs,now):
    frames=tuple(f.model_copy(update=dict(received_ns=now,source_ns=now,sequence=now)) for f in obs.frames)
    return obs.model_copy(update=dict(observation_id='new',scene_version='new',received_ns=now,
        state_received_ns=now,frames=frames,local_collision_evidence=obs.local_collision_evidence.model_copy(
            update=dict(observation_id='new',measured_at_ns=now,state_received_ns=now,source_frames=frames))))


@pytest.mark.parametrize('direction',['partial_to_full','full_to_partial','fixture_restored',
    'partial_to_object','object_to_partial','object_to_full','full_to_object'])
def test_world_observation_route_revalidates_across_geometry_recovery(direction):
    from test_plan_revalidation import observation
    from f1_harness.contracts import FixturePose
    from f1_harness.owner_bridge import JointProposal
    from f1_harness.revalidation import proof_observation
    service,partial,intent,clock=partial_setup()
    full=observation(clock[0])
    def object_scope(partial,full):
        return partial.model_copy(update=dict(geometry_scope='coarse_object_verification',
            objects=full.objects[:1]))
    original=(full if direction.startswith('full_to') else object_scope(partial,full)
              if direction.startswith('object_to') else partial)
    plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        full22=[0.]*22,held16=list(original.state16)))
    clock[0]+=100_000_000
    latest=observation(clock[0],name='new') if direction.endswith('_to_full') else _fresh_partial(partial,clock[0])
    if direction.endswith('_to_object'):
        latest=object_scope(latest,observation(clock[0],name='new'))
        context=service.dispatch('geometry_context',dict(observation=latest.model_dump(),full22=[0.]*22,held16=list(latest.state16)))
        assert context['object_pose']==list(latest.objects[0].pose)
        assert context['hole_pose'] is None and context['local_environment']['pose_available']
    if direction=='fixture_restored':
        latest=latest.model_copy(update=dict(fixture=FixturePose(fixture_id='bucket',frame_id='native',
            pose=tuple(np.eye(4).ravel()),reference_pose=tuple(np.eye(4).ravel()),
            evidence_ids=('current-head-anchors',),measured_at_ns=clock[0])))
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
        original_observation=original.model_dump(),observation=latest.model_dump(),full22=[0.]*22,
        held16=list(original.state16),after_ns=clock[0]-1))
    assert proof['geometry_checks']['swept_segments']>0
    assert proof_observation(proof,proposal=JointProposal(**plan),original=original,intent=intent,
        task=service.task,now_ns=clock[0],max_age_ns=500_000_000)==latest


def test_restored_geometry_after_receipt_requires_fresh_sweep_not_identity_fault():
    from test_plan_revalidation import observation
    from f1_harness.owner_bridge import JointProposal
    from f1_harness.revalidation import proof_observation,RevalidationEvidenceUpdated
    service,obs,intent,clock=partial_setup()
    plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    clock[0]+=100_000_000;latest=_fresh_partial(obs,clock[0])
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
        original_observation=obs.model_dump(),observation=latest.model_dump(),full22=[0.]*22,
        held16=list(obs.state16),after_ns=clock[0]-1))
    clock[0]+=100_000_000;restored=observation(clock[0],name='restored')
    with pytest.raises(RevalidationEvidenceUpdated,match='revalidation_local_evidence_updated'):
        proof_observation(proof,proposal=JointProposal(**plan),original=obs,intent=intent,
            task=service.task,now_ns=clock[0],max_age_ns=500_000_000,live_observation=restored)


@pytest.mark.parametrize('policy',['required','advisory'])
def test_partial_receipt_uses_corridor_not_new_frame_ids_or_depth_noise(policy):
    from f1_harness.local_environment import check_sweep_certificate
    from f1_harness.owner_bridge import JointProposal
    from f1_harness.revalidation import proof_observation,RevalidationEvidenceUpdated
    service,obs,intent,clock=partial_setup()
    service.fixture['tool_padding_policy']=policy
    plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    clock[0]+=100_000_000;latest=_fresh_partial(obs,clock[0])
    proof=service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=plan,
        original_observation=obs.model_dump(),observation=latest.model_dump(),full22=[0.]*22,
        held16=list(obs.state16),after_ns=clock[0]-1))
    certificate=proof['geometry_checks']['local_space_certificate']
    assert certificate['views'][0]['pixels']>0
    for index in range(20):
        clock[0]+=50_000_000
        live=_fresh_partial(obs,clock[0]);e=live.local_collision_evidence
        # Every image time and measured depth changes at 20 Hz, but surfaces
        # remain beyond the particular corridor proven by the worker.
        view=e.free_space_views[0].model_copy(update=dict(min_depth_m=(1.9+index*.001,)*16))
        live=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(view,)))))
        assert proof_observation(proof,proposal=JointProposal(**plan),original=obs,intent=intent,
            task=service.task,now_ns=clock[0],max_age_ns=500_000_000,live_observation=live)==latest
    holes=view.model_copy(update=dict(min_depth_m=(0.,)*16))
    hole_obs=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(holes,)))))
    check=check_sweep_certificate(certificate,latest,hole_obs)
    assert check['retained_original_depth_pixels']==check['checked_pixels']>0
    close=view.model_copy(update=dict(min_depth_m=(.1,)*16))
    blocked=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(close,)))))
    with pytest.raises(RevalidationEvidenceUpdated):
        proof_observation(proof,proposal=JointProposal(**plan),original=obs,intent=intent,
            task=service.task,now_ns=clock[0],max_age_ns=500_000_000,live_observation=blocked)
    moved=list(view.camera_pose_world);moved[3]+=.001
    shifted=view.model_copy(update=dict(camera_pose_world=tuple(moved)))
    changed=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(shifted,)))))
    assert check_sweep_certificate(certificate,latest,changed)['checked_pixels']>0
    moved[3]+=.02;shifted=view.model_copy(update=dict(camera_pose_world=tuple(moved)))
    changed=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(shifted,)))))
    with pytest.raises(RevalidationEvidenceUpdated):check_sweep_certificate(certificate,latest,changed)
    # Affirmative target motion cannot be hidden by a later image dropout.
    moved_e=hole_obs.local_collision_evidence.model_copy(update=dict(
        observed_surface_min=tuple(np.asarray(e.observed_surface_min)+[0,.02,0]),
        observed_surface_max=tuple(np.asarray(e.observed_surface_max)+[0,.02,0])))
    with pytest.raises(RevalidationEvidenceUpdated):
        check_sweep_certificate(certificate,latest,hole_obs.model_copy(update=dict(local_collision_evidence=moved_e)))


def test_cross_scope_preserves_camera_and_attachment_identity():
    from test_plan_revalidation import observation
    from f1_harness.revalidation import _measurement_identity,_scene_unchanged,tolerances,RevalidationSceneChanged
    service,obs,_,clock=partial_setup();full=observation(clock[0])
    changed=full.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(publisher_id='new'))
        if f.camera=='right' else f for f in full.frames)))
    with pytest.raises(HarnessError,match='camera_identity_changed'):
        _measurement_identity(obs,changed,task=service.task,skill='move_tcp')
    held=full.model_copy(update=dict(objects=(full.objects[0].model_copy(update=dict(held=True)),full.objects[1])))
    with pytest.raises(RevalidationSceneChanged,match='attachment_changed'):
        _scene_unchanged(obs,held,task=service.task,threshold=tolerances(service.fixture,'move_tcp'),
            skill='move_tcp',world_motion=True)
    with pytest.raises(RevalidationSceneChanged,match='scene_geometry_scope_changed'):
        _scene_unchanged(full,obs,task=service.task,threshold=tolerances(service.fixture,'move_tcp'),
            skill='move_tcp',world_motion=False)


def test_live_corridor_change_rechecks_same_unpublished_candidate_off_loop(tmp_path):
    from dataclasses import asdict
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.owner_bridge import OwnerBridge,JointProposal
    from test_unpublished_plan import ControlledExecutor
    service,obs,intent,clock=partial_setup()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0],lease_ms=20000)
    session=engine.open_session()['session_id'];engine.prepare_task(session,service.task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    obs=obs.model_copy(update=dict(host_boot_id=engine.host_boot_id));engine.update_observation(obs)
    intent=intent.model_copy(update=dict(session_id=session,owner_instance_id=engine.instance,epoch=engine.epoch))
    assert engine.submit_skill(intent.model_dump())['lifecycle']=='accepted'
    def plan(request,observed,full,held):
        return JointProposal(**service.dispatch('plan_skill',dict(intent=request.model_dump(),
            observation=observed.model_dump(),full22=full,held16=held)))
    def revalidate(request,candidate,original,latest,full,held,after):
        return service.dispatch('revalidate_plan',dict(intent=request.model_dump(),proposal=asdict(candidate),
            original_observation=original.model_dump(),observation=latest.model_dump(),full22=full,
            held16=held,after_ns=after))
    bridge=OwnerBridge(engine,numerical_planner=plan,result_verifier=lambda *a:None,clock=lambda:clock[0],
        feedback_age_ns=200_000_000,reference_tolerance_deg=.5,stop_gripper_delta=.1,candidate_revalidator=revalidate)
    bridge.executor.shutdown();bridge.executor=ControlledExecutor()
    state=dict(full22=[0.]*22,held16=list(obs.state16),measured16=list(obs.state16))
    def poll():return bridge.poll(**state,stamps16=(clock[0],)*16)
    try:
        assert poll() is None
        future,func,args=bridge.executor.jobs[-1];candidate=func(*args)
        clock[0]+=100_000_000;future.set_result(candidate);assert poll() is None
        original_after=bridge.revalidation_after_ns
        clock[0]+=50_000_000;latest=_fresh_partial(obs,clock[0])
        engine.update_live_observation(latest,engine.status()['perception_context']);assert poll() is None
        future,func,args=bridge.executor.jobs[-1];proof=func(*args)
        clock[0]+=50_000_000;live=_fresh_partial(obs,clock[0]);e=live.local_collision_evidence
        close=e.free_space_views[0].model_copy(update=dict(min_depth_m=(.1,)*16))
        live=live.model_copy(update=dict(local_collision_evidence=e.model_copy(update=dict(free_space_views=(close,)))))
        engine.update_live_observation(live,engine.status()['perception_context']);future.set_result(proof)
        assert poll() is None and engine.authorized
        assert bridge.pending_revalidation_plan is candidate
        assert bridge.revalidation_after_ns==original_after and len(bridge.executor.jobs)==2
        assert engine.running.request_id==intent.request_id and not engine.request_publications
        clock[0]+=50_000_000;latest=_fresh_partial(obs,clock[0])
        engine.update_live_observation(latest,engine.status()['perception_context']);assert poll() is None
        future,func,args=bridge.executor.jobs[-1];future.set_result(func(*args))
        assert poll() is not None and engine.authorized
        assert len(bridge.executor.jobs)==3 and bridge.proposal is candidate
        assert not engine.request_publications
    finally:bridge.close()
