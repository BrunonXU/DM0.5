"""Fresh measurements can validate a fixed candidate; they never refresh its old snapshot."""
from dataclasses import asdict,replace
import math

import numpy as np

from .contracts import HarnessError, Observation, SkillIntent, digest
from .geometry import transform, align_square_pose, tool_margin_departure_clear
from .observation import require_geometry_identity, geometry_age_limit_ns, stationary_admitted
from .owner_bridge import JointProposal
from .skills import propose, _motion_clear, rebase_support_lift
from .local_environment import local_scope


SCHEMA = 'f1-harness-plan-revalidation-v1'
TRANSPORT_MEASUREMENT_PENDING = 'revalidation_transport_measurement_pending'


class RevalidationExpired(HarnessError):
    """Only temporal age failed after identity and candidate geometry were checked."""


class RevalidationSceneChanged(HarnessError):
    """Reject an unpublished candidate, not the owner's physical control loop."""


class RevalidationEvidenceUpdated(HarnessError):
    """Keep a fixed unpublished candidate; asynchronously sweep its new evidence."""


def _planning_frames(observation, task, skill):
    """The auxiliary left RGB is a measurement only when the current geometry used it."""
    from .geometry_context import planning_entity_ids
    if task is None or task.admission_profile != 'development_coarse':
        return observation.frames
    required = planning_entity_ids(task, observation, skill)
    evidence = {blob for obj in observation.objects if obj.object_id in required
                for blob in obj.evidence_ids}
    if observation.fixture is not None:
        evidence.update(observation.fixture.evidence_ids)
    return tuple(frame for frame in observation.frames
                 if (frame.camera, frame.stream) != ('left', 'rgb') or frame.blob_sha256 in evidence)


def _check_aperture_frame_binding(observation, task, skill, frames):
    # The registered cross-view producer records (head RGB, head depth, left
    # RGB) on the hole. An absent or different third image cannot prove it.
    from .geometry_context import planning_entity_ids
    if task.hole_id not in planning_entity_ids(task, observation, skill):
        return
    hole = next((obj for obj in observation.objects if obj.object_id == task.hole_id), None)
    if hole is None or len(hole.evidence_ids) != 3 or hole.source in ('cached_pre_transport_aperture', 'static_return_anchor'):
        return
    by_stream = {(frame.camera, frame.stream): frame for frame in frames}
    head = (by_stream.get(('head','rgb')), by_stream.get(('head','depth')))
    if all(head) and hole.evidence_ids[:2] == tuple(frame.blob_sha256 for frame in head):
        left = by_stream.get(('left', 'rgb'))
        if left is None or left.blob_sha256 != hole.evidence_ids[2]:
            raise RevalidationSceneChanged('revalidation_scene_evidence_changed')


def align_observation(original,latest):
    """A derived coordinate view only; proof hashes always bind raw observations."""
    old={obj.object_id:obj for obj in original.objects};objects=[]
    for obj in latest.objects:
        before=old.get(obj.object_id)
        if before and before.shape==obj.shape=='square' and before.pose and obj.pose:
            pose,symmetry=align_square_pose(obj.pose,before.pose)
            changes=dict(pose=tuple(pose.ravel()))
            if obj.eef_object_pose is not None:
                changes['eef_object_pose']=tuple((transform(obj.eef_object_pose)@symmetry).ravel())
            obj=obj.model_copy(update=changes)
        objects.append(obj)
    return latest.model_copy(update={'objects':tuple(objects)})


def preview_submission_key(task,fixture,intent,observation):
    """Identity of a free-space request, never a geometry equivalence claim."""
    if (task.admission_profile!='development_coarse' or task.goal!='placed'
            or intent.skill not in ('move_tcp','transfer_above_hole')
            or intent.skill=='move_tcp' and (intent.parameters.target_frame!='world'
                or intent.parameters.support_contact)):
        return None
    target=next((o for o in observation.objects if o.object_id==task.object_id),None)
    if target is None or target.pose is None or not target.visible:return None
    def plain(value):
        if isinstance(value,np.ndarray):return value.tolist()
        if isinstance(value,np.generic):return value.item()
        if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [plain(v) for v in value]
        return value
    return digest(dict(task=digest(task),fixture=digest(plain(fixture)),skill=intent.skill,
        scope={k:getattr(intent,k) for k in ('session_id','owner_instance_id','epoch','calibration_id')},
        acquisition=observation.acquisition_instance,boot=observation.host_boot_id,
        geometry_scope=observation.geometry_scope,geometry_camera=observation.geometry_camera,
        target=dict(object_id=target.object_id,shape=target.shape,held=target.held,
                    attached=target.eef_object_pose is not None),
        cameras=[{key:getattr(f,key) for key in ('camera','stream','frame_id','width','height',
            'encoding','depth_scale_m','camera_info_sha256','clock_domain','source_epoch','publisher_id')}
            for f in _planning_frames(observation, task, intent.skill)],parameters=intent.parameters.model_dump(),
        candidate_start=intent.candidate_start,excluded_candidates=intent.excluded_candidates))


def rebind_preview_candidate(service,intent,observation,scene,full,held,preview):
    """Submit the preview's joint path to the existing fresh proof machinery.

    No old pose or attachment is installed in the current scene. Reconstruct
    the current semantic goals, preserve the candidate right-arm coordinates,
    and retime from current held output. This function grants no feasibility or
    motion: the owner still checks the actual connecting segment, every FK
    segment, native constraints and the current endpoint before publication.
    """
    from copy import deepcopy
    from .joint_motion import uses_joint_motion,reconstruction
    from .trajectory import schedule,reference_speed
    result=deepcopy(preview['candidate'])
    proposal=JointProposal(**result)
    old=np.asarray(preview['held16'],float);anchor=np.asarray(held,float)
    # A different physical finger setting is a different manipulation state;
    # do not turn preview reuse into an implicit gripper command.
    if (old.shape!=(16,) or anchor.shape!=(16,)
            or abs(old[15]-anchor[15])>service.fixture['gripper_tolerance']):return None
    try:
        stages=(reconstruction(scene,intent,proposal) if uses_joint_motion(service.fixture,intent,proposal)
                else propose(intent.skill,scene,variant=proposal.candidate_variant))['stages']
    except (HarnessError,ValueError):
        return None  # Current scene cannot even express this old route; plan anew.
    trace=result['geometry_trace']
    if len(stages)!=len(trace):return None
    for stage,entry in zip(stages,trace):
        if (entry.get('semantic_stage')!=stage.get('name') or stage.get('gripper') is not None
                or stage.get('allow_target_contact') or stage.get('support_contact_tolerance_m') is not None):
            return None
        entry['goals']=deepcopy(stage['goals'])
    targets=np.asarray(result['targets'],float)
    targets[:,:7]=anchor[:7];targets[:,14:]=anchor[14:]
    result.update(request_id=intent.request_id,epoch=intent.epoch,scene_version=intent.scene_version,
        calibration_id=intent.calibration_id,full_reference=tuple(full),
        targets=tuple(tuple(row) for row in targets))
    if result.get('trajectory') is not None:
        result['trajectory']=schedule(targets,anchor,trace,reference_speed(service.fixture,intent.skill))
    if trace:
        trace[0]['reused_numerical_candidate']=dict(source_request_id=preview['source_request_id'],
            source_observation_id=preview['observation_id'],source_candidate_sha256=candidate_hash(proposal),
            current_observation_id=observation.observation_id,current_goals_reconstructed=True,
            fresh_revalidation_required=True,native_path_revalidation_required=True,motion_authorized=False)
        if trace[0].get('candidate_evaluation'):
            trace[0]['candidate_evaluation'].update(request_id=intent.request_id,
                observation_id=observation.observation_id,epoch=intent.epoch)
    return result


