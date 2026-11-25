from dataclasses import asdict, replace
import json

import numpy as np
import pytest

from f1_harness.contracts import Frame, Pairing, Observation, SceneObject, Task, SkillIntent, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.frames import FrameChain
from f1_harness.owner_bridge import OwnerBridge, JointProposal
from f1_harness.revalidation import revalidate_candidate, proof_observation, tolerances, RevalidationExpired
from f1_harness.skill_services import PlanService
from test_full_development_skills import scene, pose
from test_unpublished_plan import ControlledExecutor


def observation(now,*,boot='boot',name='original'):
    value=scene()
    frames=tuple(Frame(camera=camera,stream=stream,frame_id=camera+'_'+stream,
        sequence=now,source_ns=now,received_ns=now,clock_domain='camera-clock',width=20,height=20,
        encoding='rgb8' if stream=='rgb' else '16UC1',blob_sha256='0'*64,
        depth_scale_m=.001 if stream=='depth' else None,camera_info_sha256='1'*64)
        for camera in ('head','right') for stream in ('rgb','depth'))
    return Observation(observation_id=name,acquisition_instance='cameras',received_ns=now,
        clock_domain='robot-clock',host_boot_id=boot,frames=frames,
        pairings=tuple(Pairing(camera=c,quality='receipt_only',rgb_sequence=now,depth_sequence=now,skew_ns=0)
                       for c in ('head','right')),state16=(0.,)*15+(18.,),state_valid_mask=(True,)*16,
        state_received_ns=now,calibration_id='coarse',scene_version=name+'-scene',stationary=True,
        geometry_scope='coarse_static_task',geometry_camera='right',
        objects=(SceneObject(object_id='block',shape='square',frame_id='native',pose=tuple(value.base_object.ravel()),
                             held=False,inserted=False,evidence_ids=('block-evidence',)),
                 SceneObject(object_id='hole',shape='square',frame_id='native',pose=tuple(value.base_hole.ravel()),
                             stationary=True,evidence_ids=('hole-evidence',))))


class Numerics:
    def __init__(self):self.poses={};self.calls=[];self.offset=0.
    def call(self,method,**kw):
        self.calls.append(method)
        if method=='forward_pose':return scene().base_eef.tolist()
        if method=='forward_path':
            result=[self.poses[row[7]].copy() for row in kw['targets']]
            for value in result:value[0,3]+=self.offset
            return [value.tolist() for value in result]
        assert method=='solve_path'
        if 'joint_path_to_validate' in kw.get('scene',{}):
            return kw['scene']['joint_path_to_validate']
        targets=[];held=list(kw['held16'])
        for goal in kw['goals']:
            held=held.copy();held[7]=len(self.poses)+1
            self.poses[held[7]]=np.array(goal);targets.append(held.copy())
        if not targets:
            self.poses[held[7]]=scene().base_eef.copy()
        if kw['gripper'] is not None:held[15]=kw['gripper']
        targets.append(held.copy())
        return targets


def service(clock,task):
    result=object.__new__(PlanService);result.task=task;result.clock=lambda:clock[0]
    from f1_harness.candidate_failures import CandidateFailures
    result.candidate_failures=CandidateFailures()
    value=replace(scene(),object_held=False,effective_clearance_m=.003)
    result.fixture=asdict(value)|dict(native_frame='native',joint_tolerance_deg=.1,settle_ms=20,gripper_tolerance=.5,
        revalidation=dict(xy_tolerance_m=.002,yaw_tolerance_rad=.08,tilt_tolerance_rad=.08))
    result.frames=FrameChain(pose(),pose(),'native','world');result.numerics=Numerics()
    return result


@pytest.fixture
def scenario():
    clock=[1_000_000_000]
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    original=observation(clock[0]);serv=service(clock,task)
    intent=SkillIntent(request_id='request',session_id='session',owner_instance_id='owner',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=original.observation_id,scene_version=original.scene_version,
        calibration_id=task.calibration_id,skill='approach_grasp')
    held=list(original.state16);full=[0.]*22
    candidate=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),full22=full,held16=held))
    clock[0]+=8_000_000_000;after=clock[0]
    clock[0]+=100_000_000;latest=observation(clock[0],name='new')
    args=dict(intent=intent.model_dump(),proposal=candidate,original_observation=original.model_dump(),
              observation=latest.model_dump(),full22=full,held16=held,after_ns=after)
    return serv,args,clock,original,latest,intent


def test_slow_plan_gets_distinct_fresh_geometry_proof(scenario):
    serv,args,clock,original,latest,intent=scenario
    proof=serv.dispatch('revalidate_plan',args)
    assert proof['original_observation_sha256']==digest(original)
    assert original.received_ns==1_000_000_000 and latest.received_ns>9_000_000_000
    assert proof['observation_sha256']==digest(latest)
    assert proof['geometry_checks']['endpoint_tolerances']['position_m']==pytest.approx(.002)
    assert not proof['precision_certified'] and 'forward_path' in serv.numerics.calls
    assert proof_observation(proof,proposal=JointProposal(**args['proposal']),original=original,
        intent=intent,task=serv.task,now_ns=clock[0],max_age_ns=500_000_000)==latest


