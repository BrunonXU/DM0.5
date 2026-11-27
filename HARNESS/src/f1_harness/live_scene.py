"""Continuous trusted scene assembly. Processing latency never refreshes acquisition time."""
from pathlib import Path
from copy import deepcopy
import json
import threading
import time

from .ipc import UnixClient
from .scene_pipeline import CalibratedScenePipeline, SecondaryCameraUnavailable, secondary_sampling_faults
from .observation import geometry_age_limit_ns, geometry_quality_warnings


class SceneContextAdvanced(ValueError):
    """Same authorization scope advanced normally; discard this sample, retain measured history."""


class SceneNotAdvanced(ValueError):
    """No new camera evidence; retain the old scene's original age and TTL."""


class CameraPairUnavailable(ValueError):
    """An identified wrist sample cannot be used to measure this image."""

    def __init__(self, owner_reason, detail=None):
        self.owner_reason = owner_reason
        super().__init__(detail or owner_reason)


PROCESSING_EXPIRY=frozenset(('perception_latency_exceeds_observation_budget',
    'evidence_archive_exceeds_observation_budget','verification_transport_exceeds_observation_budget'))


def _context_scope(context):
    # Placement completion advances the observation contract within the same
    # authorized task; it is not a new owner/session. The exact context is still
    # rechecked before publishing so a pre-placement scene cannot cross it.
    return {key:context.get(key) for key in (
        'owner_instance_id','session_id','task_id','task_hash','task_generation','epoch','authorized')}


