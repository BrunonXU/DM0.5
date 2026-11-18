"""Scripted protocol tests: no physical evidence or manipulation strategy."""
import json
from pathlib import Path

import pytest

from f1_harness.contracts import Observation,Task,digest,HarnessError
from f1_harness.evidence import EvidenceStore
from f1_harness.memory import ExperienceStore,read_snapshot
from f1_harness.program_experience import (
    record_candidate,register_run_agent_programs,pending_agent_programs,
    run_program_evidence,validated_agent_programs,
)
from f1_harness.program_trials import (
    build_program_trial,validate_program_trial,owner_program_trial,
)
from f1_harness.run_review import summarize_run
from f1_harness.sandbox import SandboxedArm
from test_agent_program_memory import JournalBackend,proposal,modify_rows


def observed(name,stamp=10):
    return Observation(observation_id=name,acquisition_instance=name,received_ns=stamp,
        clock_domain='test_monotonic',calibration_id='cal',scene_version=name,synthetic=True,
        state16=(0.,)*16,state_valid_mask=(True,)*16,state_received_ns=stamp,
        frames=tuple(dict(camera='right',stream=stream,frame_id='camera',sequence=stamp,
            source_ns=None,received_ns=stamp,clock_domain='test_monotonic',width=2,height=2,
            encoding='rgb8' if stream=='rgb' else '16UC1',blob_sha256=sha*64)
            for stream,sha in (('rgb','a'),('depth','b'))))


class TrialBackend(JournalBackend):
    def __init__(self,run,*,instruction=''):
        self.owner=EvidenceStore(run/'collected/owner/harness')
        self.synthetic=True;self.steps=0;self.name=run.name;self.preflight_result=None
        self.task=Task(task_id=run.name,object_id='block',hole_id='hole',calibration_id='cal',
                       operator_instruction=instruction)
        self.scope=dict(owner_instance_id=run.name+'-owner',session_id=run.name+'-session',
            task_id=run.name,task_hash=digest(self.task),epoch=1,calibration_id='cal')
        self.owner.event('run_identity',synthetic=True,scenario_id=None,
                         scene_manifest_sha256=None,heldout=False)
        self.owner.event('task_prepared',task=self.task.model_dump(mode='json'),
            task_hash=digest(self.task),owner_instance_id=self.scope['owner_instance_id'],
            session_id=self.scope['session_id'])

    def freeze(self,candidate=None):
        trial=build_program_trial(observed(self.name),self.task,scope=self.scope,
                                  owner_run_id=self.name,candidate=candidate)
        self.owner.event('program_trial_frozen',trial=trial)
        return trial


def trial_run(run,*,candidate=None,freeze=True,instruction='',program=None):
    backend=TrialBackend(run,instruction=instruction);store=EvidenceStore(run/'planner')
    if freeze:backend.freeze(candidate)
    arm=SandboxedArm(backend,run/'worker').start()
    source=program or proposal('verify_grasp')
    try:
        reply,binding=arm.execute_candidate(source,store,call_id='call',request_id='model')
        assert not reply.result.get('exception'),reply.result
    finally:arm.close()
    record_candidate(store,source,call_id='call',request_id='model')
    return backend


def registered(tmp_path):
    root=tmp_path/'memory';run=tmp_path/'discovery';backend=trial_run(run)
    registration=register_run_agent_programs(run,root)
    assert len(registration['candidate_ids'])==1,registration
    return root,registration['candidate_ids'][0],backend.task


def test_source_content_does_not_become_new_layout_through_clock_or_run_name(tmp_path):
    backend=TrialBackend(tmp_path/'run')
    first=backend.freeze()
    second=build_program_trial(observed('new-observation',stamp=100),backend.task,
        scope=backend.scope,owner_run_id='other-run')
    assert first['scene_content_sha256']==second['scene_content_sha256']
    assert first['manifest_sha256']!=second['manifest_sha256']
    assert first['observation']['frames'][0]['source_ns'] is None
    assert not first['heldout'] and not second['generalization_proven']
    assert validate_program_trial(first)==first
    changed=dict(first,heldout=True)
    with pytest.raises(HarnessError,match='manifest_changed'):validate_program_trial(changed)