def _measurement_times(observation,*,task,now_ns,skill=None):
    cameras=require_geometry_identity(observation,goal=task.goal,admission_profile=task.admission_profile,
                                      skill=skill,hole_id=task.hole_id)
    frames = _planning_frames(observation, task, skill)
    _check_aperture_frame_binding(observation, task, skill, frames)
    times=(observation.received_ns,observation.state_received_ns,
           *(f.received_ns for f in frames
             if f.camera in cameras or (f.camera, f.stream) == ('left', 'rgb')))
    if any(stamp is None or stamp>now_ns for stamp in times):
        raise HarnessError('revalidation_measurement_clock_invalid')
    return times


def _require_current_measurements(observation,*,task,now_ns,max_age_ns,skill=None):
    times=_measurement_times(observation,task=task,now_ns=now_ns,skill=skill)
    age_limit=geometry_age_limit_ns(max_age_ns,admission_profile=task.admission_profile,
                                    geometry_scope=observation.geometry_scope)
    if age_limit is not None and any(now_ns-stamp>age_limit for stamp in times):
        raise RevalidationExpired('revalidation_measurements_expired')


def require_planning_geometry(observation,*,intent,task,now_ns,max_age_ns,candidate_only=False):
    """Planning ingress checks identity first; age alone may reject zero motion."""
    if (task is None or intent.task_id!=task.task_id or intent.task_hash!=digest(task)
            or intent.skill not in task.allowed_skills
            or intent.calibration_id!=task.calibration_id or observation.calibration_id!=task.calibration_id
            or intent.observation_id!=observation.observation_id or intent.scene_version!=observation.scene_version):
        raise HarnessError('plan_binding_mismatch')
    objects={o.object_id:o for o in observation.objects}
    if observation.geometry_scope=='coarse_local_environment' or local_scope(observation) and intent.skill in ('move_tcp','reobserve'):
        from .local_environment import require_local_evidence
        require_local_evidence(task,observation,motion=intent.skill=='move_tcp')
        if (intent.skill not in ('move_tcp','reobserve') or intent.skill=='move_tcp' and (
                intent.parameters.target_frame!='world' or intent.parameters.support_contact)):
            raise HarnessError('local_environment_requires_world_noncontact_motion')
    from .geometry_context import planning_entity_ids
    keys=planning_entity_ids(task,observation,intent.skill)
    if any(key not in objects or objects[key].pose is None for key in keys):
        import json
        from .geometry_context import entity_status
        raise HarnessError('missing_geometry_entities:'+json.dumps(entity_status(task,observation),separators=(',',':')))
    if any(objects[key].shape!=task.shape for key in keys):
        raise HarnessError('object_hole_shape_mismatch')
    if keys and len({objects[key].frame_id for key in keys})!=1:
        raise HarnessError('native_frame_chain_not_registered')
    if candidate_only:
        # A candidate is not motion. Validate its identities and clock here;
        # the owner requires a new post-compute scene proof before publication.
        _measurement_times(observation,task=task,now_ns=now_ns,skill=intent.skill)
        return
    try:_require_current_measurements(observation,task=task,now_ns=now_ns,max_age_ns=max_age_ns,skill=intent.skill)
    except RevalidationExpired:
        raise RevalidationExpired('planning_measurements_expired') from None


def candidate_hash(proposal):
    return digest(asdict(proposal))


def _positive(value):
    return (isinstance(value,(int,float)) and not isinstance(value,bool)
            and math.isfinite(value) and value>0)


def tolerances(fixture,skill=None):
    cfg=fixture.get('revalidation') or {}
    xy,yaw,tilt=(cfg.get(key) for key in ('xy_tolerance_m','yaw_tolerance_rad','tilt_tolerance_rad'))
    clearance=fixture.get('effective_clearance_m')
    margin=fixture.get('object_planning_margin_m')
    if not all(_positive(v) for v in (xy,yaw,tilt,clearance,margin)) or margin>=clearance:
        raise HarnessError('explicit_revalidation_clearance_and_tolerances_required')
    # Free-space approaches use the configured scene/goal tolerance and actual
    # swept clearance. Only hole engagement is limited by remaining hole gap.
    engagement=skill in (None,'align','insert','release')
    free_space=skill in ('approach_grasp','transfer_above_hole','retreat','withdraw_reset','move_tcp','recover_grasp')
    goal_position=xy
    if free_space and 'free_space_goal_tolerance_m' in cfg:
        requested=cfg['free_space_goal_tolerance_m']
        limits=(fixture.get('planning_margin_m'),fixture.get('approach_clearance_m'),
                fixture.get('transfer_clearance_m'))
        if not _positive(requested) or not all(_positive(value) for value in limits):
            raise HarnessError('explicit_free_space_goal_tolerance_required')
        if requested>min(limits):raise HarnessError('free_space_goal_tolerance_exceeds_clearance')
        goal_position=requested
    opening=fixture.get('release_opening');grip_tolerance=fixture.get('gripper_tolerance')
    open_range=None
    if skill=='approach_grasp' and _positive(opening) and _positive(grip_tolerance):
        open_range=[opening-grip_tolerance,opening+grip_tolerance]
    # Grasping a block is not engagement with its destination hole. In
    # particular, goal_position_m is consumed by both fresh-plan validation
    # and measured waypoint checks; it must agree with the same phase policy.
    phase_position=goal_position if free_space else min(xy,clearance-margin) if engagement else xy
    if skill in ('grasp','regrasp') and 'grasp_position_tolerance_m' in cfg:
        requested=cfg['grasp_position_tolerance_m']
        dimensions=fixture.get('object_dimensions_m',())
        if (not _positive(requested) or len(dimensions)!=3 or not all(_positive(v) for v in dimensions)
                or requested>min(dimensions)/2 or requested>fixture.get('planning_margin_m',0)):
            raise HarnessError('grasp_tolerance_outside_capture_region')
        phase_position=requested
    return dict(position_m=phase_position,rotation_rad=min(yaw,tilt),
                xy_tolerance_m=xy,effective_clearance_m=clearance,object_planning_margin_m=margin,
                free_space=free_space,goal_position_m=phase_position,
                approach_open_range=open_range,
                observation_open_range=([0.,opening+grip_tolerance]
                    if _positive(opening) and _positive(grip_tolerance) else None),
                precision_certified=False)


def stage_tolerances(fixture,skill,stage_name):
    if skill in ('grasp','regrasp') and stage_name=='grasp_lift':
        return tolerances(fixture,'retreat')
    # Reopening and leaving a failed grasp are transit, not final capture or
    # hole engagement. Original geometry checks still require a clear exit.
    if skill=='regrasp' and stage_name in ('regrasp_open','regrasp_exit','regrasp_clearance'):
        return tolerances(fixture,'retreat')
    return tolerances(fixture,skill)


def pose_error(a,b):
    a,b=transform(a),transform(b)
    return (float(np.linalg.norm(a[:3,3]-b[:3,3])),
            float(np.arccos(np.clip((np.trace(a[:3,:3].T@b[:3,:3])-1)/2,-1.,1.))))


