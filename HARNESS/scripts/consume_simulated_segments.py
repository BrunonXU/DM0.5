"""Experimental reference timing consumption inside the FAKE replay only.

Keeps the original geometric proposal and its revalidation evidence immutable.
A separate receipt selects a fully checked conditional segment, never a whole
skill. This is an offline experiment seam, not a production owner extension or
permission to change a live command. Real state/IPC consumption remains separate.
"""
from copy import deepcopy
from dataclasses import asdict
import numpy as np

from f1_harness.contracts import digest
from f1_harness.trajectory import schedule,sample
from segment_candidates import state_binding,RecommendationInvalidated
from segment_preview import preview_segment
from f1_harness.command_forecast import sweep_anchor


class SimulatedSegmentChoices:
    def __init__(self,mode):
        if mode!='fake':raise ValueError('segment_consumption_requires_fake_engine')
        self.choices={};self.receipts=[]

    def install(self,*,output,context,current_context,cancelled,forecast_kwargs,
                geometry_trace,reference_speeds,recommendation):
        if cancelled():raise RecommendationInvalidated('candidate_cancelled_before_consumption')
        binding=state_binding(output,current_context())
        if binding!=state_binding(output,context) or binding!=recommendation.get('binding'):
            raise RecommendationInvalidated('candidate_changed_before_consumption')
        inputs=deepcopy(forecast_kwargs)
        inputs['targets']=np.asarray(inputs['targets']).tolist()
        if 'measured16' in inputs:inputs['measured16']=np.asarray(inputs['measured16']).tolist()
        expected=digest(dict(inputs=inputs,geometry_trace=geometry_trace,speeds=list(reference_speeds)))
        speed=recommendation.get('recommended_reference_speed')
        if (recommendation.get('schema')!='f1-offline-segment-recommendation-v1'
                or recommendation.get('candidate_set_sha256')!=expected
                or recommendation.get('execution_authorized') is not False
                or recommendation.get('whole_skill_verified') is not False
                or speed not in reference_speeds or speed not in (30,40,50,60)):
            raise ValueError('candidate_consumption_identity_mismatch')
        if speed>inputs['trajectory']['reference_velocity_deg_s']:
            raise ValueError('offline_choice_exceeds_original_reference_ceiling')
        trajectory=schedule(inputs['targets'],inputs['trajectory']['anchor'],geometry_trace,speed)
        forecast=preview_segment(output,**dict(inputs,trajectory=trajectory))
        last=recommendation['attempts'][-1];audits=last.get('audit')
        before=sweep_anchor(forecast['rows']);expected_samples=0
        for row in forecast['rows']:
            target=np.asarray(row['submitted16'])
            expected_samples+=max(1,int(np.ceil(np.max(abs(target[:14]-before[:14]))/.1)))
            before=target
        if (last.get('reference_speed')!=speed or last.get('plan_sha256')!=forecast['plan_sha256']
                or not isinstance(audits,list) or len(audits)!=1):
            raise ValueError('candidate_consumption_audit_mismatch')
        a=audits[0]
        if (a.get('skill')!=inputs['skill'] or a.get('request_id')!=inputs['request_id']
                or a.get('complete') is not True or a.get('passed') is not True
                or a.get('failures')!=[] or a.get('samples')!=expected_samples
                or a.get('max_joint_step_deg')!=.1):
            raise ValueError('candidate_consumption_requires_full_clear_audit')
        if cancelled() or state_binding(output,current_context())!=binding:
            raise RecommendationInvalidated('candidate_changed_during_consumption')
        key=(inputs['request_id'],inputs['start_index'])
        if key in self.choices:raise ValueError('segment_choice_already_installed')
        receipt=dict(schema='f1-simulated-reference-choice-v1',request_id=inputs['request_id'],
            skill=inputs['skill'],start_index=inputs['start_index'],end_index=forecast['end_index'],
            epoch=context['epoch'],source_plan_sha256=context['source_plan_sha256'],
            source_trajectory_sha256=digest(inputs['trajectory']),targets_sha256=digest(inputs['targets']),
            binding=binding,reference_speed=speed,selected_plan_sha256=forecast['plan_sha256'],
            first_publication_ns=inputs['first_publication_ns'],
            conditional_until_ns=forecast['rows'][-1]['virtual_ns'],
            audit_samples=expected_samples,sampled_count=0,simulation=True,
            production_authorized=False,whole_skill_verified=False)
        self.choices[key]=dict(trajectory=trajectory,receipt=receipt,forecast=forecast)
        self.receipts.append(receipt)
        return deepcopy(receipt)

    def sample_for_bridge(self,bridge,value,targets,*,start_index,elapsed_s):
        if bridge.engine.mode.value!='fake':raise ValueError('segment_consumption_requires_fake_engine')
        running=bridge.engine.running;proposal=bridge.proposal
        if running is None or running.skill not in ('transfer_above_hole','withdraw_reset'):
            return sample(value,targets,start_index=start_index,elapsed_s=elapsed_s)
        entry=self.choices.get((running.request_id,start_index))
        if entry is None:
            # Owner's first zero-time anchor is unchanged for every candidate.
            # The replay installs its choice before that sample is published.
            if elapsed_s!=0:raise ValueError('missing_segment_choice_before_advancement')
            return sample(value,targets,start_index=start_index,elapsed_s=elapsed_s)
        receipt=entry['receipt']
        if (bridge.engine.epoch!=receipt['epoch']
                or digest(asdict(proposal))!=receipt['source_plan_sha256']
                or digest(value)!=receipt['source_trajectory_sha256']
                or digest(np.asarray(targets).tolist())!=receipt['targets_sha256']):
            raise RecommendationInvalidated('consumed_segment_source_changed')
        if bridge.clock()>receipt['conditional_until_ns']:
            raise RecommendationInvalidated('simulated_choice_outlived_conditional_forecast')
        result=sample(entry['trajectory'],targets,start_index=start_index,elapsed_s=elapsed_s)
        if result[3]!=receipt['end_index']:raise ValueError('segment_choice_crossed_boundary')
        receipt['sampled_count']+=1
        return result
