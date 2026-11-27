"""Fixture-only evidence after measured placement; no invented pose for a dropped block."""
import numpy as np
from .contracts import SceneObject,digest
from .geometry import transform
from .gripper_state import observed_open


def _wants_return_scene(context):
    ctx = context or {}
    if 'return_scene_requested' in ctx:
        return ctx['return_scene_requested'] is True
    return ctx.get('placement_verified') is True


def _resolve_return_static_anchor(pipeline, observation, context, stamp, *, cause):
    from .scene_pipeline import CACHED_APERTURE_MAX_AGE_NS
    cfg = pipeline.config
    ctx = context or {}
    if ctx.get('authorized') is not True:
        raise ValueError('return_anchor_invalidated:not_authorized')
    anchor = getattr(pipeline, 'return_static_anchor', None)
    if anchor is None:
        prior_fx = getattr(pipeline, 'measured_fixture', None)
        if (prior_fx is not None and prior_fx.source == 'rgbd_anchor_registration'
                and prior_fx.measured_at_ns is not None):
            anchor = dict(
                hole_pose=tuple(prior_fx.pose),
                evidence_ids=tuple(prior_fx.evidence_ids),
                measured_at_ns=int(prior_fx.measured_at_ns),
                scope_identity=getattr(pipeline, 'context_identity', None),
                calibration_id=cfg['calibration_id'],
                acquisition_identity=getattr(pipeline, 'acquisition_identity', None),
                frame_id=prior_fx.frame_id,
                source_kind='rgbd_anchor_registration',
            )
    if anchor is None:
        raise ValueError(cause)
    if getattr(pipeline, 'context_identity', None) != anchor['scope_identity']:
        raise ValueError('return_anchor_invalidated:scope_changed')
    if (cfg['calibration_id'] != anchor['calibration_id']
            or observation.calibration_id not in (None, anchor['calibration_id'])):
        raise ValueError('return_anchor_invalidated:calibration_changed')
    if getattr(pipeline, 'acquisition_identity', None) != anchor['acquisition_identity']:
        raise ValueError('return_anchor_invalidated:acquisition_changed')
    contact_fault = pipeline._contact_fault_since(anchor['measured_at_ns'], ctx)
    if contact_fault is not None:
        pipeline.last_timing['return_static_anchor'] = dict(
            status='invalidated', reason='return_anchor_invalidated:' + contact_fault,
            measured_at_ns=anchor['measured_at_ns'],
        )
        raise ValueError('return_anchor_invalidated:' + contact_fault)
    age_ns = stamp - anchor['measured_at_ns']
    age_s = round(age_ns / 1e9, 4)
    if age_ns < 0 or age_ns > CACHED_APERTURE_MAX_AGE_NS:
        pipeline.last_timing['return_static_anchor'] = dict(
            status='invalidated', reason='return_anchor_age_exceeded',
            age_s=age_s, measured_at_ns=anchor['measured_at_ns'],
        )
        raise ValueError('return_anchor_age_exceeded')
    pipeline.last_timing['return_static_anchor'] = dict(
        status='used',
        age_s=age_s,
        measured_at_ns=anchor['measured_at_ns'],
        source_kind=anchor['source_kind'],
        fallback_cause=cause,
    )
    return anchor