def _near(a,b,tolerance,*,radius=0.):
    distance,angle=pose_error(a,b)
    return (angle<=tolerance['rotation_rad'] and
            distance+2*radius*math.sin(angle/2)<=tolerance['position_m'])


def fresh_after(observation,after_ns,*,task=None,skill=None):
    if (observation is None or observation.received_ns<=after_ns
            or observation.state_received_ns is None or observation.state_received_ns<=after_ns):
        return False
    frames = _planning_frames(observation, task, skill)
    return bool(frames) and all(frame.received_ns>after_ns for frame in frames)


def alignment_measurement_gap(original, latest, *, task, skill):
    """An incomplete view cannot replace the measured inputs of a pending alignment."""
    if (skill != 'align' or original is None or latest is None or latest.faults
            or original.synthetic or latest.synthetic or original.calibration_id != latest.calibration_id
            or original.host_boot_id != latest.host_boot_id or original.clock_domain != latest.clock_domain
            or original.acquisition_instance != latest.acquisition_instance
            or original.geometry_camera != latest.geometry_camera
            or latest.geometry_scope not in ('coarse_static_task','coarse_object_verification','coarse_local_environment')):
        return None
    from .observation import measured_alignment_pair
    before = next((o for o in original.objects if o.object_id == task.object_id), None)
    target = next((o for o in latest.objects if o.object_id == task.object_id), None)
    if (not measured_alignment_pair(original,task) or target is None or not target.visible
            or not target.evidence_ids or target.shape != before.shape or target.frame_id != before.frame_id):
        return None
    if target.held is False:
        return 'revalidation_attachment_changed'
    if not measured_alignment_pair(latest,task):
        return 'alignment_measurement_pending'
    return None


def attachment_measurement_pending(original, latest, *, task, skill):
    """Wait for a measured attachment before sweeping a still-unpublished loaded path."""
    if (skill not in ('transfer_above_hole', 'move_tcp', 'align', 'insert', 'release', 'retreat')
            or original is None or latest is None or original.synthetic or latest.synthetic or latest.faults):
        return False
    before = next((obj for obj in original.objects if obj.object_id == task.object_id), None)
    after = next((obj for obj in latest.objects if obj.object_id == task.object_id), None)
    if (before is None or before.held is not True or before.eef_object_pose is None
            or after is None or after.held is False or after.eef_object_pose is not None
            or not after.visible or after.pose is None or not after.evidence_ids
            or before.frame_id != after.frame_id or before.shape != after.shape):
        return False
    # Identity changes must reach the worker's normal rejection path. A missing
    # contact sample in the correct scene grants no proof and no motion.
    try:
        _measurement_identity(original, latest, task=task, skill=skill)
    except HarnessError:
        return False
    return True


def _registered_transport(original, latest, task, skill):
    # In this profile PlanService can register the bucket collision geometry
    # from either the task aperture or independent fixture landmarks.
    return (skill == 'transfer_above_hole' and task is not None
            and task.admission_profile == 'development_coarse' and task.goal == 'placed'
            and original.geometry_scope == 'coarse_static_task'
            and latest.geometry_scope in ('coarse_static_task','coarse_object_verification')
            and original.geometry_camera == latest.geometry_camera == 'right')


def _require_measured_transport_reference(observation, task):
    reference = observation.fixture
    if reference is not None and reference.source == 'retained_static_fixture':
        return  # Its current wrist measurement and historical lineage are checked separately.
    if reference is None:
        reference = next((obj for obj in observation.objects if obj.object_id == task.hole_id), None)
        if reference is None or not reference.visible or reference.pose is None:
            raise RevalidationEvidenceUpdated(TRANSPORT_MEASUREMENT_PENDING)
    sources = {(f.camera, f.stream): f.blob_sha256 for f in observation.frames}
    for camera in ('head','right'):
        pair = (sources.get((camera,'rgb')), sources.get((camera,'depth')))
        if None in pair:
            continue
        if reference.evidence_ids == pair:
            return
        if observation.fixture is None and reference.evidence_ids == (*pair,sources.get(('left','rgb'))):
            return
    raise RevalidationSceneChanged('revalidation_scene_evidence_changed')