def with_left_image(obs, *, measured=False, received_ns=None, serial='f'*64):
    left=obs.frames[0].model_copy(update=dict(camera='left',frame_id='left_wrist_color_optical_frame',
        camera_info_sha256=serial,blob_sha256='a'*64,
        received_ns=obs.received_ns if received_ns is None else received_ns))
    objects=obs.objects
    if measured:
        sources=tuple(f.blob_sha256 for f in obs.frames if f.camera=='head')
        objects=(objects[0],objects[1].model_copy(update={'evidence_ids': (*sources,left.blob_sha256)}))
    return obs.model_copy(update=dict(objects=objects,frames=(*obs.frames,left)))


@pytest.mark.parametrize('old_left,new_left', [(True,False),(False,True),(True,True)])
def test_unreferenced_left_rgb_does_not_change_plan_or_delay_proof(scenario,old_left,new_left):
    from f1_harness.revalidation import preview_submission_key
    serv,args,clock,original,latest,intent=scenario
    if old_left:original=with_left_image(original,serial='d'*64)
    if new_left:latest=with_left_image(latest,received_ns=args['after_ns']-1)
    args.update(original_observation=original.model_dump(),observation=latest.model_dump())
    proof=serv.dispatch('revalidate_plan',args)
    assert proof['observation_sha256']==digest(latest)  # Raw optional frames remain in the proof.
    assert proof['geometry_checks']['swept_segments']>0
    assert proof_observation(proof,proposal=JointProposal(**args['proposal']),original=original,
        intent=intent,task=serv.task,now_ns=clock[0],max_age_ns=500_000_000)==latest
    transfer=intent.model_copy(update={'skill':'transfer_above_hole'})
    assert preview_submission_key(serv.task,{},transfer,original)==preview_submission_key(serv.task,{},transfer,latest)


def test_cross_view_aperture_requires_current_bound_left_rgb(scenario):
    from f1_harness.contracts import HarnessError
    from f1_harness.revalidation import _measurement_identity,_measurement_times,fresh_after,RevalidationSceneChanged
    serv,args,clock,original,latest,_=scenario
    old=with_left_image(original,measured=True)
    new=with_left_image(latest,measured=True)
    def compare(a,b): return _measurement_identity(a,b,task=serv.task,skill='align')
    compare(old,new)
    with pytest.raises(HarnessError,match='camera_identity_changed'):
        compare(old,with_left_image(latest,measured=True,serial='d'*64))
    assert not fresh_after(with_left_image(latest,measured=True,received_ns=args['after_ns']),
                           args['after_ns'],task=serv.task,skill='align')
    missing=new.model_copy(update={'frames':latest.frames})
    with pytest.raises(RevalidationSceneChanged,match='scene_evidence_changed'):
        _measurement_times(missing,task=serv.task,now_ns=clock[0],skill='align')
    direct=latest.model_copy(update={'objects': (latest.objects[0],latest.objects[1].model_copy(
        update={'evidence_ids':tuple(f.blob_sha256 for f in latest.frames if f.camera=='head')}))})
    assert compare(old,direct) and compare(direct,new)
    with pytest.raises(HarnessError,match='camera_identity_changed'):
        compare(old,with_left_image(direct,serial='d'*64))


