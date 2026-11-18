"""Observed prefixes are candidates, never retroactively executed Python."""
import json
import time
from pathlib import Path

import pytest

from f1_harness.contracts import HarnessError, MotionParameters
from f1_harness.evidence import EvidenceStore
from f1_harness.memory import ExperienceStore,read_snapshot
from f1_harness.program_experience import (register_run_agent_programs,pending_agent_programs,
    run_program_evidence,validated_agent_programs,observed_grasp_segment)
from f1_harness.program_reuse import try_validation_program,try_verified_programs
from f1_harness.run_review import summarize_run
from f1_harness.sandbox import SandboxedArm
from test_program_trials import TrialBackend
from test_agent_program_memory import modify_rows


class MeasuredBackend(TrialBackend):
    """Synthetic owner protocol; actual sandbox and verifier, no model/robot."""
    def __init__(self,run,**kwargs):
        super().__init__(run,**kwargs);self.pending=None;self.done=False
    def execute_tool(self,name,input_dict):
        if name=='geometry_context':return dict(available=False,object_pose=[1.,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1])
        assert name=='execute_skill';self.steps+=1
        skill=input_dict['name'];request=self.name+'-'+str(self.steps)
        parameters=MotionParameters.model_validate(input_dict.get('parameters',{})).for_skill(skill)
        self.owner.event('skill_accepted',intent=dict(self.scope,request_id=request,skill=skill,
            parameters=parameters.model_dump(mode='json')))
        failed=skill=='transfer_above_hole';published=not failed and skill!='verify_grasp'
        if published:self.owner.event('harness_skill_first_publication',request_id=request,published_ns=time.monotonic_ns())
        result=dict(request_id=request,lifecycle='completed',outcome='failure' if failed else 'success',
            reason='synthetic_fixture',evidence_ids=[] if failed else [request+'-measured'],
            no_motion=not published,assisted=False,synthetic=True)
        self.owner.event('skill_result',skill=skill,result=result)
        self.done=skill=='verify_grasp';return result
    def call(self,method):
        assert method=='status'
        # No measured placement completion in this fixture; retain handoff.
        return dict(authorized=True,hold_requested=False,phase='observe',completed_skills=['verify_grasp'])


def discovery(run):
    backend=MeasuredBackend(run,instruction='放入孔');backend.freeze()
    for name,params in (('approach_grasp',dict(pitch_offset_deg=15.0,variant=2)),
                        ('grasp',{}),('verify_grasp',{}),('transfer_above_hole',{})):
        backend.execute_tool('execute_skill',dict(name=name,parameters=params))
    return backend


def test_partial_success_without_model_metadata_enters_existing_trial_and_reuse(tmp_path):
    root=tmp_path/'memory';run=tmp_path/'discovery';original=discovery(run)
    owner=run/'collected/owner/harness/trace.jsonl';raw=owner.read_bytes()
    registered=register_run_agent_programs(run,root)
    assert len(registered['candidate_ids'])==1 and registered['derived_archive_ids']
    assert not registered['unresolved']
    assert owner.read_bytes()==raw
    key=registered['candidate_ids'][0];memory=ExperienceStore(root,synthetic=True)
    candidate=memory._candidate(key)
    assert candidate['program_goal']=='verify_grasp' and original.task.goal=='placed'
    assert candidate['discovery_binding']['kind']=='owner_measured_segment'
    assert candidate['discovery_binding']['generated_source_executed_in_discovery'] is False
    assert "pitch_offset_deg=15.0" in candidate['agent_program']['source']
    assert 'transfer_above_hole' not in candidate['agent_program']['source']
    assert memory.snapshot()['count']==0
    item=pending_agent_programs(root,original.task,synthetic=True)[0]
    assert not pending_agent_programs(root,original.task.model_copy(update={'operator_instruction':'另一任务'}),synthetic=True)
    assert not pending_agent_programs(root,original.task,synthetic=False)
    validation=tmp_path/'validation';backend=MeasuredBackend(validation,instruction='放入孔')
    backend.freeze(item['trial_candidate']);store=EvidenceStore(validation/'planner')
    arm=SandboxedArm(backend,validation/'worker').start()
    try:
        result=try_validation_program(item,arm,backend,backend.task,store,
            authority_valid=lambda:True,finish_completed=lambda:None)
        assert result['completed'] is None and result['model_requests']==0
        assert backend.steps==3 and not result['attempts'][0]['result'].get('exception')
    finally:arm.close()
    validation_owner=validation/'collected/owner/harness/trace.jsonl'
    original_validation=validation_owner.read_bytes()
    modify_rows(validation_owner,lambda rows:next(r['intent']['parameters'] for r in rows
        if r.get('kind')=='skill_accepted').update(pitch_offset_deg=30.0))
    identity=run_program_evidence(validation)['identity']
    assert not memory.verify(key,validation_run=validation,scenario_id=identity['scenario_id'])['accepted']
    validation_owner.write_bytes(original_validation)
    reviewed=summarize_run(validation,root)['agent_programs']
    assert reviewed['candidate_ids']==[]
    promotion=reviewed['independent_validation'];assert promotion['promoted']==[key]
    snapshot=read_snapshot(Path(promotion['snapshot']['path']),synthetic=True)
    assert not validated_agent_programs(snapshot,original.task,synthetic=True)  # Not placed.
    assert validated_agent_programs(snapshot,original.task,goal='verify_grasp',synthetic=True)
    assert 'source_origin: owner_measured_segment' in next(iter(snapshot['files'].values()))
    reuse=tmp_path/'reuse';backend=MeasuredBackend(reuse,instruction='放入孔');backend.freeze()
    arm=SandboxedArm(backend,reuse/'worker').start();store=EvidenceStore(reuse/'planner')
    try:
        result=try_verified_programs(arm,backend,backend.task,snapshot,store,synthetic=True,
            authority_valid=lambda:True,finish_completed=lambda:None)
        assert result['model_requests']==0 and backend.steps==3
        assert result['completed'] is None  # Local grasp must not finish a placement task.
        assert not result['attempts'][0]['result'].get('exception')
    finally:arm.close()