def _measurement_identity(original,latest,*,task=None,skill=None):
    transport = _registered_transport(original,latest,task,skill)
    local_task=(task is not None and task.goal=='placed'
        and task.admission_profile=='development_coarse' and skill in ('move_tcp','reobserve')
        and (local_scope(original) or local_scope(latest)))
    local_transition=(task is not None and task.goal=='placed'
        and task.admission_profile=='development_coarse' and skill in ('move_tcp','reobserve')
        and (local_scope(original) or local_scope(latest))
        and {original.geometry_scope,latest.geometry_scope}<=
            {'coarse_local_environment','coarse_static_task','coarse_object_verification'})
    if (original.host_boot_id is None or original.host_boot_id!=latest.host_boot_id
            or original.acquisition_instance!=latest.acquisition_instance
            or original.clock_domain!=latest.clock_domain
            or original.calibration_id!=latest.calibration_id
            or not local_task and not transport and (original.geometry_scope!=latest.geometry_scope
                or original.geometry_camera!=latest.geometry_camera)):
        raise HarnessError('revalidation_measurement_identity_changed')
    if local_task and not local_transition:
        # More target geometry without a current environment is an ordinary
        # scope/measurement gap, not a substituted calibration or identity.
        raise RevalidationSceneChanged('revalidation_scene_geometry_scope_changed')
    def identity(f):
        return (f.frame_id,f.width,f.height,f.encoding,f.depth_scale_m,
                f.camera_info_sha256,f.clock_domain,f.source_epoch,f.publisher_id)
    def identities(obs):
        return {(f.camera,f.stream):identity(f) for f in _planning_frames(obs, task, skill)}
    before,after=identities(original),identities(latest)
    if local_transition:
        # Only independently validated views enter local free-space evidence.
        # Losing a view removes its free region; adding one cannot make an old
        # camera change identity. The actual route is checked in the new views.
        required={(camera,stream) for camera in (original.geometry_camera,latest.geometry_camera)
                  for stream in ('rgb','depth')}
        if any(before[key]!=after[key] for key in set(before)&set(after)):
            raise HarnessError('revalidation_camera_identity_changed')
        if not required<=set(before)&set(after):
            if original.geometry_scope!=latest.geometry_scope:
                raise RevalidationSceneChanged('revalidation_scene_geometry_scope_changed')
            raise HarnessError('revalidation_camera_identity_changed')
        return
    a,b=original.fixture,latest.fixture
    old_retained=a is not None and a.source=='retained_static_fixture'
    new_retained=b is not None and b.source=='retained_static_fixture'
    if transport:
        # A fresh independently bound reference can change which views this
        # stage needs. Check the common physical streams and the new reference;
        # the worker then reconstructs the bucket and sweeps the actual path.
        wrist={('right','rgb'),('right','depth')}
        raw_before={(f.camera,f.stream):identity(f) for f in original.frames}
        raw_after={(f.camera,f.stream):identity(f) for f in latest.frames}
        shared=(before.keys()|after.keys()) & raw_before.keys() & raw_after.keys()
        if not wrist <= before.keys() & after.keys() or any(
                raw_before[key]!=raw_after[key] for key in shared):
            raise HarnessError('revalidation_camera_identity_changed')
        target=next((o for o in latest.objects if o.object_id==task.object_id),None)
        previous=next((o for o in original.objects if o.object_id==task.object_id),None)
        if target is not None and previous is not None and (
                target.frame_id!=previous.frame_id or target.shape!=previous.shape):
            raise HarnessError('revalidation_object_identity_changed')
        if previous is not None and previous.held is True and target is not None and target.held is False and target.evidence_ids:
            raise RevalidationSceneChanged('revalidation_attachment_changed')
        if latest.faults:
            raise HarnessError('revalidation_current_observation_fault')
        if latest.geometry_scope=='coarse_object_verification' or (
                b is None and not any(o.object_id==task.hole_id and o.pose is not None for o in latest.objects)):
            raise RevalidationEvidenceUpdated(TRANSPORT_MEASUREMENT_PENDING)
        if new_retained and a is None:
            # The candidate's original aperture gives no provenance for this
            # historical fixture. Wait for a new full measurement of either.
            raise RevalidationEvidenceUpdated(TRANSPORT_MEASUREMENT_PENDING)
        if (a is None)!=(b is None) or not (old_retained or new_retained) and before.keys()!=after.keys():
            _require_measured_transport_reference(original,task)
            _require_measured_transport_reference(latest,task)
            return True
        if not (old_retained or new_retained):
            return
    if not (old_retained or new_retained):
        left = ('left', 'rgb')
        if (task is not None and task.admission_profile == 'development_coarse'
                and (before.keys() ^ after.keys()) == {left}):
            # A newly visible head-only aperture and a cross-view measurement
            # may alternate. Require independent evidence on the head-only side.
            direct = latest if left in before else original
            hole = next((o for o in direct.objects if o.object_id == task.hole_id), None)
            head = {f.stream: f for f in direct.frames if f.camera == 'head'}
            measured = before.pop(left, None) or after.pop(left)
            auxiliary = next((f for f in direct.frames if (f.camera, f.stream) == left), None)
            if before != after or auxiliary is not None and identity(auxiliary) != measured:
                raise HarnessError('revalidation_camera_identity_changed')
            if (hole is not None and hole.visible and set(head) == {'rgb', 'depth'}
                    and hole.evidence_ids == (head['rgb'].blob_sha256, head['depth'].blob_sha256)):
                return True  # New source must be swept in the worker before publication.
            if (hole is not None and hole.source == 'cached_pre_transport_aperture'
                    and skill in CACHED_APERTURE_SKILLS):
                return True  # Cached aperture alternating with fresh cross-view measurement; sweep in worker.
            raise RevalidationSceneChanged('revalidation_scene_evidence_changed')
        shared = before.keys() & after.keys()
        # Precision observations can omit the single-camera selector. Their
        # existing geometry contract uses both calibrated RGB-D pairs.
        cameras = {c for c in (original.geometry_camera, latest.geometry_camera) if c} or {'head', 'right'}
        required = {(camera, stream) for camera in cameras for stream in ('rgb', 'depth')}
        if not required <= shared or any(before[key] != after[key] for key in shared):
            raise HarnessError('revalidation_camera_identity_changed')
        return before.keys() != after.keys()
    from .geometry_context import FIXTURE_TRANSIT_SKILLS
    from .observation import CACHED_APERTURE_SKILLS
    def _cached_hole(obs):
        if task is None:
            return None
        hole = next((o for o in obs.objects if o.object_id == task.hole_id), None)
        return (hole if hole is not None and hole.source == 'cached_pre_transport_aperture'
                and hole.pose is not None and hole.evidence_ids else None)
    orig_cached = _cached_hole(original)
    latest_cached = _cached_hole(latest)
    cached_allowed = (skill in CACHED_APERTURE_SKILLS
                      and (not old_retained or orig_cached is not None)
                      and (not new_retained or latest_cached is not None))
    if (task is None or task.admission_profile!='development_coarse' or task.goal!='placed'
            or (skill not in FIXTURE_TRANSIT_SKILLS and not cached_allowed)
            or original.geometry_scope!='coarse_static_task' or original.geometry_camera!='right'):
        raise HarnessError('full_scene_required_for_motion')
    if cached_allowed and orig_cached is not None and latest_cached is not None:
        if (orig_cached.evidence_ids != latest_cached.evidence_ids
                or orig_cached.measured_at_ns != latest_cached.measured_at_ns):
            raise HarnessError('revalidation_cached_aperture_lineage_changed')
    # Only the independently measured wrist stream is current in this scene.
    # A rejected head sample cannot change its identity or become new evidence.
    wrist={('right','rgb'),('right','depth')}
    full=wrist|{('head','rgb'),('head','depth')}
    allowed_full=(full, full | {('left', 'rgb')}) if cached_allowed else (full,)
    if not transport and (set(before) not in (wrist, *allowed_full) or set(after) not in (wrist, *allowed_full)
            or any(before.get(key)!=after.get(key) for key in wrist)
            or (old_retained and set(before)!=wrist)
            or (new_retained and set(after)!=wrist)):
        raise HarnessError('revalidation_camera_identity_changed')
    if new_retained:
        # Exact lineage: this is the same static measurement, not a fresh fit.
        # Even an equal pose with a substituted timestamp/evidence is invalid.
        fields=('fixture_id','frame_id','pose','reference_pose','evidence_ids','measured_at_ns')
        if (a is None or a.measured_at_ns is None or a.measured_at_ns>original.received_ns
                or any(getattr(a,key)!=getattr(b,key) for key in fields)):
            # A real full head sample can update the static reference while
            # planning, followed by another rejected head sample. The new
            # reference is not this candidate's lineage: re-plan, never turn
            # its later measurement time into a proof for the old candidate.
            if (a is not None and a.measured_at_ns is not None
                    and a.measured_at_ns<=original.received_ns<b.measured_at_ns<=latest.received_ns
                    and all(getattr(a,key)==getattr(b,key) for key in
                            ('fixture_id','frame_id','reference_pose'))):
                if b.evidence_ids!=a.evidence_ids:
                    # A newer independent measurement is input to the sweep.
                    return
                if skill=='transfer_above_hole' and a.pose==b.pose:
                    # Repeated image content has a later observation stamp but
                    # does not prove a new measurement. Keep the unpublished
                    # candidate until a comparable current reference arrives.
                    raise RevalidationEvidenceUpdated(TRANSPORT_MEASUREMENT_PENDING)
            raise HarnessError('revalidation_retained_fixture_lineage_changed')
        return
    # The wrist identity is continuous and the restored head measurement is
    # independently validated. Adding that view is not an identity substitution.
    # Its current fixture geometry participates in the candidate's new sweep.
    return


def _held_evidence_equivalent(before,after,original,latest,*,task,skill,threshold,world_motion=False):
    if before.held==after.held:return True
    # Unknown evidence is not a measured attachment transition. This exception
    # applies only before contact, with two measured open-gripper states and no
    # attachment. Never infer held=False or reuse it for transfer/insertion.
    opening=threshold.get('observation_open_range' if world_motion else 'approach_open_range')
    if (not (skill=='approach_grasp' or skill=='move_tcp' and world_motion) or before.object_id!=task.object_id
            or before.held is True or after.held is True
            or before.eef_object_pose is not None or after.eef_object_pose is not None
            or not isinstance(opening,(list,tuple)) or len(opening)!=2):return False
    low,high=opening
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in opening):return False
    from .gripper_state import observed_open
    return bool(low<high and all(obs.state_valid_mask[15]
        and observed_open(obs.state16[15],opening) for obs in (original,latest)))