def observe_return(pipeline,observation,context):
    cfg=pipeline.config;camera=cfg['hole_camera'];name=camera['camera']
    if not context.get('authorized') or not _wants_return_scene(context):
        raise ValueError('verified_placement_required_for_return_scene')
    if observation.faults:
        from .scene_pipeline import secondary_sampling_faults
        unused=cfg['camera']
        if unused==name or not secondary_sampling_faults(observation.faults,unused):
            raise ValueError('return_capture_faults')
        pipeline.last_timing['unused_wrist_sample_faults']=list(observation.faults)
        observation=observation.model_copy(update={'faults':()})
    frames=tuple(f for f in observation.frames if f.camera==name)
    stamps=tuple(f.source_ns for f in frames)
    if (len(frames)!=2 or None in stamps or observation.state16 is None
            or observation.state_valid_mask is None or not all(observation.state_valid_mask[7:14])
            or not observation.state_valid_mask[15] or observation.state_received_ns is None):
        raise ValueError('fresh_return_camera_and_feedback_required')
    stamp=min(f.received_ns for f in frames)
    if not pipeline._gripper_is_open(observation,frame_ns=stamp):raise ValueError('return_gripper_not_open')
    if pipeline.return_stamps is not None and any(a<=b for a,b in zip(stamps,pipeline.return_stamps)):
        raise ValueError('new_return_frames_required')
    anchor_source=None
    anchor_measured_at_ns=None
    try:
        depth,evidence,masks,timing=pipeline._secondary_view(observation,camera)
        pipeline.last_timing.update(timing)
        try:
            hole, visible = _head_aperture(pipeline, observation, frames, depth, masks, camera)
        except ValueError:
            measured = pipeline._cross_view_aperture(observation, depth)
            if measured is None:
                raise
            hole, left_id = measured
            evidence = (*evidence, left_id)
            frames = (*frames, next(f for f in observation.frames if f.blob_sha256 == left_id))
            visible = True
    except ValueError as exc:
        anchor = _resolve_return_static_anchor(pipeline, observation, context, stamp, cause=str(exc))
        hole = transform(anchor['hole_pose'])
        evidence = anchor['evidence_ids']
        visible = False
        anchor_source = 'static_return_anchor'
        anchor_measured_at_ns = anchor['measured_at_ns']
    stamp=min(f.received_ns for f in frames)
    sample=dict(time=stamp,state_ns=observation.state_received_ns,
                joints=np.asarray(observation.state16[7:14]),gripper=pipeline._gripper_value(observation,frame_ns=stamp))
    history=pipeline.return_history;history.append(sample)
    while len(history)>3 and history[1]['time']<=stamp-cfg['stationary_window_ns']:history.popleft()
    stationary=bool(len(history)>=3 and len({h['state_ns'] for h in history})>=3
        and stamp-history[0]['time']>=cfg['stationary_window_ns']
        and max(np.max(abs(h['joints']-sample['joints'])) for h in history)<cfg['stationary_delta_deg']
        and max(abs(h['gripper']-sample['gripper']) for h in history)<cfg['gripper_stationary_delta'])
    warnings=list(observation.quality_warnings)
    if anchor_source == 'static_return_anchor':
        warnings.append('return_fixture_from_static_anchor')
    if not stationary:
        moving=bool(max(np.max(abs(h['joints']-sample['joints'])) for h in history)>=cfg['stationary_delta_deg']
            or max(abs(h['gripper']-sample['gripper']) for h in history)>=cfg['gripper_stationary_delta'])
        warnings.append('observed_motion' if moving else 'stationary_history_incomplete')
    pipeline.return_stamps=stamps;pipeline.last_received_ns=stamp;pipeline.revision+=1
    obj=SceneObject(object_id=cfg['hole_id'],shape='square',pose=tuple(transform(hole).ravel()),
        frame_id=cfg['native_frame'],planning_margin_m=cfg['planning_margin_m'],visible=visible,
        stationary=True,evidence_ids=evidence,source=anchor_source,measured_at_ns=anchor_measured_at_ns)
    return observation.model_copy(update=dict(objects=(obj,),stationary=stationary,
        quality_warnings=tuple(dict.fromkeys(warnings)),
        frames=frames,pairings=tuple(p for p in observation.pairings if p.camera==name),
        geometry_scope='coarse_fixture_return',geometry_camera=name,calibration_id=cfg['calibration_id'],
        scene_version=digest(dict(revision=pipeline.revision,fixture=obj.model_dump(),scope='return_only'))))


def _head_aperture(pipeline, observation, frames, depth, masks, camera):
    cfg = pipeline.config
    hole_mask=masks.get(cfg['hole_id'])
    visible=hole_mask is not None and np.count_nonzero(hole_mask)>=20
    if visible:
        from .fixture_tracking import locate_hole
        if pipeline.occlusion is None:raise ValueError('return_fixture_anchors_required')
        hole,anchors,details=locate_hole(hole_mask,depth,masks,camera,cfg['base_hole'],
            pipeline.occlusion['anchors'],identity_tolerance_m=cfg['planning_margin_m'])
        pipeline.live_hole=hole.copy();pipeline.live_fixture_anchors=anchors
        pipeline.hole_last_visible_ns=min(f.received_ns for f in frames)
        pipeline.last_timing['fixture_relocalization']=details
    else:
        if (pipeline.live_hole is None or pipeline.hole_last_visible_ns is None or pipeline.occlusion is None
                or observation.received_ns-pipeline.hole_last_visible_ns>pipeline.occlusion['max_occlusion_ns']):
            raise ValueError('return_hole_missing_without_current_fixture')
        hole=pipeline.live_hole.copy()
    if not pipeline._fixture_visible(masks,depth):raise ValueError('return_fixture_not_verified')
    return hole, visible