@pytest.mark.parametrize('mutation',['missing_publication','publication_lie','missing_evidence','assisted',
    'failed_prefix','unknown_prefix','old_checkpoint','absolute_target','external_request','interrupted',
    'overlapping_calls','duplicate_result','late_duplicate_result','late_duplicate_request'])
def test_unsupported_or_unproven_observed_prefix_cannot_generate_executable_candidate(tmp_path,mutation):
    run=tmp_path/'discovery';discovery(run)
    def change(rows):
        accepted=next(r for r in rows if r.get('kind')=='skill_accepted')
        result=next(r for r in rows if r.get('kind')=='skill_result')
        if mutation=='missing_publication':rows[:]=[r for r in rows if r.get('kind')!='harness_skill_first_publication']
        if mutation=='publication_lie':result['result']['no_motion']=True
        if mutation=='missing_evidence':result['result']['evidence_ids']=[]
        if mutation=='assisted':result['result']['assisted']=True
        if mutation=='failed_prefix':result['result']['outcome']='failure'
        if mutation=='unknown_prefix':result['result']['lifecycle']='cancelled'
        if mutation=='old_checkpoint':accepted['intent']['resume_request_id']='old'
        if mutation=='absolute_target':accepted['intent'].update(skill='move_tcp',parameters=dict(target_tcp_pose=[1,0,0,.5,0,1,0,.5,0,0,1,.5,0,0,0,1]))
        if mutation=='external_request':rows.insert(rows.index(result),dict(kind='skill_accepted',intent=dict(accepted['intent'],request_id='foreign')))
        if mutation=='interrupted':rows.insert(rows.index(result),dict(kind='authority_revoked'))
        if mutation=='overlapping_calls':
            second=[r for r in rows if r.get('kind')=='skill_accepted'][1]
            rows.remove(second);rows.insert(rows.index(result),second)
        if mutation=='duplicate_result':rows.insert(rows.index(result),dict(result))
        if mutation=='late_duplicate_result':rows.append(dict(result))
        if mutation=='late_duplicate_request':rows.append(dict(accepted))
    modify_rows(run/'collected/owner/harness/trace.jsonl',change)
    result=register_run_agent_programs(run,tmp_path/'memory')
    assert not result['candidate_ids']
    assert not pending_agent_programs(tmp_path/'memory',MeasuredBackend(tmp_path/'unused').task,synthetic=True)


def test_derived_source_and_original_owner_evidence_rechecked_before_trial_and_promotion(tmp_path):
    run=tmp_path/'discovery';backend=discovery(run);root=tmp_path/'memory'
    key=register_run_agent_programs(run,root)['candidate_ids'][0]
    memory=ExperienceStore(root,synthetic=True);candidate=memory._candidate(key)
    from f1_harness.program_experience import observed_program_discovery_match
    changed=dict(candidate,agent_program=dict(candidate['agent_program'],source='not the observed code'))
    assert observed_program_discovery_match(changed,run_program_evidence(run)) is None
    with pytest.raises(HarnessError,match='independent_validation_required'):
        memory.verify(key,validation_run=run,scenario_id=candidate['discovery_scenario'])
    EvidenceStore(run/'collected/owner/harness').event('later_edit')
    assert not pending_agent_programs(root,backend.task,synthetic=True)
    validation=tmp_path/'unrelated';discovery(validation)
    with pytest.raises(HarnessError,match='observed_program_discovery_changed'):
        memory.verify(key,validation_run=validation,scenario_id='unrelated')


def test_no_local_goal_or_only_verification_does_not_invent_a_grasp_program(tmp_path):
    run=tmp_path/'verification';backend=MeasuredBackend(run);backend.freeze()
    backend.execute_tool('execute_skill',dict(name='verify_grasp'))
    assert not register_run_agent_programs(run,tmp_path/'memory')['candidate_ids']
    empty=tmp_path/'empty';backend=MeasuredBackend(empty);backend.freeze()
    assert observed_grasp_segment(run_program_evidence(empty)) is None


def test_observed_object_relative_geometry_survives_archive_without_absolute_retargeting(tmp_path):
    run=tmp_path/'discovery';backend=MeasuredBackend(run);backend.freeze()
    params=dict(grasp_tcp_pose=[1,0,0,.02,0,1,0,0,0,0,1,.03,0,0,0,1],
                approach_direction_object=[-1.,0.,0.],approach_distance_m=.05)
    backend.execute_tool('execute_skill',dict(name='approach_grasp',parameters=params))
    backend.execute_tool('execute_skill',dict(name='grasp'))
    backend.execute_tool('execute_skill',dict(name='verify_grasp'))
    root=tmp_path/'memory';result=register_run_agent_programs(run,root)
    assert len(result['candidate_ids'])==1
    candidate=ExperienceStore(root,synthetic=True)._candidate(result['candidate_ids'][0])
    assert candidate['discovery_binding']['segment']['calls'][0]['parameters']==params
    assert pending_agent_programs(root,backend.task,synthetic=True)
