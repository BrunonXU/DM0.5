"""Owner-side task authority and one-slot semantic mailbox.

Network dispatch never calls local_start, take_pending, complete or acknowledge_hold.
Only the existing control owner may call those methods in hardware mode.
"""
from __future__ import annotations

import threading
import time
import uuid
import math
from copy import deepcopy
from collections import OrderedDict
from pathlib import Path

from .contracts import HarnessError, Mode, Observation, SkillIntent, SkillResult, Task, digest, MOTION_SKILLS, VERIFICATION_SKILLS
from .evidence import EvidenceStore
from .observation import geometry_age_limit_ns, geometry_quality_warnings, stationary_admitted


UNPUBLISHED_FRESHNESS_REASONS = frozenset((
    'planning_observation_expired_before_publication',
    'planning_frame_expired_before_publication',
    'planning_state_expired_before_publication',
    'revalidation_proof_rejected:revalidation_proof_expired',
    'plan_revalidation_rejected:revalidation_measurements_expired',
    'numerical_plan_rejected:planning_measurements_expired',
))

UNPUBLISHED_SCENE_CODES=frozenset(('revalidation_object_or_hole_changed',
    'revalidation_scene_object_set_changed','revalidation_attachment_changed','revalidation_fixture_changed',
    'revalidation_scene_geometry_scope_changed','revalidation_scene_evidence_changed',
    'revalidation_target_measurement_unavailable','revalidation_observation_requires_open_gripper',
    'revalidation_goal_changed','revalidation_actual_path_sweep_blocked',
    'revalidation_endpoint_outside_remaining_clearance'))
UNPUBLISHED_SCENE_REASONS=frozenset(prefix+code for prefix in
    ('plan_revalidation_rejected:','revalidation_proof_rejected:') for code in UNPUBLISHED_SCENE_CODES)
UNPUBLISHED_REPLAN_REASONS=UNPUBLISHED_FRESHNESS_REASONS | UNPUBLISHED_SCENE_REASONS | {'owner_candidate_rejected'}
REPLANNABLE_SKILLS = MOTION_SKILLS


