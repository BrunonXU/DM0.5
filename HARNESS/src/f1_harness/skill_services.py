"""Trusted geometry and visual postcondition services outside the controller and planner."""
from dataclasses import asdict
from pathlib import Path
from collections import OrderedDict
from copy import deepcopy
import threading
import time

from .contracts import HarnessError, Observation, SkillIntent, SkillResult, Task, digest
from .geometry import transform
from .ipc import UnixClient
from .observation import require_geometry
from .owner_bridge import JointProposal, numerical_failure_detail
from .skills import SkillGeometry, propose, stage_obstacles, tool_margin, rebase_support_lift
from .verification import verify, needs_semantic_evidence


CANDIDATE_FAILURES = frozenset((
    'numerical_joint_search_no_exact_path','numerical_joint_search_no_valid_goal',
    'numerical_joint_search_wall_time_exceeded','numerical_joint_search_path_rejected',
    'bounded_IK_did_not_reach_goal','bounded_IK_wall_time_exceeded',
    'actual_IK_candidate_collision_rejected','actual_IK_path_tool_sweep_blocked',
    'actual_IK_path_scene_sweep_blocked','actual_IK_path_attached_object_sweep_blocked',
    'scene_sweep_blocked','no_clear_square_symmetry_route','grasp_height_outside_registered_object',
    'grasp_has_no_placement_route','grasp_lift_sweep_blocked','pregrasp_target_outside_wrist_view',
    'return_route_sweep_blocked','grasp_continuation_unreachable',
    'regrasp_support_geometry_missing','regrasp_object_not_on_registered_support',
    'support_release_attachment_unresolved','support_release_contact_unavailable',
    'support_release_object_not_stationary','support_motion_requires_attachment_and_support',
    'actual_IK_uncertain_grasp_departure_not_proven'))


