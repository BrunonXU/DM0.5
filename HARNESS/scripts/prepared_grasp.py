"""FAKE replay only: one trusted worker-produced, non-authorizing grasp draft."""
from copy import deepcopy
import time
import numpy as np
from f1_harness.contracts import SkillIntent,Observation,HarnessError,digest
from f1_harness.skills import propose,rebase_support_lift
from f1_harness.trajectory import schedule,reference_speed
from f1_harness.revalidation import (_measurement_identity,_scene_unchanged,tolerances,
    require_planning_geometry,revalidate_candidate)

SCOPE=('task_id','task_hash','session_id','owner_instance_id','epoch','calibration_id')


class PreparedGraspService:
    def __init__(self, service, numeric):
        self.service,self.numeric=service,numeric
        self.prepared=None

    def dispatch(self,method,kwargs):
        if method=='prepare_grasp':return self.prepare(**kwargs)
        if method=='adopt_grasp':return self.adopt(**kwargs)
        raise HarnessError('prepared_method_forbidden')

    def prepare(self,*,key,parent_intent,parent_proposal,observation,full22):
        self.prepared=None
        parent=SkillIntent.model_validate(parent_intent);obs=Observation.model_validate(observation)
        if (parent.skill!='approach_grasp' or parent.resume_request_id or parent.verification_request_id
            or parent_proposal['request_id']!=parent.request_id or parent_proposal['epoch']!=parent.epoch
            or parent_proposal['scene_version']!=obs.scene_version
            or parent_proposal['calibration_id']!=parent.calibration_id
            or parent_proposal['full_reference']!=list(full22)):
            raise HarnessError('prepared_parent_binding_mismatch')
        # The observation remains the original observed scene. Only the solver's
        # hypothetical start is the parent's target, never a measured pose claim.
        held=list(parent_proposal['targets'][-1]);full=list(full22);full[7:14]=held[7:14]
        intent=parent.model_copy(update=dict(request_id='prepared-'+key,skill='grasp',
            candidate_start=0,excluded_candidates=(),resume_request_id=None,verification_request_id=None))
        start=time.perf_counter()
        result=self.service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=full,held16=held))
        record=dict(key=key,parent_intent=parent_intent,parent_plan_sha256=digest(parent_proposal),
            intent=intent,observation=obs,full22=full,proposal=result,prepared_at_ns=self.service.clock())
        self.prepared=record
        return dict(key=key,source_sha256=digest(result),source_proposal=result,
            source_intent=intent.model_dump(),source_observation_sha256=digest(obs),
            hypothetical_start16=held,prepared_at_ns=record['prepared_at_ns'],prepare_s=time.perf_counter()-start,
            execution_authorized=False)

    def adopt(self,*,key,intent,observation,full22,held16):
        # Single consumption, including rejected candidates. It is never a
        # reusable old authorization or a cached scene/collision verdict.
        record,self.prepared=self.prepared,None
        if record is None or record['key']!=key:raise HarnessError('prepared_candidate_missing')
        intent=SkillIntent.model_validate(intent);obs=Observation.model_validate(observation)
        source=record['proposal'];oldintent=record['intent'];oldobs=record['observation']
        if any(getattr(intent,k)!=getattr(oldintent,k) for k in SCOPE):raise HarnessError('prepared_scope_mismatch')
        if intent.skill!='grasp' or intent.resume_request_id or intent.verification_request_id:
            raise HarnessError('prepared_skill_not_eligible')
        if source['candidate_variant']<intent.candidate_start or source['candidate_variant'] in intent.excluded_candidates:
            raise HarnessError('prepared_candidate_excluded')
        service=self.service;fixture=service.fixture
        require_planning_geometry(obs,intent=intent,task=service.task,now_ns=service.clock(),max_age_ns=500_000_000)
        _measurement_identity(oldobs,obs)
        _scene_unchanged(oldobs,obs,task=service.task,threshold=tolerances(fixture,'grasp'),skill='grasp')
        full=list(full22);held=list(held16)
        if not np.array_equal(np.r_[full[:7],full[14:]],np.r_[record['full22'][:7],record['full22'][14:]]):
            raise HarnessError('prepared_non_right_reference_changed')
        if not np.array_equal(np.r_[held[:7],held[14:]],np.r_[source['trajectory']['anchor'][:7],source['trajectory']['anchor'][14:]]):
            raise HarnessError('prepared_inactive_or_gripper_changed')
        start=np.asarray(held,float);end=np.asarray(source['targets'][0],float)
        count=max(1,int(np.ceil(np.max(abs(end[:14]-start[:14]))/.1)))
        if count>100:raise HarnessError('prepared_connection_budget_fallback')
        for f in np.linspace(0,1,count+1):
            q=start+f*(end-start);self.numeric.numerics.mapping.check_action(q)
            self.numeric.numerics.gate.check_preventive(q,full)
        scene=service._scene(obs,full,held)
        stages=propose('grasp',scene,variant=source['candidate_variant'])['stages']
        if len(stages)!=len(source['geometry_trace']):raise HarnessError('prepared_topology_changed')
        result=deepcopy(source)
        for i,(stage,entry) in enumerate(zip(stages,result['geometry_trace'])):
            if stage.get('name')!=entry.get('semantic_stage') or stage['gripper']!=entry['gripper']:
                raise HarnessError('prepared_stage_changed')
            if stage.get('attachment_basis')=='predicted_for_collision_only':
                contact=service.numerics.call('forward_pose',full22=full,held16=result['targets'][entry['start_index']-1])
                stage=rebase_support_lift(stage,contact,stages[i-1]['goals'][-1],scene.base_object)
            entry['goals']=stage['goals']
            for name in ('candidate_evaluation','candidate_ranking','grasp_route_lookahead','posture_references_deg','padding_advisory'):
                entry.pop(name,None)
        result.update(request_id=intent.request_id,epoch=intent.epoch,scene_version=intent.scene_version,
            calibration_id=intent.calibration_id,full_reference=full)
        result['trajectory']=schedule(result['targets'],held,result['geometry_trace'],reference_speed(fixture,'grasp'))
        # Current observations must be newer than the ORIGINAL precompute.
        # This preliminary check is never returned as an execution proof. Owner
        # still obtains NEW post-adoption observations and revalidates normally.
        check=revalidate_candidate(service,intent=intent.model_dump(),proposal=result,
            original_observation=obs.model_dump(),observation=obs.model_dump(),full22=full,held16=held,
            after_ns=record['prepared_at_ns'])
        return dict(proposal=result,receipt=dict(key=key,source_sha256=digest(source),
            adopted_sha256=digest(result),source_observation_sha256=digest(oldobs),
            adopted_observation_sha256=digest(obs),prepared_at_ns=record['prepared_at_ns'],
            preliminary_geometry_sha256=digest(check),new_connection_points=count+1,
            targets_unchanged=result['targets']==source['targets'],execution_authorized=False,
            requires_post_adoption_owner_revalidation=True))