def _scene_unchanged(original,latest,*,task,threshold,skill=None,world_motion=False,resweep=False):
    import json
    if local_scope(original) or local_scope(latest):
        from .local_environment import require_local_evidence
        if skill!='reobserve' and not (skill=='move_tcp' and world_motion):
            raise RevalidationSceneChanged('revalidation_scene_geometry_scope_changed')
        if any(obs.geometry_scope=='coarse_object_verification' and not local_scope(obs) for obs in (original,latest)):
            raise RevalidationSceneChanged('revalidation_scene_geometry_scope_changed')
        local=[require_local_evidence(task,obs) for obs in (original,latest) if local_scope(obs)]
        if (any(obs.geometry_scope not in ('coarse_local_environment','coarse_static_task','coarse_object_verification')
                for obs in (original,latest)) or any(
                evidence.frame_id!=local[0].frame_id
                or evidence.registered_object_diagonal_m!=local[0].registered_object_diagonal_m
                or evidence.planning_margin_m!=local[0].planning_margin_m for evidence in local)):
            raise HarnessError('local_environment_revalidation_identity_changed')
        for obs in (original,latest):
            target=next((obj for obj in obs.objects if obj.object_id==task.object_id),None)
            if target is None or not target.visible or not target.evidence_ids:
                raise RevalidationSceneChanged('revalidation_target_measurement_unavailable')
            if target.frame_id!=local[0].frame_id or target.shape!=task.shape:
                raise HarnessError('revalidation_object_identity_changed')
            if target.held is True or target.eef_object_pose is not None:
                raise RevalidationSceneChanged('revalidation_attachment_changed')
            if obs.geometry_scope=='coarse_static_task' and target.pose is None:
                raise RevalidationSceneChanged('revalidation_target_measurement_unavailable')
            if skill=='move_tcp':
                opening=threshold.get('observation_open_range')
                if (not isinstance(opening,(tuple,list)) or len(opening)!=2
                        or not obs.state_valid_mask[15]
                        or not opening[0]<=obs.state16[15]<=opening[1]):
                    raise RevalidationSceneChanged('revalidation_observation_requires_open_gripper')
        if original.fixture is not None and latest.fixture is not None:
            if (original.fixture.fixture_id!=latest.fixture.fixture_id
                    or original.fixture.frame_id!=latest.fixture.frame_id
                    or original.fixture.reference_pose!=latest.fixture.reference_pose):
                raise HarnessError('fixture_reference_identity_changed')
            if not resweep and not _near(original.fixture.pose,latest.fixture.pose,threshold):
                raise RevalidationSceneChanged('revalidation_fixture_changed')
        # A restored target frame or independently measured fixture is more
        # evidence, not a different object. The world goal stays frozen and
        # every actual FK segment is checked in the newly reconstructed scene.
        return
    raw={o.object_id:o for o in latest.objects}
    aligned=align_observation(original,latest)
    old={o.object_id:o for o in original.objects};new={o.object_id:o for o in aligned.objects}
    from .geometry_context import FIXTURE_TRANSIT_SKILLS, fixture_available, planning_entity_ids
    transit=skill in FIXTURE_TRANSIT_SKILLS and fixture_available(task,original) and fixture_available(task,latest)
    switch=(_registered_transport(original,latest,task,skill)
            and (original.fixture is None)!=(latest.fixture is None))
    if set(old)!=set(new) and (not (transit or switch) or (set(old)^set(new))-{task.hole_id}):
        raise RevalidationSceneChanged('revalidation_scene_object_set_changed')
    if transit:
        a,b=original.fixture,latest.fixture
        if (a.fixture_id!=b.fixture_id or a.frame_id!=b.frame_id or a.reference_pose!=b.reference_pose):
            raise HarnessError('fixture_reference_identity_changed')
        if not resweep and not _near(a.pose,b.pose,threshold):raise RevalidationSceneChanged('revalidation_fixture_changed')
    keys=(task.object_id,) if switch else planning_entity_ids(task,original,skill)
    for key in keys:
        before,after=old.get(key),new.get(key)
        if before and after and (before.frame_id!=after.frame_id or before.shape!=after.shape):
            raise HarnessError('revalidation_object_identity_changed')
        changes={}
        if before and after:
            fields=('held','visible') if transit or switch else ('held','inserted','inserted_hole_id','visible')
            for field in fields:
                if getattr(before,field)!=getattr(after,field):
                    changes[field]=dict(before=getattr(before,field),after=getattr(after,field))
            if _held_evidence_equivalent(before,after,original,latest,task=task,skill=skill,threshold=threshold,
                                         world_motion=world_motion):
                changes.pop('held',None)
            if skill=='withdraw_reset' and key==task.hole_id:
                if before.source=='static_return_anchor' and after.source=='static_return_anchor':
                    if before.evidence_ids!=after.evidence_ids or before.measured_at_ns!=after.measured_at_ns:
                        raise HarnessError('revalidation_return_anchor_lineage_changed')
                elif resweep and before.source=='static_return_anchor' and after.source is None and after.visible:
                    changes.pop('visible',None)
        if (before is None or after is None or before.pose is None or after.pose is None
                or before.frame_id!=after.frame_id or before.shape!=after.shape
                or changes or not after.evidence_ids
                or not resweep and not _near(before.pose,after.pose,threshold)):
            metrics=dict(original_observation_id=original.observation_id,
                observation_id=latest.observation_id,object_id=key,
                position_tolerance_m=threshold['position_m'],rotation_tolerance_rad=threshold['rotation_rad'],
                changed_fields=changes,skill=skill)
            if before and after and before.pose and after.pose:
                distance,angle=pose_error(before.pose,after.pose)
                metrics.update(position_error_m=distance,rotation_error_rad=angle,
                               raw_rotation_error_rad=pose_error(before.pose,raw[key].pose)[1])
            raise RevalidationSceneChanged('revalidation_object_or_hole_changed:'+json.dumps(
                dict(diagnostic=dict(code='revalidation_object_or_hole_changed',metrics=metrics)),separators=(',',':')))
        if (before.eef_object_pose is None)!=(after.eef_object_pose is None):
            raise RevalidationSceneChanged('revalidation_attachment_changed')
        if (not resweep and before.eef_object_pose is not None
                and not _near(before.eef_object_pose,after.eef_object_pose,threshold)):
            # A new transform estimate is geometry to sweep, not evidence of
            # a different held object. Presence/held/identity changes above
            # still reject. The worker checks this measured attachment along
            # the entire fixed path and at the current semantic endpoint.
            raise RevalidationSceneChanged('revalidation_attachment_pose_changed')