class TaskEngine:
    def __init__(self, store: EvidenceStore, *, mode=Mode.READ_ONLY, clock=time.monotonic_ns,
                 lease_ms=2000, observation_age_ms=500):
        self.store, self.mode, self.clock = store, Mode(mode), clock
        self.instance = uuid.uuid4().hex
        self.host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.epoch = 0
        self.lease_ns = lease_ms * 1_000_000
        self.observation_age_ns = observation_age_ms * 1_000_000
        self.session: str | None = None
        self.heartbeat_ns = 0
        self.task: Task | None = None
        self.task_generation = 0
        self.recording_identity = None
        self.program_trial = None
        self.authorized = False
        self.authority_revocation = None
        self.motion_wait = None
        self.observation_after_ns = 0
        self.retired_motion_observations = OrderedDict()
        self.observation: Observation | None = None
        self.live_observation: Observation | None = None
        self.live_observation_invalid_reason = None
        self.pending: SkillIntent | None = None
        self.running: SkillIntent | None = None
        self.deadline_ns = 0
        self.results: dict[str, SkillResult] = {}
        self.task_requests = 0
        self.assisted = False
        self.request_hashes: dict[str, str] = {}
        self.phase = "idle"
        self.regrasps = 0
        self.corrections = 0
        self.last_failure = ""
        self.needs_correction = False
        self.completed_skills = set()
        self.external_execution = None
        self.attempted_motion = set()
        self.request_publications = {}
        self.verification_checkpoints = {}
        self.approach_arrivals = {}
        self.active_grasp_selection = None
        self.grasp_contact = None
        self.hold_requested = False
        self.web_task_scope = None
        self.hold_confirmation = None
        self.hardware_ready = False
        self._lock = threading.RLock()
        from .admission_diagnostics import AdmissionDiagnostics
        self.admission_diagnostics=AdmissionDiagnostics()

    def open_session(self):
        with self._lock:
            if self.session is not None:
                raise HarnessError("session_busy")
            self.session = uuid.uuid4().hex
            self.heartbeat_ns = self.clock()
            return dict(session_id=self.session, owner_instance_id=self.instance, epoch=self.epoch)

    def _session(self, session_id):
        if not self.session or session_id != self.session:
            raise HarnessError("session_mismatch")

    def heartbeat(self, session_id):
        with self._lock:
            self._session(session_id)
            # A late heartbeat cannot resurrect revoked authority.
            self.tick()
            self.heartbeat_ns = self.clock()
            return self.status()

    def prepare_task(self, session_id, task):
        with self._lock:
            self._session(session_id)
            if self.authorized or self.pending or self.running or self.hold_requested:
                raise HarnessError("owner_busy")
            self.task = Task.model_validate(task)
            if (getattr(self, 'required_task_hash', None) is not None
                    and digest(self.task) != self.required_task_hash):
                self.task = None
                raise HarnessError('development_task_identity_mismatch')
            if self.task.shape != 'square':
                self.task = None
                raise HarnessError('first_demo_supports_square_only')
            if (getattr(self, "required_calibration_id", None) is not None
                    and self.task.calibration_id != self.required_calibration_id):
                self.task = None
                raise HarnessError("owner_calibration_identity_mismatch")
            self.phase, self.regrasps, self.corrections, self.last_failure = "prepared", 0, 0, ""
            self.motion_checkpoints, self.motion_recoveries, self.recovery_hold = {}, 0, False
            self.task_generation += 1
            self.admission_diagnostics.reset()
            self.web_task_scope = dict(session_id=self.session, epoch=self.epoch,
                                       task_hash=digest(self.task))
            self.hold_confirmation = None
            self.authority_revocation = None
            self.motion_wait = None
            self.observation_after_ns = 0
            self.retired_motion_observations.clear()
            self.live_observation = None
            self.live_observation_invalid_reason = None
            self.needs_correction = False
            self.completed_skills.clear()
            self.external_execution = None
            self.attempted_motion.clear()
            self.request_publications.clear()
            self.grasp_contact = None
            self.approach_arrivals.clear()
            self.active_grasp_selection = None
            self.task_requests = 0
            self.program_trial = None
            self.assisted = False
            self.store.event("task_prepared", task=self.task.model_dump(), task_hash=digest(self.task),
                             session_id=self.session, owner_instance_id=self.instance,
                             task_generation=self.task_generation)
            return dict(task_hash=digest(self.task), authorized=False, epoch=self.epoch,
                        task_generation=self.task_generation)

    def local_start(self, *, physical_event: bool, feedback_healthy: bool):
        """Call only from owner handling a fresh observed START event; never RPC."""
        with self._lock:
            self.tick()
            if not physical_event:
                raise HarnessError("fresh_local_start_and_prepared_task_required")
            return self._authorize(feedback_healthy, 'local_authorization')

    def authorize_web(self, *, request_id: str, run_id: str, feedback_healthy: bool):
        """Owner-thread consumption of an explicit, bound web request; never public RPC."""
        with self._lock:
            self.tick()
            if (not getattr(self, 'web_control_enabled', False)
                    or not isinstance(request_id, str) or not request_id
                    or run_id != (self.recording_identity or {}).get('owner_run_id')):
                raise HarnessError('explicit_bound_web_owner_required')
            if self.authorized:
                raise HarnessError('web_task_already_authorized')
            return self._authorize(feedback_healthy, 'web_authorization',
                                   web_request_id=request_id, owner_run_id=run_id)

    def _authorize(self, feedback_healthy, event, **fields):
        if not feedback_healthy or not self.task or not self.session:
            raise HarnessError('fresh_local_start_and_prepared_task_required')
        if self.hold_requested or self.running or self.pending:
            raise HarnessError('hold_confirmation_required')
        if self.clock() - self.heartbeat_ns > self.lease_ns:
            raise HarnessError('session_expired')
        if self.mode in (Mode.READ_ONLY, Mode.REPLAY):
            raise HarnessError('read_only_mode')
        if self.mode == Mode.HARDWARE and not self.hardware_ready:
            raise HarnessError('hardware_capability_unavailable')
        self.epoch += 1
        self.authorized = True
        self.hold_confirmation = None
        self.authority_revocation = None
        try:
            self.store.event(event, task_hash=digest(self.task), epoch=self.epoch,
                             task_id=self.task.task_id, session_id=self.session,
                             owner_instance_id=self.instance, task_generation=self.task_generation,
                             synthetic=self.mode == Mode.FAKE, **fields)
        except Exception:
            self.authorized=False
            raise
        return self.status()

    def update_observation(self, observation: Observation):
        with self._lock:
            if self.mode == Mode.HARDWARE and observation.synthetic:
                raise HarnessError("synthetic_hardware_observation")
            if self.mode == Mode.HARDWARE and observation.host_boot_id != self.host_boot_id:
                raise HarnessError("observation_host_clock_mismatch")
            self.observation = observation
            if self.motion_wait is not None:self._retire_motion_wait_observation(observation)

    def register_program_trial(self, session_id, observation_id, candidate=None):
        """Host-only evidence registration, never a motion or learning verdict.

        The owner freezes its own current measurement before accepting actions.
        No client may supply a replacement scene or label a run successful.
        """
        from .program_trials import build_program_trial
        with self._lock:
            self._session(session_id)
            self.tick()
            if not getattr(self,'program_trials_enabled',False):
                raise HarnessError('program_trial_registration_unavailable')
            if not self.authorized or self.hold_requested or self.task is None:
                raise HarnessError('program_trial_requires_current_authority')
            if self.program_trial is not None:
                if (self.program_trial.get('candidate')==candidate
                        and self.program_trial.get('observation',{}).get('observation_id')==observation_id):
                    return dict(registered=True,trial=self.program_trial)
                raise HarnessError('program_trial_already_frozen')
            if (self.task_requests or self.pending or self.running or self.attempted_motion
                    or self.external_execution):
                raise HarnessError('program_trial_must_precede_first_action')
            obs=self.observation
            if obs is None or obs.observation_id!=observation_id:
                raise HarnessError('program_trial_observation_changed')
            identity=self.recording_identity or {}
            if not identity.get('owner_run_id'):
                raise HarnessError('program_trial_owner_run_identity_missing')
            trial=build_program_trial(obs,self.task,scope=dict(owner_instance_id=self.instance,
                session_id=self.session,task_id=self.task.task_id,task_hash=digest(self.task),
                epoch=self.epoch,calibration_id=self.task.calibration_id),
                owner_run_id=identity['owner_run_id'],candidate=candidate)
            self.store.event('program_trial_frozen',trial=trial)
            self.program_trial=trial
            return dict(registered=True,trial=trial)

    def update_live_observation(self, observation: Observation, context: dict):
        """Fresh perception for candidate revalidation, independent of the request snapshot."""
        with self._lock:
            expected = self.status()['perception_context']
            scope = ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'task_generation', 'epoch', 'authorized')
            if not isinstance(context, dict) or any(context.get(k) != expected.get(k) for k in scope):
                raise HarnessError('live_scene_authority_scope_changed')
            if bool(context.get('placement_verified')) != expected['placement_verified']:
                raise HarnessError('live_scene_placement_stage_changed')
            if self.mode == Mode.HARDWARE and (observation.synthetic or observation.host_boot_id != self.host_boot_id):
                raise HarnessError('live_scene_hardware_identity_mismatch')
            self.live_observation = observation
            self.live_observation_invalid_reason = None

    def invalidate_live_observation(self, reason):
        with self._lock:
            previous = self.live_observation
            self.live_observation = None
            self.live_observation_invalid_reason = str(reason)[:256]
            if previous is not None:
                self.store.event('live_scene_invalidated', observation_id=previous.observation_id,
                                 reason=str(reason)[:160], epoch=self.epoch)
            return dict(invalidated=True)

    def _check_intent(self, intent: SkillIntent):
        self.tick()
        self._session(intent.session_id)
        if not self.authorized or not self.task:
            raise HarnessError("not_authorized")
        if intent.owner_instance_id != self.instance or intent.epoch != self.epoch:
            raise HarnessError("owner_or_epoch_mismatch")
        if intent.task_id != self.task.task_id or intent.task_hash != digest(self.task):
            raise HarnessError("task_scope_mismatch")
        if intent.calibration_id != self.task.calibration_id:
            raise HarnessError("calibration_mismatch")
        if intent.skill not in self.task.allowed_skills:
            raise HarnessError("skill_not_authorized")
        if 'verify_placed' in self.completed_skills and intent.skill != 'withdraw_reset':
            raise HarnessError('placement_already_verified_return_only')
        if intent.verification_request_id:
            checkpoint = self.verification_checkpoints.get(intent.verification_request_id)
            if (not checkpoint or checkpoint['skill'] != intent.skill
                    or checkpoint['epoch'] != self.epoch or checkpoint['task_hash'] != digest(self.task)
                    or checkpoint['attempts'] >= self.task.local_corrections
                    or checkpoint.get('claimed_by') not in (None, intent.request_id)):
                raise HarnessError('verification_checkpoint_or_budget_invalid')
        if intent.resume_request_id:
            checkpoint = getattr(self, 'motion_checkpoints', {}).get(intent.resume_request_id)
            if (not checkpoint or checkpoint['epoch'] != self.epoch or checkpoint['task_hash'] != digest(self.task)
                    or checkpoint['skill'] != intent.skill or checkpoint.get('claimed_by') not in (None, intent.request_id)
                    or not (intent.skill in ('approach_grasp', 'transfer_above_hole', 'retreat', 'withdraw_reset','move_tcp')
                            or intent.skill=='grasp' and checkpoint.get('preclosure') is True)
                    or getattr(self, 'motion_recoveries', 0) + self.corrections >= self.task.local_corrections):
                raise HarnessError('motion_resume_checkpoint_or_budget_invalid')
            if (not self.hold_confirmation or self.hold_confirmation['stage'] != 'measured_stationary'
                    or self.hold_confirmation['measured_at_ns'] < checkpoint['stalled_at_ns']):
                raise HarnessError('motion_resume_requires_measured_hold')
            if intent.skill=='grasp' and not (self.observation and any(
                    o.object_id==self.task.object_id and o.held is False for o in self.observation.objects)):
                raise HarnessError('preclosure_resume_requires_current_empty_evidence')
        if not intent.resume_request_id and not intent.verification_request_id and intent.skill in self.attempted_motion and intent.skill in ('approach_grasp','grasp','transfer_above_hole','release','release_supported','withdraw_reset','recover_grasp'):
            raise HarnessError('motion_already_attempted_use_registered_recovery')
        if intent.skill == 'insert' and self.needs_correction and not intent.verification_request_id:
            raise HarnessError('failed_insertion_requires_retreat_and_alignment')
        if intent.skill == 'align' and self.needs_correction and 'retreat' not in self.completed_skills and not intent.verification_request_id:
            raise HarnessError('failed_alignment_requires_retreat')
        prerequisites = {"grasp": "approach_grasp", "verify_grasp": "grasp", "recover_grasp": "grasp",
                         "inspect_in_hand": "verify_grasp", "transfer_above_hole": "verify_grasp",
                         "align": "inspect_in_hand", "insert": "align", "release": "insert",
                         "verify_placed": "release", "regrasp": "approach_grasp", "withdraw_reset": "verify_placed",
                         "release_supported": "verify_grasp"}
        required = prerequisites.get(intent.skill)
        # External VLA motion is never recorded as a successful Harness skill.
        # Its local handoff can admit in-hand inspection only from current,
        # measured attachment evidence under the same owner authorization.
        hybrid=getattr(self,'hybrid_skill_allowed',None)
        if hybrid is not None and not hybrid():
            raise HarnessError('hybrid_local_handoff_required')
        attachment = (hybrid is not None and intent.skill in ('inspect_in_hand','transfer_above_hole')
            and getattr(self,'hybrid_attachment_scope',None)==(self.instance,self.session,self.epoch)
            and self.observation is not None and any(
                o.object_id==self.task.object_id and o.held is True
                and o.eef_object_pose is not None and o.evidence_ids
                for o in self.observation.objects))
        external = self.external_execution or {}
        external_scope = (external.get('scope') == (self.instance,self.session,self.epoch)
            and external.get('task_hash') == digest(self.task)
            and not self.attempted_motion.intersection(('approach_grasp','grasp','regrasp')))
        target = next((o for o in self.observation.objects if o.object_id==self.task.object_id),None) if self.observation else None
        external_admission = external_scope and (
            intent.skill in ('verify_grasp','recover_grasp') and external.get('closed') is True
            or intent.skill=='verify_placed' and external.get('released') is True
            or intent.skill=='regrasp' and target is not None and target.held is False
               and target.stationary is True and bool(target.evidence_ids))
        if (external_scope and intent.skill=='approach_grasp'
                and (target is None or target.held is not False or target.stationary is not True
                     or not target.evidence_ids)):
            raise HarnessError('external_execution_attachment_unresolved')
        if required and required not in self.completed_skills and not attachment and not external_admission:
            raise HarnessError("skill_precondition:" + required)
        if self.motion_wait is not None:
            raise HarnessError('owner_motion_wait')
        if self._motion_wait_observation_key(intent) in self.retired_motion_observations:
            raise HarnessError('observation_retired_by_motion_wait')
        obs = self.observation
        if not obs or obs.observation_id != intent.observation_id or obs.scene_version != intent.scene_version:
            raise HarnessError("observation_or_scene_mismatch")
        age_limit=geometry_age_limit_ns(self.observation_age_ns,admission_profile=self.task.admission_profile,
                                        geometry_scope=obs.geometry_scope)
        def current(stamp):
            return stamp is not None and 0<=self.clock()-stamp and (age_limit is None or self.clock()-stamp<=age_limit)
        if not current(obs.received_ns):
            raise HarnessError("stale_observation")
        if self.mode is Mode.HARDWARE and obs.frames:
            now=self.clock()
            times=(obs.state_received_ns,*(f.received_ns for f in obs.frames))
            if any(stamp is None or stamp>now or age_limit is not None and now-stamp>age_limit for stamp in times):
                raise HarnessError('stale_observation_measurements')
        if self.observation_after_ns and (obs.received_ns<=self.observation_after_ns
                or obs.state_received_ns is None or obs.state_received_ns<=self.observation_after_ns
                or any(frame.received_ns<=self.observation_after_ns for frame in obs.frames)):
            raise HarnessError('observation_before_motion_wait_recovery')
        if (obs.calibration_id != intent.calibration_id or obs.faults
                or not stationary_admitted(obs,admission_profile=self.task.admission_profile)):
            raise HarnessError("geometry_not_ready")
        warnings=geometry_quality_warnings(obs,now_ns=self.clock(),max_age_ns=self.observation_age_ns,
                                           admission_profile=self.task.admission_profile)
        if warnings:
            self.store.event('observation_quality_warning',observation_id=obs.observation_id,
                             request_id=intent.request_id,warnings=list(warnings))
        if intent.skill=='recover_grasp':
            obj=next((o for o in obs.objects if o.object_id==self.task.object_id),None)
            if ('verify_grasp' not in self.attempted_motion
                    or obj is None or not obj.visible or not obj.evidence_ids
                    or obj.held is not None or obj.eef_object_pose is not None):
                raise HarnessError('grasp_observation_requires_unresolved_grasp_verification')
        if intent.skill=='regrasp' and self.last_failure=='attachment_lost':
            obj=next((o for o in obs.objects if o.object_id==self.task.object_id),None)
            if obj is None or obj.held is not False or obj.stationary is not True:
                raise HarnessError('detached_object_requires_stationary_support_before_regrasp')
        if intent.skill=='move_tcp':
            obj=next((o for o in obs.objects if o.object_id==self.task.object_id),None)
            if (obj is None or obj.held is True and obj.eef_object_pose is None
                    or obj.held is None and 'grasp' in self.attempted_motion):
                raise HarnessError('cartesian_motion_attachment_unknown')
        from .local_environment import local_scope,require_local_evidence
        local_observation_motion=local_scope(obs) and intent.skill in ('move_tcp','reobserve')
        if obs.geometry_scope=='coarse_local_environment' or local_observation_motion:
            require_local_evidence(self.task,obs,motion=intent.skill=='move_tcp')
            # A stationary read can report a confirmed hold without a fitted
            # object pose. Manipulation still needs the attachment geometry.
            if (intent.skill not in ('move_tcp','reobserve') and
                    any(o.object_id==self.task.object_id and o.held is True for o in obs.objects)):
                raise HarnessError('full_scene_required_for_motion')
            if (intent.skill not in ('move_tcp','reobserve') or intent.skill=='move_tcp' and (
                    intent.parameters.target_frame!='world' or intent.parameters.support_contact
                    or self.attempted_motion.intersection(('grasp','regrasp','recover_grasp'))
                    or self.external_execution)):
                raise HarnessError('local_environment_only_supports_unloaded_world_observation_motion')
        if intent.skill=='approach_grasp' and self.task.goal=='placed':
            obj=next((o for o in obs.objects if o.object_id==self.task.object_id),None)
            if obj is not None and (obj.held is True or obj.eef_object_pose is not None
                    or obj.held is None and self.attempted_motion.intersection(('grasp','regrasp','recover_grasp'))):
                raise HarnessError('approach_requires_current_unheld_target')
        if obs.geometry_scope=='coarse_object_verification' and not local_observation_motion and not intent.verification_request_id and intent.skill not in (
                'verify_grasp','inspect_in_hand','reobserve'):
            raise HarnessError('full_scene_required_for_motion')
        if self.pending or self.running or self.hold_requested:
            raise HarnessError("skill_busy")
        if self.task_requests >= 64:
            raise HarnessError("session_skill_budget_exhausted")
        if intent.skill == "regrasp" and self.regrasps >= self.task.regrasp_limit and not intent.verification_request_id:
            raise HarnessError("regrasp_budget_exhausted")
        if intent.skill == "align" and self.needs_correction and self.corrections + getattr(self, 'motion_recoveries', 0) >= self.task.local_corrections and not intent.verification_request_id:
            raise HarnessError("correction_budget_exhausted")

    def prepare_skill(self, intent):
        with self._lock:
            intent = SkillIntent.model_validate(intent)
            try:self._check_intent(intent)
            except HarnessError as exc:
                self.admission_diagnostics.record(self,intent,str(exc),'prepare')
                raise
            return dict(ready=True, request_id=intent.request_id, motion_authorized=self.authorized)

    def submit_skill(self, intent):
        with self._lock:
            intent = SkillIntent.model_validate(intent)
            self._session(intent.session_id)
            if intent.resume_request_id:
                checkpoint=getattr(self,'motion_checkpoints',{}).get(intent.resume_request_id)
                if checkpoint:
                    from .contracts import MotionParameters
                    original=MotionParameters.model_validate(checkpoint.get('parameters',{}))
                    if intent.parameters.supplied() and intent.parameters!=original:
                        raise HarnessError('resume_cannot_change_motion_parameters')
                    intent=intent.model_copy(update={'parameters':original})
            content_hash = digest(intent)
            if intent.request_id in self.results:
                if self.request_hashes[intent.request_id] != content_hash:
                    raise HarnessError("request_id_content_conflict")
                return self.results[intent.request_id].model_dump()
            try:
                self._check_intent(intent)
                if (getattr(self,'program_trials_enabled',False) and self.program_trial is None
                        and not self.task_requests and not self.external_execution):
                    try:self.register_program_trial(self.session,self.observation.observation_id)
                    except (HarnessError,ValueError) as exc:
                        # Learning metadata does not add a motion gate. Recheck
                        # authority if its read noticed an expired heartbeat.
                        self.store.event('program_trial_unavailable',reason=str(exc)[:160])
                    self._check_intent(intent)
            except HarnessError as exc:
                reason=str(exc)
                self.admission_diagnostics.record(self,intent,reason,'submit')
                # A valid request rejected before admission has a known result,
                # regardless of which precondition failed. Foreign scopes and
                # malformed identities remain protocol errors, not task receipts.
                if (self.task is None or intent.owner_instance_id!=self.instance
                        or intent.epoch!=self.epoch or intent.task_id!=self.task.task_id
                        or intent.task_hash!=digest(self.task)
                        or intent.calibration_id!=self.task.calibration_id):raise
                # The request was never admitted or queued. A lost reply can
                # be queried by this same ID without making an unknown action.
                result=SkillResult(request_id=intent.request_id,lifecycle='rejected',
                    reason=reason,no_motion=True,synthetic=self.mode==Mode.FAKE)
                self.results[intent.request_id]=result
                self.request_hashes[intent.request_id]=content_hash
                self.store.event('skill_not_admitted',request_id=intent.request_id,
                    reason=reason,no_motion=True,epoch=self.epoch,
                    observation_id=intent.observation_id,observation=self.observation.model_dump() if self.observation else None)
                return result.model_dump()
            self.request_hashes[intent.request_id] = content_hash
            if intent.verification_request_id:
                checkpoint = self.verification_checkpoints[intent.verification_request_id]
                checkpoint['claimed_by'] = intent.request_id
                checkpoint['attempts'] += 1
            if intent.resume_request_id:
                self.motion_checkpoints[intent.resume_request_id]['claimed_by'] = intent.request_id
                self.motion_recoveries = getattr(self, 'motion_recoveries', 0) + 1
            self.task_requests += 1
            self.pending = intent
            self.deadline_ns = self.clock() + intent.budget_ms * 1_000_000
            result = SkillResult(request_id=intent.request_id, lifecycle="accepted", synthetic=self.mode == Mode.FAKE)
            self.results[intent.request_id] = result
            self.store.event("skill_accepted", intent=intent.model_dump())
            return result.model_dump()

    def take_pending(self):
        """Owner control tick, after physical inputs and faults have been handled."""
        with self._lock:
            self.tick()
            if not self.authorized or self.hold_requested or self.motion_wait is not None or not self.pending:
                return None
            intent, self.pending = self.pending, None
            self.running = intent
            if not intent.verification_request_id and intent.skill in MOTION_SKILLS:
                self.verification_checkpoints.clear()
            self.running_bookkeeping=dict(attempted_motion=set(self.attempted_motion),
                corrections=self.corrections,regrasps=self.regrasps,needs_correction=self.needs_correction,
                last_failure=self.last_failure,completed_skills=set(self.completed_skills),
                active_grasp_selection=deepcopy(self.active_grasp_selection),approach_arrivals=deepcopy(self.approach_arrivals))
            self.attempted_motion.add(intent.skill)
            if intent.skill=='move_tcp' and not intent.verification_request_id:
                self.completed_skills.difference_update(('approach_grasp','transfer_above_hole','align','insert'))
                self.active_grasp_selection=None
                self.approach_arrivals.clear()
            if intent.skill in ('approach_grasp','regrasp') and not intent.verification_request_id:
                # A new physical approach/regrasp must establish its own result.
                self.active_grasp_selection = None
                self.approach_arrivals.clear()
            if intent.skill == "regrasp" and not intent.verification_request_id:
                self.regrasps += 1
            if intent.skill == "align" and self.needs_correction and not intent.verification_request_id:
                self.corrections += 1
                self.needs_correction = False
            self.results[intent.request_id] = self.results[intent.request_id].model_copy(update={"lifecycle": "running"})
            return intent

    def complete(self, request_id: str, result: SkillResult, *, alignment_after_ns=None):
        with self._lock:
            self.tick()
            if not self.running or request_id != self.running.request_id or not self.authorized:
                raise HarnessError("late_or_revoked_completion")
            if result.request_id != request_id or result.lifecycle != "completed":
                raise HarnessError("invalid_completion")
            if self.mode == Mode.HARDWARE and result.synthetic:
                raise HarnessError("synthetic_completion")
            if self.mode == Mode.HARDWARE and result.outcome == 'success' and not result.evidence_ids:
                raise HarnessError("success_evidence_required")
            skill = self.running.skill
            if self.running.verification_request_id:
                checkpoint = self.verification_checkpoints[self.running.verification_request_id]
                checkpoint['claimed_by'] = None
                if result.outcome == 'success':
                    self.verification_checkpoints.clear()
            if (result.no_motion and request_id in self.request_publications
                    or result.retryable and (skill not in REPLANNABLE_SKILLS
                                            or result.reason not in UNPUBLISHED_REPLAN_REASONS)):
                raise HarnessError('unpublished_replan_not_eligible')
            self.assisted = self.assisted or result.assisted
            if (result.no_motion and result.reason.startswith('numerical_plan_rejected:')
                    and request_id not in self.request_publications):
                # No physical attempt occurred. Restore the pre-request state
                # so a different parameterized plan can be submitted in this
                # same authorization; never apply this to partial execution.
                before=self.running_bookkeeping
                self.attempted_motion=set(before['attempted_motion'])
                self.corrections=before['corrections'];self.regrasps=before['regrasps']
                self.needs_correction=before['needs_correction']
                if skill in ('move_tcp','approach_grasp','regrasp'):
                    self.completed_skills=set(before['completed_skills'])
                    self.active_grasp_selection=deepcopy(before['active_grasp_selection'])
                    self.approach_arrivals=deepcopy(before['approach_arrivals'])
            # A completed owner request has a known publication ledger. A
            # verification continuation may prove an earlier motion succeeded
            # without publishing anything in this request. Cancellation or an
            # incomplete publication uses revoke(), and is not inferred here.
            measured_result = {'assisted': self.assisted}
            if self.mode is Mode.HARDWARE:
                measured_result['no_motion'] = request_id not in self.request_publications
            result=result.model_copy(update=measured_result)
            if skill == 'approach_grasp':
                source_request = self.running.verification_request_id or request_id
                selection = self.approach_arrivals.get(source_request)
                if result.outcome == 'success' and result.evidence_ids and self._grasp_selection_scope_matches(selection):
                    self.active_grasp_selection = dict(deepcopy(selection),
                        completion_request_id=request_id, verified_evidence_ids=list(result.evidence_ids))
                    self.store.event('harness_grasp_selection_committed', **self.active_grasp_selection)
                    self.approach_arrivals.clear()
                elif not (result.outcome == 'pending_review' and result.verification_request_id == source_request):
                    self.approach_arrivals.pop(source_request, None)
            if result.outcome == "success":
                self.phase = skill
                self.completed_skills.add(skill)
                if skill=='align' and alignment_after_ns is not None:
                    self.observation_after_ns=max(self.observation_after_ns,alignment_after_ns)
                if skill=='release_supported':
                    # A measured supported release starts another manipulation
                    # phase; it is never inserted/placed and never resets budgets.
                    self.completed_skills.difference_update(('approach_grasp','grasp','verify_grasp',
                        'inspect_in_hand','transfer_above_hole','align','insert','release','verify_placed','regrasp'))
                    self.attempted_motion.difference_update(('approach_grasp','grasp','recover_grasp',
                        'transfer_above_hole','release','release_supported'))
                    self.active_grasp_selection=None
                    self.approach_arrivals.clear();self.verification_checkpoints.clear()
                    getattr(self,'motion_checkpoints',{}).clear()
                    self.external_execution=None;self.needs_correction=False
                if skill=='move_tcp':
                    # A new free-space goal is not a placement milestone.
                    self.completed_skills.difference_update(('approach_grasp','transfer_above_hole','align','insert'))
                    self.active_grasp_selection=None
                    self.approach_arrivals.clear();self.verification_checkpoints.clear()
                    self.attempted_motion.discard('approach_grasp')
                    self.attempted_motion.discard('transfer_above_hole')
                if skill == "regrasp":
                    self.completed_skills = {"approach_grasp", "grasp", "regrasp"}
                    self.needs_correction = False
                    self.attempted_motion.discard("recover_grasp")
                    self.attempted_motion.discard('transfer_above_hole')
                if skill == "retreat":
                    self.completed_skills.discard("insert")
                    self.completed_skills.discard("align")
                if skill == "insert":
                    self.last_failure = ""
                    self.needs_correction = False
                if skill == 'align':
                    self.completed_skills.discard('retreat')
                    # Successful evidence-only continuation resolves the same
                    # alignment uncertainty as a normal completed alignment.
                    # It does not take_pending's new-motion correction branch.
                    self.needs_correction = False
                    self.last_failure = ''
            else:
                self.last_failure = result.reason or result.outcome
                if skill in ("align", "insert") and not result.no_motion:
                    self.needs_correction = True
            self.results[request_id], self.running = result, None
            self.store.event("skill_result", skill=skill, result=result.model_dump())

    def _grasp_selection_scope_matches(self, selection):
        if not isinstance(selection, dict) or not self.task or not self.authorized:
            return False
        expected = dict(owner_instance_id=self.instance, session_id=self.session,
            task_id=self.task.task_id, task_hash=digest(self.task), task_generation=self.task_generation,
            epoch=self.epoch, calibration_id=self.task.calibration_id, object_id=self.task.object_id)
        return all(selection.get(key) == value for key, value in expected.items())

    def record_approach_arrival(self, *, request_id, descriptor, candidate_variant,
                                observation, measured_at_ns):
        """Owner-only record after the final measured waypoint, before verification.

        This records which registered geometry arrived. It is not a successful
        approach until complete() receives the independent verifier's evidence.
        """
        with self._lock:
            intent = self.running
            if (not self.authorized or not self.task or not intent or intent.skill != 'approach_grasp'
                    or intent.request_id != request_id or intent.epoch != self.epoch
                    or intent.verification_request_id or request_id not in self.request_publications
                    or observation is None or observation.observation_id != intent.observation_id
                    or observation.scene_version != intent.scene_version
                    or observation.calibration_id != intent.calibration_id):
                raise HarnessError('approach_arrival_scope_mismatch')
            obj = next((item for item in observation.objects if item.object_id == self.task.object_id), None)
            offset = descriptor.get('pitch_offset_deg') if isinstance(descriptor, dict) else None
            valid_descriptor=(isinstance(descriptor,dict) and set(descriptor)=={'strategy','pitch_offset_deg'}
                and descriptor.get('strategy')=='tcp_pitch' and type(offset) in (int,float)
                and math.isfinite(offset) and abs(offset)<=45)
            if isinstance(descriptor,dict) and descriptor.get('strategy')=='object_tcp':
                from .contracts import MotionParameters
                from .motion_goals import grasp_descriptor
                try:
                    parsed=MotionParameters.model_validate({k:v for k,v in descriptor.items() if k!='strategy'})
                    valid_descriptor=grasp_descriptor(parsed) is not None
                except ValueError:valid_descriptor=False
            if (not valid_descriptor
                    or type(candidate_variant) is not int or candidate_variant not in range(4)
                    or obj is None or obj.pose is None or obj.shape != 'square'):
                raise HarnessError('invalid_approach_grasp_selection')
            selection = dict(schema='f1-owner-grasp-selection-v1', owner_instance_id=self.instance,
                session_id=self.session, task_id=self.task.task_id, task_hash=digest(self.task),
                task_generation=self.task_generation, epoch=self.epoch,
                calibration_id=self.task.calibration_id, object_id=self.task.object_id,
                source_request_id=request_id, source_observation_id=observation.observation_id,
                descriptor=deepcopy(descriptor), candidate_variant=candidate_variant,
                reference_object_pose=list(obj.pose), reference_object_frame_id=obj.frame_id,
                measured_at_ns=measured_at_ns)
            self.approach_arrivals[request_id] = selection
            self.store.event('harness_approach_grasp_arrived', **selection)

    def grasp_selection_for(self, intent, *, preview=False):
        """Trusted owner-to-planner context, never writable through SkillIntent/RPC."""
        with self._lock:
            selection = self.active_grasp_selection
            if (intent.skill != 'grasp' or not self._grasp_selection_scope_matches(selection)
                    or self.hold_requested or (not preview and (not self.running or self.running.request_id != intent.request_id))
                    or any(getattr(intent, key) != selection[key] for key in (
                        'owner_instance_id','session_id','task_id','task_hash','epoch','calibration_id'))
                    or 'approach_grasp' not in self.completed_skills):
                return None
            return deepcopy(selection)

    def record_skill_publication(self, request_id: str, *, publication_id: str, now_ns: int):
        """Owner-only evidence after a complete physical publication, never an RPC."""
        with self._lock:
            if (not publication_id or not self.running or self.running.request_id != request_id
                    or not self.authorized or self.running.epoch != self.epoch
                    or self.running.verification_request_id):
                raise HarnessError('publication_without_active_skill')
            evidence = self.request_publications.get(request_id)
            if evidence is None:
                evidence = dict(first_publication_id=publication_id, first_published_ns=now_ns,
                                publication_count=0)
                self.request_publications[request_id] = evidence
                self.store.event('harness_skill_first_publication', request_id=request_id,
                                 publication_id=publication_id, published_ns=now_ns, epoch=self.epoch)
            evidence['publication_count'] += 1

    def record_grasp_close_publication(self, request_id, *, publication_id, now_ns,
                                        target_closure, expected_eef_object):
        """Owner-published contact intent; never a claim that the object is held."""
        with self._lock:
            if (not self.authorized or not self.task or not self.running
                    or self.running.request_id != request_id
                    or self.running.skill not in ('grasp','regrasp')
                    or self.running.epoch != self.epoch
                    or request_id not in self.request_publications):
                raise HarnessError('grasp_close_without_publication')
            if self.grasp_contact and self.grasp_contact['request_id'] == request_id:
                return
            self.grasp_contact = dict(request_id=request_id, publication_id=publication_id,
                published_ns=now_ns, object_id=self.task.object_id,
                target_closure=float(target_closure), expected_eef_object=expected_eef_object)
            self.store.event('harness_grasp_close_published', **self.grasp_contact)

    def complete_stalled_motion(self, *, index, measured16, now_ns, preclosure=False,observation_view=None):
        """Owner-only known progress outcome; it preserves authority, then requests hold."""
        with self._lock:
            intent = self.running
            if (not intent or not self.authorized or intent.request_id not in self.request_publications
                    or intent.epoch != self.epoch):
                raise HarnessError('stalled_motion_without_known_progress')
            checkpoint = dict(skill=intent.skill, request_id=intent.request_id, epoch=self.epoch,
                              task_hash=digest(self.task), stalled_at_ns=now_ns, index=index,
                              measured16=list(measured16), claimed_by=None,preclosure=bool(preclosure),
                              parameters=intent.parameters.supplied())
            if not hasattr(self, 'motion_checkpoints'):
                self.motion_checkpoints = {}
            self.motion_checkpoints[intent.request_id] = checkpoint
            resumable = ((intent.skill in ('approach_grasp', 'transfer_above_hole', 'retreat', 'withdraw_reset','move_tcp')
                          or intent.skill=='grasp' and preclosure)
                         and getattr(self, 'motion_recoveries', 0) + self.corrections < self.task.local_corrections)
            self.complete(intent.request_id, SkillResult(request_id=intent.request_id, lifecycle='completed',
                outcome='failure', reason='motion_stalled', cancel_stage='cancel_requested',
                planning_diagnostic=(dict(code='motion_stalled',observation_view=observation_view)
                    if resumable and intent.skill=='approach_grasp' and observation_view else None),
                resume_request_id=intent.request_id if resumable else None, synthetic=self.mode == Mode.FAKE))
            self.hold_requested = True
            self.hold_confirmation = None
            self.recovery_hold = True
            self.observation_after_ns = max(self.observation_after_ns, now_ns)
            self.store.event('harness_motion_checkpoint', **checkpoint, resumable=resumable)

    def complete_grasp_miss(self, *, now_ns, observation_id):
        """Owner observed non-attachment during lift or loaded motion; hold first."""
        with self._lock:
            intent=self.running
            if (not intent or intent.skill not in ('grasp','regrasp','recover_grasp',
                    'transfer_above_hole','move_tcp','align','insert','retreat') or not self.authorized
                    or intent.epoch!=self.epoch or intent.request_id not in self.request_publications):
                raise HarnessError('grasp_miss_without_active_published_grasp')
            lost=intent.skill not in ('grasp','regrasp','recover_grasp')
            self.complete(intent.request_id,SkillResult(request_id=intent.request_id,lifecycle='completed',
                outcome='failure',reason='attachment_lost' if lost else 'grasp_not_attached',cancel_stage='cancel_requested',
                synthetic=self.mode==Mode.FAKE))
            if lost:
                self.completed_skills.difference_update(('verify_grasp','inspect_in_hand','transfer_above_hole','align','insert'))
                self.verification_checkpoints.clear()
                self.active_grasp_selection=None
            self.hold_requested=True
            self.hold_confirmation=None
            self.recovery_hold=True
            self.observation_after_ns=max(self.observation_after_ns,now_ns)
            self.store.event('harness_grasp_miss',request_id=intent.request_id,
                observation_id=observation_id,epoch=self.epoch,skill=intent.skill,
                remaining_lift_cancelled=not lost,remaining_loaded_motion_cancelled=lost)

    def reject_unpublished_for_replan(self, request_id: str, *, reason: str, candidate_variant=None,
                                      planning_diagnostic=None):
        """Owner-only terminal rejection; a new request must bind new observations."""
        with self._lock:
            self.tick()
            running = self.running
            if (not self.authorized or self.hold_requested or not running
                    or running.request_id != request_id or running.epoch != self.epoch
                    or running.skill not in REPLANNABLE_SKILLS
                    or reason not in UNPUBLISHED_REPLAN_REASONS
                    or request_id in self.request_publications):
                raise HarnessError('unpublished_replan_not_eligible')
            self.complete(request_id, SkillResult(request_id=request_id, lifecycle='completed',
                outcome='failure', reason=reason, no_motion=True, retryable=True,
                rejected_candidate_variant=candidate_variant,
                planning_diagnostic=planning_diagnostic,
                synthetic=self.mode == Mode.FAKE))
            for key,value in self.running_bookkeeping.items():setattr(self,key,value)
            if running.resume_request_id:
                self.motion_checkpoints[running.resume_request_id]['claimed_by'] = None
                self.motion_recoveries -= 1
            if reason in UNPUBLISHED_SCENE_REASONS:
                self.observation_after_ns=max(self.observation_after_ns,self.clock())
            self.store.event('unpublished_skill_retry_available', request_id=request_id,
                             skill=running.skill, reason=reason, epoch=self.epoch,
                             no_motion=True, requires_new_request=True)

    def revoke(self, reason="cancelled", *, preempted=False):
        with self._lock:
            had_action = self.authorized or self.pending or self.running
            if reason == 'manual_takeover' and self.task:
                self.assisted = True
                self.store.event('manual_intervention',task_id=self.task.task_id,task_hash=digest(self.task),epoch=self.epoch)
            self.authorized = False
            self.motion_checkpoints = {}
            self.verification_checkpoints.clear()
            self.approach_arrivals.clear()
            self.active_grasp_selection = None
            self.grasp_contact = None
            self.motion_recoveries = 0
            self.recovery_hold = False
            self.motion_wait = None
            self.retired_motion_observations.clear()
            self.live_observation = None
            self.live_observation_invalid_reason = None
            self.epoch += 1
            if had_action or self.authority_revocation is None:
                self.authority_revocation = dict(reason=str(reason)[:500], epoch=self.epoch,
                    task_hash=digest(self.task) if self.task else None, session_id=self.session)
            revoked=[]
            for intent in (self.pending, self.running):
                if intent:
                    result = SkillResult(request_id=intent.request_id,
                                         lifecycle="preempted" if preempted else "cancelled",
                                         outcome="interrupted", reason=reason, cancel_stage="cancel_requested",
                                         synthetic=self.mode == Mode.FAKE,assisted=self.assisted)
                    self.results[intent.request_id] = result
                    revoked.append(result)
            self.pending = self.running = None
            self.hold_requested = bool(had_action) or self.hold_requested
            # Revocation is a state transition even when its journal has failed.
            for result in revoked:
                self.store.event("skill_revoked", result=result.model_dump())
            self.store.event("authority_revoked", reason=reason, epoch=self.epoch)

    def _motion_wait_observation_key(self, value):
        return (self.instance,self.session,self.epoch,self.task_generation,digest(self.task),
                value.observation_id,value.scene_version,value.calibration_id)

    def _retire_motion_wait_observation(self, observation):
        if observation is None:return
        key=self._motion_wait_observation_key(observation)
        self.retired_motion_observations[key]=None
        self.retired_motion_observations.move_to_end(key)
        if len(self.retired_motion_observations)>256:
            self.retired_motion_observations.popitem(last=False)

    def begin_idle_motion_wait(self, now_ns):
        """Owner-only pause before any skill publication, with authority unchanged."""
        with self._lock:
            self.tick()
            if (not self.authorized or self.hold_requested or self.pending or self.running
                    or self.request_publications or not self.task or not self.session):
                raise HarnessError('idle_motion_wait_not_eligible')
            if self.motion_wait is None:
                self.motion_wait=dict(reason='stationary_output_timing',since_ns=now_ns,
                    owner_instance_id=self.instance,session_id=self.session,epoch=self.epoch,
                    task_hash=digest(self.task),task_generation=self.task_generation)
                self.store.event('owner_motion_wait_started',**self.motion_wait)
            self._retire_motion_wait_observation(self.observation)
            self.observation=self.live_observation=None
            self.observation_after_ns=max(self.observation_after_ns,now_ns)
            return dict(self.motion_wait)

    def end_idle_motion_wait(self, scope, now_ns):
        with self._lock:
            self.tick()
            if (not self.authorized or self.hold_requested or self.motion_wait!=scope
                    or self.pending or self.running or self.request_publications):
                return False
            self._retire_motion_wait_observation(self.observation)
            self.observation=self.live_observation=None
            self.observation_after_ns=max(self.observation_after_ns,now_ns)
            self.motion_wait=None
            self.store.event('owner_motion_wait_completed',epoch=self.epoch,
                             observation_after_ns=self.observation_after_ns)
            return True

    def acknowledge_closed_owner_stop(self, *, first_sample_ns: int, last_sample_ns: int):
        """Owner-only passive measurements; deliberately no publication ID."""
        with self._lock:
            if self.authorized or not self.hold_requested or not 0<=first_sample_ns<last_sample_ns<=self.clock():
                raise HarnessError('closed_owner_stop_not_eligible')
            for key,result in self.results.items():
                if result.cancel_stage!='none':
                    self.results[key]=result.model_copy(update={'cancel_stage':'measured_stationary'})
            self.hold_requested=self.recovery_hold=False
            self.hold_confirmation=dict(stage='measured_stationary',publication_id=None,
                source='passive_feedback_after_owner_close',first_sample_ns=first_sample_ns,
                measured_at_ns=last_sample_ns,owner_instance_id=self.instance,
                task_generation=self.task_generation,task_hash=digest(self.task) if self.task else None)
            self.store.event('closed_owner_stop_observed',**self.hold_confirmation)

    def acknowledge_hold(self, *, publication_id: str, measured_stationary: bool):
        if not publication_id:
            raise HarnessError("hold_publication_evidence_required")
        with self._lock:
            stage = "measured_stationary" if measured_stationary else "owner_hold_committed"
            previous_stage=(self.hold_confirmation or {}).get('stage')
            for key, result in self.results.items():
                if result.cancel_stage != "none":
                    self.results[key] = result.model_copy(update={"cancel_stage": stage})
            self.hold_requested = not measured_stationary
            if measured_stationary:
                self.recovery_hold = False
            self.hold_confirmation = dict(stage=stage, publication_id=publication_id,
                measured_at_ns=self.clock(), owner_instance_id=self.instance,
                task_generation=self.task_generation, task_hash=digest(self.task) if self.task else None)
            if stage!=previous_stage:
                self.store.event("hold_acknowledged", publication_id=publication_id, stage=stage)

    def tick(self):
        with self._lock:
            now = self.clock()
            if getattr(self.store,'error',None) and (self.authorized or self.pending or self.running):
                self.revoke('owner_journal_unavailable')
            elif self.authorized and now - self.heartbeat_ns > self.lease_ns:
                self.revoke("heartbeat_expired")
            elif (self.pending or self.running) and now >= self.deadline_ns:
                self.revoke("skill_deadline")

    def cancel_skill(self, session_id, request_id):
        with self._lock:
            self._session(session_id)
            if request_id not in self.results:
                raise HarnessError("unknown_request")
            if self.results[request_id].lifecycle in ("accepted", "running", "paused"):
                self.revoke("client_cancelled")
            return self.results[request_id].model_dump()

    def skill_result(self, session_id, request_id):
        with self._lock:
            self._session(session_id)
            self.tick()
            if request_id not in self.results:
                raise HarnessError("unknown_request")
            return self.results[request_id].model_dump()

    def finish_task(self, session_id):
        with self._lock:
            self._session(session_id)
            if self.pending or self.running:
                raise HarnessError("skill_busy")
            results = list(self.results.values())
            placement_success = 'verify_placed' in self.completed_skills
            reset_required = bool(self.task and 'withdraw_reset' in self.task.allowed_skills)
            reset_success = 'withdraw_reset' in self.completed_skills
            success = placement_success and (reset_success or not reset_required)
            goal_reached = bool(self.task and self.task.goal == "approach_grasp"
                                and self.phase == "approach_grasp" and results
                                and results[-1].outcome == "success")
            self.revoke("task_finished")
            return dict(outcome="success" if success else "pending_review", training_ready=False,
                        placement_success=placement_success, reset_required=reset_required, reset_success=reset_success,
                        assisted=self.assisted,autonomous_success=bool(success and not self.assisted),
                        goal=self.task.goal if self.task else None, goal_reached=goal_reached or bool(success),
                        admission_profile=self.task.admission_profile if self.task else None,
                        partial_task=bool(self.task and self.task.goal != "placed"),
                        synthetic=self.mode == Mode.FAKE)

    def close_session(self, session_id):
        with self._lock:
            self._session(session_id)
            self.revoke("session_closed")
            self.session = None
            return self.status()

    def execution_events(self,session_id,after=0,until=None):
        """RPC-thread journal export. Never called in the real-time owner tick."""
        import json
        self._session(session_id)
        if type(after) is not int or after<0:raise HarnessError('invalid_event_cursor')
        if hasattr(self.store,'flush'):self.store.flush()
        path=self.store.root/'trace.jsonl'
        if not path.exists():return dict(events=[],next=after)
        with path.open() as stream:lines=stream.readlines()
        if lines and not lines[-1].endswith('\n'):lines.pop()
        high_watermark=len(lines) if until is None else until
        if (type(high_watermark) is not int or not after<=high_watermark<=len(lines)):
            raise HarnessError('invalid_event_watermark')
        events=[];cursor=after;size=0
        for index in range(after,high_watermark):
            line=lines[index];row=json.loads(line);length=len(json.dumps(row).encode())
            if length>48000:
                # The immutable raw event is collected with the owner recording.
                # A transport-size limit must not pin the event cursor forever.
                import hashlib
                row=dict(kind='owner_event_reference',original_kind=row.get('kind'),
                    event_id=row.get('event_id'),monotonic_ns=row.get('monotonic_ns'),
                    artifact='harness/trace.jsonl',line_index=index,
                    sha256=hashlib.sha256(line.encode()).hexdigest(),bytes=len(line.encode()))
                length=len(json.dumps(row).encode())
            if size+length>48000 or len(events)>=50:break
            events.append(row);size+=length;cursor=index+1
        return dict(events=events,next=cursor,high_watermark=high_watermark,complete=cursor>=high_watermark)

    def status(self):
        with self._lock:
            active = self.running or self.pending
            published = bool(active and active.request_id in self.request_publications)
            # Requests describe intent. Until actual publication the observed
            # manipulation phase is still the previously established one.
            stage = active.skill if published else self.phase
            hybrid_phase = getattr(self, 'hybrid_perception_phase', None)
            if not active and hybrid_phase is not None:
                stage = hybrid_phase()
            context = dict(owner_instance_id=self.instance, session_id=self.session,
                           task_id=self.task.task_id if self.task else None,
                           task_hash=digest(self.task) if self.task else None,
                           task_generation=self.task_generation, epoch=self.epoch,
                           phase='observe' if stage in ('idle', 'prepared') else stage,
                           requested_skill=active.skill if active else None,
                           action_published=published,
                           authorized=self.authorized,
                           placement_verified=bool(self.authorized and self.task
                               and 'withdraw_reset' in self.task.allowed_skills
                               and 'verify_placed' in self.completed_skills),
                           request_id=active.request_id if active else None)
            if self.grasp_contact is not None and self.authorized:
                context['grasp_contact'] = deepcopy(self.grasp_contact)
            return dict(mode=self.mode.value, owner_instance_id=self.instance, epoch=self.epoch,
                        admitted_task_hash=getattr(self, 'required_task_hash', None),
                        web_control_enabled=getattr(self, 'web_control_enabled', False),
                        owner_monotonic_ns=self.clock(), observation_age_ns=self.observation_age_ns,
                        authorized=self.authorized, phase=self.phase, hold_requested=self.hold_requested,
                        recovery_hold=getattr(self, 'recovery_hold', False),
                        motion_recoveries=getattr(self, 'motion_recoveries', 0),
                        motion_wait=dict(self.motion_wait) if self.motion_wait else None,
                        observation_after_ns=self.observation_after_ns,
                        authority_revocation=dict(self.authority_revocation) if self.authority_revocation else None,
                        stop_scope=dict(self.web_task_scope) if self.web_task_scope else None,
                        hold_confirmation=dict(self.hold_confirmation) if self.hold_confirmation else None,
                        pending=self.pending.request_id if self.pending else None,
                        running=self.running.request_id if self.running else None,
                        task_hash=digest(self.task) if self.task else None,
                        corrections=self.corrections, regrasps=self.regrasps,
                        completed_skills=sorted(self.completed_skills),
                        attempted_motion=sorted(self.attempted_motion),
                        external_execution=deepcopy(self.external_execution),
                        admission_diagnostics=self.admission_diagnostics.status(),
                        assisted=self.assisted,
                        perception_context=context, recording_identity=self.recording_identity,
                        hardware_ready=self.hardware_ready)
