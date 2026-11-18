"""Bounded, optional facts about rejected requests; no execution decisions."""
from .contracts import SKILLS,digest


REASONS=frozenset((
    'not_authorized','skill_not_authorized','placement_already_verified_return_only',
    'verification_checkpoint_or_budget_invalid','motion_resume_checkpoint_or_budget_invalid',
    'motion_resume_requires_measured_hold','preclosure_resume_requires_current_empty_evidence',
    'motion_already_attempted_use_registered_recovery','failed_insertion_requires_retreat_and_alignment',
    'failed_alignment_requires_retreat','owner_motion_wait','observation_retired_by_motion_wait',
    'observation_or_scene_mismatch','stale_observation','stale_observation_measurements',
    'observation_before_motion_wait_recovery','geometry_not_ready','full_scene_required_for_motion',
    'skill_busy','session_skill_budget_exhausted','regrasp_budget_exhausted','correction_budget_exhausted',
    'cartesian_motion_attachment_unknown','detached_object_requires_stationary_support_before_regrasp',
    'grasp_observation_requires_unresolved_grasp_verification',
)) | frozenset('skill_precondition:'+skill for skill in SKILLS)


class AdmissionDiagnostics:
    def __init__(self):
        self.reset()

    def reset(self):
        self.scope=None;self.seen=set();self.recorded=self.dropped=self.duplicates=0

    def status(self):
        return dict(recorded=self.recorded,dropped=self.dropped,duplicates=self.duplicates,
                    capacity=64,scope=self.scope,execution_effect=False)

    def record(self,engine,intent,reason,operation):
        try:self._record(engine,intent,reason,operation)
        except Exception:self.dropped+=1

    def _record(self,engine,intent,reason,operation):
        # Do not attribute another session, task or epoch to this task's history.
        if (reason not in REASONS or operation not in ('prepare','submit') or engine.task is None
                or intent.session_id!=engine.session or intent.owner_instance_id!=engine.instance
                or intent.epoch!=engine.epoch or intent.task_id!=engine.task.task_id
                or intent.task_hash!=digest(engine.task) or intent.calibration_id!=engine.task.calibration_id):
            return
        scope=(engine.task_generation,engine.epoch)
        if self.scope!=scope:
            self.scope=scope;self.seen.clear();self.recorded=self.dropped=self.duplicates=0
        # IDs are generated internally in normal runs; unexpected large inputs
        # cannot inflate diagnostics or become a second motion rejection.
        if any(len(value)>128 for value in (intent.request_id,intent.observation_id,intent.scene_version)):
            self.dropped+=1;return
        context=dict(phase=engine.phase,needs_correction=engine.needs_correction,
            completed_skills=sorted(engine.completed_skills & set(SKILLS)),
            corrections=engine.corrections,motion_recoveries=getattr(engine,'motion_recoveries',0),
            regrasps=engine.regrasps,task_requests=engine.task_requests,
            local_corrections=engine.task.local_corrections,regrasp_limit=engine.task.regrasp_limit)
        payload=dict(schema='f1-admission-rejection-v1',operation=operation,
            request_id=intent.request_id,skill=intent.skill,reason=reason,
            owner_instance_id=engine.instance,task_hash=intent.task_hash,
            task_generation=engine.task_generation,epoch=engine.epoch,
            observation_id=intent.observation_id,scene_version=intent.scene_version,
            state=context,no_new_motion=True,new_request_queued=False,
            request_had_publication=intent.request_id in engine.request_publications)
        key=digest(payload)
        if key in self.seen:self.duplicates+=1;return
        if len(self.seen)>=64:self.dropped+=1;return
        self.seen.add(key)
        try:
            writer=getattr(engine.store,'diagnostic_event',None)
            written=writer('skill_admission_rejected',**payload) if writer else None
        except Exception:
            written=None
        if written:self.recorded+=1
        else:self.dropped+=1