def test_initial_manifest_requires_actual_rgbd_and_matching_task_scope(tmp_path):
    backend=TrialBackend(tmp_path/'run')
    for observation,scope in ((observed('x').model_copy(update={'frames':()}),backend.scope),
            (observed('x'),dict(backend.scope,task_hash='wrong'))):
        with pytest.raises(HarnessError,match='initial_observation_required'):
            build_program_trial(observation,backend.task,scope=scope,owner_run_id='run')


def test_ordinary_runs_discover_then_independently_validate_same_layout_and_promote(tmp_path):
    root,key,task=registered(tmp_path)
    memory=ExperienceStore(root,synthetic=True)
    choices=pending_agent_programs(root,task,synthetic=True)
    assert [c['id'] for c in choices]==[key]
    run=tmp_path/'second';trial_run(run,candidate=choices[0]['trial_candidate'])
    owner=run/'collected/owner/harness/trace.jsonl';before=owner.read_bytes()
    result=summarize_run(run,root)['agent_programs']
    assert result['candidate_ids']==[]
    review=result['independent_validation']
    assert review['promoted']==[key]
    receipt=review['reviews'][0]
    assert receipt['accepted'] and receipt['validation_scope']=='independent_run'
    assert receipt['generalization_proven'] is False
    assert receipt['scene_content_sha256']==receipt['discovery_scene_content_sha256']
    assert owner.read_bytes()==before
    original=json.loads(before.splitlines()[0])
    assert original['scenario_id'] is None and original['heldout'] is False
    snapshot=read_snapshot(Path(review['snapshot']['path']),synthetic=True)
    assert len(validated_agent_programs(snapshot,task,goal='verify_grasp',synthetic=True))==1
    assert not pending_agent_programs(root,task,synthetic=True)
    assert not pending_agent_programs(root,task,synthetic=False)
    assert 'generalization_proven: false' in next(iter(snapshot['files'].values()))
    assert memory._candidate(key)['discovery_identity_source']=='owner_program_trial'


@pytest.mark.parametrize('change',['late','missing_prepared','wrong_task','duplicate','changed_scope','wrong_mode'])
def test_posthoc_or_unbound_trial_cannot_supply_missing_owner_identity(tmp_path,change):
    run=tmp_path/'run';trial_run(run)
    owner=run/'collected/owner/harness/trace.jsonl'
    def mutate(rows):
        trial=next(r for r in rows if r['kind']=='program_trial_frozen')
        if change=='late':rows.remove(trial);rows.append(trial)
        if change=='missing_prepared':rows[:]=[r for r in rows if r['kind']!='task_prepared']
        if change=='wrong_task':next(r for r in rows if r['kind']=='task_prepared')['task']['shape']='circle'
        if change=='duplicate':rows.append(dict(trial))
        if change=='changed_scope':next(r for r in rows if r['kind']=='skill_accepted')['intent']['epoch']=2
        if change=='wrong_mode':rows[0]['synthetic']=False
    modify_rows(owner,mutate)
    with pytest.raises(HarnessError,match='program_trial_'):run_program_evidence(run)
    registration=register_run_agent_programs(run,tmp_path/'memory')
    assert registration['archive_ids'] and not registration['candidate_ids']


def test_frozen_validation_must_name_the_exact_discovery_record(tmp_path):
    root,key,task=registered(tmp_path)
    item=pending_agent_programs(root,task,synthetic=True)[0]
    item['trial_candidate']['candidate_id']='c'*64
    run=tmp_path/'validation';trial_run(run,candidate=item['trial_candidate'])
    evidence=run_program_evidence(run)
    with pytest.raises(HarnessError,match='candidate_mismatch'):
        ExperienceStore(root,synthetic=True).verify(key,validation_run=run,
            scenario_id=evidence['identity']['scenario_id'])