def test_structured_transport_sweep_rejection_is_a_changed_scene(scenario, monkeypatch):
    from f1_harness import revalidation
    from f1_harness.contracts import HarnessError
    serv, args, *_ = scenario
    original_propose = revalidation.propose
    calls = []

    def changed_route(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise HarnessError('scene_sweep_blocked:' + json.dumps(dict(diagnostic=dict(
                code='scene_sweep_blocked', metrics=dict(route_family='demonstrated_transport',
                    segment_index=1, obstacle_role='bucket', obstacle_name='wall_x_low')))))
        return original_propose(*a, **kw)

    monkeypatch.setattr(revalidation, 'propose', changed_route)
    with pytest.raises(revalidation.RevalidationSceneChanged,
                       match='revalidation_actual_path_sweep_blocked'):
        revalidate_candidate(serv, **args)


def test_registered_transport_revalidates_its_transport_endpoint():
    from scipy.spatial.transform import Rotation
    clock = [1_000_000_000]
    task = Task(task_id='task', object_id='block', hole_id='hole', calibration_id='coarse',
                admission_profile='development_coarse')
    serv = service(clock, task)
    serv.fixture.update(transfer_policy='registered_transport',
                        candidate_order={'transfer_above_hole': [1, 0, 2, 3]})
    attachment = pose(0, 0, -.1)
    attachment[:3, :3] = Rotation.from_euler('y', 25, degrees=True).as_matrix()

    def loaded_observation(name):
        obs = observation(clock[0], name=name)
        obj = obs.objects[0].model_copy(update=dict(held=True,
            pose=tuple((scene().base_eef @ attachment).ravel()),
            eef_object_pose=tuple(attachment.ravel())))
        return obs.model_copy(update=dict(objects=(obj, obs.objects[1])))

    original = loaded_observation('loaded')
    intent = SkillIntent(request_id='transport', session_id='session', owner_instance_id='owner',
        task_id=task.task_id, task_hash=digest(task), epoch=1, observation_id=original.observation_id,
        scene_version=original.scene_version, calibration_id=task.calibration_id, skill='transfer_above_hole')
    candidate = serv.dispatch('plan_skill', dict(intent=intent.model_dump(),
        observation=original.model_dump(), full22=[0.]*22, held16=list(original.state16)))
    endpoint = np.array(candidate['geometry_trace'][-1]['goals'][-1])
    assert not np.allclose((endpoint @ attachment)[:3, :3], scene().base_hole[:3, :3])
    after = clock[0]
    clock[0] += 100_000_000
    latest = loaded_observation('fresh-loaded')
    proof = revalidate_candidate(serv, intent=intent.model_dump(), proposal=candidate,
        original_observation=original.model_dump(), observation=latest.model_dump(),
        full22=[0.]*22, held16=list(original.state16), after_ns=after)
    assert proof['geometry_checks']['swept_segments'] > 0
    assert proof['geometry_checks']['endpoint_tolerances']['free_space'] is True


def test_redundancy_preference_is_forwarded_and_does_not_claim_historical_ik(scenario,monkeypatch):
    serv,args,_,original,_,intent=scenario
    serv.fixture['redundancy_preference']='wrist_center_nullspace'
    serv.posture_prior=dict(source_run='history',postures_deg={'approach_grasp':[0]*7})
    saved=[];call=serv.numerics.call
    def capture(method,**kwargs):
        if method=='solve_path':saved.append(kwargs['scene'])
        return call(method,**kwargs)
    monkeypatch.setattr(serv.numerics,'call',capture)
    geometry=serv._scene(original,args['full22'],args['held16'])
    candidate=serv._solve_candidate(intent,geometry,args['held16'],args['full22'],0)
    assert saved and all(s['redundancy_preference']=='wrist_center_nullspace' for s in saved)
    assert all('posture_reference_deg' not in s for s in saved)
    assert all(s['redundancy_preference']=='wrist_center_nullspace' and s['posture_prior_source'] is None
               for s in candidate['geometry_trace'])
    # Numeric preference changes neither the saved path nor post-plan geometry checks.
    args['proposal']=candidate
    assert revalidate_candidate(serv,**args)['geometry_checks']['stages']>0


def test_revalidation_and_tracking_keep_selected_pregrasp_distance(scenario,monkeypatch):
    import f1_harness.skills as skills
    serv,args,clock,original,latest,intent=scenario
    serv.fixture['pregrasp_distances_m']=[.08,.06]
    serv.fixture['free_space_tracking_tolerance_deg']=1.
    monkeypatch.setattr(skills,'_motion_clear',lambda s,a,b,*rest,**kw:b[2,3]<.37)
    scene_value=serv._scene(original,args['full22'],args['held16'])
    candidate=serv._solve_candidate(intent,scene_value,args['held16'],args['full22'],0)
    assert candidate['geometry_trace'][0]['pregrasp_distance_m']==.06
    args['proposal']=candidate
    monkeypatch.setattr(skills,'_motion_clear',lambda *a,**kw:True)
    assert revalidate_candidate(serv,**args)['geometry_checks']['stages']==1
    checked=serv.check_waypoint(intent=intent.model_dump(),proposal=candidate,
        observation=original.model_dump(),full22=args['full22'],measured16=candidate['targets'][0],index=0)
    assert checked['accepted'] and checked['position_m']==0.
    candidate['geometry_trace'][0]['pregrasp_distance_m']=.04
    with pytest.raises(RuntimeError,match='unregistered_pregrasp_distance'):
        revalidate_candidate(serv,**args)


def test_open_approach_does_not_replan_on_missing_held_evidence(scenario):
    serv,args,clock,original,latest,intent=scenario
    changed=latest.model_copy(update={'objects':(
        latest.objects[0].model_copy(update={'held':None}),latest.objects[1])})
    args['observation']=changed.model_dump()
    proof=revalidate_candidate(serv,**args)
    assert proof['observation']['objects'][0]['held'] is None
    # Both remote geometry and cheap owner checks use the same stage semantics.
    assert proof_observation(proof,proposal=JointProposal(**args['proposal']),original=original,
        intent=intent,task=serv.task,now_ns=clock[0],max_age_ns=500_000_000,
        live_observation=latest)==changed


@pytest.mark.parametrize('mutation',['closed','invalid_feedback','attachment','confirmed_held','missing_config'])
def test_open_approach_exception_requires_measured_open_unattached_state(scenario,mutation):
    serv,args,_,_,latest,_=scenario
    obj=latest.objects[0].model_copy(update={'held':None})
    changes={'objects':(obj,latest.objects[1])}
    if mutation=='closed':changes['state16']=(*latest.state16[:15],100.)
    elif mutation=='invalid_feedback':changes['state_valid_mask']=(*latest.state_valid_mask[:15],False)
    elif mutation=='attachment':changes['objects']=(obj.model_copy(update={'eef_object_pose':tuple(pose().ravel())}),latest.objects[1])
    elif mutation=='confirmed_held':changes['objects']=(obj.model_copy(update={'held':True}),latest.objects[1])
    else:serv.fixture.pop('gripper_tolerance')
    args['observation']=latest.model_copy(update=changes).model_dump()
    if mutation=='invalid_feedback':
        with pytest.raises(ValueError,match='fresh measured gripper required'):
            revalidate_candidate(serv,**args)
        return
    with pytest.raises(RuntimeError,match='object_or_hole_changed') as error:
        revalidate_candidate(serv,**args)
    diagnostic=json.loads(str(error.value).split(':',1)[1])['diagnostic']
    assert 'held' in diagnostic['metrics']['changed_fields']


@pytest.mark.parametrize('skill',['grasp','regrasp','transfer_above_hole','align','insert','release','retreat'])
def test_held_uncertainty_exception_never_crosses_contact_or_loaded_stages(scenario,skill):
    from f1_harness.revalidation import _scene_unchanged
    serv,_,_,original,latest,_=scenario
    changed=latest.model_copy(update={'objects':(
        latest.objects[0].model_copy(update={'held':None}),latest.objects[1])})
    with pytest.raises(RuntimeError,match='object_or_hole_changed'):
        _scene_unchanged(original,changed,task=serv.task,skill=skill,
            threshold=tolerances(serv.fixture,'approach_grasp'))


@pytest.mark.parametrize('skill', ['transfer_above_hole', 'move_tcp', 'align', 'insert', 'release', 'retreat'])
def test_unpublished_loaded_path_waits_only_for_missing_attachment_measurement(scenario, skill):
    from f1_harness.revalidation import attachment_measurement_pending
    serv, _, _, original, latest, _ = scenario
    target = original.objects[0].model_copy(update=dict(held=True, eef_object_pose=tuple(pose().ravel())))
    loaded = original.model_copy(update=dict(objects=(target, original.objects[1])))
    def measurement(held, **changes):
        update = dict(held=held, eef_object_pose=target.eef_object_pose if held is True else None)
        obj = target.model_copy(update=update | changes)
        return latest.model_copy(update=dict(objects=(obj, latest.objects[1])))
    def pending(obs):
        return attachment_measurement_pending(loaded, obs, task=serv.task, skill=skill)
    assert pending(measurement(None))
    assert pending(measurement(True,eef_object_pose=None))
    assert not pending(measurement(True))
    assert not pending(measurement(False))
    assert not pending(measurement(None, frame_id='other'))
    assert not pending(measurement(None).model_copy(update=dict(host_boot_id='other')))
    assert not pending(measurement(None).model_copy(update=dict(faults=('invalid_frame',))))
    assert not pending(measurement(None, eef_object_pose=target.eef_object_pose))
    assert not attachment_measurement_pending(original, measurement(None), task=serv.task, skill=skill)


@pytest.mark.parametrize('turns',[1,2,3])
def test_square_axis_branch_changes_preserve_goals_sweep_and_raw_evidence(scenario,turns):
    from f1_harness.geometry import transform
    serv,args,clock,original,latest,intent=scenario
    angle=turns*np.pi/2;s=np.eye(4);s[:2,:2]=[[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]]
    objects=tuple(o.model_copy(update={'pose':tuple((transform(o.pose)@s).ravel())}) for o in latest.objects)
    changed=latest.model_copy(update={'objects':objects})
    args['observation']=changed.model_dump()
    proof=revalidate_candidate(serv,**args)
    assert proof['observation_sha256']==digest(changed) and proof['observation']==changed.model_dump()
    assert proof['geometry_checks']['swept_segments']>0
    assert proof_observation(proof,proposal=JointProposal(**args['proposal']),original=original,
        intent=intent,task=serv.task,now_ns=clock[0],max_age_ns=500_000_000)==changed


def test_phase_tolerance_keeps_hole_engagement_bound(scenario):
    serv,*_=scenario
    serv.fixture['revalidation']['xy_tolerance_m']=.005
    assert tolerances(serv.fixture,'approach_grasp')['position_m']==pytest.approx(.005)
    assert tolerances(serv.fixture,'insert')['position_m']==pytest.approx(.002)


def test_free_space_scene_and_goal_share_margin_but_hole_engagement_stays_precise(scenario):
    serv,args,*_=scenario
    serv.fixture['planning_margin_m']=.015
    serv.fixture['revalidation']['free_space_goal_tolerance_m']=.015
    serv.numerics.offset=.004
    proof=revalidate_candidate(serv,**args)
    bounds=proof['geometry_checks']['endpoint_tolerances']
    assert bounds['position_m']==pytest.approx(.015)
    assert bounds['goal_position_m']==pytest.approx(.015)
    assert tolerances(serv.fixture,'insert')['goal_position_m']==pytest.approx(.002)
    serv.fixture['revalidation']['free_space_goal_tolerance_m']=.02
    with pytest.raises(RuntimeError,match='exceeds_clearance'):revalidate_candidate(serv,**args)


@pytest.mark.parametrize('yaw,shift,accepted',[(.06,0.,True),(.09,0.,False),(.06,.02,False)])
def test_pregrasp_revalidation_measures_target_at_registered_fingertips(scenario,yaw,shift,accepted):
    from scipy.spatial.transform import Rotation
    serv,args,clock,original,latest,intent=scenario
    tip=pose(.3,0,0)
    serv.fixture.update(eef_tip_reference=tip.tolist(),object_grasp_eef=np.linalg.inv(tip).tolist(),
                        planning_margin_m=.015)
    serv.fixture['revalidation']['free_space_goal_tolerance_m']=.015
    saved=clock[0];clock[0]=original.received_ns
    args['proposal']=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),
        observation=original.model_dump(),full22=args['full22'],held16=args['held16']))
    clock[0]=saved
    obj=latest.objects[0];new_pose=np.array(obj.pose).reshape(4,4).copy()
    new_pose[:3,:3]=new_pose[:3,:3]@Rotation.from_euler('z',yaw).as_matrix();new_pose[0,3]+=shift
    args['observation']=latest.model_copy(update={'objects':(
        obj.model_copy(update={'pose':tuple(new_pose.ravel())}),latest.objects[1])}).model_dump()
    if accepted:
        proof=revalidate_candidate(serv,**args)
        assert proof['geometry_checks']['goal_reference']=='registered_fingertip'
        assert proof['geometry_checks']['swept_segments']>0
    else:
        with pytest.raises(RuntimeError,match='revalidation_object_or_hole_changed|revalidation_goal_changed'):
            revalidate_candidate(serv,**args)


