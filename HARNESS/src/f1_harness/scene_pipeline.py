"""Measured RGB-D + prompted masks -> task scene. No planner-authored pose or verdict input."""
from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, wait
import math
import time

import numpy as np

from .contracts import SceneObject, FixturePose, digest
from .geometry import transform, register_depth, align_square_pose, insertion_depth_reached
from .images import decode_cached as decode
from .scene import object_from_mask
from .conditions import object_measurement_recoverable
from .gripper_state import observed_open
from .gripper_contact import GraspClosureEpisode, target_in_closure_region


CACHED_APERTURE_MAX_AGE_NS = 45_000_000_000
OCCLUDED_FIXTURE_RETAIN_NS = CACHED_APERTURE_MAX_AGE_NS
OCCLUDED_FIXTURE_RETAIN_REASONS = frozenset((
    'bucket_landmark_registration_unavailable',
    'partial_lid_landmarks_unavailable',
))


class SecondaryCameraUnavailable(ValueError):
    """No full scene; independently validated primary evidence may be retained."""


def secondary_sampling_faults(faults, camera):
    """Only rejected samples; identities/clock epochs are checked separately."""
    return bool(faults) and set(faults)<= {
        code+':'+str(camera) for code in
        ('duplicate_source_stamp','source_time_reversed','sequence_not_increasing')}