def test_discovery_mode_cannot_stand_in_for_independent_validation(tmp_path):
    root,key,_=registered(tmp_path)
    run=tmp_path/'another';trial_run(run)
    evidence=run_program_evidence(run)
    with pytest.raises(HarnessError,match='candidate_mismatch'):
        ExperienceStore(root,synthetic=True).verify(key,validation_run=run,
            scenario_id=evidence['identity']['scenario_id'])


def test_frozen_source_cannot_claim_success_of_another_program(tmp_path):
    root,key,task=registered(tmp_path)
    item=pending_agent_programs(root,task,synthetic=True)[0]
    changed=proposal('verify_grasp')
    changed['source']+='\n\ndef unrelated_helper():\n    return 1\n'
    run=tmp_path/'different-program'
    trial_run(run,candidate=item['trial_candidate'],program=changed)
    evidence=run_program_evidence(run)
    receipt=ExperienceStore(root,synthetic=True).verify(key,validation_run=run,
        scenario_id=evidence['identity']['scenario_id'])
    assert not receipt['accepted']
    assert receipt['rejection_reason']=='agent_program_bound_goal_evidence_missing'


def test_pending_source_must_still_have_bound_success_and_match_task(tmp_path):
    root,key,task=registered(tmp_path)
    assert not pending_agent_programs(root,task.model_copy(update={'shape':'circle'}),synthetic=True)
    assert not pending_agent_programs(root,task.model_copy(update={'allowed_skills':('reobserve',)}),synthetic=True)
    path=tmp_path/'discovery/planner/trace.jsonl'
    modify_rows(path,lambda rows:rows.__setitem__(slice(None),
        [r for r in rows if r['kind']!='program_execution_started']))
    assert not pending_agent_programs(root,task,synthetic=True)


def test_same_owner_scope_cannot_label_itself_independent(tmp_path):
    root,key,task=registered(tmp_path)
    item=pending_agent_programs(root,task,synthetic=True)[0]
    with pytest.raises(HarnessError,match='independent_validation_required'):
        build_program_trial(observed('new-clock'),task,
            scope=item['trial_candidate']['discovery_scope'],owner_run_id='different-label',
            candidate=item['trial_candidate'])


def test_instruction_is_bound_in_selection_validation_and_promoted_reuse(tmp_path):
    root=tmp_path/'memory';discovery=tmp_path/'discovery'
    backend=trial_run(discovery,instruction='先扶正，再抓走')
    key=register_run_agent_programs(discovery,root)['candidate_ids'][0]
    item=pending_agent_programs(root,backend.task,synthetic=True)[0]
    assert item['id']==key
    different=backend.task.model_copy(update={'operator_instruction':'直接抓走'})
    assert not pending_agent_programs(root,different,synthetic=True)
    assert not pending_agent_programs(root,backend.task.model_copy(update={'operator_instruction':''}),synthetic=True)
    wrong=tmp_path/'different-task';trial_run(wrong,candidate=item['trial_candidate'],instruction='直接抓走')
    memory=ExperienceStore(root,synthetic=True)
    wrong_identity=run_program_evidence(wrong)['identity']
    assert not memory.verify(key,validation_run=wrong,scenario_id=wrong_identity['scenario_id'])['accepted']
    correct=tmp_path/'same-task';trial_run(correct,candidate=item['trial_candidate'],instruction='先扶正，再抓走')
    identity=run_program_evidence(correct)['identity']
    assert memory.verify(key,validation_run=correct,scenario_id=identity['scenario_id'])['accepted']
    snapshot=read_snapshot(Path(memory.promote(key)['path']),synthetic=True)
    assert validated_agent_programs(snapshot,backend.task,goal='verify_grasp',synthetic=True)
    assert not validated_agent_programs(snapshot,different,goal='verify_grasp',synthetic=True)