def test_registered_long_transfer_rebuilds_identical_branch_for_revalidation():
    from scipy.spatial.transform import Rotation
    clock=[1_000_000_000]
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    serv=service(clock,task)
    current=scene().base_eef.copy();current[:3,:3]=Rotation.from_euler('x',90,degrees=True).as_matrix()
    class StagingNumerics(Numerics):
        def call(self,method,**kw):
            if method=='forward_pose':return current.tolist()
            return super().call(method,**kw)
    serv.numerics=StagingNumerics()
    serv.fixture.update(transfer_staging_pose=pose(-.2,-.3,.6).tolist(),transfer_rotation_path='opposite_shortest')
    original=observation(clock[0])
    obj=original.objects[0].model_copy(update={'held':True,'eef_object_pose':tuple(scene().eef_object.ravel())})
    original=original.model_copy(update={'objects':(obj,original.objects[1])})
    intent=SkillIntent(request_id='transfer',session_id='session',owner_instance_id='owner',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=original.observation_id,scene_version=original.scene_version,
        calibration_id=task.calibration_id,skill='transfer_above_hole')
    held=list(original.state16);full=[0.]*22
    candidate=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),full22=full,held16=held))
    clock[0]+=6_000_000_000;after=clock[0];clock[0]+=100_000_000
    latest=observation(clock[0],name='latest').model_copy(update={'objects':original.objects})
    proof=revalidate_candidate(serv,intent=intent.model_dump(),proposal=candidate,
        original_observation=original.model_dump(),observation=latest.model_dump(),full22=full,held16=held,after_ns=after)
    assert proof['geometry_checks']['stages']==1
    assert len(candidate['geometry_trace'][0]['goals'])==7
    # A completed sweep on the last held frame cannot authorize first output
    # against a newer image with an unresolved attachment.
    from f1_harness.revalidation import RevalidationEvidenceUpdated
    missing = latest.objects[0].model_copy(update=dict(held=None, eef_object_pose=None))
    newer = observation(clock[0]+1,name='newer').model_copy(update=dict(objects=(missing,latest.objects[1])))
    with pytest.raises(RevalidationEvidenceUpdated, match='revalidation_attachment_measurement_pending'):
        proof_observation(proof,proposal=JointProposal(**candidate),original=original,
            intent=intent,task=task,now_ns=clock[0]+1,max_age_ns=500_000_000,live_observation=newer)
    serv.fixture['transfer_rotation_path']='shortest'
    with pytest.raises(RuntimeError,match='stage_binding_mismatch'):
        revalidate_candidate(serv,intent=intent.model_dump(),proposal=candidate,
            original_observation=original.model_dump(),observation=latest.model_dump(),full22=full,held16=held,after_ns=after)