class CalibratedScenePipeline:
    def __init__(self,config,mask_provider,blob_reader):
        self.config,self.segment,self.blob=config,mask_provider,blob_reader
        # Motion evidence must survive the later stationary verification window.
        self.stationary_history=deque(maxlen=256)
        self.tracking_history=deque(maxlen=256)
        self.revision=0
        self.in_hand_reference=None
        self.not_in_hand_reference=None
        self.in_hand_gripper=None
        self.contact_attachment=None
        self.grasp_closure=GraspClosureEpisode()
        self.gripper_seen=None
        self.in_hand_quarantine=None
        self.in_hand_recovery_deadline_ns=None
        self.square_pose_reference=None
        self.observed_face_object=None
        self.last_stamps=None
        self.object_camera=None
        self.object_view_stamps={}
        self.last_received_ns=None
        self.context_identity=None
        self.previous_context=None
        self.acquisition_identity=None
        self.stream_identities={}
        self.hole_last_visible_ns=None
        self.live_hole=None
        self.live_fixture_anchors=None
        self.measured_fixture=None
        self.cached_aperture=None
        self._occluded_fixture_retained=False
        self._occluded_fixture_expired=False
        self.lid_registration=None
        self.search_hint=None
        self.return_history=deque(maxlen=256)
        self.return_stamps=None
        self.rectification=None
        self.secondary_maps={}
        self.secondary_cache=None
        self.secondary_executor=None
        self.secondary_future=None
        self.development=config.get('goal','placed')=='approach_grasp'
        self.coarse=config.get('admission_profile')=='development_coarse'
        search_window=config.get('search_hint_window_ns',config.get('tracking_window_ns',0))
        if config.get('search_object_box') and (type(search_window) is not int or not 0<search_window<=30_000_000_000):
            raise ValueError('bounded_search_hint_window_required')
        preferred=config.get('pregrasp_object_camera')
        secondary=(config.get('hole_camera') or {}).get('camera')
        registered_pregrasp=(preferred==config.get('camera') or
            preferred==secondary==config.get('object_reacquisition_camera'))
        if preferred is not None and (not self.coarse or self.development or not registered_pregrasp):
            raise ValueError('pregrasp_view_requires_registered_coarse_camera')
        required=('calibration_id','base_camera','k_rgb','d_rgb','k_depth','d_depth','rgb_depth',
                  'rgb_info_sha256','depth_info_sha256','depth_scale_m','object_id','hole_id',
                  'base_hole','face_object','native_frame','uncertainty_m','hole_clearance_m',
                  'insertion_depth_m','xy_tolerance_m','yaw_tolerance_rad','stationary_window_ns',
                  'stationary_delta_deg','object_stationary_m','gripper_open_range','hole_reprojection_px',
                  'tilt_tolerance_rad',
                  'camera','roi')
        if self.development:
            unused={'hole_id','base_hole','uncertainty_m','hole_clearance_m','insertion_depth_m',
                    'xy_tolerance_m','yaw_tolerance_rad','gripper_open_range','hole_reprojection_px','tilt_tolerance_rad'}
            required=tuple(k for k in required if k not in unused)+('planning_margin_m',)
            required+=('reference_gripper_closure','reference_gripper_tolerance')
            if config.get('camera')=='right':
                required=tuple(k for k in required if k!='base_camera')+('eef_camera',)
        if any(k not in config for k in required):raise ValueError('incomplete_measured_scene_configuration')
        if self.development:
            if (config['camera'] not in ('head','right') or not 0<config['planning_margin_m']<=.05
                    or not 200_000_000<=config['stationary_window_ns']<=2_000_000_000
                    or not 0<config['stationary_delta_deg']<=.5
                    or not 0<config['object_stationary_m']<=.01):
                raise ValueError('bounded_static_development_configuration_required')
        elif self.coarse:
            if (config['camera']!='right' or not config.get('eef_camera') or not config.get('hole_camera')
                    or not 0<config['planning_margin_m']<=.05 or not 0<config['tracking_tolerance_m']<=.02
                    or not 200_000_000<=config['stationary_window_ns']<=2_000_000_000):
                raise ValueError('full_coarse_scene_configuration_required')
        elif not 0<config['uncertainty_m']<config['hole_clearance_m']:raise ValueError('scene_error_budget')
        self.occlusion=config.get('fixture_occlusion')
        self.fixture_reference_camera=None
        if config.get('independent_fixture_localization',False):
            if not self.coarse or self.occlusion is None:
                raise ValueError('independent_fixture_requires_coarse_registered_anchors')
            self.fixture_reference_camera=transform(config['hole_camera']['base_camera']).copy()
        if self.occlusion is not None:
            oc=self.occlusion
            policy=oc.get('depth_policy','strict')
            if policy not in ('strict','coarse_margin_warning') or (
                    policy=='coarse_margin_warning' and not self.coarse):
                raise ValueError('fixture_depth_warning_requires_coarse_development')
            if (oc.get('validated') is not True or not oc.get('evidence_ids')
                    or oc.get('calibration_id')!=config['calibration_id']
                    or not oc.get('fixture_id') or len(oc.get('anchors',{}))<3
                    or not 0<oc.get('max_occlusion_ns',0)
                    or not 0<oc.get('pixel_tolerance',0)
                    or not 0<oc.get('depth_tolerance_m',0)):
                raise ValueError('independently_validated_fixture_anchors_required')
            points=np.asarray([a['roi_center_px'] for a in oc['anchors'].values()],float)
            if points.shape!=(len(oc['anchors']),2) or not np.all(np.isfinite(points)) or np.linalg.matrix_rank(points-points[0])<2:
                raise ValueError('fixture_anchors_must_be_noncollinear')
            if any(not np.isfinite(a['depth_m']) or a['depth_m']<=0 for a in oc['anchors'].values()):
                raise ValueError('fixture_anchor_depth_required')

    def invalidate(self):
        """Discard evidence on an explicit identity/authority reset, not a bad sample."""
        # A short-lived search-only hint is not pose/held evidence. Its scope
        # and age are checked independently before it can suggest image pixels.
        self.stationary_history.clear();self._clear_following()
        self.measured_fixture=None
        self.cached_aperture=None
        self.return_history.clear()
        self.square_pose_reference=None
        self.observed_face_object=None
        self.hole_last_visible_ns=None
        self.live_hole=None;self.live_fixture_anchors=None

    def reject_measurement(self, reason):
        """A bad visual sample suspends a proven attachment, never invents one.

        The existing quarantine requires three new independent measurements
        consistent with the old transform, gripper and scope. Its original
        deadline is retained across repeated failures; invalid identity still
        discards everything. A feedback gap suspends current usability.
        """
        from .grasp_tracking import quarantine_following
        # The caller rejected a measurement, not the task's historical facts.
        # Scope, calibration and opening are invalidated at their source below;
        # new camera errors must not need an ever-growing string whitelist.
        proof=self.in_hand_quarantine
        empty_reference=self.not_in_hand_reference
        face_object=self.observed_face_object
        prior_fixture=(self.measured_fixture if self.measured_fixture is not None
                       and self.measured_fixture.source=='rgbd_anchor_registration' else None)
        prior_cached_aperture=getattr(self,'cached_aperture',None)
        history=list(self.tracking_history)
        if self.in_hand_reference is not None and self.tracking_history:
            sample=self.tracking_history[-1]
            deadline=(None if self.coarse else self.in_hand_recovery_deadline_ns or
                      sample['time']+self.config.get('tracking_window_ns',2_000_000_000))
            proof=quarantine_following(self.in_hand_reference,self.in_hand_gripper,
                                      sample,expires_ns=deadline)
        self.invalidate()
        self.observed_face_object=face_object
        self.not_in_hand_reference=empty_reference
        if prior_fixture is not None:
            self.measured_fixture=prior_fixture
        if prior_cached_aperture is not None:
            self.cached_aperture=prior_cached_aperture
        # These are historical samples, not a current observation or permission
        # to move. The next real frame still checks scope, grip, time and pose;
        # the rolling window expires old pre-proof samples normally.
        if proof is None:self.tracking_history.extend(history)
        if proof is not None:
            self.in_hand_quarantine=proof
            self.in_hand_recovery_deadline_ns=proof['expires_ns']

    def _clear_following(self):
        """No isolated or active attachment proof survives invalid measurement scope."""
        self.tracking_history.clear()
        self.in_hand_reference=self.in_hand_gripper=None
        self.contact_attachment=None
        self.not_in_hand_reference=None
        self.in_hand_quarantine=self.in_hand_recovery_deadline_ns=None
        self.cached_aperture=None

    def invalidate_secondary(self):
        self.hole_last_visible_ns=None
        self.live_hole=None;self.live_fixture_anchors=None
        self.secondary_cache=None

    def close(self):
        if self.secondary_executor is not None:
            self.secondary_executor.shutdown(wait=True,cancel_futures=True)
            self.secondary_executor=None

    def _secondary_view(self,observation,config,*,search_boxes=None):
        """One bounded, context-free cached camera computation; no newer times."""
        from .scene_views import rectified_view
        frames=tuple(f for f in observation.frames if f.camera==config['camera'])
        pairs=tuple(p for p in observation.pairings if p.camera==config['camera'])
        key=(digest(config),tuple(digest(f) for f in frames),tuple(digest(p) for p in pairs),
             digest(search_boxes or {}))
        if self.secondary_cache is not None and self.secondary_cache[0]==key:
            return (*self.secondary_cache[1],dict(head_view_ms=0.,head_mask_ms=0.,head_cache_hit=True))
        start=time.perf_counter()
        rgb,depth,evidence=rectified_view(observation,config,self.blob,self.secondary_maps,allow_receipt=True)
        rectified=time.perf_counter()
        masks=self._segment_view(rgb,observation,config['camera'],search_boxes)
        result=(depth,evidence,masks)
        # Keep the rectified pixels with their validated RGB-D identity. A
        # second current-frame measurement can use them without another decode.
        self.secondary_cache=(key,result,rgb)
        return (*result,dict(head_view_ms=(rectified-start)*1000,
            head_mask_ms=(time.perf_counter()-rectified)*1000,head_cache_hit=False))

    def _segment_view(self,rgb,observation,camera,boxes=None,exclusion_boxes=None,candidate_depth_filters=None):
        source_bound=getattr(self.segment,'segment_frame',None)
        if source_bound is not None:
            frame=next(f for f in observation.frames if f.camera==camera and f.stream=='rgb')
            source=dict(source_ns=frame.source_ns,stream=dict(
                acquisition_instance=observation.acquisition_instance,
                host_boot_id=observation.host_boot_id,camera=camera,frame_id=frame.frame_id,
                source_epoch=frame.source_epoch,publisher_id=frame.publisher_id,
                width=frame.width,height=frame.height))
            extra={}
            if exclusion_boxes:
                extra['exclusion_boxes']=exclusion_boxes
            if candidate_depth_filters:
                extra['candidate_depth_filters']=candidate_depth_filters
            return source_bound(rgb,view=camera,source=source,boxes=boxes,**extra)
        return (self.segment.with_search_boxes(rgb,view=camera,boxes=boxes) if boxes
                else self.segment(rgb,view=camera))

    def _project_fixture_exclusion_box(self,fixture_pose,base_camera,camera_cfg,padding_px):
        """Project the measured bucket collision box into rectified ROI pixels with near-plane clipping."""
        from itertools import product
        from .geometry import crop_intrinsics
        local_box=self.config.get('fixture_exclusion_box')
        if not local_box or fixture_pose is None or fixture_pose.source!='rgbd_anchor_registration':
            return None
        low=np.asarray(local_box['min'],float);high=np.asarray(local_box['max'],float)
        if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or np.any(low>=high):
            return None
        bits=list(product((0,1),repeat=3))
        corners=np.array([[low[d] if b[d]==0 else high[d] for d in range(3)] for b in bits],float)
        rel=np.linalg.inv(transform(base_camera))@transform(fixture_pose.pose)
        pts=corners@rel[:3,:3].T+rel[:3,3]
        near=0.01
        clipped=[p for p in pts if p[2]>=near]
        for i in range(8):
            for j in range(i+1,8):
                if sum(bits[i][d]!=bits[j][d] for d in range(3))==1:
                    p0,p1=pts[i],pts[j]
                    if (p0[2]<near)!=(p1[2]<near):
                        t=(near-p0[2])/(p1[2]-p0[2])
                        clipped.append(p0+t*(p1-p0))
        if not clipped:
            return None
        pts_front=np.asarray(clipped,float)
        pixels=pts_front@crop_intrinsics(camera_cfg['k_rgb'],camera_cfg['roi']).T
        pixels=pixels[:,:2]/pixels[:,2:]
        width,height=np.asarray(camera_cfg['roi'][2:])-camera_cfg['roi'][:2]
        pad=max(0,int(padding_px))
        x0=max(0,int(np.floor(pixels[:,0].min()))-pad)
        y0=max(0,int(np.floor(pixels[:,1].min()))-pad)
        x1=min(int(width),int(np.ceil(pixels[:,0].max()))+pad)
        y1=min(int(height),int(np.ceil(pixels[:,1].max()))+pad)
        if x1<=x0 or y1<=y0:
            return None
        return [x0,y0,x1,y1]

    def _locate_bucket_for_object(self, observation, object_camera, head_view):
        """Register a bucket body against this observation's head RGB-D and FK."""
        from .fixture_tracking import locate_fixture, locate_unprompted_bucket
        depth,evidence,masks=head_view
        cfg=self.config
        camera=cfg['hole_camera']
        anchors=self.occlusion['anchors']
        registration=camera,self.fixture_reference_camera,cfg['base_hole'],anchors
        from .lid_registration import LidRegistration
        current={(f.camera,f.stream):f.blob_sha256 for f in observation.frames}
        calibrated_head=camera['camera']=='head' and cfg.get('measured_head_pose')
        current_head=(calibrated_head and self.secondary_cache is not None and self.secondary_cache[1][1]==evidence
            and evidence==(current.get(('head','rgb')),current.get(('head','depth'))))
        rgb=self.secondary_cache[2] if current_head else None
        tolerance=cfg['planning_margin_m']
        measured_anchors=None
        try:
            pose,measured_anchors,details=locate_fixture(depth,masks,*registration,identity_tolerance_m=tolerance)
        except ValueError as exc:
            if (not calibrated_head or object_camera!='right'
                    or not str(exc).startswith('fixture_relocalization_anchor_missing:')):
                raise
            if not current_head:
                raise ValueError('bucket_landmark_frame_identity_changed') from exc
            # The contour fallback must not replace contradictory prompted identities.
            prompted=any(key in masks and np.any(masks[key]) for key in (*anchors,cfg['hole_id']))
            if not prompted:
                try:
                    pose,measured_anchors,details=locate_unprompted_bucket(rgb,depth,*registration,
                        identity_tolerance_m=tolerance)
                    self.last_timing['bucket_landmark_bootstrap']=dict(primary_error=str(exc))
                except ValueError as missing:
                    if (getattr(self,'lid_registration',None) is None
                            or str(missing)!='bucket_landmark_registration_unavailable'):
                        raise
            if measured_anchors is None:
                if getattr(self,'lid_registration',None) is None:
                    raise
                pose,details=self.lid_registration.measure(rgb,depth,camera,tolerance,cfg['hole_clearance_m'])
                if prompted:
                    from .lid_registration import verify_remaining_anchors
                    verify_remaining_anchors(pose,depth,masks,*registration,tolerance)
        if current_head and measured_anchors is not None and getattr(self,'lid_registration',None) is None:
            try:
                self.lid_registration=LidRegistration.from_landmarks(rgb,depth,camera,pose,measured_anchors,
                    evidence,tolerance)
            except ValueError:
                pass  # The directly measured three-anchor fixture remains usable.
        self.last_timing['independent_fixture']=details
        return FixturePose(fixture_id=self.occlusion['fixture_id'],pose=tuple(pose.ravel()),
            reference_pose=tuple(transform(cfg['base_hole']).ravel()),frame_id=cfg['native_frame'],
            evidence_ids=evidence,measured_at_ns=observation.received_ns)

    def _object_search_hints(self,observation,config,base_camera,*,enabled,base_eef=None):
        """Old object evidence narrows image search only, never supplies a pose.

        Compute before submitting a background view, so that worker never reads
        mutable primary tracking state. Scope resets happen in _context first.
        """
        cfg=self.config;hint=self.search_hint
        if (not enabled or hint is None or not cfg.get('search_object_box')
                or observation.received_ns<=hint['received_ns']
                or not hasattr(self.segment,'with_search_boxes')):
            return {}
        from .search_projection import object_search_box
        pose=hint['pose']
        opening=cfg.get('gripper_open_range')
        closed=(opening is not None and observation.state_valid_mask is not None
                and observation.state_valid_mask[15] and observation.state16 is not None
                and self._gripper_value(observation)>opening[1])
        if closed and base_eef is not None and hint.get('eef_object') is not None:
            # Candidate attachment predicts image pixels only. Independent
            # RGB-D evidence, never this assumption, establishes held=True.
            pose=transform(base_eef)@hint['eef_object']
        box=object_search_box(pose,base_camera,cfg['search_object_box'],config,
                              margin_m=cfg['planning_margin_m'])
        return {} if box is None else {cfg['object_id']:box}

    def _context(self,context,received_ns):
        context=context or {}
        identity=tuple(context.get(k) for k in ('owner_instance_id','session_id','task_id','task_hash','task_generation','epoch'))
        previous=self.previous_context or {}
        if identity!=self.context_identity or (previous.get('authorized') and not context.get('authorized')):
            self.lid_registration=None
            if self.context_identity is not None:
                reset=getattr(self.segment,'reset_visual_tracking',None)
                if reset is not None:reset()
            self.invalidate()
            self.search_hint=None
            self.return_history.clear();self.return_stamps=None
        # Request identity is intent, not a new physical grasp. Both following
        # and nonfollowing history survive planning/revalidation; current grip,
        # scope and independent measured motion update them at their sources.
        gap=self.config.get('evidence_max_gap_ns',500_000_000)
        if self.last_received_ns is not None and not 0<received_ns-self.last_received_ns<=gap:
            if received_ns>self.last_received_ns:
                # Sampling cadence is not a failed measurement. In coarse mode
                # the current independent pair still checks the saved attachment,
                # gripper and scope below. Existing time windows and pair identity
                # checks still apply; elapsed cadence alone rejects no new pair.
                if self.coarse:
                    self.last_timing['quality_warnings']=['measurement_gap']
                else:self.reject_measurement('measurement_gap')
            else:
                self.invalidate()
                self.lid_registration=None
        self.context_identity=identity
        self.previous_context=dict(context)
        return context

    def _associate_object_view(self,pose,camera,observation,base_eef):
        """Associate a new camera with the measured instance before committing it.

        Search projections and matching IDs are not measurements of identity.
        Compare physical object volumes using the existing measurement error;
        a closed hand may carry the object, so retain that motion hypothesis.
        An unresolved association invalidates this candidate only. The caller
        can localize/observe again; no historical pose is returned as fresh.
        """
        cfg=self.config
        diagnostics=getattr(self.segment,'diagnostic_snapshot',None)
        provenance=({} if diagnostics is None else
                    diagnostics().get(camera,{}).get(cfg['object_id'],{}).get('tracking',{}))
        if provenance.get('identity_status')=='unresolved':
            self.last_timing['object_identity']=dict(status='unresolved',camera=camera,
                reason=provenance.get('identity_reason') or provenance.get('seed_reason'),
                recovery='relocalize_target',motion_evidence=False)
            raise ValueError('object_identity_unresolved:camera='+camera)
        hint=self.search_hint
        if (self.object_camera is None or self.object_camera==camera or hint is None
                or not cfg.get('search_object_box')):
            return
        from .grasp_tracking import separated_attachment
        hypotheses=self._object_identity_hypotheses(observation,base_eef)
        tolerance=cfg.get('tracking_tolerance_m',cfg['uncertainty_m'])
        matches=[name for name,expected in hypotheses
                 if not separated_attachment(expected,pose,cfg['search_object_box'],tolerance)]
        self.last_timing['object_identity']=dict(status='associated' if matches else 'unresolved',
            previous_camera=self.object_camera,camera=camera,matching_hypotheses=matches,
            displacement_m=float(np.linalg.norm(pose[:3,3]-hint['pose'][:3,3])),
            association='registered_object_volume_with_measurement_error',
            recovery=None if matches else 'relocalize_target',motion_evidence=False)
        if not matches:
            raise ValueError('object_identity_unresolved:cross_camera_association:camera='+camera)

    def _object_identity_hypotheses(self,observation,base_eef):
        """Existing stationary/carried hypotheses, without claiming either is true."""
        cfg=self.config;hint=self.search_hint
        hypotheses=[('world',hint['pose'])]
        low,high=cfg.get('gripper_open_range',(0,0))
        # F1's zero is more open than the registered release setting. Being
        # outside that narrow setting is not itself evidence of closed fingers.
        closed=observation.state_valid_mask[15] and self._gripper_value(observation)>high
        if closed:
            attachment=self.in_hand_reference
            if attachment is None and self.not_in_hand_reference is None:
                attachment=hint.get('eef_object')
            if attachment is not None:
                hypotheses.append(('hand_motion',transform(base_eef)@attachment))
        return hypotheses

    def _fixture_visible(self,masks,depth):
        """Three independently identified non-collinear fixture anchors, in current RGB-D."""
        oc=self.occlusion
        if oc is None:return False
        checks={}
        valid=True
        for key,expected in (self.live_fixture_anchors or oc['anchors']).items():
            mask=masks.get(key)
            row=dict(expected_center_px=expected['roi_center_px'],expected_depth_m=expected['depth_m'],
                     pixel_tolerance=oc['pixel_tolerance'],depth_tolerance_m=oc['depth_tolerance_m'])
            checks[key]=row
            if mask is None or mask.sum()<20:
                row.update(valid=False,reason='missing_anchor',mask_pixels=0 if mask is None else int(mask.sum()))
                valid=False
                continue
            yy,xx=np.nonzero(mask)
            center=np.array([xx.mean(),yy.mean()])
            pixel_error=float(np.linalg.norm(center-expected['roi_center_px']))
            values=depth[mask]
            values=values[np.isfinite(values)&(values>0)]
            measured_depth=float(np.median(values)) if len(values) else None
            depth_error=None if measured_depth is None else measured_depth-expected['depth_m']
            # An anchor covered by the target is not independent fixture evidence.
            obj=masks.get(self.config['object_id'])
            covered=bool(obj is not None and np.logical_and(mask,obj).any())
            reason=('pixel_displacement' if pixel_error>oc['pixel_tolerance'] else
                    'insufficient_depth' if len(values)<20 else
                    'depth_displacement' if abs(depth_error)>oc['depth_tolerance_m'] else
                    'covered_by_target' if covered else None)
            # Explicit coarse-development policy: the precision reference may
            # drift within the already configured geometric planning margin.
            # It cannot excuse missing depth, target overlap, pixel displacement,
            # or a shift beyond the clearance already used by the path planner.
            warning=bool(reason=='depth_displacement' and not covered and self.coarse
                         and oc.get('depth_policy')=='coarse_margin_warning'
                         and abs(depth_error)<=self.config['planning_margin_m'])
            accepted=reason is None or warning
            row.update(valid=accepted,reason=reason,severity='warning' if warning else 'error' if reason else 'ok',
                       center_px=center.tolist(),pixel_error=pixel_error,
                       mask_pixels=int(mask.sum()),depth_support=len(values),measured_depth_m=measured_depth,
                       depth_error_m=depth_error,covered_by_target=covered)
            valid=valid and accepted
        # Keep physical evidence and the configured policy explicit. Record all anchors
        # so a common depth shift is distinguishable from a single missed mask.
        if not hasattr(self,'last_timing'):self.last_timing={}
        self.last_timing['fixture_check']=dict(valid=valid,anchors=checks)
        return valid

    def process(self,observation,*,base_eef,context=None,secondary_error=None,head_camera_pose=None):
        start=time.perf_counter();self.last_timing={};self._timing_at=start
        self._occluded_fixture_retained=False;self._occluded_fixture_expired=False
        self._partial_targets=[];self._partial_views={}
        # Environment evidence is recomputed from this frame, never inherited
        # from a previous partial/object-only observation supplied by a caller.
        if observation.local_collision_evidence is not None or observation.local_collision_evidence_ref is not None:
            observation=observation.model_copy(update=dict(local_collision_evidence=None,local_collision_evidence_ref=None))
        original_config=self.config
        if self.config.get('measured_head_pose') is True:
            if head_camera_pose is None and secondary_error is None:
                raise ValueError('measured_head_camera_pose_required')
            if head_camera_pose is not None:
                self.config=dict(self.config,hole_camera=dict(self.config['hole_camera'],
                    base_camera=transform(head_camera_pose).tolist()))
        self.secondary_future=None
        begin=getattr(self.segment,'begin_tracking_frame',None)
        token=begin() if begin is not None else None
        accepted_views=()
        try:
            result=self._process(observation,base_eef=base_eef,context=context,secondary_error=secondary_error)
            if result.geometry_scope not in ('coarse_local_environment','coarse_object_verification') and result.local_collision_evidence is not None:
                result=result.model_copy(update=dict(local_collision_evidence=None,local_collision_evidence_ref=None))
            accepted_views=None
            return result
        except SecondaryCameraUnavailable:
            self.invalidate_secondary()
            # Primary geometry/tracking may already have been independently
            # established. Do not commit the failed fixture view's prompts.
            accepted_views=(self.config['camera'],)
            raise
        except Exception as exc:
            prior_fixture=getattr(self,'measured_fixture',None)
            self.reject_measurement(str(exc))
            if (object_measurement_recoverable(str(exc),object_id=self.config['object_id'])
                    or str(exc)=='secondary_camera_pairing_unavailable'):
                partial=self._partial_observation(observation,str(exc),prior_fixture=prior_fixture)
                if partial is not None:
                    accepted_views=(partial.geometry_camera,)
                    if any(o.object_id==self.config['hole_id'] and o.visible and o.pose is not None
                           for o in partial.objects):
                        accepted_views=tuple(dict.fromkeys((*accepted_views,self.config['hole_camera']['camera'])))
                    return partial
            raise
        finally:
            # A failed primary view cannot leave a previous frame computing
            # beside the next one or mutating its per-view mask tracker.
            if self.secondary_future is not None:wait((self.secondary_future,))
            self.secondary_future=None
            if token is not None:
                self.segment.finish_tracking_frame(token,accepted_views=accepted_views)
                secondary_camera=(self.config.get('hole_camera') or {}).get('camera')
                if accepted_views is not None and secondary_camera not in accepted_views:
                    # Cached masks were computed with provisional prompts. A
                    # later cache hit must not skip the discarded tracking step.
                    self.secondary_cache=None
                self.last_timing['mask_tracking_commit']=dict(
                    accepted_views='all' if accepted_views is None else list(accepted_views))
            self.last_timing['total_ms']=(time.perf_counter()-start)*1000
            diagnostics=getattr(self.segment,'diagnostic_snapshot',None)
            if diagnostics is not None:self.last_timing['segmentation']=diagnostics()
            self.config=original_config

    def _contact_fault_since(self, since_ns, context):
        ctx = context or {}
        if ctx.get('authorized') is not True:
            return 'not_authorized'
        if ctx.get('hold_requested') is True:
            return 'hold_requested'
        fault = ctx.get('contact_fault')
        if isinstance(fault, dict):
            at_ns = fault.get('at_ns')
            reason = fault.get('reason') or 'contact_fault'
            if isinstance(at_ns, int) and at_ns >= since_ns:
                return str(reason)
        return None

    def _check_occluded_fixture_retention(self, err_code, *, held, prior, observation, context):
        grasp_contact=(context or {}).get('grasp_contact') or {}
        grasp_pub_ns=grasp_contact.get('published_ns')
        base_eligible=(
            err_code in OCCLUDED_FIXTURE_RETAIN_REASONS
            and held is True
            and prior is not None
            and prior.source=='rgbd_anchor_registration'
            and isinstance(grasp_pub_ns,int)
            and prior.measured_at_ns>grasp_pub_ns
        )
        if not base_eligible:
            return 'none'
        contact_fault = self._contact_fault_since(prior.measured_at_ns, context)
        if contact_fault is not None:
            self.last_timing['occluded_fixture_retention_invalidated'] = 'fixture_retention_invalidated:' + contact_fault
            return 'none'
        if 0<=observation.received_ns-prior.measured_at_ns<=OCCLUDED_FIXTURE_RETAIN_NS:
            return 'retained'
        if observation.received_ns-prior.measured_at_ns>OCCLUDED_FIXTURE_RETAIN_NS:
            return 'expired'
        return 'none'

    def _update_cached_aperture(self, observation, *, base_hole, hole_evidence, held, fixture, context):
        cfg = self.config
        ctx = context or {}
        if (not self.coarse or self.development or held is not True
                or ctx.get('authorized') is not True or ctx.get('placement_verified') is True
                or len(hole_evidence) not in (2, 3)):
            return
        grasp_contact = ctx.get('grasp_contact') or {}
        close_id = grasp_contact.get('request_id')
        pub_ns = grasp_contact.get('published_ns')
        if (not isinstance(close_id, str) or not close_id
                or not isinstance(pub_ns, int) or observation.received_ns <= pub_ns):
            return
        if (fixture is None or fixture.source != 'rgbd_anchor_registration'
                or fixture.measured_at_ns is None):
            return
        if self._contact_fault_since(observation.received_ns, ctx) is not None:
            return
        self.cached_aperture = dict(
            pose=tuple(transform(base_hole).ravel()),
            evidence_ids=tuple(hole_evidence),
            measured_at_ns=int(observation.received_ns),
            fixture_pose=tuple(fixture.pose),
            fixture_measured_at_ns=int(fixture.measured_at_ns),
            close_request_id=close_id,
            scope_identity=self.context_identity,
            calibration_id=cfg['calibration_id'],
            acquisition_identity=self.acquisition_identity,
            frame_id=cfg['native_frame'],
        )
        self.last_timing['cached_aperture'] = dict(
            status='updated',
            measured_at_ns=int(observation.received_ns),
            close_request_id=close_id,
            fixture_measured_at_ns=int(fixture.measured_at_ns),
        )

    def _resolve_cached_aperture(self, observation, *, held, current_fixture, context):
        cfg = self.config
        cache = getattr(self, 'cached_aperture', None)
        if cache is None or not self.coarse or self.development:
            return None
        ctx = context or {}
        age_ns = observation.received_ns - cache['measured_at_ns']
        age_s = round(age_ns / 1e9, 4)

        def invalidate(reason):
            self.cached_aperture = None
            self.last_timing['cached_aperture'] = dict(
                status='invalidated',
                reason='cache_invalidated:' + reason,
                age_s=age_s,
                measured_at_ns=cache['measured_at_ns'],
            )
            return None

        if ctx.get('placement_verified') is True:
            return invalidate('placement_verified')
        if ctx.get('authorized') is not True:
            return invalidate('not_authorized')
        grasp_contact = ctx.get('grasp_contact') or {}
        if grasp_contact.get('request_id') != cache['close_request_id']:
            return invalidate('close_request_changed')
        if held is not True:
            if held is False:
                return invalidate('not_held')
            self.last_timing['cached_aperture'] = dict(
                status='skipped', reason='held_unknown', age_s=age_s,
                measured_at_ns=cache['measured_at_ns'])
            return None
        if self.context_identity != cache['scope_identity']:
            return invalidate('scope_changed')
        if (cfg['calibration_id'] != cache['calibration_id']
                or observation.calibration_id not in (None, cache['calibration_id'])):
            return invalidate('calibration_changed')
        if self.acquisition_identity != cache['acquisition_identity']:
            return invalidate('acquisition_changed')
        contact_fault = self._contact_fault_since(cache['measured_at_ns'], ctx)
        if contact_fault is not None:
            return invalidate(contact_fault)
        if age_ns < 0 or age_ns > CACHED_APERTURE_MAX_AGE_NS:
            return invalidate('age_exceeded')
        if current_fixture is None:
            self.last_timing['cached_aperture'] = dict(
                status='unavailable', reason='no_current_or_retained_fixture', age_s=age_s,
                measured_at_ns=cache['measured_at_ns'])
            return None
        fixture_dev_m = None
        if current_fixture.source == 'rgbd_anchor_registration':
            fixture_dev_m = float(np.linalg.norm(
                transform(current_fixture.pose)[:3, 3] - transform(cache['fixture_pose'])[:3, 3]))
            if fixture_dev_m > cfg['planning_margin_m']:
                return invalidate('fixture_moved')
        elif current_fixture.source == 'retained_static_fixture':
            if (current_fixture.measured_at_ns is None
                    or current_fixture.measured_at_ns < cache['fixture_measured_at_ns']):
                return invalidate('retained_fixture_older_than_cache')
        else:
            return invalidate('invalid_fixture_source')
        self.last_timing['cached_aperture'] = dict(
            status='used',
            age_s=age_s,
            measured_at_ns=cache['measured_at_ns'],
            close_request_id=cache['close_request_id'],
            fixture_source=current_fixture.source,
            fixture_measured_at_ns=current_fixture.measured_at_ns,
            fixture_deviation_m=round(fixture_dev_m, 6) if fixture_dev_m is not None else 0.0,
            conditions_verified=True,
        )
        return SceneObject(
            object_id=cfg['hole_id'],
            shape='square',
            pose=cache['pose'],
            frame_id=cache['frame_id'],
            uncertainty_m=None,
            planning_margin_m=cfg['planning_margin_m'],
            evidence_ids=cache['evidence_ids'],
            visible=False,
            stationary=True,
            source='cached_pre_transport_aperture',
            measured_at_ns=cache['measured_at_ns'],
        )

    def _partial_observation(self,observation,reason,*,measured_object=None,geometry_camera=None,
                             stationary=False,quality_warnings=None,fixture=None,prior_fixture=None):
        """Preserve current identity/depth without inventing a manipulation pose."""
        if (not self.coarse or self.development or not self._partial_targets
                or observation.state_received_ns is None or measured_object is not None and measured_object.held is True):return None
        from .partial_scene import observed_free_space_view,local_collision_evidence
        cfg=self.config
        head_sample=None
        # An unavailable target in the head image does not erase independently
        # registered depth there. Failed/unsynchronised views are never added.
        if self.secondary_future is not None:
            try:
                depth,head_evidence,head_masks,_=self.secondary_future.result();view_cfg=cfg['hole_camera']
                rgb=next(f for f in observation.frames if f.camera==view_cfg['camera'] and f.stream=='rgb')
                full=np.zeros((rgb.height,rgb.width),float);x0,y0,x1,y1=view_cfg['roi']
                full[y0:y1,x0:x1]=depth
                self._partial_views[view_cfg['camera']]=(full,view_cfg,transform(view_cfg['base_camera']))
                head_sample=(view_cfg,depth,head_masks,head_evidence)
            except ValueError as exc:
                if (str(exc)!='secondary_camera_pairing_unavailable'
                        and not object_measurement_recoverable(str(exc),camera=cfg['hole_camera']['camera'],
                            object_id=cfg['object_id'])):raise
        views=[]
        for depth,view_cfg,pose in self._partial_views.values():
            try:views.append(observed_free_space_view(depth,view_cfg,pose,observation.frames))
            except ValueError as exc:
                if str(exc)!='partial_free_space_depth_unavailable':raise
                continue
        if not views:return None
        target_camera=geometry_camera or cfg['camera']
        target=next((row for row in self._partial_targets if row['camera']==target_camera),
                    None if measured_object is not None else self._partial_targets[0])
        if target is None:return None
        if target['camera'] not in {view.camera for view in views}:return None
        evidence=local_collision_evidence(observation,cfg,target['bounds'],views)
        source=tuple(f.blob_sha256 for f in observation.frames if f.camera==target['camera'])
        partial_hold = None
        if measured_object is None and cfg.get('measured_gripper_contact'):
            stamp = min(f.received_ns for f in observation.frames if f.camera==target['camera'])
            partial_hold = self.grasp_closure.measure(observation, self.previous_context or {}, cfg, at_ns=stamp)['held']
        occluded_retained=False
        occluded_expired=False
        if measured_object is None and head_sample is not None and self.fixture_reference_camera is not None:
            _,head_depth,head_masks,head_evidence=head_sample
            try:
                fixture=self._locate_bucket_for_object(observation,target['camera'],(head_depth,head_evidence,head_masks))
                self.measured_fixture=fixture
            except ValueError as exc:
                err_code=str(exc)
                retention=self._check_occluded_fixture_retention(
                    err_code,held=partial_hold,prior=prior_fixture,
                    observation=observation,context=self.previous_context)
                if retention=='retained':
                    fixture=prior_fixture.model_copy(update={'source':'retained_static_fixture'})
                    self.measured_fixture=prior_fixture
                    occluded_retained=True
                elif retention=='expired':
                    occluded_expired=True
                    self.measured_fixture=None
                else:
                    self.measured_fixture=None
                self.last_timing['independent_fixture']=dict(available=False,reason=err_code)
        obj=measured_object or SceneObject(object_id=cfg['object_id'],shape='square',pose=None,held=partial_hold,
            visible=True,stationary=None,frame_id=cfg['native_frame'],evidence_ids=source,
            planning_margin_m=cfg['planning_margin_m'])
        objects=(obj,)
        aperture_sources=()
        aperture_error=None
        if obj.held is True and head_sample is not None and cfg.get('relocalize_visible_fixture'):
            try:pose,ids,visible,error=self._direct_aperture(observation,*head_sample,placing=True)
            except ValueError as exc:
                if str(exc)!='hole_behind_camera':raise
                aperture_error=str(exc)
            else:
                if visible and error is None:
                    objects+=(SceneObject(object_id=cfg['hole_id'],shape='square',pose=tuple(pose.ravel()),
                        frame_id=cfg['native_frame'],planning_margin_m=cfg['planning_margin_m'],
                        evidence_ids=ids,visible=True,stationary=True),)
                    aperture_sources=ids
                else:
                    aperture_error=error or 'aperture_not_directly_visible'
        self.revision+=1
        self.last_stamps=tuple(next(f.source_ns for f in observation.frames
            if f.camera==cfg['camera'] and f.stream==stream) for stream in ('rgb','depth'))
        self.last_timing['partial_object_geometry']=dict(camera=target['camera'],reason=target['reason'],
            object_pose_available=obj.pose is not None,identity_provenance=target['identity_provenance'],
            observed_surface_min=evidence.observed_surface_min,observed_surface_max=evidence.observed_surface_max,
            possible_occupancy_min=evidence.possible_occupancy_min,possible_occupancy_max=evidence.possible_occupancy_max,
            depth_points=evidence.depth_points,mask_pixels=evidence.mask_pixels,
            depth_hole_pixels=evidence.mask_pixels-evidence.depth_points,
            surface_anchor=None if evidence.surface_anchor is None else evidence.surface_anchor.model_dump(),
            complete_object_extent_basis=('intersection_of_registered_diameter_bounds_about_planar_inliers'
                if evidence.surface_anchor is not None else 'registered_maximum_diagonal_plus_existing_margin'),
            free_space_views=[dict(camera=view.camera,grid_width=view.grid_width,grid_height=view.grid_height,
                valid_tiles=sum(value>0 for value in view.min_depth_m),total_tiles=len(view.min_depth_m),
                near_depth_m=view.near_depth_m,near_depth_source='positive_optical_ray_before_valid_first_return') for view in views],
            unobserved_space='depth_holes_outside_view_and_beyond_first_surface_remain_unknown',motion_evidence=False)
        warnings=tuple(dict.fromkeys((*(observation.quality_warnings if quality_warnings is None else quality_warnings),
            *(('object_pose_unavailable:'+target['reason'],) if measured_object is None else ()),
            *(('aperture_unavailable:'+aperture_error,) if aperture_error else ()),
            *(('static_fixture_prediction_from_prior_measurement',
               'static_fixture_retained_while_held_arm_occludes_head') if occluded_retained else ()),
            *(('static_fixture_retention_expired',) if occluded_expired else ()))))
        valid_cameras={view.camera for view in views}
        secondary=cfg.get('hole_camera',{}).get('camera')
        faults=observation.faults
        if secondary and secondary not in valid_cameras and secondary_sampling_faults(faults,secondary):
            self.last_timing['partial_secondary_sampling_faults']=list(faults)
            warnings=(*warnings,*('secondary_view_unavailable:'+fault for fault in faults))
            faults=()
        frames=tuple(f for f in observation.frames if f in evidence.source_frames or f.blob_sha256 in aperture_sources)
        return observation.model_copy(update=dict(objects=objects,fixture=fixture,stationary=stationary,
            local_collision_evidence=evidence,
            geometry_scope='coarse_local_environment' if measured_object is None else 'coarse_object_verification',
            geometry_camera=target['camera'],
            frames=frames,pairings=tuple(p for p in observation.pairings if p.camera in valid_cameras),
            faults=faults,
            quality_warnings=warnings,calibration_id=cfg['calibration_id'],
            scene_version=digest(dict(revision=self.revision,object=obj.model_dump(),
                local_collision_evidence=evidence.model_dump(),
                **({'aperture':objects[1].model_dump()} if aperture_sources else {})))))

    def _mark(self,name):
        now=time.perf_counter();self.last_timing[name]=(now-self._timing_at)*1000;self._timing_at=now

    def _process(self,observation,*,base_eef,context=None,secondary_error=None):
        cfg=self.config
        acquisition=(observation.acquisition_instance,observation.clock_domain,observation.host_boot_id,
                     observation.calibration_id,observation.synthetic)
        streams={(f.camera,f.stream):(f.source_epoch,f.publisher_id,f.camera_info_sha256,f.depth_scale_m)
                 for f in observation.frames if f.camera in (cfg['camera'], (cfg.get('hole_camera') or {}).get('camera'))}
        changed_stream=any(key in self.stream_identities and self.stream_identities[key]!=value
                           for key,value in streams.items())
        if acquisition!=self.acquisition_identity or changed_stream:
            if self.acquisition_identity is not None:
                reset=getattr(self.segment,'reset_visual_tracking',None)
                if reset is not None:reset()
            self.invalidate();self.invalidate_secondary()
            self.lid_registration=None
            self.return_stamps=None
            self.search_hint=None
            self.last_stamps=None;self.last_received_ns=None
            self.object_view_stamps={};self.object_camera=None
            self.acquisition_identity=acquisition
            self.stream_identities={}
        # Missing frames are unavailable measurements, not a new camera identity.
        self.stream_identities.update(streams)
        camera=(cfg['hole_camera']['camera'] if self.coarse and (context or {}).get('placement_verified') is True
                else cfg['camera'])
        primary_times=[f.received_ns for f in observation.frames if f.camera==camera]
        context=self._context(context,min(primary_times) if primary_times and self.coarse else observation.received_ns)
        # Consume authoritative gripper changes even if RGB-D pairing fails.
        self._consume_independent_opening(observation,
            min(primary_times) if primary_times else observation.received_ns)
        if (cfg.get('measured_gripper_contact')
                and (self.in_hand_reference is not None or self.in_hand_quarantine is not None)):
            stamp=min(primary_times) if primary_times else observation.received_ns
            if self.grasp_closure.measure(observation,context,cfg,at_ns=stamp)['held'] is not True:
                self._clear_following()
        if observation.state16 is not None and observation.state_valid_mask is not None:
            opening=cfg.get('gripper_open_range')
            reference_grip=(self.in_hand_gripper if self.in_hand_reference is not None else
                            self.in_hand_quarantine['gripper'] if self.in_hand_quarantine else None)
            if observation.state_valid_mask[15] and (self._gripper_is_open(observation)
                    or reference_grip is not None and cfg.get('gripper_stationary_delta') is not None
                    and abs(self._gripper_value(observation)-reference_grip)>=cfg['gripper_stationary_delta']):
                self._clear_following()
        if observation.synthetic:raise ValueError('synthetic_capture_in_real_scene_pipeline')
        if self.coarse and context.get('placement_verified') is True:
            from .return_observation import observe_return
            return observe_return(self,observation,context)
        if self.coarse and not self.development:
            from .scene_views import validate_secondary_identity
            validate_secondary_identity(observation,cfg['hole_camera'])
        if secondary_error is not None and not isinstance(secondary_error,SecondaryCameraUnavailable):
            raise TypeError('typed_secondary_camera_error_required')
        if observation.faults:
            reason='capture_faults:'+','.join(observation.faults)[:200]
            head=cfg.get('hole_camera',{}).get('camera')
            if (self.coarse and not self.development and head!=camera
                    and secondary_sampling_faults(observation.faults,head)):
                secondary_error=SecondaryCameraUnavailable(reason)
            else:raise ValueError(reason)
        pairing=next((p for p in observation.pairings if p.camera==camera),None)
        qualities=('source_time_bounded','source_metadata_matched','receipt_only') if self.development or self.coarse else (
            'source_time_bounded','source_metadata_matched')
        if pairing is None or pairing.quality not in qualities:
            raise ValueError('unverified_pairing')
        frames={f.stream:f for f in observation.frames if f.camera==camera}
        rgb_frame,depth_frame=frames['rgb'],frames['depth']
        stamps=(rgb_frame.source_ns,depth_frame.source_ns)
        if (None in stamps or (self.last_stamps is not None and
                              any(a<=b for a,b in zip(stamps,self.last_stamps)))
                or pairing.rgb_sequence!=rgb_frame.sequence or pairing.depth_sequence!=depth_frame.sequence):
            raise ValueError('new_consistent_source_frames_required')
        if (rgb_frame.camera_info_sha256!=cfg['rgb_info_sha256']
                or depth_frame.camera_info_sha256!=cfg['depth_info_sha256']
                or depth_frame.depth_scale_m!=cfg['depth_scale_m']):
            raise ValueError('camera_calibration_identity_changed')
        if observation.state16 is None or observation.state_valid_mask is None or not all(observation.state_valid_mask[7:14]):
            raise ValueError('measured_right_arm_missing')
        # Camera and surface fitting follow measured manipulation state. An
        # observation/recovery request cannot change the unchanged scene's
        # estimator. Closed or unknown fingers never borrow a supported-object
        # prior as evidence of an in-hand orientation.
        opening=cfg.get('gripper_open_range')
        pregrasp=bool(observation.state_valid_mask[15] and self._gripper_is_open(observation))
        self.last_timing['object_estimator_state']=dict(
            source='measured_gripper',supported_object_prior=pregrasp,
            gripper_feedback=self._gripper_value(observation),gripper_valid=observation.state_valid_mask[15])
        if self.coarse and not self.development and secondary_error is None:
            if self.secondary_executor is None:
                self.secondary_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='harness-head-view')
            head_cfg=cfg['hole_camera']
            head_hints=self._object_search_hints(observation,head_cfg,head_cfg['base_camera'],
                enabled=cfg.get('object_reacquisition_camera')==head_cfg['camera'],base_eef=base_eef)
            if head_hints:
                self.last_timing['head_object_search_hint']=dict(boxes=head_hints,
                    measured_at_ns=self.search_hint['received_ns'],pose_used_as_evidence=False)
            self.secondary_future=self.secondary_executor.submit(self._secondary_view,observation,head_cfg,
                search_boxes=head_hints)
        import cv2
        preferred_head=(pregrasp and cfg.get('pregrasp_object_camera')
                        and cfg.get('pregrasp_object_camera')==cfg.get('object_reacquisition_camera'))
        rgb=aligned=masks=None
        def primary_pixels():
            nonlocal rgb,aligned
            if rgb is not None:return
            self.last_timing['right_pixel_work_skipped']=False
            rgb=decode(rgb_frame,self.blob(rgb_frame.blob_sha256))
            depth=decode(depth_frame,self.blob(depth_frame.blob_sha256))
            self._mark('right_decode_ms')
            aligned=register_depth(depth,cfg['k_depth'],cfg['d_depth'],cfg['rgb_depth'],
                                   cfg['k_rgb'],cfg['d_rgb'],(rgb_frame.width,rgb_frame.height),cfg['depth_scale_m'])
            self._mark('right_registration_ms')
            # Both masks and back-projected points use rectified RGB pixels.
            k=np.asarray(cfg['k_rgb'],float)
            image_size=(rgb_frame.width,rgb_frame.height)
            if self.rectification is None or self.rectification[0]!=image_size:
                self.rectification=(image_size,cv2.initUndistortRectifyMap(k,np.asarray(cfg['d_rgb'],float),None,k,
                                                                         image_size,cv2.CV_32FC1))
            maps=self.rectification[1]
            rgb=cv2.remap(rgb,*maps,interpolation=cv2.INTER_LINEAR)
            aligned=cv2.remap(aligned,*maps,interpolation=cv2.INTER_NEAREST)
            self._mark('right_rectification_ms')
        if not preferred_head:
            primary_pixels()
        else:
            # RGB-D identity/feedback checks above remain. Only pixel work for
            # the unused wrist view is skipped; head geometry uses actual data.
            self.last_timing['right_pixel_work_skipped']=True
        x0,y0,x1,y1=cfg['roi']
        evidence=(rgb_frame.blob_sha256,depth_frame.blob_sha256)
        base_camera=(transform(base_eef)@transform(cfg['eef_camera'])
                     if (self.development or self.coarse) and camera=='right' else transform(cfg['base_camera']))
        object_camera=camera
        object_stamps=stamps
        mask_available=None
        def clear_measurement_diagnostics():
            nonlocal mask_available
            mask_available=None
            for name in ('surface_selection','observed_face_fit','surface_association','pose_normal_prior'):
                self.last_timing.pop(name,None)
        def record_measurement_failure(view,exc):
            # Facts from this attempted view, never a pose or permission to move.
            # Copy before another camera overwrites its surface fit diagnostics.
            frames=[dict(stream=f.stream,source_ns=f.source_ns,received_ns=f.received_ns,
                         blob_sha256=f.blob_sha256) for f in observation.frames if f.camera==view]
            metrics={name:deepcopy(self.last_timing[name]) for name in ('surface_selection','fixture_exclusion_region')
                     if name in self.last_timing}
            seg_diag=(getattr(self.segment,'diagnostic_snapshot',lambda:{})().get(view,{})
                      .get(cfg['object_id']) or {})
            if seg_diag.get('fixture_exclusion') is not None:
                metrics['fixture_exclusion']=deepcopy(seg_diag['fixture_exclusion'])
            seg_tracking=seg_diag.get('tracking') or {}
            if 'semantic_candidate_summary' in seg_tracking:
                metrics['semantic_candidate_summary']=deepcopy(seg_tracking['semantic_candidate_summary'])
                metrics['semantic_candidates']=seg_tracking.get('semantic_candidates')
                metrics['semantic_candidate_summary_truncated']=seg_tracking.get('semantic_candidate_summary_truncated')
            self.last_timing.setdefault('object_geometry_attempts',[]).append(dict(
                camera=view,object_id=cfg['object_id'],observation_id=observation.observation_id,
                reason=str(exc)[:500],frames=frames,metrics=metrics,mask_available=mask_available,
                stage='geometry' if mask_available is not None else 'segmentation_or_view',diagnostic_only=True))
        measured_local_input=None
        def record_local_target(mask,depth,view_cfg,pose,reason,*,pose_available=False):
            snapshot=getattr(self.segment,'diagnostic_snapshot',lambda:{})()
            tracking=snapshot.get(view_cfg['camera'],{}).get(cfg['object_id'],{}).get('tracking') or {}
            frame=next(f for f in observation.frames if f.camera==view_cfg['camera'] and f.stream=='rgb')
            if (self.coarse and not self.development and cfg.get('search_object_box')
                    and tracking.get('identity_status')=='confirmed'
                    and tracking.get('source_ns')==frame.source_ns):
                from .partial_scene import observed_target_bounds,registered_box_surface_anchor
                try:bounds=observed_target_bounds(mask,depth,view_cfg,pose,cfg['search_object_box'])
                except ValueError:pass
                else:
                    anchor=registered_box_surface_anchor(self.last_timing.get('surface_selection') or {},
                        cfg['planning_margin_m'])
                    if anchor is not None:bounds['surface_anchor']=anchor
                    association=('current_registered_pose_association_validated' if pose_available
                                 else 'no_prior_cross_camera_identity')
                    if (self.object_camera is not None and self.object_camera!=view_cfg['camera']
                            and self.search_hint is not None):
                        from itertools import product
                        corners=np.asarray(list(product(*zip(cfg['search_object_box']['min'],cfg['search_object_box']['max']))))
                        tolerance=cfg.get('tracking_tolerance_m',cfg['planning_margin_m'])
                        matches=[]
                        low=bounds['observed_surface_min'] if anchor is None else anchor['bounds_min']
                        high=bounds['observed_surface_max'] if anchor is None else anchor['bounds_max']
                        for name,expected in self._object_identity_hypotheses(observation,base_eef):
                            cloud=corners@expected[:3,:3].T+expected[:3,3]
                            if (np.all(np.asarray(high)>=cloud.min(0)-tolerance)
                                    and np.all(np.asarray(low)<=cloud.max(0)+tolerance)):
                                matches.append(name)
                        self.last_timing['partial_object_identity']=dict(camera=view_cfg['camera'],
                            previous_camera=self.object_camera,status='associated' if matches else 'unresolved',
                            matching_hypotheses=matches,method='observed_surface_intersects_registered_volume',
                            motion_evidence=False)
                        if not matches:
                            raise ValueError('object_identity_unresolved:cross_camera_partial_association:camera='+view_cfg['camera'])
                        association='observed_surface_intersects_prior_registered_volume'
                    provenance=dict(sam_identity_status=tracking['identity_status'],
                        identity_source=tracking.get('identity_source'),source_ns=tracking['source_ns'],
                        seed_source_ns=(tracking.get('seed_provenance') or {}).get('source_ns'),
                        seed_reason=tracking.get('seed_reason') or (tracking.get('seed_provenance') or {}).get('seed_reason'),
                        cross_camera_association=association,physical_object_pose_available=pose_available)
                    self._partial_targets.append(dict(camera=view_cfg['camera'],bounds=bounds,reason=reason,
                        identity_provenance=provenance))
                    self._partial_views[view_cfg['camera']]=(depth,view_cfg,pose)
        def measure(mask, depth, view_cfg, pose, evidence):
            nonlocal mask_available,measured_local_input
            mask_available=bool(np.any(mask))
            surface=dict(cfg.get('surface_selection') or {})
            if (view_cfg.get('camera')==cfg.get('pregrasp_object_camera')
                    and pregrasp):
                surface=dict(cfg.get('pregrasp_surface_selection') or surface)
            # The supported-face normal prior concerns the visible plane,
            # not the object's long axis; face dimensions determine that below.
            # The table-supported prior is never used as in-hand orientation
            # evidence. Once measured fingers close, all axes are measured again.
            if not pregrasp:
                surface.pop('pose_normal_prior',None)
                # A measured attachment rotates with the hand. Use its face
                # direction only to associate NEW depth points, never as the
                # fitted pose or as fresh proof of attachment.
                reference=self.in_hand_reference
                grip=self.in_hand_gripper
                if reference is None and self.in_hand_quarantine is not None:
                    reference=self.in_hand_quarantine['reference']
                    grip=self.in_hand_quarantine['gripper']
                if (surface and reference is not None and grip is not None
                        and observation.state_valid_mask[15]
                        and abs(self._gripper_value(observation)-grip)<cfg['gripper_stationary_delta']
                        and not (opening and opening[0]<=self._gripper_value(observation)<=opening[1])):
                    face_object=(self.observed_face_object if self.observed_face_object is not None else
                                 transform(cfg['face_object']))
                    predicted_face=transform(base_eef)@transform(reference)@np.linalg.inv(face_object)
                    surface['normal_world']=predicted_face[:3,2].tolist()
                    self.last_timing['surface_association']=dict(
                        source='measured_attachment_propagated_by_current_eef',
                        normal_world=surface['normal_world'],prediction_only=True,
                        pose_fitted_from_current_depth=True)
            try:
                result=object_from_mask(object_id=cfg['object_id'],shape='square',aligned_depth_m=depth,
                    mask=mask,k_rgb=view_cfg['k_rgb'],base_camera=pose,
                    calibration_error_m=0. if self.development or self.coarse else cfg['uncertainty_m'],
                    evidence_ids=evidence,frame_id=cfg['native_frame'],roi=view_cfg['roi'],
                    diagnostics=self.last_timing,
                    **({'surface_selection':surface} if surface else {}))
                measured_local_input=(mask,depth,view_cfg,pose)
                return result
            except ValueError as exc:
                if object_measurement_recoverable(str(exc),object_id=cfg['object_id']):
                    record_local_target(mask,depth,view_cfg,pose,str(exc))
                raise
            finally:
                warnings=(self.last_timing.get('surface_selection') or {}).get('quality_warnings',[])
                if warnings:
                    self.last_timing['quality_warnings']=list(dict.fromkeys(
                        [*self.last_timing.get('quality_warnings',[]),*warnings]))
        def measure_primary():
            nonlocal masks
            clear_measurement_diagnostics()
            primary_pixels()
            self._partial_views[camera]=(aligned,cfg,base_camera)
            hints=self._object_search_hints(observation,cfg,base_camera,enabled=True,base_eef=base_eef)
            if hints:
                masks=self._segment_view(rgb[y0:y1,x0:x1],observation,camera,hints)
                self.last_timing['object_search_hint']=dict(boxes=hints,
                    measured_at_ns=self.search_hint['received_ns'],pose_used_as_evidence=False)
            else:
                exclusion_boxes=None
                candidate_depth_filters=None
                if (self.coarse and not self.development and camera=='right'
                        and pregrasp and self.contact_attachment is None and self.in_hand_reference is None):
                    is_seeded=getattr(self.segment,'is_sam_seeded',None)
                    if is_seeded is None or not is_seeded(camera,cfg['object_id']):
                        if cfg.get('fixture_exclusion_box'):
                            fixture_pose=(self.measured_fixture if self.measured_fixture is not None
                                          and self.measured_fixture.source=='rgbd_anchor_registration' else None)
                            if (fixture_pose is None and self.secondary_future is not None
                                    and secondary_error is None and self.fixture_reference_camera is not None):
                                try:
                                    head_depth,head_evidence,head_masks,_=self.secondary_future.result()
                                    head_fixture=self._locate_bucket_for_object(
                                        observation,camera,(head_depth,head_evidence,head_masks))
                                    if head_fixture is not None and head_fixture.source=='rgbd_anchor_registration':
                                        self.measured_fixture=head_fixture
                                        fixture_pose=head_fixture
                                except ValueError:
                                    pass
                            if fixture_pose is not None and fixture_pose.source=='rgbd_anchor_registration':
                                pad_fn=getattr(self.segment,'prompt_padding_px',None)
                                pad_px=pad_fn(camera) if pad_fn is not None else int(cfg.get('prompt_padding_px',0))
                                rect=self._project_fixture_exclusion_box(fixture_pose,base_camera,cfg,pad_px)
                                if rect is not None:
                                    exclusion_boxes={cfg['object_id']:rect}
                                    self.last_timing['fixture_exclusion_region']=dict(
                                        camera=camera,box=rect,fixture_source=fixture_pose.source,
                                        fixture_measured_at_ns=fixture_pose.measured_at_ns,motion_evidence=False)
                        table_plane=cfg.get('table_support_plane')
                        if isinstance(table_plane,dict) and 'point' in table_plane and 'normal' in table_plane:
                            pt=np.asarray(table_plane['point'],float)
                            nrm=np.asarray(table_plane['normal'],float)
                            nrm_len=float(np.linalg.norm(nrm)) if nrm.shape==(3,) and np.isfinite(nrm).all() else 0.0
                            if pt.shape==(3,) and np.isfinite(pt).all() and nrm_len>0.0:
                                from .geometry import crop_intrinsics
                                candidate_depth_filters={cfg['object_id']:dict(
                                    aligned_depth_m=aligned[y0:y1,x0:x1],
                                    k_rgb=crop_intrinsics(cfg['k_rgb'],cfg['roi']),
                                    base_camera=base_camera,
                                    table_point=pt,
                                    table_normal=nrm/nrm_len,
                                    below_table_margin_m=0.05,
                                )}
                masks=(self._segment_view(rgb[y0:y1,x0:x1],observation,camera,
                                          exclusion_boxes=exclusion_boxes,
                                          candidate_depth_filters=candidate_depth_filters)
                       if self.coarse and not self.development
                       else self.segment(rgb[y0:y1,x0:x1]))
            self._mark('right_mask_ms')
            if cfg['object_id'] not in masks:
                raise ValueError('object_mask_missing:camera='+camera+':object='+cfg['object_id'])
            return measure(masks[cfg['object_id']],aligned,cfg,base_camera,evidence)
        try:
            if preferred_head:
                if secondary_error is not None:
                    raise ValueError('secondary_camera_pairing_unavailable')
                raise ValueError('registered_pregrasp_view')
            obj=measure_primary()
        except ValueError as exc:
            # A lost prompt can switch to an explicitly configured, independently
            # calibrated camera. Identity/depth/fit faults are not downgraded.
            reason=str(exc)
            if not preferred_head:record_measurement_failure(camera,exc)
            discard=getattr(self.segment,'discard_tracking_view',None)
            if discard is not None:discard(camera)
            if (not cfg.get('object_reacquisition_camera') or secondary_error is not None
                    or not self.coarse or self.development or self.secondary_future is None
                    or not (reason=='registered_pregrasp_view' or object_measurement_recoverable(
                        reason,camera=camera,object_id=cfg['object_id']))):
                raise
            view_cfg=cfg['hole_camera']
            if cfg['object_reacquisition_camera']!=view_cfg['camera']:
                raise ValueError('object_reacquisition_camera_identity')
            try:
                clear_measurement_diagnostics()
                secondary_depth,secondary_evidence,secondary_masks,timing=self.secondary_future.result()
                view_frames={f.stream:f for f in observation.frames if f.camera==view_cfg['camera']}
                secondary_stamps=tuple(view_frames[k].source_ns for k in ('rgb','depth'))
                previous=self.object_view_stamps.get(view_cfg['camera'])
                if previous and any(a<=b for a,b in zip(secondary_stamps,previous)):
                    raise ValueError('new_consistent_source_frames_required:camera='+view_cfg['camera'])
                if cfg['object_id'] not in secondary_masks:
                    raise ValueError('object_mask_missing:camera='+view_cfg['camera']+':object='+cfg['object_id']) from exc
                hx0,hy0,hx1,hy1=view_cfg['roi']
                full_depth=np.zeros((view_frames['rgb'].height,view_frames['rgb'].width),float)
                full_depth[hy0:hy1,hx0:hx1]=secondary_depth
                obj=measure(secondary_masks[cfg['object_id']],full_depth,view_cfg,
                            transform(view_cfg['base_camera']),secondary_evidence)
                object_camera=view_cfg['camera'];object_stamps=secondary_stamps
            except ValueError as head_exc:
                # Head preference saves wrist pixel work when successful. It is
                # not a ban on independently calibrated current wrist evidence.
                # Identity, time pairing and unexpected errors are never masked.
                head_reason=str(head_exc)
                record_measurement_failure(view_cfg['camera'],head_exc)
                if not object_measurement_recoverable(head_reason,camera=view_cfg['camera'],object_id=cfg['object_id']):raise
                if not preferred_head:
                    # Both views lack a usable measurement. Preserve the real
                    # primary fit failure; it is not evidence of invisibility.
                    raise exc from head_exc
                if discard is not None:discard(view_cfg['camera'])
                self.last_timing['preferred_head_rejected']=head_reason
                try:obj=measure_primary()
                except ValueError as wrist_exc:
                    record_measurement_failure(camera,wrist_exc)
                    raise
                object_camera=camera;object_stamps=stamps
                reason='preferred_head_rejected:'+head_reason
            self.last_timing['object_reacquisition']=dict(camera=object_camera,reason=reason)
        changed_view=self.object_camera is not None and self.object_camera!=object_camera
        face=transform(obj.pose)
        fitted=self.last_timing.get('observed_face_fit')
        observed_face_object=None if changed_view else self.observed_face_object
        if self.coarse and cfg.get('search_object_box') and fitted and (pregrasp or observed_face_object is None):
            from .scene import box_frame_from_face
            base_object,registration=box_frame_from_face(face,fitted,cfg['search_object_box'])
            observed_face_object=np.linalg.inv(face)@base_object
            self.last_timing['object_face_registration']=registration
        else:
            base_object=face@(observed_face_object if observed_face_object is not None else transform(cfg['face_object']))
        self._associate_object_view(base_object,object_camera,observation,base_eef)
        if changed_view:
            # Independent views have different error distributions; no apparent
            # cross-camera displacement can create a grasp. Retain an existing
            # historical proof only in quarantine until three independent
            # measurements in the associated new view revalidate it.
            from .grasp_tracking import quarantine_following, handoff_reference
            proof=self.in_hand_quarantine
            if self.in_hand_reference is not None and self.tracking_history:
                last=self.tracking_history[-1]
                deadline=(None if self.coarse else self.in_hand_recovery_deadline_ns or
                          last['time']+cfg.get('tracking_window_ns',2_000_000_000))
                proof=quarantine_following(self.in_hand_reference,self.in_hand_gripper,last,expires_ns=deadline)
            empty=self.not_in_hand_reference
            frames=tuple(f for f in observation.frames if f.camera==object_camera)
            identity=(observation.acquisition_instance,object_camera,
                tuple((f.stream,f.source_epoch,f.publisher_id) for f in frames))
            self.stationary_history.clear();self._clear_following();self.square_pose_reference=None
            if proof is not None:
                self.in_hand_quarantine=handoff_reference(proof,identity)
                self.in_hand_recovery_deadline_ns=proof['expires_ns']
            if empty is not None:
                self.not_in_hand_reference=handoff_reference(empty,identity)
            self.observed_face_object=None
        self.object_camera=object_camera
        self.object_view_stamps[object_camera]=object_stamps
        self.observed_face_object=observed_face_object
        self.search_hint=dict(pose=base_object.copy(),received_ns=observation.received_ns,
                              eef_object=np.linalg.inv(transform(base_eef))@base_object)
        if self.square_pose_reference is not None:
            base_object,_=align_square_pose(base_object,self.square_pose_reference)
        self.square_pose_reference=base_object.copy()
        self._mark('right_surface_ms')
        if self.development:
            return self._static_approach(observation,obj,base_object,stamps)
        eef=transform(base_eef)
        relative,stationary,object_still,held=self._primary_evidence(observation,base_object,eef,object_stamps,camera=object_camera)
        measured_attachment = (tuple(relative.ravel()) if held and
            (not cfg.get('measured_gripper_contact') or self.contact_attachment is not None) else None)
        quality_warnings=tuple(dict.fromkeys((*observation.quality_warnings,
            *self.last_timing.get('quality_warnings',()))))
        self.last_stamps=stamps
        def object_only(reason):
            # Object verification can use independently measured wrist evidence.
            # Exact target pose and observed local space are separate capabilities;
            # a missing hole does not discard valid evidence of either.
            measured=obj.model_copy(update=dict(pose=tuple(base_object.ravel()),held=held,
                eef_object_pose=measured_attachment,stationary=object_still,
                uncertainty_m=None,planning_margin_m=cfg['planning_margin_m']))
            self.invalidate_secondary()
            self.last_timing['fixture_unavailable']=str(reason)
            # A rejected sample is not a measurement that the rigid bucket
            # moved. Keep its last independently measured world pose with the
            # ORIGINAL time and evidence; never synthesize an aperture.
            retained=None
            occluded_retained=False
            camera_name=cfg.get('hole_camera',{}).get('camera')
            if (self.measured_fixture is not None and camera_name and
                    str(reason) in ('capture_faults:duplicate_source_stamp:'+camera_name,
                                    'capture_faults:sequence_not_increasing:'+camera_name)):
                retained=self.measured_fixture.model_copy(update={'source':'retained_static_fixture'})
            elif getattr(self,'_occluded_fixture_retained',False) and self.measured_fixture is not None:
                retained=self.measured_fixture.model_copy(update={'source':'retained_static_fixture'})
                occluded_retained=True
            else:
                self.measured_fixture=None
            if retained is None and measured_local_input is not None and held is not True:
                record_local_target(*measured_local_input,str(reason),pose_available=True)
                local=self._partial_observation(observation,str(reason),measured_object=measured,
                    geometry_camera=object_camera,stationary=stationary,
                    quality_warnings=(*quality_warnings,'fixture_unavailable:'+str(reason)))
                if local is not None:return local
            self.revision+=1
            cached_hole = self._resolve_cached_aperture(observation, held=held, current_fixture=retained, context=context)
            if cached_hole is not None and retained is not None:
                c_base_hole = transform(cached_hole.pose)
                c_hole_obj = np.linalg.inv(c_base_hole) @ base_object
                c_yaw = math.atan2(c_hole_obj[1, 0], c_hole_obj[0, 0])
                c_yaw_err = abs((c_yaw + math.pi / 4) % (math.pi / 2) - math.pi / 4)
                c_tilt = math.acos(float(np.clip(c_hole_obj[2, 2], -1, 1)))
                c_inserted = bool(
                    np.linalg.norm(c_hole_obj[:2, 3]) <= cfg['xy_tolerance_m']
                    and insertion_depth_reached(float(c_hole_obj[2, 3]), cfg['insertion_depth_m'],
                        tolerance_m=cfg.get('insertion_depth_tolerance_m', 0.))
                    and c_yaw_err <= cfg['yaw_tolerance_rad'] and c_tilt <= cfg['tilt_tolerance_rad'])
                measured = measured.model_copy(update=dict(
                    inserted=c_inserted, inserted_hole_id=cfg['hole_id'] if c_inserted else None))
                return observation.model_copy(update=dict(
                    objects=(measured, cached_hole), fixture=retained, stationary=stationary,
                    quality_warnings=tuple(dict.fromkeys((
                        *quality_warnings,
                        'aperture_from_pre_transport_cache',
                        'fixture_unavailable:' + str(reason),
                        'static_fixture_prediction_from_prior_measurement',
                        *(('static_fixture_retained_while_held_arm_occludes_head',) if occluded_retained else ()),
                    ))),
                    frames=tuple(f for f in observation.frames if f.camera == object_camera),
                    pairings=tuple(p for p in observation.pairings if p.camera == object_camera),
                    faults=(),
                    geometry_scope='coarse_static_task', geometry_camera=object_camera,
                    calibration_id=cfg['calibration_id'],
                    scene_version=digest(dict(revision=self.revision, objects=[measured.model_dump(), cached_hole.model_dump()],
                                              fixture=retained.model_dump()))))
            return observation.model_copy(update=dict(objects=(measured,),fixture=retained,stationary=stationary,
                quality_warnings=(*quality_warnings,'fixture_unavailable:'+str(reason),
                    *(('static_fixture_prediction_from_prior_measurement',) if retained else ()),
                    *(('static_fixture_retained_while_held_arm_occludes_head',) if occluded_retained else ()),
                    *(('static_fixture_retention_expired',) if getattr(self,'_occluded_fixture_expired',False) else ())),
                frames=tuple(f for f in observation.frames if f.camera==object_camera),
                pairings=tuple(p for p in observation.pairings if p.camera==object_camera),faults=(),
                geometry_scope='coarse_static_task' if retained else 'coarse_object_verification',geometry_camera=object_camera,
                calibration_id=cfg['calibration_id'],
                scene_version=digest(dict(revision=self.revision,object=measured.model_dump(),scope='object_only',
                    fixture=None if retained is None else retained.model_dump()))))
        if secondary_error is not None:return object_only(secondary_error)
        hole_cfg=cfg.get('hole_camera',cfg)
        if self.coarse:
            try:hole_depth,hole_evidence,hole_masks,timing=self.secondary_future.result()
            except ValueError as exc:
                if str(exc)=='secondary_camera_pairing_unavailable':
                    raise SecondaryCameraUnavailable(str(exc)) from exc
                raise
            self._mark('head_wait_ms');self.last_timing.update(timing)
        else:
            hole_depth=aligned[y0:y1,x0:x1];hole_masks=masks;hole_evidence=evidence
        fixture=None
        self._occluded_fixture_retained=False
        self._occluded_fixture_expired=False
        if self.fixture_reference_camera is not None:
            try:
                fixture=self._locate_bucket_for_object(observation,object_camera,(hole_depth,hole_evidence,hole_masks))
                self.measured_fixture=fixture
            except ValueError as exc:
                err_code=str(exc)
                retention=self._check_occluded_fixture_retention(
                    err_code,held=held,prior=self.measured_fixture,
                    observation=observation,context=context)
                if retention=='retained':
                    self._occluded_fixture_retained=True
                else:
                    if retention=='expired':
                        self._occluded_fixture_expired=True
                    self.measured_fixture=None
                self.last_timing['independent_fixture']=dict(available=False,reason=err_code)

        def fixture_only(reason):
            # Preserve measured target and current bucket geometry without
            # inventing an aperture or treating a nominal hole as inserted.
            self.live_hole=None
            cached_hole = self._resolve_cached_aperture(observation, held=held, current_fixture=fixture, context=context)
            if cached_hole is not None:
                c_base_hole = transform(cached_hole.pose)
                c_hole_obj = np.linalg.inv(c_base_hole) @ base_object
                c_yaw = math.atan2(c_hole_obj[1, 0], c_hole_obj[0, 0])
                c_yaw_err = abs((c_yaw + math.pi / 4) % (math.pi / 2) - math.pi / 4)
                c_tilt = math.acos(float(np.clip(c_hole_obj[2, 2], -1, 1)))
                c_inserted = bool(
                    np.linalg.norm(c_hole_obj[:2, 3]) <= cfg['xy_tolerance_m']
                    and insertion_depth_reached(float(c_hole_obj[2, 3]), cfg['insertion_depth_m'],
                        tolerance_m=cfg.get('insertion_depth_tolerance_m', 0.))
                    and c_yaw_err <= cfg['yaw_tolerance_rad'] and c_tilt <= cfg['tilt_tolerance_rad'])
                measured = obj.model_copy(update=dict(
                    pose=tuple(base_object.ravel()), held=held, eef_object_pose=measured_attachment,
                    stationary=object_still, inserted=c_inserted,
                    inserted_hole_id=cfg['hole_id'] if c_inserted else None,
                    uncertainty_m=None, planning_margin_m=cfg['planning_margin_m']))
                self.revision += 1
                return observation.model_copy(update=dict(
                    objects=(measured, cached_hole), fixture=fixture, stationary=stationary,
                    quality_warnings=tuple(dict.fromkeys((*quality_warnings, 'aperture_from_pre_transport_cache'))),
                    geometry_scope='coarse_static_task', geometry_camera=object_camera,
                    calibration_id=cfg['calibration_id'],
                    scene_version=digest(dict(revision=self.revision, objects=[measured.model_dump(), cached_hole.model_dump()],
                                              fixture=fixture.model_dump()))))
            measured=obj.model_copy(update=dict(pose=tuple(base_object.ravel()),held=held,
                eef_object_pose=measured_attachment,stationary=object_still,
                inserted=None,inserted_hole_id=None,uncertainty_m=None,planning_margin_m=cfg['planning_margin_m']))
            self.revision+=1
            self.last_timing['aperture_unavailable']=reason
            return observation.model_copy(update=dict(objects=(measured,),fixture=fixture,
                stationary=stationary,quality_warnings=(*quality_warnings,'aperture_unavailable:'+str(reason)),
                geometry_scope='coarse_static_task',
                geometry_camera=object_camera,calibration_id=cfg['calibration_id'],
                scene_version=digest(dict(revision=self.revision,object=measured.model_dump(),
                                          fixture=fixture.model_dump()))))

        placing = held is True or (context or {}).get('phase') in (
            'transfer_above_hole', 'inspect_in_hand', 'align', 'insert', 'release')
        base_hole,hole_evidence,directly_visible,head_error=self._direct_aperture(
            observation,hole_cfg,hole_depth,hole_masks,hole_evidence,placing=placing)
        if head_error:
            if head_error == 'fixture_anchor_changed_or_occluded' and fixture is None:
                raise ValueError(head_error)
            if fixture is not None:return fixture_only(head_error)
            if self.coarse:return object_only(head_error)
            raise ValueError(head_error)
        if not directly_visible:
            # Independent fixture localization and recent observed aperture
            # history are complementary evidence. A current bucket pose cannot
            # replace an unseen hole, nor erase its still-valid measured history.
            # Availability is physical evidence, independent of requested skill.
            missing_aperture=(cfg.get('relocalize_visible_fixture') is True and self.live_hole is None)
            if (not context.get('authorized') or missing_aperture
                    or self.hole_last_visible_ns is None or self.occlusion is None
                    or observation.received_ns-self.hole_last_visible_ns>self.occlusion['max_occlusion_ns']
                    or not self._fixture_visible(hole_masks,hole_depth)):
                if fixture is not None:return fixture_only('aperture_not_directly_visible')
                if missing_aperture:
                    if self.coarse:return object_only('fixture_aperture_not_detected_in_search_region')
                    raise ValueError('fixture_aperture_not_detected_in_search_region')
                if self.coarse:return object_only('hole_occlusion_without_current_fixture_evidence')
                raise ValueError('hole_occlusion_without_current_fixture_evidence')
            if self.live_hole is not None:base_hole=self.live_hole.copy()
            self.last_timing['aperture_tracking']=dict(source='recent_observed_aperture_with_current_anchors',
                last_visible_ns=self.hole_last_visible_ns,currently_visible=False,
                max_occlusion_ns=self.occlusion['max_occlusion_ns'])
        if directly_visible and head_error is None:
            self._update_cached_aperture(observation, base_hole=base_hole, hole_evidence=hole_evidence,
                                         held=held, fixture=fixture, context=context)
        hole_object=np.linalg.inv(base_hole)@base_object
        yaw=math.atan2(hole_object[1,0],hole_object[0,0])
        yaw_error=abs((yaw+math.pi/4)%(math.pi/2)-math.pi/4)
        tilt=math.acos(float(np.clip(hole_object[2,2],-1,1)))
        inserted=bool(np.linalg.norm(hole_object[:2,3])<=cfg['xy_tolerance_m']
                      and insertion_depth_reached(float(hole_object[2,3]),cfg['insertion_depth_m'],
                          tolerance_m=cfg.get('insertion_depth_tolerance_m',0.) if self.coarse else 0.)
                      and yaw_error<=cfg['yaw_tolerance_rad'] and tilt<=cfg['tilt_tolerance_rad'])
        obj=obj.model_copy(update=dict(pose=tuple(base_object.ravel()),held=held,
            eef_object_pose=measured_attachment,inserted=inserted,
            inserted_hole_id=cfg['hole_id'] if inserted else None,stationary=object_still))
        if self.coarse:
            obj=obj.model_copy(update=dict(uncertainty_m=None,planning_margin_m=cfg['planning_margin_m']))
        hole=SceneObject(object_id=cfg['hole_id'],shape='square',pose=tuple(base_hole.ravel()),
            frame_id=cfg['native_frame'],uncertainty_m=None if self.coarse else cfg['uncertainty_m'],
            planning_margin_m=cfg['planning_margin_m'] if self.coarse else None,evidence_ids=hole_evidence,
            visible=directly_visible,stationary=True)
        self.revision+=1
        self._mark('postconditions_ms')
        return observation.model_copy(update=dict(objects=(obj,hole),fixture=fixture,stationary=stationary,
            quality_warnings=quality_warnings,
            geometry_scope='coarse_static_task' if self.coarse else 'precision',
            geometry_camera=object_camera if self.coarse else None,
            calibration_id=cfg['calibration_id'],scene_version=digest(dict(revision=self.revision,objects=[obj.model_dump(),hole.model_dump()]))))

    def _direct_aperture(self,observation,hole_cfg,hole_depth,hole_masks,hole_evidence,*,placing):
        import cv2
        cfg=self.config
        base_hole=transform(cfg['base_hole'])
        hx0,hy0,hx1,hy1=hole_cfg['roi']
        # A measured fixture pose is usable only while its current visual hole matches.
        camera_hole=np.linalg.inv(transform(hole_cfg['base_camera']))@base_hole
        if camera_hole[2,3]<=0:raise ValueError('hole_behind_camera')
        projected,_=cv2.projectPoints(camera_hole[:3,3].reshape(1,3),np.zeros(3),np.zeros(3),
                                      np.asarray(hole_cfg['k_rgb'],float),np.zeros(5))
        center=projected.reshape(2)-[hx0,hy0]
        hole_mask=hole_masks.get(cfg['hole_id'],np.zeros((hy1-hy0,hx1-hx0),bool))
        object_mask=hole_masks.get(cfg['object_id'],np.zeros_like(hole_mask))
        overlap=np.logical_and(hole_mask,object_mask).sum()/max(1,min(hole_mask.sum(),object_mask.sum()))
        yy,xx=np.nonzero(hole_mask)
        directly_visible=(len(xx)>=20 and overlap<=cfg.get('maximum_overlap_fraction',.05))
        head_error = None
        if directly_visible:
            if cfg.get('relocalize_visible_fixture') is True:
                from .fixture_tracking import locate_hole
                if not self.coarse or self.occlusion is None:
                    raise ValueError('fixture_relocalization_requires_registered_coarse_anchors')
                try:
                    base_hole,anchors,details=locate_hole(hole_mask,hole_depth,hole_masks,hole_cfg,
                        cfg['base_hole'],self.occlusion['anchors'],identity_tolerance_m=cfg['planning_margin_m'])
                except ValueError as exc:
                    head_error = str(exc)
                else:
                    self.live_hole=base_hole.copy();self.live_fixture_anchors=anchors
                    self.last_timing['fixture_relocalization']=details
            elif np.linalg.norm(center-[np.mean(xx),np.mean(yy)])>cfg['hole_reprojection_px']:
                raise ValueError('fixture_or_hole_moved_reestimate_required')
            if head_error is None and self.occlusion is not None and not self._fixture_visible(hole_masks,hole_depth):
                head_error = 'fixture_anchor_changed_or_occluded'
            if head_error is None:
                self.hole_last_visible_ns=observation.received_ns

        if placing and (not directly_visible or head_error):
            measured = self._cross_view_aperture(observation, hole_depth)
            if measured is not None:
                base_hole, left_id = measured
                hole_evidence = (*hole_evidence, left_id)
                directly_visible = True
                head_error = None
        return base_hole,hole_evidence,directly_visible,head_error

    def _cross_view_aperture(self, observation, depth):
        if not self.config.get('cross_view_aperture') or self.fixture_reference_camera is None:
            return None
        from .aperture_transfer import measure_observation
        try:
            pose, details = measure_observation(self, observation, depth)
        except ValueError as exc:
            self.last_timing['cross_view_aperture'] = dict(available=False, reason=str(exc))
            return None
        self.last_timing['cross_view_aperture'] = details
        # A left measurement doesn't certify the head tracker. Re-measure on
        # the next frame even if the aperture is still hidden from the head.
        self.live_hole = self.live_fixture_anchors = self.hole_last_visible_ns = None
        return pose, details['left_rgb']

    def _consume_independent_opening(self, observation, sample_time):
        if not self.config.get('measured_gripper_contact'):
            return
        from .gripper_contact import fresh_position
        packet=observation.measured_gripper
        if fresh_position(packet,frame_ns=sample_time,
                max_age_ns=self.config['measured_gripper_max_age_ns'],
                boot_id=observation.host_boot_id) is None:
            return
        value=packet.model_dump() if hasattr(packet,'model_dump') else packet
        source=value['source_instance']
        prior=self.gripper_seen[1] if self.gripper_seen and self.gripper_seen[0]==source else 0
        rows=[r for r in value['samples'] if r['sequence']>prior and r['time_valid']
              and r['received_ns']-r['queue_delay_ns']<=sample_time]
        if rows:
            self.gripper_seen=(source,rows[-1]['sequence'])
            if any(observed_open(r['closure_percent'],self.config['gripper_open_range']) for r in rows):
                self.grasp_closure.measure(observation,self.previous_context or {},self.config,at_ns=sample_time)
                self._clear_following()

    def _gripper_value(self, observation, *, frame_ns=None):
        if self.config.get('measured_gripper_contact'):
            from .gripper_contact import fresh_position
            stamp=frame_ns if frame_ns is not None else min(
                (f.received_ns for f in observation.frames if f.camera==self.config['camera']),
                default=observation.received_ns)
            measured=fresh_position(observation.measured_gripper,frame_ns=stamp,
                max_age_ns=self.config['measured_gripper_max_age_ns'],boot_id=observation.host_boot_id)
            if measured is not None:
                return measured
        # Retain the legacy coordinate for compatibility and visual-motion
        # history when CAN is absent. It cannot prove opening in the new site.
        return observation.state16[15]

    def _gripper_is_open(self, observation, *, frame_ns=None):
        if self.config.get('measured_gripper_contact'):
            from .gripper_contact import fresh_position
            stamp=frame_ns if frame_ns is not None else min(
                (f.received_ns for f in observation.frames if f.camera==self.config['camera']),
                default=observation.received_ns)
            measured=fresh_position(observation.measured_gripper,frame_ns=stamp,
                max_age_ns=self.config['measured_gripper_max_age_ns'],boot_id=observation.host_boot_id)
            return measured is not None and observed_open(measured,self.config.get('gripper_open_range'))
        return bool(observation.state_valid_mask[15] and
                    observed_open(observation.state16[15],self.config.get('gripper_open_range')))

    def _primary_evidence(self,observation,base_object,eef,stamps,*,camera=None):
        """Only measured wrist/object evidence; independent of fixture pixels."""
        from .grasp_tracking import (following_evidence, nonfollowing_evidence, square_rotation_error,
                                     quarantine_following, revalidate_following, revalidate_nonfollowing)
        cfg=self.config
        camera=camera or cfg['camera']
        frames=tuple(f for f in observation.frames if f.camera==camera)
        sample_time=min(f.received_ns for f in frames)
        relative=np.linalg.inv(eef)@base_object
        sample=dict(time=sample_time,state_ns=observation.state_received_ns,
            joints=np.asarray(observation.state16[7:14]),gripper=self._gripper_value(observation,frame_ns=sample_time),
            obj=base_object,eef=eef,relative=relative,source_stamps=tuple(stamps),
            scope_identity=getattr(self,'context_identity',None),
            camera_identity=(observation.acquisition_instance,camera,
                tuple((f.stream,f.source_epoch,f.publisher_id) for f in frames)))
        def miss_reference(**extra):
            return dict(obj=base_object.copy(),gripper=sample['gripper'],
                camera_identity=sample['camera_identity'],scope_identity=sample['scope_identity'],
                time=sample['time'],state_ns=sample['state_ns'],source_stamps=sample['source_stamps'],**extra)
        self.stationary_history.append(sample)
        # Keep only the stationary interval plus one sample spanning its beginning.
        cutoff=sample_time-cfg['stationary_window_ns']
        while len(self.stationary_history)>3 and self.stationary_history[1]['time']<=cutoff:
            self.stationary_history.popleft()
        history=list(self.stationary_history)
        stationary=bool(len(history)>=3 and len({h['state_ns'] for h in history})>=3 and
            history[-1]['time']-history[0]['time']>=cfg['stationary_window_ns'] and
            max(np.max(np.abs(h['joints']-history[-1]['joints'])) for h in history)<cfg['stationary_delta_deg'])
        held=None
        rotation_error=square_rotation_error
        object_still=bool(len(history)>=3
            and max(np.linalg.norm(h['obj'][:3,3]-base_object[:3,3]) for h in history[-3:])<cfg['object_stationary_m']
            and max(rotation_error(h['obj'],base_object) for h in history[-3:])<cfg['yaw_tolerance_rad'])
        grip_valid=bool(observation.state_valid_mask[15])
        if self.coarse:
            stationary=bool(stationary and grip_valid
                and max(abs(h['gripper']-history[-1]['gripper']) for h in history)<cfg['gripper_stationary_delta'])
            moving=bool(max(np.max(np.abs(h['joints']-history[-1]['joints'])) for h in history)
                >=cfg['stationary_delta_deg'] or
                max(abs(h['gripper']-history[-1]['gripper']) for h in history)>=cfg['gripper_stationary_delta'])
            if not stationary:
                warning='observed_motion' if moving else 'stationary_history_incomplete'
                self.last_timing.setdefault('quality_warnings',[]).append(warning)
        grip_open=self._gripper_is_open(observation,frame_ns=sample_time)
        independent_closure=None
        if cfg.get('measured_gripper_contact'):
            from .gripper_contact import fresh_position
            independent_closure=fresh_position(observation.measured_gripper,frame_ns=sample_time,
                max_age_ns=cfg['measured_gripper_max_age_ns'],boot_id=observation.host_boot_id)
            # A physical opening overrides ALL attachment proofs, including
            # visual following established while the legacy field echoed close.

        if grip_open:
            self._clear_following()
        elif not grip_valid:
            self.reject_measurement('gripper_feedback_unavailable')
        else:
            self.tracking_history.append(sample)
            tracking_cutoff=sample_time-cfg.get('tracking_window_ns',2_000_000_000)
            while self.tracking_history and self.tracking_history[0]['time']<tracking_cutoff:self.tracking_history.popleft()
        tracking_history=list(self.tracking_history) if grip_valid else []
        # Coarse attachment and precise orientation are different questions.
        # Use measurement resolution, not collision padding or insertion yaw
        # tolerance, to establish positional following.
        # The original rotation variation remains in diagnostics. A still arm
        # or an object remaining on the table cannot establish attachment.
        tracking_tolerance=(cfg['tracking_tolerance_m'] if self.coarse else cfg['uncertainty_m'])
        tracking_rotation=None if self.coarse else cfg['yaw_tolerance_rad']
        following=following_evidence(tracking_history,translation_tolerance=tracking_tolerance,
            rotation_tolerance=tracking_rotation,gripper_tolerance=cfg.get('gripper_stationary_delta'))
        following.update(attachment_profile='coarse_position_following' if self.coarse else 'rigid_pose_following',
            translation_tolerance_m=tracking_tolerance,rotation_tolerance_rad=tracking_rotation,
            orientation_quality_advisory=bool(self.coarse and following['relative_rotation_rad']>=cfg['yaw_tolerance_rad']))
        contact = dict(accepted=False, reason='measured_contact_not_enabled')
        if cfg.get('measured_gripper_contact'):
            context = self.previous_context or {}
            contact = self.grasp_closure.measure(observation, context, cfg, at_ns=sample_time)
            held = contact['held']
            # Only current measured geometry in the published grasp region can
            # become a collision attachment. Its absence changes no CAN verdict.
            associated = bool(held and target_in_closure_region(relative,
                context['grasp_contact']['expected_eef_object'], cfg.get('search_object_box'),
                tracking_tolerance))
            self.contact_attachment = contact if associated else None
            self.in_hand_reference = relative.copy() if associated else None
            self.in_hand_gripper = sample['gripper'] if associated else None
            self.in_hand_quarantine = self.in_hand_recovery_deadline_ns = self.not_in_hand_reference = None
            self.last_stamps = stamps
            self.last_received_ns = sample_time
            self.last_timing['primary_evidence'] = dict(camera=camera, received_ns=sample_time,
                state_received_ns=observation.state_received_ns, source_ns=list(stamps),
                stationary_samples=len(self.stationary_history), tracking_samples=len(self.tracking_history),
                stationary=stationary, held=held, measured_contact=contact, independent_closure=independent_closure,
                attachment_geometry_available=associated, attachment_profile='independent_gripper_contact',
                reason=contact['reason'], visual_following=following)
            return relative, stationary, object_still, held
        if following['accepted']:
            self.contact_attachment=None
            self.not_in_hand_reference=None
            self.in_hand_reference=relative.copy();self.in_hand_gripper=sample['gripper']
            # Fresh measured following establishes attachment. A later visual
            # gap has its own bounded recovery; repeated failures never extend it.
            self.in_hand_quarantine=self.in_hand_recovery_deadline_ns=None
        quarantined=getattr(self,'in_hand_quarantine',None)
        if quarantined is not None and grip_valid:
            checked=revalidate_following(quarantined,sample,translation_tolerance=tracking_tolerance,
                rotation_tolerance=tracking_rotation,gripper_tolerance=cfg.get('gripper_stationary_delta'),
                object_box=cfg.get('search_object_box'))
            following.update(checked)
            if checked.get('detached'):
                self._clear_following()
                self.not_in_hand_reference=miss_reference(attachment=quarantined['reference'])
                tracking_history=[]
            elif checked['restored']:
                self.in_hand_reference=quarantined['reference'].copy()
                self.in_hand_gripper=quarantined['gripper']
                self.in_hand_quarantine=None
                self.in_hand_recovery_deadline_ns=None
            elif checked['discard']:
                self.in_hand_quarantine=None
                self.tracking_history.clear()
        if self.in_hand_reference is not None:
            reference_translation=float(np.linalg.norm(relative[:3,3]-self.in_hand_reference[:3,3]))
            reference_rotation=rotation_error(relative,self.in_hand_reference)
            gripper_stable=(cfg.get('gripper_stationary_delta') is None or self.in_hand_gripper is not None
                and abs(sample['gripper']-self.in_hand_gripper)<cfg['gripper_stationary_delta'])
            following.update(reference_translation_m=reference_translation,reference_rotation_rad=reference_rotation)
            if (reference_translation<tracking_tolerance and (tracking_rotation is None or reference_rotation<tracking_rotation)
                    and gripper_stable):
                held=True
                if self.coarse and reference_rotation>=cfg['yaw_tolerance_rad']:
                    following['orientation_quality_advisory']=True
                if self.contact_attachment is not None:
                    following.update(reason='measured_blocked_closure',attachment_profile='independent_gripper_contact')
                elif not following['accepted'] and not following.get('restored'):
                    following['reason']='measured_following_retained'
            else:
                following['reason']=('gripper_changed_after_following' if not gripper_stable else
                    'relative_translation_changed_after_following' if reference_translation>=tracking_tolerance else
                    'relative_rotation_changed_after_following')
                if gripper_stable and self.contact_attachment is None:
                    deadline=getattr(self,'in_hand_recovery_deadline_ns',None)
                    if deadline is None and not self.coarse:
                        deadline=sample_time+cfg.get('tracking_window_ns',2_000_000_000)
                        self.in_hand_recovery_deadline_ns=deadline
                    if deadline is None or sample_time<deadline:
                        self.in_hand_quarantine=quarantine_following(self.in_hand_reference,self.in_hand_gripper,
                            sample,expires_ns=deadline)
                        following.update(quarantine_state='pending',quarantine_started_ns=sample_time,
                            quarantine_expires_ns=deadline,quarantine_support_samples=0)
                    else:following.update(quarantine_state='expired',reason='following_quarantine_expired')
                else:self.in_hand_quarantine=None
                self.in_hand_reference=None;self.in_hand_gripper=None;self.tracking_history.clear()
                self.contact_attachment=None
        # Establish a miss from measured object/hand divergence, not from a
        # missing mask or closed fingers. Keep it across later stationary
        # verification only while the same visible object and grip agree.
        miss=nonfollowing_evidence(tracking_history,translation_tolerance=tracking_tolerance,
            gripper_tolerance=cfg.get('gripper_stationary_delta'))
        if held is not True and not grip_open and grip_valid:
            if miss['accepted']:
                self.not_in_hand_reference=miss_reference()
                self.in_hand_quarantine=self.in_hand_recovery_deadline_ns=None
            reference=self.not_in_hand_reference
            if reference is not None:
                from .grasp_tracking import separated_attachment
                separated=(reference.get('attachment') is not None and cfg.get('search_object_box') is not None
                    and separated_attachment(reference['attachment'],relative,cfg['search_object_box'],tracking_tolerance))
                if (reference['camera_identity']==sample['camera_identity']
                        and reference.get('scope_identity')==sample['scope_identity']
                        and not reference.get('suspended')
                        and (separated or np.linalg.norm(reference['obj'][:3,3]-base_object[:3,3])<tracking_tolerance)
                        and (cfg.get('gripper_stationary_delta') is None or
                             abs(reference['gripper']-sample['gripper'])<cfg['gripper_stationary_delta'])):
                    held=False
                    following['reason']=('measured_attachment_departed' if separated else 'measured_object_did_not_follow_hand')
                else:
                    reference['suspended']=True
                    checked=revalidate_nonfollowing(reference,tracking_history,
                        object_box=cfg.get('search_object_box'),translation_tolerance=tracking_tolerance,
                        gripper_tolerance=cfg.get('gripper_stationary_delta'))
                    following['nonfollowing_revalidation']=checked
                    if checked['discard']:
                        self.not_in_hand_reference=None
                    elif checked['restored']:
                        held=False
                        self.not_in_hand_reference=miss_reference()
                        following['reason']=checked['reason']
        following['nonfollowing']=miss
        if grip_open and object_still:held=False
        if grip_open:following['reason']='gripper_open'
        elif not grip_valid:following['reason']='gripper_feedback_invalid'
        self.last_stamps=stamps
        self.last_received_ns=sample_time
        self.last_timing['primary_evidence']=dict(camera=camera or cfg['camera'],received_ns=sample_time,
            state_received_ns=observation.state_received_ns,source_ns=list(stamps),
            stationary_samples=len(self.stationary_history),tracking_samples=len(self.tracking_history),
            stationary=stationary,held=held,measured_contact=contact,
            independent_closure=independent_closure,**following)
        return relative,stationary,object_still,held

    def _static_approach(self, observation, obj, base_object, stamps):
        """Noncontact development only: static measured arm AND static visible face.

        Receipt-only remains receipt-only. No held/inserted or precision claims are
        produced, and the trusted task scope must separately permit this mode.
        """
        cfg=self.config
        if (not observation.state_valid_mask[15]
                or abs(observation.state16[15]-cfg['reference_gripper_closure'])>cfg['reference_gripper_tolerance']):
            raise ValueError('fresh_fixed_opening_feedback_required')
        frame_times=[f.received_ns for f in observation.frames if f.camera==cfg['camera']]
        sample=dict(time=min(frame_times),state_ns=observation.state_received_ns,
                    joints=np.asarray(observation.state16[7:14]),gripper=observation.state16[15],position=base_object[:3,3])
        self.stationary_history.append(sample)
        cutoff=sample['time']-cfg['stationary_window_ns']
        while len(self.stationary_history)>3 and self.stationary_history[1]['time']<=cutoff:
            self.stationary_history.popleft()
        history=list(self.stationary_history)
        stable=bool(len(history)>=3 and len({h['state_ns'] for h in history})>=3
                    and None not in {h['state_ns'] for h in history}
                    and history[-1]['time']-history[0]['time']>=cfg['stationary_window_ns']
                    and max(np.max(np.abs(h['joints']-sample['joints'])) for h in history)<cfg['stationary_delta_deg']
                    and max(abs(h['gripper']-sample['gripper']) for h in history)<cfg['reference_gripper_tolerance']
                    and max(np.linalg.norm(h['position']-sample['position']) for h in history)<cfg['object_stationary_m'])
        obj=obj.model_copy(update=dict(pose=tuple(base_object.ravel()),held=None,inserted=None,
            eef_object_pose=None,inserted_hole_id=None,stationary=stable,
            uncertainty_m=None,planning_margin_m=cfg['planning_margin_m']))
        self.revision+=1;self.last_stamps=stamps;self.last_received_ns=observation.received_ns
        return observation.model_copy(update=dict(objects=(obj,),stationary=stable,
            geometry_scope='coarse_static_approach',calibration_id=cfg['calibration_id'],
            geometry_camera=cfg['camera'],
            scene_version=digest(dict(revision=self.revision,objects=[obj.model_dump()]))))