class LiveSceneObserver:
    def search_entities(self):
        """Configured image-prompt identities, not claims of detected objects."""
        registered=getattr(self.pipeline.segment,'registered_entities',None)
        if registered is None:return {}
        cfg=self.config['geometry'];views=[cfg]
        if cfg.get('hole_camera'):views.append(cfg['hole_camera'])
        anchors=cfg.get('fixture_occlusion',{}).get('anchors',{})
        profile=getattr(getattr(self,'raw',None),'profile',{})
        result={view['name']:[] for view in profile.get('inspection_cameras',[])}
        for view in views:
            camera=view['camera']
            result[camera]=[dict(object_id=key,role=(
                'target_block' if key==cfg['object_id'] else 'target_aperture' if key==cfg.get('hole_id')
                else 'fixture_anchor' if key in anchors else 'fixture_region')) for key in registered(camera)]
        return result

    def camera_observation(self):
        """Images for inspection, independent of scene estimation; never motion evidence."""
        obs = self.raw.observe()
        frames = [f for f in obs.frames if f.stream == 'rgb' and f.camera in ('head', 'right', 'left')]
        inspect = getattr(self.raw, 'inspection_frame', None)
        if inspect is not None:
            # Image-only inspection can use the latest left frame even when the
            # head did not advance. This separate read never enters RGB-D fusion.
            latest = inspect()
            if latest is not None:
                frames = [frame for frame in frames if frame.camera != 'left'] + [latest]
        for frame in frames:
            data = self.raw.store.blob(frame.blob_sha256)
            if len(data) > 32 * 1024**2:
                raise ValueError('inspection_image_too_large')
            self.store.put_blob(data)
        status=self.owner.call('status')
        with self.lock:
            for frame in frames:
                key=(obs.observation_id,frame.camera)
                self.image_issuances[key]=dict(frame=frame.model_dump(mode='json'),
                    acquisition_instance=obs.acquisition_instance,host_boot_id=obs.host_boot_id,
                    context=status.get('perception_context'))
            while len(self.image_issuances)>16:self.image_issuances.pop(next(iter(self.image_issuances)))
        return dict(observation_id=obs.observation_id, frames=[f.model_dump(mode='json') for f in frames],
                    image_only=True, motion_evidence=False, faults=list(obs.faults),
                    search_entities=self.search_entities())

    def aperture_observation(self):
        """Measure optional left-wrist candidates in raw pixels without sending an image."""
        from .aperture_image import aperture_features
        from .images import decode_cached
        frame = self.raw.inspection_frame() if hasattr(self.raw, 'inspection_frame') else None
        result = dict(camera='left', frame='raw_image_pixels', motion_evidence=False,
                      available=False, candidates={})
        viewpoint = getattr(self, 'aperture_viewpoint', None)
        if viewpoint is not None:
            def _scope_matches(target_scope):
                if target_scope is None:
                    return False
                try:
                    current = self.owner.call('status').get('perception_context') or {}
                except (OSError, RuntimeError):
                    return False
                identity = tuple(current.get(k) for k in (
                    'owner_instance_id','session_id','task_id','task_hash','task_generation','epoch'))
                return (identity == target_scope[:6] and current.get('authorized') is True
                        and not current.get('action_published') and not current.get('placement_verified')
                        and (current.get('grasp_contact') or {}).get('request_id') == target_scope[-1])

            with self.lock:
                latest_missing = self.latest is None
                pending_move = deepcopy(viewpoint.move)
                scope = viewpoint.scope
            if latest_missing and pending_move is not None and _scope_matches(scope):
                deadline = time.monotonic() + 1.5
                stop = getattr(self, 'stop', None)
                while time.monotonic() < deadline:
                    if stop is not None and stop.is_set():
                        break
                    with self.lock:
                        if self.latest is not None or viewpoint.move is None:
                            break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.02, remaining))
            with self.lock:
                move = deepcopy(viewpoint.move) if self.latest is not None else None
                scope = viewpoint.scope
            if move is not None and _scope_matches(scope):
                result['observation_move'] = move
            if 'observation_move' not in result:
                result['observation_move_reason'] = 'no_distinct_measured_view_in_current_holding_episode'
        if frame is None:
            return dict(result, reason='left_rgb_unavailable')
        try:
            pixels = decode_cached(frame, self.raw.store.blob(frame.blob_sha256))
            features = aperture_features(pixels)
        except (ValueError, RuntimeError) as exc:
            return dict(result, reason=str(exc))
        return dict(result, available=True, candidates=features, source_ns=frame.source_ns,
                    received_ns=frame.received_ns, frame_id=frame.frame_id, sequence=frame.sequence,
                    source_epoch=frame.source_epoch, source_blob_sha256=frame.blob_sha256)

    def locate_target(self,session_id,owner_instance_id,task_hash,epoch,hint):
        """Queue an image search hint for the next perception sample, never a pose."""
        from .target_relocalization import rectified_search_box
        status=self.owner.call('status');context=status.get('perception_context') or {}
        scope=dict(session_id=session_id,owner_instance_id=owner_instance_id,task_hash=task_hash,epoch=epoch)
        if (not status.get('authorized') or status.get('hold_requested') or status.get('running') or status.get('pending')
                or any(context.get(k)!=v for k,v in scope.items())):
            raise ValueError('target_hint_authority_changed')
        camera=hint.get('camera');key=(hint.get('observation_id'),camera)
        with self.lock:issued=self.image_issuances.get(key)
        if (issued is None or issued['frame']!=hint.get('frame') or issued['context']!=context):
            raise ValueError('target_hint_image_scope_changed')
        cfg=self.config['geometry']
        view=cfg if cfg['camera']==camera else cfg.get('hole_camera',{})
        if view.get('camera')!=camera:raise ValueError('target_hint_camera_unregistered')
        mapped=rectified_search_box(hint,view)
        if 'entity_hints' in hint:
            entities=hint['entity_hints']
            if not isinstance(entities,list) or not 1<=len(entities)<=8:raise ValueError('invalid_entity_hints')
            catalog=self.search_entities()
            registered={row['object_id'] for row in catalog[camera]}
            seen=set()
            for entity in entities:
                identity=entity.get('object_id')
                if not isinstance(identity,str) or identity in seen:raise ValueError('invalid_entity_hints')
                if any(entity.get(k)!=hint.get(k) for k in ('camera','observation_id','frame')):
                    raise ValueError('entity_hint_image_scope_changed')
                seen.add(identity)
            unsupported=sorted(seen-registered)
            if unsupported:
                return dict(accepted=False,applied=False,reason='unregistered_entity_hint',
                    camera=camera,unsupported_entities=unsupported,search_entities=catalog,
                    next='use_registered_entity_in_its_own_shown_view',search_only=True,motion_evidence=False)
            mapped['entity_hints']=[dict(rectified_search_box(entity,view),object_id=entity['object_id'])
                                    for entity in entities]
        with self.lock:
            if not hasattr(self,'pending_target_hints'):self.pending_target_hints={}
            previous=self.pending_target_hints.get(camera)
            # One model cell can localize the block and fixture independently.
            # Keep both views and merge entities from the same issued image;
            # the latest prompt for an entity replaces only that entity.
            if previous and previous['issued']==issued and previous['context']==context:
                old=previous['hint']
                entities={e['object_id']:e for e in (old.get('entity_hints') or
                    [dict(old,object_id=cfg['object_id'])])}
                entities.update({e['object_id']:e for e in (mapped.get('entity_hints') or
                    [dict(mapped,object_id=cfg['object_id'])])})
                mapped=dict(mapped,entity_hints=list(entities.values()))
            self.pending_target_hints[camera]=dict(issued=issued,hint=mapped,context=dict(context))
            self.target_hint_generation=getattr(self,'target_hint_generation',0)+1
            self.latest,self.error=None,'scene_not_ready:target_search_hint_pending'
        self.store.event('target_search_hint_queued',camera=camera,observation_id=hint['observation_id'],
            source_blob_sha256=issued['frame']['blob_sha256'],box=mapped['box'],scope=scope,
            search_only=True,motion_evidence=False)
        return dict(accepted=True,applied=False,camera=camera,search_only=True,motion_evidence=False,
                    next='observe_new_rgbd_before_motion')

    def _apply_target_hint(self,obs,context):
        with self.lock:pending=list(getattr(self,'pending_target_hints',{}).items())
        for camera,item in pending:self._apply_camera_hint(obs,context,camera,item)

    def _apply_camera_hint(self,obs,context,camera,pending):
        issued=pending['issued'];hint=pending['hint'];camera=hint['camera']
        frame=next((f for f in obs.frames if f.camera==camera and f.stream=='rgb'),None)
        if (context!=pending['context'] or obs.acquisition_instance!=issued['acquisition_instance']
                or obs.host_boot_id!=issued['host_boot_id']):
            with self.lock:
                if self.pending_target_hints.get(camera) is pending:del self.pending_target_hints[camera]
            self.store.event('target_search_hint_discarded',reason='scope_or_acquisition_changed',camera=camera)
            return
        old=issued['frame']
        if (frame is None or frame.source_ns is None or old['source_ns'] is None
                or frame.received_ns<=old['received_ns'] or frame.source_ns<=old['source_ns']):
            return
        identity=('frame_id','width','height','encoding','camera_info_sha256','clock_domain','source_epoch','publisher_id')
        if any(getattr(frame,k)!=old[k] for k in identity):
            with self.lock:
                if self.pending_target_hints.get(camera) is pending:del self.pending_target_hints[camera]
            self.store.event('target_search_hint_discarded',reason='camera_identity_changed',camera=camera)
            return
        with self.lock:
            if self.pending_target_hints.get(camera) is not pending:return
            del self.pending_target_hints[camera]
        entities=hint.get('entity_hints') or [dict(hint,object_id=self.config['geometry']['object_id'])]
        for entity in entities:
            points={'point_prompt':entity['point_prompt']} if 'point_prompt' in entity else {}
            self.pipeline.segment.set_search_hint(view=camera,object_id=entity['object_id'],box=entity['box'],**points)
            seed=getattr(self.pipeline.segment,'seed_issued_image',None)
            if seed is not None:
                import cv2
                import numpy as np
                from .contracts import Frame
                from .images import decode
                cfg=self.config['geometry']
                view=cfg if cfg['camera']==camera else cfg['hole_camera']
                # camera_observation archived these exact bytes when issued.
                pixels=decode(Frame.model_validate(old),self.store.blob(old['blob_sha256']))
                k=np.asarray(view['k_rgb'],float)
                maps=cv2.initUndistortRectifyMap(k,np.asarray(view['d_rgb'],float),None,k,
                    (old['width'],old['height']),cv2.CV_32FC1)
                pixels=cv2.remap(pixels,*maps,interpolation=cv2.INTER_LINEAR)
                x0,y0,x1,y1=view['roi']
                source=dict(source_ns=old['source_ns'],stream=dict(
                    acquisition_instance=issued['acquisition_instance'],host_boot_id=issued['host_boot_id'],
                    camera=camera,frame_id=old['frame_id'],source_epoch=old['source_epoch'],
                    publisher_id=old['publisher_id'],width=old['width'],height=old['height']))
                seeded=seed(view=camera,object_id=entity['object_id'],rgb=pixels[y0:y1,x0:x1],source=source)
                self.store.event('target_hint_issued_image_seed',camera=camera,object_id=entity['object_id'],
                    source_blob_sha256=old['blob_sha256'],source_ns=old['source_ns'],
                    seeded=seeded,search_only=True,motion_evidence=False)
        if any(entity['object_id']==self.config['geometry']['object_id'] for entity in entities):
            reject=getattr(self.pipeline,'reject_measurement',None)
            if reject is not None:reject('mask_uncertain_requires_new_prompt')
            else:self.pipeline.invalidate()
            self.pipeline.search_hint=None
        self.pipeline.invalidate_secondary()
        self.store.event('target_search_hint_applied',camera=camera,box=hint['box'],
                         entity_ids=[entity['object_id'] for entity in entities],
                         observation_id=obs.observation_id,search_only=True,motion_evidence=False)

    def __init__(self, raw, config, mask_provider, store):
        from .frames import FrameChain
        self.raw,self.config,self.store=raw,config,store
        self.frames=FrameChain.load(config.get('frame_chain'))
        if self.frames.native_frame!=config['geometry']['native_frame']:raise ValueError('scene_frame_identity_mismatch')
        from .aperture_view import ApertureViewpoint
        envelope = config.get('aperture_view_geometry')
        self.aperture_viewpoint = (None if envelope is None else
                                   ApertureViewpoint(envelope, self.frames, config['geometry']))
        if config['geometry'].get('measured_gripper_contact'):
            config['geometry']['measured_gripper_max_age_ns']=int(config['feedback_age_ms']*1e6)
        self.pipeline=CalibratedScenePipeline(config['geometry'],mask_provider,raw.store.blob)
        from .head_kinematics import HeadKinematics
        self.head_model=HeadKinematics(config['head_kinematics']) if config.get('head_kinematics') else None
        self.owner=UnixClient(Path(config['owner_socket']),timeout=1,scene_evidence=True)
        self.numerics=UnixClient(Path(config['numerical_socket']),timeout=2)
        self.verifier=UnixClient(Path(config['verifier_socket']),timeout=1,scene_evidence=True)
        from .scene_evidence import bind_store
        self.scene_evidence_store=bind_store(Path(config['owner_socket']).parent,store)
        self.latest=None
        self.error='scene_not_ready'
        self.error_details=None
        self.archived=set()
        self.archive_bytes=0
        from .scene_failures import SceneFailureRecorder
        self.failure_recorder_error=None
        try:self.failure_recorder=SceneFailureRecorder(store)
        except Exception as exc:
            self.failure_recorder=None;self.failure_recorder_error=type(exc).__name__
        self.stop=threading.Event()
        self.lock=threading.Lock()
        self.image_issuances={};self.pending_target_hints={};self.target_hint_generation=0
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()

    def _sample(self):
        start=time.perf_counter();self.last_timing={};self._timing_at=start
        self.failure_input=None
        self.pipeline.last_timing={}
        try:return self._sample_once()
        finally:
            self.last_timing['total_ms']=(time.perf_counter()-start)*1000
            self.last_timing['pipeline']=getattr(self.pipeline,'last_timing',{})

    def _mark(self,name):
        now=time.perf_counter();self.last_timing[name]=(now-self._timing_at)*1000;self._timing_at=now

    def _ensure_fresh(self,stage,frame_times):
        self._freshness_now=time.monotonic_ns()
        age=self._freshness_now-min(frame_times)
        self.last_timing[stage+'_oldest_frame_age_ms']=age/1e6
        budget=self.config['max_age_ms']*1_000_000
        profile=self.config.get('geometry',{}).get('admission_profile','precision')
        limit=geometry_age_limit_ns(budget,admission_profile=profile,
            geometry_scope='coarse_static_task' if profile=='development_coarse' else 'precision')
        if age<0:raise ValueError('scene_clock_in_future')
        if limit is not None and age>limit:
            raise ValueError(stage+'_exceeds_observation_budget')
        if age>budget:
            self.last_timing.setdefault('quality_warnings',[]).append(stage+'_exceeds_observation_budget')

    def _validate_feedback(self,obs,feedback,now,*,camera,age_ms,target_received_ns=None):
        import numpy as np
        if feedback['host_boot_id']!=obs.host_boot_id:raise ValueError('feedback_clock_mismatch')
        reference=np.asarray(self.config['full22_reference'],float)
        actual=np.asarray(feedback['full22'],float)
        if (reference.shape!=(22,) or actual.shape!=(22,) or not np.isfinite(actual).all()
                or (getattr(self,'head_model',None) is None and
                    np.max(np.abs(actual[14:]-reference[14:]))>self.config['reference_tolerance_deg'])):
            raise ValueError('camera_body_reference_changed:'+camera)
        stamps=feedback['stamps16']
        if any(value is None for value in stamps[7:14]):raise ValueError('feedback_expired')
        state_ns=min(stamps[7:14])
        valid=tuple(s is not None and 0<=now-s<=age_ms*1_000_000 for s in stamps)
        if not all(valid[7:14]):
            # The owner returned a historical sample selected for these camera pixels.
            # A missing current encoder or a clock fault must retain its own reason.
            if (camera=='right' and target_received_ns is not None
                    and feedback.get('target_received_ns')==target_received_ns
                    and feedback.get('matched_state_received_ns')==state_ns
                    and all(0<=stamp<=now for stamp in stamps[7:14])):
                raise CameraPairUnavailable('camera_sample_feedback_aged:right')
            raise ValueError('feedback_expired:'+camera)
        return valid,state_ns

    def _current_feedback(self,obs):
        feedback=self.owner.call('measured_feedback')
        checked_ns=time.monotonic_ns()
        if self.failure_input is not None:
            self.failure_input.update(current_feedback=feedback,current_feedback_checked_ns=checked_ns)
        valid,state_ns=self._validate_feedback(obs,feedback,checked_ns,camera='current',
                                               age_ms=self.config['feedback_age_ms'])
        return feedback,valid,state_ns

    def _matched_feedback(self,obs,frames,*,live_arm):
        """Associate geometry with an actual HAL sample, never a new timestamp."""
        import json
        times=[f.received_ns for f in frames]
        if not times:raise ValueError('frame_feedback_skew:no_frames')
        target_ns=(min(times)+max(times))//2
        feedback=self.owner.call('measured_feedback',target_received_ns=target_ns)
        # The owner can receive the nearest joint sample during this RPC.
        # Measure its age on receipt, independently for each camera.
        now=time.monotonic_ns()
        camera=frames[0].camera
        if getattr(self,'failure_input',None) is not None:
            self.failure_input.setdefault('camera_feedback',{})[camera]=feedback
            self.failure_input.setdefault('camera_feedback_checked_ns',{})[camera]=now
        # Fixed-head extrinsics use history at image time. Wrist geometry needs
        # the original live-arm age bound as well as image/feedback agreement.
        age_ms=self.config['feedback_age_ms'] if live_arm else self.config['max_age_ms']
        try:
            valid,state_ns=self._validate_feedback(obs,feedback,now,camera=camera,age_ms=age_ms,target_received_ns=target_ns)
        except CameraPairUnavailable:
            self._current_feedback(obs)
            raise
        max_skew_ns=max(abs(t-state_ns) for t in times)
        if max_skew_ns>self.config['frame_state_skew_ms']*1_000_000:
            detail='frame_feedback_skew:'+json.dumps(dict(
                limit_ms=self.config['frame_state_skew_ms'],state_received_ns=state_ns,
                frames=[dict(camera=f.camera,stream=f.stream,received_ns=f.received_ns,
                             delta_ms=(f.received_ns-state_ns)/1e6) for f in frames]),sort_keys=True)
            if (live_arm and camera=='right' and feedback.get('target_received_ns')==target_ns
                    and feedback.get('matched_state_received_ns')==state_ns):
                self._current_feedback(obs)
                raise CameraPairUnavailable('camera_sample_feedback_unpaired:right',detail)
            raise ValueError(detail)
        self.last_timing.setdefault('camera_feedback',{})[camera]=dict(
            frame_oldest_age_ms=(now-min(times))/1e6,state_received_ns=state_ns,
            max_skew_ms=max_skew_ns/1e6)
        return feedback,valid,state_ns

    def _sample_once(self):
        with self.lock:hint_generation=getattr(self,'target_hint_generation',0)
        buffer=getattr(self.raw,'buffer',None)
        if buffer is not None and hasattr(buffer,'diagnostic_events'):
            for event in buffer.diagnostic_events(getattr(self,'capture_diagnostic_cursor',0)):
                # A bounded fault sample for diagnosis, independent of motion admission.
                saved=[];missing=[]
                used=getattr(self,'diagnostic_bytes',0)
                for frame in (event.get('frame'),event.get('previous')):
                    for sha in ((frame or {}).get('blob_sha256'),(frame or {}).get('camera_info_sha256')):
                        if not sha or sha in self.archived:continue
                        try:data=self.raw.store.blob(sha)
                        except ValueError:missing.append(sha);continue
                        if used+len(data)>16*1024**2:continue
                        self.store.put_blob(data);self.archived.add(sha);saved.append(sha);used+=len(data)
                        self.archive_bytes+=len(data)
                self.diagnostic_bytes=used
                self.store.event('capture_diagnostic',**event,archived_samples=saved,evicted_samples=missing)
                self.capture_diagnostic_cursor=event['sequence']
        context=self.owner.call('status').get('perception_context')
        self._mark('owner_status_ms')
        if not isinstance(context,dict):raise ValueError('owner_perception_context_missing')
        obs=self.raw.observe()
        self._offer_grasp_measurement(obs, context)
        self._apply_target_hint(obs,context)
        self.failure_input=dict(context=dict(context),observation=obs.model_dump(),
            geometry_config=self.config['geometry'] if 'geometry' in self.config else {},
            frame_chain=self.config.get('frame_chain'),feedback=None,base_eef=None,
            search_before=None)
        camera=self.config.get('geometry',{}).get('camera',obs.frames[0].camera if obs.frames else 'right')
        secondary=self.config.get('geometry',{}).get('hole_camera',{}).get('camera')
        returning=bool(context.get('placement_verified') is True and secondary)
        if returning:
            camera=secondary
        optional_camera=(self.config.get('geometry',{}).get('camera') if returning else secondary)
        secondary_only_fault=bool(optional_camera and optional_camera!=camera
            and secondary_sampling_faults(obs.faults,optional_camera))
        primary_frames=[f for f in obs.frames if f.camera==camera]
        frame_identity=tuple((f.camera,f.stream,f.sequence,f.source_ns) for f in primary_frames)
        previous=getattr(self,'last_processed_frames',None)
        prior={(f[0],f[1]):f[3] for f in previous or ()}
        unchanged=bool(frame_identity and all((f[0],f[1]) in prior for f in frame_identity)
                       and any(f[3]==prior[(f[0],f[1])] for f in frame_identity))
        if unchanged and (not obs.faults or secondary_only_fault):
            previous_context=getattr(self.pipeline,'previous_context',None)
            if previous_context is not None and _context_scope(previous_context)!=_context_scope(context):
                raise ValueError('owner_perception_context_changed')
            if secondary_only_fault and not returning:
                raise SecondaryCameraUnavailable('capture_faults:'+','.join(obs.faults))
            raise SceneNotAdvanced('waiting_for_new_camera_evidence')
        frame_times=[f.received_ns for f in obs.frames]
        if not frame_times:raise ValueError('frame_feedback_skew:no_frames')
        feedback,valid,state_ns=self._matched_feedback(obs,primary_frames,live_arm=not returning)
        head_pose=None
        if getattr(self,'head_model',None) is not None and camera=='head':
            head_pose=self.head_model.pose(feedback['full22'])
        if returning:
            # The released object is no longer tracked in the wrist. A fixed
            # head's fixture geometry and current empty-tool state are separate
            # measurements, not a claimed synchronized RGB-D/arm sample. Keep
            # original head stamps and check fresh physical feedback separately.
            feedback,valid,state_ns=self._current_feedback(obs)
            if not valid[15]:raise ValueError('feedback_expired:current_gripper')
            self.last_timing['return_state_received_ns']=state_ns
        secondary_error=None
        if secondary and secondary!=camera:
            secondary_frames=[f for f in obs.frames if f.camera==secondary]
            pair=next((p for p in obs.pairings if p.camera==secondary),None)
            if not secondary_frames or pair is None or pair.quality=='invalid':
                secondary_error=SecondaryCameraUnavailable('secondary_camera_pairing_unavailable')
            if secondary_frames:
                try:
                    head_feedback,_,_=self._matched_feedback(obs,secondary_frames,live_arm=False)
                    if getattr(self,'head_model',None) is not None and secondary=='head':
                        head_pose=self.head_model.pose(head_feedback['full22'])
                except ValueError as exc:
                    if str(exc)=='feedback_expired:'+secondary or str(exc).startswith('frame_feedback_skew:'):
                        secondary_error=SecondaryCameraUnavailable(str(exc))
                    else:raise
        self.last_timing['cross_camera_span_ms']=(max(frame_times)-min(frame_times))/1e6
        self._mark('snapshot_feedback_ms')
        obs=obs.model_copy(update=dict(state16=tuple(feedback['measured16']),state_valid_mask=valid,state_received_ns=state_ns))
        self.failure_input['feedback']=feedback
        if head_pose is not None:
            self.failure_input['head_camera_pose']=head_pose.tolist()
            self.last_timing['head_pose_mode']='measured_kinematics'
        eef=self.numerics.call('forward_pose',full22=feedback['full22'],held16=feedback['measured16'])
        self._mark('forward_ms')
        try:
            self.failure_input['base_eef']=self.frames.native_eef(eef).tolist()
            segment=getattr(self.pipeline,'segment',None)
            self.failure_input['mask_config_sha256']=getattr(segment,'configuration_sha256',None)
            hint=getattr(self.pipeline,'search_hint',None)
            self.failure_input['search_before']=dict(
                prompts=segment.tracking_snapshot() if hasattr(segment,'tracking_snapshot') else None,
                object_camera=getattr(self.pipeline,'object_camera',None),
                hint=None if hint is None else dict(received_ns=hint['received_ns'],pose=hint['pose'].tolist()))
        except Exception as exc:
            self.failure_input['search_snapshot_error']=type(exc).__name__
        self._mark('diagnostic_snapshot_ms')
        scene=None
        try:
            scene=self.pipeline.process(obs,base_eef=self.frames.native_eef(eef),context=context,
                **({'head_camera_pose':head_pose} if head_pose is not None else {}),
                **({'secondary_error':secondary_error} if secondary_error is not None else {}))
        except SecondaryCameraUnavailable as exc:
            secondary_error=exc
        finally:
            self._record_grasp_evidence(context,obs)
        self.last_processed_frames=frame_identity
        self._mark('pipeline_ms')
        current_status=self.owner.call('status')
        current_context=current_status.get('perception_context')
        self._mark('context_recheck_ms')
        if current_context!=context:
            if not isinstance(current_context,dict) or _context_scope(current_context)!=_context_scope(context):
                raise ValueError('owner_perception_context_changed')
            if any(current_context.get(k)!=context.get(k) for k in (
                    'phase','placement_verified','action_published')):
                raise SceneContextAdvanced('owner_perception_stage_advanced')
            # Pending requests can change while the same physical scene is
            # measured. Intent metadata is not a new owner or manipulation
            # state; retain original image/feedback times and current scope.
            context=current_context
        retained_fixture=(scene is not None and scene.fixture is not None
                          and scene.fixture.source=='retained_static_fixture')
        from .local_environment import local_scope
        local_evidence=(scene.local_collision_evidence if scene is not None
                        and local_scope(scene) else None)
        independent_local=(local_evidence is not None and bool(local_evidence.source_frames)
            and all(frame.camera!=secondary for frame in local_evidence.source_frames)
            and all(view.camera!=secondary for view in local_evidence.free_space_views))
        if secondary_error is not None and (scene is None or
                scene.geometry_scope!='coarse_object_verification' and not retained_fixture and not independent_local):
            self._ensure_fresh('primary_evidence_latency',[f.received_ns for f in primary_frames])
            raise secondary_error
        frame_times=[f.received_ns for f in scene.frames]
        self._ensure_fresh('perception_latency',frame_times)
        # Persist only selected scenes, not every incoming camera frame.
        for frame in scene.frames:
            for sha in (frame.blob_sha256,frame.camera_info_sha256):
                if sha and sha not in self.archived:
                    data=self.raw.store.blob(sha)
                    if self.archive_bytes+len(data)>self.config['archive_budget_bytes']:
                        raise ValueError('scene_evidence_quota_reached_new_run_required')
                    self.store.put_blob(data);self.archived.add(sha);self.archive_bytes+=len(data)
        self._mark('archive_ms')
        self._ensure_fresh('evidence_archive',frame_times)
        scene=scene.model_copy(update=dict(quality_warnings=geometry_quality_warnings(scene,
            now_ns=self._freshness_now,max_age_ns=int(self.config['max_age_ms']*1_000_000),
            admission_profile=self.config.get('geometry',{}).get('admission_profile','precision'))))
        with self.lock:
            if hint_generation!=getattr(self,'target_hint_generation',0):
                raise SceneNotAdvanced('waiting_for_new_camera_evidence')
        from .scene_evidence import archive_observation,observation_wire
        if scene.local_collision_evidence is not None or scene.measured_gripper is not None:
            scene,added_bytes=archive_observation(scene,self.store,self.scene_evidence_store['store_id'],
                remaining_bytes=self.config['archive_budget_bytes']-self.archive_bytes)
            self.archive_bytes+=added_bytes
        payload=observation_wire(scene)
        self.verifier.call('update_scene',observation=payload)
        self.owner.call('ingest_live_scene', observation=payload, context=context)
        self._mark('verification_transport_ms')
        self._ensure_fresh('verification_transport',frame_times)
        with self.lock:
            # A prompt may be queued while this sample is being processed.
            # Its old result must not satisfy the explicit post-hint observe.
            if hint_generation!=getattr(self,'target_hint_generation',0):
                raise SceneNotAdvanced('waiting_for_new_camera_evidence')
            self.latest,self.error=scene,None
            if getattr(self, 'aperture_viewpoint', None) is not None:
                self.aperture_viewpoint.update(scene, context, eef, head_pose)
            self.error_details=(self._measurement_details('partial_object_geometry'
                if scene.geometry_scope=='coarse_local_environment' else 'partial_environment_geometry')
                if local_scope(scene) else None)
        recorder=getattr(self,'failure_recorder',None)
        if (recorder is not None and secondary_error is None
                and 'transfer_above_hole' in current_status.get('completed_skills',())
                and not context.get('action_published') and context.get('authorized') is True):
            # The owner already received its usual scene. The recorder takes only
            # the raw frames and measured data used by this same perception call.
            try:
                recorder.offer_held_aperture(self.failure_input,scene,self.raw.store.blob,
                    now_ns=self._freshness_now,max_age_ns=int(self.config['max_age_ms']*1e6))
            except Exception as exc:
                self.last_timing['held_aperture_record_error']=type(exc).__name__

    def _offer_grasp_measurement(self, observation, context, *, now_ns=None):
        """Offer read-only verification before RGB-D processing, including on failed images."""
        cfg = self.config.get('geometry', {})
        skill = context.get('requested_skill')
        if (not cfg.get('measured_gripper_contact') or not context.get('authorized')
                or not context.get('grasp_contact') or not context.get('request_id')
                or skill not in ('grasp','regrasp','verify_grasp','recover_grasp')):
            return
        now = time.monotonic_ns() if now_ns is None else now_ns
        proof = self.pipeline.grasp_closure.measure(observation, context, cfg, at_ns=now)
        packet = observation.measured_gripper
        scope = {k:context.get(k) for k in (
            'owner_instance_id','session_id','task_id','task_hash','epoch','request_id')}
        if not packet or proof.get('last_ns') is None:
            # A known sensor outage retires the last positive sample immediately.
            self.verifier.call('update_grasp_measurement', scope=scope,
                close_request_id=context['grasp_contact']['request_id'], held=None,
                reason=proof['reason'], measured_ns=now, max_age_ns=cfg['measured_gripper_max_age_ns'],
                evidence_id=None)
            self.last_gripper_receipt_key = None
            return
        key = (tuple(scope.values()), proof['request_id'], packet.source_instance,
               proof['last_sequence'], proof['held'], proof['reason'])
        if key == getattr(self, 'last_gripper_receipt_key', None):
            return
        evidence = dict(scope=scope, published_close=context['grasp_contact'], verdict=proof,
            host_boot_id=observation.host_boot_id, source=packet.source, protocol=packet.protocol,
            source_instance=packet.source_instance, samples=[row.model_dump() for row in packet.samples
                if proof['first_sequence'] <= row.sequence <= proof['last_sequence']])
        data = json.dumps(evidence, sort_keys=True, separators=(',',':'), allow_nan=False).encode()
        if self.archive_bytes + len(data) > self.config['archive_budget_bytes']:
            raise ValueError('scene_evidence_quota_reached_new_run_required')
        evidence_id = self.store.put_blob(data)
        self.archive_bytes += len(data)
        self.verifier.call('update_grasp_measurement', scope=scope, close_request_id=proof['request_id'],
            held=proof['held'], reason=proof['reason'], measured_ns=proof['last_ns'],
            max_age_ns=cfg['measured_gripper_max_age_ns'], evidence_id=evidence_id)
        self.last_gripper_receipt_key = key

    def _record_grasp_evidence(self,context,observation):
        """Keep measured following inputs, including frames never admitted for motion.

        Sparse one-second timing samples cannot reconstruct a short lift. These
        bounded records stay on the perception worker and grant no authority.
        Diagnostic I/O failure must not become a new motion condition.
        """
        if context.get('phase') not in ('grasp','regrasp','verify_grasp','recover_grasp','inspect_in_hand',
                                        'transfer_above_hole','move_tcp','align','insert','retreat'):
            return
        try:
            evidence=getattr(self.pipeline,'last_timing',{}).get('primary_evidence')
            history=getattr(self.pipeline,'stationary_history',())
            if not evidence or not history:return
            sample=history[-1]
            if sample['time']!=evidence['received_ns']:return
            key=(observation.acquisition_instance,evidence['camera'],tuple(evidence['source_ns']))
            if key==getattr(self,'last_grasp_diagnostic_key',None):return
            self.store.event('grasp_evidence_sample',diagnostic_only=True,
                context=dict(context),observation_id=observation.observation_id,
                measurements=evidence,object_pose=sample['obj'].tolist(),
                eef_pose=sample['eef'].tolist(),relative_pose=sample['relative'].tolist(),
                joints=sample['joints'].tolist(),gripper=sample['gripper'],frames=[f.model_dump() for f in observation.frames
                    if f.camera==evidence['camera']])
            self.last_grasp_diagnostic_key=key
        except Exception as exc:
            # The normal scene/feedback checks remain authoritative. Failure
            # to serialize this extra diagnostic must not invalidate them.
            if isinstance(getattr(self,'last_timing',None),dict):
                self.last_timing['grasp_diagnostic_error']=type(exc).__name__

    def _measurement_details(self,reason):
        """Snapshot only this failed sample's bounded measurement diagnostics."""
        try:
            sample=getattr(self,'failure_input',None) or {}
            timing=(getattr(self,'last_timing',{}).get('pipeline') or
                    getattr(self.pipeline,'last_timing',{}) or {})
            attempts=timing.get('object_geometry_attempts',[])
            observation=sample.get('observation') or {};context=sample.get('context') or {}
            if not attempts or any(a.get('observation_id')!=observation.get('observation_id') for a in attempts):return None
            details=dict(diagnostic_only=True,reason=reason,
                observation_id=observation.get('observation_id'),calibration_id=observation.get('calibration_id'),
                scope={k:context.get(k) for k in ('owner_instance_id','session_id','task_id','task_hash','task_generation','epoch')},
                attempts=deepcopy(attempts[:3]))
            if len(json.dumps(details,allow_nan=False).encode())>12000:
                for row in details['attempts']:row['metrics']={'omitted':'local_diagnostic_size_limit'}
            return details
        except Exception:return None

    def _run(self):
        previous=None
        previous_cycle=None
        while not self.stop.is_set():
            cycle_started=time.perf_counter()
            try:self._sample()
            except SceneNotAdvanced:
                # Polling faster than a complete multi-camera frame is not a
                # bad measurement. Never refresh timestamps or re-ingest it.
                pass
            except Exception as exc:
                # Any failed update invalidates the public scene. Normal owner progress
                # does not erase independently measured grasp/fixture history; the next
                # new frame is still rechecked under the current phase and authority.
                code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                details=self._measurement_details(code)
                recorder=getattr(self,'failure_recorder',None)
                if recorder is not None and not isinstance(exc,SceneContextAdvanced):
                    snapshot=getattr(self,'failure_input',None)
                    if snapshot is not None and details is not None:
                        snapshot=dict(snapshot,measurement_diagnostics=details)
                    recorder.offer(code,snapshot,self.raw.store.blob)
                # Late delivery is not evidence that a previously measured
                # grasp slipped. Keep original sample times; pipeline._context
                # still invalidates across its bounded evidence gap or epoch.
                if isinstance(exc,SecondaryCameraUnavailable):
                    self.pipeline.invalidate_secondary()
                elif not isinstance(exc,(SceneContextAdvanced,CameraPairUnavailable)) and code not in PROCESSING_EXPIRY:
                    reject=getattr(self.pipeline,'reject_measurement',None)
                    if reject is not None:reject(code)
                    else:self.pipeline.invalidate()
                owner_reason=exc.owner_reason if isinstance(exc,CameraPairUnavailable) else code[:256]
                try:self.owner.call('invalidate_live_scene',reason=owner_reason)
                except Exception:pass
                with self.lock:self.latest,self.error,self.error_details=None,code,details
                if code!=previous:self.store.event('scene_unavailable',reason=code,details=details)
                previous=code
            if isinstance(getattr(self,'last_timing',None),dict):
                self.last_timing['sample_interval_ms']=(None if previous_cycle is None else
                    (cycle_started-previous_cycle)*1000)
            previous_cycle=cycle_started
            now=time.perf_counter()
            if now-getattr(self,'last_timing_event_s',0)>=1:
                recorder=getattr(self,'failure_recorder',None)
                self.store.event('scene_timing',timing=getattr(self,'last_timing',{}),error=self.error,
                    diagnostics=recorder.status() if recorder is not None else
                        dict(unavailable=getattr(self,'failure_recorder_error',None)))
                self.last_timing_event_s=now
            # poll_seconds is the target sampling interval. Network/GPU work
            # already consumes that interval; adding a full sleep afterwards
            # can create evidence gaps even while the cameras keep streaming.
            self.stop.wait(max(.001,self.config.get('poll_seconds',.05)
                               -(time.perf_counter()-cycle_started)))

    def observe(self):
        with self.lock:
            scene,error=self.latest,self.error
        if scene is None:raise ValueError(error or 'scene_not_ready')
        acquisition=min([scene.received_ns]+[f.received_ns for f in scene.frames])
        now=time.monotonic_ns()
        profile=self.config.get('geometry',{}).get('admission_profile','precision')
        limit=geometry_age_limit_ns(int(self.config['max_age_ms']*1_000_000),
            admission_profile=profile,geometry_scope=scene.geometry_scope)
        if now<acquisition or (limit is not None and now-acquisition>limit):
            raise ValueError('scene_expired')
        return scene.model_copy(update=dict(quality_warnings=geometry_quality_warnings(scene,
            now_ns=now,max_age_ns=int(self.config['max_age_ms']*1_000_000),admission_profile=profile)))

    def readiness(self):
        from .conditions import observation_condition
        from .local_environment import local_scope
        with self.lock:
            local=self.latest is not None and local_scope(self.latest)
            if self.latest is not None and self.latest.geometry_scope=='coarse_object_verification' and not local:
                result=dict(state='waiting',code='required_camera_pending:head',scope='camera:head',
                            recovery='await_fixture_evidence',available_skills=['verify_grasp','inspect_in_hand','reobserve'])
            else:result=observation_condition(self.error or ('ready' if self.latest is not None else 'scene_not_ready'))
            if local:
                result.update(geometry_scope=self.latest.geometry_scope,geometry_status='partial')
            if getattr(self,'error_details',None) is not None:
                result['details']=deepcopy(self.error_details)
        result['search_entities']=self.search_entities()
        return result

    def close(self):
        self.stop.set();self.thread.join(timeout=5)
        if self.failure_recorder is not None:self.failure_recorder.close()
        self.pipeline.close()
        self.raw.close()
