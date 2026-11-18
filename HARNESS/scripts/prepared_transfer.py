"""FAKE-only next-skill drafts, sharing one existing numerical worker.

Prediction belongs to a provisional SkillGeometry, never to an Observation.
Current held evidence and full revalidation are required before returning a draft.
No motion authority or robot connection is available in this module.
"""
from copy import deepcopy
from dataclasses import replace
import time
import numpy as np

from f1_harness.contracts import SkillIntent,Observation,HarnessError,digest
from f1_harness.skills import propose,candidate_variants
from f1_harness.candidate_ranking import order_variants
from f1_harness.revalidation import require_planning_geometry,_measurement_identity,revalidate_candidate
from f1_harness.trajectory import schedule,reference_speed
from prepared_grasp import PreparedGraspService,SCOPE

METHODS=('prepare_grasp','adopt_grasp','prepare_transfer','adopt_transfer')


class PreparedTransferService:
    def __init__(self,service,numeric):
        self.service,self.numeric=service,numeric
        self.prepared=None

    def prepare(self,*,key,parent_intent,parent_proposal,observation,full22):
        self.prepared=None
        parent=SkillIntent.model_validate(parent_intent);obs=Observation.model_validate(observation)
        service=self.service
        if (parent.skill!='grasp' or parent.resume_request_id or parent.verification_request_id
                or parent_proposal['request_id']!=parent.request_id or parent_proposal['epoch']!=parent.epoch
                or parent_proposal['scene_version']!=obs.scene_version
                or parent_proposal['calibration_id']!=parent.calibration_id
                or parent_proposal['full_reference']!=list(full22)):
            raise HarnessError('prepared_parent_binding_mismatch')
        require_planning_geometry(obs,intent=parent,task=service.task,now_ns=service.clock(),
                                 max_age_ns=500_000_000,candidate_only=True)
        stages=parent_proposal['geometry_trace'];targets=parent_proposal['targets']
        closures=[s for s in stages if s['gripper']==service.fixture['grasp_opening']]
        if len(closures)!=1:raise HarnessError('prepared_grasp_closure_missing')
        contact_index=closures[0]['end_index']-1
        if not 0<=contact_index<len(targets)-1:raise HarnessError('prepared_grasp_closure_missing')
        contact=service.numerics.call('forward_pose',full22=full22,held16=targets[contact_index])
        original=service._scene(obs,full22,obs.state16)
        if original.object_held is True:raise HarnessError('prepared_parent_already_holding')
        attached=np.linalg.inv(np.asarray(contact))@original.base_object
        held=list(targets[-1]);full=list(full22);full[7:14]=held[7:14]
        if held[15]!=service.fixture['grasp_opening']:raise HarnessError('prepared_grasp_closure_missing')
        endpoint=np.asarray(service.numerics.call('forward_pose',full22=full,held16=held))
        scene=replace(original,base_eef=endpoint,base_object=endpoint@attached,
                      eef_object=attached,object_held=True)
        intent=parent.model_copy(update=dict(request_id='prepared-'+key,skill='transfer_above_hole',
            candidate_start=0,excluded_candidates=(),resume_request_id=None,verification_request_id=None))
        variants,_=order_variants(intent.skill,scene,list(candidate_variants(intent.skill)),service.posture_prior,
            camera=service.fixture.get('candidate_wrist_camera'),preferences=service.fixture.get('candidate_order'))
        # One speculative attempt only; never hold up execution to search more.
        # Ordinary planner retains its full registered fallback if this fails.
        started=time.perf_counter()
        result=service._solve_candidate(intent,scene,held,full,variants[0])
        self.prepared=dict(key=key,parent_intent=parent_intent,parent_plan_sha256=digest(parent_proposal),
            intent=intent,observation=obs,full22=full,proposal=result,prepared_at_ns=service.clock())
        return dict(key=key,source_sha256=digest(result),source_proposal=result,
            source_intent=intent.model_dump(),source_observation_sha256=digest(obs),
            hypothetical_start16=held,hypothetical_attachment=attached.tolist(),
            prediction_is_observation=False,prepared_at_ns=self.prepared['prepared_at_ns'],
            prepare_s=time.perf_counter()-started,execution_authorized=False)

    def adopt(self,*,key,intent,observation,full22,held16):
        record,self.prepared=self.prepared,None
        if record is None or record['key']!=key:raise HarnessError('prepared_candidate_missing')
        intent=SkillIntent.model_validate(intent);obs=Observation.model_validate(observation)
        if any(getattr(intent,k)!=getattr(record['intent'],k) for k in SCOPE):raise HarnessError('prepared_scope_mismatch')
        if intent.skill!='transfer_above_hole' or intent.resume_request_id or intent.verification_request_id:
            raise HarnessError('prepared_skill_not_eligible')
        source=record['proposal'];service=self.service;fixture=service.fixture
        if source['candidate_variant']<intent.candidate_start or source['candidate_variant'] in intent.excluded_candidates:
            raise HarnessError('prepared_candidate_excluded')
        require_planning_geometry(obs,intent=intent,task=service.task,now_ns=service.clock(),max_age_ns=500_000_000)
        _measurement_identity(record['observation'],obs)
        full=list(full22);held=list(held16)
        if not np.array_equal(np.r_[full[:7],full[14:]],np.r_[record['full22'][:7],record['full22'][14:]]):
            raise HarnessError('prepared_non_right_reference_changed')
        if not np.array_equal(np.r_[held[:7],held[14:]],np.r_[source['trajectory']['anchor'][:7],source['trajectory']['anchor'][14:]]):
            raise HarnessError('prepared_inactive_or_gripper_changed')
        scene=service._scene(obs,full,held)
        stages=propose(intent.skill,scene,variant=source['candidate_variant'])['stages']
        if len(stages)!=len(source['geometry_trace']):raise HarnessError('prepared_topology_changed')
        result=deepcopy(source)
        for stage,entry in zip(stages,result['geometry_trace']):
            if stage.get('name')!=entry.get('semantic_stage') or stage['gripper']!=entry['gripper']:
                raise HarnessError('prepared_stage_changed')
            entry.update(goals=stage['goals'],eef_object=stage['eef_object'])
            for name in ('candidate_evaluation','candidate_ranking','posture_references_deg','padding_advisory'):
                entry.pop(name,None)
        result.update(request_id=intent.request_id,epoch=intent.epoch,scene_version=intent.scene_version,
                      calibration_id=intent.calibration_id,full_reference=full)
        result['trajectory']=schedule(result['targets'],held,result['geometry_trace'],reference_speed(fixture,intent.skill))
        start=np.asarray(held,float);end=np.asarray(result['targets'][0],float)
        count=max(1,int(np.ceil(np.max(abs(end[:14]-start[:14]))/.1)))
        if count>100:raise HarnessError('prepared_connection_budget_fallback')
        for fraction in np.linspace(0,1,count+1):
            q=start+fraction*(end-start)
            self.numeric.numerics.mapping.check_action(q);self.numeric.numerics.gate.check_preventive(q,full)
        check=revalidate_candidate(service,intent=intent.model_dump(),proposal=result,
            original_observation=obs.model_dump(),observation=obs.model_dump(),full22=full,held16=held,
            after_ns=record['prepared_at_ns'])
        return dict(proposal=result,receipt=dict(key=key,source_sha256=digest(source),
            adopted_sha256=digest(result),parent_plan_sha256=record['parent_plan_sha256'],
            source_observation_sha256=digest(record['observation']),adopted_observation_sha256=digest(obs),
            preliminary_geometry_sha256=digest(check),new_connection_points=count+1,
            targets_unchanged=result['targets']==source['targets'],execution_authorized=False,
            requires_post_adoption_owner_revalidation=True))


class PreparedNextSkillService:
    def __init__(self,service,numeric):
        self.grasp=PreparedGraspService(service,numeric)
        self.transfer=PreparedTransferService(service,numeric)

    def dispatch(self,method,kwargs):
        if method not in METHODS:raise HarnessError('prepared_method_forbidden')
        if method.startswith('prepare_'):
            self.grasp.prepared=None;self.transfer.prepared=None
        if method.endswith('_grasp'):return self.grasp.dispatch(method,kwargs)
        return (self.transfer.prepare if method=='prepare_transfer' else self.transfer.adopt)(**kwargs)