def revalidate_candidate(service,*,intent,proposal,original_observation,observation,full22,held16,after_ns):
    """Runs off-loop: reconstruct stage goals and sweep actual candidate FK in a new scene."""
    started_at_ns=service.clock()
    intent=SkillIntent.model_validate(intent)
    proposal=JointProposal(**proposal)
    original=Observation.model_validate(original_observation)
    latest=Observation.model_validate(observation)
    task,fixture=service.task,service.fixture
    if (task.goal!='placed' or task.admission_profile!='development_coarse'
            or original.synthetic or original.faults
            or not stationary_admitted(original,admission_profile=task.admission_profile)
            or intent.task_hash!=digest(task) or intent.task_id!=task.task_id
            or intent.skill not in task.allowed_skills
            or proposal.request_id!=intent.request_id or proposal.epoch!=intent.epoch
            or proposal.scene_version!=intent.scene_version or original.scene_version!=intent.scene_version
            or original.observation_id!=intent.observation_id
            or any(v!=task.calibration_id for v in (original.calibration_id,latest.calibration_id,
                                                   intent.calibration_id,proposal.calibration_id))
            or tuple(proposal.full_reference)!=tuple(full22)):
        raise HarnessError('revalidation_scope_mismatch')
    _measurement_identity(original,latest,task=task,skill=intent.skill)
    if type(after_ns) is not int or not fresh_after(latest,after_ns,task=task,skill=intent.skill):
        raise HarnessError('post_planning_measurements_required')
    # Classify age only after the complete candidate/scene comparison below;
    # an expired frame must not conceal a changed object, camera or path.
    _measurement_times(latest,task=task,now_ns=service.clock(),skill=intent.skill)
    threshold=tolerances(fixture,intent.skill)
    goal_threshold=dict(threshold,position_m=threshold['goal_position_m'])
    # A pregrasp is defined at the registered fingertips. Re-estimating a
    # square's yaw moves the wrist around that point; treating that lever-arm
    # displacement as target translation rejects otherwise equivalent goals.
    # Before grasp closure the same registered fingertips are the contact
    # reference. Wrist yaw's lever-arm translation is not independent target
    # drift. Every actual tool segment is still swept in the fresh scene.
    goal_reference=(transform(fixture['eef_tip_reference'])
        if intent.skill in ('approach_grasp','grasp','regrasp','move_tcp') and fixture.get('eef_tip_reference') is not None
        else np.eye(4))
    world_motion=(intent.skill=='move_tcp' and intent.parameters.target_frame=='world'
                  and not intent.parameters.support_contact)
    _scene_unchanged(original,latest,task=task,threshold=threshold,skill=intent.skill,
                     world_motion=world_motion,resweep=True)
    targets=np.asarray(proposal.targets,float)
    held=np.asarray(held16,float)
    if proposal.trajectory is not None:
        from .trajectory import reference_speed,validate_schedule
        if (not validate_schedule(proposal.trajectory,proposal.targets)
                or proposal.trajectory['reference_velocity_deg_s']!=reference_speed(fixture,intent.skill)
                or not np.array_equal(np.asarray(proposal.trajectory['anchor']),held)):
            raise HarnessError('revalidation_reference_schedule_mismatch')
    if (held.shape!=(16,) or targets.ndim!=2 or targets.shape[1]!=16 or not 0<len(targets)<=100
            or not np.isfinite(targets).all() or not np.isfinite(held).all()
            or not np.all(targets[:,:7]==held[:7]) or not np.all(targets[:,14]==held[14])):
        raise HarnessError('revalidation_invalid_joint_path')
    # Independent FK owns its own mutable model, isolated from slow IK work.
    base=service.numerics.call('forward_pose',full22=full22,held16=held16)
    poses=service.numerics.call('forward_path',full22=full22,targets=targets.tolist())
    if len(poses)!=len(targets):raise HarnessError('revalidation_FK_path_length')
    poses=[transform(value) for value in poses]
    original_scene=service._scene(original,full22,held16,base_eef=base,skill=intent.skill)
    aligned_latest=align_observation(original,latest)
    scene=service._scene(aligned_latest,full22,held16,base_eef=base,skill=intent.skill)
    from .grasp_selection import recorded_scene
    original_scene=recorded_scene(service,original_scene,original,proposal,intent)
    scene=recorded_scene(service,scene,aligned_latest,proposal,intent)
    distance=proposal.geometry_trace[0].get('pregrasp_distance_m') if proposal.geometry_trace else None
    from .joint_motion import uses_joint_motion,reconstruction,is_search_stage,search_bounds
    joint_search=uses_joint_motion(fixture,intent,proposal)
    original_stages=(reconstruction(original_scene,intent,proposal,pregrasp_distance_m=distance) if joint_search else
        propose(intent.skill,original_scene,variant=proposal.candidate_variant,pregrasp_distance_m=distance)).get('stages',[])
    try:
        stages=(reconstruction(scene,intent,proposal,pregrasp_distance_m=distance) if joint_search else
            propose(intent.skill,scene,variant=proposal.candidate_variant,pregrasp_distance_m=distance,
                    _endpoint_only=intent.skill=='approach_grasp')).get('stages',[])
    except HarnessError as exc:
        if (str(exc) in ('scene_sweep_blocked','no_clear_square_symmetry_route',
                        'regrasp_approach_sweep_blocked','grasp_height_outside_registered_object')
                or str(exc).startswith(('return_route_sweep_blocked:','scene_sweep_blocked:',
                                        'local_observation_space_unknown:'))):
            raise RevalidationSceneChanged('revalidation_actual_path_sweep_blocked') from None
        raise
    trace=proposal.geometry_trace
    if len(trace)!=len(original_stages):
        raise HarnessError('revalidation_stage_contract_changed')
    if len(stages)!=len(original_stages):
        raise RevalidationSceneChanged('revalidation_goal_changed')
    local_certificate=None
    if getattr(scene,'local_collision_evidence',None) is not None:
        local_certificate=dict(views={},view_bounds={},occupied=set(),object_local_box=fixture['object_local_box'],
            target_motion_tolerance_m=threshold['position_m'],target_rotation_tolerance_rad=threshold['rotation_rad'],
            departures=[],tool_local_boxes=scene.tool_local_boxes,eef_tip_reference=scene.eef_tip_reference,
            attached_radius_m=scene.attached_radius_m)
        scene=replace(scene,local_sweep_certificate=local_certificate)
    previous=transform(base);cursor=0;checked=0;margin_departure=False;stage_tolerance_records=[]
    for ordinal,(entry,before,stage) in enumerate(zip(trace,original_stages,stages)):
        start,end=entry['start_index'],entry['end_index']
        stage_threshold=stage_tolerances(fixture,intent.skill,stage.get('name'))
        stage_tolerance_records.append(dict(stage=ordinal,name=stage.get('name'),tolerances=stage_threshold))
        goal_threshold=dict(stage_threshold,position_m=stage_threshold['goal_position_m'])
        if (entry.get('semantic_stage')!=before.get('name') or before.get('name')!=stage.get('name')
                or any(bool(entry.get(k))!=bool(before.get(k)) or bool(before.get(k))!=bool(stage.get(k))
                       for k in ('target_contact_hold','target_contact_departure','continuous_return_transit','uncertain_grasp_observation'))):
            raise HarnessError('revalidation_stage_contract_changed')
        if before.get('attachment_basis')=='predicted_for_collision_only':
            if ordinal==0 or type(start) is not int or not 0<start<len(poses):
                raise HarnessError('revalidation_lift_contact_missing')
            before=rebase_support_lift(before,poses[start-1],original_stages[ordinal-1]['goals'][-1],original_scene.base_object)
            stage=rebase_support_lift(stage,poses[start-1],stages[ordinal-1]['goals'][-1],scene.base_object)
        recorded=np.asarray(entry['goals'],float)
        reconstructed=np.asarray(before['goals'],float)
        # Independent FK/normalization may differ in the last floating point
        # bits. The immutable proposal hash still binds exact recorded bytes;
        # recomputed arithmetic needs numerical equality, not a JSON hash.
        goals_match=(recorded.shape==reconstructed.shape and
                     np.allclose(recorded,reconstructed,rtol=0,atol=1e-10))
        if (type(start) is not int or type(end) is not int or start!=cursor or not start<end<=len(targets)
                or not goals_match or entry['gripper']!=before['gripper']):
            raise HarnessError('revalidation_stage_binding_mismatch')
        if stage['gripper']!=before['gripper']:
            raise RevalidationSceneChanged('revalidation_goal_changed')
        attached=stage.get('eef_object')
        # For loaded alignment/insertion stages, the precision task target is
        # the held object relative to the hole, not the wrist Link7 origin 29 cm
        # away (where <1 deg stationary attachment noise creates >3 mm lever-arm
        # displacement at the wrist even when the block itself moves <0.5 mm).
        # Every actual tool and held-object FK segment is still swept below.
        at_attached_object=(intent.skill in ('align','insert') and attached is not None)
        stage_goal_reference=transform(attached) if at_attached_object else goal_reference
        if stage_threshold['free_space'] or stage.get('name')=='overhead_square_alignment':
            # A new scene may choose a different detour or overhead clearance
            # height to the same task goal. Validate the frozen candidate's
            # entire actual FK path below; optional transit points are not its
            # endpoint identity.
            pairs=zip(before['goals'][-1:],stage['goals'][-1:])
        else:
            if len(stage['goals'])!=len(before['goals']):
                raise RevalidationSceneChanged('revalidation_goal_changed')
            pairs=zip(before['goals'],stage['goals'])
        for a,b in pairs:
            if not _near(transform(a)@stage_goal_reference,transform(b)@stage_goal_reference,goal_threshold):
                raise RevalidationSceneChanged('revalidation_goal_changed')
        if stage.get('name')=='pregrasp_open' and any(
                not np.array_equal(q[:15],held[:15]) or q[15]!=scene.release_opening for q in targets[start:end]):
            raise HarnessError('numerical_pregrasp_open_must_hold_arm')
        if stage.get('name')=='release_on_support' and any(
                not np.array_equal(q[:15],held[:15]) or q[15]!=scene.release_opening for q in targets[start:end]):
            raise HarnessError('numerical_supported_release_must_hold_arm')
        if stage.get('target_contact_hold') and any(not np.array_equal(q[:15],held[:15])
                or q[15]!=scene.release_opening for q in targets[start:end]):
            raise HarnessError('numerical_regrasp_open_must_hold_arm')
        if stage.get('target_contact_departure'):
            from .contact_departure import target_departure_clear
            if (any(q[15]!=scene.release_opening for q in targets[start:end])
                    or not target_departure_clear(scene,[previous,*poses[start:end]])):
                raise RevalidationSceneChanged('revalidation_target_departure_not_proven')
        if stage.get('uncertain_grasp_observation'):
            from .grasp_observation import check_actual_path
            check_actual_path(scene,[previous,*poses[start:end]],targets[start:end],held,fixture)
        for index in range(start,end):
            if not _motion_clear(scene,previous,poses[index],attached,
                    allow_target_contact=stage.get('allow_target_contact',False),
                    support_lift=stage.get('support_contact_tolerance_m') is not None,
                    support_contact_direction=stage.get('support_contact_direction','depart')):
                # A fresh estimate can put the stationary pre-contact tool
                # just inside the target's planning padding. Only the FIRST
                # frozen segment may depart, and it must prove non-approach
                # continuously and restore full padding by its endpoint.
                from .skills import stage_obstacles
                departure=(index==0 and intent.skill=='approach_grasp'
                    and scene.object_held is not True and scene.eef_object is None and attached is None
                    and tool_margin_departure_clear(previous,poses[index],scene.eef_tip_reference,
                        scene.attached_radius_m,stage_obstacles(scene),margin_m=scene.planning_margin_m,
                        local_boxes=scene.tool_local_boxes,target_object_id=scene.target_object_id))
                if not departure:
                    raise RevalidationSceneChanged('revalidation_actual_path_sweep_blocked')
                margin_departure=True
            previous=poses[index];checked+=1
        expected=stage['goals'][-1] if stage['goals'] else (base if start==0 else poses[start-1])
        # Free-space waypoints are coarse positioning, followed by a new skill
        # observation. Their entire actual tool/held-object sweep was checked
        # above. Hole engagement retains the exact held-object / tip error bound.
        at_grasp_tip=(intent.skill in ('grasp','regrasp') and attached is None
                      and fixture.get('eef_tip_reference') is not None)
        if at_attached_object:
            # Exact rigid-box corner bound in the held object's own frame: by
            # convexity the maximum displacement of any point on the held block
            # is attained at one of the 8 vertices of object_local_box.
            actual_obj=poses[end-1]@stage_goal_reference
            expected_obj=transform(expected)@stage_goal_reference
            _,angle=pose_error(actual_obj,expected_obj)
            bmin,bmax=scene.object_local_box['min'],scene.object_local_box['max']
            corners=np.array([[x,y,z,1.0] for x in (bmin[0],bmax[0]) for y in (bmin[1],bmax[1]) for z in (bmin[2],bmax[2])]).T
            max_corner=float(np.max(np.linalg.norm((actual_obj@corners)[:3]-(expected_obj@corners)[:3],axis=0)))
            if angle>goal_threshold['rotation_rad'] or max_corner>goal_threshold['position_m']:
                raise RevalidationSceneChanged('revalidation_endpoint_outside_remaining_clearance')
        else:
            radius=float(np.linalg.norm(transform(scene.eef_tip_reference)[:3,3]))
            if attached is not None:
                radius=max(radius,float(np.linalg.norm(transform(attached)[:3,3]))+
                           float(np.linalg.norm(np.maximum(np.abs(scene.object_local_box['min']),
                                                           np.abs(scene.object_local_box['max'])))))
            if not _near(poses[end-1]@stage_goal_reference,transform(expected)@stage_goal_reference,
                         goal_threshold,radius=0. if stage_threshold['free_space'] or at_grasp_tip else radius):
                raise RevalidationSceneChanged('revalidation_endpoint_outside_remaining_clearance')
        if stage['gripper'] is not None and targets[end-1,15]!=stage['gripper']:
            raise HarnessError('revalidation_gripper_goal_changed')
        cursor=end
    if not stages:
        if len(targets)!=1 or not np.array_equal(targets[0],held):
            raise HarnessError('revalidation_verify_must_hold')
    elif cursor!=len(targets):raise HarnessError('revalidation_unassigned_joint_targets')
    rebound_native=bool(trace and trace[0].get('reused_numerical_candidate',{}).get('native_path_revalidation_required'))
    native_stages_checked=0
    if joint_search or rebound_native:
        for entry,stage in zip(trace,stages):
            if not is_search_stage(stage) and not rebound_native:continue
            if (stage.get('gripper') is not None or stage.get('allow_target_contact')
                    or stage.get('support_contact_tolerance_m') is not None):
                raise HarnessError('revalidation_rebound_path_requires_free_space')
            start,end=entry['start_index'],entry['end_index']
            stage_targets=targets[start:end]
            stage_held=held if start==0 else targets[start-1]
            bounds=search_bounds(scene,stage)
            bounds['joint_path_to_validate']=stage_targets.tolist()
            try:
                # Validation consumes the recorded joint path. It performs no
                # endpoint IK or OMPL search, including for a Cartesian route
                # rebound to a different held/body reference after preview.
                checked_targets=service.numerics.call('solve_path',goals=stage['goals'][-1:],gripper=None,
                    full22=full22,held16=stage_held.tolist(),scene=bounds)
            except Exception as exc:
                from .owner_bridge import numerical_failure_detail
                if numerical_failure_detail(exc)['code']=='actual_IK_candidate_collision_rejected' or str(exc).startswith('actual_IK_path_'):
                    raise RevalidationSceneChanged('revalidation_actual_path_sweep_blocked') from None
                raise
            if not np.array_equal(np.asarray(checked_targets),stage_targets):
                raise HarnessError('revalidation_joint_path_changed')
            native_stages_checked+=1
    now=service.clock()
    if getattr(scene,'local_collision_evidence',None) is not None and stages:
        from .local_environment import require_departure_endpoint
        require_departure_endpoint(scene,poses[-1])
    _require_current_measurements(latest,task=task,now_ns=now,max_age_ns=500_000_000,skill=intent.skill)
    if local_certificate is not None:
        from .local_environment import seal_sweep_certificate
        local_certificate=seal_sweep_certificate(local_certificate,latest)
    return dict(schema=SCHEMA,candidate_sha256=candidate_hash(proposal),request_id=intent.request_id,
        task_hash=digest(task),owner_instance_id=intent.owner_instance_id,session_id=intent.session_id,
        epoch=intent.epoch,calibration_id=task.calibration_id,original_observation_id=original.observation_id,
        original_observation_sha256=digest(original),observation=latest.model_dump(),
        observation_sha256=digest(latest),after_planning_ns=after_ns,checked_at_ns=now,
        geometry_checks=dict(actual_FK_waypoints=len(poses),swept_segments=checked,
                             native_fixed_path_stages=native_stages_checked,
                             swept_observation_sha256=digest(latest),
                             local_space_certificate=local_certificate,
                             first_segment_target_margin_departure=margin_departure,
                             target_contact_departure_stages=[i for i,s in enumerate(stages) if s.get('target_contact_departure')],
                             stage_endpoint_tolerances=stage_tolerance_records,
                             goal_reference=('registered_fingertip' if intent.skill in ('approach_grasp','grasp','regrasp')
                                 and fixture.get('eef_tip_reference') is not None
                                 else 'attached_object' if intent.skill in ('align','insert')
                                 and any(s.get('eef_object') is not None for s in stages) else 'eef'),
                             elapsed_ms=(now-started_at_ns)/1e6,
                             stages=len(stages),endpoint_tolerances=threshold),precision_certified=False)