@pytest.mark.parametrize('mutation,reason',[
    ('object','goal_changed'),('held','object_or_hole_changed'),
    ('camera','camera_identity_changed'),('old_frame','post_planning_measurements_required'),
    ('old_state','post_planning_measurements_required'),('moving','stationary calibrated'),
    ('boot','measurement_identity_changed'),('endpoint','endpoint_outside_remaining_clearance'),
    ('attachment','attachment_changed'),('trace','stage_binding_mismatch'),
])
def test_changed_measurements_or_candidate_do_not_get_a_proof(scenario,mutation,reason):
    serv,args,_,_,latest,_=scenario
    if mutation in ('object','hole'):
        objects=list(latest.objects);index=mutation=='hole';m=pose();m[:]=np.array(objects[index].pose).reshape(4,4);m[0,3]+=.01
        objects[index]=objects[index].model_copy(update={'pose':tuple(m.ravel())})
        args['observation']=latest.model_copy(update={'objects':tuple(objects)}).model_dump()
    elif mutation in ('held','attachment'):
        obj=latest.objects[0].model_copy(update={'held':True} if mutation=='held' else {'eef_object_pose':tuple(pose().ravel())})
        args['observation']=latest.model_copy(update={'objects':(obj,latest.objects[1])}).model_dump()
    elif mutation=='camera':args['observation']['frames'][0]['camera_info_sha256']='f'*64
    elif mutation=='old_frame':args['observation']['frames'][0]['received_ns']=args['after_ns']
    elif mutation=='old_state':args['observation']['state_received_ns']=args['after_ns']
    elif mutation=='moving':args['observation']['stationary']=False
    elif mutation=='boot':args['observation']['host_boot_id']='other'
    elif mutation=='endpoint':serv.numerics.offset=.004
    elif mutation=='trace':args['proposal']['geometry_trace'][0]['start_index']=1
    with pytest.raises((ValueError,RuntimeError),match=reason):revalidate_candidate(serv,**args)