class PlanService:
    posture_prior = None
    def __init__(self,task:Task,fixture,numerical_socket:Path,*,clock=time.monotonic_ns):
        from .frames import FrameChain
        self.task,self.fixture,self.clock=task,fixture,clock
        from .candidate_failures import CandidateFailures
        self.candidate_failures=CandidateFailures()
        self.planned_candidates=OrderedDict()
        self.planned_candidates_lock=threading.Lock()
        if type(fixture.get('native_grasp_lookahead',False)) is not bool:
            raise ValueError('native_grasp_lookahead_boolean_required')
        if type(fixture.get('joint_space_transport',False)) is not bool:
            raise ValueError('joint_space_transport_boolean_required')
        if type(fixture.get('joint_space_free_motion',False)) is not bool:
            raise ValueError('joint_space_free_motion_boolean_required')
        self.frames=FrameChain.load(fixture.get('frame_chain'))
        if self.frames.native_frame!=fixture['native_frame']:raise ValueError('fixture_frame_identity_mismatch')
        from .numerical_pool import NUMERICAL_RPC_TIMEOUT_S
        self.numerics=UnixClient(numerical_socket,
            timeout=NUMERICAL_RPC_TIMEOUT_S if fixture.get('joint_space_transport') is True
                or fixture.get('joint_space_free_motion') is True else 8)
        if fixture.get('execution_mode') not in (None,'timed_reference'):
            raise ValueError('invalid_execution_mode')
        if fixture.get('execution_mode')=='timed_reference' or 'reference_velocity_by_skill_deg_s' in fixture:
            from .trajectory import reference_speed
            reference_speed(fixture,None)
        if 'withdraw_reset' in task.allowed_skills:
            from .return_scene import validate_registration
            validate_registration(fixture)
        self.posture_prior=fixture.get('success_posture_prior')
        if fixture.get('transfer_policy','hole_aligned') not in ('hole_aligned','registered_transport'):
            raise ValueError('unregistered_transfer_policy')
        from .skills import validate_transfer_lift_fraction
        validate_transfer_lift_fraction(fixture.get('transfer_lift_rotation_fraction',0.),
            staged=fixture.get('transfer_staging_pose') is not None)
        if fixture.get('success_trajectory') is not None:
            from .demonstration import validate_template
            validate_template(fixture['success_trajectory'])
        if fixture.get('redundancy_preference') not in (None,'wrist_center_nullspace'):
            raise ValueError('invalid_redundancy_preference')
        if self.posture_prior is not None:
            import numpy as np
            prior=self.posture_prior
            if (prior.get('schema')!='f1-success-posture-prior-v1' or not prior.get('source_run')
                    or not prior.get('source_sha256') or not prior.get('postures_deg')):
                raise ValueError('success_posture_provenance_required')
            for name,values in prior['postures_deg'].items():
                data=np.asarray(values,float)
                if name not in ('approach_grasp','grasp','regrasp','inspect_in_hand','transfer_above_hole','align','insert','release','retreat') or data.shape!=(7,) or not np.all(np.isfinite(data)):
                    raise ValueError('invalid_success_posture_prior')

    def bind_observation_view(self,plan,intent,obs,scene):
        # The original right-wrist view can seed reacquisition if the
        # object disappears on approach. It is a pose proposal only.
        if intent.skill=='approach_grasp' and plan.get('geometry_trace'):
            entry=plan['geometry_trace'][0]
            entry.pop('observation_view',None)
            if (obs.geometry_camera=='right' and self.fixture.get('cartesian_motion') and scene.eef_tip_reference is not None
                    and {'move_tcp','reobserve'} <= set(self.task.allowed_skills)
                    and any(o.object_id==self.task.object_id and o.visible and o.pose is not None
                            and o.evidence_ids for o in obs.objects)):
                import numpy as np
                tcp=np.linalg.inv(self.frames.world_native)@scene.base_eef@transform(scene.eef_tip_reference)
                entry['observation_view']=dict(observation_id=obs.observation_id,
                    world_frame=self.frames.native_frame,target_frame='world',target_tcp_pose=tcp.ravel().tolist())
        return plan

    def dispatch(self,method,kwargs):
        if method=='geometry_context':
            obs=Observation.model_validate(kwargs['observation'])
            import numpy as np
            from .geometry_context import entity_status, POSE_FORMAT, tool_geometry
            from .support_motion import scene_support
            status=entity_status(self.task,obs)
            status['fixture_transit_available']=status['fixture_transit_available'] and self.fixture.get('independent_fixture_localization',False)
            if obs.calibration_id!=self.task.calibration_id or obs.faults:
                raise HarnessError('geometry_context_identity_or_fault')
            # The read-only scene and optional coarse alternatives describe the
            # same held feedback sample. Compute FK once, within the outer read.
            def measured_eef():
                from .ipc import GEOMETRY_FK_TIMEOUT_S
                client=self.numerics
                if isinstance(client,UnixClient):
                    client=UnixClient(Path(client.path),timeout=GEOMETRY_FK_TIMEOUT_S)
                return client.call('forward_pose',full22=kwargs['full22'],held16=kwargs['held16'])
            from .local_environment import local_scope
            if local_scope(obs):
                from .local_environment import build_local_scene,context
                scene=build_local_scene(self,obs,kwargs['full22'],kwargs['held16'],base_eef=measured_eef(),motion=False)
                return context(scene,obs,task=self.task,fixture=self.fixture)
            from .observation import require_geometry_identity
            available=[]
            for skill in self.task.allowed_skills:
                if skill=='move_tcp' and not self.fixture.get('cartesian_motion'):continue
                try:
                    require_geometry_identity(obs,goal=self.task.goal,admission_profile=self.task.admission_profile,
                                              skill=skill,hole_id=self.task.hole_id)
                except ValueError:continue
                available.append(skill)
            status.update(geometry_scope=obs.geometry_scope,available_skills=available,
                          cartesian_motion='move_tcp' in available,motion_authorized=False)
            if status['missing_entities'] and not (status['fixture_transit_available'] and
                    all(row['role']=='aperture' for row in status['missing_entities']) and
                    self.fixture.get('independent_fixture_localization',False)):
                # Keep good target evidence when another entity is missing.
                # Do not fabricate a bucket pose from the old fixture reference.
                poses={o.object_id:o for o in obs.objects if o.pose is not None
                       and o.object_id in (self.task.object_id,self.task.hole_id)}
                if any(o.frame_id!=self.frames.native_frame for o in poses.values()):
                    raise HarnessError('native_frame_chain_not_registered')
                eef=transform(measured_eef())
                tip=transform(self.fixture['eef_tip_reference'])
                native=np.linalg.inv(self.frames.world_native)
                return dict(schema='f1-geometric-tools-v1',world_frame=self.frames.native_frame,pose_format=POSE_FORMAT,
                    current_tcp_pose=(native@eef@tip).ravel().tolist(),
                    object_pose=list(poses[self.task.object_id].pose) if self.task.object_id in poses else None,
                    hole_pose=list(poses[self.task.hole_id].pose) if self.task.hole_id in poses else None,
                    obstacles=None,obstacle_frame_note='Unavailable collision geometry is not empty space',
                    support=scene_support(self,obs),
                    observation_id=obs.observation_id,calibration_id=obs.calibration_id,
                    **status)
            base_eef=measured_eef()
            scene=self._scene(obs,kwargs['full22'],kwargs['held16'],base_eef=base_eef)
            suggestions=None
            if self.task.shape=='square' and scene.parameterized_grasp and scene.object_held is not True:
                from .grasp_suggestions import suggest_grasps
                try:
                    coarse=(self._scene(obs,kwargs['full22'],kwargs['held16'],base_eef=base_eef,skill='approach_grasp')
                            if self.fixture.get('independent_fixture_localization') else scene)
                    suggestions=suggest_grasps(coarse,placement_scene=scene if scene.aperture_measured else None,
                        prior=self.posture_prior,camera=self.fixture.get('candidate_wrist_camera'))
                except HarnessError as exc:
                    suggestions=dict(status='unavailable',reason=str(exc).split(':',1)[0],
                        candidates=[],motion_authorized=False,numerical_feasibility_checked=False,
                        exhaustive=False,continuous_proposals_allowed=True)
            native=np.linalg.inv(self.frames.world_native)
            tip=transform(scene.eef_tip_reference)
            obstacles=[]
            for obstacle in scene.obstacles:
                if obstacle.get('type')=='halfspace':
                    obstacles.append(dict(obstacle,point=(native[:3,:3]@np.asarray(obstacle['point'])+native[:3,3]).tolist(),
                        normal=(native[:3,:3]@np.asarray(obstacle['normal'])).tolist()))
                elif 'pose' in obstacle:obstacles.append(dict(obstacle,pose=(native@transform(obstacle['pose'])).tolist()))
                else:obstacles.append(dict(obstacle,pose=native.tolist()))
            return dict(schema='f1-geometric-tools-v1',world_frame=self.frames.native_frame,pose_format=POSE_FORMAT,
                current_tcp_pose=(native@scene.base_eef@tip).ravel().tolist(),
                current_tcp_pose_object=(np.linalg.inv(scene.base_object)@scene.base_eef@tip).ravel().tolist(),
                nominal_grasp_tcp_pose=(scene.object_grasp_eef@tip).ravel().tolist(),
                object_pose=(native@scene.base_object).ravel().tolist(),
                hole_pose=None if not scene.aperture_measured or scene.base_hole is None else (native@scene.base_hole).ravel().tolist(),
                fixture_transport_reference_pose=(None if obs.fixture is None else list(obs.fixture.pose)),
                object_local_box=scene.object_local_box,object_held=scene.object_held,
                support=scene.support_measurement,
                tool_geometry=tool_geometry(scene),
                grasp_suggestions=suggestions,
                obstacles=obstacles,
                obstacle_frame_note='All obstacle poses and halfspaces use native world',
                approach_clearance_m=scene.approach_clearance_m,
                continuous_grasp=scene.parameterized_grasp,
                observation_id=obs.observation_id,calibration_id=obs.calibration_id,**status)
        if method=='check_waypoint':return self.check_waypoint(**kwargs)
        if method=='revalidate_plan':
            from .revalidation import revalidate_candidate
            return revalidate_candidate(self,**kwargs)
        if method!='plan_skill':raise HarnessError('plan_method_unavailable')
        preview=kwargs.get('preview',False)
        if type(preview) is not bool:raise HarnessError('invalid_plan_preview_flag')
        intent=SkillIntent.model_validate(kwargs['intent'])
        obs=Observation.model_validate(kwargs['observation'])
        from .revalidation import require_planning_geometry
        require_planning_geometry(obs,intent=intent,task=self.task,now_ns=self.clock(),max_age_ns=500_000_000,
            candidate_only=(self.task.goal=='placed' and self.task.admission_profile=='development_coarse'
                            and self.fixture.get('allow_stale_planning_candidate') is True))
        started=self.clock()
        deadline_ns=started+intent.budget_ms*1_000_000
        held=kwargs['held16'];full=kwargs['full22']
        scene=self._scene(obs,full,held,skill=intent.skill)
        from .motion_goals import apply_request, grasp_descriptor
        scene=apply_request(self,scene,intent)
        selection=kwargs.get('grasp_selection')
        if selection is not None and intent.skill=='grasp':
            from .grasp_selection import selected_scene
            scene=selected_scene(self,scene,obs,selection,intent)
        elif intent.skill=='grasp' and (scene.grasp_pitch_offsets_deg or scene.parameterized_grasp):
            raise HarnessError('completed_approach_grasp_selection_required')
        # A preview is never executable authority, but its deterministic
        # numerical calculation need not be repeated for identical physical
        # inputs. Current admission above and fresh owner revalidation below
        # remain mandatory. No pose/feedback rounding or successful execution
        # state is stored in this cache.
        from .candidate_failures import candidate_key
        physical_key=candidate_key(self.task,self.fixture,intent,obs,scene,held,full,None,computation_only=True)
        cache_key=(digest(dict(physical=physical_key,prior=self.posture_prior,
            selection=selection,candidate_start=intent.candidate_start,
            excluded_candidates=intent.excluded_candidates)) if physical_key else None)
        if not hasattr(self,'planned_candidates_lock'):
            self.planned_candidates=OrderedDict();self.planned_candidates_lock=threading.Lock()
        # A successful preview is an unpublished path, not an exact floating
        # point memoization key. Reuse it once for the same free-space request;
        # current admission, reconstructed goals and owner FK/native sweep
        # still decide whether its actual joints may be published.
        from .revalidation import preview_submission_key, rebind_preview_candidate
        submission_key=preview_submission_key(self.task,self.fixture,intent,obs)
        with self.planned_candidates_lock:
            if not hasattr(self,'preview_candidates'):self.preview_candidates=OrderedDict()
            submitted=(self.preview_candidates.pop(submission_key,None)
                       if submission_key and not preview else None)
            if submitted is not None:
                self.planned_candidates.pop(submitted['cache_key'],None)
        if submitted is not None:
            candidate=rebind_preview_candidate(self,intent,obs,scene,full,held,submitted)
            if candidate is not None:return self.bind_observation_view(candidate,intent,obs,scene)
        with self.planned_candidates_lock:
            cached=deepcopy(self.planned_candidates.get(cache_key)) if cache_key else None
        if cached is not None:
            cached.update(request_id=intent.request_id,epoch=intent.epoch,
                scene_version=intent.scene_version,calibration_id=intent.calibration_id)
            if cached.get('geometry_trace'):
                entry=cached['geometry_trace'][0]
                entry['reused_numerical_candidate']=dict(physical_input_sha256=cache_key,
                    source_request_id=entry.get('candidate_evaluation',{}).get('request_id'),
                    fresh_revalidation_required=True,motion_authorized=False)
                if entry.get('candidate_evaluation'):
                    entry['candidate_evaluation'].update(request_id=intent.request_id,
                        observation_id=obs.observation_id,epoch=intent.epoch)
            if preview and submission_key:
                with self.planned_candidates_lock:
                    self.preview_candidates[submission_key]=dict(candidate=deepcopy(cached),
                        cache_key=cache_key,observation_id=obs.observation_id,
                        source_request_id=intent.request_id,held16=list(held))
                    self.preview_candidates.move_to_end(submission_key)
                    while len(self.preview_candidates)>16:self.preview_candidates.popitem(last=False)
            return self.bind_observation_view(cached,intent,obs,scene)
        def completed(result):
            self.bind_observation_view(result,intent,obs,scene)
            if cache_key:
                with self.planned_candidates_lock:
                    self.planned_candidates[cache_key]=deepcopy(result)
                    self.planned_candidates.move_to_end(cache_key)
                    while len(self.planned_candidates)>16:self.planned_candidates.popitem(last=False)
            if preview and submission_key:
                with self.planned_candidates_lock:
                    self.preview_candidates[submission_key]=dict(candidate=deepcopy(result),
                        cache_key=cache_key,observation_id=obs.observation_id,
                        source_request_id=intent.request_id,held16=list(held))
                    self.preview_candidates.move_to_end(submission_key)
                    while len(self.preview_candidates)>16:self.preview_candidates.popitem(last=False)
            return result
        development=self.task.goal=='approach_grasp'
        coarse=self.task.admission_profile=='development_coarse'
        from .skills import candidate_variants
        rejected=[]
        variants=range(1) if development else candidate_variants(intent.skill)
        variants=[v for v in variants if v>=intent.candidate_start and v not in intent.excluded_candidates]
        if intent.parameters.variant is not None:
            variants=[v for v in variants if v==intent.parameters.variant]
        from .candidate_ranking import order_variants
        variants,ranking=order_variants(intent.skill,scene,variants,self.posture_prior,
                                       camera=self.fixture.get('candidate_wrist_camera'),
                                       preferences=self.fixture.get('candidate_order'))
        if intent.skill=='align' and scene.transfer_policy=='registered_transport':
            from .transport import rank_alignment_feeds
            variants,ranking=rank_alignment_feeds(scene,variants,ranking)
        attempts=[];joint_transport_attempted=False
        def fail_with_attempts(detail,variant,error):
            import json
            # An unknown service/identity exception has no structured solver
            # detail. Keep its original cause instead of replacing it with
            # the uninformative class name (e.g. just "HarnessError").
            if detail['code']==type(error).__name__:raise error
            raise HarnessError(detail['code']+':'+json.dumps(dict(diagnostic=detail,
                candidates=[*rejected,dict(variant=variant,diagnostic=detail)]),separators=(',',':'))) from None
        from .grasp_candidates import descriptors,apply_descriptor
        choices=(None,)
        if getattr(scene,'grasp_candidate_descriptor',None) is not None:
            choices=(scene.grasp_candidate_descriptor,)
        elif intent.skill in ('approach_grasp','regrasp') and (scene.grasp_pitch_offsets_deg or scene.parameterized_grasp):
            choices=descriptors(scene)
        if intent.parameters.pitch_offset_deg is not None:
            if not scene.parameterized_grasp:
                raise HarnessError('parameterized_grasp_not_enabled')
            choices=(dict(strategy='tcp_pitch',pitch_offset_deg=intent.parameters.pitch_offset_deg),)
        explicit=grasp_descriptor(intent.parameters)
        if explicit is not None:
            choices=(explicit,)
            if intent.parameters.grasp_tcp_pose is not None:variants=[0]
        candidates=[(d,v) for d in choices for v in variants]
        if self.fixture.get('geometric_candidate_ranking') and intent.skill in ('approach_grasp','regrasp'):
            from .candidate_ranking import rank_grasp_choices
            candidates,ranking=rank_grasp_choices(scene,candidates,self.posture_prior,
                camera=self.fixture.get('candidate_wrist_camera'),skill=intent.skill)
        free_search_candidates=[]
        for descriptor,variant in candidates:
            if self.clock()-started>=intent.budget_ms*1_000_000:
                raise HarnessError('registered_candidate_planning_budget_exhausted')
            candidate_started=time.perf_counter_ns()
            failure_key=None
            try:
                candidate_scene=scene if descriptor is None else apply_descriptor(scene,descriptor)
                if descriptor is not None and intent.skill=='approach_grasp':
                    from dataclasses import replace
                    candidate_scene=replace(candidate_scene,grasp_candidate_variant=variant)
                from .candidate_failures import candidate_key
                failure_key=candidate_key(self.task,self.fixture,intent,obs,candidate_scene,held,full,variant)
                previous=self.candidate_failures.get(failure_key)
                if previous is not None:
                    import json
                    raise HarnessError('numerical_candidate_reused:'+json.dumps(dict(diagnostic=previous,
                        candidates=[dict(variant=item['variant'],diagnostic=dict(code=item['code'],metrics=item['metrics']))
                                    for item in previous.get('candidates',())])))
                result=self._solve_candidate(intent,candidate_scene,held,full,variant,deadline_ns=deadline_ns)
                if (intent.skill=='approach_grasp' and self.task.goal=='placed'
                        and self.fixture.get('native_grasp_lookahead',False)):
                    result['geometry_trace'][0]['native_grasp_lookahead']=self._grasp_continuation(
                        intent,candidate_scene,result,full,deadline_ns=deadline_ns,
                        transport_deadline_ns=(started+intent.budget_ms*1_000_000
                            if preview and self.fixture.get('native_transport_lookahead_on_preview') is True else None))
                if (intent.skill=='approach_grasp' and self.task.goal=='placed'
                        and self.fixture.get('grasp_route_lookahead') is True
                        and not candidate_scene.aperture_measured):
                    self._measured_placement_advisory(obs,full,held,candidate_scene,result)
                if selection is not None and intent.skill=='grasp' and result['geometry_trace']:
                    result['geometry_trace'][0]['grasp_selection']=selection
                attempts.append(dict(variant=variant,outcome='planned',
                    elapsed_ns=time.perf_counter_ns()-candidate_started,
                    grasp_candidate_descriptor=descriptor))
                for stage in result['geometry_trace']:
                    stage['candidate_ranking']=ranking
                if result['geometry_trace']:
                    from .planning_lessons import planning_scope
                    try:
                        result['geometry_trace'][0]['candidate_evaluation']=dict(
                            schema='f1-candidate-evaluation-v1',scope=planning_scope(self.task,self.fixture,intent.skill),
                            request_id=intent.request_id,epoch=intent.epoch,observation_id=obs.observation_id,
                            registered_variants=list(candidate_variants(intent.skill)),attempts=attempts,
                            selected_variant=variant,motion_success_inferred=False)
                    except (OSError,ValueError,TypeError) as diagnostic_error:
                        result['geometry_trace'][0]['candidate_evaluation_error']=type(diagnostic_error).__name__
                return completed(result)
            except HarnessError as exc:
                detail=numerical_failure_detail(exc)
                if descriptor is not None:
                    detail['metrics'].update(grasp_strategy=descriptor['strategy'],
                        grasp_pitch_offset_deg=descriptor.get('pitch_offset_deg'))
                if intent.skill=='transfer_above_hole' and scene.transfer_policy=='registered_transport':
                    from .transport import route_family
                    detail['metrics']['route_family']=route_family(variant)
                code=detail['code']
                if code not in CANDIDATE_FAILURES:
                    fail_with_attempts(detail,variant,exc)
                if not detail['metrics'].get('reused_failure'):
                    self.candidate_failures.remember(failure_key,detail,intent.request_id)
                route_failures=detail.get('candidates',())
                if route_failures:
                    rejected.extend(dict(variant=item['variant'],diagnostic=dict(code=item['code'],
                        metrics=item['metrics'])) for item in route_failures)
                else:rejected.append(dict(variant=variant,diagnostic=detail))
                attempts.append(dict(variant=variant,outcome='rejected',
                    elapsed_ns=time.perf_counter_ns()-candidate_started,diagnostic=detail))
                from .joint_motion import eligible, SEARCHABLE_FAILURES
                # A failed Cartesian connection does not exhaust joint space.
                # Preserve the first two ranked Cartesian attempts: the real
                # V32 inputs succeed on the second without needing OMPL. Then
                # search joint space with this request's remaining time rather
                # than spending every template budget or starting a new one.
                if (not joint_transport_attempted and len(attempts)>=2 and code in SEARCHABLE_FAILURES
                        and self.fixture.get('joint_space_transport') is True
                        and intent.skill=='transfer_above_hole' and scene.transfer_policy=='registered_transport'
                        and intent.parameters.variant is None and 2 in variants
                        and self.clock()<deadline_ns):
                    joint_transport_attempted=True;search_started=time.perf_counter_ns()
                    try:
                        result=self._solve_candidate(intent,scene,held,full,2,joint_search=True,deadline_ns=deadline_ns)
                        attempts.append(dict(variant=2,outcome='planned',route_family='joint_space_transport',
                            elapsed_ns=time.perf_counter_ns()-search_started))
                        result['geometry_trace'][0]['preceding_candidate_failures']=deepcopy(rejected)
                        from .planning_lessons import planning_scope
                        try:
                            result['geometry_trace'][0]['candidate_evaluation']=dict(
                                schema='f1-candidate-evaluation-v1',scope=planning_scope(self.task,self.fixture,intent.skill),
                                request_id=intent.request_id,epoch=intent.epoch,observation_id=obs.observation_id,
                                registered_variants=list(candidate_variants(intent.skill)),attempts=attempts,
                                selected_variant=2,motion_success_inferred=False)
                        except (OSError,ValueError,TypeError) as diagnostic_error:
                            result['geometry_trace'][0]['candidate_evaluation_error']=type(diagnostic_error).__name__
                        return completed(result)
                    except HarnessError as search_error:
                        search_detail=numerical_failure_detail(search_error)
                        if search_detail['code'] not in CANDIDATE_FAILURES:fail_with_attempts(search_detail,2,search_error)
                        rejected.append(dict(variant=2,diagnostic=search_detail))
                        attempts.append(dict(variant=2,outcome='rejected',route_family='joint_space_transport',
                            elapsed_ns=time.perf_counter_ns()-search_started,diagnostic=search_detail))
                if (code in SEARCHABLE_FAILURES and eligible(self.fixture,intent.skill,candidate_scene)
                        and detail['metrics'].get('end_pose_clear') is not False):
                    free_search_candidates.append((candidate_scene,variant))
        # Failed Cartesian connections do not exhaust continuous joint space.
        # Keep the model's exact goal/grasp and use the same numerical backend,
        # owner and request; no synthetic via points or task-specific route.
        for candidate_scene,variant in free_search_candidates:
            if self.clock()>=deadline_ns:
                break
            try:
                result=self._solve_candidate(intent,candidate_scene,held,full,variant,joint_search=True,deadline_ns=deadline_ns)
                if intent.skill=='approach_grasp' and self.fixture.get('native_grasp_lookahead',False):
                    result['geometry_trace'][0]['native_grasp_lookahead']=self._grasp_continuation(
                        intent,candidate_scene,result,full,deadline_ns=deadline_ns,
                        transport_deadline_ns=started+intent.budget_ms*1_000_000 if preview else None)
                result['geometry_trace'][0]['preceding_candidate_failures']=rejected
                for stage in result['geometry_trace']:
                    stage['candidate_ranking']=ranking
                return completed(result)
            except HarnessError as exc:
                detail=numerical_failure_detail(exc)
                if detail['code'] not in CANDIDATE_FAILURES:fail_with_attempts(detail,variant,exc)
                rejected.append(dict(variant=variant,diagnostic=detail))
        # Optional implementation of the same transport primitive. Existing
        # explicit route requests/exclusions retain their meaning; no extra
        # model call, action budget, output owner or recovery scheduler.
        if (not joint_transport_attempted and self.fixture.get('joint_space_transport') is True
                and intent.skill=='transfer_above_hole' and scene.transfer_policy=='registered_transport'
                and intent.parameters.variant is None and 2 in variants
                and self.clock()<deadline_ns):
            try:
                result=self._solve_candidate(intent,scene,held,full,2,joint_search=True,deadline_ns=deadline_ns)
                result['geometry_trace'][0]['preceding_candidate_failures']=rejected
                return completed(result)
            except HarnessError as exc:
                detail=numerical_failure_detail(exc)
                if detail['code'] not in CANDIDATE_FAILURES:fail_with_attempts(detail,2,exc)
                rejected.append(dict(variant=2,diagnostic=detail))
        import json
        raise HarnessError('registered_candidates_exhausted:'+json.dumps(dict(
            diagnostic=dict(code='registered_candidates_exhausted',metrics=rejected[-1]['diagnostic']['metrics'] if rejected else {}),
            candidates=rejected),separators=(',',':')))

    def _measured_placement_advisory(self,obs,full,held,scene,result):
        """Use an available measured hole for advice without changing coarse motion.

        Coarse transit deliberately uses the bucket frame. That choice must not
        be confused with absence of an observed aperture. A future attachment
        prediction never vetoes the current approach or authorizes placement.
        """
        hole=next((o for o in obs.objects if o.object_id==self.task.hole_id),None)
        if hole is None or hole.pose is None:return
        from .skills import grasp_route_lookahead
        try:
            precise=self._scene(obs,full,held,base_eef=scene.base_eef,skill='align')
            advice=grasp_route_lookahead(scene,dict(kind='motion',skill='approach_grasp',
                stages=result['geometry_trace']),placement_scene=precise)
        except HarnessError as exc:
            detail=numerical_failure_detail(exc)
            if detail['code'] in CANDIDATE_FAILURES:
                advice=dict(predicted_attachment_only=True,numerical_feasibility_checked=False,
                    precision_placement_preview=dict(advisory_only=True,motion_authorized=False,
                        candidates=[dict(outcome='advisory',blocked_skill='placement_lookahead',
                                         diagnostic=detail)]))
            else:advice=dict(checked=False,reason='placement_prediction_unavailable',diagnostic=detail)
        result['geometry_trace'][0]['grasp_route_lookahead']=advice

    def _grasp_continuation(self,intent,scene,approach,full,*,transport_deadline_ns=None,deadline_ns=None):
        """Check contact/lift from predicted arrival; never submit future motion.

        A reachable pregrasp is not evidence that the selected grasp is reachable.
        This uses the same native planner, selected geometry and collision checks.
        Actual grasp is still reconstructed from new measurements by the owner.
        """
        from dataclasses import replace
        from .contracts import MotionParameters
        held=approach['targets'][-1]
        endpoint=self.numerics.call('forward_pose',full22=full,held16=held)
        following=intent.model_copy(update={'skill':'grasp','parameters':MotionParameters()})
        try:
            grasp=self._solve_candidate(following,replace(scene,base_eef=transform(endpoint)),held,full,0,
                                        deadline_ns=deadline_ns)
        except HarnessError as exc:
            import json
            detail=numerical_failure_detail(exc)
            if detail['code']=='planning_budget_exhausted':
                return dict(checked=False,feasible=None,advisory_only=True,
                    source='predicted_approach_endpoint',skill='grasp',motion_authorized=False,
                    actual_grasp_requires_new_measurements=True,diagnostic=detail)
            if detail['code'] not in CANDIDATE_FAILURES:raise
            return dict(checked=True,feasible=False,advisory_only=True,
                source='predicted_approach_endpoint',skill='grasp',motion_authorized=False,
                actual_grasp_requires_new_measurements=True,diagnostic=detail)
        proof=dict(checked=True,source='predicted_approach_endpoint',skill='grasp',
            feasible=True,advisory_only=True,target_count=len(grasp['targets']),motion_authorized=False,
            actual_grasp_requires_new_measurements=True)
        if transport_deadline_ns is not None:
            from .transport_advisory import native_transport_advisory
            proof['transport']=native_transport_advisory(self,intent,scene,grasp,full,transport_deadline_ns)
        return proof

    def _solve_candidate(self,intent,scene,held,full,variant,*,joint_search=False,deadline_ns=None):
        # A demonstrated bend is a fallback for the same grasp endpoint, not
        # evidence that its direct connection is impossible.
        from dataclasses import replace
        if intent.skill!='approach_grasp' or joint_search:
            return self._solve_route(intent,scene,held,full,variant,joint_search=joint_search,deadline_ns=deadline_ns)
        failures=[]
        for route in ('direct','historical'):
            try:
                result=self._solve_route(intent,replace(scene,approach_route=route),held,full,variant,deadline_ns=deadline_ns)
                result['geometry_trace'][0]['preceding_route_failures']=failures
                return result
            except HarnessError as exc:
                detail=numerical_failure_detail(exc)
                if detail['code'] not in CANDIDATE_FAILURES:raise
                detail['metrics']['route_family']=route+'_'+intent.skill
                failures.append(dict(route=route,diagnostic=detail))
                if route=='historical':
                    import json
                    raise HarnessError(detail['code']+':'+json.dumps(dict(diagnostic=detail,
                        candidates=[dict(variant=variant,diagnostic=item['diagnostic']) for item in failures]))) from exc

    def _solve_route(self,intent,scene,held,full,variant,*,joint_search=False,deadline_ns=None):
        import numpy as np
        anchor=tuple(held)
        if intent.skill=='recover_grasp':
            from .grasp_observation import require_closed
            require_closed(self.fixture,held)
        development=self.task.goal=='approach_grasp'
        coarse=self.task.admission_profile=='development_coarse'
        if joint_search:
            if intent.skill=='transfer_above_hole':
                if self.fixture.get('joint_space_transport') is not True or variant!=2:
                    raise HarnessError('numerical_joint_transport_not_enabled')
                from .transport import joint_transport_goal
                proposal=joint_transport_goal(scene)
            else:
                from .joint_motion import eligible,propose_joint_motion
                if not eligible(self.fixture,intent.skill,scene):
                    raise HarnessError('numerical_joint_free_motion_not_enabled')
                proposal=propose_joint_motion(scene,intent.skill,variant=variant)
        else:
            proposal=propose(intent.skill,scene,variant=variant)
        view=None
        if intent.skill=='approach_grasp' and self.fixture.get('candidate_wrist_camera') is not None:
            from .candidate_ranking import pregrasp_wrist_view
            view=pregrasp_wrist_view(scene,proposal['stages'][-1]['goals'][-1],
                                    self.fixture.get('candidate_wrist_camera'))
            if self.fixture.get('require_pregrasp_wrist_view') is True and not view['inside_image']:
                raise HarnessError('pregrasp_target_outside_wrist_view')
        lookahead=None
        if intent.skill in ('approach_grasp','grasp','regrasp') and self.fixture.get('grasp_route_lookahead') is True:
            from .skills import grasp_route_lookahead
            try:
                lookahead=(grasp_route_lookahead(scene,proposal) if scene.aperture_measured else
                    dict(checked=False,reason='precise_aperture_pending',transport_geometry_available=True))
            except HarnessError as exc:
                detail=numerical_failure_detail(exc)
                if detail['code'] not in CANDIDATE_FAILURES:raise
                lookahead=dict(checked=True,advisory_only=True,feasible=False,
                    predicted_attachment_only=True,motion_authorized=False,diagnostic=detail)
        targets=[held] if proposal['kind']=='verify' else []
        geometry_trace=[]
        for stage_index,stage in enumerate(proposal.get('stages',[])):
            if stage.get('attachment_basis')=='predicted_for_collision_only':
                contact=self.numerics.call('forward_pose',full22=full,held16=held)
                stage=rebase_support_lift(stage,contact,proposal['stages'][stage_index-1]['goals'][-1],scene.base_object)
            bounds=dict(attached_radius_m=scene.attached_radius_m,
                        obstacles=stage_obstacles(scene,allow_target_contact=stage.get('allow_target_contact',False)),
                        uncertainty_m=tool_margin(scene))
            from .joint_motion import is_search_stage
            if joint_search and is_search_stage(stage):
                bounds.update(joint_space_search=True,free_space_motion=True)
            redundancy=self.fixture.get('redundancy_preference')
            if redundancy is not None and stage.get('approach_route')=='direct':
                stage={key:value for key,value in stage.items() if key!='posture_reference_policy'}
            if stage.get('posture_reference_policy')=='measured_stage_start':
                redundancy=None
                # Use the existing per-goal null-space posture path, just as
                # the saved-scene output audit. The single posture target has
                # different solver semantics and is not interchangeable.
                bounds['posture_references_deg']=[list(held[7:14]) for _ in stage['goals']]
            elif stage.get('posture_reference_policy') is not None:
                raise HarnessError('unregistered_posture_reference_policy')
            elif intent.skill=='withdraw_reset':
                redundancy=None
                bounds['posture_reference_deg']=list(scene.home_right_deg)
            elif stage.get('posture_references_deg') is not None:
                redundancy=None
                bounds['posture_references_deg']=stage['posture_references_deg']
            elif redundancy is not None:
                bounds['redundancy_preference']=redundancy
            elif self.posture_prior and intent.skill in self.posture_prior['postures_deg']:
                bounds['posture_reference_deg']=self.posture_prior['postures_deg'][intent.skill]
            if scene.tool_local_boxes:
                bounds.update(eef_tip_reference=scene.eef_tip_reference,tool_local_boxes=scene.tool_local_boxes)
            if stage.get('eef_object') is not None:
                bounds.update(eef_object=stage['eef_object'],object_local_box=scene.object_local_box,
                              object_planning_margin_m=scene.object_planning_margin_m if coarse else scene.uncertainty_m)
                if stage.get('support_contact_tolerance_m') is not None:
                    bounds['support_contact_tolerance_m']=stage['support_contact_tolerance_m']
                    bounds['support_contact_direction']=stage.get('support_contact_direction','depart')
            if development:
                bounds.update(eef_tip_reference=scene.eef_tip_reference,
                              maximum_approach_translation_m=scene.maximum_approach_translation_m,
                              maximum_approach_rotation_rad=.01,tool_local_boxes=scene.tool_local_boxes)
            try:
                if deadline_ns is not None:
                    remaining_s=(deadline_ns-self.clock())/1e9
                    if remaining_s<=0:raise HarnessError('registered_candidate_planning_budget_exhausted')
                    # One request budget covers every route and stage. The
                    # native backend may shorten its original 7/15 s bound,
                    # never restart that full allowance near the deadline.
                    bounds['planning_budget_s']=remaining_s
                values=self.numerics.call('solve_path',goals=stage['goals'],gripper=stage['gripper'],full22=full,held16=held,
                    scene=bounds)
            except Exception as exc:
                import json
                diagnostic=numerical_failure_detail(exc)
                import re
                if isinstance(exc,TimeoutError) or re.search(r'(?:^|: )(?:TimeoutError|socket.timeout):',str(exc)):
                    diagnostic=dict(error_type=type(exc).__name__,code='numerical_rpc_timeout',
                        metrics=dict(reason='numerical_transport_deadline_exceeded',
                                     recovery_hint='inspect_numerical_worker_before_retry'))
                elif diagnostic['code'] in ('RuntimeError','ConnectionError','BrokenPipeError','EOFError'):
                    diagnostic=dict(error_type=type(exc).__name__,code='numerical_worker_unavailable',
                        metrics=dict(reason=diagnostic['code'],recovery_hint='inspect_numerical_worker_before_retry'))
                metrics=diagnostic['metrics']
                if metrics.get('frame')=='ik_world' and scene.eef_tip_reference is not None:
                    # Solver residuals refer to the IK EEF. Give the model
                    # target/fit poses in its native TCP frame, preserving the
                    # EEF residual label and the fit's non-executable status.
                    native=(transform(scene.diagnostic_native_from_world)
                            if scene.diagnostic_native_from_world is not None
                            else np.linalg.inv(self.frames.world_native))
                    tip=transform(scene.eef_tip_reference)
                    try:
                        target=native@transform(metrics['goal'])@tip
                        fitted=(native@transform(metrics['best_fit_eef_pose'])@tip
                                if metrics.get('best_fit_eef_pose') is not None else None)
                    except (KeyError,TypeError,ValueError):
                        pass  # A malformed optional diagnostic cannot alter the original failure.
                    else:
                        metrics.update(frame=self.frames.native_frame,target_tcp_pose=target.ravel().tolist())
                        if fitted is not None:
                            metrics.update(best_fit_tcp_pose=fitted.ravel().tolist(),
                                target_minus_fit_tcp_m=(target[:3,3]-fitted[:3,3]).tolist())
                        metrics.pop('goal',None);metrics.pop('best_fit_eef_pose',None)
                if stage.get('route_family'):
                    metrics['route_family']=stage['route_family']
                detail=dict(stage_index=stage_index,diagnostic=diagnostic)
                raise HarnessError('numerical_stage_failed:'+json.dumps(detail,separators=(',',':'))) from None
            if not values:raise HarnessError('empty_numerical_stage')
            if getattr(scene,'local_collision_evidence',None) is not None:
                from .skills import check_motion_route
                before=self.numerics.call('forward_pose',full22=full,held16=held)
                path=self.numerics.call('forward_path',full22=full,targets=values)
                check_motion_route(scene,path,start=before,skill=intent.skill,route_family='continuous_tcp')
            if stage.get('name')=='pregrasp_open' and any(
                    not np.array_equal(np.asarray(q)[:15],np.asarray(held)[:15])
                    or q[15]!=scene.release_opening for q in values):
                raise HarnessError('numerical_pregrasp_open_must_hold_arm')
            if stage.get('name')=='release_on_support' and any(
                    not np.array_equal(np.asarray(q)[:15],np.asarray(held)[:15])
                    or q[15]!=scene.release_opening for q in values):
                raise HarnessError('numerical_supported_release_must_hold_arm')
            if stage.get('target_contact_hold'):
                # Reopening cannot acquire permission to translate within the
                # target. Only the gripper coordinate may change in this stage.
                if any(not np.array_equal(np.asarray(q)[:15],np.asarray(held)[:15])
                       or q[15]!=scene.release_opening for q in values):
                    raise HarnessError('numerical_regrasp_open_must_hold_arm')
            if stage.get('target_contact_departure'):
                from .contact_departure import target_departure_clear
                before=self.numerics.call('forward_pose',full22=full,held16=held)
                path=self.numerics.call('forward_path',full22=full,targets=values)
                if (any(q[15]!=scene.release_opening for q in values)
                        or not target_departure_clear(scene,[before,*path])):
                    raise HarnessError('actual_IK_target_departure_not_proven')
            if stage.get('uncertain_grasp_observation'):
                from .grasp_observation import check_actual_path
                before=self.numerics.call('forward_pose',full22=full,held16=held)
                path=self.numerics.call('forward_path',full22=full,targets=values)
                check_actual_path(scene,[before,*path],values,held,self.fixture)
            geometry_trace.append(dict(start_index=len(targets),end_index=len(targets)+len(values),
                                       goals=stage['goals'],gripper=stage['gripper'],
                                       semantic_stage=stage.get('name'),
                                       posture_prior_source=self.posture_prior['source_run'] if self.posture_prior and redundancy is None
                                           and stage.get('posture_reference_policy')!='measured_stage_start' else None,
                                       redundancy_preference=redundancy,
                                       resume_request_id=intent.resume_request_id))
            for name in ('target_contact_hold','target_contact_departure','continuous_return_transit','uncertain_grasp_observation'):
                if stage.get(name):geometry_trace[-1][name]=True
            if stage.get('trajectory_source'):
                geometry_trace[-1].update(trajectory_source=stage['trajectory_source'],
                    posture_references_deg=stage['posture_references_deg'])
            if stage.get('route_family'):
                geometry_trace[-1]['route_family']=stage['route_family']
            if stage.get('approach_route'):
                geometry_trace[-1]['approach_route']=stage['approach_route']
            if stage.get('pregrasp_adjustment_object_m') is not None:
                geometry_trace[-1]['pregrasp_adjustment_object_m']=stage['pregrasp_adjustment_object_m']
            if stage.get('posture_reference_policy'):
                geometry_trace[-1].update(posture_reference_policy=stage['posture_reference_policy'],
                    posture_reference_deg=bounds['posture_references_deg'][0],
                    orientation_prior_source=stage.get('orientation_prior_source'))
            if stage.get('padding_advisory'):
                geometry_trace[-1]['padding_advisory']=stage['padding_advisory']
            if stage.get('pregrasp_distance_m') is not None:
                geometry_trace[-1]['pregrasp_distance_m']=stage['pregrasp_distance_m']
            targets.extend(values);held=values[-1]
        if len(targets)>100:raise HarnessError('semantic_path_exceeds_mailbox_limit')
        if getattr(scene,'grasp_candidate_descriptor',None) is not None and geometry_trace:
            geometry_trace[0].update(grasp_candidate_descriptor=scene.grasp_candidate_descriptor,
                                    grasp_candidate_variant=scene.grasp_candidate_variant)
        if lookahead is not None:geometry_trace[0]['grasp_route_lookahead']=lookahead
        if view is not None:geometry_trace[0]['pregrasp_wrist_view']=view
        trajectory=None
        if self.fixture.get('execution_mode')=='timed_reference':
            from .trajectory import schedule,reference_speed
            trajectory=schedule(targets,anchor,geometry_trace,reference_speed(self.fixture,intent.skill))
        return asdict(JointProposal(intent.request_id,intent.epoch,intent.scene_version,intent.calibration_id,
                     tuple(full),tuple(tuple(t) for t in targets),None if trajectory else self.fixture['joint_tolerance_deg'],
                     int(self.fixture['settle_ms']*1e6),self.fixture['gripper_tolerance'],tuple(geometry_trace),variant,
                     self.fixture.get('free_space_tracking_tolerance_deg')
                     if intent.skill in ('approach_grasp','transfer_above_hole','retreat','withdraw_reset','move_tcp') else None,trajectory))

    def check_waypoint(self,*,intent,proposal,observation,index,full22,measured16):
        """Check real tracking error in task space off the control thread.

        Timed references verify semantic boundaries in task space; legacy
        plans can use the same proof at free-space waypoints. Contact accuracy
        retains its registered stage tolerance. Freshness and epoch remain
        the owner's decision.
        """
        import numpy as np
        from .revalidation import stage_tolerances,pose_error
        from .skills import _motion_clear
        intent=SkillIntent.model_validate(intent);plan=JointProposal(**proposal)
        obs=Observation.model_validate(observation)
        tolerance=self.fixture.get('free_space_tracking_tolerance_deg')
        from .owner_bridge import valid_arrival_contract
        from .trajectory import reference_speed
        continuous=(plan.trajectory is not None and self.fixture.get('execution_mode')=='timed_reference'
                    and valid_arrival_contract(plan)
                    and plan.trajectory['reference_velocity_deg_s']==reference_speed(self.fixture,intent.skill))
        legacy=(plan.trajectory is None and intent.skill in ('approach_grasp','transfer_above_hole','retreat','withdraw_reset')
                and plan.free_space_tracking_tolerance_deg==tolerance
                and isinstance(tolerance,(int,float)) and 0<tolerance)
        if (not (continuous or legacy) or intent.skill not in self.task.allowed_skills
                or intent.task_hash!=digest(self.task) or intent.request_id!=plan.request_id
                or intent.epoch!=plan.epoch or intent.observation_id!=obs.observation_id
                or type(index) is not int or not 0<=index<len(plan.targets)):
            raise HarnessError('waypoint_tracking_scope_mismatch')
        measured=np.asarray(measured16,float);target=np.asarray(plan.targets[index],float)
        if measured.shape!=(16,) or not np.isfinite(measured).all():raise HarnessError('invalid_waypoint_feedback')
        error=float(np.max(np.abs(measured[7:14]-target[7:14])))
        following=plan.targets[min(index+1,len(plan.targets)-1)]
        poses=self.numerics.call('forward_path',full22=full22,targets=[target.tolist(),measured.tolist(),list(following)])
        nominal,actual,next_pose=[transform(p) for p in poses]
        entry=next((e for e in plan.geometry_trace if e['start_index']<=index<e['end_index']),{})
        threshold=stage_tolerances(self.fixture,intent.skill,entry.get('semantic_stage'))
        distance,angle=pose_error(nominal,actual)
        if self.fixture.get('eef_tip_reference') is not None:
            tip=transform(self.fixture['eef_tip_reference'])
            distance=max(distance,pose_error(nominal@tip,actual@tip)[0])
        if distance>threshold['goal_position_m'] or angle>threshold['rotation_rad']:
            return dict(accepted=False,reason='tracking_task_space_error',position_m=distance,rotation_rad=angle)
        # Revalidation binds and sweeps this owner reference before publishing.
        # A camera-time state is not the start of the executed reference path.
        anchor=plan.trajectory['anchor'] if continuous else obs.state16
        base=self.numerics.call('forward_pose',full22=plan.full_reference,held16=anchor)
        scene=self._scene(obs,plan.full_reference,anchor,base_eef=base,skill=intent.skill)
        from .grasp_selection import recorded_scene
        scene=recorded_scene(self,scene,obs,plan,intent)
        pregrasp_distance=plan.geometry_trace[0].get('pregrasp_distance_m') if plan.geometry_trace else None
        from .joint_motion import uses_joint_motion,reconstruction
        stages=(reconstruction(scene,intent,plan,pregrasp_distance_m=pregrasp_distance)
                if uses_joint_motion(self.fixture,intent,plan)
                else propose(intent.skill,scene,variant=plan.candidate_variant,
                             pregrasp_distance_m=pregrasp_distance)).get('stages',[])
        stage=next((s for entry,s in zip(plan.geometry_trace,stages)
                    if entry['start_index']<=index<entry['end_index']),None)
        if stage is None:
            if continuous and not stages and len(plan.targets)==1:
                return dict(accepted=True,reason='stationary_verification_reference',position_m=distance,rotation_rad=angle)
            raise HarnessError('waypoint_tracking_stage_missing')
        if entry.get('semantic_stage')!=stage.get('name'):
            raise HarnessError('waypoint_tracking_stage_mismatch')
        if stage.get('attachment_basis')=='predicted_for_collision_only':
            entry=next(e for e in plan.geometry_trace if e['start_index']<=index<e['end_index'])
            ordinal=stages.index(stage)
            contact=self.numerics.call('forward_pose',full22=plan.full_reference,held16=plan.targets[entry['start_index']-1])
            stage=rebase_support_lift(stage,contact,stages[ordinal-1]['goals'][-1],scene.base_object)
        sweeps=((nominal,actual,True),) if continuous else ((nominal,actual,True),(actual,next_pose,False))
        clear=all(_motion_clear(scene,a,b,stage.get('eef_object'),
            allow_target_contact=stage.get('allow_target_contact',False) and not stage.get('target_contact_departure'),
            support_lift=stage.get('support_contact_tolerance_m') is not None,
            support_contact_direction=stage.get('support_contact_direction','depart'),measurement_envelope=envelope)
            for a,b,envelope in sweeps)
        if clear and stage.get('uncertain_grasp_observation'):
            from .grasp_observation import require_closed
            from .contact_departure import contact_exit_geometry_clear
            require_closed(self.fixture,measured)
            path=self.numerics.call('forward_path',full22=plan.full_reference,targets=plan.targets)
            path[index]=actual.tolist()
            clear=contact_exit_geometry_clear(scene,[base,*path])
        if clear and index==len(plan.targets)-1 and getattr(scene,'local_collision_evidence',None) is not None:
            from .local_environment import require_departure_endpoint
            try:require_departure_endpoint(scene,actual)
            except HarnessError:clear=False
        return dict(accepted=bool(clear),reason='measured_free_space_tracking' if clear else 'tracking_sweep_blocked',
                    warning=('coarse_arrival_exceeds_contact_reference' if clear and threshold['free_space']
                             and distance>self.fixture.get('revalidation',{}).get('grasp_position_tolerance_m',float('inf')) else None),
                    error_deg=error,position_m=distance,rotation_rad=angle,index=index)

    def _scene(self,obs,full,held,*,base_eef=None,skill=None):
        import numpy as np
        from .local_environment import local_scope
        if obs.geometry_scope=='coarse_local_environment' or local_scope(obs) and skill in ('move_tcp','reobserve'):
            if skill not in ('move_tcp','reobserve'):
                raise HarnessError('local_environment_only_supports_world_observation_motion')
            from .local_environment import build_local_scene
            return build_local_scene(self,obs,full,held,base_eef=base_eef,motion=skill!='reobserve')
        if obs.geometry_scope=='coarse_fixture_return':
            from .return_scene import build_return_scene
            return build_return_scene(self,obs,full,held,base_eef=base_eef)
        objects={o.object_id:o for o in obs.objects}
        obj,hole=objects.get(self.task.object_id),objects.get(self.task.hole_id)
        development=self.task.goal=='approach_grasp'
        coarse=self.task.admission_profile=='development_coarse'
        from .geometry_context import FIXTURE_TRANSIT_SKILLS
        fixture_pose=(obs.fixture if coarse and self.fixture.get('independent_fixture_localization',False) else None)
        use_fixture=fixture_pose is not None and (skill in FIXTURE_TRANSIT_SKILLS or skill is None and (not hole or hole.pose is None))
        if not obj or obj.pose is None or (not development and not use_fixture and (not hole or hole.pose is None)):
            import json
            from .geometry_context import entity_status
            raise HarnessError('missing_geometry_entities:'+json.dumps(entity_status(self.task,obs),separators=(',',':')))
        if obj.shape!=self.task.shape or (not development and not use_fixture and hole.shape!=self.task.shape):
            raise HarnessError('object_hole_shape_mismatch')
        if obj.frame_id!=self.fixture['native_frame'] or (not development and not use_fixture and hole.frame_id!=obj.frame_id):
            raise HarnessError('native_frame_chain_not_registered')
        if fixture_pose is not None:
            import numpy as np
            reference=self.fixture.get('fixture_reference_hole')
            if (fixture_pose.frame_id!=self.fixture['native_frame'] or reference is None or
                    not np.allclose(transform(fixture_pose.reference_pose),transform(reference),rtol=0,atol=1e-9)):
                raise HarnessError('fixture_reference_identity_changed')
        if skill=='recover_grasp':
            from .grasp_observation import require_closed
            if not obs.state_valid_mask[15]:raise HarnessError('grasp_observation_gripper_feedback_missing')
            require_closed(self.fixture,obs.state16)
            require_closed(self.fixture,held)
        if development:
            reference=self.fixture['reference_gripper_closure']
            if (not obs.state_valid_mask[15]
                    or not abs(obs.state16[15]-reference)<=self.fixture['gripper_tolerance']):
                raise HarnessError('coarse_tip_opening_reference_changed')
        if base_eef is None:base_eef=self.numerics.call('forward_pose',full22=full,held16=held)
        object_pose=self.frames.world_pose(obj.pose)
        grasp=None if development else self.frames.object_ik_eef(self.fixture['object_grasp_eef'])
        if not development and self.fixture.get('grasp_pitch_source')=='success_posture_prior':
            import numpy as np
            prior=self.posture_prior
            if not prior or not prior.get('eef_poses',{}).get('grasp') or self.fixture.get('eef_tip_reference') is None:
                raise HarnessError('registered_success_grasp_orientation_required')
            reference=transform(prior['eef_poses']['grasp'])
            normal=object_pose[:3,2]
            pitch=np.arctan2(np.dot(normal,reference[:3,1]),np.dot(normal,reference[:3,2]))
            tip=transform(self.fixture['eef_tip_reference'])
            # Preserve the configured grasp point/TCP. Only the tool approach
            # pitch comes from the successful measured posture; closing X stays
            # aligned to a square edge, with the four registered yaw variants.
            point=(grasp@tip)[:3,3]
            c,s=np.cos(pitch),np.sin(pitch)
            grasp=grasp.copy();grasp[:3,:3]=[[1,0,0],[0,c,-s],[0,s,c]]
            grasp[:3,3]=point-grasp[:3,:3]@tip[:3,3]
        obstacles=self.frames.world_obstacles(self.fixture['obstacles'])
        if self.fixture.get('fixture_reference_hole') is not None and (fixture_pose is not None or hole is not None):
            # Coarse transit uses independent anchors. Fine engagement refines
            # that rigid body with the measured aperture: its goal and cutout
            # must use the SAME frame, not a precise hole in a coarse solid lid.
            import numpy as np
            reference=self.frames.world_pose(self.fixture['fixture_reference_hole'])
            measured=fixture_pose.pose if use_fixture else hole.pose
            moved=self.frames.world_pose(measured)@np.linalg.inv(reference)
            obstacles=[dict(o,pose=(moved@transform(o['pose'])).tolist())
                       if o.get('role')=='bucket' else o for o in obstacles]
        # A bumped target follows the new observation; its old fixture position
        # must neither become free space nor remain a phantom obstacle.
        if coarse and self.fixture.get('object_local_box'):
            obstacles=[dict(o,**self.fixture['object_local_box'],pose=self.frames.world_pose(obj.pose).tolist())
                if o.get('role')=='target_object' and o.get('object_id')==self.task.object_id else o
                for o in obstacles]
        from .support_motion import scene_support
        scene=SkillGeometry(base_eef=transform(base_eef),base_object=object_pose,
            support_measurement=scene_support(self,obs),
            diagnostic_native_from_world=np.linalg.inv(self.frames.world_native),
            base_hole=None if development else self.frames.world_pose(fixture_pose.pose if use_fixture else hole.pose),
            aperture_measured=not use_fixture,
            eef_object=None if obj.eef_object_pose is None else self.frames.ik_eef_object(obj.eef_object_pose),
            object_grasp_eef=grasp,
            uncertainty_m=(obj.planning_margin_m or float('inf')) if development else None if coarse else
                          max(obj.uncertainty_m or float('inf'),hole.uncertainty_m or float('inf')),
            obstacles=tuple(obstacles),
            **{k:self.fixture.get(k) for k in ('approach_clearance_m','transfer_clearance_m','insertion_depth_m',
                'attached_radius_m','effective_clearance_m','grasp_opening','release_opening')},
            goal=self.task.goal,eef_tip_reference=self.fixture.get('eef_tip_reference'),
            maximum_approach_translation_m=self.fixture.get('maximum_approach_translation_m'),
            tool_local_boxes=self.fixture.get('tool_local_boxes'),admission_profile=self.task.admission_profile,
            planning_margin_m=self.fixture.get('planning_margin_m'),
            object_planning_margin_m=self.fixture.get('object_planning_margin_m'),
            object_local_box=self.fixture.get('object_local_box'),object_held=obj.held,
            object_insertion_reference_height_m=self.fixture.get('object_insertion_reference_height_m',0.),
            target_object_id=self.task.object_id,
            transfer_staging_pose=(None if self.fixture.get('transfer_staging_pose') is None else
                self.frames.world_pose(self.fixture['transfer_staging_pose'])),
            transfer_rotation_path=self.fixture.get('transfer_rotation_path','shortest'),
            transfer_lift_rotation_fraction=self.fixture.get('transfer_lift_rotation_fraction',0.),
            approach_direction=self.fixture.get('approach_direction','vertical'),
            grasp_height_policy=self.fixture.get('grasp_height_policy','registered_point'),
            pregrasp_distances_m=self.fixture.get('pregrasp_distances_m'),
            success_trajectory=self.fixture.get('success_trajectory'),
            tool_padding_policy=self.fixture.get('tool_padding_policy','required'),
            grasp_pitch_offsets_deg=self.fixture.get('grasp_pitch_offsets_deg',()),
            parameterized_grasp=self.fixture.get('parameterized_grasp',False),
            transfer_policy=self.fixture.get('transfer_policy','hole_aligned'))
        if coarse and skill in ('approach_grasp','grasp','regrasp'):
            from .support_motion import current_support_departure
            from .gripper_state import observed_open
            scene=current_support_departure(scene,observed_open_gripper=bool(
                obs.state_valid_mask[15] and all(observed_open(opening,[0.,scene.release_opening+self.fixture['gripper_tolerance']])
                    for opening in (obs.state16[15],held[15]))))
        if skill=='regrasp' and coarse:
            from .skills import check_regrasp_support
            check_regrasp_support(scene)
        return scene