def proof_observation(proof,*,proposal,original,intent,task,now_ns,max_age_ns,live_observation=None):
    """Cheap owner-side receipt validation; never performs FK, networking or scene sweeps."""
    if (not isinstance(proof,dict) or proof.get('schema')!=SCHEMA
            or proof.get('candidate_sha256')!=candidate_hash(proposal)
            or proof.get('request_id')!=intent.request_id or proof.get('task_hash')!=digest(task)
            or proof.get('owner_instance_id')!=intent.owner_instance_id
            or proof.get('session_id')!=intent.session_id or proof.get('epoch')!=intent.epoch
            or proof.get('calibration_id')!=intent.calibration_id
            or proof.get('original_observation_id')!=original.observation_id
            or proof.get('original_observation_sha256')!=digest(original)
            or proof.get('precision_certified') is not False):
        raise HarnessError('revalidation_proof_binding_mismatch')
    latest=Observation.model_validate(proof['observation'])
    if (proof.get('observation_sha256')!=digest(latest)
            or proof.get('geometry_checks',{}).get('swept_observation_sha256')!=digest(latest)
            or latest.calibration_id!=intent.calibration_id
            or type(proof.get('checked_at_ns')) is not int
            or type(proof.get('after_planning_ns')) is not int
            or not fresh_after(latest,proof['after_planning_ns'],task=task,skill=intent.skill)):
        raise HarnessError('revalidation_proof_measurement_mismatch')
    _measurement_identity(original,latest,task=task,skill=intent.skill)
    # Preserve the real check timestamp. A future/impossible receipt is an
    # identity fault, while an otherwise valid old receipt may be recomputed.
    times=_measurement_times(latest,task=task,now_ns=now_ns,skill=intent.skill)
    checked=proof['checked_at_ns']
    if checked>now_ns or any(stamp>checked for stamp in times):
        raise HarnessError('revalidation_proof_measurement_mismatch')
    threshold=proof['geometry_checks']['endpoint_tolerances']
    if not all(_positive(threshold.get(k)) for k in ('position_m','rotation_rad')):
        raise HarnessError('revalidation_proof_measurement_mismatch')
    world_motion=(intent.skill=='move_tcp' and intent.parameters.target_frame=='world'
                  and not intent.parameters.support_contact)
    _scene_unchanged(original,latest,task=task,threshold=threshold,skill=intent.skill,
                     world_motion=world_motion,resweep=True)
    if live_observation is not None:
        aperture_source_changed = _measurement_identity(latest,live_observation,task=task,skill=intent.skill)
        if live_observation.calibration_id!=task.calibration_id:
            raise HarnessError('revalidation_scope_mismatch')
        _measurement_times(live_observation,task=task,now_ns=now_ns,skill=intent.skill)
        loaded = next((o for o in latest.objects if o.object_id==task.object_id), None)
        current = next((o for o in live_observation.objects if o.object_id==task.object_id), None)
        if (loaded and loaded.held is True and loaded.eef_object_pose is not None
                and current and current.held is False and current.evidence_ids):
            raise RevalidationSceneChanged('revalidation_attachment_changed')
        if attachment_measurement_pending(latest,live_observation,task=task,skill=intent.skill):
            # A later independent measurement can lack its collision attachment
            # even after the worker checked a complete scene. No publish from
            # that proof; recheck the same candidate within the original budget.
            raise RevalidationEvidenceUpdated('revalidation_attachment_measurement_pending')
        try:
            _scene_unchanged(latest,live_observation,task=task,threshold=threshold,skill=intent.skill,
                             world_motion=world_motion)
        except RevalidationSceneChanged as exc:
            if str(exc).split(':',1)[0] not in (
                    'revalidation_object_or_hole_changed','revalidation_fixture_changed',
                    'revalidation_scene_object_set_changed','revalidation_attachment_pose_changed'):
                raise
            # This proof covered an older scene. Keep the unpublished joint
            # candidate and resweep it in the worker under the same request.
            # No new motion permission or physical attempt is inferred here.
            raise RevalidationEvidenceUpdated(str(exc)) from None
        # The receipt has no numerical worker. Newly restored/lost geometry
        # after its sweep needs a fresh proof, rather than an identity fault or
        # an unchecked extension of the old free-space proof.
        if ((local_scope(latest) or local_scope(live_observation))
                and (latest.geometry_scope!=live_observation.geometry_scope
                    or (latest.fixture is None)!=(live_observation.fixture is None))):
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        if aperture_source_changed:
            raise RevalidationEvidenceUpdated('revalidation_scene_evidence_changed')
        if local_scope(latest):
            from .local_environment import check_sweep_certificate
            check_sweep_certificate(proof['geometry_checks'].get('local_space_certificate'),latest,live_observation)
    age_limit=geometry_age_limit_ns(max_age_ns,admission_profile=task.admission_profile,
                                    geometry_scope=latest.geometry_scope)
    if age_limit is not None and any(now_ns-stamp>age_limit for stamp in (*times,checked)):
        raise RevalidationExpired('revalidation_proof_expired')
    return latest