def test_actual_fk_sweep_is_checked_even_when_ideal_stage_is_clear(scenario):
    serv,args,_,_,_,_=scenario
    # Ideal route is far from this obstacle. A bad candidate FK enters it.
    serv.fixture['obstacles']=({'min':[.398,-.02,.275],'max':[.42,.02,.4]},)
    serv.numerics.offset=.2
    with pytest.raises(RuntimeError,match='actual_path_sweep_blocked'):
        revalidate_candidate(serv,**args)


def test_coarse_proof_age_is_advisory_but_tampering_rejected(scenario):
    serv,args,clock,original,_,intent=scenario
    proof=revalidate_candidate(serv,**args)
    plan=JointProposal(**args['proposal'])
    with pytest.raises(RuntimeError,match='binding_mismatch'):
        proof_observation(proof|{'epoch':2},proposal=plan,original=original,intent=intent,task=serv.task,
                          now_ns=clock[0],max_age_ns=500_000_000)
    clock[0]+=500_000_001
    checked=proof['checked_at_ns']
    latest=proof_observation(proof,proposal=plan,original=original,intent=intent,task=serv.task,
                            now_ns=clock[0],max_age_ns=500_000_000)
    assert latest.received_ns==args['observation']['received_ns'] and proof['checked_at_ns']==checked
    revalidate_candidate(serv,**args)


def test_remaining_clearance_is_not_tracking_tolerance(scenario):
    serv,*_=scenario
    serv.fixture['revalidation']['xy_tolerance_m']=.005
    assert tolerances(serv.fixture)['position_m']==pytest.approx(.002)
    serv.fixture['object_planning_margin_m']=.003
    with pytest.raises(RuntimeError,match='clearance_and_tolerances'):tolerances(serv.fixture)


def make_owner(tmp_path):
    clock=[1_000_000_000]
    engine=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:clock[0],lease_ms=20000)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    session=engine.open_session()['session_id'];engine.prepare_task(session,task.model_dump())
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    original=observation(clock[0],boot=engine.host_boot_id)
    engine.update_observation(original)
    intent=SkillIntent(request_id='request',session_id=session,owner_instance_id=engine.instance,task_id=task.task_id,
        task_hash=digest(task),epoch=engine.epoch,observation_id=original.observation_id,scene_version=original.scene_version,
        calibration_id=task.calibration_id,skill='approach_grasp')
    engine.submit_skill(intent.model_dump())
    serv=service(clock,task)
    def plan(intent,obs,full,held):
        return JointProposal(**serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=full,held16=held)))
    def revalidate(intent,proposal,original,latest,full,held,after):
        return revalidate_candidate(serv,intent=intent.model_dump(),proposal=asdict(proposal),
            original_observation=original.model_dump(),observation=latest.model_dump(),full22=full,held16=held,after_ns=after)
    bridge=OwnerBridge(engine,numerical_planner=plan,result_verifier=lambda *args:None,clock=lambda:clock[0],
        feedback_age_ns=200_000_000,reference_tolerance_deg=.5,stop_gripper_delta=.1,candidate_revalidator=revalidate)
    bridge.executor.shutdown();bridge.executor=ControlledExecutor()
    state=dict(full22=[0.]*22,measured16=list(original.state16),held16=list(original.state16))
    def poll():return bridge.poll(**state,stamps16=(clock[0],)*16)
    return engine,bridge,serv,clock,state,poll,original


