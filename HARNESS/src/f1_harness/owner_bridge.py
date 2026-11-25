"""Nonblocking owner mailbox. Numerics and network work never execute in the owner tick."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from collections import deque
import time
import threading

import numpy as np

from .contracts import HarnessError, Mode, SkillResult, digest
from .engine import UNPUBLISHED_FRESHNESS_REASONS, UNPUBLISHED_REPLAN_REASONS, UNPUBLISHED_SCENE_REASONS
from .gateway import OWNER_METHODS
from .ipc import UnixServer
from .verification import needs_semantic_evidence


PURE_SCENE_AGE_REASONS=frozenset((
    'scene_expired','camera_sample_feedback_aged:right','camera_sample_feedback_unpaired:right',
    'perception_latency_exceeds_observation_budget',
    'evidence_archive_exceeds_observation_budget','verification_transport_exceeds_observation_budget',
))


def numerical_failure_detail(exc):
    """Keep bounded numerical diagnostics, never arbitrary exception text or JSON fields."""
    import json
    import re
    # Parse before bounding: slicing a JSON envelope first silently loses all
    # diagnostics when several rejected candidates exceed the text cutoff.
    # Only the allowlisted, sanitized and size-bounded result leaves this call.
    raw=str(exc)
    pattern=r'(?:approach_requires_current_unheld_target|full_scene_required_for_motion|missing_geometry_entities|local_environment|local_observation|local_fine_depth|bounded_IK|actual_IK|numerical|revalidation|planning|post_planning|path_exceeds|empty_numerical|scene_sweep|scene_evidence|measured_gripper_evidence|no_clear_square|registered_candidates|grasp_height|grasp_has_no_placement_route|grasp_has_no_transport_route|grasp_lift|grasp_continuation|pregrasp_target|regrasp|return_route|support_)[A-Za-z0-9_]*'
    found=re.search(pattern,raw[:16384])
    code=found.group(0) if found else type(exc).__name__
    # Framing errors happen before the server sees a numerical request. Keep
    # their known cause explicit without copying arbitrary RPC exception text.
    framing={'IPC message too large':'numerical_rpc_message_too_large',
             'IPC message size rejected':'numerical_rpc_message_size_rejected',
             'incomplete IPC frame':'numerical_rpc_incomplete_frame'}
    for message,diagnostic in framing.items():
        if raw==message or raw in ('ValueError: '+message,'ConnectionError: '+message):
            code=diagnostic;break
    metrics={}
    try:
        value=json.JSONDecoder().raw_decode(raw[raw.index('{'):])[0]
    except (ValueError,TypeError):value={}
    if isinstance(value,dict):
        nested=value.get('diagnostic')
        if isinstance(nested,dict):
            nested_code=nested.get('code')
            if isinstance(nested_code,str) and re.fullmatch(pattern,nested_code):code=nested_code
            metrics=dict(nested.get('metrics',{})) if isinstance(nested.get('metrics'),dict) else {}
            if 'stage_index' in value:metrics['stage_index']=value['stage_index']
        else:metrics=value
    allowed={'stage_index','goal_index','iterations','position_error_m','rotation_error_rad','goal',
        'final_arm_deg','limits','stalled_ticks','cartesian_waypoint_index','cartesian_waypoint_count',
        'solves','termination','cartesian_stalled_ticks','before','after','min_constraint_margin_rad','active_planes','discarded_translation_m',
        'discarded_rotation_rad','numerical_correction_rad','publication_lead_rad','publication_lead_limit_rad',
        'original_observation_id','observation_id','object_id','position_tolerance_m',
        'rotation_tolerance_rad','raw_rotation_error_rad','changed_fields','skill',
        'held','inserted','inserted_hole_id','visible',
        'route_family','segment_index','obstacle_index','obstacle_role','obstacle_name',
        'collision_body','variant','collision_pair','clearance_m','arm_collision_indices',
        'grasp_strategy','grasp_pitch_offset_deg','missing_entities','role','reason','recovery',
        'continuation_skill','continuation_code','reused_failure','previous_request_id','recovery_hint',
        'start_pose_clear','end_pose_clear','collision_location',
        'segment_start_tcp_pose','segment_end_tcp_pose','endpoint_clearance_hint',
        'frame','translation_m','distance_m','scope','path_checked','grasp_checked','motion_authorized',
        'search_phase','ik_seed_count','ik_pose_fit_count','native_endpoint_checks','native_valid_goal_count',
        'ompl_started','best_fit_eef_pose','best_fit_native_checked','best_fit_native_valid','endpoint_rejection',
        'best_fit_executable','search_exhaustive','position_error_reference',
        'target_tcp_pose','best_fit_tcp_pose','target_minus_fit_tcp_m',
        'cell_min','cell_max','possible_target_overlap','views','camera','cells_tested',
        'refinement_budget_exhausted','evidence_observation_id','unknown_space_is_free',
        'pixel_min','pixel_max','unknown_pixels','unknown_tiles','z_min','z_max','nearest_depth_m','margin_m','clear'}
    def clean(value,depth=0):
        if depth>5:return None
        if value is None or isinstance(value,bool):return value
        if isinstance(value,str):return value[:160]
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            try:return value if np.isfinite(float(value)) else None
            except (OverflowError,ValueError):return None
        if isinstance(value,(list,tuple)):
            return [clean(item,depth+1) for item in value[:22]]
        if isinstance(value,dict):
            return {key:clean(item,depth+1) for key,item in value.items() if key in allowed}
        return None
    result=dict(error_type=type(exc).__name__,code=code[:96],metrics=clean(metrics))
    # Preserve why each registered route failed. Do not expose arbitrary RPC
    # exception strings; apply the same bounded code/metric schema as above.
    candidates=value.get('candidates',[]) if isinstance(value,dict) else []
    if isinstance(candidates,list):
        reasons=[]
        sampled=candidates if len(candidates)<=22 else candidates[:21]+candidates[-1:]
        for item in sampled:
            if not isinstance(item,dict):continue
            variant=item.get('variant');detail=item.get('diagnostic')
            if type(variant) is not int or not 0<=variant<=3 or not isinstance(detail,dict):continue
            candidate_code=detail.get('code')
            if not isinstance(candidate_code,str) or not re.fullmatch(pattern,candidate_code):continue
            reasons.append(dict(variant=variant,code=candidate_code[:96],
                metrics=clean(detail.get('metrics',{}))))
        if reasons:
            result['candidates']=reasons
            result['candidate_count']=len(candidates)
            # The last implementation (e.g. joint search after templates) must
            # survive text bounding; its route alone does not explain failure.
            result['last_candidate_failure']=reasons[-1]
    from .planning_diagnostics import bound_detail
    return bound_detail(result)


@dataclass(frozen=True)
class JointProposal:
    request_id: str
    epoch: int
    scene_version: str
    calibration_id: str
    full_reference: tuple[float, ...]
    targets: tuple[tuple[float, ...], ...]
    # Explicit real metrology configuration, never derived from follower error as force.
    tolerance_deg: float | None
    settle_ns: int
    gripper_tolerance: float | None = None
    geometry_trace: tuple[dict, ...] = ()
    candidate_variant: int = 0
    free_space_tracking_tolerance_deg: float | None = None
    trajectory: dict | None = None


def valid_arrival_contract(plan):
    if plan.trajectory is not None:
        from .trajectory import validate_schedule, stage_requires_stop
        try:
            return (plan.tolerance_deg is None and validate_schedule(plan.trajectory,plan.targets)
                    and all(not stage_requires_stop(stage) or stage['end_index']-1 in plan.trajectory['stops']
                            for stage in plan.geometry_trace))
        except (KeyError,TypeError,ValueError):return False
    return plan.tolerance_deg is not None and np.isfinite(plan.tolerance_deg) and plan.tolerance_deg>0


class OwnerBridge:
    def __init__(self, engine, *, numerical_planner, result_verifier, feedback_age_ns,
                 reference_tolerance_deg, clock=time.monotonic_ns,
                 stop_delta_deg=.01,stop_window_ns=200_000_000,stop_gripper_delta=None,candidate_revalidator=None,
                 waypoint_verifier=None):
        self.engine, self.planner, self.verifier = engine, numerical_planner, result_verifier
        self.clock = clock
        self.candidate_revalidator=candidate_revalidator
        self.waypoint_verifier=waypoint_verifier
        self.arrival_future=None
        self.arrival_check=None
        self.arrival_retry_ns=0
        self.arrival_refresh=None
        self.stationary_alignment_attempt=None
        self.stationary_alignment_request=None
        self.arrival_evidence=None
        self.trajectory_key=None
        self.trajectory_start_ns=None
        self.trajectory_start_index=0
        self.trajectory_at_boundary=False
        self.waypoint_wait_logged_ns=0
        self.progress_key = self.progress_anchor = None
        self.progress_changed_ns = None
        self.revalidation_proof=None
        self.revalidation_after_ns=None
        self.revalidation_observation=None
        self.alignment_wait=None
        self.pending_revalidation_plan=None
        self.feedback_age_ns = feedback_age_ns
        self.reference_tolerance_deg = reference_tolerance_deg
        if stop_delta_deg<=0 or stop_window_ns<=0:raise ValueError('invalid_stop_measurement_thresholds')
        self.stop_delta_deg,self.stop_window_ns=stop_delta_deg,stop_window_ns
        if engine.mode is Mode.HARDWARE and (stop_gripper_delta is None or not np.isfinite(stop_gripper_delta) or stop_gripper_delta<=0):
            raise ValueError('measured_gripper_stop_threshold_required')
        self.stop_gripper_delta=.1 if stop_gripper_delta is None else stop_gripper_delta
        self.hold_anchor=self.hold_last_stamp=None
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix="harness-numerics")
        self.future = self.proposal = None
        self.future_kind = None
        self.future_request = None
        self.plan_observation = None
        self.plan_full_reference = self.plan_held_reference = None
        self.proposal_started = False
        self.index = 0
        self.settled_since = None
        self.current_target = None
        self.waypoint_published_ns = None
        self.server = None
        self.last_feedback = None
        self.feedback_snapshot = None
        # Only real feedback is retained. Consumers may match a camera receive
        # time without pretending the latest joint state was measured earlier.
        self.feedback_history = deque(maxlen=256)
        self.feedback_history_ns = 1_000_000_000
        self.feedback_lock = threading.Lock()
        self.probe_lock=threading.Lock()
        self.web_lock=threading.Lock()
        self.web_queue=deque()
        self.web_results={}
        self.web_start_consumed=False
        self.web_was_authorized=False
        self.web_hold_stage='none'
        self.preview_planner=None
        self.geometry_provider=None
        self.preview_lock=threading.Lock()

    def serve(self, path):
        def dispatch(method, kwargs):
            if method=='hybrid_control':
                handler=getattr(self,'hybrid_control',None)
                if handler is None:raise HarnessError('hybrid_mode_not_enabled')
                return handler(**kwargs)
            if method=='preview_skill':return self.preview_skill(**kwargs)
            if method=='geometry_context':return self.geometry_context(**kwargs)
            if method=='status' and not kwargs:return self.status()
            if method in ('web_start','web_stop'):
                return self.queue_web_control(method,kwargs)
            if method=='web_control_status':return self.web_control_status(**kwargs)
            if method=='web_close_session':return self.web_close_session(kwargs)
            if method == 'shadow_probe':return self.shadow_probe(**kwargs)
            if method == 'measured_feedback' and set(kwargs) <= {'target_received_ns'}:
                return self.measured_feedback(**kwargs)
            if method=='ingest_live_scene':
                from .contracts import Observation
                obs=Observation.model_validate(kwargs['observation'])
                return self.engine.update_live_observation(obs,kwargs['context'])
            if method=='invalidate_live_scene':
                return self.engine.invalidate_live_observation(kwargs['reason'])
            if method == "ingest_observation":
                from .contracts import Observation
                obs=Observation.model_validate(kwargs["observation"])
                self.engine.update_observation(obs)
                return dict(observation_id=obs.observation_id)
            if method not in OWNER_METHODS:
                raise HarnessError("owner_RPC_method_forbidden")
            return getattr(self.engine,method)(**kwargs)
        self.server=UnixServer(path,dispatch,scene_evidence=True).start()

    def geometry_context(self,session_id):
        """Read task geometry off the control thread; no device authority is added."""
        from .observation import geometry_age_limit_ns, stationary_admitted
        with self.engine._lock:
            self.engine._session(session_id)
            obs=self.engine.observation
            if obs is None or self.engine.running or self.engine.pending:raise HarnessError('geometry_context_not_ready')
            if not self.geometry_provider:raise HarnessError('geometry_context_unavailable')
            profile=self.engine.task.admission_profile
            age_limit=geometry_age_limit_ns(self.engine.observation_age_ns,admission_profile=profile,
                                            geometry_scope=obs.geometry_scope)
            age=self.clock()-obs.received_ns
            if (not stationary_admitted(obs,admission_profile=profile) or age<0
                    or age_limit is not None and age>age_limit):
                raise HarnessError('geometry_context_expired')
            scope=(self.engine.epoch,digest(self.engine.task))
        feedback=self.measured_feedback()
        result=self.geometry_provider(obs,feedback['full22'],feedback['measured16'])
        with self.engine._lock:
            self.engine._session(session_id)
            if (scope!=(self.engine.epoch,digest(self.engine.task)) or self.engine.running or self.engine.pending):
                raise HarnessError('geometry_context_changed')
        return result

    def preview_skill(self,intent,include_geometry=False):
        """Check a candidate without submitting it or retaining an executable plan."""
        from .contracts import SkillIntent
        intent=SkillIntent.model_validate(intent)
        if type(include_geometry) is not bool:raise HarnessError('invalid_preview_geometry_flag')
        if intent.resume_request_id or intent.verification_request_id:
            raise HarnessError('preview_requires_new_geometric_request')
        if not self.preview_lock.acquire(blocking=False):raise HarnessError('preview_busy')
        try:
            with self.engine._lock:
                blocked=None
                try:self.engine._check_intent(intent)
                except HarnessError as exc:
                    if str(exc) not in ('full_scene_required_for_motion','approach_requires_current_unheld_target'):raise
                    # Read-only diagnostics for the validated task/scope. A
                    # partial scene never goes to numerical motion planning.
                    blocked=str(exc)
                obs=self.engine.observation
            feedback=self.measured_feedback()
            stamps=feedback['stamps16'][7:14]
            if any(not 0<=self.clock()-s<=self.feedback_age_ns for s in stamps):
                raise HarnessError('preview_feedback_expired')
            self.engine.store.event('geometric_preview_input',intent=intent.model_dump(),
                observation=obs.model_dump(),full_reference=list(feedback['full22']),
                held_reference=list(feedback['measured16']),no_motion=True)
            geometry=None
            if include_geometry or blocked:
                if self.geometry_provider:
                    geometry=self.geometry_provider(obs,feedback['full22'],feedback['measured16'])
                elif include_geometry:
                    raise HarnessError('geometry_context_unavailable')
                else:
                    from .geometry_context import entity_status
                    geometry=entity_status(self.engine.task,obs)
                # Explanatory fields include future task entities. They are
                # not a second admission policy. The same owner intent check
                # above and numerical planner below govern both interfaces.
            try:
                if blocked:
                    import json
                    raise HarnessError(blocked+':'+json.dumps(dict(
                        missing_entities=(geometry or {}).get('missing_entities',[]),
                        recovery_hint=('verify_attachment_or_plan_held_motion' if blocked=='approach_requires_current_unheld_target' else 'refresh_scene'),reason=';'.join(obs.quality_warnings))))
                plan=(self.preview_planner or self.planner)(intent,obs,feedback['full22'],feedback['measured16'])
                result=dict(planned=True,stages=[dict(name=e.get('semantic_stage'),
                    endpoint=e.get('goals',[])[-1] if e.get('goals') else None,
                    route_family=e.get('route_family'),wrist_view=e.get('pregrasp_wrist_view'),
                    grasp_descriptor=e.get('grasp_candidate_descriptor')) for e in plan.geometry_trace],candidate_variant=plan.candidate_variant,
                            waypoint_count=len(plan.targets),planning_diagnostic=None)
                from .placement_advisory import placement_advisory
                result['placement_advisory']=placement_advisory(plan.geometry_trace)
            except (HarnessError, RuntimeError) as exc:
                diagnostic=numerical_failure_detail(exc)
                if not isinstance(exc,HarnessError):
                    # The numerical RPC has no publisher. Only its recognized
                    # candidate rejections are ordinary preview results. A lost
                    # connection or unknown server failure remains an exception.
                    from .skill_services import CANDIDATE_FAILURES
                    local_view_rejection=(intent.skill=='move_tcp' and diagnostic['code'] in (
                        'local_observation_space_unknown','local_observation_departure_incomplete',
                        'local_observation_motion_requires_measured_open_gripper'))
                    if not local_view_rejection and diagnostic['code'] not in CANDIDATE_FAILURES | {'registered_candidates_exhausted'}:
                        raise
                result=dict(planned=False,planning_diagnostic=diagnostic)
            # A preview is an explanation, not a token bypassing new-state checks.
            with self.engine._lock:
                unchanged=(self.engine.authorized and self.engine.epoch==intent.epoch
                    and self.engine.task and digest(self.engine.task)==intent.task_hash
                    and not self.engine.running and not self.engine.pending and not self.engine.hold_requested)
            result.update(no_motion=True,executable=False,current_scope=bool(unchanged),
                observation_id=obs.observation_id,parameters=intent.parameters.supplied())
            self.engine.store.event('geometric_preview',request_id=intent.request_id,**result)
            if include_geometry:
                return dict(geometry=geometry,preview=result,observation_id=obs.observation_id,
                    motion_authorized=False,planned=result['planned'],current_scope=result['current_scope'],
                    planning_diagnostic=result.get('planning_diagnostic'),
                    missing_entities=geometry.get('missing_entities',[]),geometry_status=geometry.get('geometry_status'),
                    fixture_transit_available=geometry.get('fixture_transit_available',False),
                    aperture_required_for=geometry.get('aperture_required_for',[]))
            return result
        finally:self.preview_lock.release()

    def status(self):
        """Read-only fresh-owner readiness; never substitutes for a prior STOP receipt."""
        with self.engine._lock:
            result=self.engine.status()
            # A host may use a recently reobserved pose to generate an align
            # candidate only where this owner has a mandatory before-publish sweep.
            if self.candidate_revalidator is not None and self.engine.observation is not None:
                result['alignment_snapshot_id']=self.engine.observation.observation_id
        if hasattr(self,'hybrid_status'):result['hybrid']=self.hybrid_status()
        result['restart_readiness']=self.restart_readiness()
        return result

    def restart_readiness(self):
        now=self.clock()
        result=dict(state='waiting',reason='owner_busy',measured_at_ns=None,
                    owner_instance_id=self.engine.instance)
        with self.engine._lock:
            if self.engine.authorized or self.engine.pending or self.engine.running:return result
            if getattr(self.engine.store,'error',None) or getattr(self.engine.store,'closed',False):
                return dict(result,reason='owner_journal_unavailable')
        return self.stationary_feedback()

    def stationary_feedback(self,window_ns=None):
        """Measured window only: this does not grant restart or motion authority."""
        now=self.clock()
        window_ns=self.stop_window_ns if window_ns is None else max(self.stop_window_ns,window_ns)
        result=dict(state='waiting',reason='owner_busy',measured_at_ns=None,
                    owner_instance_id=self.engine.instance)
        with self.feedback_lock:history=list(self.feedback_history)
        samples=[]
        last_stamp=None
        for _,snapshot in reversed(history):
            stamps=snapshot['stamps16'];required=(*stamps[7:14],stamps[15])
            if any(not isinstance(s,(int,np.integer)) or isinstance(s,(bool,np.bool_)) for s in required):
                return dict(result,reason='feedback_missing')
            stamp=int(min(required))
            if last_stamp is None:
                if not all(0<=now-s<=self.feedback_age_ns for s in required):
                    return dict(result,reason='feedback_stale')
                last_stamp=stamp
            if samples and stamp>=samples[-1][0]:continue
            if samples and samples[-1][0]-stamp>self.feedback_age_ns:
                return dict(result,reason='feedback_discontinuous')
            measured=np.asarray(snapshot['measured16'],float)
            value=np.r_[measured[7:14]/self.stop_delta_deg,measured[15]/self.stop_gripper_delta]
            samples.append((stamp,value))
            if last_stamp-stamp>=window_ns:break
        if len(samples)<2 or last_stamp-samples[-1][0]<window_ns:
            return dict(result,reason='stationary_window_pending')
        values=np.asarray([value for _,value in samples])
        if np.max(np.ptp(values,axis=0))>=1:
            return dict(result,reason='measured_motion')
        return dict(result,state='ready',reason='fresh_owner_stationary',measured_at_ns=last_stamp,
                    window_start_ns=samples[-1][0],sample_count=len(samples))

    def _web_binding(self,payload,method='web_start'):
        from .contracts import digest
        engine=self.engine
        if not getattr(engine,'web_control_enabled',False) or engine.mode is not Mode.HARDWARE:
            raise HarnessError('web_control_not_enabled')
        if set(payload)!={'request_id','run_id','owner_instance_id','session_id','task_hash','epoch'}:
            raise HarnessError('web_control_binding_fields_required')
        if (any(not isinstance(payload[k],str) or not payload[k] or len(payload[k])>128
                for k in ('request_id','run_id','owner_instance_id','session_id','task_hash'))
                or type(payload['epoch']) is not int or payload['epoch']<0):
            raise HarnessError('invalid_web_control_identity')
        if (engine.task is None or payload['owner_instance_id']!=engine.instance
                or payload['task_hash']!=digest(engine.task)
                or payload['run_id']!=(engine.recording_identity or {}).get('owner_run_id')):
            raise HarnessError('web_control_scope_mismatch')
        if method=='web_start':
            valid=payload['session_id']==engine.session and payload['epoch']==engine.epoch
        else:
            scope=engine.web_task_scope or {}
            valid=(payload['session_id']==scope.get('session_id')
                and payload['task_hash']==scope.get('task_hash')
                and scope.get('epoch',engine.epoch+1)<=payload['epoch']<=engine.epoch)
        if not valid:raise HarnessError('web_control_scope_mismatch')

    def web_close_session(self,payload):
        """Private supervisor cleanup after the bound owner has confirmed its hold."""
        with self.web_lock, self.engine._lock:
            self._web_binding(payload,'web_stop')
            if (self.engine.authorized or self.engine.pending or self.engine.running
                    or self.engine.hold_requested):
                raise HarnessError('web_close_before_hold_confirmation')
            if self.web_was_authorized and not self.engine.hold_confirmation:
                raise HarnessError('web_close_without_hold_evidence')
            if self.engine.session is not None:
                return self.engine.close_session(self.engine.session)
            return self.engine.status()

    def queue_web_control(self,method,payload):
        """Owner Unix socket only; requests enqueue and never authorize on the RPC thread."""
        from .contracts import digest
        if method not in ('web_start','web_stop'):raise HarnessError('owner_RPC_method_forbidden')
        request_id=payload.get('request_id')
        if not isinstance(request_id,str):raise HarnessError('invalid_web_control_identity')
        identity=digest(dict(method=method,payload=payload))
        with self.web_lock:
            previous=self.web_results.get(request_id)
            if previous:
                if previous['identity']!=identity:raise HarnessError('web_request_id_content_conflict')
                return dict(previous['result'],duplicate=True)
            self._web_binding(payload,method)
            if len(self.web_queue)>=4 or len(self.web_results)>=16:
                raise HarnessError('web_control_queue_budget_exhausted')
            if method=='web_start' and (self.web_start_consumed or any(m=='web_start' for m,_ in self.web_queue)):
                raise HarnessError('web_start_is_one_shot_per_owner_run')
            result=dict(request_id=request_id,lifecycle='queued',motion_authorized=False,
                        stage='cancel_requested' if method=='web_stop' else 'authorization_requested')
            try:self.engine.store.event('web_control_requested',method=method,**payload)
            except Exception as exc:
                if method!='web_stop':raise
                result['journal_error']=type(exc).__name__+': '+str(exc)[:256]
            self.web_results[request_id]=dict(identity=identity,method=method,result=result)
            self.web_queue.append((method,dict(payload)))
            return dict(result)

    def web_control_status(self,request_id):
        if not getattr(self.engine,'web_control_enabled',False):raise HarnessError('web_control_not_enabled')
        if not isinstance(request_id,str):raise HarnessError('invalid_web_control_identity')
        with self.web_lock:
            item=self.web_results.get(request_id)
            if item is None:raise HarnessError('unknown_web_control_request')
            result=dict(item['result'])
            if item['method']=='web_stop' and result['lifecycle']=='completed':
                result['stage']=self.web_hold_stage
        return dict(result,owner=self.engine.status())

    def process_web_controls(self,*,feedback_healthy):
        """Called by the original owner tick and pre-publication guard, never by HTTP."""
        with self.web_lock:
            batch=list(self.web_queue);self.web_queue.clear()
            # A stop queued before the owner consumed start cancels that start.
            has_stop=any(method=='web_stop' for method,_ in batch)
            for method,payload in batch:
                request_id=payload['request_id']
                result=self.web_results[request_id]['result']
                try:
                    if method=='web_start' and has_stop:
                        self.web_start_consumed=True
                        result.update(lifecycle='rejected',stage='cancelled_before_authorization',reason='web_stop_pending')
                        continue
                    self._web_binding(payload,method)
                    if method=='web_start':
                        self.web_start_consumed=True
                        snapshot=self.feedback_snapshot or {}
                        stamps=snapshot.get('stamps16',())
                        now=self.clock()
                        healthy=(feedback_healthy and len(stamps)==16 and all(
                            stamp is not None and 0<=now-stamp<=self.feedback_age_ns
                            for stamp in (*stamps[7:14],stamps[15])))
                        self.engine.authorize_web(request_id=request_id,run_id=payload['run_id'],feedback_healthy=healthy)
                        self.web_was_authorized=True
                        result.update(lifecycle='completed',stage='owner_authorized',motion_authorized=True)
                    else:
                        try:self.revoke('web_stop_requested',preempted=True)
                        except Exception as exc:
                            if self.engine.authorized or self.engine.pending or self.engine.running:raise
                            result['journal_error']=type(exc).__name__+': '+str(exc)[:256]
                        confirmed=self.engine.hold_confirmation or {}
                        self.web_hold_stage=('cancel_requested' if self.engine.hold_requested else
                            confirmed.get('stage','no_motion_authorized'))
                        result.update(lifecycle='completed',stage=self.web_hold_stage,motion_authorized=False)
                    try:self.engine.store.event('web_control_consumed',method=method,**payload)
                    except Exception as exc:
                        if method!='web_stop':raise
                        result['journal_error']=type(exc).__name__+': '+str(exc)[:256]
                except Exception as exc:
                    result.update(lifecycle='rejected',stage='rejected',reason=str(exc),motion_authorized=False)
                    try:self.engine.store.event('web_control_rejected',method=method,request_id=request_id,reason=str(exc))
                    except Exception as journal_exc:
                        result['journal_error']=type(journal_exc).__name__+': '+str(journal_exc)[:256]

    def shadow_probe(self,session_id,skill='approach_grasp'):
        """Numerical candidate only: never takes the action mailbox or acknowledges a publication."""
        from .contracts import SkillIntent,digest
        from .observation import require_geometry
        import uuid
        if self.engine.mode is not Mode.SHADOW:raise HarnessError('shadow_owner_required')
        self.engine._session(session_id)
        if self.engine.authorized or self.engine.pending or self.engine.running:
            raise HarnessError('shadow_probe_requires_unarmed_owner')
        task,obs,feedback=self.engine.task,self.engine.observation,self.feedback_snapshot
        if task is None or obs is None or feedback is None:raise HarnessError('shadow_inputs_missing')
        if skill not in task.allowed_skills:raise HarnessError('skill_not_authorized')
        if obs.calibration_id!=task.calibration_id:raise HarnessError('shadow_calibration_mismatch')
        require_geometry(obs,now_ns=self.clock(),max_age_ns=self.engine.observation_age_ns)
        if any(s is None or not 0<=self.clock()-s<=self.feedback_age_ns for s in feedback['stamps16'][7:14]):
            raise HarnessError('shadow_feedback_stale')
        if not self.probe_lock.acquire(blocking=False):raise HarnessError('shadow_probe_busy')
        try:
            intent=SkillIntent(request_id=uuid.uuid4().hex,session_id=session_id,owner_instance_id=self.engine.instance,
                task_id=task.task_id,task_hash=digest(task),epoch=self.engine.epoch,observation_id=obs.observation_id,
                scene_version=obs.scene_version,calibration_id=task.calibration_id,skill=skill)
            future=self.executor.submit(self.planner,intent,obs,feedback['full22'],feedback['measured16'])
            try:proposal=future.result(timeout=10)
            except Exception:
                future.cancel();raise HarnessError('shadow_numerical_probe_failed') from None
            if self.engine.epoch!=intent.epoch or self.engine.task!=task or self.engine.authorized:
                raise HarnessError('shadow_identity_changed')
            if (proposal.request_id!=intent.request_id or proposal.epoch!=intent.epoch
                    or proposal.calibration_id!=intent.calibration_id or proposal.scene_version!=intent.scene_version):
                raise HarnessError('shadow_proposal_identity_mismatch')
            latest=self.feedback_snapshot
            if (latest is None or np.max(np.abs(np.asarray(latest['full22'])-feedback['full22']))>self.reference_tolerance_deg):
                raise HarnessError('shadow_reference_changed')
            require_geometry(obs,now_ns=self.clock(),max_age_ns=self.engine.observation_age_ns)
            targets=np.asarray(proposal.targets,float)
            if targets.ndim!=2 or targets.shape[1]!=16 or not 0<len(targets)<=100 or not np.isfinite(targets).all():
                raise HarnessError('invalid_shadow_path')
            if not np.all(targets[:,:7]==np.asarray(feedback['measured16'])[:7]) or not np.all(targets[:,14]==feedback['measured16'][14]):
                raise HarnessError('inactive_arm_proposal')
            result=dict(request_id=intent.request_id,skill=skill,waypoints=len(targets),
                        first=targets[0].tolist(),last=targets[-1].tolist(),
                        kind='numerical_candidate_only',command_publishers=0,physical_completion=False)
            self.engine.store.event('shadow_candidate_checked',result=result)
            return result
        finally:self.probe_lock.release()

    def local_start(self, *, physical_event, feedback_healthy):
        return self.engine.local_start(physical_event=physical_event,feedback_healthy=feedback_healthy)

    def begin_idle_timing_wait(self, now_ns):
        """Only an idle owner may retain task authority across a stationary gap."""
        with self.engine._lock:
            if (self.future is not None or self.proposal is not None or self.current_target is not None
                    or self.pending_revalidation_plan is not None or self.proposal_started):
                raise HarnessError('idle_motion_wait_has_computation_or_action')
            return self.engine.begin_idle_motion_wait(now_ns)

    def end_idle_timing_wait(self, scope, now_ns):
        with self.engine._lock:
            if (self.future is not None or self.proposal is not None or self.current_target is not None
                    or self.pending_revalidation_plan is not None or self.proposal_started):
                return False
            return self.engine.end_idle_motion_wait(scope,now_ns)

    def revoke(self, reason, *, preempted=False):
        try:self.engine.revoke(reason,preempted=preempted)
        finally:
            if self.arrival_future:self.arrival_future.cancel()
            self.arrival_future=self.arrival_check=None
            self.arrival_evidence=None
            self.arrival_refresh=None
            self.stationary_alignment_attempt=self.stationary_alignment_request=None
            self.trajectory_key=self.trajectory_start_ns=None
            self.trajectory_start_index=0;self.trajectory_at_boundary=False
            self.proposal=self.current_target=None
            self.pending_revalidation_plan=None
            self.revalidation_proof=None
            self.revalidation_after_ns=None
            self.revalidation_observation=None
            self.plan_observation=None
            self.plan_full_reference=self.plan_held_reference=None
            self.proposal_started=False
            self.settled_since=None
            self.waypoint_published_ns=None
            self.hold_anchor=self.hold_last_stamp=None
            if self.future:
                self.future.cancel()  # Running computation may finish; epoch rejects its result.

    def update_feedback(self, *, full22, measured16, stamps16):
        """Called in every control state, including manual takeover and cancellation."""
        if (len(full22)!=22 or len(measured16)!=16 or len(stamps16)!=16
                or not np.isfinite(np.r_[full22,measured16]).all()):
            raise ValueError('invalid_owner_feedback')
        snapshot=dict(full22=tuple(full22),measured16=tuple(measured16),stamps16=tuple(stamps16),
                      host_boot_id=self.engine.host_boot_id)
        now=self.clock()
        arm_stamps=snapshot['stamps16'][7:14]
        usable=all(isinstance(stamp,(int,np.integer)) and not isinstance(stamp,bool)
                   and stamp>=0 for stamp in arm_stamps)
        with self.feedback_lock:
            self.feedback_snapshot=snapshot
            if usable:
                stamp=int(min(arm_stamps))
                if (not self.feedback_history
                        or snapshot['stamps16']!=self.feedback_history[-1][1]['stamps16']):
                    self.feedback_history.append((stamp,snapshot))
            while self.feedback_history and self.feedback_history[0][0]<now-self.feedback_history_ns:
                self.feedback_history.popleft()
        self.last_feedback=np.asarray(measured16,float)

    def measured_feedback(self, target_received_ns=None):
        """Return a recorded snapshot; matching is read-only and never interpolates."""
        if target_received_ns is not None and (type(target_received_ns) is not int or target_received_ns<0):
            raise HarnessError('invalid_feedback_target_timestamp')
        with self.feedback_lock:
            snapshot=self.feedback_snapshot
            history=tuple(self.feedback_history) if target_received_ns is not None else ()
        if snapshot is None:raise HarnessError('owner_feedback_missing')
        if target_received_ns is None:return dict(snapshot)
        now=self.clock()
        if not 0<=now-target_received_ns<=self.feedback_history_ns:
            raise HarnessError('owner_feedback_history_expired')
        available=[(stamp,value) for stamp,value in history
                   if 0<=now-stamp<=self.feedback_history_ns]
        if not available:raise HarnessError('owner_feedback_history_missing')
        stamp,snapshot=min(available,key=lambda item:(abs(item[0]-target_received_ns),-item[0]))
        return dict(snapshot,matched_state_received_ns=stamp,target_received_ns=target_received_ns,
                    match_delta_ns=abs(stamp-target_received_ns))

    def _discard_unpublished_plan(self, reason, *, inputs_unchanged=False, planning_diagnostic=None):
        """A computed target is not an action. Fail it and flush any queued first target."""
        with self.engine._lock:
            running=self.engine.running
            self.engine.store.event('harness_plan_discarded',reason=reason,
                request_id=running.request_id if running else self.future_request,
                observation_id=getattr(self.plan_observation,'observation_id',None),
                before_first_publication=True)
            if (inputs_unchanged and reason in UNPUBLISHED_REPLAN_REASONS
                    and running and running.request_id==self.future_request):
                try:
                    variant=self.proposal.candidate_variant if reason=='owner_candidate_rejected' and self.proposal else None
                    self.engine.reject_unpublished_for_replan(running.request_id,reason=reason,candidate_variant=variant,
                        planning_diagnostic=planning_diagnostic)
                except HarnessError:
                    pass  # Cancellation, publication or deadline makes retry ineligible.
                else:
                    # Detach work before permitting another request. A late old
                    # future cannot be mistaken for the next request's result.
                    if self.future is not None:self.future.cancel()
                    self.future=self.future_kind=self.future_request=None
                    self.proposal=self.current_target=self.pending_revalidation_plan=None
                    self.revalidation_proof=self.revalidation_after_ns=None
                    self.revalidation_observation=None
                    self.plan_observation=self.plan_full_reference=self.plan_held_reference=None
                    self.proposal_started=False
                    self.index=0;self.settled_since=self.waypoint_published_ns=None
                    return
            if running and running.request_id==self.future_request and self.engine.authorized:
                try:
                    self.engine.complete(running.request_id,SkillResult(request_id=running.request_id,
                        lifecycle='completed',outcome='failure',reason=reason,
                        no_motion=running.request_id not in self.engine.request_publications,
                        planning_diagnostic=planning_diagnostic,
                        synthetic=self.engine.mode is Mode.FAKE))
                except HarnessError:
                    pass  # A simultaneous timeout/cancel already owns the terminal result.
            self.revoke(reason)

    def _unpublished_reference_failure(self, plan, obs, *, full22, measured16, stamps16, held16, now_ns,
                                       before_planning=False):
        """Check non-retryable bindings and physical inputs before temporal rejection."""
        running=self.engine.running
        if (not running or running.epoch!=self.engine.epoch
                or not before_planning and (plan is None or plan.request_id!=running.request_id
                    or plan.epoch!=self.engine.epoch or plan.scene_version!=running.scene_version
                    or plan.calibration_id!=running.calibration_id)
                or not self.engine.authorized or self.engine.hold_requested):
            return 'unpublished_plan_authority_changed'
        # A receipt, including zero/unknown publication counts, is stronger
        # evidence than a missing in-memory proposal_started flag. Never
        # resume such a request through the unpublished-candidate path.
        if running.request_id in self.engine.request_publications:
            return 'unpublished_plan_has_publication_receipt'
        if (obs is None or self.plan_full_reference is None or self.plan_held_reference is None
                or obs.observation_id!=running.observation_id or obs.scene_version!=running.scene_version
                or obs.calibration_id!=running.calibration_id):
            return 'unpublished_plan_observation_binding_missing'
        reference=self.plan_full_reference if before_planning else plan.full_reference
        gripper_tolerance=self.stop_gripper_delta if before_planning else plan.gripper_tolerance
        if not np.array_equal(np.asarray(reference),self.plan_full_reference):
            return 'numerical_reference_differs_from_planning_snapshot'
        try:
            if before_planning:
                valid=(len(reference)==22 and len(self.plan_held_reference)==16
                       and np.isfinite(reference).all() and np.isfinite(self.plan_held_reference).all())
                if not valid:return 'invalid_numerical_reference'
                if not np.array_equal(np.asarray(held16)[:7],np.asarray(self.plan_held_reference)[:7]) or held16[14]!=self.plan_held_reference[14]:
                    return 'inactive_arm_reference_changed'
            else:
                targets=np.asarray(plan.targets,float)
                valid=(targets.ndim==2 and targets.shape[1]==16 and 0<len(targets)<=100
                       and np.isfinite(targets).all() and np.isfinite(plan.full_reference).all()
                       and valid_arrival_contract(plan) and plan.settle_ns>0
                       and plan.gripper_tolerance is not None and np.isfinite(plan.gripper_tolerance)
                       and plan.gripper_tolerance>0)
        except (TypeError,ValueError):valid=False
        if not valid:return 'invalid_numerical_proposal'
        if (not before_planning and plan.trajectory is not None
                and not np.array_equal(plan.trajectory['anchor'],self.plan_held_reference)):
            return 'trajectory_anchor_differs_from_planning_snapshot'
        if not before_planning and (not np.all(targets[:,:7]==np.asarray(held16)[:7]) or not np.all(targets[:,14]==held16[14])):
            return 'inactive_arm_proposal'
        if (len(stamps16)!=16 or any(s is None or not 0<=now_ns-s<=self.feedback_age_ns
                                    for s in (*stamps16[7:14],stamps16[15]))):
            return 'planning_feedback_stale_before_publication'
        if (np.max(np.abs(np.asarray(full22)[7:14]-np.asarray(reference)[7:14]))>self.reference_tolerance_deg
                or np.max(np.abs(np.asarray(measured16)[7:14]-np.asarray(reference)[7:14]))>self.reference_tolerance_deg
                or np.max(np.abs(np.asarray(held16)[7:14]-np.asarray(self.plan_held_reference)[7:14]))>self.reference_tolerance_deg):
            return 'right_arm_reference_changed_during_planning'
        if (abs(measured16[15]-self.plan_held_reference[15])>gripper_tolerance
                or abs(held16[15]-self.plan_held_reference[15])>gripper_tolerance):
            return 'gripper_reference_changed_during_planning'
        if np.max(np.abs(np.asarray(full22)[14:]-np.asarray(reference)[14:]))>self.reference_tolerance_deg:
            return 'body_or_head_reference_changed'
        development=getattr(self.engine,'development_admission',None)
        if development is not None and development.get('goal')=='approach_grasp':
            reference=development.get('right_reference',{}).get('reference_gripper_closure')
            if (not isinstance(reference,(int,float)) or not np.isfinite(reference)
                    or abs(measured16[15]-reference)>min(gripper_tolerance,self.stop_gripper_delta)):
                return 'measured_development_gripper_reference_changed'
        return None

    def reject_candidate_before_publication(self, *, full22, measured16, held16, now_ns):
        """Only called after the native owner proves measured AND held poses safe."""
        plan=self.proposal;running=self.engine.running
        if (plan is None or self.proposal_started or running is None
                or running.request_id in self.engine.request_publications
                or running.skill not in ('approach_grasp','transfer_above_hole')
                or self.feedback_snapshot is None):return False
        failure=self._unpublished_reference_failure(plan,self.plan_observation,
            full22=full22,measured16=measured16,held16=held16,
            stamps16=self.feedback_snapshot['stamps16'],now_ns=now_ns)
        if failure:return False
        self._discard_unpublished_plan('owner_candidate_rejected',inputs_unchanged=True)
        return True

    def _wait_for_alignment(self, running, latest, invalid_reason, full22, measured16, stamps16, held16, now):
        from .revalidation import alignment_measurement_gap
        from .observation import OBSERVATION_WINDOW_S
        gap=alignment_measurement_gap(self.plan_observation,latest,task=self.engine.task,skill='align')
        if gap=='revalidation_attachment_changed':
            self._discard_unpublished_plan('plan_revalidation_rejected:'+gap)
            return True
        waiting=self.alignment_wait
        if gap and (waiting is None or waiting[0]!=running.request_id):
            waiting=self.alignment_wait=(running.request_id, now)
            self.engine.store.event('harness_plan_waiting_live_scene',request_id=running.request_id,
                reason=gap,latest_observation_id=latest.observation_id,after_ns=self.revalidation_after_ns)
        if waiting is None or waiting[0]!=running.request_id:
            return False
        failure=self._unpublished_reference_failure(self.pending_revalidation_plan,self.plan_observation,
            full22=full22,measured16=measured16,stamps16=stamps16,held16=held16,now_ns=now)
        if failure is not None or latest is None and invalid_reason not in PURE_SCENE_AGE_REASONS:
            self._discard_unpublished_plan(failure or 'revalidation_live_scene_invalidated')
            return True
        if gap or latest is None:
            if now-waiting[1]>=int(OBSERVATION_WINDOW_S*1e9):
                self._discard_unpublished_plan('plan_revalidation_rejected:revalidation_target_measurement_unavailable',
                    inputs_unchanged=True)
            return True
        self.alignment_wait=None
        return False

    def validate_first_publication(self, *, full22, measured16, stamps16, held16, now_ns):
        """Recheck the original planning inputs immediately before the first real publish.

        New observations may replace engine.observation while IK is running. They
        never refresh the immutable observation captured for this proposal.
        """
        if self.proposal is None or self.proposal_started:
            return True
        plan,obs=self.proposal,self.plan_observation
        running=self.engine.running
        freshness=obs
        reason=self._unpublished_reference_failure(plan,obs,full22=full22,measured16=measured16,
            stamps16=stamps16,held16=held16,now_ns=now_ns)
        if reason:
            self._discard_unpublished_plan(reason)
            return False
        if self.revalidation_proof is not None:
            from .revalidation import (proof_observation, RevalidationExpired, RevalidationSceneChanged,
                                       RevalidationEvidenceUpdated, TRANSPORT_MEASUREMENT_PENDING,
                                       alignment_measurement_gap)
            try:
                live=getattr(self.engine,'live_observation',None)
                from .local_environment import local_scope
                alignment_gap=alignment_measurement_gap(obs,live,task=self.engine.task,skill=running.skill)
                if alignment_gap=='revalidation_attachment_changed':
                    raise RevalidationSceneChanged(alignment_gap)
                if (alignment_gap or live is not None and live.geometry_scope=='coarse_object_verification'
                        and not (local_scope(live) and running.skill in ('move_tcp','reobserve'))):
                    # No movement with object-only evidence: retain the fixed
                    # candidate until the required fixture view is available.
                    self.pending_revalidation_plan=plan
                    self.proposal=self.current_target=None
                    self.revalidation_proof=None;self.revalidation_after_ns=now_ns
                    self.engine.store.event('harness_plan_waiting_live_scene',request_id=plan.request_id,
                        reason=alignment_gap or 'required_camera_pending:head',
                        after_ns=now_ns)
                    return False
                temporal_invalidation=getattr(self.engine,'live_observation_invalid_reason',None) in PURE_SCENE_AGE_REASONS
                if live is None and not temporal_invalidation:
                    raise HarnessError('revalidation_live_scene_invalidated')
                if self.revalidation_proof['after_planning_ns']!=self.revalidation_after_ns:
                    raise HarnessError('revalidation_planning_generation_mismatch')
                freshness=proof_observation(self.revalidation_proof,proposal=plan,original=obs,
                    intent=running,task=self.engine.task,now_ns=now_ns,max_age_ns=self.engine.observation_age_ns,
                    live_observation=live)
                if live is None:
                    # A known acquisition delay does not authorize this old
                    # candidate. Await new measurements and repeat its sweep;
                    # the original skill deadline and lease remain unchanged.
                    self.pending_revalidation_plan=plan
                    self.proposal=self.current_target=None
                    self.revalidation_proof=None
                    self.revalidation_after_ns=now_ns
                    self.waypoint_published_ns=None
                    self.engine.store.event('harness_plan_waiting_live_scene',request_id=plan.request_id,
                        reason=self.engine.live_observation_invalid_reason,after_ns=now_ns)
                    return False
            except RevalidationEvidenceUpdated as exc:
                # Same candidate/request/authority and original compute time.
                # New relevant space evidence goes to the existing numerical
                # worker; this tick performs no FK or recursive scene sweep.
                self.pending_revalidation_plan=plan
                self.proposal=self.current_target=None
                self.revalidation_proof=None;self.waypoint_published_ns=None
                # An incomplete live view cannot be swept, even if the worker
                # had a complete earlier one. A comparable changed view can.
                self.revalidation_observation=live if str(exc)==TRANSPORT_MEASUREMENT_PENDING else None
                self.engine.store.event('harness_plan_waiting_live_scene',request_id=plan.request_id,
                    reason=str(exc),after_ns=self.revalidation_after_ns,
                    latest_observation_id=live.observation_id)
                return False
            except RevalidationExpired as exc:
                # Time alone invalidates the proof, not a never-published plan.
                # Require a new scene and another complete sweep under the same
                # request, authority and original deadline; never reuse the proof.
                self.pending_revalidation_plan=plan
                self.proposal=self.current_target=None
                self.revalidation_proof=None;self.revalidation_after_ns=now_ns
                self.waypoint_published_ns=None
                self.engine.store.event('harness_plan_waiting_live_scene',request_id=plan.request_id,
                    reason='proof_expired_awaiting_new_measurements',after_ns=now_ns)
                return False
            except RevalidationSceneChanged as exc:
                diagnostic=numerical_failure_detail(exc)
                self.engine.store.event('harness_live_scene_changed',request_id=plan.request_id,
                                        diagnostic=diagnostic)
                self._discard_unpublished_plan('revalidation_proof_rejected:'+diagnostic['code'],inputs_unchanged=True,
                    planning_diagnostic=diagnostic)
                return False
            except (HarnessError,ValueError,TypeError,KeyError,AttributeError) as exc:
                self._discard_unpublished_plan('revalidation_proof_rejected:'+str(exc),
                    planning_diagnostic=numerical_failure_detail(exc))
                return False
        from .observation import geometry_age_limit_ns
        age_limit=geometry_age_limit_ns(self.engine.observation_age_ns,
            admission_profile=self.engine.task.admission_profile,geometry_scope=freshness.geometry_scope)
        if now_ns<freshness.received_ns:
            reason='planning_observation_clock_invalid'
        elif any(now_ns<f.received_ns for f in freshness.frames):
            reason='planning_frame_clock_invalid'
        elif freshness.state_received_ns is not None and now_ns<freshness.state_received_ns:
            reason='planning_state_clock_invalid'
        elif age_limit is not None and now_ns-freshness.received_ns>age_limit:
            reason='planning_observation_expired_before_publication'
        elif age_limit is not None and any(now_ns-f.received_ns>age_limit for f in freshness.frames):
            reason='planning_frame_expired_before_publication'
        elif age_limit is not None and freshness.state_received_ns is not None and now_ns-freshness.state_received_ns>age_limit:
            reason='planning_state_expired_before_publication'
        if reason:
            self._discard_unpublished_plan(reason,inputs_unchanged=True)
            return False
        return True

    def poll(self, *, full22, measured16, stamps16, held16):
        """Called after local buttons/faults; returns candidate only, never publishes."""
        now=self.clock()
        self.update_feedback(full22=full22,measured16=measured16,stamps16=stamps16)
        self.engine.tick()
        if len(stamps16)!=16 or any(s is None or not 0 <= now-s <= self.feedback_age_ns for s in stamps16[7:14]):
            self.revoke("harness_feedback_stale")
            return None
        self.last_feedback=np.asarray(measured16,float)
        if self.engine.hold_requested or not self.engine.authorized:
            self.proposal=self.current_target=None
            self.pending_revalidation_plan=None
            self.revalidation_proof=None
            return None
        if self.future is not None and self.future.done():
            future,self.future=self.future,None
            running=self.engine.running
            valid_running=running and running.request_id == self.future_request and running.epoch == self.engine.epoch
            try:
                proposal=future.result()
            except Exception as exc:
                if valid_running:
                    diagnostic=numerical_failure_detail(exc)
                    self.engine.store.event('harness_skill_computation_failed',request_id=running.request_id,
                        epoch=running.epoch,skill=running.skill,phase=self.future_kind,diagnostic=diagnostic)
                    if self.future_kind=='revalidate':
                        from .revalidation import TRANSPORT_MEASUREMENT_PENDING
                        reason='plan_revalidation_rejected:'+diagnostic['code']
                        transport_pending=(running.skill=='transfer_above_hole'
                                           and diagnostic['code']==TRANSPORT_MEASUREMENT_PENDING)
                        if reason in UNPUBLISHED_FRESHNESS_REASONS or transport_pending:
                            failure=self._unpublished_reference_failure(self.pending_revalidation_plan,
                                self.plan_observation,full22=full22,measured16=measured16,
                                stamps16=stamps16,held16=held16,now_ns=now)
                            current=getattr(self.engine,'live_observation',None)
                            wait_for_current=(current is not None and not current.faults
                                or current is None and getattr(self.engine,'live_observation_invalid_reason',None)
                                in PURE_SCENE_AGE_REASONS)
                            if failure is None and wait_for_current:
                                # Expired evidence needs fresh timestamps. For a
                                # missing view, a later sample may already have
                                # arrived during computation. Keep the original
                                # planning timestamp and skip the attempted view.
                                if not transport_pending:self.revalidation_after_ns=now
                                self.engine.store.event('harness_plan_waiting_live_scene',request_id=running.request_id,
                                    reason=diagnostic['code'] if transport_pending else 'worker_expired_awaiting_new_measurements',
                                    latest_observation_id=getattr(self.revalidation_observation,'observation_id',None),
                                    after_ns=self.revalidation_after_ns)
                                return None
                            if failure is None and current is None:
                                failure='revalidation_live_scene_invalidated'
                            self._discard_unpublished_plan(failure or reason,inputs_unchanged=failure is None,
                                planning_diagnostic=diagnostic)
                        elif reason in UNPUBLISHED_SCENE_REASONS:
                            failure=self._unpublished_reference_failure(self.pending_revalidation_plan,
                                self.plan_observation,full22=full22,measured16=measured16,
                                stamps16=stamps16,held16=held16,now_ns=now)
                            self._discard_unpublished_plan(failure or reason,inputs_unchanged=failure is None,
                                planning_diagnostic=diagnostic)
                        else:self._discard_unpublished_plan(reason,planning_diagnostic=diagnostic)
                    elif self.future_kind=='plan' and diagnostic['code']=='planning_measurements_expired':
                        failure=self._unpublished_reference_failure(None,self.plan_observation,
                            full22=full22,measured16=measured16,stamps16=stamps16,held16=held16,
                            now_ns=now,before_planning=True)
                        self._discard_unpublished_plan(failure or 'numerical_plan_rejected:planning_measurements_expired',
                            inputs_unchanged=failure is None)
                    else:
                        self.engine.complete(running.request_id,SkillResult(request_id=running.request_id,
                            lifecycle="completed",outcome='pending_review' if diagnostic['code']=='registered_candidates_exhausted' else 'failure',
                            no_motion=(self.future_kind=='plan'
                                       and running.request_id not in self.engine.request_publications),
                            planning_diagnostic=diagnostic if self.future_kind=='plan' else None,
                            reason="numerical_plan_rejected:"+diagnostic['code']))
                return None
            if not valid_running:
                return None
            if self.future_kind=='stationary_align':
                check=self.stationary_alignment_attempt
                candidate=self.proposal
                if any(s is None or not 0<=now-s<=self.feedback_age_ns for s in (*stamps16[7:14],stamps16[15])):
                    self.revoke('waypoint_feedback_stale')
                    return None
                fresh_stamp=min((*stamps16[7:14],stamps16[15]))
                stationary=self.stationary_feedback(candidate.settle_ns) if candidate is not None else {}
                accepted=bool(check and candidate and proposal.get('accepted') is True
                    and check['key']==(running.request_id,running.epoch)
                    and check['stamp']<=fresh_stamp
                    and np.max(np.abs(np.asarray(measured16)[7:14]-check['measured'][7:14]))<=self.stop_delta_deg
                    and abs(measured16[15]-check['measured'][15])<=self.stop_gripper_delta
                    and stationary.get('state')=='ready')
                if accepted:
                    from .stationary_alignment import constant_alignment
                    accepted=constant_alignment(running,candidate,held16)
                if accepted and self.validate_first_publication(full22=full22,measured16=measured16,
                        stamps16=stamps16,held16=held16,now_ns=now):
                    self.engine.store.event('harness_stationary_alignment_verified',request_id=running.request_id,
                        epoch=running.epoch,proof=proposal,stationary_feedback=stationary,
                        checked_stamp_ns=check['stamp'],measured_stamp_ns=fresh_stamp,
                        motion_published=False,verification_after_ns=now)
                    self.stationary_alignment_request=running.request_id
                    self.proposal=self.current_target=None
                    self.verification_after_ns=now;self.verification_wait_attempts=0
                    self.future_kind='verify'
                    self.future=self.executor.submit(self.verifier,running,now)
                return None
            if self.future_kind=='revalidate':
                candidate=self.pending_revalidation_plan
                if candidate is None:return None
                self.proposal,self.index,self.settled_since=candidate,0,None
                self.waypoint_published_ns=None
                self.revalidation_proof=proposal
                self.pending_revalidation_plan=None
                self.proposal_started=False
                if not self.validate_first_publication(full22=full22,measured16=measured16,
                        stamps16=stamps16,held16=held16,now_ns=now):return None
                self.engine.store.event('harness_plan_revalidated',proof=proposal,
                    original_observation_id=self.plan_observation.observation_id)
                proposal=candidate
            elif self.future_kind == "verify":
                if proposal is None:
                    proposal=SkillResult(request_id=running.request_id,lifecycle="completed",
                                         outcome="pending_review",reason="fresh_post_motion_evidence_missing")
                semantic_pending=needs_semantic_evidence(proposal)
                if (proposal.outcome=='pending_review' and (semantic_pending or proposal.reason in (
                        'fresh_post_motion_scene_missing','fresh_post_motion_evidence_missing','pre_motion_frame')
                        or proposal.reason.startswith('post_motion_scene_not_ready:'))):
                    self.engine.store.event('harness_verification_wait',request_id=running.request_id,
                        reason=proposal.reason,after_ns=self.verification_after_ns,motion_replayed=False,
                        observation_reason=self.engine.live_observation_invalid_reason,
                        attempt=getattr(self,'verification_wait_attempts',0)+1)
                    if semantic_pending:
                        # The verifier already consumed an inconclusive scene.
                        # Rechecking must await later measurements, including
                        # across the outer program's read-only continuation.
                        self.verification_after_ns=max(self.verification_after_ns,now)
                    attempts=getattr(self,'verification_wait_attempts',0)+1
                    self.verification_wait_attempts=attempts
                    if attempts<2:
                        self.future=self.executor.submit(self.verifier,running,self.verification_after_ns)
                        return None
                    reason=proposal.reason if semantic_pending else self.engine.live_observation_invalid_reason or proposal.reason
                    key=running.verification_request_id or running.request_id
                    checkpoint=self.engine.verification_checkpoints.setdefault(key,dict(
                        skill=running.skill,epoch=running.epoch,task_hash=running.task_hash,
                        after_ns=self.verification_after_ns,attempts=0,claimed_by=None))
                    checkpoint['after_ns']=max(checkpoint['after_ns'],self.verification_after_ns)
                    proposal=proposal.model_copy(update=dict(
                        reason='post_motion_verification_unavailable:'+reason,
                        verification_request_id=key if checkpoint['attempts']<self.engine.task.local_corrections else None))
                    self.engine.store.event('harness_verification_escalated',request_id=running.request_id,
                        reason=reason,attempts=attempts,after_ns=self.verification_after_ns,
                        motion_replayed=False)
                if self.stationary_alignment_request==running.request_id:
                    proposal=proposal.model_copy(update=dict(no_motion=True,
                        reason='stationary_alignment_and_fresh_scene' if proposal.outcome=='success' else proposal.reason))
                self.engine.complete(running.request_id,proposal,
                    alignment_after_ns=self.verification_after_ns if running.skill=='align' else None)
                if running.skill=='verify_placed' and proposal.outcome=='success':
                    self.engine.store.event('owner_placement_verified',request_id=running.request_id,
                        observation_id=self.engine.observation.observation_id,
                        evidence_ids=proposal.evidence_ids,synthetic=self.engine.mode==Mode.FAKE)
                return None
            if (not running or proposal.request_id!=running.request_id or proposal.epoch!=self.engine.epoch
                or proposal.calibration_id!=running.calibration_id or proposal.scene_version!=running.scene_version):
                return None
            if (len(proposal.full_reference)!=22 or not proposal.targets or not valid_arrival_contract(proposal)
                    or not np.isfinite(proposal.full_reference).all()
                    or proposal.settle_ns<=0
                    or proposal.gripper_tolerance is None or not np.isfinite(proposal.gripper_tolerance)
                    or proposal.gripper_tolerance<=0):
                self.revoke("invalid_numerical_proposal")
                return None
            if self.candidate_revalidator is not None and self.future_kind=='plan':
                self.pending_revalidation_plan=proposal
                self.revalidation_observation=None
                self.revalidation_after_ns=now
                self.revalidation_proof=None
                from dataclasses import asdict
                self.engine.store.event('harness_plan_waiting_fresh_revalidation',request_id=proposal.request_id,
                    candidate=asdict(proposal),original_observation=self.plan_observation.model_dump(),
                    original_held16=self.plan_held_reference,after_planning_ns=now)
            else:
                self.proposal,self.index,self.settled_since=proposal,0,None
                self.waypoint_published_ns=None
                self.proposal_started=False
        if self.pending_revalidation_plan is not None:
            if self.future is None:
                from .revalidation import attachment_measurement_pending, fresh_after
                from .observation import stationary_admitted
                from .local_environment import local_scope
                with self.engine._lock:
                    latest=getattr(self.engine,'live_observation',None)
                    running=self.engine.running
                    invalid_reason=getattr(self.engine,'live_observation_invalid_reason',None)
                attempted=self.revalidation_observation
                if (running is not None and running.skill=='align' and self._wait_for_alignment(
                        running,latest,invalid_reason,full22,measured16,stamps16,held16,now)):
                    return None
                if running is not None and running.skill=='transfer_above_hole' and attempted is not None:
                    if latest is None and invalid_reason not in PURE_SCENE_AGE_REASONS:
                        self._discard_unpublished_plan('revalidation_live_scene_invalidated')
                        return None
                    if (latest is not None and attempted.acquisition_instance==latest.acquisition_instance
                            and (latest.observation_id==attempted.observation_id
                                 or latest.received_ns<=attempted.received_ns)):
                        return None
                if (running is not None and fresh_after(latest,self.revalidation_after_ns,
                        task=self.engine.task,skill=running.skill)
                        and (latest.geometry_scope!='coarse_object_verification'
                             or local_scope(latest) and running.skill in ('move_tcp','reobserve'))
                        and not attachment_measurement_pending(self.plan_observation,latest,task=self.engine.task,skill=running.skill)
                        and stationary_admitted(latest,admission_profile=self.engine.task.admission_profile)
                        and not latest.faults):
                    self.future_kind='revalidate'
                    self.revalidation_observation=latest
                    self.engine.store.event('harness_revalidation_started',request_id=running.request_id,
                        original_observation_id=self.plan_observation.observation_id,
                        observation=latest.model_dump(),after_planning_ns=self.revalidation_after_ns,
                        publication_count=self.engine.request_publications.get(running.request_id,{}).get('publication_count',0))
                    self.future=self.executor.submit(self.candidate_revalidator,running,
                        self.pending_revalidation_plan,self.plan_observation,latest,
                        self.plan_full_reference,self.plan_held_reference,self.revalidation_after_ns)
            return None
        if self.proposal:
            plan=self.proposal
            running=self.engine.running
            # Consume the background observation before publishing another
            # loaded waypoint. No SAM, FK, RPC or polling runs in this tick.
            if (running and running.skill in ('transfer_above_hole','move_tcp','align','insert','retreat')
                    and running.request_id in self.engine.request_publications
                    and self.plan_observation is not None
                    and any(o.object_id==self.engine.task.object_id and o.held is True
                            for o in self.plan_observation.objects)):
                from .grasp_execution import lift_evidence
                live=getattr(self.engine,'live_observation',None)
                verdict=lift_evidence(live,object_id=self.engine.task.object_id,
                    calibration_id=plan.calibration_id,after_ns=self.plan_observation.received_ns,
                    gripper=measured16[15],gripper_tolerance=plan.gripper_tolerance)
                if verdict is False:
                    self.engine.complete_grasp_miss(now_ns=now,observation_id=live.observation_id)
                    if self.arrival_future:self.arrival_future.cancel()
                    self.arrival_future=self.arrival_check=None
                    self.arrival_refresh=None
                    self.proposal=self.current_target=None
                    self.hold_anchor=self.hold_last_stamp=self.settled_since=None
                    self.waypoint_published_ns=None
                    return None
            if self.future is not None and self.future_kind=='stationary_align':return None
            if not self.validate_first_publication(full22=full22,measured16=measured16,
                    stamps16=stamps16,held16=held16,now_ns=now):
                return None
            if np.max(np.abs(np.asarray(full22)[14:]-np.asarray(plan.full_reference)[14:])) > self.reference_tolerance_deg:
                self.revoke("body_or_head_reference_changed")
                return None
            from .stationary_alignment import constant_alignment,check_alignment
            running=self.engine.running
            key=(running.request_id,running.epoch) if running else None
            attempted=self.stationary_alignment_attempt
            if (not self.proposal_started and self.revalidation_proof is not None
                    and self.waypoint_verifier is not None and constant_alignment(running,plan,held16)
                    and running.request_id not in self.engine.request_publications
                    and (attempted is None or attempted['key']!=key)
                    and self.stationary_feedback(plan.settle_ns)['state']=='ready'):
                self.stationary_alignment_attempt=dict(key=key,measured=np.asarray(measured16,float).copy(),
                    stamp=min((*stamps16[7:14],stamps16[15])))
                self.future_kind,self.future_request='stationary_align',running.request_id
                self.future=self.executor.submit(check_alignment,self.waypoint_verifier,running,plan,
                    self.plan_observation,tuple(full22),tuple(measured16))
                return None
            if plan.trajectory is not None:
                from .trajectory import sample
                key=(plan.request_id,plan.epoch)
                if self.trajectory_key!=key:
                    self.trajectory_key=key;self.trajectory_start_ns=None
                    self.trajectory_start_index=0;self.arrival_evidence=None
                    self.arrival_refresh=None
                elapsed=0. if self.trajectory_start_ns is None else (now-self.trajectory_start_ns)/1e9
                target,self.index,self.trajectory_at_boundary,_=sample(plan.trajectory,plan.targets,
                    start_index=self.trajectory_start_index,elapsed_s=elapsed)
            else:target=np.asarray(plan.targets[self.index],float)
            if target.shape!=(16,) or not np.isfinite(target).all():
                self.revoke("invalid_joint_proposal")
                return None
            # Only the right arm/gripper may differ. No silent motion on the left.
            if not np.array_equal(target[:7],np.asarray(held16)[:7]) or target[14]!=held16[14]:
                self.revoke("inactive_arm_proposal")
                return None
            self.current_target=target
            return target.copy()
        if self.future is None:
            with self.engine._lock:
                intent=self.engine.take_pending()
                observation=self.engine.observation
            if intent:
                if intent.verification_request_id or intent.skill in ('verify_grasp','inspect_in_hand','verify_placed','reobserve'):
                    self.verification_after_ns=(self.engine.verification_checkpoints[intent.verification_request_id]['after_ns']
                                                if intent.verification_request_id else now)
                    if intent.verification_request_id:
                        self.engine.store.event('harness_verification_continued',request_id=intent.request_id,
                            motion_request_id=intent.verification_request_id,skill=intent.skill,
                            epoch=intent.epoch,task_hash=intent.task_hash,
                            after_ns=self.verification_after_ns,motion_replayed=False)
                    self.verification_wait_attempts=0
                    self.future_kind,self.future_request='verify',intent.request_id
                    self.future=self.executor.submit(self.verifier,intent,self.verification_after_ns)
                    return None
                # Snapshot only. Planner performs geometry, IK and scene checks off-loop.
                self.future_kind,self.future_request="plan",intent.request_id
                self.proposal_started=False
                self.plan_observation=observation
                self.revalidation_proof=None
                self.revalidation_after_ns=None
                self.plan_full_reference,self.plan_held_reference=tuple(full22),tuple(held16)
                if (observation is None or observation.observation_id!=intent.observation_id
                        or observation.scene_version!=intent.scene_version
                        or observation.calibration_id!=intent.calibration_id):
                    self._discard_unpublished_plan('planning_observation_changed_before_dispatch')
                    return None
                # Persist the exact inputs before planning can fail. The existing
                # collector discovers raw RGB-D hashes in this event, including
                # zero-candidate failures which never produce a revalidation trace.
                self.engine.store.event('harness_planning_started',request_id=intent.request_id,
                    epoch=intent.epoch,intent=intent.model_dump(),observation=observation.model_dump(),
                    full_reference=self.plan_full_reference,held_reference=self.plan_held_reference)
                self.future=self.executor.submit(self.planner,intent,observation,
                    self.plan_full_reference,self.plan_held_reference)
        return None

    def reset_closed_owner_stop(self):
        self._closed_stop_scope=None
        self._closed_stop_anchor=None
        self._closed_stop_since=self._closed_stop_stamp=None

    def observe_closed_owner_stop(self, *, measured16, now_ns):
        """Read-only stop proof after native output endpoints have closed.

        This never creates a publication receipt or grants task authority.
        Only the fault-control hook may call it, after checking owner.prepared.
        """
        if self.engine.authorized or not self.engine.hold_requested:
            self.reset_closed_owner_stop()
            return False
        scope=(self.engine.instance,self.engine.session,self.engine.epoch)
        if getattr(self,'_closed_stop_scope',None)!=scope:
            self.reset_closed_owner_stop();self._closed_stop_scope=scope
        snapshot=self.feedback_snapshot
        stamps=snapshot['stamps16'] if snapshot else ()
        required=(*stamps[7:14],stamps[15]) if len(stamps)==16 else ()
        if (not required or any(s is None or not 0<=now_ns-s<=self.feedback_age_ns for s in required)):
            self.reset_closed_owner_stop();return False
        stamp=min(required)
        if self._closed_stop_stamp is not None and stamp<=self._closed_stop_stamp:return False
        measured=np.r_[np.asarray(measured16,float)[7:14]/self.stop_delta_deg,
                       float(measured16[15])/self.stop_gripper_delta]
        if not np.isfinite(measured).all():self.reset_closed_owner_stop();return False
        if (self._closed_stop_anchor is None or self._closed_stop_stamp is None
                or stamp-self._closed_stop_stamp>self.feedback_age_ns
                or np.max(np.abs(measured-self._closed_stop_anchor))>=1):
            self._closed_stop_anchor=measured.copy();self._closed_stop_since=stamp
        self._closed_stop_stamp=stamp
        if stamp-self._closed_stop_since<self.stop_window_ns:return False
        self.engine.acknowledge_closed_owner_stop(
            first_sample_ns=self._closed_stop_since,last_sample_ns=stamp)
        if self.web_hold_stage!='none':self.web_hold_stage='measured_stationary'
        return True

    def _record_grasp_close(self, plan, publication_id, now_ns):
        running = self.engine.running
        contact = self.engine.grasp_contact
        if (not running or running.skill not in ('grasp','regrasp')
                or contact and contact['request_id'] == plan.request_id
                or self.plan_observation is None or self.engine.task is None):
            return
        stage = next((s for s in plan.geometry_trace if s.get('semantic_stage') == 'grasp_close'
                      and s['start_index'] <= self.index < s['end_index']), None)
        if (not stage or not stage.get('goals') or stage.get('gripper') is None
                or self.current_target[15] != stage['gripper']):
            return
        obj = next((o for o in self.plan_observation.objects
                    if o.object_id == self.engine.task.object_id and o.pose is not None), None)
        if obj is None:
            return
        from .geometry import transform
        expected = np.linalg.inv(transform(stage['goals'][-1])) @ transform(obj.pose)
        self.engine.record_grasp_close_publication(plan.request_id,
            publication_id=publication_id, now_ns=now_ns, target_closure=stage['gripper'],
            expected_eef_object=expected.ravel().tolist())

    def after_publication(self, *, publication_id, measured16, publication_complete, now_ns):
        if not publication_complete:
            self.revoke("owner_publication_incomplete")
            return
        if self.engine.hold_requested:
            # Stationary evidence requires repeated measurements over the configured window.
            measured=np.r_[np.asarray(measured16,float)[7:14]/self.stop_delta_deg,float(measured16[15])/self.stop_gripper_delta]
            stamps=self.feedback_snapshot['stamps16'] if self.feedback_snapshot else ()
            required=(*stamps[7:14],stamps[15]) if len(stamps)==16 else ()
            stamp=min(required) if required and all(s is not None for s in required) else None
            fresh=stamp is not None and 0<=now_ns-stamp<=self.feedback_age_ns
            advancing=fresh and (self.hold_last_stamp is None or stamp>self.hold_last_stamp)
            if not fresh:
                self.hold_anchor=None;self.settled_since=None
            elif advancing:
                if self.hold_anchor is None or np.max(np.abs(measured-self.hold_anchor))>=1:
                    self.hold_anchor=measured.copy();self.settled_since=stamp
                self.hold_last_stamp=stamp
            stopped=bool(advancing and self.settled_since is not None and stamp-self.settled_since>=self.stop_window_ns)
            try:self.engine.acknowledge_hold(publication_id=publication_id,measured_stationary=stopped)
            finally:
                if self.web_hold_stage!='none':
                    self.web_hold_stage='measured_stationary' if stopped else 'owner_hold_committed'
            return
        if self.proposal is None or self.current_target is None:
            return
        plan=self.proposal
        if not self.engine.authorized or plan.epoch!=self.engine.epoch:
            self.proposal=self.current_target=None
            self.waypoint_published_ns=None
            return
        self.proposal_started=True
        self.engine.record_skill_publication(plan.request_id,publication_id=publication_id,now_ns=now_ns)
        self._record_grasp_close(plan, publication_id, now_ns)
        stamps=self.feedback_snapshot['stamps16'] if self.feedback_snapshot else ()
        if len(stamps)!=16 or any(s is None or not 0<=now_ns-s<=self.feedback_age_ns for s in (*stamps[7:14],stamps[15])):
            self.revoke('waypoint_feedback_stale')
            return
        if plan.gripper_tolerance is None or plan.gripper_tolerance<=0:
            self.revoke('gripper_arrival_tolerance_missing')
            return
        if self.waypoint_published_ns is None:self.waypoint_published_ns=now_ns
        measured_stamp=min((*stamps[7:14],stamps[15]))
        running=self.engine.running
        lift=next((s for s in plan.geometry_trace if s.get('semantic_stage') in ('grasp_lift','grasp_observation')
                   and s['start_index']<=self.index<s['end_index']),None)
        if running and running.skill in ('grasp','regrasp','recover_grasp') and lift:
            monitor=getattr(self,'grasp_lift_monitor',None)
            if monitor is None or monitor['request_id']!=plan.request_id:
                monitor=dict(request_id=plan.request_id,started_ns=now_ns,confirmed=False)
                self.grasp_lift_monitor=monitor
            from .grasp_execution import lift_evidence
            live=getattr(self.engine,'live_observation',None)
            verdict=lift_evidence(live,object_id=self.engine.task.object_id,
                calibration_id=plan.calibration_id,after_ns=monitor['started_ns'],
                gripper=self.current_target[15],gripper_tolerance=plan.gripper_tolerance)
            if verdict is True and not monitor['confirmed']:
                monitor['confirmed']=True
                self.engine.store.event('harness_grasp_following_during_lift',request_id=plan.request_id,
                    observation_id=live.observation_id,index=self.index,elapsed_ns=now_ns-monitor['started_ns'])
            elif verdict is False:
                self.engine.complete_grasp_miss(now_ns=now_ns,observation_id=live.observation_id)
                if self.arrival_future:self.arrival_future.cancel()
                self.arrival_future=self.arrival_check=None
                self.arrival_refresh=None
                self.proposal=self.current_target=None
                self.hold_anchor=self.hold_last_stamp=self.settled_since=None
                self.waypoint_published_ns=None
                return
        # A repeated pre-publication sample cannot confirm a stage boundary.
        if measured_stamp<=self.waypoint_published_ns:return
        if plan.trajectory is not None:
            if self.trajectory_start_ns is None:self.trajectory_start_ns=now_ns
            # Dense geometric samples are streamed through the existing owner;
            # feedback is fresh but is not a 0.3-degree per-sample release gate.
            if not self.trajectory_at_boundary:return
        error=np.max(np.abs(np.asarray(measured16)[7:14]-self.current_target[7:14]))
        gripper_error=abs(float(measured16[15])-float(self.current_target[15]))
        reached=plan.trajectory is None and error<=plan.tolerance_deg
        tracking=plan.free_space_tracking_tolerance_deg
        if (not reached and (tracking is not None or plan.trajectory is not None)
                and gripper_error<=plan.gripper_tolerance and self.waypoint_verifier is not None):
            key=(plan.request_id,plan.epoch,self.index)
            prior=self.arrival_evidence
            reached=bool(prior and prior['key']==key and prior['stamp']<=measured_stamp
                and np.max(np.abs(np.asarray(measured16)[7:14]-prior['measured'][7:14]))<=self.stop_delta_deg)
            if not reached:self.arrival_evidence=None
            if self.arrival_future is not None and self.arrival_future.done():
                future,self.arrival_future=self.arrival_future,None
                check,self.arrival_check=self.arrival_check,None
                try:proof=future.result()
                except Exception as exc:proof=dict(accepted=False,reason=type(exc).__name__)
                # Geometry is a predicate on a pose, not a feedback timestamp.
                # Fresh live feedback was checked above. A slow computation may
                # qualify only the same still-equivalent measured waypoint;
                # never rewrite feedback time or extend the active request.
                same_context=bool(check and check['key']==key and check['stamp']<=measured_stamp)
                reached=bool(same_context and proof.get('accepted') is True
                    and np.max(np.abs(np.asarray(measured16)[7:14]-check['measured'][7:14]))<=self.stop_delta_deg)
                self.arrival_evidence=check if reached else None
                self.engine.store.event('harness_waypoint_tracking_check',request_id=plan.request_id,
                    index=self.index,error_deg=float(error),accepted=reached,proof=proof)
                # A valid proof can expire while the arm is still moving. Use
                # cheap feedback to advance its retry once motion slows; eager
                # geometry on every tick wastes work on soon-expired poses.
                self.arrival_refresh=(dict(key=key,stamp=measured_stamp,
                    measured=np.asarray(measured16,float).copy())
                    if same_context and proof.get('accepted') is True and not reached else None)
                self.arrival_retry_ns=now_ns+100_000_000
            refresh=self.arrival_refresh
            if refresh is not None:
                if reached or refresh['key']!=key:
                    self.arrival_refresh=None
                elif measured_stamp>refresh['stamp']:
                    if np.max(np.abs(np.asarray(measured16)[7:14]-refresh['measured'][7:14]))<=self.stop_delta_deg:
                        self.arrival_retry_ns=min(self.arrival_retry_ns,now_ns)
                        self.arrival_refresh=None
                    else:
                        refresh.update(stamp=measured_stamp,measured=np.asarray(measured16,float).copy())
            if not reached and self.arrival_future is None and now_ns>=self.arrival_retry_ns:
                self.arrival_refresh=None
                snapshot=self.feedback_snapshot
                measured=np.asarray(measured16,float).copy()
                self.arrival_check=dict(key=key,measured=measured,stamp=measured_stamp)
                self.arrival_future=self.executor.submit(self.waypoint_verifier,self.engine.running,
                    plan,self.plan_observation,self.index,snapshot['full22'],tuple(measured))
        if not reached or gripper_error>plan.gripper_tolerance:
            if now_ns-self.waypoint_wait_logged_ns>=1_000_000_000:
                self.waypoint_wait_logged_ns=now_ns
                self.engine.store.event('harness_waypoint_waiting',request_id=plan.request_id,index=self.index,
                    error_deg=float(error),gripper_error=gripper_error,
                    strict_tolerance_deg=plan.tolerance_deg,
                    task_space_verification_enabled=tracking is not None or plan.trajectory is not None,
                    remaining_ms=max(0,(self.engine.deadline_ns-now_ns)/1e6))
            self.settled_since=None
            key = (plan.request_id, plan.epoch, self.index)
            measured_progress = np.r_[np.asarray(measured16)[7:14] / self.stop_delta_deg,
                                      measured16[15] / self.stop_gripper_delta]
            if (self.progress_key != key or self.progress_anchor is None
                    or np.max(np.abs(measured_progress - self.progress_anchor)) >= 1):
                self.progress_key, self.progress_anchor = key, measured_progress.copy()
                self.progress_changed_ns = measured_stamp
            elif measured_stamp - self.progress_changed_ns >= 2_000_000_000:
                from .trajectory import preclosure_checkpoint
                empty=bool(self.plan_observation and any(o.object_id==self.engine.task.object_id and o.held is False
                    for o in self.plan_observation.objects))
                preclosure=(self.engine.running.skill=='grasp' and empty
                    and preclosure_checkpoint(plan,self.index,measured16,self.plan_held_reference))
                view=plan.geometry_trace[0].get('observation_view') if plan.geometry_trace else None
                self.engine.complete_stalled_motion(index=self.index, measured16=measured16, now_ns=now_ns,
                                                   preclosure=preclosure,observation_view=view)
                if self.arrival_future:
                    self.arrival_future.cancel()
                self.arrival_future = self.arrival_check = None
                self.arrival_refresh=None
                self.proposal = self.current_target = None
                self.hold_anchor = self.hold_last_stamp = None
                self.waypoint_published_ns = None
            return
        previous=self.plan_held_reference if self.index==0 else plan.targets[self.index-1]
        self.progress_key=self.progress_anchor=None
        final=self.index==len(plan.targets)-1
        gripper_boundary=(previous is None or previous[15]!=self.current_target[15]
                          or not final and plan.targets[self.index+1][15]!=self.current_target[15])
        settle_required=bool(final or gripper_boundary or plan.trajectory is not None)
        # Timed plans stop only at registered semantic/gripper boundaries.
        # Legacy plans retain their historical waypoint contract for replay.
        if settle_required:
            if self.settled_since is None:self.settled_since=measured_stamp
            if measured_stamp-self.settled_since < plan.settle_ns:return
        self.engine.store.event("harness_measured_waypoint",request_id=plan.request_id,
                                publication_id=publication_id,index=self.index,error_deg=float(error),
                                gripper_error=gripper_error,settle_required=settle_required,
                                measured_stamp_ns=int(measured_stamp),first_publication_ns=int(self.waypoint_published_ns))
        self.index+=1;self.settled_since=None
        self.arrival_evidence=None
        if plan.trajectory is not None:
            self.trajectory_start_index=self.index;self.trajectory_start_ns=None
        if self.arrival_future:self.arrival_future.cancel()
        self.arrival_future=self.arrival_check=None
        self.arrival_retry_ns=0
        self.arrival_refresh=None
        self.current_target=None;self.waypoint_published_ns=None
        if self.index<len(plan.targets):return
        # Verification is an independent asynchronous observation computation.
        # It must return a result tied to this request and fresh post-motion evidence.
        descriptor=plan.geometry_trace[0].get('grasp_candidate_descriptor') if plan.geometry_trace else None
        if self.engine.running and self.engine.running.skill=='approach_grasp' and descriptor is not None:
            self.engine.record_approach_arrival(request_id=plan.request_id,descriptor=descriptor,
                candidate_variant=plan.candidate_variant,observation=self.plan_observation,
                measured_at_ns=measured_stamp)
        self.proposal=self.current_target=None
        running=self.engine.running
        if running:
            self.verification_after_ns=now_ns
            self.verification_wait_attempts=0
            self.future=self.executor.submit(self.verifier,running,now_ns)
            self.future_kind,self.future_request="verify",running.request_id

    def close(self):
        self.revoke("owner_bridge_closed")
        if self.server:self.server.close()
        self.executor.shutdown(wait=False,cancel_futures=True)