class VerifyService:
    def __init__(self,task:Task,*,clock=time.monotonic_ns,fixture=None,independent_grasp=False):
        self.task,self.clock=task,clock
        self.fixture=fixture or {}
        self.latest=None
        self.independent_grasp=independent_grasp
        self.grasp_measurement=None
        self.condition=threading.Condition()

    def dispatch(self,method,kwargs):
        if method=='update_grasp_measurement':
            with self.condition:
                self.grasp_measurement=kwargs
                self.condition.notify_all()
            return dict(measured_ns=kwargs['measured_ns'])
        if method=='update_scene':
            observation=Observation.model_validate(kwargs['observation'])
            if observation.synthetic:raise HarnessError('synthetic_scene_forbidden')
            with self.condition:
                self.latest=observation;self.condition.notify_all()
            return dict(observation_id=observation.observation_id)
        if method!='verify_skill':raise HarnessError('verification_method_unavailable')
        intent=SkillIntent.model_validate(kwargs['intent'])
        if (intent.task_id!=self.task.task_id or intent.task_hash!=digest(self.task)
                or intent.calibration_id!=self.task.calibration_id or intent.skill not in self.task.allowed_skills):
            raise HarnessError('verifier_scope_mismatch')
        # A short acquisition wait is not a motion gate. Persistent missing
        # evidence must return to the orchestrator instead of hiding there for
        # the whole task deadline. No old frame is made fresh by retrying.
        after=kwargs['after_ns'];deadline=time.monotonic()+2
        needs_can = self.independent_grasp and intent.skill in ('verify_grasp','recover_grasp')
        last_reason='grasp_held_evidence_missing' if needs_can else 'fresh_post_motion_scene_missing'
        last_result=None
        seen_measurements=set()
        with self.condition:
            while True:
                measurement = self.grasp_measurement
                if (intent.skill in ('grasp','regrasp','verify_grasp','recover_grasp')
                        and measurement and measurement['close_request_id']==kwargs.get('close_request_id')
                        and all(measurement['scope'].get(k)==getattr(intent,k) for k in
                            ('owner_instance_id','session_id','task_id','task_hash','epoch','request_id'))
                        and measurement['measured_ns']>after
                        and 0<=self.clock()-measurement['measured_ns']<=measurement['max_age_ns']
                        and measurement['held'] is not None):
                    return SkillResult(request_id=intent.request_id, lifecycle='completed',
                        outcome='success' if measurement['held'] else 'failure',
                        reason=measurement['reason'], evidence_ids=(measurement['evidence_id'],)).model_dump()
                obs=self.latest
                if not needs_can and obs is not None and obs.received_ns>after:
                    if obs.calibration_id!=intent.calibration_id:
                        raise HarnessError('verifier_scope_mismatch')
                    if (any(f.received_ns<=after for f in obs.frames) or intent.skill=='transfer_above_hole'
                            and (obs.state_received_ns is None or obs.state_received_ns<=after)):
                        last_reason='pre_motion_frame'
                        last_result=None
                    else:
                        try:
                            require_geometry(obs,now_ns=self.clock(),max_age_ns=500_000_000,goal=self.task.goal,
                                             admission_profile=self.task.admission_profile,
                                             skill=intent.skill,verification=True,hole_id=self.task.hole_id)
                        except ValueError as exc:
                            last_reason='post_motion_scene_not_ready:'+str(exc)
                            last_result=None
                        else:
                            # A new scene revision with the same object-camera
                            # frames is not new visual evidence. In particular,
                            # changing only held/pose metadata cannot turn the
                            # same inconclusive frame into a verified grasp.
                            key=(obs.acquisition_instance,tuple((f.camera,f.stream,
                                f.source_epoch,f.publisher_id,f.sequence,f.source_ns)
                                for f in sorted(obs.frames,key=lambda frame:(frame.camera,frame.stream))
                                if obs.geometry_camera is None or f.camera==obs.geometry_camera))
                            if key not in seen_measurements:
                                seen_measurements.add(key)
                                result=self._verify_observation(intent,obs)
                                if not needs_semantic_evidence(result):
                                    return result.model_dump()
                                last_result=result
                                last_reason=result.reason
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    if last_result is not None:return last_result.model_dump()
                    return SkillResult(request_id=intent.request_id,lifecycle='completed',
                        reason=last_reason).model_dump()
                self.condition.wait(remaining)

    def _verify_observation(self,intent,obs):
        """Evaluate one admitted measurement; waiting owns no robot commands."""
        if intent.skill=='release_supported':
            from .support_motion import scene_support,verify_support_release
            return verify_support_release(self.task,obs,scene_support(self,obs),intent.request_id)
        if intent.skill=='recover_grasp':
            return verify('verify_grasp',self.task,obs,intent.request_id)
        if intent.skill in ('verify_grasp','inspect_in_hand','transfer_above_hole','verify_placed'):
            return verify(intent.skill,self.task,obs,intent.request_id)
        if intent.skill=='withdraw_reset':
            hole=next((o for o in obs.objects if o.object_id==self.task.hole_id),None)
            return SkillResult(request_id=intent.request_id,lifecycle='completed',
                outcome='success' if hole and hole.evidence_ids else 'pending_review',
                reason='owner_return_arrival_and_fresh_fixture',
                evidence_ids=hole.evidence_ids if hole else ())
        obj=next((o for o in obs.objects if o.object_id==self.task.object_id),None)
        outcome,reason='pending_review','postcondition_evidence_missing';evidence=()
        if obj and obj.evidence_ids and obj.visible:
            evidence=obj.evidence_ids
            if intent.skill=='insert':
                if obj.inserted is not None:
                    outcome='success' if obj.inserted and obj.held and obj.inserted_hole_id==self.task.hole_id else 'failure'
                    reason='visual_insertion_postcondition'
            elif intent.skill=='release':
                if obj.held is not None:
                    outcome='success' if not obj.held else 'failure';reason='visual_release_postcondition'
            else:
                outcome,reason='success','owner_arrival_and_fresh_scene'
        return SkillResult(request_id=intent.request_id,lifecycle='completed',outcome=outcome,
                           reason=reason,evidence_ids=evidence)