@pytest.mark.parametrize('fault',[None,'invalidated','drift','cancel','proof_hash'])
def test_owner_keeps_holding_until_independent_new_proof(tmp_path,fault):
    engine,bridge,serv,clock,state,poll,original=make_owner(tmp_path)
    try:
        assert poll() is None
        future,func,args=bridge.executor.jobs[-1]
        candidate=func(*args)
        clock[0]+=8_000_000_000;future.set_result(candidate)
        assert poll() is None and bridge.proposal is None
        assert bridge.plan_observation==original and len(bridge.executor.jobs)==1
        # A new wrapper over old image/state cannot unlock the candidate.
        engine.live_observation=original.model_copy(update={'received_ns':clock[0]+1})
        assert poll() is None and len(bridge.executor.jobs)==1
        clock[0]+=100_000_000
        latest=observation(clock[0],boot=engine.host_boot_id,name='fresh-after-IK')
        engine.update_live_observation(latest,engine.status()['perception_context'])
        assert poll() is None and len(bridge.executor.jobs)==2
        future,func,args=bridge.executor.jobs[-1];proof=func(*args)
        if fault=='invalidated':engine.live_observation=None
        elif fault=='drift':state['measured16'][7]=.6
        elif fault=='cancel':bridge.revoke('user_stop',preempted=True)
        elif fault=='proof_hash':proof['candidate_sha256']='bad'
        if not future.cancelled():future.set_result(proof)
        target=poll()
        if fault:
            assert target is None and not engine.authorized and bridge.revalidation_proof is None
        else:
            assert target is not None and engine.authorized
            assert bridge.plan_observation==original and bridge.revalidation_proof['observation_sha256']==digest(latest)
            events=[json.loads(line) for line in (engine.store.root/'trace.jsonl').read_text().splitlines()]
            assert any(e['kind']=='harness_plan_revalidated' for e in events)
            clock[0]+=500_000_001
            assert bridge.validate_first_publication(**state,stamps16=(clock[0],)*16,now_ns=clock[0])
            assert engine.authorized and bridge.proposal is not None
            assert engine.results['request'].lifecycle=='running'
            assert bridge.revalidation_proof['observation_sha256']==digest(latest)
            assert not engine.request_publications  # Admission itself never publishes.
    finally:bridge.close()


def test_numeric_stage_failure_details_reach_owner_trace_without_private_fields(tmp_path):
    engine,bridge,serv,_,_,poll,_=make_owner(tmp_path)
    original_call=serv.numerics.call
    def fail(method,**kwargs):
        if method=='solve_path':
            detail=dict(goal_index=2,iterations=300,position_error_m=.017,rotation_error_rad=.11,
                final_arm_deg=[1.,2.,3.,4.,5.,6.,7.],goal=np.eye(4).tolist(),
                limits=dict(min_constraint_margin_rad=.001,active_planes=[3,6],api_key='fixture-secret'),
                credential='fixture-secret',prompt='private input',debug='x'*2000)
            raise RuntimeError('ValueError: bounded_IK_did_not_reach_goal:'+json.dumps(detail))
        return original_call(method,**kwargs)
    serv.numerics.call=fail
    try:
        assert poll() is None
        future,function,args=bridge.executor.jobs[-1]
        try:function(*args)
        except Exception as exc:future.set_exception(exc)
        assert poll() is None
        assert engine.results['request'].reason=='numerical_plan_rejected:registered_candidates_exhausted'
        assert engine.results['request'].no_motion and not engine.results['request'].retryable
        trace=(engine.store.root/'trace.jsonl').read_text()
        started=next(row for row in map(json.loads,trace.splitlines()) if row['kind']=='harness_planning_started')
        assert started['observation']==args[1].model_dump(mode='json')
        assert started['full_reference']==list(args[2]) and started['held_reference']==list(args[3])
        assert started['request_id']=='request'
        event=next(row for row in map(json.loads,trace.splitlines()) if row['kind']=='harness_skill_computation_failed')
        assert event['request_id']=='request' and event['skill']=='approach_grasp' and event['phase']=='plan'
        diagnostic=event['diagnostic'];metrics=diagnostic['metrics']
        # The same bounded cause must reach the skill caller, not only an
        # owner log unavailable to a planning model during this request.
        result=engine.results['request']
        assert result.planning_diagnostic==diagnostic
        assert result.model_dump(mode='json')['planning_diagnostic']==diagnostic
        assert diagnostic['code']=='registered_candidates_exhausted'
        assert metrics['stage_index']==0 and metrics['goal_index']==2
        assert metrics['position_error_m']==.017 and metrics['final_arm_deg']==[1.,2.,3.,4.,5.,6.,7.]
        assert metrics['limits']['active_planes']==[3,6]
        assert 'fixture-secret' not in trace and 'private input' not in trace and 'credential' not in trace
        assert len(json.dumps(diagnostic))<4096
    finally:bridge.close()


def test_fresh_scene_route_rejection_is_replannable_not_control_fault(scenario,monkeypatch):
    import f1_harness.revalidation as module
    from f1_harness.contracts import HarnessError
    serv,args,*_=scenario;original=module.propose;calls=[]
    def changed(*a,**kw):
        calls.append(1)
        if len(calls)==2:raise HarnessError('scene_sweep_blocked')
        return original(*a,**kw)
    monkeypatch.setattr(module,'propose',changed)
    with pytest.raises(module.RevalidationSceneChanged,match='revalidation_actual_path_sweep_blocked'):
        module.revalidate_candidate(serv,**args)


@pytest.mark.parametrize('delta,accepted',[(1e-15,True),(1e-6,False)])
def test_recomputed_goal_roundoff_keeps_exact_proposal_identity(scenario,delta,accepted):
    serv,args,*_=scenario
    args['proposal']['geometry_trace'][0]['goals'][0][0][3]+=delta
    if accepted:assert revalidate_candidate(serv,**args)['candidate_sha256']
    else:
        with pytest.raises(RuntimeError,match='stage_binding_mismatch'):
            revalidate_candidate(serv,**args)


def test_fresh_free_space_detour_does_not_change_frozen_candidate_identity(scenario,monkeypatch):
    import f1_harness.revalidation as module
    serv,args,*_=scenario;original=module.propose;calls=[]
    def new_detour(*a,**kw):
        value=original(*a,**kw);calls.append(1)
        if len(calls)==2:
            stage=value['stages'][0]
            stage['goals']=[serv.numerics.call('forward_pose',full22=args['full22'],held16=args['held16']),*stage['goals']]
        return value
    monkeypatch.setattr(module,'propose',new_detour)
    proof=module.revalidate_candidate(serv,**args)
    assert proof['geometry_checks']['swept_segments']==len(args['proposal']['targets'])


def test_frozen_approach_path_can_pass_when_new_endpoint_route_is_blocked(scenario):
    from f1_harness.skills import propose
    serv,args,clock,original,latest,intent=scenario
    # The existing path stays just left of a neighbour. A 1 mm target update
    # would make a newly planned endpoint overlap its padded edge, while the
    # frozen path remains clear and within the existing 2 mm goal tolerance.
    serv.fixture['obstacles']=[dict(min=[.2352,-.1,.35],max=[.3,.1,.4],role='neighbour')]
    changed=latest.model_copy(update={'objects':(
        latest.objects[0].model_copy(update={'pose':tuple(pose(.201,0,.2).ravel())}),latest.objects[1])})
    args['observation']=changed.model_dump()
    updated=serv._scene(changed,args['full22'],args['held16'])
    with pytest.raises(RuntimeError,match='scene_sweep_blocked'):
        propose('approach_grasp',updated)
    proof=revalidate_candidate(serv,**args)
    assert proof['geometry_checks']['swept_segments']==len(args['proposal']['targets'])
    # A real FK path entering the same neighbour must still be rejected.
    serv.numerics.offset=.04
    with pytest.raises(RuntimeError,match='revalidation_actual_path_sweep_blocked'):
        revalidate_candidate(serv,**args)


def test_grasp_goal_and_arrival_do_not_borrow_destination_hole_gap(scenario):
    serv,*_=scenario
    serv.fixture['effective_clearance_m']=.0026950206417194096
    for skill in ('grasp','regrasp'):
        values=tolerances(serv.fixture,skill)
        assert values['goal_position_m']==values['position_m']==.002
    assert tolerances(serv.fixture,'insert')['goal_position_m']==pytest.approx(.0016950206417194096)


@pytest.mark.parametrize('warnings,starts',[
    (('stationary_history_incomplete','measurement_gap'),True),
    (('stationary_history_incomplete','observed_motion'),False),
    ((),False),
])
def test_coarse_owner_revalidation_uses_shared_quality_policy(tmp_path,warnings,starts):
    engine,bridge,serv,clock,state,poll,original=make_owner(tmp_path)
    try:
        poll();future,func,args=bridge.executor.jobs[-1]
        candidate=func(*args);clock[0]+=1_000_000_000;future.set_result(candidate);poll()
        clock[0]+=600_000_000
        latest=observation(clock[0],boot=engine.host_boot_id,name='slow-visual-frame').model_copy(
            update=dict(stationary=False,quality_warnings=warnings))
        engine.update_live_observation(latest,engine.status()['perception_context']);poll()
        assert len(bridge.executor.jobs)==(2 if starts else 1)
        if starts:
            future,func,args=bridge.executor.jobs[-1];future.set_result(func(*args))
            assert poll() is not None
        assert not engine.request_publications
    finally:bridge.close()
